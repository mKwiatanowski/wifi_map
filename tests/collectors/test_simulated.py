import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from homesense.collectors.simulated import (
    SimDevice,
    SimPersona,
    SimulatedCollector,
    SimulatedHousehold,
    default_household,
)
from homesense.core.clock import FakeClock
from homesense.core.models import DeviceObservation, DnsQueryRecord, is_random_mac, normalize_mac

WARSAW = ZoneInfo("Europe/Warsaw")


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=WARSAW).astimezone(UTC)


MONDAY_10 = local(2026, 10, 19, 10)
MONDAY_20 = local(2026, 10, 19, 20)
SATURDAY_10 = local(2026, 10, 24, 10)


def persona(household: SimulatedHousehold, name: str) -> SimPersona:
    return next(p for p in household.personas if p.name == name)


def household_at(at: datetime, seed: int = 42) -> tuple[SimulatedHousehold, FakeClock]:
    clock = FakeClock(at)
    return SimulatedHousehold(clock, seed=seed), clock


def test_default_household_shape():
    personas, shared = default_household()
    assert [p.name for p in personas] == ["Anna", "Piotr", "Ola"]
    assert {d.kind for d in shared} == {"tv", "iot", "laptop"}
    assert sum(d.kind == "iot" for d in shared) == 2
    assert any("DESKTOP-ABC123" in d.hostname for d in shared)
    anna = personas[0]
    assert anna.weekday_away == (7.5, 16.0)
    all_devices = [d for p in personas for d in p.devices] + shared
    for d in all_devices:
        assert d.mac == normalize_mac(d.mac)
        assert not is_random_mac(d.mac)
        assert d.ip.startswith("192.168.0.")
    assert len({d.mac for d in all_devices}) == len(all_devices)
    assert len({d.ip for d in all_devices}) == len(all_devices)


def test_anna_weekday_away_mid_morning_home_in_evening():
    household, _ = household_at(MONDAY_10)
    anna = persona(household, "Anna")
    assert household.is_home(anna, MONDAY_10) is False
    assert household.is_home(anna, MONDAY_20) is True


def test_weekend_schedule_differs_from_weekday():
    household, _ = household_at(SATURDAY_10)
    anna = persona(household, "Anna")
    assert household.is_home(anna, MONDAY_10) is False
    assert household.is_home(anna, SATURDAY_10) is True


def test_persona_without_away_window_is_always_home():
    household, _ = household_at(MONDAY_10)
    homebody = SimPersona(name="Babcia", devices=[], weekday_away=None, weekend_away=None)
    assert all(household.is_home(homebody, MONDAY_10 + timedelta(hours=h)) for h in range(0, 48, 3))


def test_departure_jitter_is_within_30_minutes_and_varies_by_day():
    household, _ = household_at(MONDAY_10)
    anna = persona(household, "Anna")
    departures = []
    for day in range(19, 24):  # Mon..Fri
        minutes = [
            m
            for m in range(6 * 60, 9 * 60)
            if not household.is_home(anna, local(2026, 10, day, 0) + timedelta(minutes=m))
        ]
        first_away = minutes[0] / 60
        assert 7.0 <= first_away <= 8.0
        departures.append(first_away)
    assert len(set(departures)) > 1


def test_is_home_uses_local_time_across_dst_change():
    household, _ = household_at(MONDAY_10)
    anna = persona(household, "Anna")
    # 2026-10-26 (Monday, after DST end) 10:00 local is 09:00 UTC
    assert household.is_home(anna, local(2026, 10, 26, 10)) is False
    assert household.is_home(anna, local(2026, 10, 26, 20)) is True


def test_same_seed_same_time_gives_same_output():
    a, _ = household_at(MONDAY_20, seed=7)
    b, _ = household_at(MONDAY_20, seed=7)
    assert a.device_observations() == b.device_observations()
    assert a.dns_queries(300) == b.dns_queries(300)


def test_different_seed_changes_output():
    outputs = set()
    for seed in range(5):
        h, _ = household_at(MONDAY_20, seed=seed)
        outputs.add(tuple(h.dns_queries(600)))
    assert len(outputs) > 1


