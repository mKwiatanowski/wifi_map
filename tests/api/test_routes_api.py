import json
from datetime import timedelta

import httpx
import pytest
import respx

from homesense.core.models import Alert, DeviceObservation
from homesense.core.timeutil import floor_hour
from homesense.network.adguard import AdGuardClient

PHONE = "f0:18:98:a1:00:01"
ADGUARD = "http://adguard.test"


# --- presence / persons ---


def test_presence_lists_persons_with_state(client, core, clock, auth):
    anna = core.repos.persons.create("Anna", color="#ff0000")
    core.repos.persons.create("Piotr")
    since = clock.now() - timedelta(hours=2)
    core.repos.presence.start_interval(anna, since)
    core.presence.restore()
    body = client.get("/api/presence").json()
    assert body == [
        {
            "person_id": anna,
            "name": "Anna",
            "color": "#ff0000",
            "home": True,
            "since": since.isoformat(),
            "via": None,
        },
        {"person_id": anna + 1, "name": "Piotr", "color": None, "home": False, "since": None, "via": None},
    ]


def test_person_crud_and_validation(client, core, auth):
    created = client.post("/api/persons", json={"name": " Anna ", "color": "#00aa00"}, headers=auth)
    assert created.status_code == 201
    person = created.json()
    assert person["name"] == "Anna" and person["privacy_mode"] == "full"
    pid = person["id"]

    assert client.post("/api/persons", json={"name": "Anna"}, headers=auth).status_code == 409
    assert client.post("/api/persons", json={"name": ""}, headers=auth).status_code == 422
    assert client.post("/api/persons", json={"name": "X", "color": "red"}, headers=auth).status_code == 422
    bad_mode = {"name": "X", "privacy_mode": "none"}
    assert client.post("/api/persons", json=bad_mode, headers=auth).status_code == 422

    patched = client.patch(f"/api/persons/{pid}", json={"privacy_mode": "stats_only"}, headers=auth)
    assert patched.status_code == 200 and patched.json()["privacy_mode"] == "stats_only"
    assert client.patch("/api/persons/999", json={"name": "Y"}, headers=auth).status_code == 404
    assert [p["name"] for p in client.get("/api/persons").json()] == ["Anna"]

    assert client.delete(f"/api/persons/{pid}", headers=auth).status_code == 204
    assert client.delete(f"/api/persons/{pid}", headers=auth).status_code == 404
    assert core.repos.persons.list() == []


def test_person_profile_and_timeline(client, core, clock, auth):
    pid = core.repos.persons.create("Anna")
    core.repos.profiles.replace("person_home", str(pid), {(0, 7): (0.5, 3)}, clock.now())
    profile = client.get(f"/api/persons/{pid}/profile").json()
    assert profile["home"] == [[0, 7, 0.5, 3]]
    assert set(profile["typical"]) == {str(wd) for wd in range(7)}
    assert client.get("/api/persons/999/profile").status_code == 404

    # 2026-10-19 local (Europe/Warsaw, UTC+2) runs 2026-10-18 22:00 .. 2026-10-19 22:00 UTC.
    now = clock.now()  # 06:00 UTC
    core.repos.presence.start_interval(pid, now - timedelta(hours=10))  # 20:00 UTC on the 18th
    core.repos.presence.close_interval(pid, now - timedelta(hours=4))
    core.repos.presence.start_interval(pid, now - timedelta(hours=1))
    body = client.get(f"/api/persons/{pid}/timeline", params={"date": "2026-10-19"}).json()
    assert body["date"] == "2026-10-19"
    assert body["intervals"] == [
        {"start": "2026-10-18T22:00:00+00:00", "end": "2026-10-19T02:00:00+00:00", "open": False},
        {"start": "2026-10-19T05:00:00+00:00", "end": "2026-10-19T06:00:00+00:00", "open": True},
    ]
    assert client.get(f"/api/persons/{pid}/timeline").json()["date"] == "2026-10-19"
    assert client.get(f"/api/persons/{pid}/timeline", params={"date": "19.10"}).status_code == 422


