from homesense.core.config import Settings


def test_env_prefix(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMESENSE_MODE", "simulated")
    monkeypatch.setenv("HOMESENSE_DATA_DIR", str(tmp_path))
    s = Settings(_env_file=None)
    assert s.mode == "simulated" and s.db_path == tmp_path.resolve() / "homesense.db"


def test_data_dir_is_resolved_once(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    s = Settings(_env_file=None, data_dir="rel")
    assert s.data_dir == (tmp_path / "rel").resolve() and s.data_dir.is_absolute()
    assert s.db_path == s.data_dir / "homesense.db" and s.auth_db_path == s.data_dir / "auth" / "auth.db"
    assert s.auth_dir == s.data_dir / "auth" and s.setup_token_path == s.auth_dir / "setup-token.txt"


def test_defaults():
    s = Settings(_env_file=None)
    assert s.port == 8080 and s.departure_timeout_min == 15 and s.timezone == "Europe/Warsaw"


def test_auth_files_are_in_private_auth_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMESENSE_DATA_DIR", str(tmp_path))
    s = Settings(_env_file=None)
    auth = tmp_path.resolve() / "auth"
    assert s.auth_dir == auth
    assert s.auth_db_path == auth / "auth.db" and s.auth_db_path.parent == s.data_dir / "auth"
    assert s.setup_token_path == auth / "setup-token.txt"
    assert s.db_path.parent == s.data_dir  # homesense.db (read by Grafana) stays outside
