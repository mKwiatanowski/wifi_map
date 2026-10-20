from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import text

from homesense.core.models import Alert
from homesense.core.timeutil import ts_from_db, ts_to_db
from homesense.storage.repositories._base import Repo

_COLS = "id, ts, kind, severity, person_id, mac, title, message, data_json, acknowledged, dedup_key"


def _row(r) -> Alert:
    return Alert(
        id=r.id,
        ts=ts_from_db(r.ts),
        kind=r.kind,
        severity=r.severity,
        person_id=r.person_id,
        mac=r.mac,
        title=r.title,
        message=r.message,
        data=json.loads(r.data_json),
        acknowledged=bool(r.acknowledged),
        dedup_key=r.dedup_key,
    )


class AlertRepo(Repo):
    def add(self, a: Alert) -> int:
        with self.db.begin() as c:
            return c.execute(
                text(
                    "INSERT INTO alerts(ts, kind, severity, person_id, mac, title, message, data_json, "
                    "acknowledged, dedup_key) "
                    "VALUES (:ts, :kind, :sev, :pid, :mac, :title, :msg, :data, :ack, :key)"
                ),
                {
                    "ts": ts_to_db(a.ts),
                    "kind": a.kind,
                    "sev": a.severity,
                    "pid": a.person_id,
                    "mac": a.mac,
                    "title": a.title,
                    "msg": a.message,
                    "data": json.dumps(a.data),
                    "ack": int(a.acknowledged),
                    "key": a.dedup_key,
                },
            ).lastrowid

    def exists_recent(self, dedup_key: str, since: datetime) -> bool:
        with self.db.connect() as c:
            return (
                c.execute(
                    text("SELECT 1 FROM alerts WHERE dedup_key = :k AND ts >= :s LIMIT 1"),
                    {"k": dedup_key, "s": ts_to_db(since)},
                ).first()
                is not None
            )

    def list(self, limit: int = 100, unacknowledged_only: bool = False) -> list[Alert]:
        where = "WHERE acknowledged = 0" if unacknowledged_only else ""
        with self.db.connect() as c:
            rows = c.execute(
                text(f"SELECT {_COLS} FROM alerts {where} ORDER BY ts DESC, id DESC LIMIT :l"), {"l": limit}
            )
            return [_row(r) for r in rows]

    def acknowledge(self, id: int) -> bool:
        """Returns False when no alert has this id."""
        with self.db.begin() as c:
            return (
                c.execute(text("UPDATE alerts SET acknowledged = 1 WHERE id = :id"), {"id": id}).rowcount == 1
            )

    def acknowledge_all(self) -> None:
        with self.db.begin() as c:
            c.execute(text("UPDATE alerts SET acknowledged = 1 WHERE acknowledged = 0"))

    def count_unacknowledged(self) -> int:
        with self.db.connect() as c:
            return c.execute(text("SELECT COUNT(*) FROM alerts WHERE acknowledged = 0")).scalar_one()

    def delete_before(self, ts: datetime) -> int:
        with self.db.begin() as c:
            return c.execute(text("DELETE FROM alerts WHERE ts < :t"), {"t": ts_to_db(ts)}).rowcount
