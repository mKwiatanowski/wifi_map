from datetime import timedelta

from homesense.core.models import DeviceObservation
from homesense.presence import coverage

PHONE = "f0:18:98:a1:00:01"
TV = "a8:23:fe:d4:00:01"
PLUG = "d8:1f:12:d4:00:02"


def _obs(ts, mac, signal, connection="5g"):
    return DeviceObservation(ts=ts, mac=mac, source="router", connection=connection, signal=signal)


def test_signal_by_hour_weights_and_uses_local_time(clock):
    # 06:00 and 06:30 UTC -> 08:00 hour in Warsaw (CEST); 18:00 UTC -> 20:00.
    t = clock.now()
    rows = [
        (t, PHONE, -50.0, 3),
        (t - timedelta(days=1), PHONE, -70.0, 1),
        (t + timedelta(hours=12), PHONE, -60.0, 2),
    ]
    assert coverage.signal_by_hour(rows, "Europe/Warsaw") == {PHONE: {8: (-55.0, 4), 20: (-60.0, 2)}}


def test_signal_hourly_repository(repos, clock):
    t = clock.now()
    repos.observations.add_many(
        [_obs(t, PHONE, -50), _obs(t + timedelta(minutes=30), PHONE, -60), _obs(t, TV, None, "wired")]
    )
    assert repos.observations.signal_hourly(t - timedelta(hours=1), t + timedelta(hours=1)) == [
        (t, PHONE, -55.0, 2)
    ]


def test_signal_hours_api_grid_and_kind_filter(client, core, clock, auth):
    now = clock.now()
    core.repos.devices.upsert_seen(PHONE, now, hostname="Anna-iPhone")
    core.repos.devices.upsert_seen(PLUG, now, hostname="Smart-Plug")
    core.repos.devices.update(PLUG, kind="iot")
    core.repos.devices.upsert_seen(TV, now, hostname="LG-TV")
    core.repos.observations.add_many(
        [
            _obs(now - timedelta(minutes=10), PHONE, -50),
            _obs(now - timedelta(days=8), PHONE, -90),  # outside 7 days
            _obs(now - timedelta(hours=2), PLUG, -70, "2g"),
            _obs(now - timedelta(minutes=5), TV, None, "wired"),
        ]
    )
    body = client.get("/api/coverage/signal-hours").json()
    assert body["days"] == 7
    devices = {d["mac"]: d for d in body["devices"]}
    assert set(devices) == {PHONE, PLUG}  # the wired TV has no RSSI
    phone = devices[PHONE]
    assert phone["kind"] == "phone" and len(phone["cells"]) == 24
    assert phone["cells"][7] == {"hour": 7, "rssi": -50.0, "samples": 1}  # 05:50 UTC = 07:50 local
    assert sum(c is not None for c in phone["cells"]) == 1

    only_iot = client.get("/api/coverage/signal-hours", params={"kinds": "iot"}).json()
    assert [d["mac"] for d in only_iot["devices"]] == [PLUG]
    assert client.get("/api/coverage/signal-hours", params={"kinds": "toaster"}).status_code == 422
    assert client.get("/api/coverage/signal-hours", params={"days": 0}).status_code == 422


def test_signal_hours_requires_login(client):
    assert client.get("/api/coverage/signal-hours").status_code == 401


def test_coverage_page_has_signal_tab(client, auth):
    html = client.get("/coverage").text
    assert 'data-tab="signal"' in html and 'data-panel="signal"' in html
    assert "Sygnał w czasie" in html and 'id="signal-filters"' in html
