from datetime import timedelta

import pytest

from homesense.core.models import Alert, DeviceObservation, PresenceEvent
from homesense.core.timeutil import floor_hour
from homesense.storage.repositories import UserRepo

MAC = "aa:bb:cc:dd:ee:01"
MAC2 = "aa:bb:cc:dd:ee:02"


# --- persons ---


def test_person_crud(repos, clock):
    pid = repos.persons.create("Ala", color="#ff0000")
    p = repos.persons.get(pid)
    assert p.name == "Ala" and p.color == "#ff0000" and p.privacy_mode == "full"
    assert p.created_at == clock.now()
    repos.persons.update(pid, name="Ola", privacy_mode="stats_only")
    assert repos.persons.get(pid).name == "Ola"
    assert repos.persons.get(pid).privacy_mode == "stats_only"
    assert [x.id for x in repos.persons.list()] == [pid]
    with pytest.raises(ValueError):
        repos.persons.update(pid, created_at="x")
    repos.persons.delete(pid)
    assert repos.persons.get(pid) is None


def test_person_delete_unassigns_devices(repos, clock):
    pid = repos.persons.create("Ala")
    repos.devices.upsert_seen(MAC, clock.now())
    repos.devices.update(MAC, person_id=pid)
    assert [d.mac for d in repos.devices.for_person(pid)] == [MAC]
    repos.persons.delete(pid)
    assert repos.devices.get(MAC).person_id is None


# --- devices ---


def test_device_upsert_new_then_existing(repos, clock):
    assert repos.devices.upsert_seen(MAC, clock.now(), ip="192.168.0.10", hostname="phone")
    clock.advance(timedelta(minutes=1))
    assert not repos.devices.upsert_seen(MAC, clock.now(), ip="192.168.0.11")
    d = repos.devices.get(MAC)
    assert d.hostname == "phone" and d.ip == "192.168.0.11" and d.last_seen == clock.now()


def test_device_upsert_keeps_max_last_seen_and_first_seen(repos, clock):
    t0 = clock.now()
    repos.devices.upsert_seen(MAC, t0, vendor="Apple")
    repos.devices.upsert_seen(MAC, t0 + timedelta(minutes=5))
    repos.devices.upsert_seen(MAC, t0 + timedelta(minutes=2))
    d = repos.devices.get(MAC)
    assert d.first_seen == t0 and d.last_seen == t0 + timedelta(minutes=5) and d.vendor == "Apple"


def test_device_mac_normalized(repos, clock):
    repos.devices.upsert_seen("AA-BB-CC-DD-EE-01", clock.now())
    assert repos.devices.get("AA:BB:CC:DD:EE:01").mac == MAC


def test_device_update_and_queries(repos, clock):
    t0 = clock.now()
    repos.devices.upsert_seen(MAC, t0, ip="192.168.0.10")
    repos.devices.upsert_seen(MAC2, t0 + timedelta(minutes=1), ip="192.168.0.10")
    repos.devices.update(MAC, label="Phone", trusted=True, counts_for_presence=True, kind="phone", notes="n")
    d = repos.devices.get(MAC)
    assert d.label == "Phone" and d.trusted is True and d.counts_for_presence is True
    assert d.kind == "phone" and d.notes == "n"
    with pytest.raises(ValueError):
        repos.devices.update(MAC, mac="x")
    assert repos.devices.find_by_ip("192.168.0.10").mac == MAC2
    assert repos.devices.find_by_ip("10.0.0.1") is None
    assert [d.mac for d in repos.devices.seen_since(t0 + timedelta(seconds=30))] == [MAC2]
    assert len(repos.devices.list()) == 2
    assert repos.devices.get("aa:bb:cc:dd:ee:99") is None


# --- observations ---


def _obs(ts, mac=MAC, online=True, down=None):
    return DeviceObservation(
        ts=ts,
        mac=mac,
        source="sim",
        online=online,
        ip="192.168.0.10",
        connection="5g",
        signal=-50,
        up_kbps=1.0 if down else None,
        down_kbps=down,
    )


