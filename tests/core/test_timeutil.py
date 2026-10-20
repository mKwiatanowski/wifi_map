from datetime import UTC, datetime

import pytest

from homesense.core.timeutil import floor_hour, to_local, ts_from_db, ts_to_db


def test_roundtrip():
    dt = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert ts_to_db(dt) == "2026-10-25 01:30:00"
    assert ts_from_db(ts_to_db(dt)) == dt


def test_naive_rejected():
    with pytest.raises(ValueError):
        ts_to_db(datetime(2026, 1, 1))


def test_to_local_dst():
    assert to_local(datetime(2026, 10, 25, 0, 30, tzinfo=UTC), "Europe/Warsaw").hour == 2
    assert to_local(datetime(2026, 10, 25, 1, 30, tzinfo=UTC), "Europe/Warsaw").hour == 2


def test_floor_hour():
    assert floor_hour(datetime(2026, 1, 1, 5, 59, 59, tzinfo=UTC)) == datetime(2026, 1, 1, 5, tzinfo=UTC)


def test_to_local_naive_rejected():
    with pytest.raises(ValueError):
        to_local(datetime(2026, 1, 1), "Europe/Warsaw")


def test_floor_hour_naive_rejected():
    with pytest.raises(ValueError):
        floor_hour(datetime(2026, 1, 1, 5, 30))
