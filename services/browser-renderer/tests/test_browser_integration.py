from __future__ import annotations

import asyncio
import errno
import ssl
from pathlib import Path

import pytest
import trustme
from chatcore.security.net_guard import BlockedURL
from chatcore_browser_renderer.engine import RendererEngine
from chatcore_browser_renderer.policy import EgressPolicy, PolicyOverrides
from chatcore_browser_renderer.proxy import EgressProxy
from chatcore_browser_renderer.settings import RendererSettings
from playwright.async_api import async_playwright


async def _require_chromium() -> None:
    try:
        async with async_playwright() as playwright:
            installed = await asyncio.to_thread(Path(playwright.chromium.executable_path).exists)
    except OSError as exc:
        if exc.errno in {errno.EPERM, errno.EACCES, errno.EADDRNOTAVAIL}:
            pytest.skip("Sandbox cannot start Playwright's local driver sockets")
        raise
    if not installed:
        pytest.skip("Playwright Chromium is not installed at PLAYWRIGHT_BROWSERS_PATH")


def _tls_context() -> ssl.SSLContext:
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    trustme.CA().issue_cert("localhost").configure_cert(tls)
    return tls


def _engine(
    port: int,
    *,
    canary_port: int | None = None,
    settings: RendererSettings | None = None,
) -> RendererEngine:
    async def resolver(host: str, _port: int) -> list[str]:
        return [host]

    return RendererEngine(
        settings
        or RendererSettings(
            render_timeout_s=3,
            render_network_idle_s=0.15,
            render_settle_s=0.03,
        ),
        proxy=EgressProxy(
            EgressPolicy(
                resolver=resolver,
                overrides=PolicyOverrides(
                    allowed_ports=frozenset({port, canary_port} - {None}),
                    allowed_loopback=frozenset({"127.0.0.1"}),
                    ignore_cert_errors=True,
                ),
            ),
            header_timeout_s=0.3,
        ),
    )


async def _start_server(handler, host: str, *, ssl_context=None):
    try:
        server = await asyncio.start_server(handler, host, 0, ssl=ssl_context)
    except OSError as exc:
        pytest.skip(
            f"Cannot bind local integration listener on {host}: {exc.errno} ({exc.strerror})"
        )
    return server, int(server.sockets[0].getsockname()[1])


def _response(body: bytes, *, status: str = "200 OK", extra: bytes = b"") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\n".encode()
        + b"Content-Type: text/html; charset=utf-8\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n"
        + extra
        + b"Connection: close\r\n\r\n"
        + body
    )


@pytest.mark.integration
@pytest.mark.browser
async def test_real_chromium_proxy_auth_handshake_and_rendered_javascript() -> None:
    await _require_chromium()
    hits = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal hits
        await reader.readuntil(b"\r\n\r\n")
        hits += 1
        body = (
            b"<body><script>setTimeout(()=>document.body.innerText='rendered after JS',20)"
            b"</script>loading</body>"
        )
        writer.write(_response(body))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_server(handler, "127.0.0.1", ssl_context=_tls_context())
    engine = _engine(port)
    try:
        await engine.start()
        result = await engine.render(f"https://127.0.0.1:{port}/")
        assert result.status == "ok"
        assert "rendered after JS" in result.text
        assert hits == 1
        with pytest.raises(BlockedURL):
            await EgressPolicy().authorize(f"127.0.0.1:{port}")
    finally:
        await engine.close()
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.parametrize(
    "vector",
    ["redirect", "img", "fetch", "xhr", "iframe", "websocket", "sendBeacon", "prefetch"],
)
@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
async def test_browser_egress_vector_is_refused(vector: str, family: str) -> None:
    await _require_chromium()
    # 0.0.0.0 is the IPv4 blocked target while the listener stays on the portable
    # 127.0.0.1 address. IPv6 uses ::1 directly. Both target ports are explicitly allowed.
    canary_ip = "127.0.0.1" if family == "ipv4" else "::1"
    target_ip = "0.0.0.0" if family == "ipv4" else "::1"
    canary_hits = 0

    async def canary_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal canary_hits
        canary_hits += 1
        writer.close()
        await writer.wait_closed()

    canary, canary_port = await _start_server(canary_handler, canary_ip)
    destination_host = f"[{target_ip}]" if ":" in target_ip else target_ip
    destination = f"https://{destination_host}:{canary_port}/blocked"

    async def origin_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        if vector == "redirect":
            response = (
                f"HTTP/1.1 302 Found\r\nLocation: {destination}\r\n"
                "Content-Length: 0\r\nConnection: close\r\n\r\n"
            ).encode()
        else:
            tag = {
                "img": f"<img src='{destination}'>",
                "iframe": f"<iframe src='{destination}'></iframe>",
                "prefetch": f"<link rel='prefetch' href='{destination}'>",
            }.get(vector, "")
            script = {
                "fetch": f"fetch('{destination}').catch(()=>{{}});",
                "xhr": f"let x=new XMLHttpRequest();x.open('GET','{destination}');x.send();",
                "websocket": (
                    f"try{{new WebSocket('{destination.replace('https:', 'wss:')}')}}catch(e){{}}"
                ),
                "sendBeacon": f"navigator.sendBeacon('{destination}','x');",
                "prefetch": (
                    f"let l=document.createElement('link');l.rel='prefetch';"
                    f"l.href='{destination}';document.head.append(l);"
                ),
            }.get(vector, "")
            body = (
                f"<html><head></head><body>{tag}<script>{script}</script>ok</body></html>".encode()
            )
            response = _response(body)
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    origin, port = await _start_server(origin_handler, "127.0.0.1", ssl_context=_tls_context())
    engine = _engine(port, canary_port=canary_port)
    try:
        await engine.start()
        result = await engine.render(f"https://127.0.0.1:{port}/{vector}")
        await asyncio.sleep(0.05)
        # A redirect to a refused address surfaces as blocked_url; sub-resource vectors are
        # refused inside an otherwise normal page.
        allowed_statuses = {"blocked_url"} if vector == "redirect" else {"ok"}
        assert result.status in allowed_statuses
        assert canary_hits == 0
        if vector == "img":
            assert target_ip in engine.last_route_refused_authorities
        else:
            assert target_ip in engine.last_connect_refused_authorities
    finally:
        await engine.close()
        origin.close()
        canary.close()
        await origin.wait_closed()
        await canary.wait_closed()


