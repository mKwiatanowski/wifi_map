from datetime import timedelta

import pytest

from homesense.core.timeutil import floor_hour
from homesense.network import analysis

MAC = "aa:bb:cc:dd:ee:01"
PRIVATE = "aa:bb:cc:dd:ee:02"


@pytest.fixture
def data(repos, clock):
    now = clock.now()
    h = floor_hour(now)
    repos.devices.upsert_seen(MAC, now, ip="192.168.0.10", hostname="laptop")
    repos.devices.upsert_seen(PRIVATE, now - timedelta(hours=1), ip="192.168.0.11")
    secret = repos.persons.create("Ola", privacy_mode="stats_only")
    repos.devices.update(PRIVATE, person_id=secret)
    repos.dns.add_hourly(h, MAC, "a.com", 3, 0)
    repos.dns.add_hourly(h, MAC, "ads.com", 2, 2)
    # Rows left over from before the person switched to stats_only.
    repos.dns.add_hourly(h, PRIVATE, "secret.com", 9, 1)
    repos.dns.mark_domain_seen(MAC, "a.com", now - timedelta(minutes=5))
    repos.dns.mark_domain_seen(PRIVATE, "secret.com", now)
    repos.dns.add_hourly_stats(h, MAC, 5, 2)
    repos.dns.add_hourly_stats(h, PRIVATE, 9, 1)
    return h


def test_top_domains_hide_stats_only_devices(repos, clock, data):
    assert analysis.top_domains(repos, None, data) == [
        {"domain": "a.com", "queries": 3, "blocked": 0},
        {"domain": "ads.com", "queries": 2, "blocked": 2},
    ]
    assert analysis.top_domains(repos, PRIVATE, data) == []
    assert analysis.top_domains(repos, MAC, data, limit=1) == [
        {"domain": "a.com", "queries": 3, "blocked": 0}
    ]


def test_blocked_and_new_domains_hide_stats_only_devices(repos, clock, data):
    assert analysis.blocked_domains(repos, data) == [{"domain": "ads.com", "blocked": 2}]
    assert analysis.new_domains(repos, data - timedelta(hours=1)) == [
        {
            "mac": MAC,
            "device": "laptop",
            "domain": "a.com",
            "first_seen": (clock.now() - timedelta(minutes=5)).isoformat(),
        }
    ]


def test_hourly_zero_fills_and_summary_counts(repos, clock, data):
    rows = analysis.hourly(repos, None, data - timedelta(hours=2), data + timedelta(hours=1))
    assert [r["queries"] for r in rows] == [0, 0, 14]
    assert rows[-1] == {"ts": data.isoformat(), "queries": 14, "blocked": 3}
    assert [r["queries"] for r in analysis.hourly(repos, MAC, data, data + timedelta(hours=1))] == [5]
    assert analysis.summary(repos, data - timedelta(hours=1), data + timedelta(hours=1)) == {
        "queries": 14,
        "blocked": 3,
        "clients": 2,
        "new_domains": 1,
    }


def test_filter_devices(repos, clock, data):
    now = clock.now()
    repos.devices.upsert_seen("aa:bb:cc:dd:ee:03", now - timedelta(days=3))
    repos.devices.update("aa:bb:cc:dd:ee:03", trusted=True)
    devices = repos.devices.list()

    def macs(kind):
        return [d.mac[-2:] for d in analysis.filter_devices(devices, kind, now)]

    assert macs("all") == ["01", "02", "03"]
    assert macs("online") == ["01"]
    assert macs("new") == ["01", "02"]
    assert macs("unassigned") == ["01", "03"]
    assert macs("unknown") == ["01"]
    with pytest.raises(ValueError):
        analysis.filter_devices(devices, "bogus", now)
