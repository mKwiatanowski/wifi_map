from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import text

from homesense.core.models import DeviceObservation, normalize_mac
from homesense.core.timeutil import ts_from_db, ts_to_db
from homesense.storage.repositories._base import Repo

_COLS = "ts, mac, ip, source, online, connection, signal, up_kbps, down_kbps"


def _row(r) -> DeviceObservation:
    return DeviceObservation(
        ts=ts_from_db(r.ts),
        mac=r.mac,
        source=r.source,
        online=bool(r.online),
        ip=r.ip,
        connection=r.connection,
        signal=r.signal,
        up_kbps=r.up_kbps,
        down_kbps=r.down_kbps,
    )


class ObservationRepo(Repo):
    """Time windows are half-open: since <= ts < until."""

    def add_many(self, obs: Iterable[DeviceObservation]) -> int:
        rows = [
            {
                "ts": ts_to_db(o.ts),
                "mac": normalize_mac(o.mac),
                "ip": o.ip,
                "source": o.source,
                "online": int(o.online),
                "connection": o.connection,
                "signal": o.signal,
                "up": o.up_kbps,
                "down": o.down_kbps,
            }
            for o in obs
        ]
        if not rows:
            return 0
        with self.db.begin() as c:
            c.execute(
                text(
                    f"INSERT INTO device_observations({_COLS}) "
                    "VALUES (:ts, :mac, :ip, :source, :online, :connection, :signal, :up, :down)"
                ),
                rows,
            )
        return len(rows)

    def for_device(self, mac: str, since: datetime, until: datetime) -> list[DeviceObservation]:
        with self.db.connect() as c:
            rows = c.execute(
                text(
                    f"SELECT {_COLS} FROM device_observations WHERE mac = :m AND ts >= :s AND ts < :u "
                    "ORDER BY ts, id"
                ),
                {"m": normalize_mac(mac), "s": ts_to_db(since), "u": ts_to_db(until)},
            )
            return [_row(r) for r in rows]

    def online_buckets(
        self, mac: str, since: datetime, until: datetime, bucket_s: int = 60
    ) -> list[datetime]:
        """Sorted distinct bucket starts (epoch-aligned, UTC) that contain an online observation."""
        if bucket_s <= 0:
            raise ValueError(f"bucket_s must be positive, got {bucket_s}")
        with self.db.connect() as c:
            rows = c.execute(
                text(
                    "SELECT DISTINCT (CAST(strftime('%s', ts) AS INTEGER) / :b) * :b AS bucket "
                    "FROM device_observations WHERE mac = :m AND online = 1 AND ts >= :s AND ts < :u "
                    "ORDER BY bucket"
                ),
                {"b": bucket_s, "m": normalize_mac(mac), "s": ts_to_db(since), "u": ts_to_db(until)},
            )
            return [datetime.fromtimestamp(r.bucket, UTC) for r in rows]

    def recent_traffic(self, mac: str, limit: int) -> list[DeviceObservation]:
        with self.db.connect() as c:
            rows = c.execute(
                text(
                    f"SELECT {_COLS} FROM device_observations WHERE mac = :m AND down_kbps IS NOT NULL "
                    "ORDER BY ts DESC, id DESC LIMIT :l"
                ),
                {"m": normalize_mac(mac), "l": limit},
            )
            return [_row(r) for r in rows]

    def signal_samples(self, since: datetime, until: datetime) -> dict[str, list[DeviceObservation]]:
        """Observations with radio data (an RSSI or a known connection type) per MAC, oldest first."""
        out: dict[str, list[DeviceObservation]] = {}
        with self.db.connect() as c:
            rows = c.execute(
                text(
                    f"SELECT {_COLS} FROM device_observations WHERE ts >= :s AND ts < :u "
                    "AND (signal IS NOT NULL OR connection != 'unknown') ORDER BY mac, ts, id"
                ),
                {"s": ts_to_db(since), "u": ts_to_db(until)},
            )
            for r in rows:
                out.setdefault(r.mac, []).append(_row(r))
        return out

    def delete_before(self, ts: datetime) -> int:
        with self.db.begin() as c:
            return c.execute(
                text("DELETE FROM device_observations WHERE ts < :t"), {"t": ts_to_db(ts)}
            ).rowcount