def test_output_is_deterministic_across_processes():
    code = (
        "from datetime import datetime, UTC\n"
        "from homesense.core.clock import FakeClock\n"
        "from homesense.collectors.simulated import SimulatedHousehold\n"
        "h = SimulatedHousehold(FakeClock(datetime(2026, 10, 19, 18, 0, tzinfo=UTC)), seed=3)\n"
        "h.inject_guest(datetime(2026, 10, 19, 17, 30, tzinfo=UTC))\n"
        "print(repr(h.device_observations()), repr(h.dns_queries(600)))\n"
        "early = [datetime(2026, 10, 19, 5, m, tzinfo=UTC) for m in range(60)]\n"
        "print([h.is_home(p, at) for p in h.personas for at in early])\n"
    )
    outputs = []
    for hash_seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]


def test_observations_are_simulated_utc_and_online():
    household, clock = household_at(MONDAY_20)
    observations = household.device_observations()
    assert observations
    for obs in observations:
        assert isinstance(obs, DeviceObservation)
        assert obs.source == "simulated"
        assert obs.online is True
        assert obs.ts == clock.now()
        assert obs.ts.utcoffset() == timedelta(0)
        assert obs.mac == normalize_mac(obs.mac)
        assert obs.down_kbps is not None and 0 <= obs.down_kbps <= 5000


def test_shared_devices_always_online_and_absent_personas_devices_offline():
    household, clock = household_at(MONDAY_10)
    _, shared = default_household()
    macs = {o.mac for o in household.device_observations()}
    assert {d.mac for d in shared} <= macs
    anna = persona(household, "Anna")
    assert not ({d.mac for d in anna.devices} & macs)


def test_laptops_online_only_in_evening_when_home():
    personas, _ = default_household()
    laptop_macs = {d.mac for p in personas for d in p.devices if d.kind == "laptop"}
    household, clock = household_at(local(2026, 10, 24, 12))  # Saturday noon
    noon = {o.mac for o in household.device_observations()}
    assert not (laptop_macs & noon)
    clock.set(local(2026, 10, 19, 21))  # Monday 21:00, everyone home
    evening = {o.mac for o in household.device_observations()}
    assert laptop_macs <= evening


def test_phones_asleep_about_ten_percent_of_minutes_when_home():
    household, clock = household_at(MONDAY_20)
    anna = persona(household, "Anna")
    phone = next(d for d in anna.devices if d.kind == "phone")
    start = local(2026, 10, 19, 19)
    online = 0
    for minute in range(240):  # 19:00-23:00 local, Anna home
        clock.set(start + timedelta(minutes=minute))
        assert household.is_home(anna, clock.now())
        online += any(o.mac == phone.mac for o in household.device_observations())
    asleep_ratio = 1 - online / 240
    assert 0.02 <= asleep_ratio <= 0.2


def test_dns_queries_only_for_online_devices_and_some_blocked_over_an_hour():
    household, clock = household_at(local(2026, 10, 19, 19))
    records: list[DnsQueryRecord] = []
    for _ in range(120):  # one hour of 30 s samples
        clock.advance(timedelta(seconds=30))
        online_ips = {o.ip for o in household.device_observations()}
        window = household.dns_queries(30)
        for record in window:
            assert isinstance(record, DnsQueryRecord)
            assert record.client_ip in online_ips
            assert clock.now() - timedelta(seconds=30) < record.ts <= clock.now()
            assert record.ts.utcoffset() == timedelta(0)
        records.extend(window)
    assert len(records) > 50
    blocked = [r for r in records if r.blocked]
    assert blocked
    assert len(blocked) < len(records) * 0.2
    assert all(r.reason and r.reason.startswith("Filtered") for r in blocked)
    assert all(not (r.reason or "").startswith("Filtered") for r in records if not r.blocked)


def test_dns_queries_never_from_absent_persona_devices():
    household, clock = household_at(MONDAY_10)
    anna = persona(household, "Anna")
    anna_ips = {d.ip for d in anna.devices}
    assert not any(r.client_ip in anna_ips for r in household.dns_queries(3600))


def test_guest_present_only_inside_injected_window():
    start = local(2026, 10, 19, 18)
    household, clock = household_at(start - timedelta(minutes=5))
    household.inject_guest(start, minutes=30)

    def guest_obs() -> list[DeviceObservation]:
        return [o for o in household.device_observations() if o.hostname == "Guest-Phone"]

    assert guest_obs() == []
    clock.set(start)
    seen = guest_obs()
    assert len(seen) == 1
    guest = seen[0]
    assert guest.mac == normalize_mac(guest.mac)
    assert is_random_mac(guest.mac)
    known = {d.mac for p in household.personas for d in p.devices} | {d.mac for d in household.shared}
    assert guest.mac not in known
    clock.set(start + timedelta(minutes=29))
    assert len(guest_obs()) == 1
    clock.set(start + timedelta(minutes=30))
    assert guest_obs() == []


