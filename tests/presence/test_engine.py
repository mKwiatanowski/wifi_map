import asyncio
from datetime import timedelta

import pytest

from homesense.alerts.service import AlertService
from homesense.core.events import EventBus
from homesense.core.models import AlertRaised, ObservationsBatch, PresenceEvent
from homesense.presence.engine import PersonState, PresenceEngine

PHONE = "aa:bb:cc:dd:ee:01"
LAPTOP = "aa:bb:cc:dd:ee:02"
TIMEOUT = timedelta(minutes=15)


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def published(bus):
    events: list[PresenceEvent] = []

    async def on_event(e):
        events.append(e)

    bus.subscribe(PresenceEvent, on_event)
    return events


@pytest.fixture
def engine(repos, bus, clock):
    alerts = AlertService(repos, bus, clock)
    e = PresenceEngine(repos, bus, alerts, clock, TIMEOUT)
    e.restore()
    return e


def _person_with_device(repos, clock, name="Ala", mac=PHONE):
    pid = repos.persons.create(name)
    _attach(repos, clock, pid, mac)
    return pid


def _attach(repos, clock, pid, mac):
    repos.devices.upsert_seen(mac, clock.now())
    repos.devices.update(mac, person_id=pid, counts_for_presence=True)


def _seen(repos, clock, mac=PHONE):
    repos.devices.upsert_seen(mac, clock.now())


async def test_phone_seen_produces_arrive(engine, repos, clock, published):
    pid = _person_with_device(repos, clock)
    events = await engine.tick()
    assert events == [PresenceEvent(ts=clock.now(), person_id=pid, kind="arrive", mac=PHONE)]
    assert published == events
    assert repos.presence.events(person_id=pid) == events
    assert repos.presence.open_interval(pid).start_ts == clock.now()
    assert engine.states() == [PersonState(person_id=pid, home=True, since=clock.now(), via_mac=PHONE)]


async def test_repeated_tick_while_home_emits_nothing(engine, repos, clock):
    _person_with_device(repos, clock)
    await engine.tick()
    clock.advance(timedelta(minutes=1))
    _seen(repos, clock)
    assert await engine.tick() == []


async def test_phone_asleep_10_minutes_does_not_leave(engine, repos, clock, published):
    """Review Focus #1: brief WiFi disappearance must not produce a false 'left'."""
    pid = _person_with_device(repos, clock)
    await engine.tick()
    for _ in range(10):
        clock.advance(timedelta(minutes=1))
        assert await engine.tick() == []
    clock.advance(timedelta(minutes=5))  # exactly at the timeout: still home
    assert await engine.tick() == []
    assert [e.kind for e in published] == ["arrive"]
    assert engine.states()[0].home is True
    assert repos.presence.open_interval(pid) is not None


async def test_phone_missing_16_minutes_leaves_at_last_seen(engine, repos, clock, published):
    pid = _person_with_device(repos, clock)
    await engine.tick()
    last_seen = clock.now()
    clock.advance(timedelta(minutes=16))
    events = await engine.tick()
    assert events == [PresenceEvent(ts=last_seen, person_id=pid, kind="leave", mac=PHONE)]
    assert [e.kind for e in published] == ["arrive", "leave"]
    assert repos.presence.open_interval(pid) is None
    [interval] = repos.presence.intervals(pid, last_seen - timedelta(hours=1), clock.now())
    assert interval.end_ts == last_seen
    assert engine.states() == [PersonState(person_id=pid, home=False, since=last_seen, via_mac=None)]


async def test_phone_returns_after_leave_arrives_again(engine, repos, clock):
    pid = _person_with_device(repos, clock)
    await engine.tick()
    clock.advance(timedelta(minutes=30))
    await engine.tick()
    _seen(repos, clock)
    events = await engine.tick()
    assert [(e.kind, e.ts) for e in events] == [("arrive", clock.now())]
    assert len(repos.presence.intervals(pid, clock.now() - timedelta(hours=2), clock.now() + TIMEOUT)) == 2


async def test_second_device_keeps_person_home(engine, repos, clock, published):
    pid = _person_with_device(repos, clock)
    _attach(repos, clock, pid, LAPTOP)
    await engine.tick()
    for _ in range(4):
        clock.advance(timedelta(minutes=10))
        _seen(repos, clock, LAPTOP)  # phone stays silent
        assert await engine.tick() == []
    assert [e.kind for e in published] == ["arrive"]
    assert engine.states()[0].home is True
    assert engine.states()[0].via_mac == LAPTOP


async def test_device_not_counting_for_presence_is_ignored(engine, repos, clock):
    pid = repos.persons.create("Ala")
    _seen(repos, clock)
    repos.devices.update(PHONE, person_id=pid, counts_for_presence=False)
    assert await engine.tick() == []
    assert engine.states() == [PersonState(person_id=pid, home=False, since=None, via_mac=None)]


