import asyncio
import json
import re
from datetime import timedelta
from typing import get_args

import pytest

from homesense.api import routes_pages
from homesense.api.routes_api import DeviceKind
from homesense.cli import _ensure_sim_persons
from homesense.core.models import Alert, DeviceObservation

MAC = "f0:18:98:a1:00:01"
PAGES = ["/", "/persons", "/devices", "/network", "/analytics", "/alerts", "/settings", "/privacy"]


def _h1(html: str) -> str:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    assert match, "page has no <h1>"
    return match.group(1).strip()


# --- access ---


@pytest.mark.parametrize(
    "path", [*PAGES, "/persons/1", f"/devices/{MAC}", "/partials/presence", "/partials/events"]
)
def test_pages_redirect_to_login_without_session(client, path):
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize("path", ["/", "/partials/presence", "/partials/events", "/api/presence"])
def test_htmx_requests_without_session_get_hx_redirect(client, path):
    response = client.get(path, headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login"


def test_device_filter_query_parameter(client, core, clock, auth):
    core.repos.devices.upsert_seen(MAC, clock.now() - timedelta(days=2))  # offline
    assert MAC in client.get("/devices").text
    online = client.get("/devices", params={"filter": "online"}).text
    assert MAC not in online and "Brak urządzeń w tym widoku" in online
    assert MAC in client.get("/devices", params={"filter": "bogus"}).text  # falls back to all


# --- Review Focus #5: empty database ---


@pytest.mark.parametrize(
    ("path", "heading", "empty_text"),
    [
        ("/", "Teraz", "Brak osób"),
        ("/persons", "Osoby", "Brak osób"),
        ("/devices", "Urządzenia", "Brak urządzeń"),
        ("/network", "Sieć", "Brak danych DNS"),
        ("/analytics", "Analityka", "Grafana jest niedostępna"),
        ("/alerts", "Alerty", "Brak alertów"),
        ("/settings", "Ustawienia", "brak danych"),
        ("/privacy", "Co zbiera system", "Brak osób"),
    ],
)
def test_pages_render_with_empty_database(client, auth, path, heading, empty_text):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert _h1(response.text) == heading
    assert empty_text in response.text


def test_detail_pages_render_without_history(client, core, clock, auth):
    pid = core.repos.persons.create("Anna")
    core.repos.devices.upsert_seen(MAC, clock.now() - timedelta(days=2))
    person = client.get(f"/persons/{pid}")
    assert person.status_code == 200
    assert _h1(person.text) == "Anna"
    assert "Brak danych" in person.text
    device = client.get(f"/devices/{MAC}")
    assert device.status_code == 200
    assert _h1(device.text) == MAC
    assert "Brak danych" in device.text


def test_unknown_person_and_device_are_404(client, auth):
    assert client.get("/persons/999").status_code == 404
    assert client.get("/devices/00:11:22:33:44:55").status_code == 404
    assert client.get("/devices/not-a-mac").status_code == 404


def test_partials_render_with_empty_database(client, auth):
    presence = client.get("/partials/presence")
    assert presence.status_code == 200 and "Brak osób" in presence.text
    events = client.get("/partials/events")
    assert events.status_code == 200 and "Brak zdarzeń" in events.text


# --- with data ---


def test_now_shows_simulated_persons(client, core, clock, auth):
    _ensure_sim_persons(core)

    async def steps() -> None:
        for _ in range(5):
            await core.step()
            clock.advance(timedelta(minutes=1))

    asyncio.run(steps())
    page = client.get("/").text
    for name in ("Anna", "Piotr", "Ola"):
        assert name in page
    assert "Brak osób" not in page
    partial = client.get("/partials/presence").text
    assert "Anna" in partial and "<html" not in partial


def test_person_page_heatmap_and_typical_times(client, core, clock, auth):
    pid = core.repos.persons.create("Anna", color="#ff0000")
    core.repos.profiles.replace("person_home", str(pid), {(0, 7): (0.5, 3), (6, 23): (1.0, 2)}, clock.now())
    html = client.get(f"/persons/{pid}").text
    assert html.count('class="hm-cell') == 7 * 24
    assert "lvl-5" in html and "lvl-10" in html
    assert 'data-color="#ff0000"' in html


def test_network_page_embeds_chart_data(client, core, clock, auth):
    core.repos.devices.upsert_seen(MAC, clock.now())
    hour = clock.now().replace(minute=0, second=0, microsecond=0)
    core.repos.dns.add_hourly(hour, MAC, "example.com", 12, 2)
    core.repos.dns.add_hourly_stats(hour, MAC, 12, 2)
    html = client.get("/network").text
    assert "example.com" in html
    data = json.loads(
        re.search(r'<script type="application/json" id="chart-hourly">(.*?)</script>', html, re.S)[1]
    )
    assert len(data["labels"]) == 48
    assert sum(data["datasets"][0]["data"]) == 12


def test_device_page_lists_persons_and_kinds(client, core, clock, auth):
    core.repos.persons.create("Anna")
    core.repos.devices.upsert_seen(MAC, clock.now(), "192.168.0.101", "Anna-iPhone")
    core.repos.observations.add_many(
        [DeviceObservation(ts=clock.now(), mac=MAC, source="sim", down_kbps=80.0)]
    )
    html = client.get(f"/devices/{MAC}").text
    assert _h1(html) == "Anna-iPhone"
    assert ">Anna</option>" in html
    for kind in get_args(DeviceKind):
        assert f'value="{kind}"' in html
    assert 'id="chart-online"' in html and 'id="chart-traffic"' in html


def test_device_kinds_have_polish_labels():
    assert set(routes_pages.KIND_LABELS) == set(get_args(DeviceKind))


def test_alert_badge_and_list(client, core, clock, auth):
    core.repos.alerts.add(
        Alert(kind="new_device", severity="warning", title="Nowe urządzenie", message="x", ts=clock.now())
    )
    html = client.get("/alerts").text
    assert re.search(r'class="badge"[^>]*>\s*1\s*<', html)
    assert "Nowe urządzenie" in html and "Brak alertów" not in html


# --- security ---


def test_untrusted_strings_are_escaped(client, core, clock, auth):
    core.repos.devices.upsert_seen(MAC, clock.now(), "192.168.0.101", "<script>alert(1)</script>")
    for path in ("/devices", f"/devices/{MAC}"):
        html = client.get(path).text
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_pages_carry_csrf_token_and_security_headers(client, auth):
    response = client.get("/")
    token = auth["X-CSRF-Token"]
    assert f'<meta name="csrf-token" content="{token}">' in response.text
    assert "hx-headers" in response.text and token in re.search(r"hx-headers='([^']*)'", response.text)[1]
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'self'" in csp
    assert "'unsafe-inline'" not in csp
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_login_page_has_csp(client, core):
    core.users.create("admin", "x")
    response = client.get("/login")
    assert response.status_code == 200
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]


# --- PWA ---


def test_manifest_is_standalone_json(client):
    response = client.get("/static/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["display"] == "standalone"
    assert {i["sizes"] for i in manifest["icons"]} == {"192x192", "512x512"}
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_service_worker_served_at_root_scope(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["service-worker-allowed"] == "/"
    assert "javascript" in response.headers["content-type"]
    assert "/api/" in response.text  # API requests are explicitly bypassed
    assert '"/static/js/", "/static/css/"' in response.text  # app code is network-first


@pytest.mark.parametrize(
    "path",
    [
        "/static/js/app.js",
        "/static/css/app.css",
        "/static/vendor/htmx.min.js",
        "/static/vendor/alpine.min.js",
        "/static/vendor/chart.umd.min.js",
        "/static/icons/favicon.svg",
    ],
)
def test_static_assets_exist(client, path):
    assert client.get(path).status_code == 200
