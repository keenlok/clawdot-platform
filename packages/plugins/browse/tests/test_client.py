import inspect

import httpx
import pytest
from chatcore_browse.client import GENERIC_ERROR, STATUS_MESSAGES, RenderClient
from chatcore_browse.settings import BrowseSettings


def response(payload, status=200, headers=None):
    return httpx.Response(status, json=payload, headers=headers)


async def client_for(handler):
    calls = []

    async def record(request):
        calls.append(request)
        result = handler(request)
        return await result if inspect.isawaitable(result) else result

    return RenderClient("https://renderer.invalid", 1, httpx.MockTransport(record)), calls


@pytest.mark.parametrize(
    "status", ["blocked_by_site", "blocked_url", "timeout", "too_large", "busy", "error"]
)
async def test_status_mapping(status):
    async def handler(request):
        return response({"status": status, "error": "hostile details"})

    client, _ = await client_for(handler)
    page = await client.render("https://example.com", max_response_bytes=1000)
    assert page.status == (status if status != "error" else "error")
    if status in STATUS_MESSAGES:
        assert STATUS_MESSAGES[page.status]
    else:
        assert GENERIC_ERROR == "The page could not be rendered."


async def test_blocked_site_is_reported_plainly_and_not_retried():
    async def handler(request):
        return response({"status": "blocked_by_site", "error": "do not reveal"})

    client, calls = await client_for(handler)
    page = await client.render("https://example.com", max_response_bytes=1000)
    assert len(calls) == 1
    assert page.status == "blocked_by_site"
    assert (
        STATUS_MESSAGES[page.status]
        == "This site blocked automated access, so I can't read it. Please open the link yourself."
    )
    assert "do not reveal" not in STATUS_MESSAGES[page.status]


async def test_success_payload_validation_and_no_credentials_or_cookies():
    async def handler(request):
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        assert request.method == "POST"
        assert request.url.path == "/render"
        assert request.read() == b'{"url":"https://example.com"}'
        return response(
            {
                "status": "ok",
                "final_url": "https://example.com/a",
                "title": "A\nB",
                "text": "Body\x00\x85",
                "truncated": False,
                "extra": "ignored",
            }
        )

    client, _ = await client_for(handler)
    page = await client.render("https://example.com", max_response_bytes=1000)
    assert page.status == "ok"
    assert page.title == "AB"
    assert page.text == "Body"
    assert page.final_url == "https://example.com/a"
    assert page.truncated is False


@pytest.mark.parametrize(
    "final_url",
    [
        "http://example.com",
        "https://127.0.0.1",
        "https://10.0.0.2",
        "https://user:pass@example.com",
        "https://127.1",
        "https://2130706433",
        "https://0x7f000001",
        "https://0177.0.0.1",
        "https://deadbeef",
    ],
)
async def test_invalid_final_url_is_generic_error(final_url):
    client, _ = await client_for(
        lambda request: response(
            {"status": "ok", "final_url": final_url, "title": "x", "text": "x", "truncated": False}
        )
    )
    assert (await client.render("https://example.com", max_response_bytes=1000)).status == "error"


async def test_oversize_body_is_aborted_mid_stream():
    class CountingStream(httpx.AsyncByteStream):
        def __init__(self):
            self.chunks_yielded = 0

        async def __aiter__(self):
            self.chunks_yielded += 1
            yield b"x" * 65_536
            self.chunks_yielded += 1
            yield b"should not be read"

        async def aclose(self):
            return None

    stream = CountingStream()
    client, calls = await client_for(lambda request: httpx.Response(200, stream=stream))
    assert (await client.render("https://example.com", max_response_bytes=10)).status == "error"
    assert len(calls) == 1
    assert stream.chunks_yielded == 1


async def test_malformed_json_and_redirect_are_generic_and_redirect_not_followed():
    client, calls = await client_for(
        lambda request: httpx.Response(302, headers={"location": "https://elsewhere.invalid"})
    )
    assert (await client.render("https://example.com", max_response_bytes=100)).status == "error"
    assert len(calls) == 1

    malformed, malformed_calls = await client_for(
        lambda request: httpx.Response(200, content=b"not json")
    )
    assert (await malformed.render("https://example.com", max_response_bytes=100)).status == "error"
    assert len(malformed_calls) == 1


async def test_request_failure_log_has_cause_without_url_path(caplog):
    async def handler(request):
        raise httpx.ConnectError("hidden exception detail", request=request)

    client, _ = await client_for(handler)
    caplog.set_level("WARNING", logger="chatcore_browse.client")
    await client.render("https://example.com/private/path?q=secret", max_response_bytes=100)
    record = next(
        record for record in caplog.records if "renderer request failed" in record.message
    )
    assert (
        record.message == "renderer request failed host=example.com error=ConnectError cause=none"
    )
    assert "private" not in record.message and "secret" not in record.message


@pytest.mark.parametrize("status", [403, 429])
async def test_http_access_wall_maps_to_blocked_by_site(status):
    async def handler(request):
        return httpx.Response(status)

    client, calls = await client_for(handler)
    page = await client.render("https://example.com", max_response_bytes=100)
    assert page.status == "blocked_by_site"
    assert len(calls) == 1


def test_renderer_endpoint_rejects_userinfo_in_constructor_and_settings():
    with pytest.raises(ValueError):
        RenderClient("https://user:pass@renderer.invalid", 1)
    with pytest.raises(ValueError):
        BrowseSettings(render_url="https://user:pass@renderer.invalid")


def test_max_fetches_per_turn_uses_browse_environment_setting(monkeypatch):
    monkeypatch.delenv("BROWSE_MAX_FETCHES_PER_TURN", raising=False)
    assert BrowseSettings().max_fetches_per_turn == 3
    monkeypatch.setenv("BROWSE_MAX_FETCHES_PER_TURN", "5")
    assert BrowseSettings().max_fetches_per_turn == 5
