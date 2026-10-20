"""Tray icon for the PC running the service: polls the local /api/health and opens the panel.

pystray, Pillow and pywebview come from the optional `desktop` extra and are imported lazily, so this
module imports without them. Health is fetched from 127.0.0.1 on purpose: /api/health returns
`people_home` only to loopback callers (or a session), the LAN sees just the overall status.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import threading
import webbrowser
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from PIL.Image import Image

    from homesense.core.config import Settings

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 30
HEALTH_TIMEOUT_S = 3.0
ICON_SIZE = 64

GREEN = (46, 160, 67)
AMBER = (210, 153, 34)
RED = (200, 50, 50)
_COLORS = {"ok": GREEN, "degraded": AMBER}

LABEL_OPEN_PANEL = "Otwórz panel"
LABEL_OPEN_BROWSER = "Otwórz w przeglądarce"
LABEL_PAUSE_ROUTER = "Wstrzymaj router 15 min"
LABEL_QUIT = "Zakończ"


def health_color(status: str | None) -> tuple[int, int, int]:
    """ok → green, degraded → amber, down / unknown / unreachable → red."""
    return _COLORS.get(status or "", RED)


def make_icon(color: tuple[int, int, int]) -> Image:
    """64×64 RGBA: a filled circle in the status colour with a white house glyph."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((0, 0, ICON_SIZE - 1, ICON_SIZE - 1), fill=(*color, 255))
    white = (255, 255, 255, 255)
    draw.polygon([(14, 33), (32, 15), (50, 33)], fill=white)  # roof
    draw.rectangle((20, 33, 44, 50), fill=white)  # walls
    draw.rectangle((29, 39, 35, 50), fill=(*color, 255))  # door
    return image


async def fetch_health(base_url: str, http: httpx.AsyncClient) -> dict | None:
    """GET <base_url>/api/health with a 3 s timeout; None when unreachable or the answer is unusable."""
    try:
        response = await http.get(f"{base_url}/api/health", timeout=HEALTH_TIMEOUT_S)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("health check failed: %s", exc)
        return None
    return payload if isinstance(payload, dict) else None


def base_url(settings: Settings) -> str:
    """The service always listens on loopback too (host 0.0.0.0 or 127.0.0.1), so talk to it there."""
    return f"http://127.0.0.1:{settings.port}"


def _people(n: int) -> str:
    if n == 1:
        return "osoba"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "osoby"
    return "osób"


def tray_title(payload: dict | None) -> str:
    if payload is None:
        return "HomeSense — brak połączenia"
    people = payload.get("people_home")
    if isinstance(people, int) and not isinstance(people, bool):
        return f"HomeSense — {people} {_people(people)} w domu"
    return f"HomeSense — stan: {payload.get('status', 'nieznany')}"


def open_window(url: str) -> None:
    """Runs in its own process (pywebview needs that process's main thread). Browser if no pywebview."""
    try:
        import webview
    except ImportError:
        webbrowser.open(url)
        return
    webview.create_window("HomeSense", url, width=1280, height=820)
    webview.start()


def spawn_window(url: str) -> None:
    # open_window is a top-level function of an importable module, so Windows' spawn start method
    # can pickle it; the child re-imports this module, which has no side effects at import time.
    multiprocessing.Process(target=open_window, args=(url,), daemon=True).start()


class TrayController:
    """Menu actions and health polling, independent of pystray so it can be tested headless."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.icon: Any = None  # pystray.Icon, set by main()
        self.stop_event = threading.Event()

    def menu_entries(self) -> list[tuple[str, Callable[[], None]]]:
        return [
            (LABEL_OPEN_PANEL, self.open_panel),
            (LABEL_OPEN_BROWSER, self.open_in_browser),
            # Pausing needs a logged-in, CSRF-protected API call; the settings page has the button.
            (LABEL_PAUSE_ROUTER, self.open_settings),
            (LABEL_QUIT, self.quit),
        ]

    def open_panel(self) -> None:
        spawn_window(f"{self.base}/")

    def open_in_browser(self) -> None:
        webbrowser.open(f"{self.base}/")

    def open_settings(self) -> None:
        spawn_window(f"{self.base}/settings")

    def quit(self) -> None:
        self.stop_event.set()
        if self.icon is not None:
            self.icon.stop()

    def apply_health(self, payload: dict | None) -> None:
        if self.icon is None:
            return
        self.icon.icon = make_icon(health_color(payload.get("status") if payload else None))
        self.icon.title = tray_title(payload)

    def poll_forever(self, interval_s: float = POLL_INTERVAL_S) -> None:
        """Poll until quit; one private event loop and HTTP client for the thread's lifetime."""
        loop = asyncio.new_event_loop()
        try:
            http = httpx.AsyncClient()
            try:
                while not self.stop_event.is_set():
                    payload = loop.run_until_complete(fetch_health(self.base, http))
                    try:
                        self.apply_health(payload)
                    except Exception:  # a drawing hiccup must not end polling
                        log.exception("tray update failed")
                    self.stop_event.wait(interval_s)
            finally:
                loop.run_until_complete(http.aclose())
        finally:
            loop.close()


def main(settings: Settings) -> None:
    import pystray

    controller = TrayController(base_url(settings))
    entries = controller.menu_entries()
    menu = pystray.Menu(
        *(
            pystray.MenuItem(label, _menu_action(action), **({"default": True} if i == 0 else {}))
            for i, (label, action) in enumerate(entries)
        )
    )
    icon = pystray.Icon("homesense", make_icon(health_color(None)), tray_title(None), menu)
    controller.icon = icon

    def setup(icon_: Any) -> None:
        icon_.visible = True
        threading.Thread(target=controller.poll_forever, name="tray-health", daemon=True).start()

    icon.run(setup=setup)
    controller.stop_event.set()


def _menu_action(action: Callable[[], None]) -> Callable[[Any, Any], None]:
    # pystray inspects the callback's arity; give it the (icon, item) form explicitly.
    def handler(icon: Any, item: Any) -> None:
        action()

    return handler
