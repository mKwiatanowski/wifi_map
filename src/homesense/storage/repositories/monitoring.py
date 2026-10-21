from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from homesense.core.models import MonitoringPause
from homesense.core.timeutil import ts_from_db, ts_to_db
from homesense.storage.repositories._base import Repo, opt_from_db

_COLS = "id, start_ts, until_ts, end_ts"


def _row(r) -> MonitoringPause:
    return MonitoringPause(
        id=r.id,
        start_ts=ts_from_db(r.start_ts),
        until_ts=opt_from_db(r.until_ts),
        end_ts=opt_from_db(r.end_ts),
    )


class MonitoringRepo(Repo):
    """Monitoring pauses; at most one is open (end_ts NULL) at a time."""

    def open_pause(self) -> MonitoringPause | None:
        with self.db.connect() as c:
            r = c.execute(
                text(f"SELECT {_COLS} FROM monitoring_pauses WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1")
            ).first()
        return _row(r) if r else None

    def last_closed(self) -> MonitoringPause | None:
        with self.db.connect() as c:
            r = c.execute(
                text(
                    f"SELECT {_COLS} FROM monitoring_pauses WHERE end_ts IS NOT NULL "
                    "ORDER BY end_ts DESC, id DESC LIMIT 1"
                )
            ).first()
        return _row(r) if r else None

    def start(self, start: datetime, until: datetime | None) -> MonitoringPause:
        """Opens a pause; an already open one is replaced (its start is kept, the end changes)."""
        with self.db.begin() as c:
            r = c.execute(
                text(
                    "SELECT id, start_ts FROM monitoring_pauses WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1"
                )
            ).first()
            until_db = None if until is None else ts_to_db(until)
            if r is not None:
                c.execute(
                    text("UPDATE monitoring_pauses SET until_ts = :u WHERE id = :i"),
                    {"u": until_db, "i": r.id},
                )
                pause_id = r.id
            else:
                pause_id = c.execute(
                    text("INSERT INTO monitoring_pauses(start_ts, until_ts) VALUES (:s, :u)"),
                    {"s": ts_to_db(start), "u": until_db},
                ).lastrowid
            row = c.execute(
                text(f"SELECT {_COLS} FROM monitoring_pauses WHERE id = :i"), {"i": pause_id}
            ).first()
        return _row(row)

    def close(self, end: datetime) -> bool:
        with self.db.begin() as c:
            return (
                c.execute(
                    text("UPDATE monitoring_pauses SET end_ts = :e WHERE end_ts IS NULL"),
                    {"e": ts_to_db(end)},
                ).rowcount
                > 0
            )

    def delete_before(self, ts: datetime) -> int:
        """Closed pauses that ended before `ts` (retention)."""
        with self.db.begin() as c:
            return c.execute(
                text("DELETE FROM monitoring_pauses WHERE end_ts IS NOT NULL AND end_ts < :t"),
                {"t": ts_to_db(ts)},
            ).rowcount
