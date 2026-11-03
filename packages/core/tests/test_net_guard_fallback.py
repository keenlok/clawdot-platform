from __future__ import annotations

import asyncio
import socket
import time

import httpx
import pytest
from chatcore.security import net_guard


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")


def test_resolve_deduplicates_in_getaddrinfo_order_and_partitions_ipv4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: answers)
    monkeypatch.delenv("NET_PREFER_IPV4", raising=False)
    assert asyncio.run(net_guard._resolve("example.com", 443)) == [
        "93.184.216.34",
        "1.1.1.1",
        "2606:4700::1",
    ]
    monkeypatch.setenv("NET_PREFER_IPV4", "false")
    assert asyncio.run(net_guard._resolve("example.com", 443)) == [
        "2606:4700::1",
        "93.184.216.34",
        "1.1.1.1",
    ]


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_connect_failure_falls_back_with_ip_pinning_and_sni(
    monkeypatch: pytest.MonkeyPatch, failure: type[httpx.HTTPError]
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "2606:4700::1":
            raise failure("dial failed", request=request)
        return _ok(request)

    monkeypatch.setattr(net_guard, "_resolve", _resolve_addresses)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    result = await net_guard.safe_get(
        "https://example.com/path", max_bytes=100, timeout_s=2, allowed_types=("text/*",)
    )
    assert result.body == b"ok"
    assert [request.url.host for request in requests] == ["2606:4700::1", "93.184.216.34"]
    assert all(request.extensions["sni_hostname"] == "example.com" for request in requests)
    assert all(request.headers["host"] == "example.com" for request in requests)


async def test_all_connection_attempts_fail_chains_last_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    monkeypatch.setattr(net_guard, "_resolve", _resolve_addresses)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    with pytest.raises(net_guard.FetchError, match="site could not be reached") as caught:
        await net_guard.safe_get(
            "https://example.com/", max_bytes=10, timeout_s=1, allowed_types=("text/*",)
        )
    assert net_guard.failure_cause(caught.value) == "ConnectError"


async def test_attempt_count_is_capped_by_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def resolve(*_) -> list[str]:
        return ["93.184.216.34", "1.1.1.1", "8.8.8.8", "9.9.9.9", "208.67.222.222"]

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        raise httpx.ConnectError("no route", request=request)

    monkeypatch.setenv("NET_CONNECT_ATTEMPTS", "3")
    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    with pytest.raises(net_guard.FetchError):
        await net_guard.safe_get(
            "https://example.com/", max_bytes=1, timeout_s=1, allowed_types=("text/*",)
        )
    assert len(calls) == 3


@pytest.mark.parametrize("blocked_index", [0, 1, 4])
async def test_every_answer_checked_even_beyond_attempt_cap(
    monkeypatch: pytest.MonkeyPatch, blocked_index: int
) -> None:
    addresses = ["93.184.216.34", "1.1.1.1", "8.8.8.8", "9.9.9.9", "208.67.222.222"]
    addresses[blocked_index] = "10.0.0.1"
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _ok(request)

    monkeypatch.setenv("NET_CONNECT_ATTEMPTS", "2")

    async def resolve(*_) -> list[str]:
        return addresses

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    with pytest.raises(net_guard.BlockedURL):
        await net_guard.safe_get(
            "https://example.com/", max_bytes=1, timeout_s=1, allowed_types=("text/*",)
        )
    assert calls == 0


async def test_response_or_body_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(500, headers={"content-type": "text/plain"}, text="error")

    monkeypatch.setattr(net_guard, "_resolve", _resolve_addresses)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    result = await net_guard.safe_get(
        "https://example.com/", max_bytes=10, timeout_s=1, allowed_types=("text/*",)
    )
    assert result.status == 500
    assert len(calls) == 1


async def test_body_read_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("body read failed")
            yield b""

        async def aclose(self) -> None:
            pass

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=BrokenStream())

    monkeypatch.setattr(net_guard, "_resolve", _resolve_addresses)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    with pytest.raises(net_guard.FetchError) as caught:
        await net_guard.safe_get(
            "https://example.com/", max_bytes=10, timeout_s=1, allowed_types=("text/*",)
        )
    assert net_guard.failure_cause(caught.value) == "ReadError"
    assert len(calls) == 1


