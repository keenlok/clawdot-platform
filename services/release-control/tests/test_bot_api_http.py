from __future__ import annotations

import httpx
import pytest
from chatcore_release_control.bot_api_http import BotApiHTTPTransport, BotTransportError


@pytest.mark.asyncio
async def test_transport_uses_fixed_origin_no_redirects_and_returns_only_result() -> None:
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "api.telegram.org"
        assert request.extensions["timeout"]["connect"] <= 5
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 19}})

    transport = BotApiHTTPTransport(http_transport=httpx.MockTransport(handler))
    result = await transport.call("sendMessage", {"chat_id": 7, "text": "ok"}, token="123:secret")
    assert result == {"message_id": 19}
    assert seen[0].url.path == "/bot123:secret/sendMessage"
    assert seen[0].extensions["timeout"]["connect"] == 5.0
    assert "123:secret" not in repr(transport)
    await transport.close()


@pytest.mark.asyncio
async def test_transport_rejects_redirects_and_does_not_expose_api_diagnostics() -> None:
    async def redirect(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://attacker.invalid/"})

    transport = BotApiHTTPTransport(http_transport=httpx.MockTransport(redirect))
    with pytest.raises(BotTransportError) as error:
        await transport.call("getMe", {}, token="123:secret")
    assert error.value.code == "upstream_error"
    assert "123:secret" not in repr(error.value)
    with pytest.raises(BotTransportError):
        await transport.call("setWebhook", {}, token="123:secret")
    await transport.close()


@pytest.mark.asyncio
async def test_document_is_multipart_and_download_proxies_bounded_bytes_without_url() -> None:
    paths = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/sendDocument"):
            assert b"file.bin" in await request.aread()
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "documents/a.bin"}}
            )
        if request.url.path.startswith("/file/bot"):
            return httpx.Response(200, content=b"file")
        raise AssertionError(request.url.path)

    transport = BotApiHTTPTransport(http_transport=httpx.MockTransport(handler))
    sent = await transport.call(
        "sendDocument",
        {"chat_id": 7, "document": b"bytes", "filename": "file.bin"},
        token="123:secret",
    )
    assert sent == {"message_id": 1}
    body = await transport.download_file("remote-file-id", 4, token="123:secret")
    assert body == b"file"
    assert paths == [
        "/bot123:secret/sendDocument",
        "/bot123:secret/getFile",
        "/file/bot123:secret/documents/a.bin",
    ]
    assert "123:secret" not in repr(transport) and body == b"file"
    await transport.close()


@pytest.mark.asyncio
async def test_download_rejects_unsafe_file_path_and_size_overflow() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "../secret"}})
        return httpx.Response(200, content=b"x" * 5)

    transport = BotApiHTTPTransport(http_transport=httpx.MockTransport(handler))
    with pytest.raises(BotTransportError):
        await transport.download_file("remote-file-id", 10, token="123:secret")
    with pytest.raises(BotTransportError):
        await transport.download_file("remote-file-id", 4, token="123:secret")
    await transport.close()
