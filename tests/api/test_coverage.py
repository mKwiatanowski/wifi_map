from datetime import timedelta

import pytest

from homesense.core.models import DeviceObservation

PHONE = "f0:18:98:a1:00:01"
PLAN = {
    "width_m": 10,
    "height_m": 8,
    "router": {"x": 1, "y": 1},
    "rooms": [
        {"name": "Salon", "x": 0, "y": 0, "w": 5, "h": 4},
        {"name": "Kuchnia", "x": 5, "y": 0, "w": 5, "h": 4},
    ],
}


@pytest.fixture
def phone(core, clock):
    core.repos.devices.upsert_seen(PHONE, clock.now(), hostname="Anna-iPhone")
    return PHONE


def _obs(ts, signal, connection="5g"):
    return DeviceObservation(ts=ts, mac=PHONE, source="router", connection=connection, signal=signal)


def test_coverage_requires_login(client):
    assert client.get("/api/coverage").status_code == 401
    assert client.get("/coverage").status_code == 303


def test_plan_round_trip_and_validation(client, auth):
    assert client.get("/api/coverage").json() == {"plan": None, "points": []}
    saved = client.put("/api/coverage/plan", json=PLAN, headers=auth)
    assert saved.status_code == 200 and saved.json() == {
        **PLAN,
        "width_m": 10.0,
        "height_m": 8.0,
        "router": {"x": 1.0, "y": 1.0},
        "rooms": [{k: (float(v) if k != "name" else v) for k, v in r.items()} for r in PLAN["rooms"]],
    }
    assert client.get("/api/coverage").json()["plan"]["rooms"][1]["name"] == "Kuchnia"

    outside = {**PLAN, "rooms": [{"name": "X", "x": 8, "y": 0, "w": 5, "h": 1}]}
    assert client.put("/api/coverage/plan", json=outside, headers=auth).status_code == 422
    assert (
        client.put("/api/coverage/plan", json={**PLAN, "router": {"x": 11, "y": 1}}, headers=auth).status_code
        == 422
    )
    assert client.put("/api/coverage/plan", json={**PLAN, "width_m": 0}, headers=auth).status_code == 422
    assert client.put("/api/coverage/plan", json=PLAN).status_code == 403  # CSRF


def test_measurement_median_band_and_states(client, core, clock, auth, phone):
    client.put("/api/coverage/plan", json=PLAN, headers=auth)
    created = client.post("/api/coverage/points", json={"mac": PHONE, "x": 4, "y": 3}, headers=auth)
    assert created.status_code == 201, created.text
    point = created.json()
    assert point["status"] == "measuring" and point["remaining_s"] == 60 and point["device"] == "Anna-iPhone"
    start = clock.now()
    core.repos.observations.add_many(
        [
            _obs(start - timedelta(seconds=5), -30),  # before the window: ignored
            _obs(start + timedelta(seconds=10), -60),
            _obs(start + timedelta(seconds=30), -50),
            _obs(start + timedelta(seconds=50), -70, connection="2g"),
            _obs(start + timedelta(seconds=40), None),
        ]
    )
    clock.advance(timedelta(seconds=20))
    live = client.get(f"/api/coverage/points/{point['id']}").json()
    assert live["status"] == "measuring" and live["remaining_s"] == 40
    # Another measurement may not start while one is running.
    assert (
        client.post("/api/coverage/points", json={"mac": PHONE, "x": 1, "y": 1}, headers=auth).status_code
        == 409
    )

    clock.advance(timedelta(seconds=41))
    done = client.get(f"/api/coverage/points/{point['id']}").json()
    assert done["status"] == "ok" and done["rssi"] == -60.0 and done["samples"] == 3 and done["band"] == "5"
    assert core.repos.coverage.get_point(point["id"]).done


def test_point_without_samples_is_invalid(client, clock, auth, phone):
    client.put("/api/coverage/plan", json=PLAN, headers=auth)
    point = client.post(
        "/api/coverage/points", json={"mac": PHONE, "x": 4, "y": 3, "window_s": 30}, headers=auth
    )
    clock.advance(timedelta(seconds=31))
    body = client.get(f"/api/coverage/points/{point.json()['id']}").json()
    assert body["status"] == "no_samples" and body["rssi"] is None and body["band"] == "unknown"


