from __future__ import annotations

import asyncio

import pytest
from chatcore.security.net_guard import BlockedURL
from chatcore_browser_renderer.policy import EgressPolicy, PolicyOverrides


class Resolver:
    def __init__(self, answers: list[str]):
        self.answers = answers
        self.calls = 0

    async def __call__(self, host: str, port: int) -> list[str]:
        self.calls += 1
        return self.answers


class Connector:
    def __init__(self):
        self.dials: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int):
        self.dials.append((host, port))
        return object(), object()


@pytest.mark.parametrize(
    "authority",
    [
        "127.0.0.1:443",
        "127.1:443",
        "2130706433:443",
        "0x7f.0.0.1:443",
        "0177.0.0.1:443",
        "0:443",
        "[::1]:443",
        "[::]:443",
        "[::ffff:127.0.0.1]:443",
        "[::ffff:7f00:1]:443",
        "[64:ff9b::7f00:1]:443",
        "[2002:7f00:1::]:443",
        "[2001:0:0:0:0:0:f5ff:fffe]:443",
        "169.254.169.254:443",
        "[fd00:ec2::254]:443",
        "[fe80::1%25eth0]:443",
        "100.64.0.1:443",
        "100.100.100.200:443",
        "10.0.0.1:443",
        "172.16.5.4:443",
        "192.168.1.1:443",
        "0.0.0.0:443",
        "224.0.0.1:443",
        "localhost:443",
        "localhost.:443",
        "foo.localhost:443",
        "postgresql:443",
        "elasticsearch:443",
        "sb-bot-valkey:443",
        "singlelabel:443",
        "a@127.0.0.1:443",
        "evil.com@127.0.0.1:443",
        "LOCALHOST:443",
        "example.com:80",
        "example.com:8080",
        "example.com:22",
        "example.com:5432",
        "example.com:6379",
        "example.com:65535",
        "example.com:0",
        "example.com:443abc",
        "",
        "x" * 2050,
        "locаlhost:443",  # Cyrillic small a.
        "[::1:443",
        "[2606:4700:4700::1111]:443bad",
    ],
)
async def test_full_adversarial_authority_matrix_never_dials(authority: str) -> None:
    connector = Connector()
    policy = EgressPolicy(resolver=Resolver(["93.184.216.34"]), connector=connector)
    with pytest.raises(BlockedURL):
        await policy.open_tunnel(authority)
    assert connector.dials == []


@pytest.mark.parametrize(
    "authority",
    [
        "127.0.0.1:443",
        "127.1:443",
        "2130706433:443",
        "0x7f.0.0.1:443",
        "0177.0.0.1:443",
        "0:443",
        "[::1]:443",
        "[::]:443",
        "[::ffff:127.0.0.1]:443",
        "[64:ff9b::7f00:1]:443",
        "169.254.169.254:443",
        "localhost:443",
        "localhost.:443",
        "postgresql:443",
        "a@127.0.0.1:443",
        "evil.com@127.0.0.1:443",
        "example.com:80",
        "example.com:5432",
    ],
)
async def test_policy_refuses_unsafe_authority_without_connecting(authority: str) -> None:
    resolver = Resolver(["93.184.216.34"])
    connector = Connector()
    policy = EgressPolicy(resolver=resolver, connector=connector)
    with pytest.raises(BlockedURL):
        await policy.open_tunnel(authority)
    assert connector.dials == []


async def test_policy_pins_first_validated_ip_and_resolves_once() -> None:
    resolver = Resolver(["93.184.216.34"])
    connector = Connector()
    policy = EgressPolicy(resolver=resolver, connector=connector)
    target = await policy.authorize("example.com:443")
    assert target.ip == "93.184.216.34"
    assert resolver.calls == 1
    assert connector.dials == []


async def test_mixed_public_and_private_answers_are_refused() -> None:
    resolver = Resolver(["93.184.216.34", "10.0.0.1"])
    connector = Connector()
    policy = EgressPolicy(resolver=resolver, connector=connector)
    with pytest.raises(BlockedURL):
        await policy.open_tunnel("example.com:443")
    assert connector.dials == []


async def test_private_cname_style_answer_is_refused_before_dial() -> None:
    connector = Connector()
    policy = EgressPolicy(resolver=Resolver(["192.168.20.8"]), connector=connector)
    with pytest.raises(BlockedURL):
        await policy.open_tunnel("public-looking.example:443")
    assert connector.dials == []


async def test_test_overrides_are_explicit() -> None:
    policy = EgressPolicy(overrides=PolicyOverrides())
    assert policy.overrides is not None


async def test_policy_has_no_environment_switch_for_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSER_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("NET_GUARD_ALLOW_PRIVATE", "1")
    monkeypatch.setenv("BROWSER_ALLOWED_PORTS", "5432")
    monkeypatch.setenv("BROWSER_ALLOWED_LOOPBACK", "127.0.0.1")
    monkeypatch.setenv("BROWSER_IGNORE_CERT_ERRORS", "1")
    policy = EgressPolicy()
    assert policy.overrides is None
    with pytest.raises(BlockedURL):
        await policy.authorize("localhost:443")


