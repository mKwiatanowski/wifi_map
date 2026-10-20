from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from homesense.alerts.service import AlertService
from homesense.core.events import EventBus
from homesense.core.models import Alert, DeviceObservation, PresenceEvent
from homesense.patterns.detectors import (
    BlockedBurst,
    DetectorContext,
    DnsSpike,
    NewDomainBurst,
    NightActivity,
    TrafficSpike,
    UnknownDevicePresent,
    UnusualArrival,
    UnusualPresence,
    default_detectors,
    run_detectors,
)
from homesense.presence.engine import PersonState

# clock fixture: Mon 2026-10-19 06:00 UTC = 08:00 local (CEST)
TZ = "Europe/Warsaw"
WAW = ZoneInfo(TZ)
MAC = "aa:bb:cc:dd:ee:01"
MAC2 = "aa:bb:cc:dd:ee:02"
H = timedelta(hours=1)


def local(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=WAW).astimezone(UTC)


def ctx(repos, clock, presence=None) -> DetectorContext:
    return DetectorContext(repos=repos, now=clock.now(), tz=TZ, presence=presence or [])


def last_hour(clock) -> datetime:
    return clock.now().replace(minute=0, second=0, microsecond=0) - H


# --- UnusualPresence ---


def test_unusual_presence_home_at_rare_slot(repos, clock):
    pid = repos.persons.create("Ala")
    repos.profiles.replace("person_home", str(pid), {(0, 8): (0.05, 4)}, clock.now())
    alerts = UnusualPresence().evaluate(ctx(repos, clock, [PersonState(pid, True, None, None)]))
    assert len(alerts) == 1
    a = alerts[0]
    assert (a.kind, a.severity, a.person_id) == ("unusual_presence", "warning", pid)
    assert a.title == "Nietypowa obecność"
    assert "Ala" in a.message and "w domu" in a.message
    assert a.dedup_key == f"unusual_presence:{pid}:2026101906"


def test_unusual_presence_away_at_usual_home_slot(repos, clock):
    pid = repos.persons.create("Ala")
    repos.profiles.replace("person_home", str(pid), {(0, 8): (0.95, 5)}, clock.now())
    alerts = UnusualPresence().evaluate(ctx(repos, clock, [PersonState(pid, False, None, None)]))
    assert len(alerts) == 1 and "poza domem" in alerts[0].message


@pytest.mark.parametrize(
    ("value", "home"),
    [((0.05, 3), True), ((0.5, 10), True), ((0.05, 10), False), ((0.95, 10), True)],
)
def test_unusual_presence_negative(repos, clock, value, home):
    pid = repos.persons.create("Ala")
    repos.profiles.replace("person_home", str(pid), {(0, 8): value}, clock.now())
    assert UnusualPresence().evaluate(ctx(repos, clock, [PersonState(pid, home, None, None)])) == []


def test_unusual_presence_without_profile(repos, clock):
    pid = repos.persons.create("Ala")
    assert UnusualPresence().evaluate(ctx(repos, clock, [PersonState(pid, True, None, None)])) == []


# --- UnusualArrival ---


def _past_monday_arrivals(repos, pid, n=4):
    for i, (day, minute) in enumerate([(21, 0), (28, 10), (5, 20), (12, 30)][:n]):
        month = 9 if day > 15 else 10
        repos.presence.add_event(
            PresenceEvent(ts=local(2026, month, day, 17, minute), person_id=pid, kind="arrive")
        )
        repos.presence.add_event(PresenceEvent(ts=local(2026, month, day, 7, i), person_id=pid, kind="leave"))


def test_unusual_arrival_far_from_median(repos, clock):
    pid = repos.persons.create("Ala")
    _past_monday_arrivals(repos, pid)
    ts = clock.now() - timedelta(minutes=5)  # 07:55 local, typical arrival ~17:15
    repos.presence.add_event(PresenceEvent(ts=ts, person_id=pid, kind="arrive"))
    alerts = UnusualArrival().evaluate(ctx(repos, clock))
    assert len(alerts) == 1
    a = alerts[0]
    assert (a.kind, a.severity, a.person_id) == ("unusual_arrival", "info", pid)
    assert a.title == "Nietypowa godzina powrotu"
    assert "Ala" in a.message and "07:55" in a.message and "17:15" in a.message
    assert a.dedup_key == f"unusual_arrival:{pid}:20261019055500"


