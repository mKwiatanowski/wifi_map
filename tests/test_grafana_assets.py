"""Grafana provisioning and dashboard assets (grafana/).

Every dashboard query is executed against a freshly migrated database so schema drift breaks a
test instead of a panel. Dashboards may only use the `$__from` / `$__to` global variables
(milliseconds since the epoch, as Grafana interpolates them); queries divide them by 1000.
"""

from __future__ import annotations

import configparser
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

GRAFANA_DIR = Path(__file__).resolve().parents[1] / "grafana"
DASHBOARDS_DIR = GRAFANA_DIR / "dashboards"
DATASOURCE_UID = "homesense-sqlite"
EXPECTED_UIDS = {
    "presence": "homesense-presence",
    "devices": "homesense-devices",
    "dns": "homesense-dns",
    "traffic": "homesense-traffic",
}
ALLOWED_VARIABLES = {"$__from", "$__to"}
_VARIABLE = re.compile(r"\$\{[^}]*\}|\$\w+|\[\[[^\]]*\]\]")

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
FROM_MS = int(datetime(2026, 10, 12, 12, 0, tzinfo=UTC).timestamp() * 1000)
TO_MS = int(NOW.timestamp() * 1000)

PRIVATE_DOMAIN = "secret-private.example"


def _dashboard(name: str) -> dict:
    return json.loads((DASHBOARDS_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _panels(panels: list[dict]):
    for panel in panels:
        yield panel
        yield from _panels(panel.get("panels", []))


def _targets():
    for name in EXPECTED_UIDS:
        for panel in _panels(_dashboard(name)["panels"]):
            for target in panel.get("targets", []):
                yield name, panel, target


def _sql(target: dict) -> str:
    sql = target["queryText"]
    return sql.replace("$__from", str(FROM_MS)).replace("$__to", str(TO_MS))


def _query(db, target: dict) -> tuple[list[str], list[tuple]]:
    with db.connect() as c:
        result = c.exec_driver_sql(_sql(target))
        return list(result.keys()), [tuple(r) for r in result]


@pytest.fixture
def seeded_db(db):
    """Migrated DB with a little of everything, including a stats_only person's leftover domains."""
    ts = "2026-10-18 10:00:00"
    statements = [
        "INSERT INTO persons(id, name, color, privacy_mode, created_at) VALUES "
        f"(1, 'Anna', '#ff0000', 'full', '{ts}'), (2, 'Piotr', NULL, 'stats_only', '{ts}')",
        "INSERT INTO devices(mac, person_id, label, hostname, vendor, ip, first_seen, last_seen, "
        "counts_for_presence) VALUES "
        f"('aa:aa:aa:aa:aa:01', 1, 'Telefon Anny', NULL, 'Apple', '192.168.0.10', '{ts}', '{ts}', 1), "
        f"('aa:aa:aa:aa:aa:02', 2, NULL, 'piotr-pc', 'Lenovo', '192.168.0.11', '{ts}', '{ts}', 1), "
        f"('aa:aa:aa:aa:aa:03', NULL, NULL, NULL, NULL, '192.168.0.12', '{ts}', '{ts}', 0)",
        "INSERT INTO device_observations(ts, mac, ip, source, online, up_kbps, down_kbps) VALUES "
        "('2026-10-18 10:00:00', 'aa:aa:aa:aa:aa:01', '192.168.0.10', 'sim', 1, 12.5, 230.0), "
        "('2026-10-18 10:01:00', 'aa:aa:aa:aa:aa:01', '192.168.0.10', 'sim', 1, 10.0, 200.0), "
        "('2026-10-18 10:00:00', 'aa:aa:aa:aa:aa:02', '192.168.0.11', 'sim', 1, 3.0, 40.0), "
        "('2026-10-18 10:02:00', 'aa:aa:aa:aa:aa:03', '192.168.0.12', 'sim', 0, NULL, NULL)",
        "INSERT INTO device_observations(ts, mac, ip, source, online, connection, signal) VALUES "
        "('2026-10-18 10:03:00', 'aa:aa:aa:aa:aa:01', '192.168.0.10', 'sim', 1, '5g', -55), "
        "('2026-10-18 10:04:00', 'aa:aa:aa:aa:aa:01', '192.168.0.10', 'sim', 1, '5g', -65)",
        "INSERT INTO presence_events(ts, person_id, kind, mac) VALUES "
        "('2026-10-17 07:00:00', 1, 'leave', 'aa:aa:aa:aa:aa:01'), "
        "('2026-10-17 16:00:00', 1, 'arrive', 'aa:aa:aa:aa:aa:01'), "
        "('2026-10-18 08:00:00', 2, 'arrive', 'aa:aa:aa:aa:aa:02')",
        "INSERT INTO presence_intervals(person_id, start_ts, end_ts) VALUES "
        "(1, '2026-10-10 18:00:00', '2026-10-17 07:00:00'), "
        "(1, '2026-10-17 16:00:00', NULL), "
        "(2, '2026-10-18 08:00:00', NULL)",
        "INSERT INTO dns_hourly(hour_ts, mac, domain, queries, blocked) VALUES "
        "('2026-10-18 10:00:00', 'aa:aa:aa:aa:aa:01', 'example.com', 40, 0), "
        "('2026-10-18 10:00:00', 'aa:aa:aa:aa:aa:01', 'ads.example', 10, 10), "
        "('2026-10-18 10:00:00', 'unknown', 'example.com', 5, 0), "
        # Leftover from before Piotr switched to stats_only: must never be shown.
        f"('2026-10-18 10:00:00', 'aa:aa:aa:aa:aa:02', '{PRIVATE_DOMAIN}', 999, 0)",
        "INSERT INTO dns_hourly_stats(hour_ts, mac, queries, blocked) VALUES "
        "('2026-10-18 10:00:00', 'aa:aa:aa:aa:aa:01', 50, 10), "
        "('2026-10-18 10:00:00', 'aa:aa:aa:aa:aa:02', 30, 0), "
        "('2026-10-18 10:00:00', 'unknown', 5, 0)",
        "INSERT INTO domain_first_seen(mac, domain, first_seen) VALUES "
        "('aa:aa:aa:aa:aa:01', 'example.com', '2026-10-18 10:00:00'), "
        f"('aa:aa:aa:aa:aa:02', '{PRIVATE_DOMAIN}', '2026-10-18 10:00:00')",
        "INSERT INTO profiles(subject_type, subject_id, weekday, hour, value, samples, updated_at) VALUES "
        f"('person_home', '1', 0, 7, 0.75, 4, '{ts}'), ('person_home', '1', 6, 23, 1.0, 4, '{ts}'), "
        f"('device_online', 'aa:aa:aa:aa:aa:01', 0, 7, 0.5, 4, '{ts}')",
    ]
    with db.begin() as c:
        for statement in statements:
            c.exec_driver_sql(statement)
    return db


# --- dashboards ---


@pytest.mark.parametrize(("name", "uid"), EXPECTED_UIDS.items())
def test_dashboard_metadata(name, uid):
    dash = _dashboard(name)
    assert dash["uid"] == uid
    assert dash["editable"] is False
    assert dash["timezone"] == "browser"
    assert dash["schemaVersion"] >= 39
    assert dash["time"]["from"] == ("now-7d" if name in ("presence", "devices") else "now-24h")
    assert dash["panels"], "dashboard has no panels"


def test_every_panel_and_target_uses_homesense_datasource():
    panels = [p for name in EXPECTED_UIDS for p in _panels(_dashboard(name)["panels"])]
    for panel in panels:
        if panel["type"] == "row":
            continue
        assert panel["datasource"]["uid"] == DATASOURCE_UID, panel["title"]
        assert panel["targets"], panel["title"]
        for target in panel["targets"]:
            assert target["datasource"] == {"type": "frser-sqlite-datasource", "uid": DATASOURCE_UID}


def test_targets_are_well_formed():
    for _name, panel, target in _targets():
        assert target["queryType"] in ("table", "time series"), panel["title"]
        assert target["rawQueryText"] == target["queryText"], panel["title"]
        variables = set(_VARIABLE.findall(target["queryText"]))
        assert variables <= ALLOWED_VARIABLES, (panel["title"], variables)


def test_panel_titles_are_polish_and_unique():
    titles = [p["title"] for name in EXPECTED_UIDS for p in _panels(_dashboard(name)["panels"])]
    assert len(titles) == len(set(titles))
    assert any(set(t) & set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ") for t in titles)


def test_dashboard_links_have_no_trailing_slash():
    for name in EXPECTED_UIDS:
        text = (DASHBOARDS_DIR / f"{name}.json").read_text(encoding="utf-8")
        for url in re.findall(r'"url"\s*:\s*"([^"]*)"', text):
            assert not url.endswith("/") and "//" not in url.split("://")[-1], url


def test_every_query_runs_on_empty_migrated_db(db):
    executed = 0
    for _name, _panel, target in _targets():
        _query(db, target)  # must not raise
        executed += 1
    assert executed >= 10


def test_every_query_returns_rows_on_seeded_db(seeded_db):
    for _name, panel, target in _targets():
        columns, rows = _query(seeded_db, target)
        assert rows, panel["title"]
        if "time" in columns:
            assert target["timeColumns"] == ["time"], panel["title"]
            assert all(isinstance(r[columns.index("time")], int) for r in rows), panel["title"]


def test_domain_panels_hide_stats_only_devices(seeded_db):
    domain_panels = 0
    for _name, panel, target in _targets():
        if not re.search(r"\b(dns_hourly|domain_first_seen)\b", target["queryText"]):
            continue
        domain_panels += 1
        _columns, rows = _query(seeded_db, target)
        assert rows, panel["title"]
        assert PRIVATE_DOMAIN not in repr(rows), panel["title"]
    assert domain_panels == 2  # top domains + new domains


def test_signal_by_hour_panel_averages_rssi(seeded_db):
    (target,) = [t for _n, p, t in _targets() if p["title"] == "Sygnał Wi-Fi wg godziny doby"]
    columns, rows = _query(seeded_db, target)
    row = dict(zip(columns, rows[0], strict=True))
    assert row["Urządzenie"] == "Telefon Anny" and row["Średni sygnał (dBm)"] == -60.0
    assert row["Najsłabszy (dBm)"] == -65 and row["Próbek"] == 2


def test_presence_timeline_marks_open_interval_as_home(seeded_db):
    target = next(t for _n, p, t in _targets() if p["type"] == "state-timeline")
    columns, rows = _query(seeded_db, target)
    assert columns == ["time", "person", "home"]
    anna = [(r[0], r[2]) for r in rows if r[1] == "Anna"]
    assert anna[0] == (FROM_MS // 1000, 1)  # interval started before the range: clamped to its start
    assert (int(datetime(2026, 10, 17, 7, 0, tzinfo=UTC).timestamp()), 0) in anna
    assert anna[-1] == (TO_MS // 1000, 1)  # still home at the end of the range
    assert [t for t, _ in anna] == sorted(t for t, _ in anna)


def test_blocked_ratio_is_percent_of_all_queries(seeded_db):
    target = next(t for _n, p, t in _targets() if p["type"] == "stat")
    _columns, rows = _query(seeded_db, target)
    assert rows[0][0] == pytest.approx(100.0 * 10 / 85)


# --- provisioning ---


def test_datasource_provisioning():
    doc = yaml.safe_load((GRAFANA_DIR / "provisioning/datasources/homesense.yaml").read_text("utf-8"))
    (ds,) = doc["datasources"]
    assert ds["type"] == "frser-sqlite-datasource"
    assert ds["uid"] == DATASOURCE_UID
    assert ds["jsonData"]["path"] == "{{DB_PATH}}"
    assert ds["jsonData"]["pathPrefix"] == "file:"
    assert ds["jsonData"]["pathOptions"].split("&") == ["mode=ro", "_pragma=query_only(1)"]
    assert ds["editable"] is False


def _provisioned_uri(db_path: Path) -> str:
    """The exact string frser passes to sql.Open: pathPrefix + path + "?" + pathOptions."""
    doc = yaml.safe_load((GRAFANA_DIR / "provisioning/datasources/homesense.yaml").read_text("utf-8"))
    cfg = doc["datasources"][0]["jsonData"]
    return (
        cfg["pathPrefix"] + cfg["path"].replace("{{DB_PATH}}", db_path.as_posix()) + "?" + cfg["pathOptions"]
    )


def _frser_equivalent_connection(db_path: Path) -> sqlite3.Connection:
    """A connection equivalent to what frser-sqlite-datasource opens with the provisioned settings.

    Not frser itself (Grafana is not installed in dev): frser (pkg/plugin/query.go) opens the
    provisioned URI with the modernc driver, which runs each `_pragma=` option as a PRAGMA, and then
    sets SQLITE_LIMIT_ATTACHED to 0. SQLite ignores the `_pragma` URI parameter, so both are applied
    by hand here. test_bare_provisioned_uri_allows_attach pins why the attach limit matters.
    """
    uri = _provisioned_uri(db_path)
    con = sqlite3.connect(uri, uri=True)
    for option in uri.partition("?")[2].split("&"):
        if option.startswith("_pragma="):
            name, _, value = option.removeprefix("_pragma=").partition("(")
            con.execute(f"PRAGMA {name} = {value.rstrip(')')}")
    con.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    return con


def test_frser_equivalent_connection_is_read_only(db, tmp_path):
    con = _frser_equivalent_connection(db.path)
    try:
        assert con.execute("SELECT COUNT(*) FROM persons").fetchone() == (0,)
        for sql in (
            "INSERT INTO settings(key, value_json) VALUES ('x', '1')",
            "CREATE TABLE evil (a)",
            f"ATTACH DATABASE '{(tmp_path / 'evil.db').as_posix()}' AS evil",
        ):
            with pytest.raises(sqlite3.OperationalError):
                con.execute(sql)
    finally:
        con.close()
    assert not (tmp_path / "evil.db").exists()


def test_bare_provisioned_uri_allows_attach(db, tmp_path):
    """Pins the dependency: mode=ro alone does NOT stop ATTACH; frser's attach limit (0 unless
    GF_PLUGIN_UNSAFE_ALLOW_ATTACH_LIMIT_ABOVE_ZERO=true) is what blocks it. If this ever fails,
    SQLite got stricter, which is fine; the frser-equivalent test above is the one that must hold."""
    con = sqlite3.connect(_provisioned_uri(db.path), uri=True)
    try:
        con.execute(f"ATTACH DATABASE '{(tmp_path / 'other.db').as_posix()}' AS other")
        assert [r[1] for r in con.execute("PRAGMA database_list")] == ["main", "other"]
    finally:
        con.close()


def test_mode_ro_alone_blocks_writes_to_the_main_file(db):
    """Even without frser's extra pragma/limit, mode=ro (the URI part SQLite honours) refuses writes."""
    con = sqlite3.connect(f"file:{db.path.as_posix()}?mode=ro", uri=True)
    try:
        for sql in ("INSERT INTO settings(key, value_json) VALUES ('x', '1')", "CREATE TABLE evil (a)"):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                con.execute(sql)
    finally:
        con.close()


_SECRET_COLUMN = re.compile(r"password|hash|secret|token", re.IGNORECASE)


def test_grafana_database_holds_no_credentials(db):
    """homesense.db (what Grafana reads) has no users table and no secret-looking column."""
    with db.connect() as c:
        tables = [r[0] for r in c.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")]
        assert "users" not in tables
        for table in tables:
            for column in c.exec_driver_sql(f"PRAGMA table_info('{table}')"):
                assert not _SECRET_COLUMN.search(column[1]), (table, column[1])


def test_dashboard_provider_provisioning():
    doc = yaml.safe_load((GRAFANA_DIR / "provisioning/dashboards/homesense.yaml").read_text("utf-8"))
    (provider,) = doc["providers"]
    assert provider["type"] == "file"
    assert provider["allowUiUpdates"] is False
    assert provider["options"]["path"] == "{{DASHBOARDS_DIR}}"


def test_grafana_ini_template():
    ini = configparser.RawConfigParser()
    ini.read_string((GRAFANA_DIR / "grafana.ini.template").read_text(encoding="utf-8"))
    assert ini["server"]["http_addr"] == "127.0.0.1"
    assert ini["server"]["http_port"] == "3000"
    assert ini["server"]["root_url"] == "%(protocol)s://%(domain)s:%(http_port)s/grafana/"
    assert ini["server"]["serve_from_sub_path"] == "true"
    assert ini["security"]["allow_embedding"] == "true"
    assert ini["security"]["disable_gravatar"] == "true"
    assert ini["security"]["cookie_samesite"] == "lax"
    assert ini["security"]["disable_initial_admin_creation"] == "true"
    assert ini["auth.anonymous"]["enabled"] == "true"
    assert ini["auth.anonymous"]["org_role"] == "Viewer"
    assert ini["auth"]["disable_login_form"] == "false"
    assert ini["users"]["allow_sign_up"] == "false"
    assert ini["paths"]["provisioning"] == "{{PROVISIONING_DIR}}"
    assert ini["paths"]["data"].startswith("{{DATA_DIR}}")
    assert ini["paths"]["logs"].startswith("{{DATA_DIR}}")
    text = (GRAFANA_DIR / "grafana.ini.template").read_text(encoding="utf-8")
    assert "0.0.0.0" not in text


def test_presence_timeline_home_wins_on_equal_timestamps(db):
    """Leave and re-arrive in the same second: the later 'home' row must come last."""
    with db.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO persons(id, name, privacy_mode, created_at) "
            "VALUES (1, 'Anna', 'full', '2026-10-01 00:00:00')"
        )
        c.exec_driver_sql(
            "INSERT INTO presence_intervals(person_id, start_ts, end_ts) VALUES "
            "(1, '2026-10-15 08:00:00', '2026-10-15 12:00:00'), (1, '2026-10-15 12:00:00', NULL)"
        )
    target = next(t for _n, p, t in _targets() if p["type"] == "state-timeline")
    _columns, rows = _query(db, target)
    noon = int(datetime(2026, 10, 15, 12, 0, tzinfo=UTC).timestamp())
    assert [r[2] for r in rows if r[0] == noon] == [0, 1]
