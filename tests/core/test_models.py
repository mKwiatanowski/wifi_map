import pytest

from homesense.core.models import Device, is_random_mac, normalize_mac


def test_normalize_mac_variants():
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("aabb.ccdd.eeff") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("AA-BB-cc:dd.EE:FF") == "aa:bb:cc:dd:ee:ff"


def test_normalize_mac_invalid():
    with pytest.raises(ValueError):
        normalize_mac("zz:bb")


def test_random_mac():
    assert is_random_mac("da:a1:19:00:00:01")
    assert not is_random_mac("00:11:22:33:44:55")


def test_device_display_name(utc_start):
    kw = dict(person_id=None, ip=None, first_seen=utc_start, last_seen=utc_start, trusted=False,
              counts_for_presence=True, kind=None, notes=None)
    mac = "aa:bb:cc:dd:ee:ff"
    assert Device(mac=mac, label="L", hostname="h", vendor="v", **kw).display_name == "L"
    assert Device(mac=mac, label=None, hostname="h", vendor="v", **kw).display_name == "h"
    assert Device(mac=mac, label=None, hostname=None, vendor="v", **kw).display_name == "v"
    d = Device(mac="da:a1:19:00:00:01", label=None, hostname=None, vendor=None, **kw)
    assert d.display_name == "da:a1:19:00:00:01" and d.is_random
