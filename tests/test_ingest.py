from datetime import timedelta

import pytest

from homesense.alerts.service import AlertService
from homesense.core.events import EventBus
from homesense.core.models import (
    AlertRaised,
    DeviceObservation,
    DnsQueryRecord,
    NewDeviceEvent,
    ObservationsBatch,
)
from homesense.core.timeutil import floor_hour
from homesense.ingest import UNKNOWN_MAC, Ingest

MAC = "a0:bb:cc:dd:ee:01"
MAC2 = "a0:bb:cc:dd:ee:02"
RANDOM_MAC = "da:bb:cc:dd:ee:03"


class FakeVendor:
    def __init__(self, result="Acme Corp"):
        self.result = result
        self.calls = []

    def __call__(self, mac):
        self.calls.append(mac)
        return self.result


@pytest.fixture
def events():
    return []


@pytest.fixture
def vendor():
    return FakeVendor()


@pytest.fixture
def ingest(repos, clock, events, vendor):
    bus = EventBus()

    async def record(e):
        events.append(e)

    bus.subscribe_all(record)
    return Ingest(repos, bus, AlertService(repos, bus, clock), clock, vendor)


def _dev(ts, mac=MAC, ip="192.168.0.20", hostname=None, online=True):
    return DeviceObservation(ts=ts, mac=mac, source="test", online=online, ip=ip, hostname=hostname)


def _dns(ts, ip="192.168.0.20", domain="example.com", blocked=False):
    return DnsQueryRecord(ts=ts, client_ip=ip, domain=domain, blocked=blocked)


def _of(events, cls):
    return [e for e in events if isinstance(e, cls)]


# --- devices ---


async def test_new_device_creates_device_alert_and_event(ingest, repos, clock, events, vendor):
    now = clock.now()
    await ingest.handle([_dev(now, hostname="pixel-ala")])

    d = repos.devices.get(MAC)
    assert d is not None and d.ip == "192.168.0.20" and d.hostname == "pixel-ala"
    assert d.vendor == "Acme Corp"
    assert vendor.calls == [MAC]

    alerts = repos.alerts.list()
    assert len(alerts) == 1
    a = alerts[0]
    assert a.kind == "new_device" and a.severity == "warning"
    assert a.title == "Nowe urządzenie w sieci"
    assert a.message == "pixel-ala (192.168.0.20)"
    assert a.mac == MAC and a.dedup_key == f"new_device:{MAC}"
    assert a.data == {"random_mac": False}

    assert _of(events, NewDeviceEvent) == [NewDeviceEvent(mac=MAC, ts=now)]
    assert len(_of(events, AlertRaised)) == 1
    assert len(repos.observations.for_device(MAC, now, now + timedelta(seconds=1))) == 1


async def test_new_device_without_ip_message_is_just_the_name(ingest, repos, clock):
    await ingest.handle([_dev(clock.now(), ip=None, hostname="tv-salon")])
    assert [a.message for a in repos.alerts.list()] == ["tv-salon"]


async def test_invalid_mac_skipped_rest_of_batch_processed(ingest, repos, clock, events, caplog):
    now = clock.now()
    good = _dev(now)
    dns = _dns(now)
    bad_online = _dev(now, mac="not-a-mac")
    bad_offline = _dev(now, mac="zz:zz", online=False)
    with caplog.at_level("WARNING", logger="homesense.ingest"):
        await ingest.handle([bad_online, good, bad_offline, dns])

    assert repos.devices.get(MAC) is not None
    assert len(repos.observations.for_device(MAC, now, now + timedelta(seconds=1))) == 1
    assert repos.dns.top_domains(MAC, floor_hour(now)) == [("example.com", 1, 0)]
    assert _of(events, ObservationsBatch) == [ObservationsBatch((good, dns))]
    assert sum("invalid MAC" in r.getMessage() for r in caplog.records) == 2


async def test_new_device_message_falls_back_to_vendor_then_mac(repos, clock):
    bus = EventBus()
    ing = Ingest(repos, bus, AlertService(repos, bus, clock), clock, FakeVendor())
    await ing.handle([_dev(clock.now(), mac=MAC)])
    ing_none = Ingest(repos, bus, AlertService(repos, bus, clock), clock, FakeVendor(None))
    await ing_none.handle([_dev(clock.now(), mac=RANDOM_MAC, ip="192.168.0.30")])

    by_mac = {a.mac: a for a in repos.alerts.list()}
    assert by_mac[MAC].message == "Acme Corp (192.168.0.20)"
    assert by_mac[RANDOM_MAC].message == f"{RANDOM_MAC} (192.168.0.30)"
    assert by_mac[RANDOM_MAC].data == {"random_mac": True}


async def test_repeated_sighting_no_second_alert_and_no_vendor_lookup(ingest, repos, clock, events, vendor):
    await ingest.handle([_dev(clock.now())])
    clock.advance(timedelta(minutes=1))
    await ingest.handle([_dev(clock.now(), ip="192.168.0.21")])
    clock.advance(timedelta(hours=5))  # past the dedup window: still not new
    await ingest.handle([_dev(clock.now())])

    assert len(repos.alerts.list()) == 1
    assert len(_of(events, NewDeviceEvent)) == 1
    assert vendor.calls == [MAC]
    assert repos.devices.get(MAC).last_seen == clock.now()


