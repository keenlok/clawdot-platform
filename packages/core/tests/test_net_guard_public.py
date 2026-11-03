from __future__ import annotations

import httpx
import pytest
from chatcore.security import net_guard


@pytest.mark.parametrize(
    "destination",
    [
        "https://example.com/path?q=1",
        "example.com:443",
        "[2606:4700:4700::1111]:443",
    ],
)
def test_parse_https_destination_accepts_public_https_destinations(destination: str) -> None:
    assert net_guard.parse_https_destination(destination) in {
        ("example.com", 443),
        ("2606:4700:4700::1111", 443),
    }


def test_parse_https_destination_canonicalizes_uppercase_and_trailing_dot() -> None:
    assert net_guard.parse_https_destination("HTTPS://Example.COM.:443/path") == (
        "example.com",
        443,
    )


@pytest.mark.parametrize(
    "destination",
    [
        "http://example.com",
        "example.com:80",
        "example.com:8080",
        "user@example.com:443",
        "https://user:password@example.com",
        "localhost:443",
        "foo.localhost:443",
        "postgresql:443",
        "[::1]:443",
        "not an authority",
    ],
)
def test_parse_https_destination_rejects_unsafe_destinations(destination: str) -> None:
    with pytest.raises(net_guard.BlockedURL):
        net_guard.parse_https_destination(destination)


def test_resolve_public_rejects_every_private_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "10.1.2.3"]

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    with pytest.raises(net_guard.BlockedURL):
        import asyncio

        asyncio.run(net_guard.resolve_public("example.com", 443))


def test_resolve_public_accepts_public_address(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    import asyncio

    assert asyncio.run(net_guard.resolve_public("example.com", 443)) == ["93.184.216.34"]


def test_extra_blocked_matches_safe_get_environment_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NET_GUARD_EXTRA_BLOCKED", " 192.0.2.1, example.net ,, 203.0.113.0/24 ")
    assert net_guard.get_extra_blocked() == ("192.0.2.1", "example.net", "203.0.113.0/24")


def test_explicit_extra_blocked_precedes_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NET_GUARD_EXTRA_BLOCKED", "example.net")
    assert net_guard.get_extra_blocked(("192.0.2.0/24",)) == (
        "192.0.2.0/24",
        "example.net",
    )


async def test_resolve_public_rejects_empty_dns_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(_host: str, _port: int) -> list[str]:
        return []

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    with pytest.raises(net_guard.FetchError):
        await net_guard.resolve_public("example.com", 443)


@pytest.mark.parametrize(
    "url,address",
    [
        ("https://127.1/", None),
        ("https://[::ffff:7f00:1]/", None),
        ("https://169.254.169.254/", None),
        ("https://10.1.2.3/", None),
        ("https://example.com/", "93.184.216.34"),
        ("https://example.com/", "192.168.1.4"),
    ],
)
async def test_public_helpers_and_safe_get_make_the_same_decision(
    monkeypatch: pytest.MonkeyPatch, url: str, address: str | None
) -> None:
    async def resolve(_host: str, _port: int) -> list[str]:
        return [address] if address else []

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(
        net_guard,
        "_make_transport",
        lambda: httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text="ok")
        ),
    )

    async def wrapper_decision() -> None:
        host, port = net_guard.parse_https_destination(url)
        await net_guard.resolve_public(host, port)

    async def safe_get_decision() -> None:
        await net_guard.safe_get(url, max_bytes=10, timeout_s=1, allowed_types=("text/html",))

    for decision in (wrapper_decision, safe_get_decision):
        if address == "93.184.216.34":
            await decision()
        else:
            with pytest.raises(net_guard.BlockedURL):
                await decision()
