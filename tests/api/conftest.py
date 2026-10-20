import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from homesense.api.server import create_app
from homesense.app import HomeSenseApp
from homesense.core.config import Settings
from homesense.security import hash_password

USERNAME = "admin"
PASSWORD = "tajne-haslo-123"
GRAFANA_URL = "http://grafana.test"

_CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_from_html(html: str) -> str:
    match = _CSRF_FIELD.search(html)
    assert match, "csrf_token hidden field missing"
    return match.group(1)


@pytest.fixture
def core(tmp_path, clock):
    settings = Settings(
        _env_file=None,
        mode="simulated",
        data_dir=tmp_path,
        windows_toast=False,
        secret_key="test-secret-key",
        grafana_url=GRAFANA_URL,
    )
    app = HomeSenseApp(settings, clock)
    app.initialize()
    yield app
    asyncio.run(app.stop())


@pytest.fixture
def client(core):
    with TestClient(create_app(core.settings, core, start_background=False), follow_redirects=False) as c:
        yield c


def login(client: TestClient, core: HomeSenseApp) -> str:
    """Creates the admin user (if needed), logs in and returns the session's CSRF token."""
    if core.users.get_by_username(USERNAME) is None:
        core.users.create(USERNAME, hash_password(PASSWORD))
    token = csrf_from_html(client.get("/login").text)
    response = client.post("/login", data={"username": USERNAME, "password": PASSWORD, "csrf_token": token})
    assert response.status_code == 303, response.text
    me = client.get("/api/me")
    assert me.status_code == 200
    return me.json()["csrf_token"]


@pytest.fixture
def auth(client, core) -> dict[str, str]:
    """Logged-in client; returns the CSRF header for mutating requests."""
    return {"X-CSRF-Token": login(client, core)}
