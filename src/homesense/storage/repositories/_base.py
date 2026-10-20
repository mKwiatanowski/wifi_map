from __future__ import annotations

from datetime import datetime

from homesense.core.clock import Clock, SystemClock
from homesense.core.timeutil import ts_from_db
from homesense.storage.db import Database


def opt_from_db(s: str | None) -> datetime | None:
    return None if s is None else ts_from_db(s)


def check_fields(fields: dict, allowed: frozenset[str]) -> None:
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"fields not updatable: {sorted(bad)}")


class Repo:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock = clock or SystemClock()
