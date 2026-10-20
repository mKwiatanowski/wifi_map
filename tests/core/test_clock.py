from datetime import UTC, datetime, timedelta

import pytest

from homesense.core.clock import FakeClock, SystemClock


def test_fake_clock_advance():
    c = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    c.advance(timedelta(minutes=5))
    assert c.now() == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)


def test_fake_clock_set():
    c = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    c.set(datetime(2027, 1, 1, tzinfo=UTC))
    assert c.now().year == 2027


def test_system_clock_aware():
    assert SystemClock().now().utcoffset() == timedelta(0)


def test_fake_clock_init_naive_rejected():
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 1, 1))


def test_fake_clock_set_naive_rejected():
    c = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError):
        c.set(datetime(2027, 1, 1))
    assert c.now() == datetime(2026, 1, 1, tzinfo=UTC)