def test_inject_guest_rejects_naive_datetime():
    household, _ = household_at(MONDAY_10)
    with pytest.raises(ValueError):
        household.inject_guest(datetime(2026, 10, 19, 10, 0))


def test_custom_household():
    device = SimDevice(mac="00:11:22:33:44:55", ip="192.168.0.200", hostname="Solo-Phone", kind="phone")
    solo = SimPersona(name="Solo", devices=[device], weekday_away=None, weekend_away=None)
    household = SimulatedHousehold(FakeClock(MONDAY_10), personas=[solo], shared=[])
    assert household.personas == [solo]
    assert household.shared == []
    macs = {o.mac for o in household.device_observations()}
    assert macs <= {device.mac}


async def test_collector_returns_devices_and_dns_for_interval():
    household, clock = household_at(MONDAY_20)
    collector = SimulatedCollector(household, interval_s=600)
    assert collector.name == "simulated"
    assert collector.interval_s == 600
    observations = await collector.collect()
    expected = household.device_observations() + household.dns_queries(600)
    assert observations == expected
    assert any(isinstance(o, DnsQueryRecord) for o in observations)
    assert any(isinstance(o, DeviceObservation) for o in observations)


# --- radio signal (for the device radar) ---


def _signals_over_time(seed: int = 42, minutes: int = 60) -> dict[str, list[tuple[str, int | None]]]:
    household, clock = household_at(MONDAY_20, seed=seed)
    out: dict[str, list[tuple[str, int | None]]] = {}
    for _ in range(minutes):
        for o in household.device_observations():
            out.setdefault(o.mac, []).append((o.connection, o.signal))
        clock.advance(timedelta(minutes=1))
    return out


def test_phones_signal_moves_within_range():
    signals = _signals_over_time()
    phone = signals["f0:18:98:a1:00:01"]  # Anna-iPhone, home on Monday evening
    values = [s for _, s in phone]
    assert all(c == "5g" for c, _ in phone)
    assert all(-82 <= v <= -40 for v in values)
    assert max(values) - min(values) >= 10
    steps = [abs(b - a) for a, b in zip(values, values[1:], strict=False)]
    assert max(steps) <= 12  # wanders, does not teleport


def test_laptop_signal_mostly_stable_and_iot_fixed():
    signals = _signals_over_time()
    laptop = [s for _, s in signals["3c:a9:f4:a1:00:02"]]  # Anna-Laptop, online 18-23
    assert max(laptop) - min(laptop) <= 4
    plug = {s for _, s in signals["d8:1f:12:d4:00:02"]}
    assert len(plug) == 1 and -85 <= next(iter(plug)) <= -55


def test_wired_shared_devices_have_no_signal():
    signals = _signals_over_time(minutes=2)
    for mac in ("a8:23:fe:d4:00:01", "98:fa:9b:d4:00:04"):  # LG-TV, DESKTOP-ABC123
        assert signals[mac] and all(c == "wired" and s is None for c, s in signals[mac])


def test_signal_is_deterministic_per_seed():
    assert _signals_over_time(minutes=5) == _signals_over_time(minutes=5)
    assert _signals_over_time(minutes=5) != _signals_over_time(seed=7, minutes=5)


def test_survey_signal_follows_log_distance_and_forces_device_online(clock):
    from homesense.collectors.simulated import SimulatedHousehold

    household = SimulatedHousehold(clock, p0_dbm=-40.0, path_loss_n=3.0)
    phone = household.personas[0].devices[0]
    near = household.survey_signal(phone, 1.0, clock.now())
    far = household.survey_signal(phone, 10.0, clock.now())
    assert -42 <= near <= -38 and -72 <= far <= -68
    assert household.survey_signal(phone, 1000.0, clock.now()) == -95  # clamped

    # 08:00 local on a Monday: Anna is out, but the phone being measured is reported anyway.
    macs = {o.mac for o in household.device_observations()}
    assert phone.mac not in macs
    obs = {o.mac: o for o in household.device_observations({phone.mac: 10.0})}
    assert -72 <= obs[phone.mac].signal <= -68
    wired = household.shared[-1]
    assert obs[wired.mac].signal is None  # wired devices are never given a survey RSSI
