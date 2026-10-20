from datetime import UTC, datetime, timedelta
from typing import Protocol

from homesense.core.timeutil import require_aware


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = require_aware(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, dt: datetime) -> None:
        self._now = require_aware(dt)
