from urllib.parse import unquote

import anyio
import httpx
import pytest
import respx

from tests.api.conftest import GRAFANA_URL


def test_grafana_requires_login(client):
    response = client.get("/grafana/d/presence")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@respx.mock(base_url=GRAFANA_URL)
def test_grafana_proxy_forwards_path_and_query(respx_mock, client, auth):
    route = respx_mock.get("/grafana/d/presence/obecnosc", params={"kiosk": "1", "orgId": "1"}).respond(
        200,
        text="<html>dashboard</html>",
        headers={
            "content-type": "text/html",
            "x-grafana": "yes",
            "set-cookie": "grafana_session=x",
            "connection": "close",
        },
    )
    response = client.get(
        "/grafana/d/presence/obecnosc?kiosk=1&orgId=1",
        headers={**auth, "Connection": "keep-alive", "X-Custom": "1"},
    )
    assert response.status_code == 200
    assert response.text == "<html>dashboard</html>"
    assert response.headers["x-grafana"] == "yes"
    assert "set-cookie" not in response.headers

    sent = route.calls.last.request
    assert sent.url.path == "/grafana/d/presence/obecnosc"
    assert sent.url.params["kiosk"] == "1"
    assert sent.headers["x-custom"] == "1"
    assert sent.headers["host"] == "grafana.test"
    for header in ("cookie", "x-csrf-token"):
        assert header not in sent.headers


SAME_ORIGIN = "http://testserver"


@respx.mock(base_url=GRAFANA_URL)
def test_grafana_proxy_forwards_same_origin_post(respx_mock, client, auth):
    route = respx_mock.post("/grafana/api/ds/query").respond(200, json={"results": {}})
    response = client.post(
        "/grafana/api/ds/query", json={"queries": []}, headers={**auth, "Origin": SAME_ORIGIN}
    )
    assert response.status_code == 200 and response.json() == {"results": {}}
    sent = route.calls.last.request
    assert sent.content == b'{"queries":[]}'
    assert "origin" not in sent.headers and "referer" not in sent.headers


@respx.mock(base_url=GRAFANA_URL)
def test_grafana_proxy_accepts_same_origin_referer(respx_mock, client, auth):
    respx_mock.post("/grafana/api/live/publish").respond(200)
    referer = {"Referer": f"{SAME_ORIGIN}/grafana/d/presence?kiosk"}
    assert client.post("/grafana/api/live/publish", headers=referer).status_code == 200


@respx.mock(base_url=GRAFANA_URL, assert_all_called=False)
def test_grafana_proxy_rejects_cross_origin_and_unlisted_mutations(respx_mock, client, auth):
    route = respx_mock.route().respond(200)
    cross = client.post("/grafana/api/ds/query", json={}, headers={"Origin": "http://evil.example"})
    cross_referer = client.post("/grafana/api/ds/query", headers={"Referer": "http://evil.example/x"})
    no_origin = client.post("/grafana/api/ds/query", json={})
    port_mismatch = client.post("/grafana/api/ds/query", headers={"Origin": "http://testserver:8081"})
    unlisted = client.delete("/grafana/api/dashboards/uid/x", headers={"Origin": SAME_ORIGIN})
    unlisted_post = client.post("/grafana/api/dashboards/db", headers={"Origin": SAME_ORIGIN})
    for response in (cross, cross_referer, no_origin, port_mismatch, unlisted, unlisted_post):
        assert response.status_code == 403
    assert not route.calls


@respx.mock(base_url=GRAFANA_URL)
def test_grafana_down_returns_502_page(respx_mock, client, auth):
    respx_mock.get("/grafana/").mock(side_effect=httpx.ConnectError("refused"))
    response = client.get("/grafana/")
    assert response.status_code == 502
    assert "Grafana niedostępna" in response.text


@respx.mock(base_url=GRAFANA_URL)
def test_grafana_proxy_rejects_dot_segments(respx_mock, client, auth):
    assert client.get("/grafana/public/%2e%2e/%2e%2e/api/admin").status_code == 400
    assert not respx_mock.calls


def _raw_request(client, method: str, raw_path: bytes, headers: dict[str, str] | None = None) -> int:
    """Send `raw_path` to the app verbatim (httpx would normalize dot segments); returns the status."""
    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    raw_headers = [(b"host", b"testserver"), (b"cookie", cookie.encode())]
    raw_headers += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": unquote(raw_path.decode("ascii")),
        "raw_path": raw_path,
        "root_path": "",
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent: list[dict] = []
    done = client.portal.call(anyio.Event)
    requested = False

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await done.wait()  # the client "disconnects" only after the full response
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            done.set()

    client.portal.call(client.app, scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


TRICKY_PATHS = [
    b"/grafana/api/ds/query/../../dashboards/x",
    b"/grafana/api/ds/%2e%2e/dashboards/x",
    b"/grafana/api/ds/%2E%2E/dashboards/x",
    b"/grafana/api%2Fdashboards%2Fx",
    b"/grafana/api//ds/query",
    b"/grafana/api\\ds\\query",
    b"/grafana/api/ds/./query",
    b"/grafana/api/ds/query/",
    b"/grafana/api/ds/%2571uery",
    b"/grafana/api/ds/qu%20ery",
    b"/grafana/api/ds/query/extra",
    b"/grafana/API/DS/QUERY",
    b"/grafana/api/live/a/../../../dashboards/x",
    b"/grafana/api/live/pub.lish",
]


@pytest.mark.parametrize("raw_path", TRICKY_PATHS)
@respx.mock(base_url=GRAFANA_URL, assert_all_called=False)
def test_grafana_proxy_rejects_path_tricks(respx_mock, client, auth, raw_path):
    route = respx_mock.route().respond(200)
    assert _raw_request(client, "POST", raw_path, {"Origin": SAME_ORIGIN}) in (400, 403)
    assert not route.calls


# Not canonical, so rejected for every method (the others are only blocked by the mutation allowlist).
NON_CANONICAL = [p for p in TRICKY_PATHS if not p.endswith((b"extra", b"QUERY", b"pub.lish"))]


@pytest.mark.parametrize("raw_path", NON_CANONICAL)
@respx.mock(base_url=GRAFANA_URL, assert_all_called=False)
def test_grafana_proxy_rejects_path_tricks_for_get(respx_mock, client, auth, raw_path):
    route = respx_mock.route().respond(200)
    assert _raw_request(client, "GET", raw_path) == 400
    assert not route.calls


@respx.mock(base_url=GRAFANA_URL)
def test_grafana_proxy_forwards_canonical_path(respx_mock, client, auth):
    route = respx_mock.route().respond(200)
    assert _raw_request(client, "GET", b"/grafana/public/build/app%2D1.js") == 200
    assert route.calls.last.request.url.raw_path == b"/grafana/public/build/app-1.js"
    assert _raw_request(client, "POST", b"/grafana/api/live/publish", {"Origin": SAME_ORIGIN}) == 200
