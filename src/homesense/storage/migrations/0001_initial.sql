-- HomeSense initial schema (spec section 5). All timestamps: UTC text '%Y-%m-%d %H:%M:%S'.

CREATE TABLE persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    color TEXT,
    privacy_mode TEXT NOT NULL DEFAULT 'full' CHECK (privacy_mode IN ('full', 'stats_only')),
    created_at TEXT NOT NULL
);

CREATE TABLE devices (
    mac TEXT PRIMARY KEY,
    person_id INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    label TEXT,
    hostname TEXT,
    vendor TEXT,
    ip TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    trusted INTEGER NOT NULL DEFAULT 0,
    counts_for_presence INTEGER NOT NULL DEFAULT 0,
    kind TEXT,
    notes TEXT
);

CREATE TABLE device_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mac TEXT NOT NULL,
    ip TEXT,
    source TEXT NOT NULL,
    online INTEGER NOT NULL,
    connection TEXT NOT NULL DEFAULT 'unknown',
    signal INTEGER,
    up_kbps REAL,
    down_kbps REAL
);
CREATE INDEX ix_device_observations_mac_ts ON device_observations (mac, ts);
CREATE INDEX ix_device_observations_ts ON device_observations (ts);

CREATE TABLE presence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    person_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('arrive', 'leave')),
    mac TEXT
);
CREATE INDEX ix_presence_events_person_ts ON presence_events (person_id, ts);

CREATE TABLE presence_intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT
);

CREATE TABLE dns_hourly (
    hour_ts TEXT NOT NULL,
    mac TEXT NOT NULL,
    domain TEXT NOT NULL,
    queries INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_ts, mac, domain)
);
CREATE INDEX ix_dns_hourly_hour ON dns_hourly (hour_ts);

CREATE TABLE dns_hourly_stats (
    hour_ts TEXT NOT NULL,
    mac TEXT NOT NULL,
    queries INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_ts, mac)
);

CREATE TABLE domain_first_seen (
    mac TEXT NOT NULL,
    domain TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (mac, domain)
);

CREATE TABLE profiles (
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    weekday INTEGER NOT NULL,
    hour INTEGER NOT NULL,
    value REAL NOT NULL,
    samples INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subject_type, subject_id, weekday, hour)
);

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    person_id INTEGER,
    mac TEXT,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    acknowledged INTEGER NOT NULL DEFAULT 0,
    dedup_key TEXT
);
CREATE INDEX ix_alerts_ts ON alerts (ts);
CREATE INDEX ix_alerts_dedup_ts ON alerts (dedup_key, ts);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE collector_status (
    name TEXT PRIMARY KEY,
    last_ok_ts TEXT,
    last_error_ts TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
