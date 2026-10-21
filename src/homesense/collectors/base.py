from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol

from homesense.core.clock import Clock
from homesense.core.models import Observation
from homesense.storage.repositories.collector_status import CollectorStatusRepo

log = logging.getLogger(__name__)

FAILING_THRESHOLD = 3
# Caps the backoff exponent so a long outage cannot overflow float math.
MAX_BACKOFF_EXPONENT = 32


class Collector(Protocol):
    name: str
    interval_s: float

    async def collect(self) -> list[Observation]: ...


Sink = Callable[[list[Observation]], Awaitable[None]]


class CollectorRunner:
    """Runs collectors periodically; every failure is caught, logged and recorded, never raised."""

    def __init__(
        self,
        collectors: list[Collector],
        sink: Sink,
        status: CollectorStatusRepo,
        clock: Clock,
        max_backoff_s: float = 600,
        is_paused: Callable[[], bool] | None = None,
    ) -> None:
        self._collectors = list(collectors)
        self._sink = sink
        self._status = status
        self._clock = clock
        self._max_backoff_s = max_backoff_s
        self._failures: dict[str, int] = {c.name: 0 for c in self._collectors}
        self._paused: dict[str, datetime] = {}
        # Global monitoring switch (monitoring.MonitoringControl); per-collector pauses are _paused.
        self._monitoring_paused = is_paused or (lambda: False)

    @property
    def monitoring_paused(self) -> bool:
        return self._monitoring_paused()

    async def run_once(self, collector: Collector) -> bool:
        """Skipped (True, no query, no status change) while monitoring or this collector is paused."""
        if self._monitoring_paused() or self.paused_until(collector.name) is not None:
            return True
        try:
            observations = await collector.collect()
            await self._sink(observations)
        except Exception as exc:
            self._failures[collector.name] = self._failures.get(collector.name, 0) + 1
            message = f"{type(exc).__name__}: {exc}"
            log.warning("collector %s failed: %s", collector.name, message)
            try:
                self._status.record_error(collector.name, self._clock.now(), message)
            except Exception:
                log.exception("could not record error status for collector %s", collector.name)
            return False
        self._failures[collector.name] = 0
        try:
            self._status.record_ok(collector.name, self._clock.now())
        except Exception:
            log.exception("could not record ok status for collector %s", collector.name)
        return True

    def next_delay(self, collector: Collector) -> float:
        failures = self._failures.get(collector.name, 0)
        if failures == 0:
            return collector.interval_s
        return min(collector.interval_s * 2 ** min(failures, MAX_BACKOFF_EXPONENT), self._max_backoff_s)

    async def run_forever(self) -> None:
        tasks = [asyncio.create_task(self._loop(c), name=f"collector:{c.name}") for c in self._collectors]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _loop(self, collector: Collector) -> None:
        while True:
            try:
                await self.run_once(collector)
                delay = self.next_delay(collector)
            except Exception:
                log.exception("unexpected error in loop of collector %s", collector.name)
                delay = collector.interval_s
            await asyncio.sleep(delay)

    def paused_until(self, name: str) -> datetime | None:
        until = self._paused.get(name)
        if until is None:
            return None
        if self._clock.now() >= until:
            del self._paused[name]
            return None
        return until

    def pause(self, name: str, until: datetime) -> None:
        self._paused[name] = until

    def health(self) -> Literal["ok", "degraded", "down"]:
        failing = sum(1 for c in self._collectors if self._failures.get(c.name, 0) >= FAILING_THRESHOLD)
        if self._collectors and failing == len(self._collectors):
            return "down"
        if failing:
            return "degraded"
        return "ok"
