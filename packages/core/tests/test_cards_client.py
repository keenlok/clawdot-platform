import base64

import httpx
import pytest
from chatcore.cards.client import CardRenderClient
from chatcore.cards.errors import CardError
from chatcore.cards.models import validate_card

PNG = b"\x89PNG\r\n\x1a\nbytes"


@pytest.mark.asyncio
async def test_card_client_posts_and_decodes_png():
    async def handler(request):
        assert request.url.path == "/card"
        return httpx.Response(200, json={"status": "ok", "pages": [base64.b64encode(PNG).decode()]})

    client = CardRenderClient("http://renderer", transport=httpx.MockTransport(handler))
    assert await client.render(validate_card({"title": "x"})) == [PNG]


@pytest.mark.asyncio
async def test_card_client_maps_renderer_errors_without_response_text():
    async def handler(request):
        return httpx.Response(200, json={"status": "busy"})

    client = CardRenderClient("http://renderer", transport=httpx.MockTransport(handler))
    with pytest.raises(CardError) as error:
        await client.render(validate_card({"title": "x"}))
    assert error.value.code == "busy"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("busy", "busy"),
        ("too_large", "too_large"),
        ("timeout", "timeout"),
        ("invalid", "unavailable"),
        ("error", "unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_card_client_maps_each_renderer_status(status, code):
    async def handler(_request):
        return httpx.Response(200, json={"status": status, "secret": "response text"})

    client = CardRenderClient("http://renderer", transport=httpx.MockTransport(handler))
    with pytest.raises(CardError) as error:
        await client.render(validate_card({"title": "x"}))
    assert error.value.code == code
    assert "response text" not in str(error.value)


@pytest.mark.parametrize("status", [[], 123, None])
@pytest.mark.asyncio
async def test_card_client_rejects_non_string_status(status):
    async def handler(_request):
        return httpx.Response(200, json={"status": status})

    client = CardRenderClient("http://renderer", transport=httpx.MockTransport(handler))
    with pytest.raises(CardError) as error:
        await client.render(validate_card({"title": "x"}))
    assert error.value.code == "unavailable"


@pytest.mark.parametrize(
    "body",
    [
        {"status": "ok", "pages": ["Y2FyZA=="]},
        {"status": "ok", "pages": ["not base64!"]},
        {"status": "ok", "pages": []},
    ],
)
@pytest.mark.asyncio
async def test_card_client_rejects_bad_png_payloads(body):
    async def handler(_request):
        return httpx.Response(200, json=body)

    client = CardRenderClient("http://renderer", transport=httpx.MockTransport(handler))
    with pytest.raises(CardError) as error:
        await client.render(validate_card({"title": "x"}))
    assert error.value.code == "unavailable"


@pytest.mark.asyncio
async def test_card_client_does_not_follow_redirect_or_include_response_text():
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(
            302, headers={"Location": "http://renderer/elsewhere"}, text="secret body"
        )

    client = CardRenderClient("http://renderer", transport=httpx.MockTransport(handler))
    with pytest.raises(CardError) as error:
        await client.render(validate_card({"title": "x"}))
    assert calls == ["/card"]
    assert error.value.code == "unavailable" and "secret body" not in str(error.value)


@pytest.mark.asyncio
async def test_card_client_maps_connection_timeout_and_http_500():
    async def connection(_request):
        raise httpx.ConnectError("private connection detail")

    client = CardRenderClient("http://renderer", transport=httpx.MockTransport(connection))
    with pytest.raises(CardError) as error:
        await client.render(validate_card({"title": "x"}))
    assert error.value.code == "unavailable" and "private connection detail" not in str(error.value)

    async def timeout(_request):
        raise httpx.ReadTimeout("private timeout detail")

    client = CardRenderClient("http://renderer", transport=httpx.MockTransport(timeout))
    with pytest.raises(CardError) as error:
        await client.render(validate_card({"title": "x"}))
    assert error.value.code == "timeout" and "private timeout detail" not in str(error.value)

    async def server_error(_request):
        return httpx.Response(500, text="private response body")

    client = CardRenderClient("http://renderer", transport=httpx.MockTransport(server_error))
    with pytest.raises(CardError) as error:
        await client.render(validate_card({"title": "x"}))
    assert error.value.code == "unavailable" and "private response body" not in str(error.value)


@pytest.mark.asyncio
async def test_card_client_caps_streamed_response():
    class Oversized(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (28 * 1024 * 1024 + 1)

        async def aclose(self):
            pass

    async def handler(_request):
        return httpx.Response(200, stream=Oversized())

    client = CardRenderClient("http://renderer", transport=httpx.MockTransport(handler))
    with pytest.raises(CardError) as error:
        await client.render(validate_card({"title": "x"}))
    assert error.value.code == "too_large"
