from datetime import timedelta

import pytest

from homesense.core.models import DeviceObservation
from homesense.presence import radar

PHONE = "f0:18:98:a1:00:01"
LAPTOP = "3c:a9:f4:a1:00:02"
TV = "a8:23:fe:d4:00:01"
PLUG = "d8:1f:12:d4:00:02"


def _obs(ts, mac, signal=None, connection="5g"):
    return DeviceObservation(ts=ts, mac=mac, source="sim", connection=connection, signal=signal)


@pytest.fixture
def household(core, clock):
    """Anna's phone (wireless, moving) and laptop (stored kind), a wired TV, a plug without RSSI
    that went offline an hour ago."""
    now = clock.now()
    anna = core.repos.persons.create("Anna", color="#ff0000")
    repos = core.repos
    repos.devices.upsert_seen(PHONE, now, hostname="Anna-iPhone")
    repos.devices.update(PHONE, person_id=anna)
    repos.devices.upsert_seen(LAPTOP, now, hostname="box-1")
    repos.devices.update(LAPTOP, person_id=anna, kind="laptop")
    repos.devices.upsert_seen(TV, now, hostname="LG-TV")
    repos.devices.upsert_seen(PLUG, now - timedelta(hours=1), hostname="Smart-Plug")
    repos.observations.add_many(
        [
            _obs(now - timedelta(minutes=40), PHONE, -80),  # outside the 30 min trail, inside lookback
            *[_obs(now - timedelta(minutes=m), PHONE, -55) for m in (20, 10, 1)],
            _obs(now - timedelta(minutes=1), LAPTOP, -50),
            _obs(now - timedelta(minutes=1), TV, None, connection="wired"),
            _obs(now - timedelta(minutes=61), PLUG, None, connection="unknown"),
        ]
    )
    return anna


def _by_mac(response):
    assert response.status_code == 200, response.text
    return {d["mac"]: d for d in response.json()}


def test_map_requires_login(client):
    assert client.get("/api/map").status_code == 401


def test_map_page_requires_login(client):
    response = client.get("/map")
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_map_empty_database(client, auth):
    assert client.get("/api/map").json() == []


def test_map_entries(client, auth, household):
    devices = _by_mac(client.get("/api/map"))
    assert set(devices) == {PHONE, LAPTOP, TV, PLUG}

    phone = devices[PHONE]
    assert phone["name"] == "Anna-iPhone"
    assert phone["kind"] == "phone" and phone["kind_guessed"] is True
    assert phone["person_id"] == household and phone["person_name"] == "Anna"
    assert phone["color"] == "#ff0000"
    assert phone["online"] is True and phone["wired"] is False and phone["connection"] == "5g"
    assert phone["rssi"] == -55
    assert -80 < phone["rssi_smoothed"] < -55  # smoothed, still pulled by the older -80
    assert phone["distance_m"] == pytest.approx(radar.rssi_to_distance(phone["rssi_smoothed"]), abs=0.01)
    assert phone["angle_deg"] == pytest.approx(radar.angle_for_mac(PHONE), abs=0.01)
    assert len(phone["trail"]) == 3  # only the last 30 minutes
    assert set(phone["trail"][0]) == {"ts", "distance_m"}
    assert phone["last_seen"]

    assert devices[LAPTOP]["kind"] == "computer" and devices[LAPTOP]["kind_guessed"] is False

    tv = devices[TV]
    assert tv["wired"] is True and tv["distance_m"] == 0.5 and tv["rssi"] is None
    assert tv["kind"] == "tv" and tv["person_id"] is None and tv["color"] is None

    plug = devices[PLUG]
    assert plug["distance_m"] is None and plug["online"] is False and plug["kind"] == "iot"


def test_map_filters(client, auth, household):
    assert set(_by_mac(client.get("/api/map", params={"kinds": "phone,tv"}))) == {PHONE, TV}
    assert set(_by_mac(client.get("/api/map", params={"person_id": household}))) == {PHONE, LAPTOP}
    assert PLUG not in _by_mac(client.get("/api/map", params={"online_only": 1}))
    no_trail = _by_mac(client.get("/api/map", params={"trail_minutes": 0}))
    assert no_trail[PHONE]["trail"] == [] and no_trail[PHONE]["distance_m"] is not None


@pytest.mark.parametrize(
    "params", [{"kinds": "phone,toaster"}, {"trail_minutes": -1}, {"trail_minutes": 10_000}]
)
def test_map_rejects_bad_parameters(client, auth, params):
    assert client.get("/api/map", params=params).status_code == 422


def test_map_page_renders_empty(client, auth):
    response = client.get("/map")
    assert response.status_code == 200
    html = response.text
    assert "<h1>Mapa</h1>" in html
    assert "Kierunek na mapie jest umowny" in html
    assert "brak danych o sygnale" in html.lower()
    assert "/static/js/map.js" in html
    assert 'href="/map"' in html  # in the navigation


def test_map_page_renders_with_data(client, auth, household):
    html = client.get("/map").text
    assert 'value="phone"' in html and "Anna" in html  # kind filter and person select


def test_map_page_escapes_person_names(client, core, auth):
    core.repos.persons.create("<script>alert(1)</script>")
    html = client.get("/map").text
    assert "<script>alert(1)</script>" not in html


def test_map_script_served(client):
    response = client.get("/static/js/map.js")
    assert response.status_code == 200
    assert "innerHTML" not in response.text
