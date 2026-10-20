from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from homesense.core.models import DeviceObservation, PresenceEvent
from homesense.patterns.profiles import SLOTS, ProfileBuilder, time_of_day_stats, typical_times

TZ = "Europe/Warsaw"
WAW = ZoneInfo(TZ)
MAC = "aa:bb:cc:dd:ee:01"
MAC2 = "aa:bb:cc:dd:ee:02"


def local(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=WAW).astimezone(UTC)


def add_stay(repos, pid, start: datetime, end: datetime) -> None:
    repos.presence.add_event(PresenceEvent(ts=start, person_id=pid, kind="arrive"))
    repos.presence.start_interval(pid, start)
    repos.presence.add_event(PresenceEvent(ts=end, person_id=pid, kind="leave"))
    repos.presence.close_interval(pid, end)


@pytest.fixture
def builder(repos, clock):
    return ProfileBuilder(repos, clock, TZ)


def test_slots_cover_week():
    assert len(SLOTS) == 168 and SLOTS[0] == (0, 0) and SLOTS[-1] == (6, 23)


def test_person_home_evenings(repos, clock, builder):
    # clock: Mon 2026-10-19 06:00 UTC (08:00 local); home 18:00-24:00 local since Sun Oct 4
    pid = repos.persons.create("Ala")
    for d in range(4, 19):
        add_stay(repos, pid, local(2026, 10, d, 18), local(2026, 10, d + 1, 0))
    prof = builder.build_person_home(pid)
    assert prof[(0, 20)] == pytest.approx((1.0, 2))
    assert prof[(0, 10)] == pytest.approx((0.0, 2))
    assert prof[(6, 23)][0] == pytest.approx(1.0)
    assert prof[(6, 17)][0] == pytest.approx(0.0)
    assert all(n >= 2 for _, n in prof.values())


def test_person_home_partial_hours_are_clipped(repos, clock, builder):
    pid = repos.persons.create("Ala")
    add_stay(repos, pid, local(2026, 10, 5, 18, 30), local(2026, 10, 5, 19, 15))
    add_stay(repos, pid, local(2026, 10, 12, 18, 30), local(2026, 10, 12, 19, 15))
    prof = builder.build_person_home(pid)
    assert prof[(0, 18)] == pytest.approx((0.5, 2))
    assert prof[(0, 19)] == pytest.approx((0.25, 2))


def test_person_home_open_interval_counts_until_now(repos, clock, builder):
    pid = repos.persons.create("Ala")
    start = clock.now() - timedelta(days=14)
    repos.presence.add_event(PresenceEvent(ts=start, person_id=pid, kind="arrive"))
    repos.presence.start_interval(pid, start)
    prof = builder.build_person_home(pid)
    assert len(prof) == 168
    assert all(v == pytest.approx(1.0) for v, _ in prof.values())


def test_person_home_insufficient_data(repos, clock, builder):
    pid = repos.persons.create("Ala")
    assert builder.build_person_home(pid) == {}
    add_stay(repos, pid, clock.now() - timedelta(hours=20), clock.now() - timedelta(hours=2))
    assert builder.build_person_home(pid) == {}


def test_person_home_samples_start_at_first_device_sighting(repos, clock, builder):
    pid = repos.persons.create("Ala")
    repos.devices.upsert_seen(MAC, clock.now() - timedelta(days=14))
    repos.devices.update(MAC, person_id=pid)
    prof = builder.build_person_home(pid)
    assert prof[(0, 20)] == pytest.approx((0.0, 2))


def test_person_home_window_limits_samples(repos, clock):
    pid = repos.persons.create("Ala")
    add_stay(repos, pid, clock.now() - timedelta(days=60), clock.now() - timedelta(days=59))
    prof = ProfileBuilder(repos, clock, TZ, window_days=14).build_person_home(pid)
    assert prof[(0, 20)][1] == 2


def test_person_home_across_dst_change(repos, clock, builder):
    # 2026-10-25 03:00 CEST -> 02:00 CET: local 02:00 on Sunday happens twice
    clock.set(datetime(2026, 10, 27, 12, 0, tzinfo=UTC))
    pid = repos.persons.create("Ala")
    for d in range(10, 27):  # home 18:00 -> 08:00 local, every night since Sat Oct 10
        add_stay(repos, pid, local(2026, 10, d, 18), local(2026, 10, d + 1, 8))
    prof = builder.build_person_home(pid)
    # Sundays Oct 11, 18 and Oct 25 (twice) -> 4 samples
    assert prof[(6, 2)] == pytest.approx((1.0, 4))
    assert prof[(6, 3)] == pytest.approx((1.0, 3))
    assert prof[(6, 12)] == pytest.approx((0.0, 3))
    # slots follow local time on both sides of the change
    assert prof[(6, 20)][0] == pytest.approx(1.0)
    assert prof[(0, 18)][0] == pytest.approx(1.0)
    assert prof[(0, 17)][0] == pytest.approx(0.0)