# --- devices ---


def test_devices_filter_and_patch_assigns_person(client, core, clock, auth):
    now = clock.now()
    core.repos.devices.upsert_seen(PHONE, now, ip="192.168.0.23", hostname="anna-phone")
    core.repos.devices.upsert_seen("aa:bb:cc:dd:ee:02", now - timedelta(days=2))
    pid = core.repos.persons.create("Anna")

    assert [d["mac"] for d in client.get("/api/devices").json()] == ["aa:bb:cc:dd:ee:02", PHONE]
    online = client.get("/api/devices", params={"filter": "online"}).json()
    assert [d["mac"] for d in online] == [PHONE] and online[0]["online"] is True
    assert client.get("/api/devices", params={"filter": "bogus"}).status_code == 422

    change = {"person_id": pid, "label": "Telefon Anny", "counts_for_presence": True}
    response = client.patch("/api/devices/F0-18-98-A1-00-01", json=change, headers=auth)
    assert response.status_code == 200
    device = response.json()
    assert device["person_id"] == pid and device["person_name"] == "Anna"
    assert device["display_name"] == "Telefon Anny" and device["counts_for_presence"] is True
    stored = core.repos.devices.get(PHONE)
    assert stored.person_id == pid and stored.label == "Telefon Anny"
    assert [d["mac"] for d in client.get("/api/devices", params={"filter": "unassigned"}).json()] == [
        "aa:bb:cc:dd:ee:02"
    ]

    unassign = client.patch(f"/api/devices/{PHONE}", json={"person_id": None}, headers=auth)
    assert unassign.json()["person_id"] is None
    assert client.patch("/api/devices/nie-mac", json={}, headers=auth).status_code == 422
    assert client.patch("/api/devices/aa:aa:aa:aa:aa:aa", json={}, headers=auth).status_code == 404
    assert client.patch(f"/api/devices/{PHONE}", json={"person_id": 999}, headers=auth).status_code == 422
    assert client.patch(f"/api/devices/{PHONE}", json={"mac": "x"}, headers=auth).status_code == 422
    assert client.patch(f"/api/devices/{PHONE}", json={"kind": "toaster"}, headers=auth).status_code == 422
    tv = client.patch(f"/api/devices/{PHONE}", json={"kind": "tv"}, headers=auth)
    assert tv.status_code == 200 and tv.json()["kind"] == "tv"
    assert client.patch(f"/api/devices/{PHONE}", json={"kind": None}, headers=auth).json()["kind"] is None


def test_device_history(client, core, clock, auth):
    now = clock.now()
    core.repos.devices.upsert_seen(PHONE, now)
    core.repos.observations.add_many(
        [
            DeviceObservation(
                ts=now - timedelta(minutes=30), mac=PHONE, source="router", down_kbps=120.0, up_kbps=5.0
            ),
            DeviceObservation(ts=now - timedelta(minutes=1), mac=PHONE, source="arp"),
            DeviceObservation(ts=now - timedelta(hours=30), mac=PHONE, source="arp"),
        ]
    )
    body = client.get(f"/api/devices/{PHONE}/history", params={"hours": 24}).json()
    assert body["online"] == [
        (now - timedelta(minutes=30)).isoformat(),
        (now - timedelta(minutes=5)).isoformat(),
    ]
    assert body["traffic"] == [
        {"ts": (now - timedelta(minutes=30)).isoformat(), "up_kbps": 5.0, "down_kbps": 120.0}
    ]
    assert client.get("/api/devices/aa:aa:aa:aa:aa:aa/history").status_code == 404
    assert client.get(f"/api/devices/{PHONE}/history", params={"hours": 0}).status_code == 422


# --- network ---


