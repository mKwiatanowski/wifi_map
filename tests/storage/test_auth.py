"""auth.db and the move of login accounts out of homesense.db (main migration 0002)."""

import shutil
import sqlite3
import sys

import pytest

from homesense.security import hash_password, verify_password
from homesense.storage.auth import (
    migrate_databases,
    open_auth_db,
    open_settings_auth_db,
    relocate_legacy_auth_db,
)
from homesense.storage.db import MIGRATIONS_DIR, Database
from homesense.storage.repositories import UserRepo

LATEST_VERSION = max(int(p.name[:4]) for p in MIGRATIONS_DIR.glob("*.sql"))


@pytest.fixture
def v1_main(tmp_path):
    """homesense.db at schema version 1 (users still inside), with one account."""
    v1_dir = tmp_path / "v1_migrations"
    v1_dir.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_initial.sql", v1_dir)
    db = Database(tmp_path / "homesense.db", v1_dir)
    assert db.migrate() == 1
    with db.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
            ("admin", hash_password("stare-haslo"), "2026-10-01 10:00:00"),
        )
    db.close()
    return tmp_path / "homesense.db"


def _main_users(path) -> list[tuple]:
    db = Database(path)
    try:
        if not db.has_table("users"):
            return []
        with db.connect() as c:
            return c.exec_driver_sql("SELECT username FROM users").all()
    finally:
        db.close()


def test_fresh_install_creates_both_databases(tmp_path):
    main, auth = Database(tmp_path / "homesense.db"), open_auth_db(tmp_path / "auth.db")
    assert migrate_databases(main, auth) == LATEST_VERSION
    assert not main.has_table("users")
    assert auth.has_table("users")
    assert UserRepo(auth).count() == 0
    assert migrate_databases(main, auth) == LATEST_VERSION  # idempotent
    main.close()
    auth.close()


def test_upgrade_moves_existing_users_to_auth_db(tmp_path, v1_main):
    main, auth = Database(v1_main), open_auth_db(tmp_path / "auth.db")
    assert migrate_databases(main, auth) == LATEST_VERSION
    assert not main.has_table("users")
    row = UserRepo(auth).get_by_username("admin")
    assert row is not None and verify_password("stare-haslo", row[2])
    with auth.connect() as c:
        assert c.exec_driver_sql("SELECT created_at FROM users").scalar_one() == "2026-10-01 10:00:00"
    main.close()
    auth.close()


def test_upgrade_resumes_after_interrupted_copy(tmp_path, v1_main):
    """auth.db already got the row (crash before the drop): no duplicate, no loss."""
    auth = open_auth_db(tmp_path / "auth.db")
    auth.migrate()
    UserRepo(auth).create("admin", "already-copied-hash")
    main = Database(v1_main)
    migrate_databases(main, auth)
    assert UserRepo(auth).count() == 1
    assert UserRepo(auth).get_by_username("admin")[2] == "already-copied-hash"
    assert not main.has_table("users")
    main.close()
    auth.close()


def test_plain_main_migrate_never_drops_unmoved_users(v1_main):
    """Without the move (Database.migrate alone) migration 0002 aborts instead of losing accounts."""
    main = Database(v1_main)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        main.migrate()
    main.close()
    assert _main_users(v1_main) == [("admin",)]
    main = Database(v1_main)
    with main.connect() as c:
        assert c.exec_driver_sql("SELECT MAX(version) FROM schema_version").scalar_one() == 1
    main.close()


def _raw_bytes(db_path) -> bytes:
    """homesense.db plus its -wal file (if present), as stored on disk."""
    data = db_path.read_bytes()
    wal = db_path.with_name(db_path.name + "-wal")
    return data + (wal.read_bytes() if wal.exists() else b"")


