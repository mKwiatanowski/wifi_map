from datetime import UTC, datetime, timedelta

import pytest

from homesense.app import HomeSenseApp
from homesense.core.clock import FakeClock
from homesense.core.config import Settings
from homesense.core.events import EventBus
from homesense.core.models import MonitoringChanged
from homesense.monitoring import MonitoringControl

ANNA_PHONE = "f0:18:98:a1:00:01"
# Monday 20:00 Europe/Warsaw: every simulated persona is home.
EVENING = datetime(2026, 10, 19, 18, 0, tzinfo=UTC)


@pytest.fixture
def events():
    return []


@pytest.fixture
def control(repos, clock, events):
    bus = EventBus()

    async def record(event):
        events.append(event)

    bus.subscribe(MonitoringChanged, record)
    ctl = MonitoringControl(repos.monitoring, bus, clock)
    ctl.load()
    return ctl


def test_running_by_default(control):
    state = control.state()
    assert not state.paused and state.until is None and state.resumed_at is None
    assert not control.is_paused()


async def test_timed_pause_auto_resumes_at_until(control, clock, events):
    start = clock.now()
    state = await control.pause(timedelta(hours=1))
    assert state.paused and state.since == start and state.until == start + timedelta(hours=1)
    assert control.is_paused()
    assert events == [MonitoringChanged(paused=True, until=start + timedelta(hours=1), ts=start)]

    clock.advance(timedelta(minutes=59))
    assert not await control.expire()
    clock.advance(timedelta(minutes=5))
    assert not control.is_paused()  # expired even before expire() ran
    assert control.resumed_at() == start + timedelta(hours=1)
    assert await control.expire()
    assert events[-1] == MonitoringChanged(paused=False, until=None, ts=start + timedelta(hours=1))
    assert control.state().resumed_at == start + timedelta(hours=1)
    assert not await control.expire()


async def test_indefinite_pause_until_resume(control, clock, events):
    await control.pause(None)
    clock.advance(timedelta(days=3))
    assert not await control.expire()
    assert control.state().paused and control.state().until is None
    state = await control.resume()
    assert not state.paused and state.resumed_at == clock.now()
    assert [e.paused for e in events] == [True, False]
    await control.resume()  # resuming while running is a no-op without an event
    assert len(events) == 2


async def test_pause_again_changes_end_and_keeps_start(control, clock, repos):
    start = clock.now()
    await control.pause(timedelta(hours=1))
    clock.advance(timedelta(minutes=10))
    state = await control.pause(timedelta(hours=8))
    assert state.since == start and state.until == clock.now() + timedelta(hours=8)
    assert repos.monitoring.open_pause().until_ts == state.until


async def test_state_survives_restart(control, repos, clock):
    await control.pause(timedelta(hours=8))
    restarted = MonitoringControl(repos.monitoring, EventBus(), clock)
    restarted.load()
    assert restarted.is_paused() and restarted.state().until == clock.now() + timedelta(hours=8)

    clock.advance(timedelta(hours=9))  # expired while the service was stopped
    again = MonitoringControl(repos.monitoring, EventBus(), clock)
    again.load()
    assert not again.is_paused()
    assert repos.monitoring.open_pause() is None
    assert again.resumed_at() == clock.now() - timedelta(hours=1)


# --- whole app (simulated household) ---


def _settings(tmp_path) -> Settings:
    return Settings(_env_file=None, mode="simulated", data_dir=tmp_path, windows_toast=False)


@pytest.fixture
async def sim(tmp_path):
    clock = FakeClock(EVENING)
    app = HomeSenseApp(_settings(tmp_path), clock)
    app.initialize()
    anna = app.repos.persons.create("Anna")
    app.repos.devices.upsert_seen(ANNA_PHONE, clock.now() - timedelta(days=1))
    app.repos.devices.update(ANNA_PHONE, person_id=anna, counts_for_presence=True, trusted=True)
    yield app, clock, anna
    await app.stop()


async def _run(app, clock, minutes):
    for _ in range(minutes):
        await app.step()
        clock.advance(timedelta(minutes=1))


async def test_pause_freezes_collection_presence_and_alerts(sim):
    app, clock, anna = sim
    await _run(app, clock, 5)
    assert {s.person_id: s.home for s in app.presence.states()}[anna]
    events_before = len(app.repos.presence.events())

    paused_at = clock.now()
    await app.monitoring.pause(timedelta(hours=2))
    alerts_before = len(app.repos.alerts.list(limit=1000))
    await _run(app, clock, 90)
    await app._run_detectors()

    assert app.repos.observations.for_device(ANNA_PHONE, paused_at, clock.now()) == []
    assert {s.person_id: s.home for s in app.presence.states()}[anna]  # frozen, not "left"
    assert len(app.repos.presence.events()) == events_before
    assert len(app.repos.alerts.list(limit=1000)) == alerts_before
    status = app.repos.collector_status.get("simulated")
    assert status.consecutive_failures == 0 and status.last_ok_ts < paused_at


async def test_resume_after_long_pause_causes_no_false_leave_or_arrive(sim):
    app, clock, anna = sim
    await _run(app, clock, 5)
    events_before = len(app.repos.presence.events())
    await app.monitoring.pause(timedelta(hours=1))
    # Runs past the auto-resume and for twice the departure timeout after it.
    await _run(app, clock, 60 + 2 * app.settings.departure_timeout_min)
    assert not app.monitoring.is_paused()
    assert {s.person_id: s.home for s in app.presence.states()}[anna]
    assert len(app.repos.presence.events()) == events_before
    assert app.repos.observations.for_device(ANNA_PHONE, clock.now() - timedelta(minutes=10), clock.now())


async def test_departure_during_pause_is_recorded_after_the_hold(sim):
    """A person still unseen one departure timeout after the resume leaves (back-dated to last sighting)."""
    app, clock, anna = sim
    await _run(app, clock, 5)
    await app.monitoring.pause(None)
    clock.advance(timedelta(hours=3))
    app.household.personas = [p for p in app.household.personas if p.name != "Anna"]  # she has left
    await app.monitoring.resume()
    resumed = clock.now()
    timeout = timedelta(minutes=app.settings.departure_timeout_min)
    await _run(app, clock, app.settings.departure_timeout_min - 1)
    assert {s.person_id: s.home for s in app.presence.states()}[anna]  # still held
    await _run(app, clock, 3)
    assert not {s.person_id: s.home for s in app.presence.states()}[anna]
    leave = app.repos.presence.events()[-1]
    assert leave.kind == "leave" and leave.ts < resumed and clock.now() >= resumed + timeout