def test_unusual_departure_far_from_median(repos, clock):
    pid = repos.persons.create("Ala")
    _past_monday_arrivals(repos, pid)  # leaves at 07:00-07:03
    clock.set(local(2026, 10, 19, 10, 10))
    repos.presence.add_event(PresenceEvent(ts=local(2026, 10, 19, 10, 5), person_id=pid, kind="leave"))
    alerts = UnusualArrival().evaluate(ctx(repos, clock))
    assert len(alerts) == 1
    a = alerts[0]
    assert a.title == "Nietypowa godzina wyjścia"
    assert "Ala wyszedł/wyszła" in a.message and "10:05" in a.message
    assert a.data["event"] == "leave"


def test_unusual_arrival_within_tolerance(repos, clock):
    pid = repos.persons.create("Ala")
    _past_monday_arrivals(repos, pid)
    clock.set(local(2026, 10, 19, 18, 20))
    # median 17:15, IQR 15 min -> tolerance 60 min
    repos.presence.add_event(PresenceEvent(ts=local(2026, 10, 19, 18, 14), person_id=pid, kind="arrive"))
    assert UnusualArrival().evaluate(ctx(repos, clock)) == []


def test_unusual_arrival_needs_four_samples(repos, clock):
    pid = repos.persons.create("Ala")
    _past_monday_arrivals(repos, pid, n=3)
    repos.presence.add_event(
        PresenceEvent(ts=clock.now() - timedelta(minutes=5), person_id=pid, kind="arrive")
    )
    assert UnusualArrival().evaluate(ctx(repos, clock)) == []


def test_unusual_arrival_ignores_old_events(repos, clock):
    pid = repos.persons.create("Ala")
    _past_monday_arrivals(repos, pid)
    repos.presence.add_event(
        PresenceEvent(ts=clock.now() - timedelta(minutes=11), person_id=pid, kind="arrive")
    )
    assert UnusualArrival().evaluate(ctx(repos, clock)) == []


# --- NightActivity ---


@pytest.fixture
def night(clock):
    clock.set(local(2026, 10, 19, 3, 10))  # last full hour = 02:00-03:00 local
    return local(2026, 10, 19, 2)


def test_night_activity(repos, clock, night):
    repos.devices.upsert_seen(MAC, night, hostname="tv")
    repos.dns.add_hourly_stats(night, MAC, 30, 0)
    repos.profiles.replace("device_dns", MAC, {(0, 2): (1.0, 4)}, clock.now())
    alerts = NightActivity().evaluate(ctx(repos, clock))
    assert len(alerts) == 1
    a = alerts[0]
    assert (a.kind, a.severity, a.mac) == ("night_activity", "warning", MAC)
    assert a.title == "Aktywność w nocy"
    assert "tv" in a.message and "30" in a.message and "zwykle około 1" in a.message
    assert a.dedup_key == f"night_activity:{MAC}:2026-10-19"


def test_night_activity_without_profile_slot(repos, clock, night):
    repos.dns.add_hourly_stats(night, MAC, 30, 0)
    alerts = NightActivity().evaluate(ctx(repos, clock))
    assert len(alerts) == 1 and "zwykle" not in alerts[0].message


async def test_night_activity_dedup_per_local_date(repos, clock, night):
    service = AlertService(repos, EventBus(), clock)
    repos.dns.add_hourly_stats(night, MAC, 30, 0)
    assert await run_detectors([NightActivity()], ctx(repos, clock), service) == 1
    clock.advance(H)  # 04:10 local, last full hour 03:00-04:00, same local date
    repos.dns.add_hourly_stats(night + H, MAC, 40, 0)
    assert await run_detectors([NightActivity()], ctx(repos, clock), service) == 0
    assert len(repos.alerts.list()) == 1


