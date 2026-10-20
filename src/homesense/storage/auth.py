"""The auth database (auth.db): login accounts, kept apart from homesense.db.

Grafana reads homesense.db (read-only), so nothing secret may live there. Older installs had the
`users` table in homesense.db; `migrate_databases` moves those rows to auth.db before main migration
0002 drops the table (idempotent: a crash midway is finished on the next run, no row is lost).
Deletion and drop run with secure_delete, and once 0002 has run the main DB is checkpointed,
VACUUMed and checkpointed again (WAL truncated), so no password hash bytes stay in homesense.db or
its -wal file. No ATTACH is used: the two databases are separate connections.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from homesense.storage.db import BUSY_TIMEOUT_MS, Database

if TYPE_CHECKING:
    from homesense.core.config import Settings

log = logging.getLogger(__name__)

AUTH_MIGRATIONS_DIR = Path(__file__).parent / "auth_migrations"
USERS_MOVED_VERSION = 2  # main migration that drops homesense.db's `users` table
SCRUB_PENDING_KEY = "auth_scrub_pending"  # settings row written by migration 0002
SCRUB_BUSY_TIMEOUT_MS = 5000  # how long each checkpoint may wait for readers (e.g. Grafana)


AUTH_DIR_MODE = 0o700
LEGACY_AUTH_DB = "auth.db"  # older layout: <data_dir>/auth.db, before the private <data_dir>/auth/
_SQLITE_SIDE_FILES = ("-wal", "-shm", "-journal")


def open_auth_db(path: Path) -> Database:
    """The auth DB at the absolute, resolved `path` (never relative to a later CWD).

    A missing parent folder is created owner-only (0700 where the OS supports modes).
    """
    path = Path(path).resolve()
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=AUTH_DIR_MODE)
    return Database(path, AUTH_MIGRATIONS_DIR)


def relocate_legacy_auth_db(legacy: Path, target: Path) -> bool:
    """Move an auth.db (and its -wal/-shm/-journal) from `legacy` to `target`; True if anything moved.

    Side files move first and the main file last, so a crash midway is finished on the next call.
    An existing `target` is never overwritten (the legacy file is left in place with a warning).
    Must run before any connection to either file is open.
    """
    legacy, target = Path(legacy), Path(target)
    if not legacy.exists():
        return False
    if target.exists():
        log.warning("Both %s and %s exist; keeping the latter, remove the old file manually", legacy, target)
        return False
    if not target.parent.exists():
        target.parent.mkdir(parents=True, mode=AUTH_DIR_MODE)
    for suffix in _SQLITE_SIDE_FILES:
        side = legacy.with_name(legacy.name + suffix)
        if side.exists():
            os.replace(side, target.with_name(target.name + suffix))
    os.replace(legacy, target)
    log.info("Moved login database %s to %s", legacy, target)
    return True


def open_settings_auth_db(settings: Settings) -> Database:
    """The auth DB of `settings`, after moving one from the old <data_dir>/auth.db location."""
    relocate_legacy_auth_db(settings.data_dir / LEGACY_AUTH_DB, settings.auth_db_path)
    return open_auth_db(settings.auth_db_path)


def _move_users(main: Database, auth: Database) -> None:
    """Copy main.users rows missing in auth.db, then delete the rows auth.db now has."""
    if not main.has_table("users"):
        return
    with main.connect() as c:
        rows = c.exec_driver_sql("SELECT username, password_hash, created_at FROM users").all()
    if not rows:
        return
    with auth.begin() as c:
        for username, password_hash, created_at in rows:
            c.exec_driver_sql(
                "INSERT INTO users(username, password_hash, created_at) SELECT ?, ?, ? "
                "WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = ?)",
                (username, password_hash, created_at, username),
            )
    with auth.connect() as c:
        moved = [r[0] for r in c.exec_driver_sql("SELECT username FROM users").all()]
    with main.begin() as c:
        c.exec_driver_sql("PRAGMA secure_delete = ON")
        for username in moved:
            c.exec_driver_sql("DELETE FROM users WHERE username = ?", (username,))


def _scrub(main: Database) -> bool:
    """Rewrite homesense.db without free pages and empty its WAL (old row images live in both).

    True only if both checkpoints completed (no other connection blocked them).
    """
    raw = main.engine.raw_connection()
    complete = True
    try:
        con = raw.driver_connection
        con.commit()  # VACUUM must run outside a transaction
        con.execute(f"PRAGMA busy_timeout = {int(SCRUB_BUSY_TIMEOUT_MS)}")
        for statement in ("PRAGMA wal_checkpoint(TRUNCATE)", "VACUUM", "PRAGMA wal_checkpoint(TRUNCATE)"):
            row = con.execute(statement).fetchone()
            if row is not None and row[0] != 0:
                complete = False
                log.warning("%s after moving users was blocked by another connection: %s", statement, row)
    finally:
        raw.driver_connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")  # back to the pool default
        raw.close()
    return complete


def _scrub_pending(main: Database) -> bool:
    with main.connect() as c:
        return (
            c.exec_driver_sql("SELECT 1 FROM settings WHERE key = ?", (SCRUB_PENDING_KEY,)).first()
            is not None
        )


def migrate_databases(main: Database, auth: Database) -> int:
    """Migrate auth.db, then homesense.db (moving old accounts first); returns the main version.

    Migration 0002 leaves a persisted `auth_scrub_pending` flag; it is cleared only after a complete
    scrub, so a crash (or a blocked checkpoint) between 0002 and the scrub is retried on next start.
    """
    auth.migrate()

    def before(version: int) -> None:
        if version == USERS_MOVED_VERSION:
            _move_users(main, auth)

    version = main.migrate(before=before)
    if _scrub_pending(main) and _scrub(main):
        with main.begin() as c:
            c.exec_driver_sql("DELETE FROM settings WHERE key = ?", (SCRUB_PENDING_KEY,))
    return version
