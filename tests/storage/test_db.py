from sqlalchemy import text

from homesense.storage.db import Database


def test_migrate_idempotent(tmp_path):
    db = Database(tmp_path / "x.db")
    assert db.migrate() == 2
    assert db.migrate() == 2
    db.close()


def test_creates_parent_dir(tmp_path):
    db = Database(tmp_path / "nested" / "dir" / "x.db")
    assert db.migrate() == 2
    assert (tmp_path / "nested" / "dir" / "x.db").exists()
    db.close()


def test_wal_enabled(db):
    with db.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


def test_pragmas(db):
    with db.connect() as c:
        assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert c.exec_driver_sql("PRAGMA synchronous").scalar() == 1  # NORMAL


def test_all_tables_created(db):
    expected = {
        "persons",
        "devices",
        "device_observations",
        "presence_events",
        "presence_intervals",
        "dns_hourly",
        "dns_hourly_stats",
        "domain_first_seen",
        "profiles",
        "alerts",
        "settings",
        "collector_status",
        "schema_version",
    }
    with db.connect() as c:
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert expected <= names
    assert "users" not in names  # login accounts live in auth.db (storage.auth)


def test_indexes_created(db):
    with db.connect() as c:
        rows = c.execute(
            text("SELECT tbl_name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
        ).all()
    idx = {(t, s.split("(", 1)[1].rstrip(")").replace(" ", "")) for t, s in rows}
    for item in [
        ("device_observations", "mac,ts"),
        ("device_observations", "ts"),
        ("presence_events", "person_id,ts"),
        ("alerts", "ts"),
        ("alerts", "dedup_key,ts"),
        ("dns_hourly", "hour_ts"),
    ]:
        assert item in idx


def test_begin_commits(db):
    with db.begin() as c:
        c.execute(text("INSERT INTO settings(key, value_json) VALUES ('a', '1')"))
    with db.connect() as c:
        assert c.execute(text("SELECT value_json FROM settings WHERE key='a'")).scalar() == "1"
