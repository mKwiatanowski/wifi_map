from homesense.vendor import VendorLookup


def _vendors_file(tmp_path):
    p = tmp_path / "mac-vendors.txt"
    p.write_bytes(b"286FB9:Nokia Shanghai Bell Co., Ltd.\nF4F5D8:Google, Inc.\n")
    return p


def test_lookup_known_prefix(tmp_path):
    v = VendorLookup(vendors_path=_vendors_file(tmp_path))
    assert v.lookup("f4:f5:d8:12:34:56") == "Google, Inc."
    assert v.lookup("28-6F-B9-00-00-01") == "Nokia Shanghai Bell Co., Ltd."


def test_unknown_prefix_and_invalid_mac_return_none(tmp_path):
    v = VendorLookup(vendors_path=_vendors_file(tmp_path))
    assert v.lookup("00:00:00:00:00:01") is None
    assert v.lookup("not-a-mac") is None


def test_missing_vendor_list_returns_none(tmp_path):
    v = VendorLookup(vendors_path=tmp_path / "missing.txt")
    assert v.lookup("f4:f5:d8:12:34:56") is None


def test_lookup_is_cached(tmp_path):
    path = _vendors_file(tmp_path)
    v = VendorLookup(vendors_path=path)
    assert v.lookup("f4:f5:d8:12:34:56") == "Google, Inc."
    path.unlink()  # list is loaded once, results are cached
    assert v.lookup("f4:f5:d8:12:34:56") == "Google, Inc."
    assert v.lookup("28:6f:b9:00:00:01") == "Nokia Shanghai Bell Co., Ltd."


async def test_lookup_works_inside_running_event_loop(tmp_path):
    # Ingest.handle is async; the library's sync MacLookup would call run_until_complete here.
    v = VendorLookup(vendors_path=_vendors_file(tmp_path))
    assert v.lookup("f4:f5:d8:12:34:56") == "Google, Inc."


def test_default_uses_library_vendor_list_location(tmp_path, monkeypatch):
    import mac_vendor_lookup

    path = _vendors_file(tmp_path)
    monkeypatch.setattr(mac_vendor_lookup.BaseMacLookup, "find_vendors_list", lambda self: str(path))
    assert VendorLookup().lookup("f4:f5:d8:12:34:56") == "Google, Inc."


def test_default_without_any_vendor_list_returns_none(monkeypatch):
    import mac_vendor_lookup

    monkeypatch.setattr(mac_vendor_lookup.BaseMacLookup, "find_vendors_list", lambda self: None)
    assert VendorLookup().lookup("f4:f5:d8:12:34:56") is None
