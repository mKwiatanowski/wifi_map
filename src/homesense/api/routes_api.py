"""JSON API under /api. Everything except /api/health needs a session; mutations need the CSRF header."""

# No `from __future__ import annotations`: FastAPI/pydantic need runtime-resolvable annotations here.

import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from datetime import date as Date
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, field_validator
from sqlalchemy.exc import IntegrityError

from homesense.api.deps import csrf_protect, csrf_token, current_user, get_core, require_user
from homesense.app import HomeSenseApp
from homesense.core.models import Alert, Device, Person, normalize_mac
from homesense.core.timeutil import floor_hour, to_local
from homesense.monitoring import PAUSE_HOURS, MonitoringState
from homesense.network import analysis
from homesense.network.adguard import AdGuardClient, AdGuardError
from homesense.patterns.profiles import PERSON_HOME, typical_times
from homesense.presence import radar
from homesense.storage.repositories.dns import UNKNOWN_MAC

log = logging.getLogger(__name__)

Core = Annotated[HomeSenseApp, Depends(get_core)]
User = Annotated[str, Depends(require_user)]

public = APIRouter(prefix="/api")
router = APIRouter(prefix="/api", dependencies=[Depends(require_user), Depends(csrf_protect)])

ADGUARD_DOWN = "AdGuard niedostępny"
ROUTER_COLLECTOR = "router"
KNOWN_CHANNELS = frozenset({"log", "windows", "telegram", "ntfy"})
MAX_HOURS = 24 * 31
HISTORY_BUCKET_S = 300
MAX_TRAIL_MINUTES = 24 * 60
RADAR_LOOKBACK_MIN = 60  # RSSI history used for smoothing, even with a shorter trail
UNPROCESSABLE = 422  # status constant name differs between Starlette versions

Hours = Annotated[int, Query(ge=1, le=MAX_HOURS)]
Severity = Literal["info", "warning", "critical"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Color = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]
PrivacyMode = Literal["full", "stats_only"]
# Device kinds: the simulated household uses phone/laptop/tv/iot; the rest are for manual tagging.
DeviceKind = Literal["phone", "laptop", "tablet", "desktop", "tv", "iot", "other"]
Hour = Annotated[StrictInt, Field(ge=0, le=23)]

# One DNS label: letters/digits/underscore/hyphen, 1-63 chars, no leading/trailing hyphen.
_LABEL = r"(?!-)[a-z0-9_-]{1,63}(?<!-)"
_DOMAIN = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
_MAX_DOMAIN = 253


def _not_null(value: Any) -> Any:
    if value is None:
        raise ValueError("must not be null")
    return value


def _empty_to_none(value: str | None) -> str | None:
    return value or None


# --- request models ---


class PersonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    color: Color | None = None
    privacy_mode: PrivacyMode = "full"


class PersonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    color: Color | None = None
    privacy_mode: PrivacyMode | None = None

    _check_not_null = field_validator("name", "privacy_mode")(_not_null)


class DevicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    person_id: int | None = None
    label: Annotated[str, StringConstraints(max_length=64)] | None = None
    trusted: bool | None = None
    counts_for_presence: bool | None = None
    kind: DeviceKind | None = None
    notes: Annotated[str, StringConstraints(max_length=500)] | None = None

    _check_not_null = field_validator("trusted", "counts_for_presence")(_not_null)
    _blank_is_none = field_validator("label", "notes")(_empty_to_none)


class DomainIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str

    @field_validator("domain")
    @classmethod
    def _valid_domain(cls, value: str) -> str:
        """Plain hostname only: AdGuard rule metacharacters (^ | $ / \\ , * ...) or spaces are rejected."""
        domain = value.strip().lower().removesuffix(".")
        if len(domain) > _MAX_DOMAIN or not _DOMAIN.fullmatch(domain):
            raise ValueError("invalid domain name")
        return domain


class MacIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mac: str

    _normalize = field_validator("mac")(normalize_mac)


class QuietHoursIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: Hour
    end: Hour
    allow: list[Severity] = []


class AlertRulesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channels: dict[Annotated[str, StringConstraints(min_length=1, max_length=64)], list[str]]
    min_severity: Severity
    quiet_hours: QuietHoursIn


class ChannelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Annotated[str, StringConstraints(min_length=1, max_length=32)]


class PauseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minutes: Annotated[int, Field(ge=1, le=24 * 60)] = 15


