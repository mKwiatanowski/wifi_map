from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from homesense.core.timeutil import ts_to_db
from homesense.storage.repositories._base import Repo

ProfileValues = dict[tuple[int, int], tuple[float, int]]  # (weekday, hour) -> (value, samples)


class ProfileRepo(Repo):
    def replace(
        self, subject_type: str, subject_id: str, values: ProfileValues, updated_at: datetime
    ) -> None:
        key = {"st": subject_type, "sid": subject_id}
        rows = [
            {**key, "wd": wd, "h": h, "v": v, "n": n, "t": ts_to_db(updated_at)}
            for (wd, h), (v, n) in values.items()
        ]
        with self.db.begin() as c:
            c.execute(text("DELETE FROM profiles WHERE subject_type = :st AND subject_id = :sid"), key)
            if rows:
                c.execute(
                    text(
                        "INSERT INTO profiles"
                        "(subject_type, subject_id, weekday, hour, value, samples, updated_at) "
                        "VALUES (:st, :sid, :wd, :h, :v, :n, :t)"
                    ),
                    rows,
                )

    def get(self, subject_type: str, subject_id: str) -> ProfileValues:
        with self.db.connect() as c:
            rows = c.execute(
                text(
                    "SELECT weekday, hour, value, samples FROM profiles "
                    "WHERE subject_type = :st AND subject_id = :sid"
                ),
                {"st": subject_type, "sid": subject_id},
            )
            return {(r.weekday, r.hour): (r.value, r.samples) for r in rows}
