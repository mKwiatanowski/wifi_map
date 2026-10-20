from types import SimpleNamespace

import pytest

from homesense.collectors.router_tplink import TplinkRouterCollector, map_connection


def make_status():
    return SimpleNamespace(
        devices=[
            SimpleNamespace(
                macaddr="AA-BB-CC-DD-EE-01",
                ipaddr="192.168.0.10",
                hostname="iPhone",
                type=SimpleNamespace(name="HOST_5G"),
                signal=-55,
                up_speed=1000,
                down_speed=5000,
            )
        ]
    )


class FakeClient:
    def __init__(self, status=None, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[str] = []

    def authorize(self) -> None:
        self.calls.append("authorize")

    def get_status(self):
        self.calls.append("get_status")
        if self.error is not None:
            raise self.error
        return self.status

    def logout(self) -> None:
        self.calls.append("logout")


async def test_collect_maps_devices_to_observations(clock):
    client = FakeClient(make_status())
    collector = TplinkRouterCollector(
        "192.168.0.1", "admin", "secret", clock, client_factory=lambda: client
    )

    observations = await collector.collect()

    assert len(observations) == 1
    obs = observations[0]
    assert obs.ts == clock.now()
    assert obs.mac == "aa:bb:cc:dd:ee:01"
    assert obs.source == "router"
    assert obs.ip == "192.168.0.10"
    assert obs.hostname == "iPhone"
    assert obs.connection == "5g"
    assert obs.signal == -55
    assert obs.up_kbps == 8.0
    assert obs.down_kbps == 40.0
    assert client.calls == ["authorize", "get_status", "logout"]
    assert collector.name == "router"
    assert collector.interval_s == 60


async def test_missing_optional_fields_become_none(clock):
    device = SimpleNamespace(
        macaddr="aa:bb:cc:dd:ee:02", ipaddr="192.168.0.11", hostname="", type=SimpleNamespace(name="WIRED")
    )
    client = FakeClient(SimpleNamespace(devices=[device]))
    collector = TplinkRouterCollector(
        "192.168.0.1", "admin", "secret", clock, client_factory=lambda: client
    )

    [obs] = await collector.collect()

    assert obs.hostname is None
    assert obs.connection == "wired"
    assert obs.signal is None
    assert obs.up_kbps is None
    assert obs.down_kbps is None


async def test_logout_called_and_error_propagates_when_get_status_raises(clock):
    client = FakeClient(error=RuntimeError("router unreachable"))
    collector = TplinkRouterCollector(
        "192.168.0.1", "admin", "secret", clock, client_factory=lambda: client
    )

    with pytest.raises(RuntimeError, match="router unreachable"):
        await collector.collect()

    assert client.calls == ["authorize", "get_status", "logout"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("WIRED", "wired"),
        ("HOST_2G", "2g"),
        ("GUEST_5G", "5g"),
        ("IOT_6G", "6g"),
        ("HOST_MLO", "unknown"),
        ("UNKNOWN", "unknown"),
    ],
)
def test_map_connection(name, expected):
    assert map_connection(SimpleNamespace(name=name)) == expected


def test_map_connection_accepts_real_library_enum_and_garbage():
    from tplinkrouterc6u import Connection

    assert map_connection(Connection.HOST_5G) == "5g"
    assert map_connection(Connection.WIRED) == "wired"
    assert map_connection(None) == "unknown"
    assert map_connection("host_2g") == "2g"


class FailingLoginClient(FakeClient):
    """Mimics the library: logout raises when the client never authorised."""

    def authorize(self) -> None:
        self.calls.append("authorize")
        raise PermissionError("login failed: wrong password")

    def logout(self) -> None:
        self.calls.append("logout")
        raise Exception("Not authorised")


async def test_authorize_error_propagates_unmasked_and_logout_is_skipped(clock):
    client = FailingLoginClient()
    collector = TplinkRouterCollector(
        "192.168.0.1", "admin", "secret", clock, client_factory=lambda: client
    )

    with pytest.raises(PermissionError, match="wrong password"):
        await collector.collect()

    assert client.calls == ["authorize"]


async def test_get_status_error_wins_over_failing_logout(clock):
    client = FakeClient(error=TimeoutError("router timed out"))

    def broken_logout() -> None:
        client.calls.append("logout")
        raise Exception("Not authorised")

    client.logout = broken_logout
    collector = TplinkRouterCollector(
        "192.168.0.1", "admin", "secret", clock, client_factory=lambda: client
    )

    with pytest.raises(TimeoutError, match="router timed out"):
        await collector.collect()

    assert client.calls == ["authorize", "get_status", "logout"]


async def test_failing_logout_after_success_still_returns_observations(clock):
    client = FakeClient(make_status())

    def broken_logout() -> None:
        raise Exception("Not authorised")

    client.logout = broken_logout
    collector = TplinkRouterCollector(
        "192.168.0.1", "admin", "secret", clock, client_factory=lambda: client
    )

    assert len(await collector.collect()) == 1
