"""Server-Sent Events: live presence / device / alert events from the EventBus."""

# No `from __future__ import annotations`: FastAPI inspects these signatures at runtime.

import asyncio
import dataclasses
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from homesense.api.deps import get_core, require_user
from homesense.app import HomeSenseApp
from homesense.core.events import EventBus
from homesense.core.models import AlertRaised, MonitoringChanged, NewDeviceEvent, PresenceEvent

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_user)])

HEARTBEAT_S = 15.0
QUEUE_SIZE = 100
# Only these events reach the browser. ObservationsBatch carries raw DNS queries (domain names even
# of stats_only persons, before ingest drops them), so it is never streamed.
STREAMED_EVENTS = (PresenceEvent, NewDeviceEvent, AlertRaised, MonitoringChanged)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def encode_event(event: object) -> str:
    """JSON {"type": class name, "data": {...}} with datetimes as ISO-8601 strings."""
    data = dataclasses.asdict(event) if dataclasses.is_dataclass(event) else {}
    return json.dumps({"type": type(event).__name__, "data": _jsonable(data)}, ensure_ascii=False)


async def event_stream(
    bus: EventBus, is_disconnected: Callable[[], Awaitable[bool]], heartbeat_s: float = HEARTBEAT_S
) -> AsyncIterator[str]:
    """SSE chunks: a ': connected' comment once subscribed, then events and ': heartbeat' comments.

    Ends when `is_disconnected()` is true (checked at least every `heartbeat_s`); always unsubscribes.
    """
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=QUEUE_SIZE)

    async def on_event(event: object) -> None:
        if not isinstance(event, STREAMED_EVENTS):
            return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("SSE client too slow, dropping %s", type(event).__name__)

    bus.subscribe_all(on_event)
    try:
        yield ": connected\n\n"
        while not await is_disconnected():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue
            yield f"data: {encode_event(event)}\n\n"
    finally:
        bus.unsubscribe_all(on_event)


@router.get("/api/events/stream")
async def stream(request: Request, core: Annotated[HomeSenseApp, Depends(get_core)]) -> StreamingResponse:
    heartbeat_s = getattr(request.app.state, "sse_heartbeat_s", HEARTBEAT_S)
    return StreamingResponse(
        event_stream(core.bus, request.is_disconnected, heartbeat_s),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
