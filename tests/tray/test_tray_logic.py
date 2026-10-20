import sys
import threading
import types

import httpx
import pytest
import respx

from homesense.tray import app as tray

BASE = "http://127.0.0.1:8080"


# --- health_color ---


@pytest.mark.parametrize(
    ("status", "color"),
    [
        ("ok", (46, 160, 67)),
        ("degraded", (210, 153, 34)),
        ("down", (200, 50, 50)),
        (None, (200, 50, 50)),
        ("weird", (200, 50, 50)),
    ],
)
def test_health_color(status, color):
    assert tray.health_color(status) == color


# --- fetch_health ---


@respx.mock
async def test_fetch_health_ok():
    route = respx.get(f"{BASE}/api/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "people_home": 2, "collectors": []})
    )
    async with httpx.AsyncClient() as http:
        payload = await tray.fetch_health(BASE, http)
    assert route.called
    assert payload == {"status": "ok", "people_home": 2, "collectors": []}


@respx.mock
async def test_fetch_health_uses_three_second_timeout():
    route = respx.get(f"{BASE}/api/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    async with httpx.AsyncClient() as http:
        await tray.fetch_health(BASE, http)
    timeout = route.calls.last.request.extensions["timeout"]
    assert timeout["connect"] == 3 and timeout["read"] == 3


@respx.mock
async def test_fetch_health_connection_error_returns_none():
    respx.get(f"{BASE}/api/health").mock(side_effect=httpx.ConnectError("refused"))
    async with httpx.AsyncClient() as http:
        assert await tray.fetch_health(BASE, http) is None


@respx.mock
async def test_fetch_health_http_error_returns_none():
    respx.get(f"{BASE}/api/health").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        assert await tray.fetch_health(BASE, http) is None


@respx.mock
@pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
async def test_fetch_health_bad_body_returns_none(body):
    respx.get(f"{BASE}/api/health").mock(return_value=httpx.Response(200, content=body))
    async with httpx.AsyncClient() as http:
        assert await tray.fetch_health(BASE, http) is None


# --- make_icon ---


def test_make_icon_is_64x64_rgba():
    pytest.importorskip("PIL")
    image = tray.make_icon((46, 160, 67))
    assert image.size == (64, 64)
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0  # transparent corner
    assert image.getpixel((32, 6))[:3] == (46, 160, 67)  # circle body in the status colour


# --- urls and title ---


def test_base_url_is_loopback_with_settings_port(settings):
    settings.port = 9123
    settings.host = "0.0.0.0"
    assert tray.base_url(settings) == "http://127.0.0.1:9123"


@pytest.mark.parametrize(
    ("payload", "title"),
    [
        ({"status": "ok", "people_home": 0}, "HomeSense — 0 osób w domu"),
        ({"status": "ok", "people_home": 1}, "HomeSense — 1 osoba w domu"),
        ({"status": "ok", "people_home": 3}, "HomeSense — 3 osoby w domu"),
        ({"status": "ok", "people_home": 5}, "HomeSense — 5 osób w domu"),
        ({"status": "ok", "people_home": 12}, "HomeSense — 12 osób w domu"),
        ({"status": "ok", "people_home": 22}, "HomeSense — 22 osoby w domu"),
        ({"status": "degraded"}, "HomeSense — stan: degraded"),
        (None, "HomeSense — brak połączenia"),
    ],
)
def test_tray_title(payload, title):
    assert tray.tray_title(payload) == title


# --- open_window ---


def test_open_window_uses_pywebview(monkeypatch):
    calls = []
    fake = types.ModuleType("webview")
    fake.create_window = lambda *a, **kw: calls.append(("create", a, kw))
    fake.start = lambda: calls.append(("start",))
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(tray.webbrowser, "open", lambda url: calls.append(("browser", url)))

    tray.open_window(f"{BASE}/")

    assert calls == [("create", ("HomeSense", f"{BASE}/"), {"width": 1280, "height": 820}), ("start",)]


def test_open_window_falls_back_to_browser_without_pywebview(monkeypatch):
    opened = []
    monkeypatch.setitem(sys.modules, "webview", None)  # makes `import webview` raise ImportError
    monkeypatch.setattr(tray.webbrowser, "open", opened.append)

    tray.open_window(f"{BASE}/")

    assert opened == [f"{BASE}/"]


# --- controller (menu actions, health updates) ---


class FakeIcon:
    def __init__(self):
        self.icon = None
        self.title = None
        self.stopped = False

    def stop(self):
        self.stopped = True


@pytest.fixture
def controller(monkeypatch):
    windows, browser = [], []
    monkeypatch.setattr(tray, "spawn_window", windows.append)
    monkeypatch.setattr(tray.webbrowser, "open", browser.append)
    ctl = tray.TrayController(BASE)
    ctl.windows, ctl.browser = windows, browser
    return ctl


def test_menu_labels_in_order(controller):
    assert [label for label, _ in controller.menu_entries()] == [
        "Otwórz panel",
        "Otwórz w przeglądarce",
        "Wstrzymaj router 15 min",
        "Zakończ",
    ]


def test_menu_actions_open_panel_browser_and_settings(controller):
    actions = dict(controller.menu_entries())
    actions["Otwórz panel"]()
    actions["Otwórz w przeglądarce"]()
    actions["Wstrzymaj router 15 min"]()
    assert controller.windows == [f"{BASE}/", f"{BASE}/settings"]
    assert controller.browser == [f"{BASE}/"]


def test_quit_stops_poller_and_icon(controller):
    icon = FakeIcon()
    controller.icon = icon
    dict(controller.menu_entries())["Zakończ"]()
    assert controller.stop_event.is_set()
    assert icon.stopped


def test_apply_health_updates_title_and_icon(controller, monkeypatch):
    monkeypatch.setattr(tray, "make_icon", lambda color: ("icon", color))
    icon = FakeIcon()
    controller.icon = icon

    controller.apply_health({"status": "degraded", "people_home": 2})
    assert icon.title == "HomeSense — 2 osoby w domu"
    assert icon.icon == ("icon", (210, 153, 34))

    controller.apply_health(None)
    assert icon.title == "HomeSense — brak połączenia"
    assert icon.icon == ("icon", (200, 50, 50))


@respx.mock
def test_poll_loop_polls_until_stopped(controller):
    route = respx.get(f"{BASE}/api/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "people_home": 1})
    )
    seen = []

    def record(payload):
        seen.append(payload)
        controller.stop_event.set()

    controller.apply_health = record
    thread = threading.Thread(target=controller.poll_forever, kwargs={"interval_s": 30})
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert route.call_count == 1
    assert seen == [{"status": "ok", "people_home": 1}]


def test_spawn_window_starts_process_with_top_level_target(monkeypatch):
    started = []

    class FakeProcess:
        def __init__(self, target, args, daemon):
            started.append((target, args, daemon))

        def start(self):
            started.append("started")

    monkeypatch.setattr(tray.multiprocessing, "Process", FakeProcess)
    tray.spawn_window(f"{BASE}/")
    assert started == [(tray.open_window, (f"{BASE}/",), True), "started"]


# --- main wiring with a fake pystray ---


def test_main_builds_icon_and_runs(monkeypatch, settings):
    pytest.importorskip("PIL")
    created = {}

    class Icon(FakeIcon):
        def __init__(self, name, icon, title, menu):
            super().__init__()
            created.update(name=name, title=title, menu=menu)
            self.icon = icon
            self.title = title

        def run(self, setup=None):
            created["ran"] = True
            created["setup"] = setup

    fake = types.ModuleType("pystray")
    fake.Icon = Icon
    fake.Menu = lambda *items: list(items)
    fake.MenuItem = lambda text, action, **kw: (text, action, kw)
    monkeypatch.setitem(sys.modules, "pystray", fake)

    threads = []

    def fake_thread(**kw):
        threads.append(kw)
        return types.SimpleNamespace(start=lambda: threads.append("started"))

    monkeypatch.setattr(tray.threading, "Thread", fake_thread)
    settings.port = 8181

    tray.main(settings)

    assert created["name"] == "homesense"
    assert created["title"] == "HomeSense — brak połączenia"
    assert [item[0] for item in created["menu"]] == [
        "Otwórz panel",
        "Otwórz w przeglądarce",
        "Wstrzymaj router 15 min",
        "Zakończ",
    ]
    assert created["menu"][0][2] == {"default": True}
    assert created["ran"]
    # the poller is started from the icon's setup callback, as a daemon thread
    shown = types.SimpleNamespace(visible=False)
    created["setup"](shown)
    assert shown.visible is True
    assert threads[0]["daemon"] is True
    assert threads[-1] == "started"
