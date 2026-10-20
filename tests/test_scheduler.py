import asyncio

import pytest

from homesense.scheduler import Periodic


async def test_periodic_keeps_running_after_exceptions(caplog):
    calls = 0

    async def job():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("boom")

    task = asyncio.create_task(Periodic("job", 0.001, job).run_forever())
    for _ in range(200):
        await asyncio.sleep(0.001)
        if calls >= 4:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 4
    assert "periodic job job failed" in caplog.text


async def test_periodic_delayed_first_run():
    calls = 0

    async def job():
        nonlocal calls
        calls += 1

    task = asyncio.create_task(Periodic("job", 3600, job, run_immediately=False).run_forever())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 0
