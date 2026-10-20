import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from homesense.app import HomeSenseApp
from homesense.collectors.adguard_dns import AdGuardDnsCollector
from homesense.collectors.arp_scan import ArpScanCollector
from homesense.collectors.router_tplink import TplinkRouterCollector
from homesense.collectors.simulated import SimulatedCollector
from homesense.core.clock import FakeClock
from homesense.core.config import Settings

ANNA_PHONE = "f0:18:98:a1:00:01"
# Monday 20:00 Europe/Warsaw (CEST): Anna is home on a weekday evening.
EVENING = datetime(2026, 10, 19, 18, 0, tzinfo=UTC)


def _settings(tmp_path, **kw) -> Settings:
    base = {"mode": "simulated", "data_dir": tmp_path, "windows_toast": False}
    return Settings(_env_file=None, **{**base, **kw})


@pytest.fixture
async def sim_app(tmp_path):
    clock = FakeClock(EVENING)
    app = HomeSenseApp(_settings(tmp_path), clock)
    yield app, clock
    await app.stop()


async def test_simulated_steps_mark_anna_home(sim_app):
    app, clock = sim_app
    app.initialize()
    anna = app.repos.persons.create("Anna")
    app.repos.devices.upsert_seen(ANNA_PHONE, clock.now() - timedelta(days=1))
    app.repos.devices.update(ANNA_PHONE, person_id=anna, counts_for_presence=True)

    for _ in range(30):
        await app.step()
        clock.advance(timedelta(minutes=1))

    states = {s.person_id: s for s in app.presence.states()}
    assert states[anna].home
    assert len(app.repos.devices.list()) > 0
    stats = app.repos.dns.hourly_stats(None, EVENING - timedelta(hours=1), clock.now() + timedelta(hours=1))
    assert sum(q for _, _, q, _ in stats) > 0


async def test_detector_context_reflects_presence(sim_app):
    app, clock = sim_app
    await app.step()
    ctx = app.detector_context()
    assert ctx.now == clock.now()
    assert ctx.tz == "Europe/Warsaw"
    assert ctx.presence == app.presence.states()


def test_simulated_mode_builds_only_simulated_collector(tmp_path):
    app = HomeSenseApp(_settings(tmp_path), FakeClock(EVENING))
    collectors = app.build_collectors()
    assert len(collectors) == 1 and isinstance(collectors[0], SimulatedCollector)
    assert app.household is not None
    assert app.adguard is None
    app.db.close()


async def test_live_mode_builds_enabled_collectors_without_network(tmp_path):
    app = HomeSenseApp(_settings(tmp_path, mode="live"), FakeClock(EVENING))
    kinds = [type(c) for c in app.build_collectors()]
    assert kinds == [TplinkRouterCollector, ArpScanCollector, AdGuardDnsCollector]
    assert app.household is None and app.adguard is not None
    await app.stop()

    app = HomeSenseApp(
        _settings(tmp_path, mode="live", router_enabled=False, arp_enabled=False, adguard_enabled=False),
        FakeClock(EVENING),
    )
    assert app.build_collectors() == []
    assert app.adguard is None
    await app.stop()


async def test_notifier_registration(tmp_path, monkeypatch):
    app = HomeSenseApp(_settings(tmp_path), FakeClock(EVENING))
    assert app.notifiers.names() == ["log"]
    await app.stop()

    monkeypatch.setattr("homesense.app.sys.platform", "win32")
    full = _settings(
        tmp_path, windows_toast=True, telegram_bot_token="t", telegram_chat_id="c", ntfy_topic="topic"
    )
    app = HomeSenseApp(full, FakeClock(EVENING))
    assert sorted(app.notifiers.names()) == ["log", "ntfy", "telegram", "windows"]
    await app.stop()

    monkeypatch.setattr("homesense.app.sys.platform", "linux")
    app = HomeSenseApp(_settings(tmp_path, windows_toast=True, telegram_bot_token="t"), FakeClock(EVENING))
    assert app.notifiers.names() == ["log"]
    await app.stop()


def test_quiet_hours_come_from_settings(tmp_path):
    app = HomeSenseApp(_settings(tmp_path, quiet_hours_start=22, quiet_hours_end=6), FakeClock(EVENING))
    app.initialize()
    assert app.rules.config()["quiet_hours"]["start"] == 22
    assert app.rules.config()["quiet_hours"]["end"] == 6
    app.db.close()


async def test_start_and_stop_run_background_jobs(tmp_path):
    clock = FakeClock(EVENING)
    app = HomeSenseApp(_settings(tmp_path), clock)
    await app.start()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if app.repos.collector_status.get("simulated") is not None:
            break
    assert app.repos.collector_status.get("simulated").last_ok_ts is not None
    tasks = list(app.tasks)
    assert {t.get_name() for t in tasks} >= {
        "collectors",
        "presence",
        "profiles",
        "detectors",
        "retention",
        "vacuum",
    }
    await app.stop()
    assert all(t.done() for t in tasks)


async def test_stop_waits_for_running_thread_job(tmp_path):
    app = HomeSenseApp(_settings(tmp_path), FakeClock(EVENING))
    finished = []

    def slow_rebuild():
        time.sleep(0.2)
        app.repos.persons.list()  # still uses the DB after the job was cancelled
        finished.append(True)

    app.profiles.rebuild_all = slow_rebuild
    await app.start()
    await asyncio.sleep(0.05)
    await app.stop()
    assert finished == [True]