@pytest.mark.integration
@pytest.mark.browser
async def test_context_cookie_isolation_and_read_only_boundaries() -> None:
    await _require_chromium()
    requests: list[tuple[str, str]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        headers = request.decode("latin-1").split("\r\n")
        method, path, *_ = headers[0].split()
        cookie = next(
            (
                line.split(":", 1)[1].strip()
                for line in headers
                if line.lower().startswith("cookie:")
            ),
            "",
        )
        requests.append((method + " " + path, cookie))
        if path == "/cookie-one":
            body = b"<body>cookie one</body>"
            extra = b"Set-Cookie: private=one; Secure; SameSite=Lax\r\n"
        elif path == "/cookie-two":
            body, extra = b"<body>cookie two</body>", b""
        elif path == "/form":
            body, extra = (
                b"<form action='/submit' method='post'><button>send</button></form>form",
                b"",
            )
        elif path == "/download":
            body, extra = b"<a href='/file' download>download</a>", b""
        elif path == "/file":
            body, extra = (
                b"x" * 8192,
                b"Content-Type: application/octet-stream\r\n"
                b"Content-Disposition: attachment; filename=x.bin\r\n",
            )
        elif path == "/loop":
            body, extra = b"<script>while(true){}</script>", b""
        elif path == "/large":
            body, extra = b"<body>" + b"x" * 32768 + b"</body>", b""
        elif path == "/submit":
            body, extra = b"submitted", b""
        else:
            body, extra = b"<body>ok</body>", b""
        writer.write(_response(body, extra=extra))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_server(handler, "127.0.0.1", ssl_context=_tls_context())
    engine = _engine(port)
    try:
        await engine.start()
        assert (await engine.render(f"https://127.0.0.1:{port}/cookie-one")).status == "ok"
        assert (await engine.render(f"https://127.0.0.1:{port}/cookie-two")).status == "ok"
        assert next(cookie for path, cookie in requests if path == "GET /cookie-two") == ""
        assert (await engine.render(f"https://127.0.0.1:{port}/form")).status == "ok"
        assert (await engine.render(f"https://127.0.0.1:{port}/download")).status == "ok"
        assert not any(
            path.startswith("POST /submit") or path == "GET /file" for path, _ in requests
        )
        assert (await engine.render(f"https://127.0.0.1:{port}/loop")).status == "timeout"
        bounded = _engine(
            port,
            settings=RendererSettings(
                render_timeout_s=3,
                render_network_idle_s=0.15,
                render_settle_s=0,
                render_max_bytes=2048,
            ),
        )
        await bounded.start()
        assert (await bounded.render(f"https://127.0.0.1:{port}/large")).status == "too_large"
        await bounded.close()
    finally:
        await engine.close()
        assert engine.browser is None and engine.browser_manager is None
        server.close()
        await server.wait_closed()


@pytest.mark.integration
async def test_localhost_canary_at_unallowed_port_is_refused_by_port_policy() -> None:
    hits = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal hits
        hits += 1
        writer.close()
        await writer.wait_closed()

    canary, port = await _start_server(handler, "127.0.0.1")

    class Connector:
        calls = 0

        async def __call__(self, host, port):
            self.calls += 1
            raise AssertionError("port policy should reject before connecting")

    connector = Connector()

    async def resolver(host, _port):
        return ["127.0.0.1"]

    policy = EgressPolicy(
        resolver=resolver,
        connector=connector,
        overrides=PolicyOverrides(
            allowed_ports=frozenset({443}),
            allowed_loopback=frozenset({"127.0.0.1"}),
        ),
    )
    try:
        with pytest.raises(BlockedURL):
            await policy.open_tunnel(f"127.0.0.1:{port}")
        assert connector.calls == 0
        assert hits == 0
    finally:
        canary.close()
        await canary.wait_closed()
