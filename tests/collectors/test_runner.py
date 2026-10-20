import asyncio
import contextlib
from datetime import timedelta

from homesense.collectors.base import CollectorRunner
from homesense.core.models import DeviceObservation, Observation


class FakeCollector:
    def __init__(self, name: str, interval_s: float = 0.01, error: Exception | None = None) -> None:
        self.name = name
        self.interval_s = interval_s
        self.error = error
        self.calls = 0
        self.clock = None

    async def collect(self) -> list[Observation]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [DeviceObservation(ts=self.clock.now(), mac="aa:bb:cc:dd:ee:01", source=self.name)]


class RecordingSink:
    def __init__(self) -> None:
        self.batches: list[list[Observation]] = []

    async def __call__(self, observations: list[Observation]) -> None:
        self.batches.append(observations)


def make(clock, repos, *collectors, max_backoff_s: float = 600):
    for c in collectors:
        c.clock = clock
    sink = RecordingSink()
    runner = CollectorRunner(
        list(collectors), sink, repos.collector_status, clock, max_backoff_s=max_backoff_s
    )
    return runner, sink


async def test_success_records_ok_and_passes_observations_to_sink(clock, repos):
    router = FakeCollector("router")
    runner, sink = make(clock, repos, router)

    assert await runner.run_once(router) is True

    assert len(sink.batches) == 1
    assert sink.batches[0][0].source == "router"
    status = repos.collector_status.get("router")
    assert status.last_ok_ts == clock.now()
    assert status.consecutive_failures == 0


async def test_failing_collector_records_error_while_other_still_delivers(clock, repos):
    router = FakeCollector("router", error=PermissionError("login failed: wrong password"))
    arp = FakeCollector("arp")
    runner, sink = make(clock, repos, router, arp)

    for _ in range(3):
        assert await runner.run_once(router) is False
        assert await runner.run_once(arp) is True

    router_status = repos.collector_status.get("router")
    assert router_status.consecutive_failures == 3
    assert "PermissionError" in router_status.last_error
    assert "wrong password" in router_status.last_error
    assert router_status.last_error_ts == clock.now()
    assert repos.collector_status.get("arp").consecutive_failures == 0
    assert len(sink.batches) == 3
    assert all(batch[0].source == "arp" for batch in sink.batches)
    assert runner.health() == "degraded"


async def test_sink_failure_is_caught_and_recorded(clock, repos):
    router = FakeCollector("router")
    router.clock = clock

    async def broken_sink(observations):
        raise RuntimeError("db locked")

    runner = CollectorRunner([router], broken_sink, repos.collector_status, clock)

    assert await runner.run_once(router) is False
    assert "db locked" in repos.collector_status.get("router").last_error


async def test_pause_skips_collect_until_deadline(clock, repos):
    router = FakeCollector("router")
    runner, sink = make(clock, repos, router)
    until = clock.now() + timedelta(minutes=5)

    runner.pause("router", until)

    assert runner.paused_until("router") == until
    assert await runner.run_once(router) is True
    assert router.calls == 0
    assert sink.batches == []

    clock.advance(timedelta(minutes=5))
    assert runner.paused_until("router") is None
    assert await runner.run_once(router) is True
    assert router.calls == 1


async def test_paused_until_unknown_collector_is_none(clock, repos):
    runner, _ = make(clock, repos, FakeCollector("router"))
    assert runner.paused_until("nope") is None


async def test_health_transitions_ok_degraded_down(clock, repos):
    router = FakeCollector("router", error=PermissionError("wrong password"))
    arp = FakeCollector("arp")
    runner, _ = make(clock, repos, router, arp)
    assert runner.health() == "ok"

    for _ in range(2):
        await runner.run_once(router)
    assert runner.health() == "ok"

    await runner.run_once(router)
    assert runner.health() == "degraded"

    arp.error = ConnectionError("unreachable")
    for _ in range(3):
        await runner.run_once(arp)
    assert runner.health() == "down"

    arp.error = None
    await runner.run_once(arp)
    assert runner.health() == "degraded"


async def test_backoff_delay_grows_and_is_capped(clock, repos):
    router = FakeCollector("router", interval_s=10, error=OSError("timeout"))
    runner, _ = make(clock, repos, router, max_backoff_s=60)

    assert runner.next_delay(router) == 10
    await runner.run_once(router)
    assert runner.next_delay(router) == 20
    await runner.run_once(router)
    assert runner.next_delay(router) == 40
    await runner.run_once(router)
    assert runner.next_delay(router) == 60

    router.error = None
    await runner.run_once(router)
    assert runner.next_delay(router) == 10


async def test_run_forever_runs_all_collectors_and_is_cancellable(clock, repos):
    router = FakeCollector("router", interval_s=0.01, error=PermissionError("wrong password"))
    arp = FakeCollector("arp", interval_s=0.01)
    runner, sink = make(clock, repos, router, arp, max_backoff_s=0.02)

    task = asyncio.create_task(runner.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert router.calls >= 1
    assert arp.calls >= 2
    assert len(sink.batches) == arp.calls
    assert repos.collector_status.get("router").consecutive_failures == router.calls


async def test_backoff_does_not_overflow_after_very_long_outage(clock, repos):
    router = FakeCollector("router", interval_s=10.0, error=OSError("unreachable"))
    runner, _ = make(clock, repos, router, max_backoff_s=600)
    runner._failures["router"] = 2000

    assert runner.next_delay(router) == 600


class FlakyClock:
    """Delegates to a FakeClock but raises on the first `fail_times` calls."""

    def __init__(self, inner, fail_times: int) -> None:
        self.inner = inner
        self.fail_times = fail_times

    def now(self):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("clock glitch")
        return self.inner.now()


class SignallingSink(RecordingSink):
    def __init__(self) -> None:
        super().__init__()
        self.delivered = asyncio.Event()

    async def __call__(self, observations: list[Observation]) -> None:
        await super().__call__(observations)
        self.delivered.set()


async def test_run_forever_survives_unexpected_loop_error(clock, repos):
    arp = FakeCollector("arp", interval_s=0.01)
    arp.clock = clock
    sink = SignallingSink()
    flaky_clock = FlakyClock(clock, fail_times=1)
    runner = CollectorRunner([arp], sink, repos.collector_status, flaky_clock)
    runner.pause("arp", clock.now())  # forces paused_until -> clock.now() outside run_once's try

    task = asyncio.create_task(runner.run_forever())
    # Wait for the first delivery after the glitch instead of a fixed sleep (flaky under load).
    await asyncio.wait_for(sink.delivered.wait(), timeout=10)
    assert flaky_clock.fail_times == 0  # the loop error really happened before the delivery
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert arp.calls >= 1
    assert len(sink.batches) == arp.calls
