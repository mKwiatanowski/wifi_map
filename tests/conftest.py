from datetime import UTC, datetime

import pytest

from homesense.core.clock import FakeClock
from homesense.core.config import Settings


@pytest.fixture
def utc_start() -> datetime:
    return datetime(2026, 10, 19, 6, 0, tzinfo=UTC)


@pytest.fixture
def clock(utc_start) -> FakeClock:
    return FakeClock(utc_start)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, mode="simulated", data_dir=tmp_path)


@pytest.fixture
def db(tmp_path):
    from homesense.storage.db import Database

    database = Database(tmp_path / "homesense.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def auth_db(tmp_path):
    from homesense.storage.auth import open_auth_db

    database = open_auth_db(tmp_path / "auth.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def repos(db, clock):
    from homesense.storage.repositories import Repos

    return Repos.create(db, clock=clock)