async def test_second_connect_revalidates_dns_rebinding() -> None:
    class RebindingResolver:
        def __init__(self):
            self.calls = 0

        async def __call__(self, host: str, port: int) -> list[str]:
            self.calls += 1
            return ["93.184.216.34"] if self.calls == 1 else ["127.0.0.1"]

    class Writer:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    resolver = RebindingResolver()
    connector = Connector()

    async def dial(host: str, port: int):
        connector.dials.append((host, port))
        return __import__("asyncio").StreamReader(), Writer()

    policy = EgressPolicy(resolver=resolver, connector=dial)
    first, _, _ = await policy.open_tunnel("example.com:443")
    assert first.ip == "93.184.216.34"
    assert resolver.calls == 1
    assert connector.dials == [("93.184.216.34", 443)]
    with pytest.raises(BlockedURL):
        await policy.open_tunnel("example.com:443")
    assert resolver.calls == 2
    assert connector.dials == [("93.184.216.34", 443)]


async def test_empty_resolution_is_refused() -> None:
    policy = EgressPolicy(resolver=Resolver([]), connector=Connector())
    from chatcore.security.net_guard import FetchError

    with pytest.raises(FetchError):
        await policy.authorize("example.com:443")


async def test_extra_blocked_ip_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NET_GUARD_EXTRA_BLOCKED", "93.184.216.34")
    connector = Connector()
    policy = EgressPolicy(resolver=Resolver(["93.184.216.34"]), connector=connector)
    with pytest.raises(BlockedURL):
        await policy.open_tunnel("example.com:443")
    assert not connector.dials


@pytest.mark.parametrize(
    ("host", "answer"),
    [
        ("example.com", "93.184.216.34"),
        ("www.example.net", "1.1.1.1"),
        ("example.org", "2606:4700:4700::1111"),
    ],
)
async def test_public_hostnames_and_addresses_are_allowed(host: str, answer: str) -> None:
    connector = Connector()
    policy = EgressPolicy(resolver=Resolver([answer]), connector=connector)
    target = await policy.authorize(f"{host}:443")
    assert target.host == host
    assert target.ip == answer


async def test_uppercase_and_trailing_dot_hosts_use_safe_get_normalization() -> None:
    policy = EgressPolicy(resolver=Resolver(["93.184.216.34"]))
    target = await policy.authorize("EXAMPLE.COM.:443")
    assert target.host == "example.com"


@pytest.mark.parametrize(
    ("blocked", "answer"),
    [
        ("93.184.216.0/24", "93.184.216.34"),
        ("93.184.216.34", "93.184.216.34"),
        ("blocked.example", "93.184.216.34"),
    ],
)
async def test_extra_blocked_cidr_ip_and_hostname_are_refused(
    monkeypatch: pytest.MonkeyPatch, blocked: str, answer: str
) -> None:
    monkeypatch.setenv("NET_GUARD_EXTRA_BLOCKED", blocked)
    connector = Connector()
    policy = EgressPolicy(resolver=Resolver([answer]), connector=connector)
    authority = "blocked.example:443" if blocked == "blocked.example" else "example.com:443"
    with pytest.raises(BlockedURL):
        await policy.open_tunnel(authority)
    assert connector.dials == []


async def test_connector_tries_only_validated_addresses() -> None:
    resolver = Resolver(["93.184.216.34", "1.1.1.1"])
    connected = []

    async def connector(host: str, port: int):
        connected.append((host, port))
        if host == "93.184.216.34":
            raise OSError("fake connect failure")
        return __import__("asyncio").StreamReader(), object()

    policy = EgressPolicy(resolver=resolver, connector=connector)
    target, _, _ = await policy.open_tunnel("example.com:443")
    assert target.ip == "1.1.1.1"
    assert connected == [("93.184.216.34", 443), ("1.1.1.1", 443)]


async def test_connector_times_out_then_tries_next_address() -> None:
    connected = []

    async def connector(host: str, port: int):
        connected.append(host)
        if host == "93.184.216.34":
            await asyncio.sleep(1)
        return asyncio.StreamReader(), object()

    policy = EgressPolicy(
        resolver=Resolver(["93.184.216.34", "1.1.1.1"]),
        connector=connector,
        connect_timeout_s=0.01,
    )
    target, _, _ = await policy.open_tunnel("example.com:443")
    assert target.ip == "1.1.1.1"
    assert connected == ["93.184.216.34", "1.1.1.1"]


async def test_connector_attempts_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NET_CONNECT_ATTEMPTS", "2")
    connected = []

    async def connector(host: str, port: int):
        connected.append(host)
        raise OSError("unreachable")

    policy = EgressPolicy(
        resolver=Resolver(["93.184.216.34", "1.1.1.1", "8.8.8.8"]), connector=connector
    )
    with pytest.raises(OSError, match="Pinned destination"):
        await policy.open_tunnel("example.com:443")
    assert connected == ["93.184.216.34", "1.1.1.1"]
