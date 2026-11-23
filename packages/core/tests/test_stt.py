import httpx
import pytest
from chatcore.stt import STTError, transcribe


@pytest.mark.asyncio
async def test_transcribe_posts_multipart_and_returns_text():
    seen = {}

    def handler(request):
        seen["request"] = request
        return httpx.Response(200, json={"text": "hello", "language_code": "en"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await transcribe(
        client, b"audio", key="fake", model="scribe_v2", keyterms=["claw dot"]
    )
    assert result == ("hello", "en")
    request = seen["request"]
    assert request.url.path == "/v1/speech-to-text"
    assert "multipart/form-data" in request.headers["content-type"]
    assert b"model_id" in request.content and b"scribe_v2" in request.content
    assert b"claw dot" in request.content
    assert request.headers["xi-api-key"] == "fake"
    await client.aclose()


@pytest.mark.asyncio
async def test_transcribe_hides_provider_response():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(502, text="private"))
    )
    with pytest.raises(STTError):
        await transcribe(client, b"audio", key="fake", model="scribe_v2")
    await client.aclose()
