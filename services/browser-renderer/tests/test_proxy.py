from __future__ import annotations

import asyncio
import base64

import pytest
from chatcore.security.net_guard import BlockedURL
from chatcore_browser_renderer.budget import RenderBudget
from chatcore_browser_renderer.policy import PinnedTarget
from chatcore_browser_renderer.proxy import EgressProxy


class DenyPolicy:
    async def open_tunnel(self, authority):
        raise BlockedURL("blocked")


class AllowPolicy:
    def __init__(self):
        self.calls = []

    async def open_tunnel(self, authority):
        self.calls.append(authority)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return PinnedTarget("example.com", "93.184.216.34", 443), reader, Writer()


class WaitingPolicy:
    async def open_tunnel(self, authority):
        return PinnedTarget("example.com", "93.184.216.34", 443), asyncio.StreamReader(), Writer()


class FailingConnectPolicy:
    async def open_tunnel(self, authority):
        raise OSError("resolved address 93.184.216.34 refused")


class Writer:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


async def run_request(proxy: EgressProxy, payload: bytes, *, budget: RenderBudget | None = None):
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    writer = Writer()
    lease = proxy.register(budget) if budget else None
    if lease:
        token = base64.b64encode(f"{lease.token}:render".encode()).decode()
        # Put the per-render proxy credentials in a request header without changing authority.
        payload = payload.replace(
            b"\r\n\r\n", f"\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode(), 1
        )
        reader = asyncio.StreamReader()
        reader.feed_data(payload)
        reader.feed_eof()
    await proxy._handle(reader, writer)
    return bytes(writer.data), writer


async def start_tunnel(proxy: EgressProxy, budget: RenderBudget):
    lease = proxy.register(budget)
    token = base64.b64encode(f"{lease.token}:render".encode()).decode()
    reader = asyncio.StreamReader()
    reader.feed_data(
        f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode()
    )
    writer = Writer()
    task = asyncio.create_task(proxy._handle(reader, writer))
    for _ in range(20):
        if b"200 Connection Established" in writer.data or writer.closed:
            break
        await asyncio.sleep(0)
    return reader, writer, task


async def test_non_connect_request_is_forbidden() -> None:
    proxy = EgressProxy(DenyPolicy())
    response, _ = await run_request(proxy, b"GET https://example.com/ HTTP/1.1\r\n\r\n")
    assert response.startswith(b"HTTP/1.1 403 Forbidden")


async def test_header_over_limit_is_rejected() -> None:
    proxy = EgressProxy(DenyPolicy())
    response, _ = await run_request(
        proxy, b"GET / HTTP/1.1\r\nX-Fill: " + b"a" * 9000 + b"\r\n\r\n"
    )
    assert response.startswith(b"HTTP/1.1 400 Bad Request")


async def test_authorized_but_blocked_connect_returns_403_and_records_hostname() -> None:
    proxy = EgressProxy(DenyPolicy())
    budget = RenderBudget()
    response, _ = await run_request(
        proxy,
        b"CONNECT bad.example:443 HTTP/1.1\r\n\r\n",
        budget=budget,
    )
    assert response.startswith(b"HTTP/1.1 403 Forbidden")
    assert budget.refused_authorities == ["bad.example"]


async def test_chromium_proxy_authentication_handshake_then_blocked_target() -> None:
    policy = AllowPolicy()
    proxy = EgressProxy(policy)
    budget = RenderBudget()
    lease = proxy.register(budget)
    first, _ = await run_request(proxy, b"CONNECT bad.example:443 HTTP/1.1\r\n\r\n")
    assert first.startswith(b"HTTP/1.1 407 Proxy Authentication Required")
    assert b'Proxy-Authenticate: Basic realm="render"' in first
    assert policy.calls == []

    token = base64.b64encode(f"{lease.token}:render".encode()).decode()
    second, _ = await run_request(
        proxy,
        f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode(),
    )
    assert second.startswith(b"HTTP/1.1 200 Connection Established")
    assert policy.calls == ["example.com:443"]


async def test_wrong_proxy_credentials_get_407_without_dial() -> None:
    policy = AllowPolicy()
    proxy = EgressProxy(policy)
    budget = RenderBudget()
    proxy.register(budget)
    response, _ = await run_request(
        proxy,
        b"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic d3Jvbmc6cmVuZGVy\r\n\r\n",
    )
    assert response.startswith(b"HTTP/1.1 407 Proxy Authentication Required")
    assert policy.calls == []


async def test_pipelined_bytes_before_tunnel_are_rejected() -> None:
    proxy = EgressProxy(AllowPolicy())
    budget = RenderBudget()
    response, _ = await run_request(
        proxy, b"CONNECT example.com:443 HTTP/1.1\r\n\r\nhello", budget=budget
    )
    assert response.startswith(b"HTTP/1.1 400 Bad Request")


