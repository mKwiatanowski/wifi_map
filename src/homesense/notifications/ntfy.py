from __future__ import annotations

import httpx

from homesense.core.models import Alert
from homesense.notifications.base import NotificationError

_PRIORITY = {"info": 3, "warning": 4, "critical": 5}


class NtfyNotifier:
    """Publishes alerts to ntfy using the JSON publish API (UTF-8 safe titles, unlike the Title header)."""

    name = "ntfy"

    def __init__(self, base_url: str, topic: str, http: httpx.AsyncClient | None = None) -> None:
        self._url = base_url.rstrip("/")
        self._topic = topic
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient(timeout=10)

    async def send(self, alert: Alert) -> None:
        payload = {
            "topic": self._topic,
            "title": alert.title,
            "message": alert.message,
            "priority": _PRIORITY.get(alert.severity, 3),
        }
        try:
            response = await self._http.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            # `from None`: the httpx exception text/request carry the base URL, which may embed credentials
            raise NotificationError(f"ntfy publish failed: {type(exc).__name__}") from None
        if not response.is_success:
            raise NotificationError(f"ntfy publish returned HTTP {response.status_code}")

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
