from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

from homesense.core.clock import Clock
from homesense.core.events import EventBus
from homesense.core.models import Alert, AlertRaised
from homesense.storage.repositories import Repos

log = logging.getLogger(__name__)


class AlertService:
    """Stores alerts and publishes AlertRaised, suppressing repeats of a dedup_key within the window.

    While `suppressed()` is true (monitoring paused) alerts are dropped: nothing is stored or sent.
    """

    def __init__(
        self,
        repos: Repos,
        bus: EventBus,
        clock: Clock,
        dedup_window: timedelta = timedelta(hours=3),
        suppressed: Callable[[], bool] | None = None,
    ) -> None:
        self._repos = repos
        self._bus = bus
        self._clock = clock
        self._dedup_window = dedup_window
        self._suppressed = suppressed or (lambda: False)

    async def raise_alert(self, alert: Alert) -> Alert | None:
        """Returns the stored alert (with id set), or None when it was deduplicated or suppressed."""
        if self._suppressed():
            log.info("alert %s dropped: monitoring is paused", alert.kind)
            return None
        if alert.dedup_key and self._repos.alerts.exists_recent(
            alert.dedup_key, self._clock.now() - self._dedup_window
        ):
            return None
        alert.id = self._repos.alerts.add(alert)
        await self._bus.publish(AlertRaised(alert))
        return alert
