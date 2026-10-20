-- HomeSense auth database (auth.db): login accounts only. Grafana is never pointed at this file.
-- Timestamps: UTC text '%Y-%m-%d %H:%M:%S'.

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
