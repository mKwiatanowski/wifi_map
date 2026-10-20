from __future__ import annotations

from typing import Protocol

from homesense.core.models import Alert


class NotificationError(Exception):
    """A notifier failed to deliver an alert (transport error or non-2xx answer)."""


class Notifier(Protocol):
    name: str

    async def send(self, alert: Alert) -> None: ...
