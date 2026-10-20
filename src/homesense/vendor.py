from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from homesense.core.models import normalize_mac

log = logging.getLogger(__name__)


def _default_vendors_path() -> str | None:
    """Location of mac_vendor_lookup's vendor list (bundled with the package, or its ~/.cache copy)."""
    from mac_vendor_lookup import BaseMacLookup

    return BaseMacLookup().find_vendors_list()


class VendorLookup:
    """MAC -> vendor name from mac_vendor_lookup's OUI list; returns None on any failure.

    The library's sync `MacLookup.lookup` uses `loop.run_until_complete`, which fails inside the
    running asyncio loop (Ingest.handle is async), and downloads the list from IEEE when no local copy
    exists. So this reads the library's local vendor file ("PREFIX:Vendor" lines) synchronously and
    never touches the network.
    """

    def __init__(self, vendors_path: str | Path | None = None) -> None:
        self._vendors_path = vendors_path
        self._prefixes: dict[str, str] | None = None
        self._cached_lookup = lru_cache(maxsize=4096)(self._lookup)

    def lookup(self, mac: str) -> str | None:
        return self._cached_lookup(mac)

    def _load(self) -> dict[str, str]:
        if self._prefixes is None:
            self._prefixes = {}
            try:
                path = self._vendors_path or _default_vendors_path()
                if path is None:
                    log.warning("MAC vendor list not found; vendor lookup disabled")
                else:
                    for line in Path(path).read_bytes().decode("utf-8", "replace").splitlines():
                        prefix, sep, vendor = line.partition(":")
                        if sep:
                            self._prefixes[prefix.strip().upper()] = vendor.strip()
            except Exception:
                log.warning("could not load MAC vendor list", exc_info=True)
        return self._prefixes

    def _lookup(self, mac: str) -> str | None:
        try:
            prefix = normalize_mac(mac).replace(":", "")[:6].upper()
            return self._load().get(prefix) or None
        except Exception:
            return None
