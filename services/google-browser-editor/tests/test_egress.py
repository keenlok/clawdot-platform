import asyncio
import base64

import pytest
from chatcore.security.egress_proxy import EgressProxy, RenderBudget
from chatcore.security.net_guard import BlockedURL
from chatcore_google_browser_editor.egress import AllowlistedEgressPolicy


class DelegatePolicy:
    def __init__(self):
        self.calls = []

    async def open_tunnel(self, authority):
        self.calls.append(authority)
        raise BlockedURL("test public address not dialed")


class Writer:
    def __init__(self):
        self.data = bytearray()

    def write(self, value):
        self.data.extend(value)

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None


@pytest.mark.asyncio
async def test_nonallowlisted_https_and_wss_authorities_are_blocked_before_dns() -> None:
    delegate = DelegatePolicy()
    policy = AllowlistedEgressPolicy(frozenset({"docs.google.com"}), delegate)
    for authority in ("evil.example:443", "ws.evil.example:443", "docs.google.com:8443"):
        with pytest.raises(BlockedURL):
            await policy.open_tunnel(authority)
    assert delegate.calls == []


@pytest.mark.asyncio
async def test_connect_proxy_rejects_public_unallowlisted_host() -> None:
    delegate = DelegatePolicy()
    proxy = EgressProxy(AllowlistedEgressPolicy(frozenset({"docs.google.com"}), delegate))
    budget = RenderBudget()
    lease = proxy.register(budget)
    token = base64.b64encode(f"{lease.token}:render".encode()).decode()
    reader = asyncio.StreamReader()
    reader.feed_data(
        (
            "CONNECT public-but-unapproved.example:443 HTTP/1.1\r\n"
            f"Proxy-Authorization: Basic {token}\r\n\r\n"
        ).encode()
    )
    reader.feed_eof()
    writer = Writer()
    await proxy._handle(reader, writer)
    assert bytes(writer.data).startswith(b"HTTP/1.1 403 Forbidden")
    assert delegate.calls == []
    await proxy.close()
