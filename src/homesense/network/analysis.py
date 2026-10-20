"""DNS / device analysis helpers used by the web API (pure functions over the repositories).

Privacy: domain names of devices owned by a `stats_only` person are never returned, even if
older rows still exist (e.g. the person switched to `stats_only` recently).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta

from homesense.core.models import Device
from homesense.core.timeutil import floor_hour
from homesense.storage.repositories import Repos

STATS_ONLY = "stats_only"
ONLINE_WINDOW = timedelta(minutes=5)
NEW_DEVICE_WINDOW = timedelta(hours=24)
DEVICE_FILTERS = ("all", "online", "new", "unassigned", "unknown")
_HOUR = timedelta(hours=1)


def private_macs(repos: Repos) -> set[str]:
    """MACs of devices whose owner has privacy_mode='stats_only'."""
    private = {p.id for p in repos.persons.list() if p.privacy_mode == STATS_ONLY}
    return {d.mac for d in repos.devices.list() if d.person_id in private}


def top_domains(repos: Repos, mac: str | None, since: datetime, limit: int = 20) -> list[dict]:
    private = private_macs(repos)
    if mac is not None and mac in private:
        return []
    rows = repos.dns.top_domains(mac, since, limit, exclude_macs=private if mac is None else ())
    return [{"domain": d, "queries": q, "blocked": b} for d, q, b in rows]


def blocked_domains(repos: Repos, since: datetime, limit: int = 20) -> list[dict]:
    rows = repos.dns.top_blocked(since, limit, exclude_macs=private_macs(repos))
    return [{"domain": d, "blocked": b} for d, b in rows]


def new_domains(repos: Repos, since: datetime, limit: int = 200) -> list[dict]:
    """Domains seen for the first time per device, newest first."""
    private = private_macs(repos)
    names = {d.mac: d.display_name for d in repos.devices.list()}
    rows = [r for r in repos.dns.new_domains(since) if r[0] not in private]
    rows.sort(key=lambda r: r[2], reverse=True)
    return [
        {"mac": mac, "device": names.get(mac, mac), "domain": domain, "first_seen": ts.isoformat()}
        for mac, domain, ts in rows[:limit]
    ]


def hourly(repos: Repos, mac: str | None, since: datetime, until: datetime) -> list[dict]:
    """Queries/blocked per UTC hour in [floor_hour(since), until), zero-filled."""
    totals: dict[datetime, list[int]] = defaultdict(lambda: [0, 0])
    start = floor_hour(since)
    for hour_ts, _mac, queries, blocked in repos.dns.hourly_stats(mac, start, until):
        totals[hour_ts][0] += queries
        totals[hour_ts][1] += blocked
    result = []
    hour = start
    while hour < until:
        queries, blocked = totals.get(hour, (0, 0))
        result.append({"ts": hour.isoformat(), "queries": queries, "blocked": blocked})
        hour += _HOUR
    return result


def summary(repos: Repos, since: datetime, until: datetime) -> dict:
    stats = repos.dns.hourly_stats(None, floor_hour(since), until)
    return {
        "queries": sum(s[2] for s in stats),
        "blocked": sum(s[3] for s in stats),
        "clients": len({s[1] for s in stats if s[2] > 0}),
        "new_domains": len(new_domains(repos, since, limit=10_000)),
    }


def is_online(device: Device, now: datetime) -> bool:
    return device.last_seen >= now - ONLINE_WINDOW


def filter_devices(devices: Iterable[Device], kind: str, now: datetime) -> list[Device]:
    """kind: all | online | new (first seen < 24 h ago) | unassigned | unknown (unassigned + untrusted)."""
    if kind not in DEVICE_FILTERS:
        raise ValueError(f"unknown device filter: {kind!r}")
    checks = {
        "all": lambda d: True,
        "online": lambda d: is_online(d, now),
        "new": lambda d: d.first_seen >= now - NEW_DEVICE_WINDOW,
        "unassigned": lambda d: d.person_id is None,
        "unknown": lambda d: d.person_id is None and not d.trusted,
    }
    return [d for d in devices if checks[kind](d)]