class MonitoringPauseIn(BaseModel):
    """hours: 1, 8 or 24; null = until resumed by hand."""

    model_config = ConfigDict(extra="forbid")
    hours: int | None = None

    @field_validator("hours")
    @classmethod
    def _allowed_hours(cls, value: int | None) -> int | None:
        if value is not None and value not in PAUSE_HOURS:
            raise ValueError(f"hours must be one of {PAUSE_HOURS} or null")
        return value


# --- serialization ---


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


def monitoring_json(state: MonitoringState) -> dict:
    return {
        "paused": state.paused,
        "since": _iso(state.since),
        "until": _iso(state.until),
        "resumed_at": _iso(state.resumed_at),
    }


def _person(p: Person) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "color": p.color,
        "privacy_mode": p.privacy_mode,
        "created_at": _iso(p.created_at),
    }


def _device(d: Device, names: dict[int, str], now: datetime) -> dict:
    return {
        "mac": d.mac,
        "display_name": d.display_name,
        "label": d.label,
        "hostname": d.hostname,
        "vendor": d.vendor,
        "ip": d.ip,
        "kind": d.kind,
        "notes": d.notes,
        "person_id": d.person_id,
        "person_name": names.get(d.person_id) if d.person_id is not None else None,
        "trusted": d.trusted,
        "counts_for_presence": d.counts_for_presence,
        "first_seen": _iso(d.first_seen),
        "last_seen": _iso(d.last_seen),
        "online": analysis.is_online(d, now),
        "is_random": d.is_random,
    }


def _alert(a: Alert) -> dict:
    return {
        "id": a.id,
        "ts": _iso(a.ts),
        "kind": a.kind,
        "severity": a.severity,
        "title": a.title,
        "message": a.message,
        "person_id": a.person_id,
        "mac": a.mac,
        "data": a.data,
        "acknowledged": a.acknowledged,
    }


def _person_names(core: HomeSenseApp) -> dict[int, str]:
    return {p.id: p.name for p in core.repos.persons.list()}


def _mac_path(mac: str) -> str:
    try:
        return normalize_mac(mac)
    except ValueError:
        raise HTTPException(UNPROCESSABLE, "Nieprawidłowy adres MAC") from None


def _mac_query(mac: str | None) -> str | None:
    if not mac:
        return None
    return UNKNOWN_MAC if mac == UNKNOWN_MAC else _mac_path(mac)


def _get_person(core: HomeSenseApp, person_id: int) -> Person:
    person = core.repos.persons.get(person_id)
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nie ma takiej osoby")
    return person


def _get_device(core: HomeSenseApp, mac: str) -> Device:
    device = core.repos.devices.get(mac)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nie ma takiego urządzenia")
    return device


# --- health (public: the tray polls it) ---


def _is_loopback(request: Request) -> bool:
    """The TCP peer is loopback. Forwarded headers are deliberately ignored (and uvicorn runs with
    proxy_headers=False), so a LAN client cannot claim to be 127.0.0.1."""
    host = request.client.host if request.client else None
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


@public.get("/health")
def health(request: Request, core: Core) -> dict:
    """Overall status for everyone; collector details and how many people are home only for loopback
    callers (the tray) or a valid session, since "nobody home" must not leak to the LAN.
    Never names or error texts."""
    status_only = {"status": core.runner.health()}
    if not (_is_loopback(request) or current_user(request) is not None):
        return status_only
    statuses = {s.name: s for s in core.repos.collector_status.all()}
    paused = core.monitoring.is_paused()
    collectors = []
    for collector in core.collectors:
        st = statuses.get(collector.name)
        collectors.append(
            {
                "name": collector.name,
                "last_ok_ts": _iso(st.last_ok_ts) if st else None,
                "last_error_ts": _iso(st.last_error_ts) if st else None,
                "consecutive_failures": st.consecutive_failures if st else 0,
                "paused_until": _iso(core.runner.paused_until(collector.name)),
                "paused": paused or core.runner.paused_until(collector.name) is not None,
            }
        )
    return {
        **status_only,
        "collectors": collectors,
        "people_home": sum(1 for s in core.presence.states() if s.home),
        "monitoring": monitoring_json(core.monitoring.state()),
    }


@router.get("/me")
def me(request: Request, user: User) -> dict:
    return {"username": user, "csrf_token": csrf_token(request)}


