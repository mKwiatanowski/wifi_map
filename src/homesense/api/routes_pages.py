"""HTML pages of the panel (Polish UI) and the root-scoped service worker.

Every page needs a session (require_user: 303 to /login). Data comes from the repositories, the
analysis helpers and the JSON API's view functions, so pages and /api/* never disagree; this module
contains no SQL. Mutations are done by the browser against /api/* (HTMX with the X-CSRF-Token header).
"""

# No `from __future__ import annotations`: FastAPI inspects these signatures at runtime.

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response
from fastapi.templating import Jinja2Templates

from homesense.api import routes_api
from homesense.api.deps import csrf_token, get_core, require_user
from homesense.app import HomeSenseApp
from homesense.core.clock import Clock
from homesense.core.models import Device, Person, normalize_mac
from homesense.core.timeutil import floor_hour, to_local
from homesense.monitoring import PAUSE_HOURS
from homesense.network import analysis
from homesense.patterns.profiles import PERSON_HOME, typical_times
from homesense.presence.engine import PersonState

Core = Annotated[HomeSenseApp, Depends(get_core)]
User = Annotated[str, Depends(require_user)]

router = APIRouter(dependencies=[Depends(require_user)])
public = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"

NAV = [
    ("now", "/", "Teraz"),
    ("persons", "/persons", "Osoby"),
    ("devices", "/devices", "Urządzenia"),
    ("map", "/map", "Mapa"),
    ("coverage", "/coverage", "Zasięg Wi-Fi"),
    ("network", "/network", "Sieć"),
    ("analytics", "/analytics", "Analityka"),
    ("alerts", "/alerts", "Alerty"),
    ("settings", "/settings", "Ustawienia"),
    ("privacy", "/privacy", "Prywatność"),
]
WEEKDAYS = ["Pn", "Wt", "Śr", "Cz", "Pt", "Sb", "Nd"]
WEEKDAYS_LONG = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
KIND_LABELS = {
    "phone": "Telefon",
    "laptop": "Laptop",
    "tablet": "Tablet",
    "desktop": "Komputer",
    "tv": "Telewizor",
    "iot": "IoT",
    "other": "Inne",
}
RADAR_KIND_LABELS = {
    "phone": "Telefony",
    "computer": "Komputery",
    "tablet": "Tablety",
    "tv": "Telewizory",
    "iot": "IoT",
    "unknown": "Nieznane",
}
RADAR_RINGS_M = [2, 5, 10, 20]
BAND_LABELS = {"2.4": "2,4 GHz", "5": "5 GHz", "6": "6 GHz", "unknown": "nieznane"}
SURVEY_WINDOWS_S = [30, 60, 120, 180]
SURVEY_DEFAULT_WINDOW_S = 60
DEVICE_FILTER_LABELS = {
    "all": "Wszystkie",
    "online": "Online",
    "new": "Nowe (24 h)",
    "unassigned": "Nieprzypisane",
    "unknown": "Nieznane",
}
SEVERITY_LABELS = {"info": "Informacja", "warning": "Ostrzeżenie", "critical": "Krytyczny"}
RULE_KIND_LABELS = {
    "new_device": "Nowe urządzenie",
    "arrive": "Przyjście",
    "leave": "Wyjście",
    "*": "Pozostałe",
}
CHANNEL_LABELS = {"log": "Log", "windows": "Windows", "telegram": "Telegram", "ntfy": "ntfy"}
GRAFANA_DASHBOARDS = [
    ("presence", "homesense-presence", "Obecność"),
    ("devices", "homesense-devices", "Urządzenia"),
    ("dns", "homesense-dns", "DNS"),
    ("traffic", "homesense-traffic", "Ruch"),
]
FEED_DAYS = 7
FEED_LIMIT = 20
NETWORK_HOURS = 48
MAX_TRAFFIC_POINTS = 300
BUCKET_MIN = routes_api.HISTORY_BUCKET_S // 60


# --- template helpers ---


