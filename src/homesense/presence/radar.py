"""Device radar: estimated distance of each device from the router (pure functions, no I/O).

A single access point only reports per-device RSSI, so only the distance is estimated (log-distance
path loss model on EMA-smoothed RSSI). The angle on the map is arbitrary but stable: derived from the
MAC, so dots do not overlap randomly and stay put between refreshes.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from homesense.core.models import Device, DeviceObservation, normalize_mac

KINDS = ("phone", "computer", "tablet", "tv", "iot", "unknown")
DEFAULT_P0_DBM = -40.0
DEFAULT_PATH_LOSS_N = 3.0
MIN_DISTANCE_M = 0.5
MAX_DISTANCE_M = 30.0
WIRED_DISTANCE_M = 0.5
EMA_ALPHA = 0.3
MAX_TRAIL_POINTS = 60

# Stored device kinds (API DeviceKind) -> radar kinds.
_STORED_KIND = {"laptop": "computer", "desktop": "computer", "other": "unknown"}

# Order matters: tablets before phones (iPad / Galaxy Tab), the first matching kind wins.
# "(?<![a-z])" keeps short tokens like "esp", "tv", "hue" from matching inside other words.
_KIND_PATTERNS = [
    ("tablet", re.compile(r"ipad|tablet|galaxy[-_ ]?tab|(?<![a-z])tab[-_ ]?s\d")),
    ("phone", re.compile(r"iphone|galaxy|pixel|android|redmi|xiaomi|oneplus|huawei|(?<![a-z])poco")),
    (
        "computer",
        re.compile(r"macbook|imac|laptop|desktop-|thinkpad|notebook|(?<![a-z])pc(?![a-z])"),
    ),
    ("tv", re.compile(r"(?<![a-z])tv(?![a-z])|bravia|webos|tizen|chromecast|roku|firetv|appletv")),
    (
        "iot",
        re.compile(r"(?<![a-z])esp|espressif|tuya|shelly|sonoff|tapo|(?<![a-z])hue(?![a-z])|plug|vacuum"),
    ),
]


def rssi_to_distance(rssi: float, p0: float = DEFAULT_P0_DBM, n: float = DEFAULT_PATH_LOSS_N) -> float:
    """Log-distance path loss: d = 10 ** ((P0 - RSSI) / (10 n)) metres, clamped to 0.5-30 m."""
    distance = 10 ** ((p0 - rssi) / (10 * n))
    return min(MAX_DISTANCE_M, max(MIN_DISTANCE_M, distance))


def ema(values: Sequence[float], alpha: float = EMA_ALPHA) -> list[float]:
    """Exponential moving average, one smoothed value per input (the first is the input itself)."""
    if not 0 < alpha <= 1:
        raise ValueError(f"alpha must be in (0, 1], got {alpha}")
    out: list[float] = []
    for value in values:
        out.append(value if not out else alpha * value + (1 - alpha) * out[-1])
    return out


def angle_for_mac(mac: str) -> float:
    """Stable pseudo-random angle in [0, 360) degrees from the MAC's sha256."""
    digest = hashlib.sha256(normalize_mac(mac).encode("ascii")).digest()
    return int.from_bytes(digest[:4], "big") / 2**32 * 360


def _match_kind(text: str | None) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(lowered):
            return kind
    return None


def guess_kind(hostname: str | None, vendor: str | None) -> str:
    """Radar kind from the hostname, else from the OUI vendor, else "unknown"."""
    return _match_kind(hostname) or _match_kind(vendor) or "unknown"


def device_kind(device: Device) -> tuple[str, bool]:
    """(radar kind, guessed?). A kind set by the user wins; laptop/desktop map to "computer"."""
    if device.kind:
        kind = _STORED_KIND.get(device.kind, device.kind)
        return (kind if kind in KINDS else "unknown"), False
    return guess_kind(device.hostname, device.vendor), True


@dataclass
class Position:
    connection: str = "unknown"
    wired: bool = False
    rssi: int | None = None
    rssi_smoothed: float | None = None
    distance_m: float | None = None
    trail: list[dict] = field(default_factory=list)


def _downsample(points: list[dict], limit: int) -> list[dict]:
    if len(points) <= limit:
        return points
    step = len(points) / limit
    picked = [points[int(i * step)] for i in range(limit - 1)]
    return [*picked, points[-1]]


def position(
    samples: Sequence[DeviceObservation],
    since: datetime,
    p0: float = DEFAULT_P0_DBM,
    n: float = DEFAULT_PATH_LOSS_N,
    alpha: float = EMA_ALPHA,
    max_trail: int = MAX_TRAIL_POINTS,
) -> Position:
    """Distance estimate from a device's recent observations (oldest first).

    Wired (by the latest observation): fixed next to the router. No RSSI at all: distance None.
    The trail holds the smoothed distance of samples at or after `since`, at most `max_trail` points.
    """
    if not samples:
        return Position()
    latest = samples[-1]
    if latest.connection == "wired":
        return Position(connection="wired", wired=True, distance_m=WIRED_DISTANCE_M)
    wireless = [s for s in samples if s.signal is not None and s.connection != "wired"]
    if not wireless:
        return Position(connection=latest.connection)
    smoothed = ema([s.signal for s in wireless], alpha)
    trail = [
        {"ts": s.ts, "distance_m": round(rssi_to_distance(v, p0, n), 2)}
        for s, v in zip(wireless, smoothed, strict=True)
        if s.ts >= since
    ]
    return Position(
        connection=latest.connection,
        rssi=wireless[-1].signal,
        rssi_smoothed=round(smoothed[-1], 1),
        distance_m=round(rssi_to_distance(smoothed[-1], p0, n), 2),
        trail=_downsample(trail, max_trail),
    )
