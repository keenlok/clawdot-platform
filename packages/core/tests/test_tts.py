import json

import httpx
import pytest
from chatcore import tts

MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64synthetic"
SUBSCRIPTION = {
    "tier": "creator",
    "status": "active",
    "character_count": 1200,
    "character_limit": 100_000,
    "next_character_count_reset_unix": 1_765_000_000,
    "can_extend_character_limit": False,
    "max_credit_limit_extension": 0,
    "next_invoice": {"amount_due_cents": 2200},
}


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_synthesize_omits_language_code_when_not_given():
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=MP3)

    async with client(handler) as http:
        audio = await tts.synthesize(
            http,
            key="k",
            text="hello",
            voice="v1",
            model="eleven_multilingual_v2",
            output_format="mp3_44100_64",
            max_bytes=1024,
        )
    assert audio == MP3
    assert json.loads(seen[0].read()) == {"text": "hello", "model_id": "eleven_multilingual_v2"}
    assert seen[0].url.params["output_format"] == "mp3_44100_64"


async def test_synthesize_enforces_byte_cap_while_streaming():
    async def handler(request):
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=MP3 * 100)

    async with client(handler) as http:
        with pytest.raises(tts.TTSError) as caught:
            await tts.synthesize(
                http, key="k", text="x", voice="v", model="m", output_format="f", max_bytes=100
            )
    assert caught.value.code == "invalid_audio"


async def test_unexpected_transport_error_is_opaque_with_class_detail():
    async def handler(request):
        raise httpx.ConnectError("secret detail")

    async with client(handler) as http:
        with pytest.raises(tts.TTSError) as caught:
            await tts.synthesize(
                http, key="k", text="x", voice="v", model="m", output_format="f", max_bytes=100
            )
    assert caught.value.code == "provider"
    assert caught.value.detail == "ConnectError"
    assert "secret" not in str(caught.value)


async def test_fetch_subscription_parses_usage_fields_only():
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, json=SUBSCRIPTION)

    async with client(handler) as http:
        usage = await tts.fetch_subscription(http, key="secret-key")
    assert seen[0].url == "https://api.elevenlabs.io/v1/user/subscription"
    assert seen[0].headers["xi-api-key"] == "secret-key"
    assert usage == tts.SubscriptionUsage("active", 1200, 100_000, 1_765_000_000, False, 0)
    assert usage.remaining == 98_800
    assert not usage.overage_possible


@pytest.mark.parametrize(
    ("can_extend", "extension", "possible"),
    [(True, "unlimited", True), (True, 500, True), (True, 0, False), (False, "unlimited", False)],
)
def test_overage_possible(can_extend, extension, possible):
    usage = tts.parse_subscription(
        SUBSCRIPTION
        | {"can_extend_character_limit": can_extend, "max_credit_limit_extension": extension}
    )
    assert usage.overage_possible is possible
    assert usage.max_extension == (None if extension == "unlimited" else extension)


@pytest.mark.parametrize(
    "change",
    [
        {"character_count": "1200"},
        {"character_limit": -1},
        {"character_count": True},
        {"status": None},
        {"can_extend_character_limit": "no"},
        {"max_credit_limit_extension": None},
        {"next_character_count_reset_unix": "soon"},
    ],
)
async def test_malformed_subscription_is_invalid(change):
    async def handler(request):
        return httpx.Response(200, json=SUBSCRIPTION | change)

    async with client(handler) as http:
        with pytest.raises(tts.TTSError) as caught:
            await tts.fetch_subscription(http, key="k")
    assert caught.value.code == "invalid"


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_subscription_http_error_code_hides_body(status):
    async def handler(request):
        return httpx.Response(status, text="body with secret")

    async with client(handler) as http:
        with pytest.raises(tts.TTSError) as caught:
            await tts.fetch_subscription(http, key="k")
    assert caught.value.code == f"http_{status}"


async def test_subscription_response_is_size_bounded():
    async def handler(request):
        return httpx.Response(200, content=b" " * (70 * 1024))

    async with client(handler) as http:
        with pytest.raises(tts.TTSError) as caught:
            await tts.fetch_subscription(http, key="k")
    assert caught.value.code == "invalid"


async def test_subscription_timeout():
    async def handler(request):
        raise httpx.ReadTimeout("slow")

    async with client(handler) as http:
        with pytest.raises(tts.TTSError) as caught:
            await tts.fetch_subscription(http, key="k")
    assert caught.value.code == "timeout"


def test_valid_speech_text():
    assert tts.valid_speech_text("line one\nline two", max_chars=20)
    assert not tts.valid_speech_text("   ", max_chars=20)
    assert not tts.valid_speech_text("a" * 21, max_chars=20)
    assert not tts.valid_speech_text("bad\x00", max_chars=20)
    assert not tts.valid_speech_text(5, max_chars=20)
