from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from homesense.alerts.service import AlertService
from homesense.core.clock import Clock
from homesense.core.events import EventBus
from homesense.core.models import (
    Alert,
    Device,
    DeviceObservation,
    DnsQueryRecord,
    NewDeviceEvent,
    Observation,
    ObservationsBatch,
    is_random_mac,
    normalize_mac,
)
from homesense.core.timeutil import floor_hour
from homesense.storage.repositories import Repos

log = logging.getLogger(__name__)

UNKNOWN_MAC = "unknown"

_FULL_PRIVACY = "full"


class Ingest:
    """Sink for collector batches: persists observations, detects new devices, aggregates DNS."""

    def __init__(
        self,
        repos: Repos,
        bus: EventBus,
        alerts: AlertService,
        clock: Clock,
        vendor: Callable[[str], str | None],
    ) -> None:
        self._repos = repos
        self._bus = bus
        self._alerts = alerts
        self._clock = clock
        self._vendor = vendor

    async def handle(self, observations: list[Observation]) -> None:
        """Device observations with an invalid MAC are logged and dropped (also from ObservationsBatch),
        so one bad record cannot lose the rest of the batch."""
        valid: list[Observation] = []
        for o in observations:
            if isinstance(o, DeviceObservation):
                try:
                    o = replace(o, mac=normalize_mac(o.mac))
                except ValueError:
                    log.warning("skipping device observation with invalid MAC %r from %s", o.mac, o.source)
                    continue
            valid.append(o)
        devices = [o for o in valid if isinstance(o, DeviceObservation)]
        dns = [o for o in valid if isinstance(o, DnsQueryRecord)]
        for o in devices:
            if o.online:
                await self._handle_sighting(o)
        self._repos.observations.add_many(devices)
        self._handle_dns(dns)
        await self._bus.publish(ObservationsBatch(tuple(valid)))

    async def _handle_sighting(self, o: DeviceObservation) -> None:
        mac = o.mac  # normalized in handle()
        # Vendor lookup only for devices we have never seen (ruling R3).
        vendor = self._vendor(mac) if self._repos.devices.get(mac) is None else None
        created = self._repos.devices.upsert_seen(mac, o.ts, o.ip, o.hostname, vendor)
        if not created:
            return
        await self._bus.publish(NewDeviceEvent(mac=mac, ts=o.ts))
        name = o.hostname or vendor or mac
        await self._alerts.raise_alert(
            Alert(
                kind="new_device",
                severity="warning",
                title="Nowe urządzenie w sieci",
                message=f"{name} ({o.ip})" if o.ip else name,
                ts=o.ts,
                mac=mac,
                dedup_key=f"new_device:{mac}",
                data={"random_mac": is_random_mac(mac)},
            )
        )

    def _handle_dns(self, records: list[DnsQueryRecord]) -> None:
        if not records:
            return
        owners: dict[str, tuple[str, bool]] = {}  # client_ip -> (mac, keep domain names)
        stats_q: Counter[tuple[datetime, str]] = Counter()
        stats_b: Counter[tuple[datetime, str]] = Counter()
        domain_q: Counter[tuple[datetime, str, str]] = Counter()
        domain_b: Counter[tuple[datetime, str, str]] = Counter()
        first_seen: dict[tuple[str, str], datetime] = {}

        for r in records:
            if r.client_ip not in owners:
                owners[r.client_ip] = self._resolve_client(r.client_ip)
            mac, keep_domains = owners[r.client_ip]
            hour = floor_hour(r.ts)
            stats_q[hour, mac] += 1
            stats_b[hour, mac] += int(r.blocked)
            if keep_domains:
                domain_q[hour, mac, r.domain] += 1
                domain_b[hour, mac, r.domain] += int(r.blocked)
                key = (mac, r.domain)
                if key not in first_seen or r.ts < first_seen[key]:
                    first_seen[key] = r.ts

        dns = self._repos.dns
        for (hour, mac), q in stats_q.items():
            dns.add_hourly_stats(hour, mac, q, stats_b[hour, mac])
        for (hour, mac, domain), q in domain_q.items():
            dns.add_hourly(hour, mac, domain, q, domain_b[hour, mac, domain])
        for (mac, domain), ts in first_seen.items():
            dns.mark_domain_seen(mac, domain, ts)

    def _resolve_client(self, client_ip: str) -> tuple[str, bool]:
        """(mac or UNKNOWN_MAC, whether domain names may be stored for it)."""
        device = self._repos.devices.find_by_ip(client_ip)
        if device is None:
            return UNKNOWN_MAC, True
        return device.mac, self._owner_allows_domains(device)

    def _owner_allows_domains(self, device: Device) -> bool:
        if device.person_id is None:
            return True
        person = self._repos.persons.get(device.person_id)
        return person is None or person.privacy_mode == _FULL_PRIVACY
