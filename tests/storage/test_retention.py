from datetime import timedelta

from homesense.core.models import Alert, DeviceObservation, PresenceEvent
from homesense.core.timeutil import floor_hour
from homesense.storage.retention import purge_old, vacuum

MAC = "aa:bb:cc:dd:ee:01"


def test_retention_purges_old(repos, clock):
    now = clock.now()
    old = now - timedelta(days=40)
    new = now - timedelta(days=1)
    pid = repos.persons.create("Ala")
    repos.observations.add_many([DeviceObservation(ts=t, mac=MAC, source="sim") for t in (old, new)])
    for t in (old, new):
        repos.alerts.add(Alert(kind="k", severity="info", title="t", message="m", ts=t))
        repos.dns.add_hourly(floor_hour(t), MAC, "a.com", 1, 0)
        repos.dns.add_hourly_stats(floor_hour(t), MAC, 1, 0)
        repos.presence.add_event(PresenceEvent(ts=t, person_id=pid, kind="arrive"))
    repos.presence.start_interval(pid, old)
    repos.presence.close_interval(pid, old + timedelta(hours=1))
    repos.presence.start_interval(pid, new)

    result = purge_old(repos, now, 30)

    assert result == {"observations": 1, "presence": 2, "dns": 2, "alerts": 1, "monitoring_pauses": 0}
    assert len(repos.observations.for_device(MAC, old - timedelta(days=1), now)) == 1
    assert len(repos.alerts.list()) == 1
    assert repos.dns.top_domains(MAC, old - timedelta(days=1)) == [("a.com", 1, 0)]
    assert len(repos.dns.hourly_stats(MAC, old - timedelta(days=1), now)) == 1
    assert len(repos.presence.events(pid)) == 1
    assert repos.presence.open_interval(pid).start_ts == new


def test_retention_purges_stale_first_seen_but_keeps_active(repos, clock):
    now = clock.now()
    old = now - timedelta(days=40)
    repos.dns.mark_domain_seen(MAC, "stale.com", old)
    repos.dns.add_hourly(floor_hour(old), MAC, "stale.com", 1, 0)
    repos.dns.mark_domain_seen(MAC, "daily.com", old)
    repos.dns.add_hourly(floor_hour(old), MAC, "daily.com", 1, 0)
    repos.dns.add_hourly(floor_hour(now), MAC, "daily.com", 1, 0)

    result = purge_old(repos, now, 30)

    assert result["dns"] == 3  # 2 old dns_hourly rows + 1 stale domain_first_seen row
    assert repos.dns.new_domains(old - timedelta(days=1)) == [(MAC, "daily.com", old)]
    assert not repos.dns.mark_domain_seen(MAC, "daily.com", now)
    assert repos.dns.mark_domain_seen(MAC, "stale.com", now)


def test_vacuum_runs(repos, db, clock):
    repos.settings.set("k", "v" * 1000)
    vacuum(db)
    assert repos.settings.get("k") == "v" * 1000


def test_retention_purges_only_old_closed_monitoring_pauses(repos, clock):
    now = clock.now()
    repos.monitoring.start(now - timedelta(days=40), now - timedelta(days=39))
    repos.monitoring.close(now - timedelta(days=39))
    repos.monitoring.start(now - timedelta(days=35), None)  # still open: kept
    assert purge_old(repos, now, 30)["monitoring_pauses"] == 1
    assert repos.monitoring.open_pause() is not None