async def test_same_new_device_twice_in_one_batch_alerts_once(ingest, repos, clock, events):
    now = clock.now()
    await ingest.handle([_dev(now), _dev(now + timedelta(seconds=30))])
    assert len(repos.alerts.list()) == 1
    assert len(_of(events, NewDeviceEvent)) == 1
    assert len(repos.observations.for_device(MAC, now, now + timedelta(minutes=1))) == 2


async def test_offline_observation_stored_but_does_not_create_device(ingest, repos, clock, events):
    now = clock.now()
    await ingest.handle([_dev(now, online=False)])
    assert repos.devices.get(MAC) is None
    assert repos.alerts.list() == []
    obs = repos.observations.for_device(MAC, now, now + timedelta(seconds=1))
    assert len(obs) == 1 and obs[0].online is False


# --- DNS ---


async def test_dns_from_known_device_stored_under_its_mac(ingest, repos, clock):
    now = clock.now()
    await ingest.handle([_dev(now)])
    await ingest.handle(
        [
            _dns(now, domain="example.com"),
            _dns(now + timedelta(minutes=1), domain="example.com", blocked=True),
            _dns(now + timedelta(minutes=2), domain="ads.test", blocked=True),
        ]
    )
    hour = floor_hour(now)
    assert repos.dns.top_domains(MAC, hour) == [("example.com", 2, 1), ("ads.test", 1, 1)]
    assert repos.dns.hourly_stats(MAC, hour, hour + timedelta(hours=1)) == [(hour, MAC, 3, 2)]
    assert {(m, d) for m, d, _ in repos.dns.new_domains(hour)} == {(MAC, "example.com"), (MAC, "ads.test")}


async def test_dns_aggregated_across_hours(ingest, repos, clock):
    now = clock.now()
    await ingest.handle([_dev(now)])
    later = now + timedelta(hours=1, minutes=5)
    await ingest.handle([_dns(now), _dns(later)])
    h0, h1 = floor_hour(now), floor_hour(later)
    assert repos.dns.hourly_stats(MAC, h0, h1 + timedelta(hours=1)) == [(h0, MAC, 1, 0), (h1, MAC, 1, 0)]
    # first-seen is the earliest sighting in the batch
    assert repos.dns.new_domains(h0) == [(MAC, "example.com", now)]


@pytest.mark.parametrize("ip", ["127.0.0.1", "fe80::1", "2001:db8::42"])
async def test_dns_from_unknown_ip_counted_under_unknown(ingest, repos, clock, ip):
    now = clock.now()
    await ingest.handle([_dns(now, ip=ip, domain="example.com")])
    hour = floor_hour(now)
    assert repos.dns.hourly_stats(UNKNOWN_MAC, hour, hour + timedelta(hours=1)) == [(hour, UNKNOWN_MAC, 1, 0)]
    # no owner => full privacy mode => domain rows kept under "unknown"
    assert repos.dns.top_domains(UNKNOWN_MAC, hour) == [("example.com", 1, 0)]


async def test_dns_owner_stats_only_keeps_no_domain_names(ingest, repos, clock):
    now = clock.now()
    await ingest.handle([_dev(now), _dev(now, mac=MAC2, ip="192.168.0.21")])
    pid = repos.persons.create("Ala", privacy_mode="stats_only")
    repos.devices.update(MAC, person_id=pid)
    await ingest.handle([_dns(now, domain="secret.example"), _dns(now, domain="x.test", blocked=True)])

    hour = floor_hour(now)
    assert repos.dns.top_domains(MAC, hour) == []
    assert repos.dns.top_domains(None, hour) == []
    assert repos.dns.new_domains(hour - timedelta(days=1)) == []
    assert repos.dns.hourly_stats(MAC, hour, hour + timedelta(hours=1)) == [(hour, MAC, 2, 1)]


async def test_dns_owner_full_keeps_domain_names(ingest, repos, clock):
    now = clock.now()
    await ingest.handle([_dev(now)])
    pid = repos.persons.create("Ola", privacy_mode="full")
    repos.devices.update(MAC, person_id=pid)
    await ingest.handle([_dns(now)])
    assert repos.dns.top_domains(MAC, floor_hour(now)) == [("example.com", 1, 0)]


# --- batch event ---


async def test_observations_batch_published_once_per_handle(ingest, clock, events):
    now = clock.now()
    obs = [_dev(now), _dns(now), _dns(now, ip="127.0.0.1")]
    await ingest.handle(obs)
    assert _of(events, ObservationsBatch) == [ObservationsBatch(tuple(obs))]
    await ingest.handle([])
    assert _of(events, ObservationsBatch)[-1] == ObservationsBatch(())
    assert len(_of(events, ObservationsBatch)) == 2