def test_observations_add_and_query(repos, clock):
    t0 = clock.now()
    n = repos.observations.add_many(
        [_obs(t0), _obs(t0 + timedelta(minutes=1)), _obs(t0, mac="AA:BB:CC:DD:EE:02")]
    )
    assert n == 3
    got = repos.observations.for_device(MAC, t0, t0 + timedelta(hours=1))
    assert [o.ts for o in got] == [t0, t0 + timedelta(minutes=1)]
    assert got[0].connection == "5g" and got[0].signal == -50 and got[0].online is True
    assert got[0].source == "sim" and got[0].ip == "192.168.0.10"
    assert len(repos.observations.for_device(MAC2, t0, t0 + timedelta(hours=1))) == 1
    assert repos.observations.add_many([]) == 0


def test_online_buckets(repos, clock):
    t0 = clock.now()
    repos.observations.add_many(
        [
            _obs(t0),
            _obs(t0 + timedelta(seconds=10)),
            _obs(t0 + timedelta(seconds=50)),
            _obs(t0 + timedelta(seconds=70)),
            _obs(t0 + timedelta(minutes=5), online=False),
        ]
    )
    got = repos.observations.online_buckets(MAC, t0, t0 + timedelta(hours=1))
    assert got == [t0, t0 + timedelta(minutes=1)]
    assert repos.observations.online_buckets(MAC, t0, t0 + timedelta(hours=1), bucket_s=3600) == [t0]


def test_recent_traffic(repos, clock):
    t0 = clock.now()
    repos.observations.add_many(
        [
            _obs(t0, down=10.0),
            _obs(t0 + timedelta(minutes=1)),
            _obs(t0 + timedelta(minutes=2), down=20.0),
            _obs(t0 + timedelta(minutes=3), down=30.0),
        ]
    )
    got = repos.observations.recent_traffic(MAC, 2)
    assert [o.down_kbps for o in got] == [30.0, 20.0]


def test_observations_delete_before(repos, clock):
    t0 = clock.now()
    repos.observations.add_many([_obs(t0), _obs(t0 + timedelta(days=1))])
    assert repos.observations.delete_before(t0 + timedelta(hours=1)) == 1
    assert len(repos.observations.for_device(MAC, t0, t0 + timedelta(days=2))) == 1


# --- presence ---


def test_presence_events(repos, clock):
    pid = repos.persons.create("Ala")
    t0 = clock.now()
    repos.presence.add_event(PresenceEvent(ts=t0 + timedelta(hours=1), person_id=pid, kind="leave", mac=MAC))
    repos.presence.add_event(PresenceEvent(ts=t0, person_id=pid, kind="arrive"))
    evs = repos.presence.events(pid)
    assert [e.kind for e in evs] == ["arrive", "leave"] and evs[1].mac == MAC and evs[0].ts == t0
    assert [e.kind for e in repos.presence.events(since=t0 + timedelta(minutes=1))] == ["leave"]
    assert [e.kind for e in repos.presence.events(until=t0 + timedelta(minutes=1))] == ["arrive"]
    assert repos.presence.events(person_id=pid + 1) == []


def test_presence_first_event_ts(repos, clock):
    pid = repos.persons.create("Ala")
    t0 = clock.now()
    assert repos.presence.first_event_ts(pid) is None
    repos.presence.add_event(PresenceEvent(ts=t0 + timedelta(hours=1), person_id=pid, kind="leave"))
    repos.presence.add_event(PresenceEvent(ts=t0, person_id=pid, kind="arrive"))
    assert repos.presence.first_event_ts(pid) == t0
    assert repos.presence.first_event_ts(pid + 1) is None


