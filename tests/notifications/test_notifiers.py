import json
import logging
import sys
from datetime import UTC, datetime

import httpx
import pytest
import respx

from homesense.core.models import Alert
from homesense.notifications.base import NotificationError
from homesense.notifications.log import LogNotifier
from homesense.notifications.ntfy import NtfyNotifier
from homesense.notifications.telegram import TelegramNotifier
from homesense.notifications.windows_toast import (
    MESSAGE_MAX,
    TITLE_MAX,
    WindowsToastNotifier,
    sanitize_toast_text,
)

TOKEN = "123456:SECRET-token"
TG_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
TITLE = "Nowe urządzenie w sieci"
MESSAGE = "Pojawiło się nieznane urządzenie aa:bb:cc:dd:ee:01"


def alert(severity="warning") -> Alert:
    return Alert(
        kind="new_device",
        severity=severity,
        title=TITLE,
        message=MESSAGE,
        ts=datetime(2026, 10, 20, 10, tzinfo=UTC),
    )


class FakeWinotify:
    """Stand-in for the winotify module; records calls instead of showing a toast."""

    def __init__(self) -> None:
        self.calls: list = []
        calls = self.calls

        class Notification:
            def __init__(self, **kw):
                calls.append(kw)

            def show(self):
                calls.append("show")

        self.Notification = Notification


# --- telegram ---


@respx.mock
async def test_telegram_payload():
    route = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    async with httpx.AsyncClient() as http:
        await TelegramNotifier(TOKEN, "42", http=http).send(alert("warning"))
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == {"chat_id": "42", "text": f"⚠️ {TITLE}\n{MESSAGE}"}


@pytest.mark.parametrize(("severity", "emoji"), [("info", "ℹ️"), ("warning", "⚠️"), ("critical", "🚨")])
@respx.mock
async def test_telegram_emoji_per_severity(severity, emoji):
    route = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    async with httpx.AsyncClient() as http:
        await TelegramNotifier(TOKEN, "42", http=http).send(alert(severity))
    assert json.loads(route.calls.last.request.content)["text"].startswith(f"{emoji} ")


