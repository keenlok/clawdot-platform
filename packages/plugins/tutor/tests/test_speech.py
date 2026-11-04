import json

import httpx
import pytest
from chatcore_tutor.settings import TutorSettings
from chatcore_tutor.speech import SpeechError, SpeechService
from pydantic import SecretStr

MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64synthetic"


def configured(**overrides):
    return TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("test-key"),
        TUTOR_TTS_CANTONESE_VOICE_ID="voice_yue",
        TUTOR_TTS_MANDARIN_VOICE_ID="voice_zh",
        TUTOR_TTS_JAPANESE_VOICE_ID="voice_ja",
        **overrides,
    )


@pytest.mark.parametrize(
    ("language", "code", "model", "voice"),
    [
        ("Cantonese", "yue", "eleven_v4", "voice_yue"),
        ("Mandarin Chinese", "zh", "eleven_v3", "voice_zh"),
        ("Japanese", "ja", "eleven_v3", "voice_ja"),
    ],
)
async def test_provider_request_uses_language_specific_fixed_model_and_voice(
    language, code, model, voice
):
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=MP3)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = SpeechService(configured(), client=client)
    assert not seen
    audio = await service.synthesize("你好", language)
    assert audio == MP3
    request = seen[0]
    assert request.url.scheme == "https" and request.url.host == "api.elevenlabs.io"
    assert request.url.path == f"/v1/text-to-speech/{voice}"
    assert request.headers["xi-api-key"] == "test-key"
    body = request.read().decode()
    assert json.loads(body) == {"text": "你好", "model_id": model, "language_code": code}
    assert request.url.params.get_list("output_format") == ["mp3_44100_128"]
    assert '"speed"' not in body
    await service.aclose()


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_non_200_provider_body_is_hidden_from_safe_error(status):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="secret provider diagnostic")

    service = SpeechService(
        configured(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(SpeechError) as error:
        await service.synthesize("x", "Japanese")
    assert "secret provider diagnostic" not in str(error.value)
    assert calls == 1
    await service.aclose()


async def test_aggregate_quota_and_busy_slot_reject_before_provider_call():
    calls = 0
    entered = __import__("asyncio").Event()
    release = __import__("asyncio").Event()

    async def handler(request):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=MP3)

    service = SpeechService(
        configured(TUTOR_TTS_MAX_CHARS_PER_DAY=1),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    import asyncio

    first = asyncio.create_task(service.synthesize("a", "Japanese", user_key="tg:1"))
    await entered.wait()
    with pytest.raises(SpeechError):
        await service.synthesize("b", "Japanese", user_key="tg:2")
    assert calls == 1
    release.set()
    await first
    await service.aclose()


async def test_third_concurrent_job_is_rejected_without_provider_call():
    import asyncio

    entered = 0
    two_entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        nonlocal entered
        entered += 1
        if entered == 2:
            two_entered.set()
        await release.wait()
        return httpx.Response(200, headers={"content-type": "audio/mp3"}, content=MP3)

    service = SpeechService(
        configured(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    first = asyncio.create_task(service.synthesize("a", "Japanese", user_key="tg:1"))
    second = asyncio.create_task(service.synthesize("b", "Japanese", user_key="tg:2"))
    await two_entered.wait()
    with pytest.raises(SpeechError, match="busy"):
        await service.synthesize("c", "Japanese", user_key="tg:3")
    assert entered == 2
    release.set()
    await asyncio.gather(first, second)
    await service.aclose()


async def test_streamed_audio_cap_is_enforced_without_trusting_headers():
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield MP3
            yield b"x" * (2 * 1024 * 1024)

    async def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "audio/mpeg", "content-length": "1"},
            stream=Chunks(),
        )

    service = SpeechService(
        configured(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(SpeechError, match="invalid_audio"):
        await service.synthesize("a", "Japanese")
    await service.aclose()


@pytest.mark.parametrize("audio", [MP3, b"\xff\xfb\x90\x64synthetic"])
async def test_accepts_either_valid_mp3_signature(audio):
    async def handler(request):
        return httpx.Response(200, headers={"content-type": "audio/mp3"}, content=audio)

    service = SpeechService(
        configured(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert await service.synthesize("a", "Japanese") == audio
    await service.aclose()


async def test_redirect_is_not_followed_and_timeout_is_sanitized():
    calls = []

    async def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://elsewhere.invalid"})

    service = SpeechService(
        configured(), client=httpx.AsyncClient(transport=httpx.MockTransport(redirect))
    )
    with pytest.raises(SpeechError, match="provider"):
        await service.synthesize("a", "Japanese")
    assert len(calls) == 1
    await service.aclose()

    async def timeout(request):
        raise httpx.ReadTimeout("secret timeout diagnostic")

    service = SpeechService(
        configured(), client=httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    )
    with pytest.raises(SpeechError, match="timeout") as error:
        await service.synthesize("a", "Japanese")
    assert "secret timeout diagnostic" not in str(error.value)
    await service.aclose()


async def test_provider_cancellation_keeps_usage_charge_and_releases_slot():
    import asyncio

    entered = asyncio.Event()
    wait_forever = asyncio.Event()

    async def handler(request):
        entered.set()
        await wait_forever.wait()
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=MP3)

    service = SpeechService(
        configured(TUTOR_TTS_MAX_CHARS_PER_DAY=1),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    task = asyncio.create_task(service.synthesize("a", "Japanese"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service._active == 0 and service._day_chars == 1
    with pytest.raises(SpeechError, match="quota"):
        await service.synthesize("b", "Japanese")
    await service.aclose()


async def test_daily_quota_rolls_at_utc_midnight_and_user_hour_is_capped():
    from datetime import UTC, datetime, timedelta

    instant = datetime(2026, 11, 4, 23, 59, tzinfo=UTC)
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=MP3)

    service = SpeechService(
        configured(TUTOR_TTS_MAX_CHARS_PER_DAY=1),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        monotonic=lambda: 100,
        utcnow=lambda: instant,
    )
    await service.synthesize("a", "Japanese", user_key="tg:1")
    with pytest.raises(SpeechError, match="quota"):
        await service.synthesize("b", "Japanese", user_key="tg:1")
    instant += timedelta(minutes=2)
    await service.synthesize("b", "Japanese", user_key="tg:1")
    assert calls == 2
    await service.aclose()

    service = SpeechService(
        configured(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        monotonic=lambda: 200,
    )
    for _ in range(20):
        await service.synthesize("a", "Japanese", user_key="tg:limit")
    with pytest.raises(SpeechError, match="quota"):
        await service.synthesize("a", "Japanese", user_key="tg:limit")
    assert calls == 22
    await service.aclose()


async def test_user_hour_expires_and_full_user_map_fails_closed():
    from collections import deque

    clock = [0.0]
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=MP3)

    service = SpeechService(
        configured(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        monotonic=lambda: clock[0],
    )
    for _ in range(20):
        await service.synthesize("a", "Japanese", user_key="tg:hour")
    with pytest.raises(SpeechError, match="quota"):
        await service.synthesize("a", "Japanese", user_key="tg:hour")
    clock[0] = 3600
    await service.synthesize("a", "Japanese", user_key="tg:hour")
    assert calls == 21

    service._users = {f"u{i}": deque([3600]) for i in range(10_000)}
    with pytest.raises(SpeechError, match="quota"):
        await service.synthesize("a", "Japanese", user_key="new")
    assert calls == 21 and len(service._users) == 10_000
    await service.aclose()


async def test_owned_client_closes_and_injected_client_remains_caller_owned():
    owned = SpeechService(configured())
    owned_client = owned._client
    await owned.aclose()
    assert owned_client.is_closed

    injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None))
    service = SpeechService(configured(), client=injected)
    await service.aclose()
    assert not injected.is_closed
    await injected.aclose()


@pytest.mark.parametrize(
    ("text", "language"),
    [
        (None, "Japanese"),
        ("", "Japanese"),
        ("x" * 121, "Japanese"),
        ("\x00", "Japanese"),
        ("\ud800", "Japanese"),
        ("x", ["Japanese"]),
    ],
)
async def test_invalid_service_inputs_fail_before_http_or_quota(text, language):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=MP3)

    service = SpeechService(
        configured(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(SpeechError):
        await service.synthesize(text, language)
    assert calls == 0 and service._day_chars == 0 and service._active == 0
    await service.aclose()


async def test_zero_budget_rejects_even_empty_text_without_http():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=MP3)

    service = SpeechService(
        configured(TUTOR_TTS_MAX_CHARS_PER_DAY=0),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SpeechError):
        await service.synthesize("", "Japanese")
    with pytest.raises(SpeechError, match="quota"):
        await service.synthesize("a", "Japanese")
    assert calls == 0 and service._day_chars == 0
    await service.aclose()


async def test_slow_processing_remains_inside_worker_reservation_and_normal_skips_it():
    import asyncio

    entered = 0
    provider_calls = 0
    processing = 0
    two_processing = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=MP3)

    async def processor(audio):
        nonlocal entered, processing
        entered += 1
        processing += 1
        if processing == 2:
            two_processing.set()
        await release.wait()
        processing -= 1
        return audio

    service = SpeechService(
        configured(TUTOR_TTS_MAX_CHARS_PER_DAY=10),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        slow_processor=processor,
        ffmpeg_checker=lambda: True,
    )
    await service.synthesize("n", "Japanese")
    assert entered == 0 and provider_calls == 1
    first = asyncio.create_task(service.synthesize("a", "Japanese", pace="slow", user_key="1"))
    second = asyncio.create_task(service.synthesize("b", "Japanese", pace="slow", user_key="2"))
    await two_processing.wait()
    with pytest.raises(SpeechError, match="busy"):
        await service.synthesize("c", "Japanese", pace="slow", user_key="3")
    assert provider_calls == 3 and entered == 2 and service._day_chars == 3
    release.set()
    await asyncio.gather(first, second)
    await service.synthesize("c", "Japanese", pace="slow", user_key="3")
    assert provider_calls == 4 and entered == 3 and service._day_chars == 4
    await service.aclose()
