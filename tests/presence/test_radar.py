from datetime import UTC, datetime, timedelta

import pytest

from homesense.core.models import Device, DeviceObservation
from homesense.presence import radar

T0 = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
MAC = "aa:bb:cc:dd:ee:01"


def _obs(minute: int, signal: int | None, connection: str = "5g") -> DeviceObservation:
    return DeviceObservation(
        ts=T0 + timedelta(minutes=minute), mac=MAC, source="sim", connection=connection, signal=signal
    )


def _device(kind=None, hostname=None, vendor=None, label=None) -> Device:
    return Device(
        mac=MAC,
        person_id=None,
        label=label,
        hostname=hostname,
        vendor=vendor,
        ip=None,
        first_seen=T0,
        last_seen=T0,
        trusted=False,
        counts_for_presence=False,
        kind=kind,
        notes=None,
    )


# --- distance ---


def test_distance_at_reference_power_is_one_metre():
    assert radar.rssi_to_distance(-40) == pytest.approx(1.0)
    assert radar.rssi_to_distance(-50, p0=-50, n=2.0) == pytest.approx(1.0)


def test_distance_follows_log_distance_model():
    # -70 dBm, P0 -40, n 3 -> 10^(30/30) = 10 m
    assert radar.rssi_to_distance(-70) == pytest.approx(10.0)
    assert radar.rssi_to_distance(-60, p0=-40, n=2.0) == pytest.approx(10.0)


def test_distance_is_monotonic_in_rssi():
    distances = [radar.rssi_to_distance(r) for r in range(-30, -100, -1)]
    assert distances == sorted(distances)


def test_distance_is_clamped():
    assert radar.rssi_to_distance(-10) == radar.MIN_DISTANCE_M == 0.5
    assert radar.rssi_to_distance(-120) == radar.MAX_DISTANCE_M == 30.0


# --- smoothing ---


def test_ema_smooths_towards_new_values():
    assert radar.ema([]) == []
    assert radar.ema([-50]) == [-50]
    smoothed = radar.ema([-50, -80], alpha=0.3)
    assert smoothed == [pytest.approx(-50), pytest.approx(-59)]


def test_ema_damps_a_single_spike():
    values = [-50] * 10 + [-90] + [-50] * 2
    smoothed = radar.ema(values)
    assert max(abs(v - -50) for v in smoothed) < 15


def test_ema_rejects_bad_alpha():
    with pytest.raises(ValueError):
        radar.ema([1.0], alpha=0)


# --- angle ---


def test_angle_is_stable_and_in_range():
    a = radar.angle_for_mac(MAC)
    assert a == radar.angle_for_mac("AA-BB-CC-DD-EE-01")
    assert 0 <= a < 360


def test_angles_differ_between_devices():
    angles = {radar.angle_for_mac(f"aa:bb:cc:dd:ee:{i:02x}") for i in range(20)}
    assert len(angles) == 20


# --- kind ---


@pytest.mark.parametrize(
    ("hostname", "vendor", "expected"),
    [
        ("Anna-iPhone", None, "phone"),
        ("iPad-Ola", None, "tablet"),
        ("Galaxy-Tab-S8", None, "tablet"),
        ("Piotr-Galaxy", None, "phone"),
        ("Pixel-7", None, "phone"),
        ("android-1234abcd", None, "phone"),
        ("Ola-Redmi", None, "phone"),
        ("Anna-MacBook-Pro", None, "computer"),
        ("Anna-Laptop", None, "computer"),
        ("DESKTOP-AB12CD", None, "computer"),
        ("LAPTOP-XYZ", None, "computer"),
        ("Piotr-ThinkPad", None, "computer"),
        ("DESKTOP-ABC123", None, "computer"),
        ("LG-TV", None, "tv"),
        ("BRAVIA-4K", None, "tv"),
        ("webOS-TV", None, "tv"),
        ("Samsung-Tizen", None, "tv"),
        ("Chromecast", None, "tv"),
        ("Roku-Ultra", None, "tv"),
        ("ESP_A1B2C3", None, "iot"),
        ("esp32-kitchen", None, "iot"),
        ("shelly1-ABC", None, "iot"),
        ("sonoff-basic", None, "iot"),
        ("Tapo_Plug", None, "iot"),
        ("Philips-hue", None, "iot"),
        ("Smart-Plug", None, "iot"),
        (None, "Espressif Inc.", "iot"),
        (None, "Tuya Smart Inc.", "iot"),
        (None, "Xiaomi Communications", "phone"),
        ("Robot-Vacuum", "Xiaomi Communications", "iot"),  # hostname wins over vendor
        (None, None, "unknown"),
        ("despina", "Acme", "unknown"),
        ("Staticbox", None, "unknown"),
    ],
)
def test_guess_kind(hostname, vendor, expected):
    assert radar.guess_kind(hostname, vendor) == expected


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("phone", "phone"),
        ("laptop", "computer"),
        ("desktop", "computer"),
        ("tablet", "tablet"),
        ("tv", "tv"),
        ("iot", "iot"),
        ("other", "unknown"),
    ],
)
def test_stored_kind_wins_and_is_mapped(stored, expected):
    assert radar.device_kind(_device(kind=stored, hostname="LG-TV")) == (expected, False)


def test_device_kind_is_guessed_without_stored_kind():
    assert radar.device_kind(_device(hostname="Anna-iPhone")) == ("phone", True)


# --- position ---


def test_position_without_samples_has_no_distance():
    pos = radar.position([], since=T0)
    assert pos.distance_m is None and pos.rssi is None and pos.trail == [] and not pos.wired


def test_position_wireless_uses_smoothed_rssi():
    samples = [_obs(0, -70), _obs(1, -70), _obs(2, -40)]
    pos = radar.position(samples, since=T0)
    assert pos.rssi == -40
    assert pos.rssi_smoothed == pytest.approx(-61.0)
    assert pos.distance_m == pytest.approx(radar.rssi_to_distance(-61.0), abs=0.01)
    assert pos.connection == "5g" and not pos.wired
    assert [p["distance_m"] for p in pos.trail][0] == pytest.approx(10.0)
    assert len(pos.trail) == 3


def test_position_trail_respects_since_and_limit():
    samples = [_obs(m, -60) for m in range(100)]
    pos = radar.position(samples, since=T0 + timedelta(minutes=50), max_trail=10)
    assert len(pos.trail) <= 10
    assert pos.trail[-1]["ts"] == samples[-1].ts
    assert all(p["ts"] >= T0 + timedelta(minutes=50) for p in pos.trail)


def test_position_wired_device_sits_next_to_router():
    pos = radar.position([_obs(0, -60), _obs(1, None, connection="wired")], since=T0)
    assert pos.wired and pos.distance_m == radar.WIRED_DISTANCE_M == 0.5
    assert pos.rssi is None and pos.connection == "wired"


def test_position_with_connection_but_no_signal():
    pos = radar.position([_obs(0, None, connection="2g")], since=T0)
    assert pos.distance_m is None and pos.connection == "2g"