@respx.mock
async def test_telegram_non_2xx_raises_without_token():
    respx.post(TG_URL).mock(return_value=httpx.Response(401, json={"ok": False}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(NotificationError, match="401") as ei:
            await TelegramNotifier(TOKEN, "42", http=http).send(alert())
    assert TOKEN not in str(ei.value)


@respx.mock
async def test_telegram_transport_error_raises_without_token():
    respx.post(TG_URL).mock(side_effect=httpx.ConnectError(f"cannot connect to {TG_URL}"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(NotificationError) as ei:
            await TelegramNotifier(TOKEN, "42", http=http).send(alert())
    assert TOKEN not in str(ei.value)
    # the original httpx error (whose text contains the URL) must not be chained into tracebacks
    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__


def test_telegram_repr_hides_token():
    n = TelegramNotifier(TOKEN, "42")
    assert n.name == "telegram"
    assert TOKEN not in repr(n)


@respx.mock
async def test_telegram_owns_client_when_none_given():
    route = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    n = TelegramNotifier(TOKEN, "42")
    await n.send(alert())
    await n.aclose()
    assert route.call_count == 1


# --- ntfy ---


@pytest.mark.parametrize(("severity", "priority"), [("info", 3), ("warning", 4), ("critical", 5)])
@respx.mock
async def test_ntfy_json_publish_payload(severity, priority):
    route = respx.post("https://ntfy.example.com/").mock(return_value=httpx.Response(200, json={"id": "x"}))
    async with httpx.AsyncClient() as http:
        await NtfyNotifier("https://ntfy.example.com/", "homesense-abc", http=http).send(alert(severity))
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == {
        "topic": "homesense-abc",
        "title": TITLE,
        "message": MESSAGE,
        "priority": priority,
    }


@respx.mock
async def test_ntfy_non_2xx_raises():
    respx.post("https://ntfy.example.com/").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        with pytest.raises(NotificationError, match="500"):
            await NtfyNotifier("https://ntfy.example.com", "t", http=http).send(alert())


@respx.mock
async def test_ntfy_transport_error_hides_url_credentials():
    url = "https://user:s3cret-pass@ntfy.example.com"
    respx.post("https://ntfy.example.com/").mock(side_effect=httpx.ConnectError(f"cannot connect to {url}"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(NotificationError) as ei:
            await NtfyNotifier(url, "t", http=http).send(alert())
    assert "s3cret-pass" not in str(ei.value)
    assert "ConnectError" in str(ei.value)
    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__


@respx.mock
async def test_ntfy_owns_client_when_none_given():
    route = respx.post("https://ntfy.example.com/").mock(return_value=httpx.Response(200))
    n = NtfyNotifier("https://ntfy.example.com", "t")
    assert n.name == "ntfy"
    await n.send(alert())
    await n.aclose()
    assert route.call_count == 1


# --- log ---


@pytest.mark.parametrize(
    ("severity", "level"),
    [("info", logging.INFO), ("warning", logging.WARNING), ("critical", logging.CRITICAL)],
)
async def test_log_notifier_logs_alert(caplog, severity, level):
    n = LogNotifier()
    assert n.name == "log"
    with caplog.at_level(logging.DEBUG, logger="homesense.notifications.log"):
        await n.send(alert(severity))
    [rec] = caplog.records
    assert rec.levelno == level
    assert TITLE in rec.getMessage()
    assert "new_device" in rec.getMessage()


# --- windows toast (never shows a real toast) ---


async def test_windows_toast_uses_injected_backend():
    shown = []
    n = WindowsToastNotifier(show=lambda title, message: shown.append((title, message)))
    assert n.name == "windows"
    await n.send(alert())
    assert shown == [(TITLE, MESSAGE)]


async def test_windows_toast_default_backend_uses_winotify(monkeypatch):
    fake = FakeWinotify()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winotify", fake)
    await WindowsToastNotifier().send(alert())
    assert fake.calls == [{"app_id": "HomeSense", "title": TITLE, "msg": MESSAGE}, "show"]


async def test_windows_toast_without_winotify_warns_once_and_noops(monkeypatch, caplog):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winotify", None)  # makes `import winotify` raise ImportError
    n = WindowsToastNotifier()
    with caplog.at_level(logging.WARNING, logger="homesense.notifications.windows_toast"):
        await n.send(alert())
        await n.send(alert())
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_windows_toast_noop_off_windows(monkeypatch, caplog):
    fake = FakeWinotify()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "winotify", fake)
    n = WindowsToastNotifier()
    with caplog.at_level(logging.WARNING, logger="homesense.notifications.windows_toast"):
        await n.send(alert())
        await n.send(alert())
    assert fake.calls == []
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


# --- toast text sanitization (winotify builds a PowerShell script from title/msg) ---

_FORBIDDEN = set("$`\"'@<>&;|{}[]\r\n\t\x00\x1b“”„‘’")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"; Start-Process calc; "', "Start-Process calc"),
        ("$(whoami)", "(whoami)"),
        ("`whoami`", "whoami"),
        ('line one\n"@\nStart-Process calc', "line one Start-Process calc"),
        ("]]><script>alert(1)</script>", "scriptalert(1)/script"),
        ("a & b ; c | d", "a b c d"),
        ("host\r\nname\x00\x1b[31m", "host name31m"),
        ("{x}[y]'z'“q”„w”", "xyzqw"),
        ("Nowe urządzenie w sieci", "Nowe urządzenie w sieci"),
        ("ZAŻÓŁĆ gęślą jaźń 12:30 (a_b-c/d.e, f)", "ZAŻÓŁĆ gęślą jaźń 12:30 (a_b-c/d.e, f)"),
    ],
)
def test_sanitize_toast_text_allow_list(raw, expected):
    out = sanitize_toast_text(raw, 200)
    assert out == expected
    assert not (_FORBIDDEN & set(out))


def test_sanitize_toast_text_truncates_and_collapses_whitespace():
    assert sanitize_toast_text("a    b\t\tc", 200) == "a b c"
    assert sanitize_toast_text("x" * 1000, TITLE_MAX) == "x" * TITLE_MAX
    assert sanitize_toast_text("ab " * 100, 5) == "ab ab"


async def test_windows_toast_sends_only_inert_text_to_winotify(monkeypatch):
    fake = FakeWinotify()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winotify", fake)
    evil = Alert(
        kind="new_device",
        severity="warning",
        title='Nowe urządzenie w sieci "; Start-Process calc; "' + "A" * 200,
        message='host $(whoami) `whoami`\r\n"@\nStart-Process calc ]]><script>&;' + "B" * 500,
        ts=datetime(2026, 10, 20, 10, tzinfo=UTC),
    )
    await WindowsToastNotifier().send(evil)
    [kw, shown] = fake.calls
    assert shown == "show"
    assert kw["app_id"] == "HomeSense"
    assert kw["title"] == ("Nowe urządzenie w sieci Start-Process calc " + "A" * 200)[:TITLE_MAX]
    assert kw["msg"] == ("host (whoami) whoami Start-Process calc script" + "B" * 500)[:MESSAGE_MAX]
    for text in (kw["title"], kw["msg"]):
        assert not (_FORBIDDEN & set(text))
