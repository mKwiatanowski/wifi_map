-- Monitoring on/off switch (monitoring.MonitoringControl). One row per pause; the open pause has
-- end_ts NULL. until_ts NULL = paused until resumed by hand. Times: UTC text '%Y-%m-%d %H:%M:%S'.
CREATE TABLE monitoring_pauses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts TEXT NOT NULL,
    until_ts TEXT,
    end_ts TEXT
);
CREATE INDEX ix_monitoring_pauses_end ON monitoring_pauses (end_ts);
