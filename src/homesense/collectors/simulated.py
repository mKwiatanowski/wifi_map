"""Simulated household: the only data source used in dev (no real network).

All randomness is derived from stable string seeds (``random.Random(str)`` seeds deterministically
from the string, unlike ``hash()`` of a tuple which changes with PYTHONHASHSEED), so the same seed
and the same instant give the same output in every process.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from homesense.core.clock import Clock
from homesense.core.models import DeviceObservation, DnsQueryRecord, Observation, normalize_mac
from homesense.core.timeutil import require_aware, to_local

SOURCE = "simulated"
GUEST_HOSTNAME = "Guest-Phone"

JITTER_H = 0.5
ASLEEP_RATIO = 0.10
LAPTOP_HOURS = (18, 23)
TV_EVENING_HOURS = (18, 23)
AD_RATIO = 0.05
MAX_DOWN_KBPS = 5000.0

# DNS queries per hour by device kind (tv only in the evening).
DNS_RATE_PER_H = {"phone": 30.0, "laptop": 40.0, "tv": 60.0, "iot": 20.0}

DOMAINS = {
    "phone": [
        "graph.facebook.com",
        "i.instagram.com",
        "www.youtube.com",
        "api.whatsapp.net",
        "www.google.com",
        "mail.google.com",
        "api.spotify.com",
        "www.onet.pl",
        "allegro.pl",
        "icloud.com",
        "gateway.icloud.com",
        "www.tiktok.com",
    ],
    "laptop": [
        "www.google.com",
        "github.com",
        "outlook.office365.com",
        "teams.microsoft.com",
        "www.wikipedia.org",
        "www.netflix.com",
        "stackoverflow.com",
        "www.wp.pl",
        "update.microsoft.com",
        "www.olx.pl",
    ],
    "tv": [
        "www.netflix.com",
        "nflxvideo.net",
        "www.youtube.com",
        "googlevideo.com",
        "lgtvsdp.com",
        "player.pl",
        "api.hbomax.com",
        "www.disneyplus.com",
    ],
    "iot": [
        "a1.tuyaeu.com",
        "m1.tuyaeu.com",
        "pool.ntp.org",
        "time.google.com",
        "mqtt.tuyaeu.com",
        "firmware.tuyaeu.com",
    ],
}
AD_DOMAINS = [
    "doubleclick.net",
    "googleads.g.doubleclick.net",
    "ads.facebook.com",
    "adservice.google.com",
    "app-measurement.com",
    "telemetry.lgtvcommon.com",
    "ads.yahoo.com",
    "tracking.tuyaeu.com",
]
REASON_ALLOWED = "NotFilteredNotFound"
REASON_BLOCKED = "FilteredBlackList"

CONNECTION_BY_KIND = {"phone": "5g", "laptop": "5g", "tv": "wired", "iot": "2g"}

# RSSI (dBm) for the device radar. Phones wander around the flat, laptops sit on a desk with a
# little noise, IoT devices never move.
PHONE_RSSI = (-82.0, -40.0)
PHONE_PERIODS_S = ((900.0, 2400.0), (240.0, 600.0))
LAPTOP_RSSI = (-62.0, -48.0)
LAPTOP_NOISE_DB = 2
FIXED_RSSI = (-78.0, -60.0)


@dataclass
class SimDevice:
    mac: str
    ip: str
    hostname: str
    kind: str  # phone|laptop|tv|iot
    connection: str | None = None  # None = by kind (CONNECTION_BY_KIND)

    @property
    def link(self) -> str:
        return self.connection or CONNECTION_BY_KIND.get(self.kind, "unknown")


@dataclass
class SimPersona:
    name: str
    devices: list[SimDevice]
    # local hours away, e.g. (7.5, 16.0); None = home all day
    weekday_away: tuple[float, float] | None
    weekend_away: tuple[float, float] | None


def default_household() -> tuple[list[SimPersona], list[SimDevice]]:
    """Three personas (Anna, Piotr, Ola) and the shared devices (TV, 2 IoT, desktop PC)."""
    personas = [
        SimPersona(
            name="Anna",
            devices=[
                SimDevice("f0:18:98:a1:00:01", "192.168.0.101", "Anna-iPhone", "phone"),
                SimDevice("3c:a9:f4:a1:00:02", "192.168.0.102", "Anna-Laptop", "laptop"),
            ],
            weekday_away=(7.5, 16.0),
            weekend_away=(11.0, 14.0),
        ),
        SimPersona(
            name="Piotr",
            devices=[
                SimDevice("8c:f5:a3:b2:00:01", "192.168.0.111", "Piotr-Galaxy", "phone"),
                SimDevice("98:fa:9b:b2:00:02", "192.168.0.112", "Piotr-ThinkPad", "laptop"),
            ],
            weekday_away=(8.0, 17.5),
            weekend_away=None,
        ),
        SimPersona(
            name="Ola",
            devices=[
                SimDevice("64:09:80:c3:00:01", "192.168.0.121", "Ola-Redmi", "phone"),
                SimDevice("3c:a9:f4:c3:00:02", "192.168.0.122", "Ola-Laptop", "laptop"),
            ],
            weekday_away=(7.75, 14.5),
            weekend_away=(15.0, 20.0),
        ),
    ]
    shared = [
        SimDevice("a8:23:fe:d4:00:01", "192.168.0.50", "LG-TV", "tv"),
        SimDevice("d8:1f:12:d4:00:02", "192.168.0.60", "Smart-Plug", "iot"),
        SimDevice("d8:1f:12:d4:00:03", "192.168.0.61", "Robot-Vacuum", "iot"),
        SimDevice("98:fa:9b:d4:00:04", "192.168.0.10", "DESKTOP-ABC123", "laptop", connection="wired"),
    ]
    return personas, shared


def _hour_of_day(local_dt: datetime) -> float:
    return local_dt.hour + local_dt.minute / 60 + local_dt.second / 3600


def _in_hours(hour: float, window: tuple[int, int]) -> bool:
    return window[0] <= hour < window[1]


@dataclass
class _GuestVisit:
    device: SimDevice
    start: datetime
    end: datetime


class SimulatedHousehold:
    def __init__(
        self,
        clock: Clock,
        tz: str = "Europe/Warsaw",
        seed: int = 42,
        personas: list[SimPersona] | None = None,
        shared: list[SimDevice] | None = None,
    ) -> None:
        default_personas, default_shared = default_household()
        self.personas = default_personas if personas is None else personas
        self.shared = default_shared if shared is None else shared
        self._clock = clock
        self._tz = tz
        self._seed = seed
        self._guests: list[_GuestVisit] = []

    def _rng(self, *parts: object) -> random.Random:
        return random.Random(":".join(str(p) for p in (self._seed, *parts)))

    def is_home(self, persona: SimPersona, at: datetime) -> bool:
        local_dt = to_local(at, self._tz)
        day = local_dt.date()
        window = persona.weekend_away if day.weekday() >= 5 else persona.weekday_away
        if window is None:
            return True
        rng = self._rng("away", persona.name, day.isoformat())
        leave = window[0] + rng.uniform(-JITTER_H, JITTER_H)
        back = window[1] + rng.uniform(-JITTER_H, JITTER_H)
        return not (leave <= _hour_of_day(local_dt) < back)

    def _phone_asleep(self, device: SimDevice, at: datetime) -> bool:
        minute = int(at.timestamp() // 60)
        return self._rng("asleep", device.mac, minute).random() < ASLEEP_RATIO

    def _online_devices(self, now: datetime) -> list[SimDevice]:
        hour = _hour_of_day(to_local(now, self._tz))
        online: list[SimDevice] = []
        for persona in self.personas:
            if not self.is_home(persona, now):
                continue
            for device in persona.devices:
                if device.kind == "phone" and self._phone_asleep(device, now):
                    continue
                if device.kind == "laptop" and not _in_hours(hour, LAPTOP_HOURS):
                    continue
                online.append(device)
        online.extend(self.shared)
        online.extend(g.device for g in self._guests if g.start <= now < g.end)
        return online

    def signal(self, device: SimDevice, at: datetime) -> int | None:
        """Simulated RSSI at `at`: None for wired devices.

        Stateless, so every process agrees: a phone's "random walk" is two sine waves with
        per-device random periods and phases plus a little per-sample noise.
        """
        if device.link == "wired":
            return None
        t = at.timestamp()
        rng = self._rng("rssi", device.mac)
        noise = self._rng("rssi-noise", device.mac, int(t))
        if device.kind == "phone":
            low, high = PHONE_RSSI
            wave = 0.0
            for weight, (p_min, p_max) in zip((0.65, 0.35), PHONE_PERIODS_S, strict=True):
                period, phase = rng.uniform(p_min, p_max), rng.uniform(0, 2 * math.pi)
                wave += weight * math.sin(2 * math.pi * t / period + phase)
            value = (low + high) / 2 + (high - low) / 2 * wave + noise.uniform(-1.5, 1.5)
            return round(min(high, max(low, value)))
        if device.kind == "laptop":
            base = rng.uniform(*LAPTOP_RSSI)
            return round(base) + noise.randint(-LAPTOP_NOISE_DB // 2, LAPTOP_NOISE_DB // 2)
        return round(rng.uniform(*FIXED_RSSI))

    def device_observations(self) -> list[DeviceObservation]:
        now = require_aware(self._clock.now()).astimezone(UTC)
        observations = []
        for device in self._online_devices(now):
            rng = self._rng("traffic", device.mac, now.isoformat())
            observations.append(
                DeviceObservation(
                    ts=now,
                    mac=normalize_mac(device.mac),
                    source=SOURCE,
                    online=True,
                    ip=device.ip,
                    hostname=device.hostname,
                    connection=device.link,
                    signal=self.signal(device, now),
                    down_kbps=round(rng.uniform(0, MAX_DOWN_KBPS), 1),
                )
            )
        return observations

    def _dns_rate_per_h(self, device: SimDevice, hour: float) -> float:
        if device.kind == "tv" and not _in_hours(hour, TV_EVENING_HOURS):
            return 0.0
        return DNS_RATE_PER_H.get(device.kind, 0.0)

    def dns_queries(self, window_s: float) -> list[DnsQueryRecord]:
        """Queries in (now - window_s, now] for currently online devices (Poisson arrivals)."""
        now = require_aware(self._clock.now()).astimezone(UTC)
        hour = _hour_of_day(to_local(now, self._tz))
        records: list[DnsQueryRecord] = []
        for device in self._online_devices(now):
            rate_per_s = self._dns_rate_per_h(device, hour) / 3600
            if rate_per_s <= 0 or window_s <= 0:
                continue
            rng = self._rng("dns", device.mac, now.isoformat(), window_s)
            elapsed = rng.expovariate(rate_per_s)
            while elapsed < window_s:
                ts = now - timedelta(seconds=window_s - elapsed)
                if rng.random() < AD_RATIO:
                    domain, blocked, reason = rng.choice(AD_DOMAINS), True, REASON_BLOCKED
                else:
                    domain, blocked, reason = rng.choice(DOMAINS[device.kind]), False, REASON_ALLOWED
                records.append(
                    DnsQueryRecord(ts=ts, client_ip=device.ip, domain=domain, blocked=blocked, reason=reason)
                )
                elapsed += rng.expovariate(rate_per_s)
        records.sort(key=lambda r: r.ts)
        return records

    def inject_guest(self, at: datetime, minutes: int = 60) -> None:
        """An unknown phone with a random (locally administered) MAC present in [at, at + minutes)."""
        start = require_aware(at).astimezone(UTC)
        rng = self._rng("guest", start.isoformat(), len(self._guests))
        octets = [rng.randrange(256) for _ in range(6)]
        octets[0] = (octets[0] & 0xFC) | 0x02  # unicast, locally administered
        mac = ":".join(f"{o:02x}" for o in octets)
        ip = f"192.168.0.{200 + len(self._guests) % 50}"
        device = SimDevice(mac=mac, ip=ip, hostname=GUEST_HOSTNAME, kind="phone")
        self._guests.append(_GuestVisit(device=device, start=start, end=start + timedelta(minutes=minutes)))


class SimulatedCollector:
    name = "simulated"

    def __init__(self, household: SimulatedHousehold, interval_s: float = 30) -> None:
        self.interval_s = interval_s
        self._household = household

    async def collect(self) -> list[Observation]:
        observations: list[Observation] = list(self._household.device_observations())
        observations.extend(self._household.dns_queries(self.interval_s))
        return observations
