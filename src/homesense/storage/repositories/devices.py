from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from homesense.core.models import Device, normalize_mac
from homesense.core.timeutil import ts_from_db, ts_to_db
from homesense.storage.repositories._base import Repo, check_fields

_UPDATABLE = frozenset({"person_id", "label", "trusted", "counts_for_presence", "kind", "notes"})
_BOOLS = frozenset({"trusted", "counts_for_presence"})
_COLS = (
    "mac, person_id, label, hostname, vendor, ip, first_seen, last_seen, trusted, "
    "counts_for_presence, kind, notes"
)


def _row(r) -> Device:
    return Device(
        mac=r.mac,
        person_id=r.person_id,
        label=r.label,
        hostname=r.hostname,
        vendor=r.vendor,
        ip=r.ip,
        first_seen=ts_from_db(r.first_seen),
        last_seen=ts_from_db(r.last_seen),
        trusted=bool(r.trusted),
        counts_for_presence=bool(r.counts_for_presence),
        kind=r.kind,
        notes=r.notes,
    )


class DeviceRepo(Repo):
    def get(self, mac: str) -> Device | None:
        with self.db.connect() as c:
            r = c.execute(
                text(f"SELECT {_COLS} FROM devices WHERE mac = :m"), {"m": normalize_mac(mac)}
            ).first()
        return _row(r) if r else None

    def list(self) -> list[Device]:
        with self.db.connect() as c:
            return [_row(r) for r in c.execute(text(f"SELECT {_COLS} FROM devices ORDER BY mac"))]

    def upsert_seen(
        self,
        mac: str,
        ts: datetime,
        ip: str | None = None,
        hostname: str | None = None,
        vendor: str | None = None,
    ) -> bool:
        """Record a sighting. Returns True if the device was newly created."""
        p = {"m": normalize_mac(mac), "ts": ts_to_db(ts), "ip": ip, "h": hostname, "v": vendor}
        with self.db.begin() as c:
            created = (
                c.execute(
                    text(
                        "INSERT OR IGNORE INTO devices(mac, hostname, vendor, ip, first_seen, last_seen) "
                        "VALUES (:m, :h, :v, :ip, :ts, :ts)"
                    ),
                    p,
                ).rowcount
                == 1
            )
            if not created:
                c.execute(
                    text(
                        "UPDATE devices SET ip = COALESCE(:ip, ip), hostname = COALESCE(:h, hostname), "
                        "vendor = COALESCE(:v, vendor), "
                        "last_seen = CASE WHEN :ts > last_seen THEN :ts ELSE last_seen END WHERE mac = :m"
                    ),
                    p,
                )
        return created

    def update(self, mac: str, /, **fields) -> None:
        check_fields(fields, _UPDATABLE)
        if not fields:
            return
        params = {k: (int(bool(v)) if k in _BOOLS else v) for k, v in fields.items()}
        sets = ", ".join(f"{k} = :{k}" for k in params)
        with self.db.begin() as c:
            c.execute(
                text(f"UPDATE devices SET {sets} WHERE mac = :mac"), {**params, "mac": normalize_mac(mac)}
            )

    def find_by_ip(self, ip: str) -> Device | None:
        with self.db.connect() as c:
            r = c.execute(
                text(f"SELECT {_COLS} FROM devices WHERE ip = :ip ORDER BY last_seen DESC LIMIT 1"),
                {"ip": ip},
            ).first()
        return _row(r) if r else None

    def for_person(self, person_id: int) -> list[Device]:
        with self.db.connect() as c:
            rows = c.execute(
                text(f"SELECT {_COLS} FROM devices WHERE person_id = :p ORDER BY mac"), {"p": person_id}
            )
            return [_row(r) for r in rows]

    def seen_since(self, ts: datetime) -> list[Device]:
        with self.db.connect() as c:
            rows = c.execute(
                text(f"SELECT {_COLS} FROM devices WHERE last_seen >= :ts ORDER BY mac"), {"ts": ts_to_db(ts)}
            )
            return [_row(r) for r in rows]
