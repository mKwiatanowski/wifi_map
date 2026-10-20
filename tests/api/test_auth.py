import logging
import sys
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from homesense.api.auth import SETUP_TOKEN_FILE
from homesense.api.server import create_app
from homesense.security import hash_password
from tests.api.conftest import PASSWORD, USERNAME, csrf_from_html, login

LAN_HOST = ("192.168.0.50", 1234)


def _client(core, host):
    return TestClient(
        create_app(core.settings, core, start_background=False), follow_redirects=False, client=host
    )


def _someone_home(core, clock):
    pid = core.repos.persons.create("Anna")
    core.repos.presence.start_interval(pid, clock.now() - timedelta(hours=1))
    core.presence.restore()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_health_full_for_loopback(core, clock, host):
    _someone_home(core, clock)
    with _client(core, (host, 1234)) as c:
        response = c.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["people_home"] == 1
    assert [col["name"] for col in body["collectors"]] == ["simulated"]
    assert "Anna" not in response.text


@pytest.mark.parametrize("host", [LAN_HOST, ("testclient", 50000)])
def test_health_status_only_for_anonymous_lan_callers(core, clock, host):
    _someone_home(core, clock)
    with _client(core, host) as c:
        plain = c.get("/api/health")
        spoofed = c.get("/api/health", headers={"X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"})
    for response in (plain, spoofed):
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_health_full_for_logged_in_lan_caller(core, clock):
    _someone_home(core, clock)
    with _client(core, LAN_HOST) as c:
        login(c, core)
        body = c.get("/api/health").json()
        assert body["people_home"] == 1 and "collectors" in body
        # A revoked session (password changed) falls back to the status-only answer.
        core.users.set_password(USERNAME, hash_password("inne-haslo-789"))
        assert c.get("/api/health").json() == {"status": "ok"}


def test_api_requires_login(client):
    response = client.get("/api/persons")
    assert response.status_code == 401
    assert response.json()["detail"] == "Wymagane logowanie"


