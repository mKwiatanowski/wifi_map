from __future__ import annotations

import asyncio
import ipaddress
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable

from homesense.core.clock import Clock
from homesense.core.models import DeviceObservation, normalize_mac

Runner = Callable[[list[str]], Awaitable[str]]

_ARP_ROW = re.compile(
    r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})\s+(\S+)\s*$"
)
# Largest subnet a ping sweep may cover (/22 = 1022 hosts).
MIN_PREFIX_LEN = 22
_DYNAMIC_TYPES = {"dynamic", "dynamiczny"}
_SKIPPED_MAC_PREFIXES = ("ff:ff:ff:ff:ff:ff", "01:00:5e:", "33:33:")


def parse_arp_output(text: str) -> list[tuple[str, str]]:
    """Parse Windows ``arp -a`` output (English or Polish) into [(ip, normalized mac)] for dynamic rows."""
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _ARP_ROW.match(line)
        if match is None or match.group(3).lower() not in _DYNAMIC_TYPES:
            continue
        try:
            ip = str(ipaddress.ip_address(match.group(1)))
        except ValueError:
            continue
        mac = normalize_mac(match.group(2))
        if mac.startswith(_SKIPPED_MAC_PREFIXES):
            continue
        rows.append((ip, mac))
    return rows


async def run_command(args: list[str]) -> str:
    """Run a command without a console window and return its stdout (exit code is ignored)."""
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=creationflags,
    )
    stdout, _ = await process.communicate()
    encoding = "oem" if sys.platform == "win32" else "utf-8"
    return stdout.decode(encoding, errors="replace")


class ArpScanCollector:
    """Ping-sweeps the subnet to populate the ARP cache, then reports devices from ``arp -a``."""

    name = "arp"

    def __init__(
        self,
        subnet: str,
        clock: Clock,
        interval_s: float = 120,
        runner: Runner | None = None,
        ping_concurrency: int = 64,
    ) -> None:
        self.interval_s = interval_s
        self._network = ipaddress.ip_network(subnet, strict=False)
        if self._network.prefixlen < MIN_PREFIX_LEN:
            raise ValueError(f"subnet {subnet!r} is too wide for an ARP ping sweep (max /{MIN_PREFIX_LEN})")
        self._clock = clock
        self._runner = runner or run_command
        self._ping_concurrency = ping_concurrency

    async def _ping_sweep(self) -> None:
        semaphore = asyncio.Semaphore(self._ping_concurrency)

        async def ping(ip: str) -> None:
            async with semaphore:
                await self._runner(["ping", "-n", "1", "-w", "500", ip])

        try:
            async with asyncio.TaskGroup() as group:
                for host in self._network.hosts():
                    group.create_task(ping(str(host)))
        except ExceptionGroup as failures:
            # Surface the first ping failure as-is so the runner records a readable error.
            raise failures.exceptions[0] from None

    async def collect(self) -> list[DeviceObservation]:
        await self._ping_sweep()
        output = await self._runner(["arp", "-a"])
        now = self._clock.now()
        # The same neighbour can be listed under several interfaces; report it once.
        rows = dict.fromkeys(
            row for row in parse_arp_output(output) if ipaddress.ip_address(row[0]) in self._network
        )
        return [DeviceObservation(ts=now, mac=mac, source="arp", ip=ip) for ip, mac in rows]