def _as_datetime(value: datetime | str | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def configure_templates(templates: Jinja2Templates, tz: str, clock: Clock) -> None:
    """Jinja filters that present UTC datetimes (or ISO strings) in the configured local timezone."""

    def when(value: datetime | str | None) -> str:
        """HH:MM today, otherwise DD.MM HH:MM; '—' for None."""
        dt = _as_datetime(value)
        if dt is None:
            return "—"
        local = to_local(dt, tz)
        if local.date() == to_local(clock.now(), tz).date():
            return local.strftime("%H:%M")
        return local.strftime("%d.%m %H:%M")

    def ago(value: datetime | str | None) -> str:
        """Elapsed time like '2 h 5 min' (empty for None)."""
        dt = _as_datetime(value)
        if dt is None:
            return ""
        minutes = max(0, int((clock.now() - dt).total_seconds() // 60))
        days, rest = divmod(minutes, 24 * 60)
        hours, mins = divmod(rest, 60)
        if days:
            return f"{days} d {hours} h"
        if hours:
            return f"{hours} h {mins} min"
        return f"{mins} min"

    templates.env.filters["when"] = when
    templates.env.filters["ago"] = ago


def _render(request: Request, name: str, page: str | None = None, **context: Any) -> Response:
    core: HomeSenseApp = request.app.state.core
    base = {
        "csrf_token": csrf_token(request),
        "user": request.session.get("user"),
        "nav": NAV,
        "page": page,
        "unacked": core.repos.alerts.count_unacknowledged(),
        "monitoring": core.monitoring.state(),
        "pause_hours": PAUSE_HOURS,
    }
    response = request.app.state.templates.TemplateResponse(request, name, {**base, **context})
    response.headers["Cache-Control"] = "no-store"
    return response


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, what)


def _person_or_404(core: HomeSenseApp, person_id: int) -> Person:
    person = core.repos.persons.get(person_id)
    if person is None:
        raise _not_found("Nie ma takiej osoby")
    return person


def _device_or_404(core: HomeSenseApp, mac: str) -> Device:
    try:
        mac = normalize_mac(mac)
    except ValueError:
        raise _not_found("Nie ma takiego urządzenia") from None
    device = core.repos.devices.get(mac)
    if device is None:
        raise _not_found("Nie ma takiego urządzenia")
    return device


def _device_names(core: HomeSenseApp) -> dict[str, str]:
    return {d.mac: d.display_name for d in core.repos.devices.list()}


# --- Teraz ---


def _presence_cards(core: HomeSenseApp) -> list[dict]:
    states: dict[int, PersonState] = {s.person_id: s for s in core.presence.states()}
    names = _device_names(core)
    cards = []
    for person in core.repos.persons.list():
        st = states.get(person.id)
        cards.append(
            {
                "person": person,
                "home": bool(st and st.home),
                "since": st.since if st else None,
                "via": names.get(st.via_mac, st.via_mac) if st and st.via_mac else None,
            }
        )
    return cards


def _event_feed(core: HomeSenseApp) -> list[dict]:
    since = core.clock.now() - timedelta(days=FEED_DAYS)
    events = core.repos.presence.events(since=since)[-FEED_LIMIT:]
    persons = {p.id: p.name for p in core.repos.persons.list()}
    names = _device_names(core)
    return [
        {
            "ts": e.ts,
            "person": persons.get(e.person_id, f"Osoba #{e.person_id}"),
            "kind": e.kind,
            "device": names.get(e.mac, e.mac) if e.mac else None,
        }
        for e in reversed(events)
    ]


@router.get("/")
def now_page(request: Request, core: Core) -> Response:
    now = core.clock.now()
    devices = core.repos.devices.list()
    return _render(
        request,
        "now.html",
        "now",
        cards=_presence_cards(core),
        feed=_event_feed(core),
        online=len(analysis.filter_devices(devices, "online", now)),
        new_devices=len(analysis.filter_devices(devices, "new", now)),
        person_index={str(p.id): p.name for p in core.repos.persons.list()},
    )


@router.get("/partials/presence")
def presence_partial(request: Request, core: Core) -> Response:
    return _render(request, "partials/presence_cards.html", cards=_presence_cards(core))


@router.get("/partials/events")
def events_partial(request: Request, core: Core) -> Response:
    return _render(request, "partials/event_feed.html", feed=_event_feed(core))


# --- persons ---


@router.get("/persons")
def persons_page(request: Request, core: Core) -> Response:
    counts = Counter(d.person_id for d in core.repos.devices.list() if d.person_id is not None)
    return _render(request, "persons.html", "persons", cards=_presence_cards(core), device_counts=counts)


def _heatmap(core: HomeSenseApp, person_id: int) -> list[dict] | None:
    """7 rows (Monday first) of 24 cells with a 0-10 level of the home probability; None without data."""
    values = core.repos.profiles.get(PERSON_HOME, str(person_id))
    if not values:
        return None
    rows = []
    for wd, label in enumerate(WEEKDAYS):
        cells = []
        for hour in range(24):
            value = values.get((wd, hour))
            cells.append(
                {
                    "hour": hour,
                    "level": None if value is None else round(value[0] * 10),
                    "percent": None if value is None else round(value[0] * 100),
                    "samples": None if value is None else value[1],
                }
            )
        rows.append({"label": label, "cells": cells})
    return rows


@router.get("/persons/{person_id}")
def person_page(request: Request, person_id: int, core: Core) -> Response:
    person = _person_or_404(core, person_id)
    since = core.clock.now() - timedelta(days=core.profiles.window_days)
    typical = typical_times(
        core.repos.presence.events(person_id=person_id, since=since), core.settings.timezone
    )
    typical_rows = [
        {
            "label": WEEKDAYS_LONG[wd],
            "arrive": typical.get(wd, {}).get("arrive"),
            "leave": typical.get(wd, {}).get("leave"),
        }
        for wd in range(7)
    ]
    card = next(c for c in _presence_cards(core) if c["person"].id == person_id)
    return _render(
        request,
        "person_detail.html",
        "persons",
        person=person,
        card=card,
        devices=core.repos.devices.for_person(person_id),
        heatmap=_heatmap(core, person_id),
        hours=list(range(24)),
        window_days=core.profiles.window_days,
        typical=typical_rows,
        has_typical=any(r["arrive"] or r["leave"] for r in typical_rows),
        timeline=routes_api.person_timeline(person_id, core),
    )


# --- devices ---


@router.get("/devices")
def devices_page(
    request: Request, core: Core, device_filter: Annotated[str, Query(alias="filter")] = "all"
) -> Response:
    kind = device_filter if device_filter in analysis.DEVICE_FILTERS else "all"
    now = core.clock.now()
    persons = {p.id: p.name for p in core.repos.persons.list()}
    devices = sorted(
        analysis.filter_devices(core.repos.devices.list(), kind, now), key=lambda d: d.last_seen, reverse=True
    )
    return _render(
        request,
        "devices.html",
        "devices",
        devices=[
            {"d": d, "online": analysis.is_online(d, now), "owner": persons.get(d.person_id)} for d in devices
        ],
        filter=kind,
        filters=DEVICE_FILTER_LABELS,
        kinds=KIND_LABELS,
    )


def _online_chart(core: HomeSenseApp, online: list[str]) -> dict:
    """Minutes online per local hour for the last 24 hours (from 5-minute online buckets)."""
    tz = core.settings.timezone
    first = floor_hour(core.clock.now()) - timedelta(hours=23)
    hours = [first + timedelta(hours=i) for i in range(24)]
    minutes = Counter(floor_hour(datetime.fromisoformat(b)) for b in online)
    return {
        "labels": [to_local(h, tz).strftime("%H:00") for h in hours],
        "datasets": [{"label": "Minuty online", "data": [minutes.get(h, 0) * BUCKET_MIN for h in hours]}],
    }


def _traffic_chart(core: HomeSenseApp, traffic: list[dict]) -> dict | None:
    if not traffic:
        return None
    step = max(1, len(traffic) // MAX_TRAFFIC_POINTS)
    points = traffic[::step]
    tz = core.settings.timezone
    return {
        "labels": [to_local(datetime.fromisoformat(p["ts"]), tz).strftime("%H:%M") for p in points],
        "datasets": [
            {"label": "Wysyłanie (kb/s)", "data": [p["up_kbps"] or 0 for p in points]},
            {"label": "Pobieranie (kb/s)", "data": [p["down_kbps"] or 0 for p in points]},
        ],
    }


@router.get("/devices/{mac}")
def device_page(request: Request, mac: str, core: Core) -> Response:
    device = _device_or_404(core, mac)
    persons = core.repos.persons.list()
    owner = next((p for p in persons if p.id == device.person_id), None)
    history = routes_api.device_history(device.mac, core, hours=24)
    private = owner is not None and owner.privacy_mode == analysis.STATS_ONLY
    return _render(
        request,
        "device_detail.html",
        "devices",
        device=device,
        online=analysis.is_online(device, core.clock.now()),
        owner=owner,
        persons=persons,
        kinds=KIND_LABELS,
        online_chart=_online_chart(core, history["online"]) if history["online"] else None,
        traffic_chart=_traffic_chart(core, history["traffic"]),
        private=private,
        domains=[] if private else routes_api.network_top_domains(core, mac=device.mac, hours=24, limit=15),
    )


# --- network ---


def _hourly_chart(core: HomeSenseApp, rows: list[dict]) -> dict:
    tz = core.settings.timezone
    return {
        "labels": [to_local(datetime.fromisoformat(r["ts"]), tz).strftime("%d.%m %H:00") for r in rows],
        "datasets": [
            {"label": "Zapytania", "data": [r["queries"] for r in rows]},
            {"label": "Zablokowane", "data": [r["blocked"] for r in rows]},
        ],
    }


@router.get("/network")
def network_page(request: Request, core: Core) -> Response:
    hourly = routes_api.network_hourly(core, hours=NETWORK_HOURS)
    return _render(
        request,
        "network.html",
        "network",
        summary=routes_api.network_summary(core, hours=24),
        hourly_chart=_hourly_chart(core, hourly),
        has_dns=any(r["queries"] for r in hourly),
        top=routes_api.network_top_domains(core, hours=24, limit=20),
        blocked=routes_api.network_blocked(core, hours=24),
        new_domains=routes_api.network_new_domains(core, hours=24)[:20],
        adguard=core.adguard is not None,
    )


@router.get("/map")
def map_page(request: Request, core: Core) -> Response:
    """Device radar; the data comes from /api/map (static/js/map.js)."""
    return _render(
        request,
        "map.html",
        "map",
        kinds=RADAR_KIND_LABELS,
        persons=core.repos.persons.list(),
        rings=RADAR_RINGS_M,
    )


@router.get("/coverage")
def coverage_page(request: Request, core: Core) -> Response:
    """Wi-Fi coverage survey; the data comes from /api/coverage (static/js/coverage.js)."""
    devices = sorted(core.repos.devices.list(), key=lambda d: d.display_name.lower())
    return _render(
        request,
        "coverage.html",
        "coverage",
        devices=devices,
        bands=BAND_LABELS,
        windows=SURVEY_WINDOWS_S,
        default_window=SURVEY_DEFAULT_WINDOW_S,
    )


# --- analytics / alerts / settings / privacy ---


@router.get("/analytics")
def analytics_page(request: Request) -> Response:
    return _render(request, "analytics.html", "analytics", dashboards=GRAFANA_DASHBOARDS)


@router.get("/alerts")
def alerts_page(request: Request, core: Core, unack: bool = False) -> Response:
    return _render(
        request,
        "alerts.html",
        "alerts",
        alerts=core.repos.alerts.list(limit=200, unacknowledged_only=unack),
        unack_only=unack,
        severities=SEVERITY_LABELS,
    )


@router.get("/settings")
def settings_page(request: Request, core: Core, user: User) -> Response:
    statuses = {s.name: s for s in core.repos.collector_status.all()}
    collectors = [
        {"name": c.name, "status": statuses.get(c.name), "paused_until": core.runner.paused_until(c.name)}
        for c in core.collectors
    ]
    return _render(
        request,
        "settings.html",
        "settings",
        username=user,
        rules=core.rules.config(),
        rule_kinds=RULE_KIND_LABELS,
        channels=CHANNEL_LABELS,
        severities=SEVERITY_LABELS,
        notifiers=core.notifiers.names(),
        collectors=collectors,
        has_router=any(c.name == routes_api.ROUTER_COLLECTOR for c in core.collectors),
        settings=core.settings,
    )


@router.get("/privacy")
def privacy_page(request: Request, core: Core) -> Response:
    return _render(
        request, "privacy.html", "privacy", persons=core.repos.persons.list(), settings=core.settings
    )


# --- PWA ---


@public.get("/sw.js")
def service_worker() -> FileResponse:
    """The service worker must be served from the root so that its scope can be '/'."""
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="text/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )
