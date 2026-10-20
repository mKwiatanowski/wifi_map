from __future__ import annotations

from sqlalchemy import text

from homesense.core.models import Person
from homesense.core.timeutil import ts_from_db, ts_to_db
from homesense.storage.repositories._base import Repo, check_fields

_UPDATABLE = frozenset({"name", "color", "privacy_mode"})
_COLS = "id, name, color, privacy_mode, created_at"


def _row(r) -> Person:
    return Person(
        id=r.id, name=r.name, color=r.color, privacy_mode=r.privacy_mode, created_at=ts_from_db(r.created_at)
    )


class PersonRepo(Repo):
    def create(self, name: str, color: str | None = None, privacy_mode: str = "full") -> int:
        with self.db.begin() as c:
            res = c.execute(
                text("INSERT INTO persons(name, color, privacy_mode, created_at) VALUES (:n, :c, :p, :t)"),
                {"n": name, "c": color, "p": privacy_mode, "t": ts_to_db(self.clock.now())},
            )
            return res.lastrowid

    def get(self, id: int) -> Person | None:
        with self.db.connect() as c:
            r = c.execute(text(f"SELECT {_COLS} FROM persons WHERE id = :id"), {"id": id}).first()
        return _row(r) if r else None

    def list(self) -> list[Person]:
        with self.db.connect() as c:
            return [_row(r) for r in c.execute(text(f"SELECT {_COLS} FROM persons ORDER BY name, id"))]

    def update(self, id: int, /, **fields) -> None:
        check_fields(fields, _UPDATABLE)
        if not fields:
            return
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        with self.db.begin() as c:
            c.execute(text(f"UPDATE persons SET {sets} WHERE id = :id"), {**fields, "id": id})

    def delete(self, id: int) -> None:
        with self.db.begin() as c:
            c.execute(text("DELETE FROM persons WHERE id = :id"), {"id": id})
