from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from homesense.core.clock import Clock
from homesense.core.models import DnsQueryRecord
from homesense.core.timeutil import ts_from_db, ts_to_db
from homesense.network.adguard import AdGuardClient, parse_querylog
from homesense.storage.repositories.settings import SettingsRepo

CURSOR_KEY = "adguard.cursor"


def _whole_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


class AdGuardDnsCollector:
    """Pulls new DNS queries from the AdGuard Home query log, paging backwards to the stored cursor.

    The cursor is stored with whole-second precision (DB format), so queries are compared at
    whole seconds: anything in the cursor's second is treated as already seen. This avoids
    re-ingesting the newest query of the previous run at the cost of possibly skipping a
    query logged later within that same second.

    AdGuard keeps logging while monitoring is paused; queries older than `not_before()` (the end of
    the last monitoring pause) are skipped, so the backlog of the pause is never ingested.
    """

    name = "adguard"

    def __init__(
        self,
        client: AdGuardClient,
        settings_repo: SettingsRepo,
        clock: Clock,
        interval_s: float = 30,
        max_pages: int = 20,
        not_before: Callable[[], datetime | None] | None = None,
    ) -> None:
        self.interval_s = interval_s
        self._client = client
        self._settings = settings_repo
        self._clock = clock
        self._max_pages = max_pages
        self._not_before = not_before or (lambda: None)

    def _is_new(self, record: DnsQueryRecord, cursor: datetime | None, window_start: datetime) -> bool:
        if cursor is not None:
            return _whole_seconds(record.ts) > cursor
        return record.ts >= window_start

    async def collect(self) -> list[DnsQueryRecord]:
        raw_cursor = self._settings.get(CURSOR_KEY)
        cursor = ts_from_db(raw_cursor) if raw_cursor else None
        window_start = self._clock.now() - timedelta(seconds=self.interval_s * 2)

        new: list[DnsQueryRecord] = []
        older_than: str | None = None
        for _ in range(self._max_pages):
            payload = await self._client.querylog(older_than=older_than)
            records = parse_querylog(payload)
            if not records:
                break
            reached_seen = False
            for record in records:
                if self._is_new(record, cursor, window_start):
                    new.append(record)
                else:
                    reached_seen = True
            older_than = payload.get("oldest")
            if reached_seen or not older_than:
                break

        new.sort(key=lambda r: r.ts)
        if new:
            self._settings.set(CURSOR_KEY, ts_to_db(new[-1].ts))
        resumed = self._not_before()
        return new if resumed is None else [r for r in new if r.ts >= resumed]
