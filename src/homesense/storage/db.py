from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy
from sqlalchemy import Connection, event

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BUSY_TIMEOUT_MS = 5000
_MIGRATION_NAME = re.compile(r"(\d{4})_[\w-]+\.sql")


def _set_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        # NORMAL is safe with WAL (no corruption; only the last commits may roll back on power loss)
        # and avoids an fsync per commit, which dominates ingest time on Windows.
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cur.execute("PRAGMA foreign_keys=ON")
    finally:
        cur.close()


def _migrations(directory: Path) -> list[tuple[int, Path]]:
    found = []
    for p in directory.glob("*.sql"):
        m = _MIGRATION_NAME.fullmatch(p.name)
        if m:
            found.append((int(m.group(1)), p))
    return sorted(found)


class Database:
    def __init__(self, path: Path, migrations_dir: Path = MIGRATIONS_DIR) -> None:
        self.path = Path(path)
        self.migrations_dir = Path(migrations_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: sqlalchemy.Engine = sqlalchemy.create_engine(
            f"sqlite:///{self.path.as_posix()}", connect_args={"check_same_thread": False}
        )
        event.listen(self.engine, "connect", _set_pragmas)

    def migrate(self, before: Callable[[int], None] | None = None) -> int:
        """Apply pending migrations in order; return the current schema version.

        `before(version)` runs (outside the migration's transaction) right before each pending
        migration is applied, e.g. to move data elsewhere first.
        """
        with self.begin() as c:
            c.exec_driver_sql("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        current = self._current_version()
        for version, path in _migrations(self.migrations_dir):
            if version <= current:
                continue
            if before is not None:
                before(version)
            script = path.read_text(encoding="utf-8")
            raw = self.engine.raw_connection()
            try:
                # executescript commits implicitly, so wrap the script in an explicit transaction
                # to apply each migration atomically together with its schema_version row.
                raw.driver_connection.executescript(
                    f"BEGIN;\n{script}\nINSERT INTO schema_version(version) VALUES ({version});\nCOMMIT;"
                )
            except Exception:
                raw.driver_connection.rollback()
                raise
            finally:
                raw.close()
            current = version
        return current

    def has_table(self, name: str) -> bool:
        with self.connect() as c:
            return (
                c.exec_driver_sql(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
                ).first()
                is not None
            )

    def _current_version(self) -> int:
        with self.connect() as c:
            return c.exec_driver_sql("SELECT COALESCE(MAX(version), 0) FROM schema_version").scalar_one()

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with self.engine.connect() as conn:
            yield conn

    def close(self) -> None:
        self.engine.dispose()
