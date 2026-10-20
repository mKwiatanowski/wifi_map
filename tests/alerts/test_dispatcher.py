import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from homesense.alerts.dispatcher import NotificationDispatcher
from homesense.alerts.rules import DEFAULT_RULES, AlertRules
from homesense.core.clock import FakeClock
from homesense.core.models import Alert, AlertRaised
from homesense.core.registry import Registry

TZ = "Europe/Warsaw"
NOON = datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo(TZ)).astimezone(UTC)
NIGHT = datetime(2026, 10, 20, 1, 0, tzinfo=ZoneInfo(TZ)).astimezone(UTC)


class FakeNotifier:
    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.sent.append(alert)
        if self.fail:
            raise RuntimeError(f"{self.name} is down")


def alert(kind="new_device", severity="warning") -> Alert:
    return Alert(kind=kind, severity=severity, title="Nowe urządzenie", message="m", ts=NOON)


def make(repos, now, *notifiers) -> NotificationDispatcher:
    reg = Registry()
    for n in notifiers:
        reg.register(n.name, n)
    return NotificationDispatcher(AlertRules(repos.settings, DEFAULT_RULES, TZ), reg, FakeClock(now))


async def test_calls_only_registered_notifiers(repos):
    log_n, tg = FakeNotifier("log"), FakeNotifier("telegram")
    d = make(repos, NOON, log_n, tg)  # rules also list windows + ntfy, which are not registered
    a = alert()
    await d.on_alert(AlertRaised(a))
    assert log_n.sent == [a]
    assert tg.sent == [a]


async def test_respects_kind_rules(repos):
    log_n, tg = FakeNotifier("log"), FakeNotifier("telegram")
    d = make(repos, NOON, log_n, tg)
    await d.on_alert(AlertRaised(alert("arrive", "info")))
    assert len(log_n.sent) == 1
    assert tg.sent == []


async def test_uses_clock_for_quiet_hours(repos):
    log_n, tg = FakeNotifier("log"), FakeNotifier("telegram")
    d = make(repos, NIGHT, log_n, tg)
    await d.on_alert(AlertRaised(alert(severity="warning")))
    assert len(log_n.sent) == 1
    assert tg.sent == []
    await d.on_alert(AlertRaised(alert(severity="critical")))
    assert len(tg.sent) == 1


async def test_survives_one_notifier_raising(repos, caplog):
    bad, good = FakeNotifier("telegram", fail=True), FakeNotifier("ntfy")
    d = make(repos, NOON, bad, good)
    a = alert()
    with caplog.at_level(logging.ERROR, logger="homesense.alerts.dispatcher"):
        await d.on_alert(AlertRaised(a))
    assert bad.sent == [a]
    assert good.sent == [a]
    assert any("telegram" in r.getMessage() for r in caplog.records)


async def test_send_test_uses_only_that_channel(repos):
    log_n, tg = FakeNotifier("log"), FakeNotifier("telegram")
    d = make(repos, NOON, log_n, tg)
    await d.send_test("telegram")
    assert log_n.sent == []
    [sent] = tg.sent
    assert sent.title == "Test powiadomień"
    assert sent.kind == "test"
    assert sent.ts == NOON


async def test_send_test_ignores_quiet_hours(repos):
    tg = FakeNotifier("telegram")
    d = make(repos, NIGHT, tg)
    await d.send_test("telegram")
    assert len(tg.sent) == 1


async def test_send_test_unknown_channel_raises_keyerror(repos):
    d = make(repos, NOON, FakeNotifier("log"))
    with pytest.raises(KeyError):
        await d.send_test("ntfy")


async def test_send_test_propagates_notifier_failure(repos):
    d = make(repos, NOON, FakeNotifier("telegram", fail=True))
    with pytest.raises(RuntimeError):
        await d.send_test("telegram")


async def test_broken_rules_fall_back_to_log(repos, caplog):
    log_n, tg = FakeNotifier("log"), FakeNotifier("telegram")
    d = make(repos, NOON, log_n, tg)
    repos.settings.set("alerts.rules", {"quiet_hours": {"start": "late", "end": 7}})  # int("late") fails
    a = alert()
    with caplog.at_level(logging.ERROR, logger="homesense.alerts.dispatcher"):
        await d.on_alert(AlertRaised(a))
    assert log_n.sent == [a]
    assert tg.sent == []
    assert any("rules" in r.getMessage() for r in caplog.records)
