import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from homesense.core.models import DnsQueryRecord
from homesense.network.adguard import AdGuardClient, AdGuardError, parse_querylog

BASE = "http://adguard.test:3001"
FIXTURE = Path(__file__).parent.parent / "fixtures" / "adguard_querylog.json"


def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
async def client():
    c = AdGuardClient(BASE, "admin", "secret")
    yield c
    await c.aclose()


def test_parse_querylog_fixture():
    records = parse_querylog(fixture_payload())

    assert records == [
        DnsQueryRecord(
            ts=datetime(2026, 10, 19, 6, 0, 5, 123000, tzinfo=UTC),
            client_ip="192.168.0.23",
            domain="api.spotify.com",
            blocked=False,
            reason="NotFilteredNotFound",
        ),
        DnsQueryRecord(
            ts=datetime(2026, 10, 19, 6, 0, 3, tzinfo=UTC),
            client_ip="192.168.0.40",
            domain="ads.example",
            blocked=True,
            reason="FilteredBlackList",
        ),
    ]
    assert records[1].ts.utcoffset().total_seconds() == 0


@pytest.mark.parametrize(
    ("reason", "blocked"),
    [
        ("FilteredBlackList", True),
        ("FilteredBlockedService", True),
        ("FilteredSafeBrowsing", True),
        ("FilteredParental", True),
        ("NotFilteredNotFound", False),
        ("NotFilteredWhiteList", False),
        ("Rewrite", False),
    ],
)
def test_parse_querylog_blocked_by_reason(reason, blocked):
    payload = {
        "data": [
            {
                "time": "2026-10-19T06:00:00Z",
                "client": "::1",
                "question": {"name": "X.COM."},
                "reason": reason,
            }
        ]
    }
    [record] = parse_querylog(payload)
    assert record.blocked is blocked
    assert record.reason == reason
    assert record.domain == "x.com"


def test_parse_querylog_skips_malformed_items_and_handles_empty():
    payload = {
        "data": [{"client": "1.2.3.4"}, {"time": "garbage", "client": "1.2.3.4", "question": {"name": "a"}}]
    }
    assert parse_querylog(payload) == []
    assert parse_querylog({}) == []
    assert parse_querylog({"data": None}) == []


@respx.mock
async def test_status_uses_basic_auth(client):
    route = respx.get(f"{BASE}/control/status").mock(return_value=httpx.Response(200, json={"running": True}))

    assert await client.status() == {"running": True}
    assert route.calls.last.request.headers["Authorization"].startswith("Basic ")


@respx.mock
async def test_no_auth_header_without_username():
    route = respx.get(f"{BASE}/control/status").mock(return_value=httpx.Response(200, json={}))
    c = AdGuardClient(BASE + "/")
    try:
        await c.status()
    finally:
        await c.aclose()
    assert "Authorization" not in route.calls.last.request.headers


@respx.mock
async def test_querylog_passes_paging_params(client):
    route = respx.get(f"{BASE}/control/querylog").mock(
        return_value=httpx.Response(200, json=fixture_payload())
    )

    payload = await client.querylog(older_than="2026-10-19T08:00:03+02:00", limit=100)

    assert payload["oldest"] == "2026-10-19T08:00:03+02:00"
    params = route.calls.last.request.url.params
    assert params["older_than"] == "2026-10-19T08:00:03+02:00"
    assert params["limit"] == "100"


@respx.mock
async def test_querylog_without_older_than_omits_param(client):
    route = respx.get(f"{BASE}/control/querylog").mock(return_value=httpx.Response(200, json={"data": []}))

    await client.querylog()

    params = route.calls.last.request.url.params
    assert "older_than" not in params
    assert params["limit"] == "500"


def mock_rules(rules: list[str]):
    respx.get(f"{BASE}/control/filtering/status").mock(
        return_value=httpx.Response(200, json={"enabled": True, "user_rules": rules})
    )
    return respx.post(f"{BASE}/control/filtering/set_rules").mock(return_value=httpx.Response(200))


def posted_rules(route) -> list[str]:
    return json.loads(route.calls.last.request.content)["rules"]


@respx.mock
async def test_block_domain_adds_rule_and_keeps_existing(client):
    set_rules = mock_rules(["@@||good.com^", "! comment"])

    await client.block_domain("evil.com")

    assert posted_rules(set_rules) == ["@@||good.com^", "! comment", "||evil.com^"]


@respx.mock
async def test_block_domain_already_present_does_not_post(client):
    set_rules = mock_rules(["||evil.com^"])

    await client.block_domain("evil.com")

    assert not set_rules.called


@respx.mock
async def test_unblock_domain_removes_rule(client):
    set_rules = mock_rules(["||evil.com^", "||other.com^"])

    await client.unblock_domain("evil.com")

    assert posted_rules(set_rules) == ["||other.com^"]


@respx.mock
async def test_block_and_unblock_client(client):
    set_rules = mock_rules(["||other.com^"])
    await client.block_client("192.168.0.40")
    assert posted_rules(set_rules) == ["||other.com^", "||*^$client=192.168.0.40"]

    mock_rules(["||*^$client=192.168.0.40", "||other.com^"])
    await client.unblock_client("192.168.0.40")
    assert posted_rules(set_rules) == ["||other.com^"]


@respx.mock
async def test_blocked_rules_returns_user_rules(client):
    mock_rules(["||evil.com^"])
    assert await client.blocked_rules() == ["||evil.com^"]


@respx.mock
async def test_blocked_rules_handles_null_user_rules(client):
    respx.get(f"{BASE}/control/filtering/status").mock(
        return_value=httpx.Response(200, json={"user_rules": None})
    )
    assert await client.blocked_rules() == []


@respx.mock
async def test_non_2xx_raises_adguard_error(client):
    respx.get(f"{BASE}/control/status").mock(return_value=httpx.Response(401, text="unauthorized"))

    with pytest.raises(AdGuardError, match="401"):
        await client.status()


@respx.mock
async def test_set_rules_non_2xx_raises_adguard_error(client):
    respx.get(f"{BASE}/control/filtering/status").mock(
        return_value=httpx.Response(200, json={"user_rules": []})
    )
    respx.post(f"{BASE}/control/filtering/set_rules").mock(return_value=httpx.Response(500))

    with pytest.raises(AdGuardError, match="500"):
        await client.block_domain("evil.com")


@respx.mock
async def test_connection_error_raises_adguard_error(client):
    respx.get(f"{BASE}/control/status").mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(AdGuardError, match="connection refused"):
        await client.status()


@respx.mock
async def test_invalid_json_raises_adguard_error(client):
    respx.get(f"{BASE}/control/status").mock(return_value=httpx.Response(200, text="<html>"))

    with pytest.raises(AdGuardError):
        await client.status()


@respx.mock
async def test_injected_http_client_is_not_closed():
    respx.get(f"{BASE}/control/status").mock(return_value=httpx.Response(200, json={}))
    async with httpx.AsyncClient() as http:
        c = AdGuardClient(BASE, http=http)
        await c.status()
        await c.aclose()
        assert not http.is_closed
