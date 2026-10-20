from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from homesense.core.models import Alert
from homesense.core.timeutil import to_local
from homesense.storage.repositories import SettingsRepo

SETTINGS_KEY = "alerts.rules"
LOG_CHANNEL = "log"
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

DEFAULT_RULES = {
    "channels": {
        "new_device": ["log", "windows", "telegram", "ntfy"],
        "arrive": ["log"],
        "leave": ["log"],
        "*": ["log", "windows", "telegram", "ntfy"],
    },
    "min_severity": "info",
    "quiet_hours": {"start": 23, "end": 7, "allow": ["critical"]},
}


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _in_quiet_hours(local_hour: int, start: int, end: int) -> bool:
    """Start hour inclusive, end hour exclusive; wraps midnight when start > end; start == end disables."""
    if start == end:
        return False
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end


class AlertRules:
    """Routing rules: which notification channels receive an alert, persisted in settings."""

    def __init__(self, settings_repo: SettingsRepo, defaults: dict, tz: str) -> None:
        self._settings = settings_repo
        self._defaults = copy.deepcopy(defaults)
        self._tz = tz

    def config(self) -> dict:
        saved: dict[str, Any] = self._settings.get(SETTINGS_KEY) or {}
        return _deep_merge(self._defaults, saved)

    def save(self, cfg: dict) -> None:
        self._settings.set(SETTINGS_KEY, cfg)

    def channels_for(self, alert: Alert, now: datetime) -> list[str]:
        cfg = self.config()
        channels_by_kind: dict[str, list[str]] = cfg.get("channels", {})
        channels = channels_by_kind.get(alert.kind, channels_by_kind.get("*", []))
        channels = list(dict.fromkeys(channels))

        severity = SEVERITY_RANK.get(alert.severity, 0)
        if severity < SEVERITY_RANK.get(cfg.get("min_severity", "info"), 0):
            return []

        quiet = cfg.get("quiet_hours") or {}
        if "start" in quiet and "end" in quiet:
            hour = to_local(now, self._tz).hour
            is_quiet = _in_quiet_hours(hour, int(quiet["start"]), int(quiet["end"]))
            if is_quiet and alert.severity not in quiet.get("allow", []):
                channels = [c for c in channels if c == LOG_CHANNEL]
        return channels
