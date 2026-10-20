from __future__ import annotations

import logging

from homesense.core.models import Alert

log = logging.getLogger(__name__)

_LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "critical": logging.CRITICAL}


class LogNotifier:
    """Writes alerts to the application log."""

    name = "log"

    async def send(self, alert: Alert) -> None:
        log.log(
            _LEVELS.get(alert.severity, logging.INFO),
            "alert [%s/%s] %s: %s",
            alert.kind,
            alert.severity,
            alert.title,
            alert.message,
        )