def test_presence_intervals_open_close(repos, clock):
    pid = repos.persons.create("Ala")
    t0 = clock.now()
    assert repos.presence.open_interval(pid) is None
    repos.presence.start_interval(pid, t0)
    iv = repos.presence.open_interval(pid)
    assert iv.person_id == pid and iv.start_ts == t0 and iv.end_ts is None
    repos.presence.close_interval(pid, t0 + timedelta(hours=2))
    assert repos.presence.open_interval(pid) is None
    repos.presence.start_interval(pid, t0 + timedelta(hours=5))
    ivs = repos.presence.intervals(pid, t0 + timedelta(hours=1), t0 + timedelta(hours=6))
    assert [(i.start_ts, i.end_ts) for i in ivs] == [
        (t0, t0 + timedelta(hours=2)),
        (t0 + timedelta(hours=5), None),
    ]
    assert repos.presence.intervals(pid, t0 + timedelta(hours=3), t0 + timedelta(hours=4)) == []


def test_presence_delete_before_keeps_open_interval(repos, clock):
    pid = repos.persons.create("Ala")
    t0 = clock.now()
    repos.presence.add_event(PresenceEvent(ts=t0, person_id=pid, kind="arrive"))
    repos.presence.start_interval(pid, t0)
    repos.presence.close_interval(pid, t0 + timedelta(hours=1))
    repos.presence.start_interval(pid, t0 + timedelta(hours=2))
    assert repos.presence.delete_before(t0 + timedelta(days=1)) == 2
    assert repos.presence.open_interval(pid) is not None


# --- dns ---


def test_dns_upsert_adds(repos, clock):
    h = floor_hour(clock.now())
    repos.dns.add_hourly(h, MAC, "a.com", 3, 1)
    repos.dns.add_hourly(h, MAC, "a.com", 2, 0)
    assert repos.dns.top_domains(MAC, h) == [("a.com", 5, 1)]


def test_dns_top_and_stats(repos, clock):
    h = floor_hour(clock.now())
    repos.dns.add_hourly(h, MAC, "a.com", 3, 0)
    repos.dns.add_hourly(h, MAC2, "a.com", 1, 0)
    repos.dns.add_hourly(h, MAC2, "ads.com", 4, 4)
    repos.dns.add_hourly(h - timedelta(hours=2), MAC, "old.com", 9, 9)
    assert repos.dns.top_domains(None, h) == [("a.com", 4, 0), ("ads.com", 4, 4)]
    assert repos.dns.top_domains(None, h, limit=1) == [("a.com", 4, 0)]
    assert repos.dns.top_blocked(h) == [("ads.com", 4)]
    repos.dns.add_hourly_stats(h, MAC, 3, 0)
    repos.dns.add_hourly_stats(h, MAC, 2, 1)
    repos.dns.add_hourly_stats(h, MAC2, 5, 4)
    assert repos.dns.hourly_stats(MAC, h, h + timedelta(hours=1)) == [(h, MAC, 5, 1)]
    assert repos.dns.hourly_stats(None, h, h + timedelta(hours=1)) == [(h, MAC, 5, 1), (h, MAC2, 5, 4)]


def test_dns_top_excludes_macs(repos, clock):
    h = floor_hour(clock.now())
    repos.dns.add_hourly(h, MAC, "a.com", 3, 0)
    repos.dns.add_hourly(h, MAC2, "private.com", 5, 2)
    repos.dns.add_hourly(h, MAC2, "a.com", 1, 1)
    assert repos.dns.top_domains(None, h, exclude_macs=["AA-BB-CC-DD-EE-02"]) == [("a.com", 3, 0)]
    assert repos.dns.top_domains(None, h, exclude_macs=[]) == [("private.com", 5, 2), ("a.com", 4, 1)]
    assert repos.dns.top_blocked(h, exclude_macs=[MAC2]) == []
    assert repos.dns.top_blocked(h, exclude_macs=[MAC]) == [("private.com", 2), ("a.com", 1)]


