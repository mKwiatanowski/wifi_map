from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from homesense.storage.repositories._base import Repo


class SettingsRepo(Repo):
    def get(self, key: str, default: Any = None) -> Any:
        with self.db.connect() as c:
            raw = c.execute(text("SELECT value_json FROM settings WHERE key = :k"), {"k": key}).scalar()
        return default if raw is None else json.loads(raw)

    def set(self, key: str, value: Any) -> None:
        with self.db.begin() as c:
            c.execute(
                text(
                    "INSERT INTO settings(key, value_json) VALUES (:k, :v) "
                    "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json"
                ),
                {"k": key, "v": json.dumps(value)},
            )