def test_network_stats_respect_privacy(client, core, clock, auth):
    h = floor_hour(clock.now())
    private = "aa:bb:cc:dd:ee:09"
    core.repos.devices.upsert_seen(PHONE, clock.now())
    core.repos.devices.upsert_seen(private, clock.now())
    core.repos.devices.update(private, person_id=core.repos.persons.create("Ola", privacy_mode="stats_only"))
    core.repos.dns.add_hourly(h, PHONE, "a.com", 3, 1)
    core.repos.dns.add_hourly(h, private, "secret.com", 7, 0)
    core.repos.dns.add_hourly_stats(h, PHONE, 3, 1)
    core.repos.dns.mark_domain_seen(PHONE, "a.com", clock.now())
    core.repos.dns.mark_domain_seen(private, "secret.com", clock.now())

    top = client.get("/api/network/top-domains").json()
    assert top == [{"domain": "a.com", "queries": 3, "blocked": 1}]
    assert client.get("/api/network/top-domains", params={"mac": private}).json() == []
    assert client.get("/api/network/top-domains", params={"mac": "zly"}).status_code == 422
    assert client.get("/api/network/blocked").json() == [{"domain": "a.com", "blocked": 1}]
    assert [d["domain"] for d in client.get("/api/network/new-domains").json()] == ["a.com"]
    hourly = client.get("/api/network/hourly", params={"mac": PHONE, "hours": 3}).json()
    assert [r["queries"] for r in hourly] == [0, 0, 3]
    assert client.get("/api/network/summary").json()["queries"] == 3
    assert "secret.com" not in json.dumps(
        [client.get(p).json() for p in ("/api/network/top-domains", "/api/network/new-domains")]
    )


@pytest.fixture
def adguard(core):
    http = httpx.AsyncClient()
    core.adguard = AdGuardClient(ADGUARD, http=http)
    yield core.adguard
    core.adguard = None


@respx.mock(base_url=ADGUARD, assert_all_called=False)
def test_block_and_unblock_domain(respx_mock, client, auth, adguard):
    respx_mock.get("/control/filtering/status").respond(json={"user_rules": ["||old.com^"]})
    set_rules = respx_mock.post("/control/filtering/set_rules").respond(200)

    response = client.post("/api/network/block-domain", json={"domain": "Ads.Example.com."}, headers=auth)
    assert response.status_code == 200
    assert json.loads(set_rules.calls.last.request.content) == {"rules": ["||old.com^", "||ads.example.com^"]}

    response = client.post("/api/network/unblock-domain", json={"domain": "old.com"}, headers=auth)
    assert response.status_code == 200
    assert json.loads(set_rules.calls.last.request.content) == {"rules": []}


@pytest.mark.parametrize(
    "domain",
    [
        "",
        "a b.com",
        "||evil.com^",
        "evil.com$client=1",
        "a.com/x",
        "*.a.com",
        "a\\b.com",
        "a|b",
        "-a.com",
        "a.com,b.com",
        "x" * 64 + ".com",
        "a..com",
    ],
)
def test_block_domain_rejects_bad_input(client, auth, adguard, domain):
    response = client.post("/api/network/block-domain", json={"domain": domain}, headers=auth)
    assert response.status_code == 422


def test_block_domain_503_without_adguard(client, auth):
    response = client.post("/api/network/block-domain", json={"domain": "ads.com"}, headers=auth)
    assert response.status_code == 503
    assert response.json() == {"detail": "AdGuard niedostępny"}


@respx.mock(base_url=ADGUARD)
def test_block_domain_503_when_adguard_down(respx_mock, client, auth, adguard):
    respx_mock.get("/control/filtering/status").mock(side_effect=httpx.ConnectError("down"))
    response = client.post("/api/network/block-domain", json={"domain": "ads.com"}, headers=auth)
    assert response.status_code == 503
    assert response.json() == {"detail": "AdGuard niedostępny"}


