from datetime import UTC, datetime

import httpx
import pytest
import respx

from homesense.collectors.adguard_dns import CURSOR_KEY, AdGuardDnsCollector
from homesense.network.adguard import AdGuardClient, AdGuardError

BASE = "http://adguard.test:3001"
QUERYLOG = f"{BASE}/control/querylog"


def item(time: str, domain: str, client: str = "192.168.0.23", reason: str = "NotFilteredNotFound") -> dict:
    return {"time": time, "client": client, "question": {"name": domain, "type": "A"}, "reason": reason}


def utc(h: int, m: int, s: int, us: int = 0) -> datetime:
    return datetime(2026, 10, 19, h, m, s, us, tzinfo=UTC)


class Pages:
    """Serves querylog pages keyed by the ``older_than`` param (None = newest page)."""

    def __init__(self, pages: dict[str | None, dict]) -> None:
        self.pages = pages
        self.requested: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        older_than = request.url.params.get("older_than")
        self.requested.append(older_than)
        return httpx.Response(200, json=self.pages.get(older_than, {"data": [], "oldest": ""}))


@pytest.fixture
async def adguard():
    c = AdGuardClient(BASE, "admin", "secret")
    yield c
    await c.aclose()


@pytest.fixture
def collector(adguard, repos, clock):
    return AdGuardDnsCollector(adguard, repos.settings, clock, interval_s=30)


@respx.mock
async def test_first_run_takes_only_recent_window_and_stores_cursor(collector, repos, clock):
    clock.set(utc(6, 0, 30))  # window = interval_s * 2 = 60 s -> from 05:59:30
    pages = Pages(
        {
            None: {
                "data": [
                    item("2026-10-19T08:00:05.123+02:00", "api.spotify.com"),
                    item("2026-10-19T08:00:03+02:00", "ads.example.", "192.168.0.40", "FilteredBlackList"),
                    item("2026-10-19T07:58:00+02:00", "old.example"),
                ],
                "oldest": "2026-10-19T07:58:00+02:00",
            },
            "2026-10-19T07:58:00+02:00": {"data": [item("2026-10-19T07:57:00+02:00", "older.example")]},
        }
    )
    respx.get(QUERYLOG).mock(side_effect=pages)

    records = await collector.collect()

    assert [(r.ts, r.domain, r.blocked) for r in records] == [
        (utc(6, 0, 3), "ads.example", True),
        (utc(6, 0, 5, 123000), "api.spotify.com", False),
    ]
    assert pages.requested == [None]  # stopped once an item fell outside the window
    assert repos.settings.get(CURSOR_KEY) == "2026-10-19 06:00:05"
    assert collector.name == "adguard"
    assert collector.interval_s == 30


@respx.mock
async def test_second_run_returns_only_newer_records_and_advances_cursor(collector, repos, clock):
    repos.settings.set(CURSOR_KEY, "2026-10-19 06:00:05")
    clock.set(utc(6, 1, 0))
    respx.get(QUERYLOG).mock(
        side_effect=Pages(
            {
                None: {
                    "data": [
                        item("2026-10-19T08:00:40+02:00", "b.example"),
                        item("2026-10-19T08:00:20+02:00", "a.example"),
                        item("2026-10-19T08:00:05.123+02:00", "api.spotify.com"),
                        item("2026-10-19T08:00:03+02:00", "ads.example."),
                    ],
                    "oldest": "2026-10-19T08:00:03+02:00",
                }
            }
        )
    )

    records = await collector.collect()

    assert [(r.ts, r.domain) for r in records] == [(utc(6, 0, 20), "a.example"), (utc(6, 0, 40), "b.example")]
    assert repos.settings.get(CURSOR_KEY) == "2026-10-19 06:00:40"


