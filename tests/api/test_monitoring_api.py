from datetime import timedelta

import pytest


def test_monitoring_requires_login(client):
    assert client.get("/api/monitoring").status_code == 401
    assert client.post("/api/monitoring/pause", json={"hours": 1}).status_code == 401


def test_monitoring_mutations_need_csrf(client, auth):
    assert client.post("/api/monitoring/pause", json={"hours": 1}).status_code == 403
    assert client.post("/api/monitoring/resume").status_code == 403


def test_pause_and_resume(client, core, clock, auth):
    assert client.get("/api/monitoring").json() == {
        "paused": False,
        "since": None,
        "until": None,
        "resumed_at": None,
    }
    body = client.post("/api/monitoring/pause", json={"hours": "8"}, headers=auth).json()
    assert body["paused"] and body["until"] == (clock.now() + timedelta(hours=8)).isoformat()
    assert core.monitoring.is_paused()

    body = client.post("/api/monitoring/pause", json={}, headers=auth).json()
    assert body["paused"] and body["until"] is None  # until resumed

    body = client.post("/api/monitoring/resume", headers=auth).json()
    assert not body["paused"] and body["resumed_at"] == clock.now().isoformat()
    assert not core.monitoring.is_paused()


@pytest.mark.parametrize("hours", [0, 2, 25, -1, "x"])
def test_pause_rejects_other_durations(client, auth, hours):
    assert client.post("/api/monitoring/pause", json={"hours": hours}, headers=auth).status_code == 422


def test_health_shows_pause_only_with_details(client, core, auth):
    client.post("/api/monitoring/pause", json={"hours": 1}, headers=auth)
    body = client.get("/api/health").json()
    assert body["monitoring"]["paused"] is True
    assert all(c["paused"] for c in body["collectors"])


def test_anonymous_health_is_status_only_while_paused(client, core):
    import asyncio

    asyncio.run(core.monitoring.pause(None))
    body = client.get("/api/health", headers={}).json()
    # TestClient's peer is "testclient" (not loopback), so anonymous callers get the status only.
    assert body == {"status": "ok"}


def test_header_switch_on_every_page(client, core, clock, auth):
    html = client.get("/").text
    assert "Monitoring: włączony" in html and "data-monitoring-box" in html
    client.post("/api/monitoring/pause", json={"hours": 1}, headers=auth)
    html = client.get("/devices").text
    assert "Monitoring: wstrzymany do" in html and "Wznów" in html
    settings = client.get("/settings").text
    assert 'id="monitoring"' in settings and "wstrzymany (monitoring)" in settings
