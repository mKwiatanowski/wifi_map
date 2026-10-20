import time

from homesense import cli
from homesense.patterns.profiles import PERSON_HOME
from homesense.storage.db import Database
from homesense.storage.repositories import Repos


def test_simulate_eight_days(tmp_path, monkeypatch, capsys):
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)  # no developer .env
    data_dir = tmp_path / "data"
    monkeypatch.setenv("HOMESENSE_DATA_DIR", str(data_dir))
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC"):
        monkeypatch.delenv(f"HOMESENSE_{name}", raising=False)

    started = time.perf_counter()
    assert cli.main(["simulate", "--days", "8", "--step-s", "300"]) == 0
    assert time.perf_counter() - started < 60
    assert "new_device" in capsys.readouterr().out

    db = Database(data_dir / "homesense.db")
    repos = Repos.create(db)
    persons = {p.name: p for p in repos.persons.list()}
    assert set(persons) == {"Anna", "Piotr", "Ola"}
    assert repos.profiles.get(PERSON_HOME, str(persons["Anna"].id))
    anna_devices = repos.devices.for_person(persons["Anna"].id)
    assert any(d.counts_for_presence for d in anna_devices)

    guest_alerts = [
        a for a in repos.alerts.list(limit=10_000) if a.kind == "new_device" and a.data.get("random_mac")
    ]
    assert guest_alerts
    guest = repos.devices.get(guest_alerts[0].mac)
    assert guest.hostname == "Guest-Phone"

    # Re-running on the same data dir does not duplicate persons.
    assert cli.main(["simulate", "--days", "1", "--step-s", "900"]) == 0
    assert len(repos.persons.list()) == 3
    db.close()