@respx.mock
async def test_pages_backwards_until_cursor_reached(collector, repos):
    repos.settings.set(CURSOR_KEY, "2026-10-19 06:00:00")
    pages = Pages(
        {
            None: {
                "data": [item("2026-10-19T08:00:30+02:00", "c.example")],
                "oldest": "2026-10-19T08:00:30+02:00",
            },
            "2026-10-19T08:00:30+02:00": {
                "data": [item("2026-10-19T08:00:20+02:00", "b.example")],
                "oldest": "2026-10-19T08:00:20+02:00",
            },
            "2026-10-19T08:00:20+02:00": {
                "data": [
                    item("2026-10-19T08:00:10+02:00", "a.example"),
                    item("2026-10-19T07:59:59+02:00", "x.example"),
                ],
                "oldest": "2026-10-19T07:59:59+02:00",
            },
        }
    )
    respx.get(QUERYLOG).mock(side_effect=pages)

    records = await collector.collect()

    assert [r.domain for r in records] == ["a.example", "b.example", "c.example"]
    assert pages.requested == [None, "2026-10-19T08:00:30+02:00", "2026-10-19T08:00:20+02:00"]
    assert repos.settings.get(CURSOR_KEY) == "2026-10-19 06:00:30"


@respx.mock
async def test_paging_stops_at_max_pages(adguard, repos, clock):
    repos.settings.set(CURSOR_KEY, "2026-10-19 05:00:00")
    collector = AdGuardDnsCollector(adguard, repos.settings, clock, max_pages=3)
    calls: list[str | None] = []

    def endless(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("older_than"))
        n = len(calls)
        time = f"2026-10-19T08:00:{60 - n:02d}+02:00"
        return httpx.Response(200, json={"data": [item(time, f"d{n}.example")], "oldest": time})

    respx.get(QUERYLOG).mock(side_effect=endless)

    records = await collector.collect()

    assert len(calls) == 3
    assert [r.domain for r in records] == ["d3.example", "d2.example", "d1.example"]


@respx.mock
async def test_empty_log_returns_nothing_and_keeps_cursor(collector, repos):
    repos.settings.set(CURSOR_KEY, "2026-10-19 06:00:05")
    respx.get(QUERYLOG).mock(return_value=httpx.Response(200, json={"data": [], "oldest": ""}))

    assert await collector.collect() == []
    assert repos.settings.get(CURSOR_KEY) == "2026-10-19 06:00:05"


@respx.mock
async def test_first_run_with_empty_log_stores_no_cursor(collector, repos):
    respx.get(QUERYLOG).mock(return_value=httpx.Response(200, json={"data": []}))

    assert await collector.collect() == []
    assert repos.settings.get(CURSOR_KEY) is None


@respx.mock
async def test_adguard_error_propagates_and_cursor_untouched(collector, repos):
    repos.settings.set(CURSOR_KEY, "2026-10-19 06:00:05")
    respx.get(QUERYLOG).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(AdGuardError):
        await collector.collect()
    assert repos.settings.get(CURSOR_KEY) == "2026-10-19 06:00:05"


@respx.mock
async def test_queries_logged_during_a_monitoring_pause_are_skipped(adguard, repos, clock):
    """AdGuard keeps logging while paused; only queries after the resume are ingested."""
    repos.settings.set(CURSOR_KEY, "2026-10-19 05:00:00")  # before the pause
    clock.set(utc(6, 1, 0))
    collector = AdGuardDnsCollector(
        adguard, repos.settings, clock, interval_s=30, not_before=lambda: utc(6, 0, 30)
    )
    respx.get(QUERYLOG).mock(
        side_effect=Pages(
            {
                None: {
                    "data": [
                        item("2026-10-19T08:00:40+02:00", "after.example"),
                        item("2026-10-19T07:30:00+02:00", "during-pause.example"),
                        item("2026-10-19T06:59:00+02:00", "seen.example"),
                    ],
                    "oldest": "2026-10-19T06:59:00+02:00",
                }
            }
        )
    )
    records = await collector.collect()
    assert [r.domain for r in records] == ["after.example"]
    assert repos.settings.get(CURSOR_KEY) == "2026-10-19 06:00:40"