def test_login_page_redirects_to_setup_without_users(client):
    response = client.get("/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def _setup_token(core) -> str:
    return (core.settings.setup_token_path).read_text(encoding="utf-8").strip()


def test_setup_token_written_at_startup_without_users(client, core):
    token = _setup_token(core)
    assert len(token) >= 32
    page = client.get("/setup")
    assert page.status_code == 200
    assert '<input name="setup_token"' in page.text  # typed by the user, never pre-filled
    assert token not in page.text


def test_setup_ignores_token_query_parameter(client, core):
    token = _setup_token(core)
    page = client.get("/setup", params={"token": token})
    assert page.status_code == 200
    assert token not in page.text
    echo = client.get("/setup", params={"token": "XYZ-echo-me"})
    assert "XYZ-echo-me" not in echo.text


def test_setup_token_never_logged(core, caplog):
    with (
        caplog.at_level(logging.DEBUG),
        TestClient(create_app(core.settings, core, start_background=False)),
    ):
        token = _setup_token(core)
    assert any(SETUP_TOKEN_FILE in r.getMessage() for r in caplog.records)
    assert all(token not in r.getMessage() for r in caplog.records)


def test_setup_token_not_written_when_users_exist(core):
    core.users.create(USERNAME, hash_password(PASSWORD))
    stale = core.settings.setup_token_path
    stale.write_text("old", encoding="utf-8")
    with TestClient(create_app(core.settings, core, start_background=False), follow_redirects=False) as c:
        assert not stale.exists()
        assert c.get("/setup").status_code == 404


def test_setup_requires_setup_token(client, core):
    page = client.get("/setup")
    assert page.status_code == 200
    assert "Utwórz pierwsze konto" in page.text
    csrf = csrf_from_html(page.text)
    form = {"username": "admin", "password": PASSWORD, "password_confirm": PASSWORD, "csrf_token": csrf}

    missing = client.post("/setup", data=form)
    wrong = client.post("/setup", data={**form, "setup_token": "zgadywanie"})
    for response in (missing, wrong):
        assert response.status_code == 403
        assert "Nieprawidłowy token konfiguracji" in response.text
    assert core.users.count() == 0


def test_setup_creates_first_user_and_logs_in(client, core):
    token = _setup_token(core)
    csrf = csrf_from_html(client.get("/setup").text)
    form = {"username": "admin", "password": PASSWORD, "password_confirm": PASSWORD, "setup_token": token}

    assert client.post("/setup", data=form).status_code == 403  # no CSRF token
    assert core.users.count() == 0

    response = client.post("/setup", data={**form, "csrf_token": csrf})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert core.users.count() == 1
    assert client.get("/api/me").json()["username"] == "admin"
    assert not (core.settings.setup_token_path).exists()

    # Once a user exists the setup page is gone.
    assert client.get("/setup").status_code == 404
    fresh = client.get("/api/me").json()["csrf_token"]
    again = client.post("/setup", data={**form, "username": "intruder", "csrf_token": fresh})
    assert again.status_code == 404
    assert core.users.get_by_username("intruder") is None


def test_setup_validates_input(client, core):
    token = _setup_token(core)
    csrf = csrf_from_html(client.get("/setup").text)
    base = {"csrf_token": csrf, "setup_token": token, "username": "admin"}
    response = client.post("/setup", data={**base, "password": PASSWORD, "password_confirm": "inne"})
    assert response.status_code == 400
    assert "Hasła nie są takie same" in response.text
    assert token not in response.text  # the setup token is never rendered back
    short = {**base, "password": "krótkie", "password_confirm": "krótkie"}
    assert "co najmniej 8 znaków" in client.post("/setup", data=short).text
    empty = {**base, "username": "  ", "password": PASSWORD, "password_confirm": PASSWORD}
    assert client.post("/setup", data=empty).status_code == 400
    assert core.users.count() == 0


def test_wrong_credentials_do_not_reveal_username(client, core):
    core.users.create(USERNAME, hash_password(PASSWORD))
    page = client.get("/login")
    assert page.status_code == 200 and "Zaloguj się" in page.text
    token = csrf_from_html(page.text)

    wrong_pw = client.post("/login", data={"username": USERNAME, "password": "zle", "csrf_token": token})
    no_user = client.post("/login", data={"username": "nikt", "password": "zle", "csrf_token": token})
    for response in (wrong_pw, no_user):
        assert response.status_code == 401
        assert "Nieprawidłowa nazwa użytkownika lub hasło" in response.text
    assert client.get("/api/me").status_code == 401


def test_login_requires_csrf(client, core):
    core.users.create(USERNAME, hash_password(PASSWORD))
    client.get("/login")
    response = client.post("/login", data={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 403
    assert client.get("/api/me").status_code == 401


def test_login_rotates_session_and_logout_clears_it(client, core):
    core.users.create(USERNAME, hash_password(PASSWORD))
    pre_login_token = csrf_from_html(client.get("/login").text)
    token = login(client, core)
    assert token != pre_login_token
    assert client.get("/api/persons").status_code == 200
    # Logged-in users skip the login page.
    assert client.get("/login").headers["location"] == "/"

    assert client.post("/logout", data={"csrf_token": "falszywy"}).status_code == 403
    response = client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert client.get("/api/persons").status_code == 401


def test_mutations_require_csrf_header(client, core, auth):
    assert client.post("/api/persons", json={"name": "Anna"}).status_code == 403
    assert (
        client.post("/api/persons", json={"name": "Anna"}, headers={"X-CSRF-Token": "x"}).status_code == 403
    )
    response = client.post("/api/persons", json={"name": "Anna"}, headers=auth)
    assert response.status_code == 201
    assert [p.name for p in core.repos.persons.list()] == ["Anna"]


def test_session_revoked_when_user_deleted(client, core, auth):
    assert client.get("/api/persons").status_code == 200
    core.users.delete(USERNAME)
    assert client.get("/api/persons").status_code == 401
    # The stale session was cleared, so it stays logged out.
    assert client.get("/grafana/").headers["location"] == "/login"


def test_session_revoked_when_password_changed(client, core, auth):
    assert client.get("/api/persons").status_code == 200
    core.users.set_password(USERNAME, hash_password("nowe-haslo-456"))
    assert client.get("/api/persons").status_code == 401
    # Logging in again with the new password works.
    token = csrf_from_html(client.get("/login").text)
    form = {"username": USERNAME, "password": "nowe-haslo-456", "csrf_token": token}
    assert client.post("/login", data=form).status_code == 303
    assert client.get("/api/persons").status_code == 200


def test_setup_token_expires_after_an_hour(client, core, clock):
    token = _setup_token(core)
    csrf = csrf_from_html(client.get("/setup").text)
    form = {
        "username": "admin",
        "password": PASSWORD,
        "password_confirm": PASSWORD,
        "setup_token": token,
        "csrf_token": csrf,
    }
    clock.advance(timedelta(hours=1, seconds=1))
    response = client.post("/setup", data=form)
    assert response.status_code == 403
    assert "Token konfiguracji wygasł" in response.text and "Uruchom ponownie" in response.text
    assert core.users.count() == 0


def test_setup_token_still_valid_just_before_expiry(client, core, clock):
    token = _setup_token(core)
    csrf = csrf_from_html(client.get("/setup").text)
    clock.advance(timedelta(minutes=59))
    form = {"username": "admin", "password": PASSWORD, "password_confirm": PASSWORD}
    response = client.post("/setup", data={**form, "setup_token": token, "csrf_token": csrf})
    assert response.status_code == 303
    assert not (core.settings.setup_token_path).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_setup_token_file_is_private(client, core):
    mode = (core.settings.setup_token_path).stat().st_mode & 0o777
    assert mode == 0o600


def test_setup_token_lives_in_private_auth_folder(client, core):
    assert core.settings.setup_token_path.parent == core.settings.auth_dir
    assert core.settings.setup_token_path.exists()
    assert not (core.settings.data_dir / SETUP_TOKEN_FILE).exists()


def test_legacy_setup_token_in_data_dir_is_removed(core):
    legacy = core.settings.data_dir / SETUP_TOKEN_FILE
    legacy.write_text("old-token", encoding="utf-8")
    with TestClient(create_app(core.settings, core, start_background=False)):
        assert not legacy.exists()
