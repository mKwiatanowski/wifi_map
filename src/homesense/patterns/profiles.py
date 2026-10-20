"""Weekly (weekday, hour) pattern profiles.

Profiles are built from hour instances iterated in UTC and mapped to the local (weekday, hour)
slot via `to_local`. This keeps DST transitions safe: the repeated local 02:00 hour on the
autumn change simply contributes two samples to its slot, the skipped spring hour none.
Weekday follows `datetime.weekday()` (0 = Monday).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta

from homesense.core.clock import Clock
from homesense.core.models import PresenceEvent
from homesense.core.timeutil import floor_hour, to_local
from homesense.storage.repositories import Repos
from homesense.storage.repositories.profiles import ProfileValues

SLOTS = [(wd, h) for wd in range(7) for h in range(24)]

PERSON_HOME = "person_home"
DEVICE_ACTIVE = "device_active"
DEVICE_DNS = "device_dns"

_HOUR = timedelta(hours=1)
_HOUR_S = 3600.0


class _HourGrid:
    """Hour instances [start, end) in UTC, each mapped to its local (weekday, hour) slot."""

    def __init__(self, start: datetime, end: datetime, tz: str) -> None:
        self.start = start
        self.n = max(0, int((end - start) / _HOUR))
        self.end = start + self.n * _HOUR
        self.slots = [_slot(start + i * _HOUR, tz) for i in range(self.n)]

    def index(self, ts: datetime) -> int:
        return int((ts - self.start) // _HOUR)

    def aggregate(self, values: list[float], min_samples: int) -> ProfileValues:
        sums: dict[tuple[int, int], float] = defaultdict(float)
        counts: dict[tuple[int, int], int] = defaultdict(int)
        for slot, v in zip(self.slots, values, strict=True):
            sums[slot] += v
            counts[slot] += 1
        return {s: (sums[s] / n, n) for s, n in counts.items() if n >= min_samples}


def _slot(ts: datetime, tz: str) -> tuple[int, int]:
    loc = to_local(ts, tz)
    return loc.weekday(), loc.hour


class ProfileBuilder:
    def __init__(
        self, repos: Repos, clock: Clock, tz: str, window_days: int = 30, min_samples: int = 2
    ) -> None:
        self.repos = repos
        self.clock = clock
        self.tz = tz
        self.window_days = window_days
        self.min_samples = min_samples

    # --- window ---

    def _bounds(self) -> tuple[datetime, datetime]:
        """[window start, end) in whole UTC hours; the current, incomplete hour is excluded."""
        end = floor_hour(self.clock.now())
        return end - timedelta(days=self.window_days), end

    def _grid(self, first_data: Iterable[datetime | None]) -> _HourGrid | None:
        """Grid from the hour of the first data point (clipped to the window), or None without data."""
        known = [t for t in first_data if t is not None]
        if not known:
            return None
        since, until = self._bounds()
        return _HourGrid(max(since, floor_hour(min(known))), until, self.tz)

    # --- builders ---

    def build_person_home(self, person_id: int) -> ProfileValues:
        """Mean fraction of each hour the person was home."""
        since, until = self._bounds()
        intervals = self.repos.presence.intervals(person_id, since, until)
        grid = self._grid(
            [
                self.repos.presence.first_event_ts(person_id),
                *(iv.start_ts for iv in intervals[:1]),
                *(d.first_seen for d in self.repos.devices.for_person(person_id)),
            ]
        )
        if grid is None:
            return {}
        covered = [0.0] * grid.n
        for iv in intervals:
            start = max(iv.start_ts, grid.start)
            end = min(iv.end_ts or grid.end, grid.end)
            if end <= start:
                continue
            i = grid.index(start)
            h_start = grid.start + i * _HOUR
            while h_start < end:
                h_end = h_start + _HOUR
                covered[i] += (min(end, h_end) - max(start, h_start)).total_seconds()
                i += 1
                h_start = h_end
        return grid.aggregate([c / _HOUR_S for c in covered], self.min_samples)

    def build_device_active(self, mac: str) -> ProfileValues:
        """Mean fraction of minutes per hour with the device online."""
        since, until = self._bounds()
        buckets = self.repos.observations.online_buckets(mac, since, until, bucket_s=60)
        device = self.repos.devices.get(mac)
        grid = self._grid([device.first_seen if device else None, *buckets[:1]])
        if grid is None:
            return {}
        minutes = [0] * grid.n
        for b in buckets:
            if b >= grid.start:
                minutes[grid.index(b)] += 1
        return grid.aggregate([m / 60 for m in minutes], self.min_samples)

    def build_device_dns(self, mac: str) -> ProfileValues:
        """Mean DNS queries per hour."""
        since, until = self._bounds()
        stats = self.repos.dns.hourly_stats(mac, since, until)
        device = self.repos.devices.get(mac)
        grid = self._grid([device.first_seen if device else None, *(s[0] for s in stats[:1])])
        if grid is None:
            return {}
        queries = [0.0] * grid.n
        for hour_ts, _mac, q, _blocked in stats:
            if hour_ts >= grid.start:
                queries[grid.index(hour_ts)] += q
        return grid.aggregate(queries, self.min_samples)

    def rebuild_all(self) -> None:
        since, _ = self._bounds()
        now = self.clock.now()
        profiles = self.repos.profiles
        for person in self.repos.persons.list():
            profiles.replace(PERSON_HOME, str(person.id), self.build_person_home(person.id), now)
        for device in self.repos.devices.seen_since(since):
            profiles.replace(DEVICE_ACTIVE, device.mac, self.build_device_active(device.mac), now)
            profiles.replace(DEVICE_DNS, device.mac, self.build_device_dns(device.mac), now)


def _fmt_clock(minutes: float) -> str:
    m = round(minutes) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def _fmt_duration(minutes: float) -> str:
    m = round(minutes)
    return f"{m // 60}:{m % 60:02d}"


TimeStats = tuple[float, float, int]  # (median minutes, IQR minutes, sample count)


def time_of_day_stats(events: Iterable[PresenceEvent], tz: str) -> dict[int, dict[str, TimeStats | None]]:
    """weekday -> {"arrive"/"leave": (median, IQR, n)} of local time of day in minutes since midnight.

    A kind with no events on that weekday is None; a single event has IQR 0. Times are plain minutes
    since local midnight (no wrap-around), so events straddling midnight are not merged.
    """
    samples: dict[tuple[int, str], list[int]] = defaultdict(list)
    for e in events:
        loc = to_local(e.ts, tz)
        samples[(loc.weekday(), e.kind)].append(loc.hour * 60 + loc.minute)
    result: dict[int, dict[str, TimeStats | None]] = {}
    for wd in range(7):
        result[wd] = {}
        for kind in ("arrive", "leave"):
            xs = samples.get((wd, kind), [])
            if not xs:
                result[wd][kind] = None
                continue
            iqr = 0.0
            if len(xs) >= 2:
                q1, _, q3 = statistics.quantiles(xs, n=4, method="inclusive")
                iqr = float(q3 - q1)
            result[wd][kind] = (float(statistics.median(xs)), iqr, len(xs))
    return result


def typical_times(events: list[PresenceEvent], tz: str) -> dict[int, dict[str, tuple[str, str] | None]]:
    """weekday -> {"arrive"/"leave": (median "HH:MM", IQR "H:MM")} by local time of day.

    A kind with fewer than 2 events on that weekday is None (see `time_of_day_stats`).
    """
    return {
        wd: {
            kind: None if st is None or st[2] < 2 else (_fmt_clock(st[0]), _fmt_duration(st[1]))
            for kind, st in kinds.items()
        }
        for wd, kinds in time_of_day_stats(events, tz).items()
    }