async def test_redirect_fallback_then_private_answer_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions: list[str] = []
    calls = 0

    async def resolve(host: str, port: int) -> list[str]:
        resolutions.append(host)
        return _addresses() if host == "example.com" else ["93.184.216.34", "10.0.0.1"]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.host == "2606:4700::1":
            raise httpx.ConnectError("v6 route unavailable", request=request)
        return httpx.Response(302, headers={"location": "https://other.example/"})

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    with pytest.raises(net_guard.BlockedURL):
        await net_guard.safe_get(
            "https://example.com/", max_bytes=10, timeout_s=2, allowed_types=("text/*",)
        )
    assert resolutions == ["example.com", "other.example"]
    assert calls == 2


async def test_resolve_redirects_rejects_private_and_caps_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"] if host == "example.com" else ["127.0.0.1"]

    async def private_redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://private.example/"})

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(private_redirect))
    with pytest.raises(net_guard.BlockedURL):
        await net_guard.resolve_redirects(
            "https://example.com/", timeout_s=1, stop_at=lambda _: False
        )

    async def loop_redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/next"})

    async def public(*_) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(net_guard, "_resolve", public)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(loop_redirect))
    with pytest.raises(net_guard.FetchError, match="redirected too many"):
        await net_guard.resolve_redirects(
            "https://example.com/", timeout_s=1, stop_at=lambda _: False, max_redirects=1
        )


async def test_connection_attempt_timeout_shares_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = time.monotonic()

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        raise httpx.ConnectTimeout("connect timed out", request=request)

    monkeypatch.setattr(net_guard, "_resolve", _resolve_addresses)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    with pytest.raises(net_guard.FetchError) as caught:
        await net_guard.safe_get(
            "https://example.com/", max_bytes=1, timeout_s=0.1, allowed_types=("text/*",)
        )
    assert str(caught.value) == "The page request timed out."
    assert time.monotonic() - started < 0.25


async def test_each_attempt_timeout_is_bounded_by_shrinking_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    addresses = ["93.184.216.34", "1.1.1.1", "8.8.8.8"]
    loop = asyncio.get_running_loop()
    clock = [loop.time()]
    monkeypatch.setattr(loop, "time", lambda: clock[0])
    attempts: list[dict[str, float]] = []

    async def resolve(*_) -> list[str]:
        return addresses

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.extensions["timeout"])
        if len(attempts) < 3:
            clock[0] += 1.5
            raise httpx.ConnectError("no route", request=request)
        return _ok(request)

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    await net_guard.safe_get(
        "https://example.com/", max_bytes=10, timeout_s=6, allowed_types=("text/*",)
    )

    assert attempts[0]["connect"] == pytest.approx(2.0)
    assert attempts[0]["read"] == pytest.approx(6.0)
    assert attempts[0]["pool"] == pytest.approx(6.0)
    assert attempts[1]["connect"] == pytest.approx(2.25)
    assert attempts[1]["read"] == pytest.approx(4.5)
    assert attempts[1]["pool"] == pytest.approx(4.5)
    assert attempts[2]["connect"] == pytest.approx(3.0)
    assert attempts[2]["read"] == pytest.approx(3.0)
    assert attempts[2]["pool"] == pytest.approx(3.0)


async def test_fetcherror_without_cause_has_none() -> None:
    assert net_guard.failure_cause(net_guard.FetchError("no cause")) == "none"


async def test_resolve_redirects_stops_without_reading_final_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulls = 0
    requested: list[str] = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal pulls
            pulls += 1
            yield b"should not read"

        async def aclose(self) -> None:
            pass

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.headers["host"] + str(request.url.path))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/middle"}, stream=Stream())
        if request.url.path == "/middle":
            return httpx.Response(301, headers={"location": "/search?q=hi"}, stream=Stream())
        return httpx.Response(200, stream=Stream())

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    result = await net_guard.resolve_redirects(
        "https://example.com/start", timeout_s=2, stop_at=lambda url: url.endswith("/search?q=hi")
    )
    assert result.final_url.endswith("/search?q=hi")
    assert result.status is None and result.hops == 2
    assert len(requested) == 2
    assert pulls == 0


def _addresses():
    return ["2606:4700::1", "93.184.216.34"]


async def _resolve_addresses(*_) -> list[str]:
    return _addresses()
