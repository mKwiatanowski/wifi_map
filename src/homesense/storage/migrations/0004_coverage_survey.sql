-- Wi-Fi coverage survey (/coverage). User-created configuration: exempt from the retention purge,
-- removed only when the user deletes it. Coordinates in metres, origin = top-left of the plan.
CREATE TABLE coverage_plan (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    width_m REAL NOT NULL,
    height_m REAL NOT NULL,
    router_x REAL,
    router_y REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE coverage_rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    w REAL NOT NULL,
    h REAL NOT NULL
);

-- One measurement: the device's RSSI from router observations in [started_at, ends_at).
-- done = 0 while measuring; rssi NULL after done = no sample (invalid point).
CREATE TABLE coverage_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mac TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    started_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    rssi REAL,
    samples INTEGER NOT NULL DEFAULT 0,
    band TEXT NOT NULL DEFAULT 'unknown'
);