# --- presence / persons ---


@router.get("/presence")
def presence(core: Core) -> list[dict]:
    states = {s.person_id: s for s in core.presence.states()}
    result = []
    for p in core.repos.persons.list():
        st = states.get(p.id)
        result.append(
            {
                "person_id": p.id,
                "name": p.name,
                "color": p.color,
                "home": bool(st and st.home),
                "since": _iso(st.since) if st else None,
                "via": st.via_mac if st else None,
            }
        )
    return result


@router.get("/persons")
def list_persons(core: Core) -> list[dict]:
    return [_person(p) for p in core.repos.persons.list()]


@router.post("/persons", status_code=status.HTTP_201_CREATED)
def create_person(body: PersonIn, core: Core) -> dict:
    try:
        person_id = core.repos.persons.create(body.name, body.color, body.privacy_mode)
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Osoba o tej nazwie już istnieje") from None
    return _person(_get_person(core, person_id))


@router.patch("/persons/{person_id}")
def update_person(person_id: int, body: PersonPatch, core: Core) -> dict:
    _get_person(core, person_id)
    try:
        core.repos.persons.update(person_id, **body.model_dump(exclude_unset=True))
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Osoba o tej nazwie już istnieje") from None
    return _person(_get_person(core, person_id))


@router.delete("/persons/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_person(person_id: int, core: Core) -> Response:
    _get_person(core, person_id)
    core.repos.persons.delete(person_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/persons/{person_id}/profile")
def person_profile(person_id: int, core: Core) -> dict:
    _get_person(core, person_id)
    values = core.repos.profiles.get(PERSON_HOME, str(person_id))
    since = core.clock.now() - timedelta(days=core.profiles.window_days)
    events = core.repos.presence.events(person_id=person_id, since=since)
    return {
        "home": [[wd, h, v, n] for (wd, h), (v, n) in sorted(values.items())],
        "typical": typical_times(events, core.settings.timezone),
    }


@router.get("/persons/{person_id}/timeline")
def person_timeline(
    person_id: int, core: Core, day: Annotated[Date | None, Query(alias="date")] = None
) -> dict:
    """Home intervals clipped to the local day; an ongoing interval ends at min(now, end of day)."""
    _get_person(core, person_id)
    tz = ZoneInfo(core.settings.timezone)
    now = core.clock.now()
    day = day or to_local(now, core.settings.timezone).date()
    start = datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(), tzinfo=tz).astimezone(UTC)
    intervals = []
    for iv in core.repos.presence.intervals(person_id, start, end):
        iv_start = max(iv.start_ts, start)
        iv_end = min(iv.end_ts or now, end)
        if iv_end > iv_start:
            intervals.append({"start": _iso(iv_start), "end": _iso(iv_end), "open": iv.end_ts is None})
    return {"date": day.isoformat(), "intervals": intervals}


# --- devices ---


@router.get("/devices")
def list_devices(
    core: Core, kind: Annotated[Literal[analysis.DEVICE_FILTERS], Query(alias="filter")] = "all"
) -> list[dict]:
    now = core.clock.now()
    names = _person_names(core)
    return [_device(d, names, now) for d in analysis.filter_devices(core.repos.devices.list(), kind, now)]


@router.patch("/devices/{mac}")
def update_device(mac: str, body: DevicePatch, core: Core) -> dict:
    mac = _mac_path(mac)
    _get_device(core, mac)
    fields = body.model_dump(exclude_unset=True)
    person_id = fields.get("person_id")
    if person_id is not None and core.repos.persons.get(person_id) is None:
        raise HTTPException(UNPROCESSABLE, "Nie ma takiej osoby")
    core.repos.devices.update(mac, **fields)
    return _device(_get_device(core, mac), _person_names(core), core.clock.now())


@router.get("/devices/{mac}/history")
def device_history(mac: str, core: Core, hours: Annotated[int, Query(ge=1, le=24 * 7)] = 24) -> dict:
    """Online 5-minute buckets and traffic samples of the last `hours`."""
    mac = _mac_path(mac)
    _get_device(core, mac)
    now = core.clock.now()
    since, until = now - timedelta(hours=hours), now + timedelta(seconds=1)
    buckets = core.repos.observations.online_buckets(mac, since, until, bucket_s=HISTORY_BUCKET_S)
    traffic = [
        {"ts": _iso(o.ts), "up_kbps": o.up_kbps, "down_kbps": o.down_kbps}
        for o in core.repos.observations.for_device(mac, since, until)
        if o.up_kbps is not None or o.down_kbps is not None
    ]
    return {"online": [_iso(b) for b in buckets], "traffic": traffic}


# --- device radar (map) ---


def _radar_kinds(kinds: str | None) -> set[str] | None:
    if not kinds:
        return None
    wanted = {k.strip() for k in kinds.split(",") if k.strip()}
    unknown = wanted - set(radar.KINDS)
    if unknown:
        raise HTTPException(UNPROCESSABLE, f"Nieznane typy urządzeń: {', '.join(sorted(unknown))}")
    return wanted


@router.get("/map")
def device_map(
    core: Core,
    kinds: str | None = None,
    person_id: int | None = None,
    online_only: bool = False,
    trail_minutes: Annotated[int, Query(ge=0, le=MAX_TRAIL_MINUTES)] = 30,
) -> list[dict]:
    """Estimated distance of each device from the router (RSSI only; the angle is arbitrary)."""
    wanted = _radar_kinds(kinds)
    now = core.clock.now()
    lookback = timedelta(minutes=max(trail_minutes, RADAR_LOOKBACK_MIN))
    samples = core.repos.observations.signal_samples(now - lookback, now + timedelta(seconds=1))
    trail_since = now - timedelta(minutes=trail_minutes) if trail_minutes else now + timedelta(days=1)
    persons = {p.id: p for p in core.repos.persons.list()}
    result = []
    for d in core.repos.devices.list():
        kind, guessed = radar.device_kind(d)
        online = analysis.is_online(d, now)
        if (wanted is not None and kind not in wanted) or (online_only and not online):
            continue
        if person_id is not None and d.person_id != person_id:
            continue
        person = persons.get(d.person_id) if d.person_id is not None else None
        pos = radar.position(
            samples.get(d.mac, []),
            since=trail_since,
            p0=core.settings.radar_p0_dbm,
            n=core.settings.radar_path_loss_n,
        )
        result.append(
            {
                "mac": d.mac,
                "name": d.display_name,
                "kind": kind,
                "kind_guessed": guessed,
                "person_id": d.person_id,
                "person_name": person.name if person else None,
                "color": person.color if person else None,
                "online": online,
                "connection": pos.connection,
                "wired": pos.wired,
                "rssi": pos.rssi,
                "rssi_smoothed": pos.rssi_smoothed,
                "distance_m": pos.distance_m,
                "angle_deg": round(radar.angle_for_mac(d.mac), 2),
                "last_seen": _iso(d.last_seen),
                "trail": [{"ts": _iso(p["ts"]), "distance_m": p["distance_m"]} for p in pos.trail],
            }
        )
    result.sort(key=lambda e: (e["name"].lower(), e["mac"]))
    return result


# --- network ---


@router.get("/network/top-domains")
def network_top_domains(
    core: Core, mac: str | None = None, hours: Hours = 24, limit: Annotated[int, Query(ge=1, le=200)] = 20
) -> list[dict]:
    since = core.clock.now() - timedelta(hours=hours)
    return analysis.top_domains(core.repos, _mac_query(mac), since, limit)


@router.get("/network/blocked")
def network_blocked(core: Core, hours: Hours = 24) -> list[dict]:
    return analysis.blocked_domains(core.repos, core.clock.now() - timedelta(hours=hours))


@router.get("/network/new-domains")
def network_new_domains(core: Core, hours: Hours = 24) -> list[dict]:
    return analysis.new_domains(core.repos, core.clock.now() - timedelta(hours=hours))


def _hour_window(core: HomeSenseApp, hours: int) -> tuple[datetime, datetime]:
    """The last `hours` whole UTC hours, including the current (incomplete) one."""
    until = floor_hour(core.clock.now()) + timedelta(hours=1)
    return until - timedelta(hours=hours), until


@router.get("/network/hourly")
def network_hourly(core: Core, mac: str | None = None, hours: Hours = 48) -> list[dict]:
    since, until = _hour_window(core, hours)
    return analysis.hourly(core.repos, _mac_query(mac), since, until)


@router.get("/network/summary")
def network_summary(core: Core, hours: Hours = 24) -> dict:
    since, until = _hour_window(core, hours)
    return analysis.summary(core.repos, since, until)


async def _adguard(core: HomeSenseApp, action: Callable[[AdGuardClient], Awaitable[None]]) -> None:
    if core.adguard is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, ADGUARD_DOWN)
    try:
        await action(core.adguard)
    except AdGuardError as exc:
        log.warning("AdGuard request failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, ADGUARD_DOWN) from None


def _device_ip(core: HomeSenseApp, mac: str) -> str:
    device = _get_device(core, mac)
    if not device.ip:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Urządzenie nie ma znanego adresu IP")
    return device.ip


@router.post("/network/block-domain")
async def block_domain(body: DomainIn, core: Core) -> dict:
    await _adguard(core, lambda a: a.block_domain(body.domain))
    return {"ok": True, "domain": body.domain}


@router.post("/network/unblock-domain")
async def unblock_domain(body: DomainIn, core: Core) -> dict:
    await _adguard(core, lambda a: a.unblock_domain(body.domain))
    return {"ok": True, "domain": body.domain}


@router.post("/network/block-device")
async def block_device(body: MacIn, core: Core) -> dict:
    ip = _device_ip(core, body.mac)
    await _adguard(core, lambda a: a.block_client(ip))
    return {"ok": True, "mac": body.mac, "ip": ip}


@router.post("/network/unblock-device")
async def unblock_device(body: MacIn, core: Core) -> dict:
    ip = _device_ip(core, body.mac)
    await _adguard(core, lambda a: a.unblock_client(ip))
    return {"ok": True, "mac": body.mac, "ip": ip}


# --- alerts ---


@router.get("/alerts")
def list_alerts(
    core: Core, unack: bool = False, limit: Annotated[int, Query(ge=1, le=1000)] = 100
) -> list[dict]:
    return [_alert(a) for a in core.repos.alerts.list(limit=limit, unacknowledged_only=unack)]


@router.post("/alerts/ack-all")
def ack_all_alerts(core: Core) -> dict:
    core.repos.alerts.acknowledge_all()
    return {"ok": True}


@router.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, core: Core) -> dict:
    if not core.repos.alerts.acknowledge(alert_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nie ma takiego alertu")
    return {"ok": True}


# --- settings ---


@router.get("/settings/alerts")
def get_alert_settings(core: Core) -> dict:
    return core.rules.config()


@router.put("/settings/alerts")
def put_alert_settings(body: AlertRulesIn, core: Core) -> dict:
    known = KNOWN_CHANNELS | set(core.notifiers.names())
    unknown = sorted({c for channels in body.channels.values() for c in channels} - known)
    if unknown:
        raise HTTPException(UNPROCESSABLE, f"Nieznane kanały powiadomień: {', '.join(unknown)}")
    core.rules.save(body.model_dump())
    return core.rules.config()


@router.post("/settings/test-notification")
async def test_notification(body: ChannelIn, core: Core) -> dict:
    try:
        await core.dispatcher.send_test(body.channel)
    except KeyError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Kanał powiadomień „{body.channel}” nie jest skonfigurowany."
        ) from None
    except Exception:
        log.exception("test notification via %s failed", body.channel)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Nie udało się wysłać powiadomienia testowego."
        ) from None
    return {"ok": True}


# --- monitoring on/off ---


@router.get("/monitoring")
def get_monitoring(core: Core) -> dict:
    return monitoring_json(core.monitoring.state())


@router.post("/monitoring/pause")
async def pause_monitoring(core: Core, user: User, body: MonitoringPauseIn | None = None) -> dict:
    hours = (body or MonitoringPauseIn()).hours
    state = await core.monitoring.pause(None if hours is None else timedelta(hours=hours))
    log.info("monitoring paused by %s for %s", user, f"{hours} h" if hours else "until resumed")
    return monitoring_json(state)


@router.post("/monitoring/resume")
async def resume_monitoring(core: Core, user: User) -> dict:
    state = await core.monitoring.resume()
    log.info("monitoring resumed by %s", user)
    return monitoring_json(state)


# --- router ---


@router.post("/router/pause")
def pause_router(core: Core, body: PauseIn | None = None) -> dict:
    until = core.clock.now() + timedelta(minutes=(body or PauseIn()).minutes)
    core.runner.pause(ROUTER_COLLECTOR, until)
    return {"paused_until": _iso(until)}
