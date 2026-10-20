import httpx
import pytest
import respx

from homesense import cli
from homesense.core.config import Settings
from homesense.security import verify_password
from homesense.storage.auth import open_auth_db
from homesense.storage.db import Database
from homesense.storage.repositories import UserRepo


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated environment: no developer .env (cwd is a temp dir), simulated mode, temp data dir."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("HOMESENSE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("HOMESENSE_MODE", "simulated")
    monkeypatch.setenv("HOMESENSE_WINDOWS_TOAST", "false")
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC", "SECRET_KEY"):
        monkeypatch.delenv(f"HOMESENSE_{name}", raising=False)
    return data_dir


def _users(data_dir) -> tuple[Database, UserRepo]:
    db = open_auth_db(data_dir / "auth" / "auth.db")
    return db, UserRepo(db)


def test_init_db(env, capsys):
    assert cli.main(["init-db"]) == 0
    assert (env / "homesense.db").exists() and (env / "auth" / "auth.db").exists()
    assert not (env / "auth.db").exists()
    assert "schema version" in capsys.readouterr().out


def test_create_user_then_update_password(env):
    assert cli.main(["create-user", "admin", "--password", "x"]) == 0
    db, users = _users(env)
    _, username, stored = users.get_by_username("admin")
    assert username == "admin" and verify_password("x", stored)

    assert cli.main(["create-user", "admin", "--password", "y"]) == 0
    _, _, stored = users.get_by_username("admin")
    assert verify_password("y", stored) and not verify_password("x", stored)
    assert users.count() == 1
    db.close()


def test_create_user_prompts_for_password(env, monkeypatch):
    answers = iter(["pw1", "pw1"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["create-user", "anna"]) == 0
    db, users = _users(env)
    assert verify_password("pw1", users.get_by_username("anna")[2])
    db.close()


def test_create_user_rejects_mismatched_prompt(env, monkeypatch):
    answers = iter(["pw1", "pw2"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["create-user", "anna"]) == 1


def test_serve_refuses_live_mode_without_secret_key(env, monkeypatch, capsys):
    monkeypatch.setenv("HOMESENSE_MODE", "live")
    monkeypatch.setattr(cli, "_run_server", lambda settings: pytest.fail("server must not start"))
    assert cli.main(["serve"]) == 2
    assert "HOMESENSE_SECRET_KEY" in capsys.readouterr().err


def test_serve_simulated_warns_and_starts(env, monkeypatch, caplog):
    started = []
    monkeypatch.setenv("HOMESENSE_MODE", "live")
    monkeypatch.setattr(cli, "_run_server", started.append)
    assert cli.main(["serve", "--simulated", "--port", "9999", "--host", "127.0.0.1"]) == 0
    (settings,) = started
    assert settings.mode == "simulated" and settings.port == 9999 and settings.host == "127.0.0.1"
    assert "HOMESENSE_SECRET_KEY" in caplog.text


def test_run_server_never_trusts_proxy_headers(env, monkeypatch):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    settings = Settings(_env_file=None, mode="simulated", data_dir=env, host="127.0.0.1", port=9999)
    cli._run_server(settings)
    (kwargs,) = calls
    assert kwargs["proxy_headers"] is False
    assert kwargs["host"] == "127.0.0.1" and kwargs["port"] == 9999


@respx.mock(assert_all_called=False)
async def test_doctor_simulated_skips_router_and_adguard(respx_mock, env):
    respx_mock.get("http://127.0.0.1:3000/api/health").mock(return_value=httpx.Response(200, json={}))
    settings = Settings(_env_file=None, mode="simulated", data_dir=env)
    async with httpx.AsyncClient() as http:
        results = {name: status for name, status, _ in await cli.run_checks(settings, http)}
    assert results == {"database": "OK", "router": "SKIP", "adguard": "SKIP", "grafana": "OK"}


@respx.mock(assert_all_called=False)
async def test_doctor_live_reports_failures(respx_mock, env, monkeypatch):
    respx_mock.get("http://127.0.0.1:3001/control/status").mock(return_value=httpx.Response(200, json={}))
    respx_mock.get("http://127.0.0.1:3000/api/health").mock(side_effect=httpx.ConnectError("refused"))

    def bad_login(settings):
        raise RuntimeError("bad password")

    monkeypatch.setattr(cli, "_router_login", bad_login)
    settings = Settings(_env_file=None, mode="live", data_dir=env)
    async with httpx.AsyncClient() as http:
        results = {name: status for name, status, _ in await cli.run_checks(settings, http)}
    assert results == {"database": "OK", "router": "FAIL", "adguard": "OK", "grafana": "FAIL"}


def test_doctor_exit_code(env, monkeypatch, capsys):
    async def fake_checks(settings, http):
        return [("database", "OK", "v1"), ("grafana", "FAIL", "down")]

    monkeypatch.setattr(cli, "run_checks", fake_checks)
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "grafana" in out and "FAIL" in out

    async def ok_checks(settings, http):
        return [("database", "OK", "v1"), ("router", "SKIP", "simulated")]

    monkeypatch.setattr(cli, "run_checks", ok_checks)
    assert cli.main(["doctor"]) == 0
