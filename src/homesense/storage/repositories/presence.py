from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from homesense.core.models import PresenceEvent, PresenceInterval
from homesense.core.timeutil import ts_from_db, ts_to_db
from homesense.storage.repositories._base import Repo, opt_from_db


def _interval(r) -> PresenceInterval:
    return PresenceInterval(
        person_id=r.person_id, start_ts=ts_from_db(r.start_ts), end_ts=opt_from_db(r.end_ts)
    )


class PresenceRepo(Repo):
    """Time windows are half-open: since <= ts < until."""

    def add_event(self, e: PresenceEvent) -> int:
        with self.db.begin() as c:
            return c.execute(
                text("INSERT INTO presence_events(ts, person_id, kind, mac) VALUES (:ts, :p, :k, :m)"),
                {"ts": ts_to_db(e.ts), "p": e.person_id, "k": e.kind, "m": e.mac},
            ).lastrowid

    def events(
        self, person_id: int | None = None, since: datetime | None = None, until: datetime | None = None
    ) -> list[PresenceEvent]:
        where, p = [], {}
        if person_id is not None:
            where.append("person_id = :p")
            p["p"] = person_id
        if since is not None:
            where.append("ts >= :s")
            p["s"] = ts_to_db(since)
        if until is not None:
            where.append("ts < :u")
            p["u"] = ts_to_db(until)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self.db.connect() as c:
            rows = c.execute(
                text(f"SELECT ts, person_id, kind, mac FROM presence_events {clause} ORDER BY ts, id"), p
            )
            return [
                PresenceEvent(ts=ts_from_db(r.ts), person_id=r.person_id, kind=r.kind, mac=r.mac)
                for r in rows
            ]

    def first_event_ts(self, person_id: int) -> datetime | None:
        with self.db.connect() as c:
            ts = c.execute(
                text("SELECT MIN(ts) FROM presence_events WHERE person_id = :p"), {"p": person_id}
            ).scalar()
        return opt_from_db(ts)

    def open_interval(self, person_id: int) -> PresenceInterval | None:
        with self.db.connect() as c:
            r = c.execute(
                text(
                    "SELECT person_id, start_ts, end_ts FROM presence_intervals "
                    "WHERE person_id = :p AND end_ts IS NULL ORDER BY start_ts DESC LIMIT 1"
                ),
                {"p": person_id},
            ).first()
        return _interval(r) if r else None

    def start_interval(self, person_id: int, ts: datetime) -> None:
        """Open a new interval; no-op if one is already open for this person."""
        with self.db.begin() as c:
            c.execute(
                text(
                    "INSERT INTO presence_intervals(person_id, start_ts) SELECT :p, :ts WHERE NOT EXISTS "
                    "(SELECT 1 FROM presence_intervals WHERE person_id = :p AND end_ts IS NULL)"
                ),
                {"p": person_id, "ts": ts_to_db(ts)},
            )

    def close_interval(self, person_id: int, ts: datetime) -> None:
        with self.db.begin() as c:
            c.execute(
                text("UPDATE presence_intervals SET end_ts = :ts WHERE person_id = :p AND end_ts IS NULL"),
                {"p": person_id, "ts": ts_to_db(ts)},
            )

    def intervals(self, person_id: int, since: datetime, until: datetime) -> list[PresenceInterval]:
        """Intervals overlapping [since, until); open intervals extend to infinity."""
        with self.db.connect() as c:
            rows = c.execute(
                text(
                    "SELECT person_id, start_ts, end_ts FROM presence_intervals WHERE person_id = :p "
                    "AND start_ts < :u AND (end_ts IS NULL OR end_ts > :s) ORDER BY start_ts, id"
                ),
                {"p": person_id, "s": ts_to_db(since), "u": ts_to_db(until)},
            )
            return [_interval(r) for r in rows]

    def delete_before(self, ts: datetime) -> int:
        """Delete events and closed intervals older than ts. Open intervals are kept."""
        t = {"t": ts_to_db(ts)}
        with self.db.begin() as c:
            n = c.execute(text("DELETE FROM presence_events WHERE ts < :t"), t).rowcount
            n += c.execute(
                text("DELETE FROM presence_intervals WHERE end_ts IS NOT NULL AND end_ts < :t"), t
            ).rowcount
        return n