def test_device_active_from_observations(repos, clock, builder):
    repos.devices.upsert_seen(MAC, clock.now() - timedelta(days=14))
    obs = []
    for d in range(5, 19):
        t = local(2026, 10, d, 20)
        obs += [DeviceObservation(ts=t + timedelta(minutes=m), mac=MAC, source="sim") for m in range(30)]
    obs.append(DeviceObservation(ts=local(2026, 10, 5, 21, 5), mac=MAC, source="sim", online=False))
    repos.observations.add_many(obs)
    prof = builder.build_device_active(MAC)
    assert prof[(0, 20)] == pytest.approx((0.5, 2))
    assert prof[(0, 21)] == pytest.approx((0.0, 2))
    assert prof[(3, 20)][0] == pytest.approx(0.5)


def test_device_active_unknown_device_is_empty(builder):
    assert builder.build_device_active(MAC) == {}


def test_device_dns_mean_queries(repos, clock, builder):
    repos.devices.upsert_seen(MAC, clock.now() - timedelta(days=14))
    repos.dns.add_hourly_stats(local(2026, 10, 5, 20), MAC, 10, 1)
    repos.dns.add_hourly_stats(local(2026, 10, 12, 20), MAC, 20, 0)
    repos.dns.add_hourly_stats(local(2026, 10, 12, 20), MAC2, 500, 0)
    prof = builder.build_device_dns(MAC)
    assert prof[(0, 20)] == pytest.approx((15.0, 2))
    assert prof[(0, 21)] == pytest.approx((0.0, 2))


def test_rebuild_all_persists_profiles(repos, clock, builder):
    pid = repos.persons.create("Ala")
    other = repos.persons.create("Ola")
    repos.profiles.replace("person_home", str(other), {(0, 0): (0.7, 9)}, clock.now())
    repos.devices.upsert_seen(MAC, clock.now() - timedelta(days=14))
    repos.devices.upsert_seen(MAC2, clock.now() - timedelta(days=50))  # not seen in window
    for d in range(5, 19):
        add_stay(repos, pid, local(2026, 10, d, 18), local(2026, 10, d + 1, 0))
        repos.observations.add_many(
            [DeviceObservation(ts=local(2026, 10, d, 20, m), mac=MAC, source="sim") for m in range(60)]
        )
    repos.dns.add_hourly_stats(local(2026, 10, 5, 20), MAC, 10, 0)
    builder.rebuild_all()
    assert repos.profiles.get("person_home", str(pid))[(0, 20)] == pytest.approx((1.0, 2))
    assert repos.profiles.get("person_home", str(other)) == {}
    assert repos.profiles.get("device_active", MAC)[(0, 20)] == pytest.approx((1.0, 2))
    assert repos.profiles.get("device_dns", MAC)[(0, 20)] == pytest.approx((5.0, 2))
    assert repos.profiles.get("device_active", MAC2) == {}


def test_typical_times_median_and_iqr():
    pid = 1
    events = [
        PresenceEvent(ts=local(2026, 10, 5, 17, 0), person_id=pid, kind="arrive"),
        PresenceEvent(ts=local(2026, 10, 12, 17, 30), person_id=pid, kind="arrive"),
        PresenceEvent(ts=local(2026, 10, 26, 18, 0), person_id=pid, kind="arrive"),  # after DST change
        PresenceEvent(ts=local(2026, 10, 6, 7, 45), person_id=pid, kind="leave"),
    ]
    tt = typical_times(events, TZ)
    assert set(tt) == set(range(7))
    assert tt[0]["arrive"] == ("17:30", "0:30")
    assert tt[0]["leave"] is None
    assert tt[1] == {"arrive": None, "leave": None}


def test_typical_times_empty():
    assert typical_times([], TZ) == {wd: {"arrive": None, "leave": None} for wd in range(7)}


def test_time_of_day_stats_numeric():
    pid = 1
    events = [
        PresenceEvent(ts=local(2026, 10, 5, 17, 0), person_id=pid, kind="arrive"),
        PresenceEvent(ts=local(2026, 10, 12, 17, 30), person_id=pid, kind="arrive"),
        PresenceEvent(ts=local(2026, 10, 26, 18, 0), person_id=pid, kind="arrive"),
        PresenceEvent(ts=local(2026, 10, 6, 7, 45), person_id=pid, kind="leave"),
    ]
    st = time_of_day_stats(events, TZ)
    assert set(st) == set(range(7))
    assert st[0]["arrive"] == pytest.approx((17 * 60 + 30, 30.0, 3))
    assert st[0]["leave"] is None
    assert st[1]["leave"] == pytest.approx((7 * 60 + 45, 0.0, 1))
    assert st[2] == {"arrive": None, "leave": None}