@pytest.mark.parametrize(("queries", "profile"), [(20, 1.0), (30, 5.0)])
def test_night_activity_negative(repos, clock, night, queries, profile):
    repos.dns.add_hourly_stats(night, MAC, queries, 0)
    repos.profiles.replace("device_dns", MAC, {(0, 2): (profile, 4)}, clock.now())
    assert NightActivity().evaluate(ctx(repos, clock)) == []


def test_night_activity_only_at_night(repos, clock):
    repos.dns.add_hourly_stats(last_hour(clock), MAC, 300, 0)  # 07:00 local
    assert NightActivity().evaluate(ctx(repos, clock)) == []


def test_night_activity_ignores_unknown_mac(repos, clock, night):
    repos.dns.add_hourly_stats(night, "unknown", 300, 0)
    assert NightActivity().evaluate(ctx(repos, clock)) == []


# --- DnsSpike ---


def _dns_history(repos, clock, mac=MAC, hours=48):
    start = last_hour(clock)
    for i in range(1, hours + 1):
        repos.dns.add_hourly_stats(start - i * H, mac, 40 if i % 2 else 60, 0)  # mean 50, sigma 10


def test_dns_spike(repos, clock):
    repos.devices.upsert_seen(MAC, clock.now(), hostname="laptop")
    _dns_history(repos, clock)
    repos.dns.add_hourly_stats(last_hour(clock), MAC, 500, 0)
    alerts = DnsSpike().evaluate(ctx(repos, clock))
    assert len(alerts) == 1
    a = alerts[0]
    assert (a.kind, a.severity, a.mac) == ("dns_spike", "warning", MAC)
    assert a.title == "Skok zapytań DNS"
    assert "laptop" in a.message and "500" in a.message
    assert a.dedup_key == f"dns_spike:{MAC}:2026101905"


def test_dns_spike_below_absolute_floor(repos, clock):
    _dns_history(repos, clock)
    repos.dns.add_hourly_stats(last_hour(clock), MAC, 150, 0)  # > mean+4 sigma, but <= 200
    assert DnsSpike().evaluate(ctx(repos, clock)) == []


def test_dns_spike_within_normal_variation(repos, clock):
    start = last_hour(clock)
    for i in range(1, 49):
        repos.dns.add_hourly_stats(start - i * H, MAC, 100 if i % 2 else 700, 0)  # mean 400, sigma 300
    repos.dns.add_hourly_stats(start, MAC, 900, 0)
    assert DnsSpike().evaluate(ctx(repos, clock)) == []


def test_dns_spike_needs_history(repos, clock):
    _dns_history(repos, clock, hours=10)
    repos.dns.add_hourly_stats(last_hour(clock), MAC, 500, 0)
    assert DnsSpike().evaluate(ctx(repos, clock)) == []


def test_dns_spike_uses_last_full_hour_only(repos, clock):
    _dns_history(repos, clock)
    repos.dns.add_hourly_stats(last_hour(clock) + H, MAC, 500, 0)  # current, incomplete hour
    assert DnsSpike().evaluate(ctx(repos, clock)) == []


# --- BlockedBurst ---


def test_blocked_burst(repos, clock):
    repos.dns.add_hourly_stats(last_hour(clock), MAC, 100, 51)
    repos.dns.add_hourly_stats(last_hour(clock), MAC2, 100, 50)
    alerts = BlockedBurst().evaluate(ctx(repos, clock))
    assert len(alerts) == 1
    a = alerts[0]
    assert (a.kind, a.severity, a.mac) == ("blocked_burst", "warning", MAC)
    assert a.title == "Dużo zablokowanych zapytań"
    assert MAC in a.message and "51" in a.message
    assert a.dedup_key == f"blocked_burst:{MAC}:2026101905"


def test_blocked_burst_ignores_older_hours(repos, clock):
    repos.dns.add_hourly_stats(last_hour(clock) - H, MAC, 100, 80)
    assert BlockedBurst().evaluate(ctx(repos, clock)) == []


# --- NewDomainBurst ---


def _new_domains(repos, clock, mac, n):
    for i in range(n):
        repos.dns.mark_domain_seen(mac, f"d{i}.example", last_hour(clock) + timedelta(minutes=i))


