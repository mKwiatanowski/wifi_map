from __future__ import annotations

import logging

from homesense.alerts.rules import LOG_CHANNEL, AlertRules
from homesense.core.clock import Clock
from homesense.core.models import Alert, AlertRaised
from homesense.core.registry import Registry
from homesense.notifications.base import Notifier

log = logging.getLogger(__name__)

TEST_TITLE = "Test powiadomień"
TEST_MESSAGE = "To jest testowe powiadomienie z HomeSense. Jeśli je widzisz, kanał działa."


class NotificationDispatcher:
    """Routes raised alerts to registered notifiers according to AlertRules."""

    def __init__(self, rules: AlertRules, notifiers: Registry[Notifier], clock: Clock) -> None:
        self._rules = rules
        self._notifiers = notifiers
        self._clock = clock

    async def on_alert(self, event: AlertRaised) -> None:
        alert = event.alert
        try:
            channels = self._rules.channels_for(alert, self._clock.now())
        except Exception:
            log.exception("evaluating alert rules failed for %s; falling back to log only", alert.kind)
            channels = [LOG_CHANNEL]
        registered = set(self._notifiers.names())
        for channel in channels:
            if channel not in registered:
                continue
            try:
                await self._notifiers.get(channel).send(alert)
            except Exception:
                log.exception("notifier %s failed to send alert %s (id=%s)", channel, alert.kind, alert.id)

    async def send_test(self, channel: str) -> None:
        """Sends a test alert through one channel; KeyError if not registered, notifier errors propagate."""
        if channel not in self._notifiers.names():
            raise KeyError(f"notification channel not registered: {channel}")
        alert = Alert(
            kind="test", severity="info", title=TEST_TITLE, message=TEST_MESSAGE, ts=self._clock.now()
        )
        await self._notifiers.get(channel).send(alert)