def test_upgrade_leaves_no_password_hash_bytes_in_main_db(tmp_path, v1_main):
    """Ruling R33: freed pages and WAL frames of homesense.db must not keep the moved hashes."""
    db = Database(v1_main)
    with db.connect() as c:
        stored = c.exec_driver_sql("SELECT password_hash FROM users").scalar_one()
    db.close()
    _algo, salt_hex, hash_hex = stored.split("$")
    assert stored.encode() in _raw_bytes(v1_main)  # the check below is meaningful

    main, auth = Database(v1_main), open_auth_db(tmp_path / "auth.db")
    migrate_databases(main, auth)
    # While the app keeps homesense.db open (no close-time checkpoint) ...
    raw = _raw_bytes(v1_main)
    for secret in (stored, salt_hex, hash_hex):
        assert secret.encode() not in raw
    main.close()
    # ... and after it closed.
    raw = _raw_bytes(v1_main)
    for secret in (stored, salt_hex, hash_hex):
        assert secret.encode() not in raw
    row = UserRepo(auth).get_by_username("admin")
    assert row is not None and verify_password("stare-haslo", row[2])
    auth.close()


def test_no_users_copy_left_in_main_db(tmp_path, v1_main):
    main, auth = Database(v1_main), open_auth_db(tmp_path / "auth.db")
    migrate_databases(main, auth)
    with main.connect() as c:
        names = [r[0] for r in c.exec_driver_sql("SELECT name FROM sqlite_master")]
        names += [r[0] for r in c.exec_driver_sql("SELECT name FROM sqlite_temp_master")]
        databases = {r[1] for r in c.exec_driver_sql("PRAGMA database_list")}
    assert not [n for n in names if "user" in n.lower()]
    assert databases <= {"main", "temp"}  # no persistent ATTACH alias
    main.close()
    auth.close()


def test_auth_db_path_is_absolute_under_data_dir(tmp_path, monkeypatch):
    from homesense.core.config import Settings

    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, mode="simulated", data_dir="relative-data")
    assert settings.auth_db_path.is_absolute()
    assert settings.auth_db_path == (tmp_path / "relative-data" / "auth" / "auth.db").resolve()
    auth = open_auth_db(tmp_path / "x" / ".." / "auth.db")
    assert auth.path.is_absolute() and auth.path == (tmp_path / "auth.db").resolve()
    auth.close()


def _scrub_flag(path) -> bool:
    db = Database(path)
    try:
        with db.connect() as c:
            return (
                c.exec_driver_sql("SELECT 1 FROM settings WHERE key = 'auth_scrub_pending'").first()
                is not None
            )
    finally:
        db.close()


def test_fresh_install_clears_scrub_flag(tmp_path):
    main, auth = Database(tmp_path / "homesense.db"), open_auth_db(tmp_path / "auth.db")
    migrate_databases(main, auth)
    main.close()
    auth.close()
    assert not _scrub_flag(tmp_path / "homesense.db")


def test_scrub_is_retried_after_crash(tmp_path, v1_main, monkeypatch):
    import homesense.storage.auth as auth_module

    def crash(_main):
        raise RuntimeError("power cut")

    monkeypatch.setattr(auth_module, "_scrub", crash)
    main, auth = Database(v1_main), open_auth_db(tmp_path / "auth.db")
    with pytest.raises(RuntimeError):
        migrate_databases(main, auth)
    main.close()
    assert _scrub_flag(v1_main)  # 0002 committed, scrub still pending

    monkeypatch.undo()
    main = Database(v1_main)
    migrate_databases(main, auth)
    main.close()
    assert not _scrub_flag(v1_main)
    assert UserRepo(auth).get_by_username("admin") is not None
    auth.close()