def test_new_domain_burst_iot(repos, clock):
    repos.devices.upsert_seen(MAC, clock.now())
    repos.devices.update(MAC, kind="iot", label="Kamera")
    _new_domains(repos, clock, MAC, 11)
    alerts = NewDomainBurst().evaluate(ctx(repos, clock))
    assert len(alerts) == 1
    a = alerts[0]
    assert (a.kind, a.severity, a.mac) == ("new_domain_burst", "warning", MAC)
    assert "Kamera" in a.message and "11" in a.message
    assert a.data["domains"][0] == "d0.example"
    assert a.dedup_key == f"new_domain_burst:{MAC}:2026101905"


def test_new_domain_burst_negative(repos, clock):
    repos.devices.upsert_seen(MAC, clock.now())
    repos.devices.update(MAC, kind="iot")
    repos.devices.upsert_seen(MAC2, clock.now())
    repos.devices.update(MAC2, kind="phone")
    _new_domains(repos, clock, MAC, 10)
    _new_domains(repos, clock, MAC2, 30)
    assert NewDomainBurst().evaluate(ctx(repos, clock)) == []


# --- TrafficSpike ---


def _traffic(repos, clock, baseline_n, last, base=(100.0, 120.0)):
    """Baseline alternates the two `base` values (default: mean 110, sigma 10), then `last` samples."""
    now = clock.now()
    obs = [
        DeviceObservation(
            ts=now - timedelta(minutes=len(last) + baseline_n - i),
            mac=MAC,
            source="sim",
            down_kbps=base[i % 2],
        )
        for i in range(baseline_n)
    ]
    obs += [
        DeviceObservation(ts=now - timedelta(minutes=len(last) - 1 - i), mac=MAC, source="sim", down_kbps=v)
        for i, v in enumerate(last)
    ]
    repos.observations.add_many(obs)
    repos.devices.upsert_seen(MAC, now - timedelta(days=1))
    repos.devices.upsert_seen(MAC, now, hostname="pc")


def test_traffic_spike(repos, clock):
    _traffic(repos, clock, 200, [6000.0, 7000.0, 8000.0])
    alerts = TrafficSpike().evaluate(ctx(repos, clock))
    assert len(alerts) == 1
    a = alerts[0]
    assert (a.kind, a.severity, a.mac) == ("traffic_spike", "warning", MAC)
    assert "pc" in a.message
    assert a.dedup_key == f"traffic_spike:{MAC}:2026101906"


def test_traffic_spike_below_absolute_floor(repos, clock):
    _traffic(repos, clock, 200, [4900.0, 4950.0, 4990.0])  # far above mean + 4 sigma, but < 5000
    assert TrafficSpike().evaluate(ctx(repos, clock)) == []


def test_traffic_spike_idle_baseline_small_rise(repos, clock):
    _traffic(repos, clock, 200, [1.0, 1.0, 1.0], base=(0.0, 0.0))  # sigma 0 must not make 1 kb/s a spike
    assert TrafficSpike().evaluate(ctx(repos, clock)) == []


def test_traffic_spike_idle_baseline_above_floor(repos, clock):
    _traffic(repos, clock, 200, [5001.0, 6000.0, 7000.0], base=(0.0, 0.0))
    alerts = TrafficSpike().evaluate(ctx(repos, clock))
    assert len(alerts) == 1 and alerts[0].data["threshold"] == pytest.approx(5000.0)


def test_traffic_spike_busy_baseline_uses_sigma(repos, clock):
    # mean 11000, sigma 1000 -> threshold 15000
    _traffic(repos, clock, 200, [14000.0, 14500.0, 14900.0], base=(10000.0, 12000.0))
    assert TrafficSpike().evaluate(ctx(repos, clock)) == []


def test_traffic_spike_needs_three_high_samples(repos, clock):
    _traffic(repos, clock, 200, [6000.0, 110.0, 8000.0])
    assert TrafficSpike().evaluate(ctx(repos, clock)) == []


