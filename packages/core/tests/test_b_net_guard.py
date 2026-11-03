from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from chatcore.security.net_guard import BlockedURL, safe_get


class RecordingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, delay_s: float = 0) -> None:
        self.chunks = chunks
        self.delay_s = delay_s
        self.pulled = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk
            if self.delay_s:
                await asyncio.sleep(self.delay_s)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "address",
    [
        "0.1.2.3",
        "10.1.2.3",
        "127.0.0.1",
        "169.254.1.1",
        "172.16.1.1",
        "192.168.1.1",
        "100.64.0.1",
        "192.0.0.1",
        "198.18.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "::",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "::ffff:127.0.0.1",
    ],
)
async def test_blocks_private_addresses(address: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from chatcore.security import net_guard

    async def resolve(host: str, port: int) -> list[str]:
        return [address]

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    url_address = f"[{address}]" if ":" in address else address
    with pytest.raises(BlockedURL):
        await safe_get(
            f"http://{url_address}/", max_bytes=10, timeout_s=1, allowed_types=("text/",)
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",
        "http://0x7f.1/",
        "http://0177.0.0.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::127.0.0.1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[2002:7f00:1::]/",
        "http://[fec0::1]/",
        "http://[2001:db8::1]/",
        "http://192.0.2.1/",
        "http://user:pass@example.com/",
        "file://example.com/",
        "http://example.com:8080/",
        "http://example.com:0/",
        "http://localhost/",
        "http://singlelabel/",
    ],
)
async def test_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(BlockedURL):
        await safe_get(url, max_bytes=10, timeout_s=1, allowed_types=("text/",))


async def test_checks_every_dns_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatcore.security import net_guard

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "10.0.0.1"]

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    with pytest.raises(BlockedURL):
        await safe_get("https://example.com/", max_bytes=10, timeout_s=1, allowed_types=("text/",))


async def test_checks_extra_blocked_ip_and_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatcore.security import net_guard

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    with pytest.raises(BlockedURL):
        await safe_get(
            "https://example.com/",
            max_bytes=10,
            timeout_s=1,
            allowed_types=("text/*",),
            extra_blocked=("93.184.216.0/24",),
        )
    with pytest.raises(BlockedURL):
        await safe_get(
            "https://blocked.example/",
            max_bytes=10,
            timeout_s=1,
            allowed_types=("text/*",),
            extra_blocked=("blocked.example",),
        )


async def test_rechecks_redirect_target(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from chatcore.security import net_guard

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/"})

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    with pytest.raises(BlockedURL):
        await safe_get("https://example.com/", max_bytes=10, timeout_s=1, allowed_types=("text/",))


async def test_caps_stream_and_pins_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from chatcore.security import net_guard

    observed: list[httpx.Request] = []

    async def resolve(host: str, port: int) -> list[str]:
        assert host == "example.com"
        return ["93.184.216.34"]

    stream = RecordingStream([b"abc", b"def", b"ghi", b"jkl"])

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=stream)

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    result = await safe_get(
        "https://example.com/page", max_bytes=5, timeout_s=1, allowed_types=("text/*",)
    )
    assert result.body == b"abcde"
    assert result.truncated
    assert observed[0].url.host == "93.184.216.34"
    assert observed[0].headers["host"] == "example.com"
    assert observed[0].headers["user-agent"] == "Mozilla/5.0 (compatible; sb_bot/0.1)"
    assert observed[0].extensions["sni_hostname"] == "example.com"
    assert stream.pulled == 2
    assert stream.closed


async def test_content_type_rejected_before_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from chatcore.security import net_guard

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    stream = RecordingStream([b"must not read"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, stream=stream)

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    with pytest.raises(net_guard.FetchError):
        await safe_get("https://example.com/", max_bytes=10, timeout_s=1, allowed_types=("text/*",))
    assert stream.pulled == 0


async def test_total_timeout_covers_body_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatcore.security import net_guard

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    stream = RecordingStream([b"first"], delay_s=0.2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=stream)

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    start = time.monotonic()
    with pytest.raises(net_guard.FetchError):
        await safe_get(
            "https://example.com/", max_bytes=10, timeout_s=0.05, allowed_types=("text/*",)
        )
    assert time.monotonic() - start < 0.18


@pytest.mark.parametrize("encoding", ["identity", "gzip"])
async def test_compressed_response_is_rejected_without_reading(
    monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    from chatcore.security import net_guard

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    stream = RecordingStream([b"body"])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/plain", "content-encoding": encoding},
            stream=stream,
        )

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    if encoding == "identity":
        result = await safe_get(
            "https://example.com/", max_bytes=10, timeout_s=1, allowed_types=("text/*",)
        )
        assert result.body == b"body"
        assert stream.pulled == 1
    else:
        with pytest.raises(net_guard.FetchError):
            await safe_get(
                "https://example.com/", max_bytes=10, timeout_s=1, allowed_types=("text/*",)
            )
        assert stream.pulled == 0
    assert requests[0].headers["accept-encoding"] == "identity"


async def test_follows_redirect_even_when_redirect_is_encoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatcore.security import net_guard

    resolved_hosts: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        resolved_hosts.append(host)
        return ["93.184.216.34"]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "example.com":
            return httpx.Response(
                302,
                headers={
                    "location": "https://other.example/page",
                    "content-encoding": "gzip",
                },
                content=b"",
            )
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    result = await safe_get(
        "https://example.com/", max_bytes=10, timeout_s=1, allowed_types=("text/*",)
    )
    assert result.final_url == "https://other.example/page"
    assert result.body == b"ok"
    assert resolved_hosts == ["example.com", "other.example"]
