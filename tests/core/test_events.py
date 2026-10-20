from datetime import UTC, datetime

from homesense.core.events import EventBus
from homesense.core.models import NewDeviceEvent


async def test_publish_typed_and_all_and_errors_swallowed():
    bus = EventBus()
    got = []

    async def h(e):
        got.append(("t", e))

    async def a(e):
        got.append(("a", e))

    async def bad(e):
        raise RuntimeError("x")

    bus.subscribe(NewDeviceEvent, bad)
    bus.subscribe(NewDeviceEvent, h)
    bus.subscribe_all(a)
    ev = NewDeviceEvent(mac="aa:bb:cc:dd:ee:ff", ts=datetime.now(UTC))
    await bus.publish(ev)
    assert got == [("t", ev), ("a", ev)]


async def test_unsubscribe_all():
    bus = EventBus()
    got = []

    async def a(e):
        got.append(e)

    bus.subscribe_all(a)
    bus.unsubscribe_all(a)
    await bus.publish(object())
    assert got == []


async def test_unsubscribe_all_bound_method():
    class Sub:
        def __init__(self):
            self.got = []

        async def on_event(self, e):
            self.got.append(e)

    bus = EventBus()
    s = Sub()
    bus.subscribe(NewDeviceEvent, s.on_event)
    bus.subscribe_all(s.on_event)
    bus.unsubscribe_all(s.on_event)
    await bus.publish(NewDeviceEvent(mac="aa:bb:cc:dd:ee:ff", ts=datetime.now(UTC)))
    assert s.got == []


async def test_subscriber_count():
    bus = EventBus()

    async def a(e):
        pass

    assert bus.subscriber_count() == 0
    bus.subscribe(NewDeviceEvent, a)
    bus.subscribe_all(a)
    assert bus.subscriber_count() == 2
    bus.unsubscribe_all(a)
    assert bus.subscriber_count() == 0
