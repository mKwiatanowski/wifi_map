from datetime import timedelta

import pytest

from homesense.alerts.service import AlertService
from homesense.core.events import EventBus
from homesense.core.models import Alert, AlertRaised


def _alert(ts, key="new_device:aa:bb:cc:dd:ee:01"):
    return Alert(kind="new_device", severity="warning", title="t", message="m", ts=ts, dedup_key=key)


@pytest.fixture
def raised():
    return []


@pytest.fixture
def service(repos, clock, raised):
    bus = EventBus()

    async def on_raised(e):
        raised.append(e)

    bus.subscribe(AlertRaised, on_raised)
    return AlertService(repos, bus, clock)


async def test_raise_alert_stores_sets_id_and_publishes(service, repos, clock, raised):
    a = await service.raise_alert(_alert(clock.now()))
    assert a is not None and a.id is not None
    assert [x.id for x in repos.alerts.list()] == [a.id]
    assert raised == [AlertRaised(a)]


async def test_dedup_within_window_returns_none(service, repos, clock, raised):
    assert await service.raise_alert(_alert(clock.now())) is not None
    clock.advance(timedelta(hours=2, minutes=59))
    assert await service.raise_alert(_alert(clock.now())) is None
    assert len(repos.alerts.list()) == 1
    assert len(raised) == 1


async def test_dedup_window_expired_raises_again(service, repos, clock):
    await service.raise_alert(_alert(clock.now()))
    clock.advance(timedelta(hours=3, minutes=1))
    assert await service.raise_alert(_alert(clock.now())) is not None
    assert len(repos.alerts.list()) == 2


async def test_no_dedup_key_never_deduplicated(service, repos, clock):
    await service.raise_alert(_alert(clock.now(), key=None))
    await service.raise_alert(_alert(clock.now(), key=None))
    assert len(repos.alerts.list()) == 2


async def test_custom_dedup_window(repos, clock):
    service = AlertService(repos, EventBus(), clock, dedup_window=timedelta(minutes=10))
    await service.raise_alert(_alert(clock.now()))
    clock.advance(timedelta(minutes=11))
    assert await service.raise_alert(_alert(clock.now())) is not None


async def test_alerts_suppressed_while_monitoring_paused(repos, clock, raised):
    paused = True
    bus = EventBus()

    async def on_raised(e):
        raised.append(e)

    bus.subscribe(AlertRaised, on_raised)
    service = AlertService(repos, bus, clock, suppressed=lambda: paused)
    assert await service.raise_alert(_alert(clock.now())) is None
    assert repos.alerts.list() == [] and raised == []
    paused = False
    assert await service.raise_alert(_alert(clock.now())) is not None
