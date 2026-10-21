import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

_HEX12 = re.compile(r"[0-9a-f]{12}")


def normalize_mac(mac: str) -> str:
    digits = re.sub(r"[-:.]", "", mac.strip().lower())
    if not _HEX12.fullmatch(digits):
        raise ValueError(f"invalid MAC address: {mac!r}")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def is_random_mac(mac: str) -> bool:
    """True when the locally-administered bit is set (second hex digit in 2, 6, a, e)."""
    return normalize_mac(mac)[1] in "26ae"


@dataclass(frozen=True)
class DeviceObservation:
    ts: datetime
    mac: str
    source: str
    online: bool = True
    ip: str | None = None
    hostname: str | None = None
    connection: str = "unknown"  # wired|2g|5g|6g|unknown
    signal: int | None = None
    up_kbps: float | None = None
    down_kbps: float | None = None


@dataclass(frozen=True)
class DnsQueryRecord:
    ts: datetime
    client_ip: str
    domain: str
    blocked: bool = False
    reason: str | None = None


Observation = DeviceObservation | DnsQueryRecord


@dataclass
class Person:
    id: int
    name: str
    color: str | None
    privacy_mode: str
    created_at: datetime


@dataclass
class Device:
    mac: str
    person_id: int | None
    label: str | None
    hostname: str | None
    vendor: str | None
    ip: str | None
    first_seen: datetime
    last_seen: datetime
    trusted: bool
    counts_for_presence: bool
    kind: str | None
    notes: str | None

    @property
    def display_name(self) -> str:
        return self.label or self.hostname or self.vendor or self.mac

    @property
    def is_random(self) -> bool:
        return is_random_mac(self.mac)


@dataclass(frozen=True)
class PresenceEvent:
    ts: datetime
    person_id: int
    kind: Literal["arrive", "leave"]
    mac: str | None = None


@dataclass
class PresenceInterval:
    person_id: int
    start_ts: datetime
    end_ts: datetime | None


@dataclass
class Alert:
    kind: str
    severity: Literal["info", "warning", "critical"]
    title: str
    message: str
    ts: datetime
    person_id: int | None = None
    mac: str | None = None
    data: dict = field(default_factory=dict)
    dedup_key: str | None = None
    id: int | None = None
    acknowledged: bool = False


@dataclass
class CollectorStatus:
    name: str
    last_ok_ts: datetime | None
    last_error_ts: datetime | None
    last_error: str | None
    consecutive_failures: int


@dataclass(frozen=True)
class ObservationsBatch:
    observations: tuple[Observation, ...]


@dataclass(frozen=True)
class NewDeviceEvent:
    mac: str
    ts: datetime


@dataclass(frozen=True)
class AlertRaised:
    alert: Alert


@dataclass(frozen=True)
class MonitoringPause:
    id: int
    start_ts: datetime
    until_ts: datetime | None  # None = until resumed by hand
    end_ts: datetime | None  # None = still paused


@dataclass(frozen=True)
class MonitoringChanged:
    """Published when monitoring is paused or resumed (also on auto-resume of a timed pause)."""

    paused: bool
    until: datetime | None
    ts: datetime


@dataclass(frozen=True)
class CoverageRoom:
    name: str
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class CoveragePlan:
    """Floor plan of the coverage survey; metres, origin top-left, y grows downwards."""

    width_m: float
    height_m: float
    router: tuple[float, float] | None
    rooms: tuple[CoverageRoom, ...] = ()


@dataclass(frozen=True)
class CoveragePoint:
    id: int
    mac: str
    x: float
    y: float
    started_at: datetime
    ends_at: datetime
    done: bool
    rssi: float | None  # median dBm; None = no sample (after done: invalid point)
    samples: int
    band: str  # 2.4|5|6|unknown
