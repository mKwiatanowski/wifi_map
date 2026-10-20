"""Anomaly detectors over stored data, profiles and current presence.

Each detector is pure with respect to I/O beyond the repositories: `evaluate(ctx)` returns the
alerts it would raise; `run_detectors` hands them to the AlertService, which drops repeats by
`dedup_key`. DNS-based detectors look at the last full hour [floor_hour(now) - 1h, floor_hour(now)).
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from homesense.alerts.service import AlertService
from homesense.core.models import Alert
from homesense.core.timeutil import floor_hour, to_local
from homesense.patterns.profiles import DEVICE_DNS, PERSON_HOME, time_of_day_stats
from homesense.presence.engine import PersonState
from homesense.storage.repositories import Repos
from homesense.storage.repositories.dns import UNKNOWN_MAC

log = logging.getLogger(__name__)

_HOUR = timedelta(hours=1)

# TrafficSpike guards (ruling R17): sigma floor against idle / zero-variance baselines, and an
# absolute floor so small rises never alert.
TRAFFIC_MIN_STD_KBPS = 50.0
TRAFFIC_MIN_KBPS = 5000.0


@dataclass
class DetectorContext:
    repos: Repos
    now: datetime
    tz: str
    presence: list[PersonState]


class Detector(Protocol):
    name: str

    def evaluate(self, ctx: DetectorContext) -> list[Alert]: ...


# --- helpers ---


def _last_full_hour(now: datetime) -> tuple[datetime, datetime]:
    end = floor_hour(now)
    return end - _HOUR, end


def _hour_key(ts: datetime) -> str:
    return floor_hour(ts).strftime("%Y%m%d%H")


def _person_name(repos: Repos, person_id: int) -> str:
    person = repos.persons.get(person_id)
    return person.name if person else f"Osoba #{person_id}"


def _device_name(repos: Repos, mac: str) -> str:
    device = repos.devices.get(mac)
    return device.display_name if device else mac


def _last_hour_stats(ctx: DetectorContext) -> list[tuple[datetime, str, int, int]]:
    """Per-device DNS stats of the last full hour, without unresolved ('unknown') clients."""
    start, end = _last_full_hour(ctx.now)
    return [s for s in ctx.repos.dns.hourly_stats(None, start, end) if s[1] != UNKNOWN_MAC]


def _mean_std(xs: list[float]) -> tuple[float, float]:
    return statistics.fmean(xs), statistics.pstdev(xs)


def _hhmm(minutes: float) -> str:
    m = round(minutes) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


# --- detectors ---


class UnusualPresence:
    """Home in a slot where the person is rarely home (p < 0.1), or away where they almost always are."""

    name = "unusual_presence"
    min_samples = 4

    def evaluate(self, ctx: DetectorContext) -> list[Alert]:
        loc = to_local(ctx.now, ctx.tz)
        slot = (loc.weekday(), loc.hour)
        alerts = []
        for state in ctx.presence:
            entry = ctx.repos.profiles.get(PERSON_HOME, str(state.person_id)).get(slot)
            if entry is None or entry[1] < self.min_samples:
                continue
            p = entry[0]
            if not ((state.home and p < 0.1) or (not state.home and p > 0.9)):
                continue
            name = _person_name(ctx.repos, state.person_id)
            pct = round(p * 100)
            if state.home:
                message = (
                    f"{name} jest w domu o nietypowej porze — zwykle o tej godzinie bywa w domu "
                    f"tylko w {pct}% przypadków."
                )
            else:
                message = (
                    f"{name} jest poza domem o nietypowej porze — zwykle o tej godzinie jest w domu "
                    f"({pct}% przypadków)."
                )
            alerts.append(
                Alert(
                    kind=self.name,
                    severity="warning",
                    title="Nietypowa obecność",
                    message=message,
                    ts=ctx.now,
                    person_id=state.person_id,
                    data={"home": state.home, "probability": p, "samples": entry[1]},
                    dedup_key=f"unusual_presence:{state.person_id}:{ctx.now:%Y%m%d%H}",
                )
            )
        return alerts


class UnusualArrival:
    """The latest presence event (past 10 min) is far from the typical time for that weekday.

    Typical = median ± max(2·IQR, 60 min) of the person's earlier events of the same kind within the
    window (at least 4 of them).
    """

    name = "unusual_arrival"
    recent = timedelta(minutes=10)
    min_samples = 4
    min_tolerance_min = 60.0

    def __init__(self, window_days: int = 30) -> None:
        self.window = timedelta(days=window_days)

    def evaluate(self, ctx: DetectorContext) -> list[Alert]:
        alerts = []
        for person in ctx.repos.persons.list():
            recent = ctx.repos.presence.events(person.id, since=ctx.now - self.recent)
            if not recent:
                continue
            event = recent[-1]
            history = ctx.repos.presence.events(person.id, since=ctx.now - self.window, until=event.ts)
            loc = to_local(event.ts, ctx.tz)
            stats = time_of_day_stats(history, ctx.tz)[loc.weekday()][event.kind]
            if stats is None or stats[2] < self.min_samples:
                continue
            median, iqr, n = stats
            tolerance = max(2 * iqr, self.min_tolerance_min)
            minute = loc.hour * 60 + loc.minute
            if abs(minute - median) <= tolerance:
                continue
            arrive = event.kind == "arrive"
            verb = "wrócił(a) do domu" if arrive else "wyszedł/wyszła z domu"
            alerts.append(
                Alert(
                    kind=self.name,
                    severity="info",
                    title="Nietypowa godzina powrotu" if arrive else "Nietypowa godzina wyjścia",
                    message=(
                        f"{person.name} {verb} o {loc:%H:%M}, a zwykle około {_hhmm(median)} "
                        f"(±{round(tolerance)} min)."
                    ),
                    ts=ctx.now,
                    person_id=person.id,
                    mac=event.mac,
                    data={
                        "event": event.kind,
                        "median_min": median,
                        "iqr_min": iqr,
                        "samples": n,
                        "minute": minute,
                    },
                    dedup_key=f"unusual_arrival:{person.id}:{event.ts:%Y%m%d%H%M%S}",
                )
            )
        return alerts


class NightActivity:
    """More than 20 DNS queries in a night hour (01–05 local) where the device is normally quiet (< 5)."""

    name = "night_activity"
    night_hours = range(1, 6)
    min_queries = 20
    quiet_profile = 5.0

    def evaluate(self, ctx: DetectorContext) -> list[Alert]:
        start, end = _last_full_hour(ctx.now)
        loc = to_local(start, ctx.tz)
        if loc.hour not in self.night_hours:
            return []
        alerts = []
        for _, mac, queries, _blocked in _last_hour_stats(ctx):
            if queries <= self.min_queries:
                continue
            entry = ctx.repos.profiles.get(DEVICE_DNS, mac).get((loc.weekday(), loc.hour))
            usual = entry[0] if entry else None
            if usual is not None and usual >= self.quiet_profile:
                continue
            loc_end = to_local(end, ctx.tz)
            usual_txt = f" (zwykle około {usual:.0f})" if usual is not None else ""
            alerts.append(
                Alert(
                    kind=self.name,
                    severity="warning",
                    title="Aktywność w nocy",
                    message=(
                        f"{_device_name(ctx.repos, mac)}: liczba zapytań DNS w godzinach "
                        f"{loc:%H:%M}–{loc_end:%H:%M} wyniosła {queries}{usual_txt}."
                    ),
                    ts=ctx.now,
                    mac=mac,
                    data={"queries": queries, "profile": usual, "hour": start.isoformat()},
                    dedup_key=f"night_activity:{mac}:{loc.date().isoformat()}",
                )
            )
        return alerts


class DnsSpike:
    """Last full hour queries > 200 and > mean + 4σ of the device's hourly queries over the window.

    The baseline uses the stored hourly rows (hours with activity) before the last full hour and
    needs at least `min_history` of them.
    """

    name = "dns_spike"
    min_queries = 200
    sigmas = 4.0
    min_history = 24

    def __init__(self, window_days: int = 30) -> None:
        self.window = timedelta(days=window_days)

    def evaluate(self, ctx: DetectorContext) -> list[Alert]:
        start, _ = _last_full_hour(ctx.now)
        alerts = []
        for _, mac, queries, _blocked in _last_hour_stats(ctx):
            if queries <= self.min_queries:
                continue
            history = [float(q) for _, _, q, _ in ctx.repos.dns.hourly_stats(mac, start - self.window, start)]
            if len(history) < self.min_history:
                continue
            mean, std = _mean_std(history)
            if queries <= mean + self.sigmas * std:
                continue
            alerts.append(
                Alert(
                    kind=self.name,
                    severity="warning",
                    title="Skok zapytań DNS",
                    message=(
                        f"{_device_name(ctx.repos, mac)}: liczba zapytań DNS w ostatniej godzinie "
                        f"wyniosła {queries} (średnio {mean:.0f})."
                    ),
                    ts=ctx.now,
                    mac=mac,
                    data={"queries": queries, "mean": mean, "std": std},
                    dedup_key=f"dns_spike:{mac}:{_hour_key(start)}",
                )
            )
        return alerts


class BlockedBurst:
    """More than 50 blocked DNS queries from one device in the last full hour."""

    name = "blocked_burst"
    min_blocked = 50

    def evaluate(self, ctx: DetectorContext) -> list[Alert]:
        start, _ = _last_full_hour(ctx.now)
        return [
            Alert(
                kind=self.name,
                severity="warning",
                title="Dużo zablokowanych zapytań",
                message=(
                    f"{_device_name(ctx.repos, mac)}: liczba zablokowanych zapytań DNS w ostatniej "
                    f"godzinie wyniosła {blocked} (na {queries} wszystkich)."
                ),
                ts=ctx.now,
                mac=mac,
                data={"blocked": blocked, "queries": queries},
                dedup_key=f"blocked_burst:{mac}:{_hour_key(start)}",
            )
            for _, mac, queries, blocked in _last_hour_stats(ctx)
            if blocked > self.min_blocked
        ]


class NewDomainBurst:
    """An IoT device contacted more than 10 never-seen domains in the last full hour."""

    name = "new_domain_burst"
    min_new = 10
    sample_size = 20

    def evaluate(self, ctx: DetectorContext) -> list[Alert]:
        start, end = _last_full_hour(ctx.now)
        alerts = []
        for device in ctx.repos.devices.list():
            if device.kind != "iot":
                continue
            domains = [d for _, d, ts in ctx.repos.dns.new_domains(start, mac=device.mac) if ts < end]
            if len(domains) <= self.min_new:
                continue
            alerts.append(
                Alert(
                    kind=self.name,
                    severity="warning",
                    title="Dużo nowych domen",
                    message=(
                        f"{device.display_name}: urządzenie IoT połączyło się w ostatniej godzinie "
                        f"z nowymi domenami (liczba: {len(domains)})."
                    ),
                    ts=ctx.now,
                    mac=device.mac,
                    data={"count": len(domains), "domains": domains[: self.sample_size]},
                    dedup_key=f"new_domain_burst:{device.mac}:{_hour_key(start)}",
                )
            )
        return alerts


class TrafficSpike:
    """The 3 latest download samples all exceed max(mean + 4·max(σ, 50), 5000) kb/s, where mean and σ
    come from the 200 samples before them (at least 50 needed).

    Only devices whose latest sample is fresh (within `fresh`) are checked.
    """

    name = "traffic_spike"
    latest = 3
    baseline = 200
    min_baseline = 50
    sigmas = 4.0
    fresh = timedelta(minutes=10)

    def evaluate(self, ctx: DetectorContext) -> list[Alert]:
        alerts = []
        for device in ctx.repos.devices.seen_since(ctx.now - self.fresh):
            samples = ctx.repos.observations.recent_traffic(device.mac, self.latest + self.baseline)
            recent, base = samples[: self.latest], samples[self.latest :]
            if len(base) < self.min_baseline or recent[0].ts < ctx.now - self.fresh:
                continue
            mean, std = _mean_std([o.down_kbps for o in base])
            threshold = max(mean + self.sigmas * max(std, TRAFFIC_MIN_STD_KBPS), TRAFFIC_MIN_KBPS)
            if not all(o.down_kbps > threshold for o in recent):
                continue
            current = recent[0].down_kbps
            alerts.append(
                Alert(
                    kind=self.name,
                    severity="warning",
                    title="Skok ruchu sieciowego",
                    message=(
                        f"{device.display_name}: pobieranie {current:.0f} kb/s, zwykle około {mean:.0f} kb/s."
                    ),
                    ts=ctx.now,
                    mac=device.mac,
                    data={
                        "down_kbps": [o.down_kbps for o in recent],
                        "mean": mean,
                        "std": std,
                        "threshold": threshold,
                    },
                    dedup_key=f"traffic_spike:{device.mac}:{_hour_key(ctx.now)}",
                )
            )
        return alerts


class UnknownDevicePresent:
    """An unassigned, untrusted device has been on the network for 30+ minutes and is still online."""

    name = "unknown_device_present"
    min_age = timedelta(minutes=30)
    online_within = timedelta(minutes=5)

    def evaluate(self, ctx: DetectorContext) -> list[Alert]:
        alerts = []
        for device in ctx.repos.devices.seen_since(ctx.now - self.online_within):
            if device.person_id is not None or device.trusted:
                continue
            if device.first_seen > ctx.now - self.min_age:
                continue
            first = to_local(device.first_seen, ctx.tz)
            alerts.append(
                Alert(
                    kind=self.name,
                    severity="warning",
                    title="Nieznane urządzenie w sieci",
                    message=(
                        f"{device.display_name} jest w sieci (pierwszy raz widziane {first:%d.%m %H:%M}) "
                        "i nie jest przypisane do nikogo ani oznaczone jako zaufane."
                    ),
                    ts=ctx.now,
                    mac=device.mac,
                    data={"ip": device.ip, "vendor": device.vendor},
                    dedup_key=(
                        f"unknown_device_present:{device.mac}:{to_local(ctx.now, ctx.tz).date().isoformat()}"
                    ),
                )
            )
        return alerts


def default_detectors() -> list[Detector]:
    return [
        UnusualPresence(),
        UnusualArrival(),
        NightActivity(),
        DnsSpike(),
        BlockedBurst(),
        NewDomainBurst(),
        TrafficSpike(),
        UnknownDevicePresent(),
    ]


async def run_detectors(detectors: Iterable[Detector], ctx: DetectorContext, alerts: AlertService) -> int:
    """Evaluate every detector and raise its alerts; returns the number of alerts raised.

    A failing detector or a failing raise is logged and skipped without affecting the rest.
    """
    raised = 0
    for detector in detectors:
        try:
            found = detector.evaluate(ctx)
        except Exception:
            log.exception("detector %s failed", getattr(detector, "name", detector))
            continue
        for alert in found:
            try:
                if await alerts.raise_alert(alert) is not None:
                    raised += 1
            except Exception:
                log.exception("raising alert %s from detector %s failed", alert.dedup_key, detector.name)
    return raised
