from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable

from homesense.core.models import Alert

log = logging.getLogger(__name__)

ShowToast = Callable[[str, str], None]

TITLE_MAX = 64
MESSAGE_MAX = 200
_ALLOWED_PUNCTUATION = frozenset(" .:-_/(),")
_ASCII_DIGITS = frozenset("0123456789")


def sanitize_toast_text(text: str, max_len: int) -> str:
    """Allow-list filter for text handed to winotify.

    winotify formats title/message into a PowerShell script (double-quoted here-string, `$Toast.Tag = "..."`)
    and runs powershell.exe, while alert text contains attacker-controlled hostnames and domains. Only
    letters (incl. diacritics), ASCII digits, space and ``. : - _ / ( ) ,`` survive; whitespace runs collapse
    to one space; the result is truncated to ``max_len``.
    """
    kept = []
    for ch in text:
        if ch.isspace():
            kept.append(" ")
        elif ch.isalpha() or ch in _ASCII_DIGITS or ch in _ALLOWED_PUNCTUATION:
            kept.append(ch)
    return " ".join("".join(kept).split())[:max_len].rstrip()


def _load_winotify_backend(app_id: str) -> ShowToast | None:
    if sys.platform != "win32":
        return None
    try:
        import winotify
    except ImportError:
        return None

    def show(title: str, message: str) -> None:
        winotify.Notification(app_id=app_id, title=title, msg=message).show()

    return show


class WindowsToastNotifier:
    """Shows a Windows toast via winotify; no-op (one warning) when winotify or Windows is unavailable."""

    name = "windows"

    def __init__(self, app_id: str = "HomeSense", show: ShowToast | None = None) -> None:
        self._app_id = app_id
        self._show = show
        self._resolved = show is not None

    def _backend(self) -> ShowToast | None:
        if not self._resolved:
            self._resolved = True
            self._show = _load_winotify_backend(self._app_id)
            if self._show is None:
                log.warning("Windows toasts unavailable (winotify not installed or not on Windows); skipping")
        return self._show

    async def send(self, alert: Alert) -> None:
        show = self._backend()
        if show is None:
            return
        title = sanitize_toast_text(alert.title, TITLE_MAX)
        message = sanitize_toast_text(alert.message, MESSAGE_MAX)
        await asyncio.to_thread(show, title, message)
