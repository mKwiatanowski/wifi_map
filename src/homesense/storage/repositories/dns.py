from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import text

from homesense.core.models import normalize_mac
from homesense.core.timeutil import ts_from_db, ts_to_db
from homesense.storage.repositories._base import Repo

UNKNOWN_MAC = "unknown"


def _mac_key(mac: str) -> str:
    """Normalized MAC, or the literal 'unknown' for clients that couldn't be resolved."""
    return mac if mac == UNKNOWN_MAC else normalize_mac(mac)


def _opt_mac_key(mac: str | None) -> str | None:
    return None if mac is None else _mac_key(mac)


def _exclude_clause(macs: Iterable[str]) -> tuple[str, dict[str, str]]:
    """SQL fragment `AND mac NOT IN (...)` with its parameters (empty when nothing is excluded)."""
    keys = sorted({_mac_key(m) for m in macs})
    if not keys:
        return "", {}
    params = {f"x{i}": key for i, key in enumerate(keys)}
    return f"AND mac NOT IN ({', '.join(':' + name for name in params)})", params


class DnsRepo(Repo):
    """`mac` is a normalized MAC or "unknown" (unresolved clients); anything else raises ValueError.

    Time windows are half-open: since <= ts < until.
    """

    def add_hourly(self, hour_ts: datetime, mac: str, domain: str, queries: int, blocked: int) -> None:
        with self.db.begin() as c:
            c.execute(
                text(
                    "INSERT INTO dns_hourly(hour_ts, mac, domain, queries, blocked) "
                    "VALUES (:h, :m, :d, :q, :b) "
                    "ON CONFLICT(hour_ts, mac, domain) DO UPDATE SET "
                    "queries = queries + excluded.queries, blocked = blocked + excluded.blocked"
                ),
                {"h": ts_to_db(hour_ts), "m": _mac_key(mac), "d": domain, "q": queries, "b": blocked},
            )

    def add_hourly_stats(self, hour_ts: datetime, mac: str, queries: int, blocked: int) -> None:
        with self.db.begin() as c:
            c.execute(
                text(
                    "INSERT INTO dns_hourly_stats(hour_ts, mac, queries, blocked) VALUES (:h, :m, :q, :b) "
                    "ON CONFLICT(hour_ts, mac) DO UPDATE SET "
                    "queries = queries + excluded.queries, blocked = blocked + excluded.blocked"
                ),
                {"h": ts_to_db(hour_ts), "m": _mac_key(mac), "q": queries, "b": blocked},
            )

    def mark_domain_seen(self, mac: str, domain: str, ts: datetime) -> bool:
        """Returns True if this (mac, domain) pair was seen for the first time."""
        with self.db.begin() as c:
            return (
                c.execute(
                    text(
                        "INSERT OR IGNORE INTO domain_first_seen(mac, domain, first_seen) VALUES (:m, :d, :t)"
                    ),
                    {"m": _mac_key(mac), "d": domain, "t": ts_to_db(ts)},
                ).rowcount
                == 1
            )

    def top_domains(
        self, mac: str | None, since: datetime, limit: int = 20, exclude_macs: Iterable[str] = ()
    ) -> list[tuple[str, int, int]]:
        """(domain, queries, blocked) by queries; rows of `exclude_macs` are left out of the sums."""
        mac_clause = "AND mac = :m" if mac is not None else ""
        excl_clause, excl = _exclude_clause(exclude_macs)
        with self.db.connect() as c:
            rows = c.execute(
                text(
                    f"SELECT domain, SUM(queries) AS q, SUM(blocked) AS b FROM dns_hourly "
                    f"WHERE hour_ts >= :s {mac_clause} {excl_clause} "
                    "GROUP BY domain ORDER BY q DESC, domain LIMIT :l"
                ),
                {"s": ts_to_db(since), "m": _opt_mac_key(mac), "l": limit, **excl},
            )
            return [(r.domain, r.q, r.b) for r in rows]

    def hourly_stats(
        self, mac: str | None, since: datetime, until: datetime
    ) -> list[tuple[datetime, str, int, int]]:
        mac_clause = "AND mac = :m" if mac is not None else ""
        with self.db.connect() as c:
            rows = c.execute(
                text(
                    f"SELECT hour_ts, mac, queries, blocked FROM dns_hourly_stats "
                    f"WHERE hour_ts >= :s AND hour_ts < :u {mac_clause} ORDER BY hour_ts, mac"
                ),
                {"s": ts_to_db(since), "u": ts_to_db(until), "m": _opt_mac_key(mac)},
            )
            return [(ts_from_db(r.hour_ts), r.mac, r.queries, r.blocked) for r in rows]

    def new_domains(self, since: datetime, mac: str | None = None) -> list[tuple[str, str, datetime]]:
        mac_clause = "AND mac = :m" if mac is not None else ""
        with self.db.connect() as c:
            rows = c.execute(
                text(
                    "SELECT mac, domain, first_seen FROM domain_first_seen "
                    f"WHERE first_seen >= :s {mac_clause} "
                    "ORDER BY first_seen, mac, domain"
                ),
                {"s": ts_to_db(since), "m": _opt_mac_key(mac)},
            )
            return [(r.mac, r.domain, ts_from_db(r.first_seen)) for r in rows]

    def top_blocked(
        self, since: datetime, limit: int = 20, exclude_macs: Iterable[str] = ()
    ) -> list[tuple[str, int]]:
        excl_clause, excl = _exclude_clause(exclude_macs)
        with self.db.connect() as c:
            rows = c.execute(
                text(
                    f"SELECT domain, SUM(blocked) AS b FROM dns_hourly WHERE hour_ts >= :s {excl_clause} "
                    "GROUP BY domain HAVING b > 0 ORDER BY b DESC, domain LIMIT :l"
                ),
                {"s": ts_to_db(since), "l": limit, **excl},
            )
            return [(r.domain, r.b) for r in rows]

    def delete_before(self, ts: datetime) -> int:
        """Delete hourly aggregates older than ts, plus domain_first_seen rows older than ts that have
        no hourly activity at/after ts. Domains still in use keep their first_seen, so they don't
        re-appear as 'new' after the retention window."""
        t = {"t": ts_to_db(ts)}
        with self.db.begin() as c:
            n = c.execute(text("DELETE FROM dns_hourly WHERE hour_ts < :t"), t).rowcount
            n += c.execute(text("DELETE FROM dns_hourly_stats WHERE hour_ts < :t"), t).rowcount
            n += c.execute(
                text(
                    "DELETE FROM domain_first_seen WHERE first_seen < :t AND NOT EXISTS ("
                    "SELECT 1 FROM dns_hourly h WHERE h.mac = domain_first_seen.mac "
                    "AND h.domain = domain_first_seen.domain AND h.hour_ts >= :t)"
                ),
                t,
            ).rowcount
        return n
