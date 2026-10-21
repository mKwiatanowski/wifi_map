from __future__ import annotations

from datetime import datetime, timedelta

from homesense.storage.db import Database
from homesense.storage.repositories import Repos


def purge_old(repos: Repos, now: datetime, retention_days: int) -> dict[str, int]:
    """Delete observations, presence events/closed intervals, DNS data, alerts and ended monitoring
    pauses older than `retention_days`. Returns deleted row counts per area.

    The coverage survey (plan, rooms, points) is user-created configuration and is never purged."""
    cutoff = now - timedelta(days=retention_days)
    return {
        "observations": repos.observations.delete_before(cutoff),
        "presence": repos.presence.delete_before(cutoff),
        "dns": repos.dns.delete_before(cutoff),
        "alerts": repos.alerts.delete_before(cutoff),
        "monitoring_pauses": repos.monitoring.delete_before(cutoff),
    }


def vacuum(db: Database) -> None:
    """Reclaim free pages (run weekly). VACUUM cannot run inside a transaction, hence AUTOCOMMIT."""
    with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.exec_driver_sql("VACUUM")