def test_mark_domain_seen_first_only(repos, clock):
    t0 = clock.now()
    t1 = t0 + timedelta(hours=1)
    assert repos.dns.mark_domain_seen(MAC, "a.com", t0)
    assert not repos.dns.mark_domain_seen(MAC, "a.com", t1)
    assert repos.dns.mark_domain_seen(MAC2, "a.com", t1)
    assert repos.dns.new_domains(t0) == [(MAC, "a.com", t0), (MAC2, "a.com", t1)]
    assert repos.dns.new_domains(t0, mac=MAC2) == [(MAC2, "a.com", t1)]
    assert repos.dns.new_domains(t1 + timedelta(seconds=1)) == []


def test_dns_delete_before(repos, clock):
    h = floor_hour(clock.now())
    repos.dns.add_hourly(h - timedelta(days=2), MAC, "a.com", 1, 0)
    repos.dns.add_hourly_stats(h - timedelta(days=2), MAC, 1, 0)
    repos.dns.add_hourly(h, MAC, "a.com", 1, 0)
    assert repos.dns.delete_before(h - timedelta(days=1)) == 2
    assert repos.dns.top_domains(MAC, h - timedelta(days=3)) == [("a.com", 1, 0)]


# --- alerts ---


def _alert(ts, key="k", sev="warning"):
    return Alert(
        kind="new_device",
        severity=sev,
        title="T",
        message="M",
        ts=ts,
        mac=MAC,
        data={"x": [1]},
        dedup_key=key,
    )


def test_alert_dedup_exists_recent(repos, clock):
    t0 = clock.now()
    aid = repos.alerts.add(_alert(t0))
    assert isinstance(aid, int)
    assert repos.alerts.exists_recent("k", t0 - timedelta(hours=1))
    assert not repos.alerts.exists_recent("k", t0 + timedelta(seconds=1))
    assert not repos.alerts.exists_recent("other", t0 - timedelta(hours=1))


def test_alert_list_and_ack(repos, clock):
    t0 = clock.now()
    a1 = repos.alerts.add(_alert(t0))
    a2 = repos.alerts.add(_alert(t0 + timedelta(minutes=1), key="k2"))
    lst = repos.alerts.list()
    assert [a.id for a in lst] == [a2, a1]
    first = lst[0]
    assert first.data == {"x": [1]} and first.acknowledged is False and first.ts == t0 + timedelta(minutes=1)
    assert first.mac == MAC and first.severity == "warning" and first.dedup_key == "k2"
    assert repos.alerts.count_unacknowledged() == 2
    assert repos.alerts.acknowledge(a1) is True
    assert repos.alerts.acknowledge(a1) is True  # already acknowledged still exists
    assert repos.alerts.acknowledge(9999) is False
    assert [a.id for a in repos.alerts.list(unacknowledged_only=True)] == [a2]
    assert repos.alerts.count_unacknowledged() == 1
    repos.alerts.acknowledge_all()
    assert repos.alerts.count_unacknowledged() == 0
    assert len(repos.alerts.list(limit=1)) == 1


# --- profiles / settings / users / collector status ---


def test_profiles_replace_and_get(repos, clock):
    repos.profiles.replace("person_home", "1", {(0, 7): (0.5, 4), (6, 23): (1.0, 2)}, clock.now())
    assert repos.profiles.get("person_home", "1") == {(0, 7): (0.5, 4), (6, 23): (1.0, 2)}
    repos.profiles.replace("person_home", "1", {(1, 1): (0.25, 1)}, clock.now())
    assert repos.profiles.get("person_home", "1") == {(1, 1): (0.25, 1)}
    assert repos.profiles.get("person_home", "2") == {}


def test_settings_json_roundtrip(repos):
    repos.settings.set("k", {"a": [1, 2]})
    assert repos.settings.get("k") == {"a": [1, 2]}
    repos.settings.set("k", 5)
    assert repos.settings.get("k") == 5
    assert repos.settings.get("missing", default="d") == "d"


@pytest.fixture
def users(auth_db, clock):
    return UserRepo(auth_db, clock)