def test_point_validation(client, core, auth, phone):
    body = {"mac": PHONE, "x": 4, "y": 3}
    assert client.post("/api/coverage/points", json=body, headers=auth).status_code == 409  # no plan yet
    client.put("/api/coverage/plan", json=PLAN, headers=auth)
    assert client.post("/api/coverage/points", json={**body, "x": 11}, headers=auth).status_code == 422
    assert client.post("/api/coverage/points", json={**body, "mac": "bad"}, headers=auth).status_code == 422
    unknown = {**body, "mac": "02:00:00:00:00:99"}
    assert client.post("/api/coverage/points", json=unknown, headers=auth).status_code == 404
    assert client.post("/api/coverage/points", json={**body, "window_s": 5}, headers=auth).status_code == 422
    import asyncio

    asyncio.run(core.monitoring.pause(None))
    assert client.post("/api/coverage/points", json=body, headers=auth).status_code == 409


def test_delete_point_and_clear_survey(client, core, clock, auth, phone):
    client.put("/api/coverage/plan", json=PLAN, headers=auth)
    ids = []
    for x in (1, 2):
        ids.append(
            client.post("/api/coverage/points", json={"mac": PHONE, "x": x, "y": 1}, headers=auth).json()[
                "id"
            ]
        )
        clock.advance(timedelta(seconds=61))
    assert client.delete(f"/api/coverage/points/{ids[0]}", headers=auth).status_code == 204
    assert client.delete(f"/api/coverage/points/{ids[0]}", headers=auth).status_code == 404
    assert [p["id"] for p in client.get("/api/coverage").json()["points"]] == [ids[1]]
    assert client.delete("/api/coverage/points", headers=auth).status_code == 204
    assert client.get("/api/coverage").json()["points"] == []
    assert client.get("/api/coverage").json()["plan"] is not None  # the plan stays


def test_survey_is_exempt_from_retention(core, clock, auth, client, phone):
    from homesense.storage.retention import purge_old

    client.put("/api/coverage/plan", json=PLAN, headers=auth)
    client.post("/api/coverage/points", json={"mac": PHONE, "x": 1, "y": 1}, headers=auth)
    clock.advance(timedelta(days=90))
    purge_old(core.repos, clock.now(), 30)
    assert len(core.repos.coverage.points()) == 1 and core.repos.coverage.get_plan() is not None


def test_coverage_page_renders_devices_and_script(client, auth, phone):
    html = client.get("/coverage").text
    assert "<h1>Zasięg Wi-Fi</h1>" in html
    assert "/static/js/coverage.js" in html and 'href="/coverage"' in html
    assert f'value="{PHONE}"' in html and "Anna-iPhone" in html
    assert client.get("/static/js/coverage.js").status_code == 200
    assert "/static/js/coverage.js" in client.get("/sw.js").text


async def test_simulated_survey_emits_log_distance_rssi(core, clock, auth, client, phone):
    """In simulated mode the measured device reports RSSI from the router-to-point distance."""
    await core.step()  # simulated devices appear
    client.put("/api/coverage/plan", json=PLAN, headers=auth)
    near = client.post("/api/coverage/points", json={"mac": PHONE, "x": 1.5, "y": 1}, headers=auth).json()
    for _ in range(3):
        await core.step()
        clock.advance(timedelta(seconds=30))
    near = client.get(f"/api/coverage/points/{near['id']}").json()
    far = client.post("/api/coverage/points", json={"mac": PHONE, "x": 10, "y": 8}, headers=auth).json()
    for _ in range(3):
        await core.step()
        clock.advance(timedelta(seconds=30))
    far = client.get(f"/api/coverage/points/{far['id']}").json()
    assert near["status"] == far["status"] == "ok"
    # P0 = -40 dBm, n = 3: 0.5 m -> ~-31, 11.4 m -> ~-72 (+/- 2 dB noise)
    assert near["rssi"] >= -36 and -76 <= far["rssi"] <= -68
    assert near["band"] == "5"
