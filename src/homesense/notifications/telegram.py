from __future__ import annotations

import httpx

from homesense.core.models import Alert
from homesense.notifications.base import NotificationError

_EMOJI = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}


class TelegramNotifier:
    """Sends alerts via the Telegram Bot API. The bot token is part of the URL, so errors never include it."""

    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, http: httpx.AsyncClient | None = None) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient(timeout=10)

    def __repr__(self) -> str:
        return f"TelegramNotifier(chat_id={self._chat_id!r})"

    async def send(self, alert: Alert) -> None:
        text = f"{_EMOJI.get(alert.severity, '')} {alert.title}\n{alert.message}"
        try:
            response = await self._http.post(self._url, json={"chat_id": self._chat_id, "text": text})
        except httpx.HTTPError as exc:
            # `from None`: the httpx exception text/request carry the token-bearing URL
            raise NotificationError(f"telegram sendMessage failed: {type(exc).__name__}") from None
        if not response.is_success:
            raise NotificationError(f"telegram sendMessage returned HTTP {response.status_code}")

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