async def test_person_without_presence_devices_never_home(engine, repos, clock, published):
    pid = repos.persons.create("Ola")
    for _ in range(3):
        assert await engine.tick() == []
        clock.advance(timedelta(minutes=20))
    assert published == []
    assert engine.states() == [PersonState(person_id=pid, home=False, since=None, via_mac=None)]


async def test_restore_keeps_open_interval_without_duplicate_arrive(repos, bus, clock, published):
    pid = _person_with_device(repos, clock)
    first = PresenceEngine(repos, bus, AlertService(repos, bus, clock), clock, TIMEOUT)
    first.restore()
    await first.tick()
    arrived_at = clock.now()

    clock.advance(timedelta(minutes=5))
    _seen(repos, clock)
    restarted = PresenceEngine(repos, bus, AlertService(repos, bus, clock), clock, TIMEOUT)
    restarted.restore()
    assert restarted.states() == [PersonState(person_id=pid, home=True, since=arrived_at, via_mac=None)]
    assert await restarted.tick() == []
    assert [e.kind for e in published] == ["arrive"]
    assert len(repos.presence.events(person_id=pid)) == 1
    assert repos.presence.open_interval(pid).start_ts == arrived_at
    assert restarted.states()[0].via_mac == PHONE


async def test_restore_then_device_gone_leaves(repos, bus, clock):
    pid = _person_with_device(repos, clock)
    repos.presence.start_interval(pid, clock.now())
    last_seen = clock.now()
    clock.advance(timedelta(hours=1))
    engine = PresenceEngine(repos, bus, AlertService(repos, bus, clock), clock, TIMEOUT)
    engine.restore()
    events = await engine.tick()
    assert [(e.kind, e.ts) for e in events] == [("leave", last_seen)]


async def test_arrive_and_leave_alerts_raised(engine, repos, clock):
    pid = _person_with_device(repos, clock, name="Ala")
    await engine.tick()
    clock.advance(timedelta(minutes=16))
    await engine.tick()
    alerts = sorted(repos.alerts.list(), key=lambda a: a.id)
    assert [(a.kind, a.severity, a.title, a.person_id, a.dedup_key) for a in alerts] == [
        ("arrive", "info", "Ala wrócił(a) do domu", pid, None),
        ("leave", "info", "Ala wyszedł/wyszła", pid, None),
    ]


async def test_on_batch_runs_tick(engine, repos, clock, published):
    _person_with_device(repos, clock)
    await engine.on_batch(ObservationsBatch(observations=()))
    assert [e.kind for e in published] == ["arrive"]


async def test_concurrent_ticks_do_not_duplicate_events(engine, repos, bus, clock, published):
    async def slow_subscriber(_):
        await asyncio.sleep(0)  # yield mid-tick so concurrent ticks interleave

    bus.subscribe(PresenceEvent, slow_subscriber)
    pid = _person_with_device(repos, clock)
    await asyncio.gather(engine.tick(), engine.on_batch(ObservationsBatch(observations=())), engine.tick())
    assert [e.kind for e in published] == ["arrive"]
    assert len(repos.presence.events(person_id=pid)) == 1
    assert len(repos.alerts.list()) == 1


async def test_concurrent_ticks_are_serialized(engine, repos, bus, clock):
    seen: list[tuple[str, int]] = []

    async def record(e):
        if isinstance(e, PresenceEvent):
            seen.append(("event", e.person_id))
            await asyncio.sleep(0)  # yield mid-tick so an unguarded second tick would interleave
        elif isinstance(e, AlertRaised):
            seen.append(("alert", e.alert.person_id))

    bus.subscribe_all(record)
    ala = _person_with_device(repos, clock, name="Ala", mac=PHONE)
    await engine.tick()
    ola = _person_with_device(repos, clock, name="Ola", mac=LAPTOP)
    clock.advance(timedelta(minutes=20))
    _seen(repos, clock, LAPTOP)
    await asyncio.gather(engine.tick(), engine.tick())
    assert seen[2:] == [("event", ala), ("alert", ala), ("event", ola), ("alert", ola)]


async def test_presence_device_unassigned_while_home_leaves_now(engine, repos, clock):
    pid = _person_with_device(repos, clock)
    await engine.tick()
    clock.advance(timedelta(minutes=2))
    repos.devices.update(PHONE, counts_for_presence=False)
    events = await engine.tick()
    assert events == [PresenceEvent(ts=clock.now(), person_id=pid, kind="leave", mac=PHONE)]
    assert repos.presence.open_interval(pid) is None
