"""Monitoring on/off switch: pauses all collection while the service keeps running.

The state lives in homesense.db (monitoring_pauses), so a pause survives a restart. While paused the
collector runner skips every collector, the presence engine and the anomaly detectors do not run and
no alert is raised. After a resume the presence engine holds departures for one departure timeout
(see PresenceEngine), so the gap is never read as everyone having left.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from homesense.core.clock import Clock
from homesense.core.events import EventBus
from homesense.core.models import MonitoringChanged, MonitoringPause
from homesense.storage.repositories.monitoring import MonitoringRepo

log = logging.getLogger(__name__)

# Allowed pause lengths for the API/UI, in hours; None = until resumed by hand.
PAUSE_HOURS = (1, 8, 24)


@dataclass(frozen=True)
class MonitoringState:
    paused: bool
    since: datetime | None = None
    until: datetime | None = None  # None while paused = until resumed
    resumed_at: datetime | None = None  # end of the last pause


class MonitoringControl:
    def __init__(self, repo: MonitoringRepo, bus: EventBus, clock: Clock) -> None:
        self._repo = repo
        self._bus = bus
        self._clock = clock
        self._open: MonitoringPause | None = None
        self._resumed_at: datetime | None = None
        self._lock = asyncio.Lock()

    def load(self) -> None:
        """Reads the persisted state (after migrations); an expired timed pause ends at its until."""
        self._open = self._repo.open_pause()
        last = self._repo.last_closed()
        self._resumed_at = last.end_ts if last else None
        if self._expired():
            self._close(self._open.until_ts)

    def _expired(self) -> bool:
        p = self._open
        return p is not None and p.until_ts is not None and self._clock.now() >= p.until_ts

    def _close(self, end: datetime) -> None:
        self._repo.close(end)
        self._open = None
        self._resumed_at = end
        log.info("monitoring resumed (pause ended %s)", end.isoformat())

    def is_paused(self) -> bool:
        """True while a pause is open and not yet expired (expiry is applied by `expire()`)."""
        return self._open is not None and not self._expired()

    def resumed_at(self) -> datetime | None:
        """When the last pause ended (None: never paused or still paused)."""
        if self._expired():
            return self._open.until_ts
        return None if self._open is not None else self._resumed_at

    def state(self) -> MonitoringState:
        if self.is_paused():
            p = self._open
            return MonitoringState(True, since=p.start_ts, until=p.until_ts)
        return MonitoringState(False, resumed_at=self.resumed_at())

    async def pause(self, duration: timedelta | None) -> MonitoringState:
        """Pauses for `duration` from now (None = until resumed); pausing again changes the end."""
        async with self._lock:
            now = self._clock.now()
            if self._expired():
                self._close(self._open.until_ts)
            until = None if duration is None else now + duration
            self._open = self._repo.start(now, until)
            log.info("monitoring paused until %s", until.isoformat() if until else "resumed")
            state = self.state()
        await self._bus.publish(MonitoringChanged(paused=True, until=until, ts=now))
        return state

    async def resume(self) -> MonitoringState:
        async with self._lock:
            now = self._clock.now()
            changed = self._open is not None
            if self._expired():
                now = self._open.until_ts
            if changed:
                self._close(now)
            state = self.state()
        if changed:
            await self._bus.publish(MonitoringChanged(paused=False, until=None, ts=now))
        return state

    async def expire(self) -> bool:
        """Auto-resume of an expired timed pause (periodic job); True when it resumed now."""
        async with self._lock:
            if not self._expired():
                return False
            end = self._open.until_ts
            self._close(end)
        await self._bus.publish(MonitoringChanged(paused=False, until=None, ts=end))
        return True