def test_traffic_spike_needs_fifty_baseline_samples(repos, clock):
    _traffic(repos, clock, 49, [6000.0, 7000.0, 8000.0])
    assert TrafficSpike().evaluate(ctx(repos, clock)) == []


def test_traffic_spike_ignores_stale_data(repos, clock):
    _traffic(repos, clock, 200, [6000.0, 7000.0, 8000.0])
    clock.advance(timedelta(minutes=11))
    repos.devices.upsert_seen(MAC, clock.now())
    assert TrafficSpike().evaluate(ctx(repos, clock)) == []


# --- UnknownDevicePresent ---


def test_unknown_device_present(repos, clock):
    repos.devices.upsert_seen(MAC, clock.now() - H, hostname="obcy")
    repos.devices.upsert_seen(MAC, clock.now() - timedelta(minutes=1))
    alerts = UnknownDevicePresent().evaluate(ctx(repos, clock))
    assert len(alerts) == 1
    a = alerts[0]
    assert (a.kind, a.severity, a.mac) == ("unknown_device_present", "warning", MAC)
    assert a.title == "Nieznane urządzenie w sieci"
    assert "obcy" in a.message
    assert a.dedup_key == f"unknown_device_present:{MAC}:2026-10-19"


@pytest.mark.parametrize("case", ["trusted", "assigned", "too_new", "gone"])
def test_unknown_device_present_negative(repos, clock, case):
    now = clock.now()
    first, last = now - H, now - timedelta(minutes=1)
    if case == "too_new":
        first = now - timedelta(minutes=29)
    if case == "gone":
        last = now - timedelta(minutes=6)
    repos.devices.upsert_seen(MAC, first)
    repos.devices.upsert_seen(MAC, last)
    if case == "trusted":
        repos.devices.update(MAC, trusted=True)
    if case == "assigned":
        repos.devices.update(MAC, person_id=repos.persons.create("Ala"))
    assert UnknownDevicePresent().evaluate(ctx(repos, clock)) == []


# --- run_detectors ---


class _Boom:
    name = "boom"

    def evaluate(self, ctx):
        raise RuntimeError("boom")


class _Fixed:
    name = "fixed"

    def evaluate(self, ctx):
        return [
            Alert(kind="fixed", severity="info", title="t", message="m", ts=ctx.now, dedup_key="fixed:1"),
            Alert(kind="fixed", severity="info", title="t", message="m", ts=ctx.now, dedup_key="fixed:2"),
        ]


async def test_run_detectors_failure_does_not_stop_others(repos, clock, caplog):
    service = AlertService(repos, EventBus(), clock)
    n = await run_detectors([_Boom(), _Fixed()], ctx(repos, clock), service)
    assert n == 2
    assert len(repos.alerts.list()) == 2
    assert "boom" in caplog.text


class _FlakyAlerts:
    """AlertService stand-in whose first raise fails."""

    def __init__(self):
        self.calls = []

    async def raise_alert(self, alert):
        self.calls.append(alert.dedup_key)
        if len(self.calls) == 1:
            raise RuntimeError("db locked")
        return alert


async def test_run_detectors_failing_raise_does_not_skip_rest(repos, clock, caplog):
    flaky = _FlakyAlerts()
    assert await run_detectors([_Fixed(), _Fixed()], ctx(repos, clock), flaky) == 3
    assert flaky.calls == ["fixed:1", "fixed:2", "fixed:1", "fixed:2"]
    assert "db locked" in caplog.text


async def test_run_detectors_counts_only_raised(repos, clock):
    service = AlertService(repos, EventBus(), clock)
    await run_detectors([_Fixed()], ctx(repos, clock), service)
    assert await run_detectors([_Fixed()], ctx(repos, clock), service) == 0


async def test_default_detectors_run_on_empty_db(repos, clock):
    detectors = default_detectors()
    assert {d.name for d in detectors} == {
        "unusual_presence",
        "unusual_arrival",
        "night_activity",
        "dns_spike",
        "blocked_burst",
        "new_domain_burst",
        "traffic_spike",
        "unknown_device_present",
    }
    assert await run_detectors(detectors, ctx(repos, clock), AlertService(repos, EventBus(), clock)) == 0