def test_users(users):
    assert users.count() == 0
    uid = users.create("admin", "h1")
    assert users.get_by_username("admin") == (uid, "admin", "h1")
    users.set_password("admin", "h2")
    assert users.get_by_username("admin")[2] == "h2"
    assert users.get_by_username("nobody") is None
    assert users.count() == 1


def test_delete_user(users):
    users.create("admin", "h1")
    users.delete("admin")
    assert users.get_by_username("admin") is None


def test_create_first_user_only_when_empty(users):
    assert users.create_first("admin", "h1") is True
    assert users.create_first("second", "h2") is False
    assert users.get_by_username("second") is None
    assert users.count() == 1


def test_collector_status(repos, clock):
    t0 = clock.now()
    assert repos.collector_status.get("router") is None
    repos.collector_status.record_error("router", t0, "timeout")
    repos.collector_status.record_error("router", t0 + timedelta(seconds=30), "auth")
    s = repos.collector_status.get("router")
    assert s.consecutive_failures == 2 and s.last_error == "auth"
    assert s.last_error_ts == t0 + timedelta(seconds=30) and s.last_ok_ts is None
    repos.collector_status.record_ok("router", t0 + timedelta(minutes=1))
    s = repos.collector_status.get("router")
    assert (
        s.consecutive_failures == 0 and s.last_ok_ts == t0 + timedelta(minutes=1) and s.last_error == "auth"
    )
    repos.collector_status.record_ok("dns", t0)
    assert [c.name for c in repos.collector_status.all()] == ["dns", "router"]


def test_online_buckets_rejects_non_positive_bucket(repos, clock):
    t0 = clock.now()
    for b in (0, -60):
        with pytest.raises(ValueError):
            repos.observations.online_buckets(MAC, t0, t0 + timedelta(hours=1), bucket_s=b)


def test_dns_mac_normalized_and_unknown_kept(repos, clock):
    h = floor_hour(clock.now())
    repos.dns.add_hourly(h, "AA-BB-CC-DD-EE-01", "a.com", 1, 0)
    repos.dns.add_hourly(h, "unknown", "a.com", 2, 0)
    repos.dns.add_hourly_stats(h, "AA:BB:CC:DD:EE:01", 1, 0)
    repos.dns.add_hourly_stats(h, "unknown", 2, 0)
    assert repos.dns.mark_domain_seen("AA-BB-CC-DD-EE-01", "a.com", h)
    assert not repos.dns.mark_domain_seen(MAC, "a.com", h)
    assert repos.dns.top_domains(MAC, h) == [("a.com", 1, 0)]
    assert repos.dns.top_domains("unknown", h) == [("a.com", 2, 0)]
    assert repos.dns.hourly_stats(None, h, h + timedelta(hours=1)) == [(h, MAC, 1, 0), (h, "unknown", 2, 0)]
    assert repos.dns.new_domains(h, mac="AA:BB:CC:DD:EE:01") == [(MAC, "a.com", h)]
    with pytest.raises(ValueError):
        repos.dns.add_hourly(h, "not-a-mac", "a.com", 1, 0)


def test_observations_signal_samples_grouped_by_mac(repos, clock):
    t0 = clock.now()
    arp_only = DeviceObservation(ts=t0, mac=MAC, source="arp")  # no connection, no signal
    wired = DeviceObservation(ts=t0, mac=MAC2, source="router", connection="wired")
    repos.observations.add_many(
        [
            _obs(t0 + timedelta(minutes=2)),
            _obs(t0),
            arp_only,
            wired,
            _obs(t0 - timedelta(hours=2)),  # before the window
            _obs(t0 + timedelta(hours=1)),  # at `until` (half-open)
        ]
    )
    got = repos.observations.signal_samples(t0, t0 + timedelta(hours=1))
    assert set(got) == {MAC, MAC2}
    assert [o.ts for o in got[MAC]] == [t0, t0 + timedelta(minutes=2)]
    assert all(o.signal == -50 for o in got[MAC])
    assert got[MAC2][0].connection == "wired" and got[MAC2][0].signal is None
