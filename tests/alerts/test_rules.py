import copy
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from homesense.alerts.rules import DEFAULT_RULES, AlertRules
from homesense.core.models import Alert

TZ = "Europe/Warsaw"
WAW = ZoneInfo(TZ)
ALL = ["log", "windows", "telegram", "ntfy"]


def local(h, mi=0, d=20) -> datetime:
    # 2026-10-20 is CEST (UTC+2); 2026-10-26 is CET (UTC+1)
    return datetime(2026, 10, d, h, mi, tzinfo=WAW).astimezone(UTC)


def alert(kind="new_device", severity="warning") -> Alert:
    return Alert(kind=kind, severity=severity, title="t", message="m", ts=local(12))


@pytest.fixture
def rules(repos):
    return AlertRules(repos.settings, DEFAULT_RULES, TZ)


def test_default_rules_verbatim():
    assert DEFAULT_RULES == {
        "channels": {
            "new_device": ["log", "windows", "telegram", "ntfy"],
            "arrive": ["log"],
            "leave": ["log"],
            "*": ["log", "windows", "telegram", "ntfy"],
        },
        "min_severity": "info",
        "quiet_hours": {"start": 23, "end": 7, "allow": ["critical"]},
    }


def test_config_defaults_when_nothing_saved(rules):
    assert rules.config() == DEFAULT_RULES


def test_config_deep_merges_saved_over_defaults(rules, repos):
    repos.settings.set(
        "alerts.rules", {"channels": {"arrive": ["log", "telegram"]}, "quiet_hours": {"start": 22}}
    )
    cfg = rules.config()
    assert cfg["channels"]["arrive"] == ["log", "telegram"]
    assert cfg["channels"]["new_device"] == ALL
    assert cfg["channels"]["*"] == ALL
    assert cfg["quiet_hours"] == {"start": 22, "end": 7, "allow": ["critical"]}
    assert cfg["min_severity"] == "info"


def test_config_does_not_mutate_defaults(repos):
    defaults = copy.deepcopy(DEFAULT_RULES)
    r = AlertRules(repos.settings, defaults, TZ)
    r.config()["channels"]["arrive"].append("ntfy")
    repos.settings.set("alerts.rules", {"channels": {"leave": []}})
    r.config()["quiet_hours"]["allow"].append("info")
    assert defaults == DEFAULT_RULES


def test_save_persists_and_roundtrips(rules, repos):
    rules.save({"min_severity": "warning"})
    assert repos.settings.get("alerts.rules") == {"min_severity": "warning"}
    assert rules.config()["min_severity"] == "warning"
    assert rules.config()["channels"] == DEFAULT_RULES["channels"]


def test_kind_specific_channels(rules):
    assert rules.channels_for(alert("arrive", "info"), local(12)) == ["log"]
    assert rules.channels_for(alert("leave", "info"), local(12)) == ["log"]
    assert rules.channels_for(alert("new_device", "warning"), local(12)) == ALL


def test_unknown_kind_falls_back_to_star(rules):
    rules.save({"channels": {"*": ["log", "ntfy"]}})
    assert rules.channels_for(alert("dns_spike", "warning"), local(12)) == ["log", "ntfy"]


def test_unknown_kind_without_star_gets_nothing(repos):
    r = AlertRules(repos.settings, {"channels": {"arrive": ["log"]}, "min_severity": "info"}, TZ)
    assert r.channels_for(alert("dns_spike"), local(12)) == []


def test_min_severity_drops_lower(rules):
    rules.save({"min_severity": "warning"})
    assert rules.channels_for(alert(severity="info"), local(12)) == []
    assert rules.channels_for(alert(severity="warning"), local(12)) == ALL
    assert rules.channels_for(alert(severity="critical"), local(12)) == ALL


def test_quiet_hours_at_0100_local_warning_only_log_critical_all(rules):
    now = local(1)
    assert now.hour == 23  # 01:00 CEST is 23:00 UTC the previous day
    assert rules.channels_for(alert(severity="warning"), now) == ["log"]
    assert rules.channels_for(alert(severity="info"), now) == ["log"]
    assert rules.channels_for(alert(severity="critical"), now) == ALL


@pytest.mark.parametrize(
    ("h", "mi", "quiet"),
    [
        (22, 59, False),
        (23, 0, True),
        (23, 59, True),
        (0, 0, True),
        (6, 59, True),
        (7, 0, False),
        (12, 0, False),
    ],
)
def test_quiet_hours_wrap_around_midnight(rules, h, mi, quiet):
    expected = ["log"] if quiet else ALL
    assert rules.channels_for(alert(severity="warning"), local(h, mi)) == expected


@pytest.mark.parametrize(("h", "quiet"), [(0, False), (1, True), (4, True), (5, False), (23, False)])
def test_quiet_hours_non_wrapping_window(rules, h, quiet):
    rules.save({"quiet_hours": {"start": 1, "end": 5}})
    expected = ["log"] if quiet else ALL
    assert rules.channels_for(alert(severity="warning"), local(h)) == expected


def test_quiet_hours_equal_start_end_disabled(rules):
    rules.save({"quiet_hours": {"start": 7, "end": 7}})
    assert rules.channels_for(alert(severity="warning"), local(7)) == ALL
    assert rules.channels_for(alert(severity="warning"), local(1)) == ALL


def test_quiet_hours_allow_list_configurable(rules):
    rules.save({"quiet_hours": {"allow": ["warning", "critical"]}})
    assert rules.channels_for(alert(severity="warning"), local(1)) == ALL
    assert rules.channels_for(alert(severity="info"), local(1)) == ["log"]


def test_quiet_hours_without_log_channel_yields_nothing(rules):
    rules.save({"channels": {"new_device": ["telegram"]}})
    assert rules.channels_for(alert(severity="warning"), local(1)) == []


def test_quiet_hours_use_local_time_across_dst(rules):
    # CET: 06:30 local = 05:30 UTC (quiet), 07:30 local = 06:30 UTC (not quiet)
    assert rules.channels_for(alert(severity="warning"), local(6, 30, d=26)) == ["log"]
    assert rules.channels_for(alert(severity="warning"), local(7, 30, d=26)) == ALL


def test_duplicate_channels_collapsed(rules):
    rules.save({"channels": {"arrive": ["log", "telegram", "log"]}})
    assert rules.channels_for(alert("arrive", "info"), local(12)) == ["log", "telegram"]
