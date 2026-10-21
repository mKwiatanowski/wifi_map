"""Wi-Fi coverage survey (/coverage): measurement points from router RSSI observations.

A point records the RSSI the router reports for one device while the user stands at a spot on the
floor plan. Its value is the median of the samples in [started_at, ends_at); a point without any
sample is invalid (kept, but not used for the heatmap). Pure functions over the repositories.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Iterable
from datetime import datetime

from homesense.core.models import CoveragePoint, DeviceObservation
from homesense.core.timeutil import to_local
from homesense.storage.repositories import Repos

BAND_BY_CONNECTION = {"2g": "2.4", "5g": "5", "6g": "6"}
UNKNOWN_BAND = "unknown"
BANDS = ("2.4", "5", "6", UNKNOWN_BAND)
MIN_WINDOW_S = 30
MAX_WINDOW_S = 180


def summarize(observations: Iterable[DeviceObservation]) -> tuple[float | None, int, str]:
    """(median RSSI or None, number of RSSI samples, most common band) of a measurement window."""
    with_signal = [o for o in observations if o.signal is not None]
    signals = [o.signal for o in with_signal]
    bands = Counter(BAND_BY_CONNECTION.get(o.connection, UNKNOWN_BAND) for o in with_signal)
    known = [(n, b) for b, n in bands.items() if b != UNKNOWN_BAND]
    band = max(known)[1] if known else UNKNOWN_BAND
    median = round(float(statistics.median(signals)), 1) if signals else None
    return median, len(signals), band


def window_samples(repos: Repos, point: CoveragePoint) -> list[DeviceObservation]:
    return repos.observations.for_device(point.mac, point.started_at, point.ends_at)


def finalize_due(repos: Repos, now: datetime) -> int:
    """Stores the result of every measurement whose window has ended; returns how many."""
    finished = 0
    for point in repos.coverage.measuring():
        if now < point.ends_at:
            continue
        rssi, samples, band = summarize(window_samples(repos, point))
        repos.coverage.finish_point(point.id, rssi, samples, band)
        finished += 1
    return finished


def live_point(repos: Repos, point: CoveragePoint) -> CoveragePoint:
    """A running measurement with the samples collected so far (not stored)."""
    if point.done:
        return point
    rssi, samples, band = summarize(window_samples(repos, point))
    return CoveragePoint(**{**point.__dict__, "rssi": rssi, "samples": samples, "band": band})


def signal_by_hour(
    rows: Iterable[tuple[datetime, str, float, int]], tz: str
) -> dict[str, dict[int, tuple[float, int]]]:
    """MAC -> local hour of day (0-23) -> (sample-weighted average RSSI, samples) from hourly rows."""
    sums: dict[str, dict[int, list[float]]] = {}
    for hour_ts, mac, rssi, n in rows:
        cell = sums.setdefault(mac, {}).setdefault(to_local(hour_ts, tz).hour, [0.0, 0])
        cell[0] += rssi * n
        cell[1] += n
    return {
        mac: {h: (round(total / n, 1), int(n)) for h, (total, n) in hours.items() if n}
        for mac, hours in sums.items()
    }


def active_targets(repos: Repos, now: datetime) -> dict[str, float]:
    """MAC -> distance (m) from the router of every running measurement (for the simulator)."""
    plan = repos.coverage.get_plan()
    if plan is None or plan.router is None:
        return {}
    rx, ry = plan.router
    return {
        p.mac: math.hypot(p.x - rx, p.y - ry)
        for p in repos.coverage.measuring()
        if p.started_at <= now < p.ends_at
    }