async def test_post_and_absolute_form_requests_are_forbidden() -> None:
    for line in (b"POST / HTTP/1.1\r\n\r\n", b"GET http://example.com/ HTTP/1.1\r\n\r\n"):
        response, _ = await run_request(EgressProxy(DenyPolicy()), line)
        assert response.startswith(b"HTTP/1.1 403 Forbidden")


async def test_malformed_request_and_header_are_rejected() -> None:
    proxy = EgressProxy(DenyPolicy())
    response, _ = await run_request(proxy, b"CONNECT\r\n\r\n")
    assert response.startswith(b"HTTP/1.1 400 Bad Request")
    response, _ = await run_request(proxy, b"CONNECT example.com:443 HTTP/1.1\r\nMalformed\r\n\r\n")
    assert response.startswith(b"HTTP/1.1 400 Bad Request")


async def test_slow_and_half_open_headers_are_rejected() -> None:
    for payload in (b"", b"CONNECT example.com:443 HTTP/1.1\r\n"):
        reader = asyncio.StreamReader()
        if payload:
            reader.feed_data(payload)
        writer = Writer()
        await EgressProxy(DenyPolicy(), header_timeout_s=0.01)._handle(reader, writer)
        assert bytes(writer.data).startswith(b"HTTP/1.1 400 Bad Request")


@pytest.mark.parametrize("limit", ["max_tunnels", "max_concurrent_tunnels"])
async def test_many_tunnels_over_budget_close_render_tunnels(limit: str) -> None:
    proxy = EgressProxy(WaitingPolicy())
    budget = RenderBudget(**{limit: 1}, idle_timeout_s=2)
    first_reader, first_writer, first_task = await start_tunnel(proxy, budget)
    assert b"200 Connection Established" in first_writer.data
    _, second_writer, second_task = await start_tunnel(proxy, budget)
    await second_task
    results = await asyncio.gather(first_task, return_exceptions=True)
    assert bytes(second_writer.data).startswith(b"HTTP/1.1 403 Forbidden")
    assert budget.too_large
    assert first_writer.closed
    assert isinstance(results[0], asyncio.CancelledError)
    await proxy.close()


async def test_byte_budget_overrun_closes_tunnel() -> None:
    proxy = EgressProxy(WaitingPolicy())
    budget = RenderBudget(max_bytes=2, idle_timeout_s=1)
    reader, writer, task = await start_tunnel(proxy, budget)
    reader.feed_data(b"abc")
    await task
    assert budget.too_large
    assert writer.closed
    await proxy.close()


async def test_exhausted_budget_refuses_later_connect_without_dial() -> None:
    class TrackingPolicy:
        def __init__(self):
            self.calls = []

        async def open_tunnel(self, authority):
            self.calls.append(authority)
            return (
                PinnedTarget("example.com", "93.184.216.34", 443),
                asyncio.StreamReader(),
                Writer(),
            )

    policy = TrackingPolicy()
    proxy = EgressProxy(policy)
    budget = RenderBudget(max_bytes=1, idle_timeout_s=1)
    lease = proxy.register(budget)
    reader, writer, task = await start_tunnel(proxy, budget)
    reader.feed_data(b"too many")
    await task
    assert budget.too_large
    token = base64.b64encode(f"{lease.token}:render".encode()).decode()
    response, _ = await run_request(
        proxy,
        f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode(),
    )
    assert response.startswith(b"HTTP/1.1 403 Forbidden")
    assert policy.calls == ["example.com:443"]
    assert await budget.charge(1) is False
    await proxy.close()


async def test_idle_tunnel_times_out() -> None:
    proxy = EgressProxy(WaitingPolicy())
    budget = RenderBudget(idle_timeout_s=0.01)
    _, writer, task = await start_tunnel(proxy, budget)
    await task
    assert b"200 Connection Established" in writer.data
    assert not budget.too_large
    assert writer.closed
    await proxy.close()


async def test_download_activity_keeps_silent_client_tunnel_alive() -> None:
    class DownloadReader:
        async def read(self, _size):
            await asyncio.sleep(0.01)
            return b"chunk"

    class SilentReader:
        async def read(self, _size):
            await asyncio.Event().wait()

    budget = RenderBudget(max_bytes=1024, idle_timeout_s=0.025)
    client_writer = Writer()
    upstream_writer = Writer()
    task = asyncio.create_task(
        EgressProxy()._tunnel(
            SilentReader(),
            client_writer,
            DownloadReader(),
            upstream_writer,
            budget,
            asyncio.current_task(),
        )
    )
    await asyncio.sleep(0.12)
    assert not task.done()
    assert budget.bytes_used >= 40
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_upstream_connect_failure_is_502_without_resolved_address() -> None:
    proxy = EgressProxy(FailingConnectPolicy())
    budget = RenderBudget()
    response, _ = await run_request(
        proxy, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n", budget=budget
    )
    assert response.startswith(b"HTTP/1.1 502 Bad Gateway")
    assert b"93.184.216.34" not in response
