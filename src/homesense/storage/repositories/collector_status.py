from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from homesense.core.models import CollectorStatus
from homesense.core.timeutil import ts_to_db
from homesense.storage.repositories._base import Repo, opt_from_db

_COLS = "name, last_ok_ts, last_error_ts, last_error, consecutive_failures"


def _row(r) -> CollectorStatus:
    return CollectorStatus(
        name=r.name,
        last_ok_ts=opt_from_db(r.last_ok_ts),
        last_error_ts=opt_from_db(r.last_error_ts),
        last_error=r.last_error,
        consecutive_failures=r.consecutive_failures,
    )


class CollectorStatusRepo(Repo):
    def record_ok(self, name: str, ts: datetime) -> None:
        with self.db.begin() as c:
            c.execute(
                text(
                    "INSERT INTO collector_status(name, last_ok_ts, consecutive_failures) "
                    "VALUES (:n, :t, 0) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "last_ok_ts = excluded.last_ok_ts, consecutive_failures = 0"
                ),
                {"n": name, "t": ts_to_db(ts)},
            )

    def record_error(self, name: str, ts: datetime, error: str) -> None:
        with self.db.begin() as c:
            c.execute(
                text(
                    "INSERT INTO collector_status(name, last_error_ts, last_error, consecutive_failures) "
                    "VALUES (:n, :t, :e, 1) ON CONFLICT(name) DO UPDATE SET "
                    "last_error_ts = excluded.last_error_ts, last_error = excluded.last_error, "
                    "consecutive_failures = consecutive_failures + 1"
                ),
                {"n": name, "t": ts_to_db(ts), "e": error},
            )

    def all(self) -> list[CollectorStatus]:
        with self.db.connect() as c:
            return [_row(r) for r in c.execute(text(f"SELECT {_COLS} FROM collector_status ORDER BY name"))]

    def get(self, name: str) -> CollectorStatus | None:
        with self.db.connect() as c:
            r = c.execute(text(f"SELECT {_COLS} FROM collector_status WHERE name = :n"), {"n": name}).first()
        return _row(r) if r else None