def test_blocked_checkpoint_keeps_scrub_pending(tmp_path, v1_main, monkeypatch):
    monkeypatch.setattr("homesense.storage.auth.SCRUB_BUSY_TIMEOUT_MS", 50)
    reader = sqlite3.connect(v1_main)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM persons").fetchall()  # holds a read snapshot
    main, auth = Database(v1_main), open_auth_db(tmp_path / "auth.db")
    try:
        migrate_databases(main, auth)
    finally:
        reader.rollback()
        reader.close()
    assert _scrub_flag(v1_main)
    migrate_databases(main, auth)  # next start: nothing blocks it any more
    main.close()
    auth.close()
    assert not _scrub_flag(v1_main)


# --- private auth folder (<data_dir>/auth) and the move from the old <data_dir>/auth.db layout ---


def _legacy_auth_db(data_dir, username="admin"):
    """An auth.db at the pre-folder location, with an account, its WAL left behind."""
    data_dir.mkdir(parents=True, exist_ok=True)
    auth = open_auth_db(data_dir / "auth.db")
    auth.migrate()
    UserRepo(auth).create(username, hash_password("haslo-123"))
    auth.close()
    return data_dir / "auth.db"


def _settings(data_dir):
    from homesense.core.config import Settings

    return Settings(_env_file=None, mode="simulated", data_dir=data_dir)


def test_open_auth_db_creates_private_folder(tmp_path):
    settings = _settings(tmp_path / "data")
    auth = open_settings_auth_db(settings)
    assert auth.path == settings.auth_db_path and settings.auth_dir.is_dir()
    if sys.platform != "win32":
        assert settings.auth_dir.stat().st_mode & 0o777 == 0o700
    auth.close()


def test_legacy_auth_db_is_moved_into_auth_folder(tmp_path):
    data_dir = tmp_path / "data"
    legacy = _legacy_auth_db(data_dir)
    (data_dir / "auth.db-wal").write_bytes(b"")  # side files move along (main file last)
    (data_dir / "auth.db-shm").write_bytes(b"")
    settings = _settings(data_dir)

    auth = open_settings_auth_db(settings)
    try:
        assert UserRepo(auth).get_by_username("admin") is not None
    finally:
        auth.close()
    assert not legacy.exists()
    assert not (data_dir / "auth.db-wal").exists() and not (data_dir / "auth.db-shm").exists()
    assert settings.auth_db_path.exists()


def test_legacy_auth_db_move_is_idempotent(tmp_path):
    data_dir = tmp_path / "data"
    _legacy_auth_db(data_dir)
    settings = _settings(data_dir)
    assert relocate_legacy_auth_db(data_dir / "auth.db", settings.auth_db_path) is True
    assert relocate_legacy_auth_db(data_dir / "auth.db", settings.auth_db_path) is False
    auth = open_settings_auth_db(settings)
    assert UserRepo(auth).count() == 1
    auth.close()


def test_legacy_move_finishes_after_partial_move(tmp_path):
    """Crash after the WAL was moved but before the main file: the next start moves the rest."""
    data_dir = tmp_path / "data"
    legacy = _legacy_auth_db(data_dir)
    settings = _settings(data_dir)
    settings.auth_dir.mkdir()
    (settings.auth_dir / "auth.db-wal").write_bytes(b"")
    assert relocate_legacy_auth_db(legacy, settings.auth_db_path) is True
    assert not legacy.exists() and settings.auth_db_path.exists()


def test_legacy_auth_db_never_overwrites_new_one(tmp_path, caplog):
    data_dir = tmp_path / "data"
    settings = _settings(data_dir)
    new = open_settings_auth_db(settings)
    new.migrate()
    UserRepo(new).create("nowy", hash_password("haslo-123"))
    new.close()
    legacy = _legacy_auth_db(data_dir, username="stary")

    assert relocate_legacy_auth_db(legacy, settings.auth_db_path) is False
    assert legacy.exists()
    assert any("auth.db" in r.getMessage() for r in caplog.records)
    auth = open_settings_auth_db(settings)
    assert UserRepo(auth).get_by_username("nowy") is not None
    assert UserRepo(auth).get_by_username("stary") is None
    auth.close()
