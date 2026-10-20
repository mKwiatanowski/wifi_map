-- Login accounts now live in auth.db (storage/auth.py), which Grafana never reads.
-- storage.auth.migrate_databases copies existing rows to auth.db and deletes them here before this
-- migration runs; the guard aborts the migration (and keeps the table) if any row was not moved.

-- secure_delete zeroes the freed pages; storage.auth then checkpoints and VACUUMs so no copy of the
-- (already moved, deleted) rows survives in homesense.db or its -wal file.
PRAGMA secure_delete = ON;

CREATE TEMP TABLE _users_moved_guard (remaining INTEGER NOT NULL CHECK (remaining = 0));
INSERT INTO _users_moved_guard SELECT COUNT(*) FROM users;
DROP TABLE _users_moved_guard;
DROP TABLE users;

-- Persisted until storage.auth has scrubbed the file (retried on the next start after a crash).
INSERT OR REPLACE INTO settings(key, value_json) VALUES ('auth_scrub_pending', 'true');
