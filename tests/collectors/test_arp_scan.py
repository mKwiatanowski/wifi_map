from pathlib import Path

import pytest

from homesense.collectors.arp_scan import ArpScanCollector, parse_arp_output

FIXTURE = Path(__file__).parent.parent / "fixtures" / "arp_windows.txt"


def fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_parse_returns_dynamic_rows_with_normalized_macs():
    assert parse_arp_output(fixture_text()) == [
        ("192.168.0.1", "00:11:22:33:44:55"),
        ("192.168.0.23", "da:a1:19:00:00:01"),
        ("192.168.0.40", "a4:83:e7:11:22:33"),
    ]


def test_parse_skips_broadcast_and_multicast_even_when_dynamic():
    text = (
        "  192.168.0.255         ff-ff-ff-ff-ff-ff     dynamic\n"
        "  224.0.0.251           01-00-5e-00-00-fb     dynamic\n"
        "  192.168.0.9           33-33-00-00-00-01     dynamic\n"
        "  192.168.0.10          aa-bb-cc-dd-ee-0a     dynamic\n"
    )
    assert parse_arp_output(text) == [("192.168.0.10", "aa:bb:cc:dd:ee:0a")]


def test_parse_ignores_garbage():
    assert parse_arp_output("") == []
    text = "No ARP Entries Found.\n  1.2.3.4 not-a-mac dynamic\n  999.1.1.1   aa-bb-cc-dd-ee-01   dynamic\n"
    assert parse_arp_output(text) == []


class FakeRunner:
    def __init__(self, arp_output: str) -> None:
        self.arp_output = arp_output
        self.commands: list[list[str]] = []

    async def __call__(self, args: list[str]) -> str:
        self.commands.append(args)
        if args[0] == "arp":
            return self.arp_output
        return ""


async def test_collect_returns_only_observations_within_subnet(clock):
    text = fixture_text() + "  10.0.0.5              aa-bb-cc-dd-ee-05     dynamic\n"
    runner = FakeRunner(text)
    collector = ArpScanCollector("192.168.0.0/24", clock, runner=runner)

    observations = await collector.collect()

    assert [(o.ip, o.mac) for o in observations] == [
        ("192.168.0.1", "00:11:22:33:44:55"),
        ("192.168.0.23", "da:a1:19:00:00:01"),
        ("192.168.0.40", "a4:83:e7:11:22:33"),
    ]
    assert all(o.source == "arp" and o.ts == clock.now() and o.online for o in observations)
    assert collector.name == "arp"
    assert collector.interval_s == 120


async def test_collect_pings_every_host_then_reads_arp_table(clock):
    runner = FakeRunner("")
    collector = ArpScanCollector("192.168.0.0/24", clock, runner=runner, ping_concurrency=8)

    assert await collector.collect() == []

    pings = runner.commands[:-1]
    assert len(pings) == 254
    assert pings[0] == ["ping", "-n", "1", "-w", "500", "192.168.0.1"]
    assert {p[-1] for p in pings} == {f"192.168.0.{i}" for i in range(1, 255)}
    assert runner.commands[-1] == ["arp", "-a"]


async def test_collect_propagates_runner_errors(clock):
    async def broken(args: list[str]) -> str:
        raise OSError("arp not found")

    collector = ArpScanCollector("192.168.0.0/30", clock, runner=broken)
    with pytest.raises(OSError, match="arp not found"):
        await collector.collect()


async def test_collect_propagates_arp_error_after_successful_pings(clock):
    async def arp_fails(args: list[str]) -> str:
        if args[0] == "arp":
            raise OSError("arp failed")
        return ""

    collector = ArpScanCollector("192.168.0.0/30", clock, runner=arp_fails)
    with pytest.raises(OSError, match="arp failed"):
        await collector.collect()


@pytest.mark.parametrize("subnet", ["10.0.0.0/8", "192.168.0.0/16", "192.168.96.0/21"])
def test_rejects_subnets_wider_than_slash_22(clock, subnet):
    with pytest.raises(ValueError, match="too wide"):
        ArpScanCollector(subnet, clock, runner=FakeRunner(""))


def test_accepts_slash_22(clock):
    ArpScanCollector("192.168.0.0/22", clock, runner=FakeRunner(""))


async def test_collect_dedupes_same_neighbour_seen_on_several_interfaces(clock):
    text = (
        "Interface: 192.168.0.127 --- 0x7\n"
        "  192.168.0.1           00-11-22-33-44-55     dynamic\n"
        "Interface: 192.168.0.171 --- 0x12\n"
        "  192.168.0.1           00-11-22-33-44-55     dynamic\n"
        "  192.168.0.40          a4-83-e7-11-22-33     dynamic\n"
    )
    collector = ArpScanCollector("192.168.0.0/24", clock, runner=FakeRunner(text))

    observations = await collector.collect()

    assert [(o.ip, o.mac) for o in observations] == [
        ("192.168.0.1", "00:11:22:33:44:55"),
        ("192.168.0.40", "a4:83:e7:11:22:33"),
    ]
