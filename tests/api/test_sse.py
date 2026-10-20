import asyncio
import json
import time
from datetime import UTC, datetime

import anyio

from homesense.api.sse import encode_event, event_stream
from homesense.core.events import EventBus
from homesense.core.models import (
    DeviceObservation,
    DnsQueryRecord,
    NewDeviceEvent,
    ObservationsBatch,
    PresenceEvent,
)

TS = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
EVENT = PresenceEvent(ts=TS, person_id=1, kind="arrive", mac="f0:18:98:a1:00:01")


def test_encode_event_serializes_dataclass():
    assert json.loads(encode_event(EVENT)) == {
        "type": "PresenceEvent",
        "data": {
            "ts": "2026-10-19T06:00:00+00:00",
            "person_id": 1,
            "kind": "arrive",
            "mac": "f0:18:98:a1:00:01",
        },
    }


async def test_stream_heartbeats_filters_and_unsubscribes():
    bus = EventBus()
    disconnected = False

    async def is_disconnected() -> bool:
        return disconnected

    stream = event_stream(bus, is_disconnected, heartbeat_s=0.01)

    async def run() -> None:
        nonlocal disconnected
        assert (await anext(stream)).startswith(":")  # connected comment, subscribed from here
        # Raw observations (DNS domains) are never streamed.
        dns = DnsQueryRecord(ts=TS, client_ip="192.168.0.5", domain="secret.com")
        await bus.publish(
            ObservationsBatch((dns, DeviceObservation(ts=TS, mac="aa:bb:cc:dd:ee:01", source="arp")))
        )
        await bus.publish(NewDeviceEvent(mac="aa:bb:cc:dd:ee:01", ts=TS))
        chunk = await anext(stream)
        assert chunk.startswith("data: ") and chunk.endswith("\n\n")
        assert json.loads(chunk[len("data: ") :])["type"] == "NewDeviceEvent"
        assert await anext(stream) == ": heartbeat\n\n"
        disconnected = True
        async for _ in stream:
            pass

    await asyncio.wait_for(run(), timeout=5)
    assert bus.subscriber_count() == 0


def test_sse_endpoint_receives_published_presence_event(client, core, auth):
    client.app.state.sse_heartbeat_s = 0.05

    baseline = core.bus.subscriber_count()

    async def publish_once_subscribed() -> None:
        with anyio.fail_after(5):
            while core.bus.subscriber_count() <= baseline:
                await anyio.sleep(0.01)
        await core.bus.publish(EVENT)

    client.portal.start_task_soon(publish_once_subscribed)
    deadline = time.monotonic() + 5
    received = None
    with client.stream("GET", "/api/events/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data: "):
                received = json.loads(line[len("data: ") :])
                break
            assert time.monotonic() < deadline, "no event within 5 s"
    assert received["type"] == "PresenceEvent"
    assert received["data"]["person_id"] == 1


def test_sse_requires_login(client):
    assert client.get("/api/events/stream").status_code == 401
