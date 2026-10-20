"""Minimal periodic job runner for the app's background work."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


class Periodic:
    """Runs an async job every `interval_s` seconds; every exception is logged and the loop continues.

    The first run happens immediately unless `run_immediately` is False (then after one interval).
    Cancellation (asyncio.CancelledError) propagates and ends the loop.
    """

    def __init__(
        self,
        name: str,
        interval_s: float,
        fn: Callable[[], Awaitable[None]],
        run_immediately: bool = True,
    ) -> None:
        self.name = name
        self.interval_s = interval_s
        self._fn = fn
        self._run_immediately = run_immediately

    async def run_forever(self) -> None:
        if not self._run_immediately:
            await asyncio.sleep(self.interval_s)
        while True:
            try:
                await self._fn()
            except Exception:
                log.exception("periodic job %s failed", self.name)
            await asyncio.sleep(self.interval_s)
