from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homesense.core.clock import Clock
from homesense.core.models import DeviceObservation, normalize_mac

log = logging.getLogger(__name__)

_CONNECTION_MARKERS = (("WIRED", "wired"), ("2G", "2g"), ("5G", "5g"), ("6G", "6g"))


def map_connection(conn: Any) -> str:
    """Map a tplinkrouterc6u ``Connection`` (matched by enum name) to wired|2g|5g|6g|unknown."""
    if conn is None:
        return "unknown"
    name = str(getattr(conn, "name", conn)).upper()
    for marker, value in _CONNECTION_MARKERS:
        if marker in name:
            return value
    return "unknown"


def _bytes_per_s_to_kbps(value: Any) -> float | None:
    return None if value is None else value * 8 / 1000


class TplinkRouterCollector:
    """Reads the connected-client list from a TP-Link router (tplinkrouterc6u)."""

    name = "router"

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        clock: Clock,
        interval_s: float = 60,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.interval_s = interval_s
        self._clock = clock
        if client_factory is None:

            def client_factory() -> Any:
                from tplinkrouterc6u import TplinkRouterProvider

                return TplinkRouterProvider.get_client(f"http://{host}", password, username)

        self._client_factory = client_factory

    def _fetch_status(self) -> Any:
        client = self._client_factory()
        # Only log out after a successful login: the library's logout raises when not authorised,
        # which would otherwise hide the real error (e.g. a wrong password).
        client.authorize()
        try:
            return client.get_status()
        finally:
            try:
                client.logout()
            except Exception:
                log.warning("router logout failed", exc_info=True)

    async def collect(self) -> list[DeviceObservation]:
        status = await asyncio.to_thread(self._fetch_status)
        now = self._clock.now()
        return [
            DeviceObservation(
                ts=now,
                mac=normalize_mac(d.macaddr),
                source="router",
                ip=d.ipaddr,
                hostname=d.hostname or None,
                connection=map_connection(d.type),
                signal=getattr(d, "signal", None),
                up_kbps=_bytes_per_s_to_kbps(getattr(d, "up_speed", None)),
                down_kbps=_bytes_per_s_to_kbps(getattr(d, "down_speed", None)),
            )
            for d in status.devices
        ]
