import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._typed: list[tuple[type, Handler]] = []
        self._all: list[Handler] = []

    def subscribe(self, event_type: type, handler: Handler) -> None:
        self._typed.append((event_type, handler))

    def subscribe_all(self, handler: Handler) -> None:
        self._all.append(handler)

    def unsubscribe_all(self, handler: Handler) -> None:
        self._all = [h for h in self._all if h != handler]
        self._typed = [(t, h) for t, h in self._typed if h != handler]

    def subscriber_count(self) -> int:
        """Number of registered (typed + catch-all) subscriptions."""
        return len(self._typed) + len(self._all)

    async def publish(self, event: object) -> None:
        handlers = [h for t, h in self._typed if isinstance(event, t)] + list(self._all)
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                log.exception("event handler %r failed for %s", handler, type(event).__name__)