@respx.mock(base_url=ADGUARD, assert_all_called=False)
def test_block_device_resolves_ip(respx_mock, client, core, clock, auth, adguard):
    respx_mock.get("/control/filtering/status").respond(json={"user_rules": []})
    set_rules = respx_mock.post("/control/filtering/set_rules").respond(200)
    core.repos.devices.upsert_seen(PHONE, clock.now(), ip="192.168.0.23")
    core.repos.devices.upsert_seen("aa:bb:cc:dd:ee:02", clock.now())

    response = client.post("/api/network/block-device", json={"mac": "F0:18:98:A1:00:01"}, headers=auth)
    assert response.status_code == 200
    assert json.loads(set_rules.calls.last.request.content) == {"rules": ["||*^$client=192.168.0.23"]}

    assert client.post("/api/network/unblock-device", json={"mac": PHONE}, headers=auth).status_code == 200
    no_ip = client.post("/api/network/block-device", json={"mac": "aa:bb:cc:dd:ee:02"}, headers=auth)
    assert no_ip.status_code == 404
    unknown = client.post("/api/network/block-device", json={"mac": "aa:aa:aa:aa:aa:aa"}, headers=auth)
    assert unknown.status_code == 404
    assert client.post("/api/network/block-device", json={"mac": "zly"}, headers=auth).status_code == 422


# --- alerts / settings / router ---


def _alert(clock, **kw) -> Alert:
    return Alert(
        kind="new_device", severity="warning", title="Nowe urządzenie", message="m", ts=clock.now(), **kw
    )


def test_alerts_list_and_ack(client, core, clock, auth):
    first = core.repos.alerts.add(_alert(clock))
    core.repos.alerts.add(_alert(clock, mac=PHONE))
    assert len(client.get("/api/alerts").json()) == 2
    assert client.post(f"/api/alerts/{first}/ack", headers=auth).status_code == 200
    unack = client.get("/api/alerts", params={"unack": 1}).json()
    assert len(unack) == 1 and unack[0]["mac"] == PHONE and unack[0]["acknowledged"] is False
    assert unack[0]["ts"] == clock.now().isoformat()
    assert client.post("/api/alerts/9999/ack", headers=auth).status_code == 404
    assert client.post("/api/alerts/ack-all", headers=auth).status_code == 200
    assert client.get("/api/alerts", params={"unack": 1}).json() == []


def test_alert_settings_roundtrip_and_validation(client, core, auth):
    cfg = client.get("/api/settings/alerts").json()
    assert cfg == core.rules.config()
    new = {
        "channels": {"*": ["log"], "arrive": []},
        "min_severity": "warning",
        "quiet_hours": {"start": 22, "end": 6, "allow": ["critical"]},
    }
    response = client.put("/api/settings/alerts", json=new, headers=auth)
    assert response.status_code == 200
    saved = core.rules.config()
    assert saved["min_severity"] == "warning" and saved["quiet_hours"]["start"] == 22
    assert saved["channels"]["*"] == ["log"]

    invalid = [
        {**new, "min_severity": "loud"},
        {**new, "channels": {"*": ["pigeon"]}},
        {**new, "channels": {"*": "log"}},
        {**new, "quiet_hours": {"start": 24, "end": 6, "allow": []}},
        {**new, "quiet_hours": {"start": "22", "end": 6, "allow": []}},
        {**new, "quiet_hours": {"start": 22, "end": 6, "allow": ["loud"]}},
        {**new, "extra": 1},
        {"channels": {}},
    ]
    for payload in invalid:
        assert client.put("/api/settings/alerts", json=payload, headers=auth).status_code == 422, payload
    assert core.rules.config() == saved


def test_test_notification(client, auth):
    assert client.post("/api/settings/test-notification", json={"channel": "log"}, headers=auth).json() == {
        "ok": True
    }
    response = client.post("/api/settings/test-notification", json={"channel": "telegram"}, headers=auth)
    assert response.status_code == 404
    assert "nie jest skonfigurowany" in response.json()["detail"]


def test_router_pause(client, core, clock, auth):
    response = client.post("/api/router/pause", json={"minutes": 15}, headers=auth)
    assert response.status_code == 200
    until = clock.now() + timedelta(minutes=15)
    assert response.json() == {"paused_until": until.isoformat()}
    assert core.runner.paused_until("router") == until
    assert client.post("/api/router/pause", json={}, headers=auth).json() == {
        "paused_until": until.isoformat()
    }
    assert client.post("/api/router/pause", json={"minutes": 0}, headers=auth).status_code == 422
