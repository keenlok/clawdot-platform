import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from chatcore import tts
from chatcore_voice.service import NarrationError, NarrationService
from chatcore_voice.settings import VoiceSettings, parse_voices
from pydantic import SecretStr, ValidationError

MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64synthetic"


def configured(**overrides):
    values = {
        "VOICE_TTS_ENABLED": True,
        "VOICE_ELEVENLABS_API_KEY": SecretStr("test-key"),
        "VOICE_TTS_VOICES": "English=voice_en,Japanese=voice_ja",
    }
    return VoiceSettings(**(values | overrides))


def usage(**changes):
    values = {
        "status": "active",
        "character_count": 0,
        "character_limit": 100_000,
        "next_reset_unix": None,
        "can_extend": False,
        "max_extension": 0,
    }
    return tts.SubscriptionUsage(**(values | changes))


class Fetch:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result or usage(), error, 0

    async def __call__(self, client, *, key):
        self.calls += 1
        if self.error:
            raise tts.TTSError(self.error)
        return self.result


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def service(settings=None, *, fetch=None, handler=None, clock=None, utcnow=None):
    seen = []

    async def default(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=MP3)

    svc = NarrationService(
        settings or configured(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler or default)),
        monotonic=clock or Clock(),
        utcnow=utcnow or (lambda: datetime(2026, 11, 9, tzinfo=UTC)),
        fetch_usage=fetch or Fetch(),
    )
    return svc, seen


def test_voice_settings_parse_and_reject_bad_configuration():
    assert parse_voices(" English = a_1 , Malay=b-2") == {"English": "a_1", "Malay": "b-2"}
    assert parse_voices("") == {}
    for bad in ("Klingon=abc", "English=", "English=a b", "English=a,English=b", "English"):
        with pytest.raises(ValueError):
            parse_voices(bad)
    with pytest.raises(ValidationError):
        VoiceSettings(VOICE_TTS_MODEL="eleven_v3")
    with pytest.raises(ValidationError):
        VoiceSettings(VOICE_TTS_USAGE_CHECK="sometimes")
    defaults = VoiceSettings()
    assert defaults.VOICE_TTS_ENABLED is False
    assert defaults.VOICE_TTS_MAX_CHARS_PER_DAY == 50_000
    assert defaults.VOICE_TTS_USAGE_CHECK == "required"


async def test_success_uses_configured_voice_model_and_low_bitrate():
    svc, seen = service()
    assert await svc.narrate("Hello there.", "English", user_key="u") == MP3
    request = seen[0]
    assert request.url.path == "/v1/text-to-speech/voice_en"
    assert request.url.params["output_format"] == "mp3_44100_64"
    assert json.loads(request.read()) == {
        "text": "Hello there.",
        "model_id": "eleven_multilingual_v2",
    }
    assert svc.languages == ("English", "Japanese")


async def test_flash_model_sends_language_code():
    svc, seen = service(configured(VOICE_TTS_MODEL="eleven_flash_v2_5"))
    await svc.narrate("こんにちは", "Japanese", user_key="u")
    assert json.loads(seen[0].read())["language_code"] == "ja"


@pytest.mark.parametrize(
    ("text", "language", "code"),
    [
        ("", "English", "invalid_text"),
        ("x" * 3001, "English", "invalid_text"),
        ("hi\x00", "English", "invalid_text"),
        ("hi", "Malay", "invalid_language"),
        ("hi", "english", "invalid_language"),
    ],
)
async def test_invalid_requests_make_no_calls(text, language, code):
    fetch = Fetch()
    svc, seen = service(fetch=fetch)
    with pytest.raises(NarrationError) as caught:
        await svc.narrate(text, language, user_key="u")
    assert caught.value.code == code
    assert not seen and fetch.calls == 0


async def test_per_request_limit_is_configurable():
    svc, _ = service(configured(VOICE_TTS_MAX_CHARS_PER_REQUEST=10))
    with pytest.raises(NarrationError):
        await svc.narrate("x" * 11, "English", user_key="u")
    await svc.narrate("x" * 10, "English", user_key="u")


async def test_user_hour_cap_counts_characters_and_expires():
    clock = Clock()
    svc, seen = service(configured(VOICE_TTS_MAX_CHARS_PER_USER_HOUR=25), clock=clock)
    await svc.narrate("a" * 20, "English", user_key="alice")
    with pytest.raises(NarrationError) as caught:
        await svc.narrate("a" * 6, "English", user_key="alice")
    assert caught.value.code == "quota"
    await svc.narrate("a" * 20, "English", user_key="bob")
    clock.now += 3600
    await svc.narrate("a" * 20, "English", user_key="alice")
    assert len(seen) == 3


async def test_daily_cap_resets_at_utc_midnight():
    day = [datetime(2026, 11, 9, 23, 59, tzinfo=UTC)]
    svc, seen = service(configured(VOICE_TTS_MAX_CHARS_PER_DAY=30), utcnow=lambda: day[0])
    await svc.narrate("a" * 20, "English", user_key="a")
    with pytest.raises(NarrationError) as caught:
        await svc.narrate("a" * 20, "English", user_key="b")
    assert caught.value.code == "quota"
    day[0] += timedelta(minutes=2)
    await svc.narrate("a" * 20, "English", user_key="b")
    assert len(seen) == 2


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (usage(status="past_due"), "account_inactive"),
        (usage(character_count=99_000), "provider_quota"),
        (usage(can_extend=True, max_extension=None), "overage_enabled"),
        (usage(can_extend=True, max_extension=500), "overage_enabled"),
    ],
)
async def test_provider_preflight_refuses_without_tts_call_or_local_charge(result, code):
    svc, seen = service(fetch=Fetch(result))
    with pytest.raises(NarrationError) as caught:
        await svc.narrate("a" * 100, "English", user_key="u")
    assert caught.value.code == code
    assert not seen
    report = await svc.report()
    assert report.day_chars == 0
    assert svc._active == 0


async def test_overage_allowed_by_operator():
    svc, seen = service(
        configured(VOICE_TTS_ALLOW_OVERAGE=True),
        fetch=Fetch(usage(can_extend=True, max_extension=None)),
    )
    await svc.narrate("hello", "English", user_key="u")
    assert len(seen) == 1


async def test_reserve_margin_and_local_spend_between_cache_refreshes():
    fetch = Fetch(usage(character_count=100_000 - 2_000 - 150))
    clock = Clock()
    svc, seen = service(fetch=fetch, clock=clock)
    await svc.narrate("a" * 100, "English", user_key="u")
    with pytest.raises(NarrationError) as caught:
        await svc.narrate("a" * 100, "English", user_key="u")
    assert caught.value.code == "provider_quota"
    assert fetch.calls == 1 and len(seen) == 1
    fetch.result = usage()
    clock.now += 60
    await svc.narrate("a" * 100, "English", user_key="u")
    assert fetch.calls == 2 and len(seen) == 2


@pytest.mark.parametrize("error", ["http_403", "timeout", "invalid"])
async def test_required_usage_check_fails_closed(error):
    svc, seen = service(fetch=Fetch(error=error))
    with pytest.raises(NarrationError) as caught:
        await svc.narrate("hello", "English", user_key="u")
    assert caught.value.code == "usage_unavailable"
    assert not seen


async def test_advisory_usage_check_continues_and_off_never_fetches():
    svc, seen = service(configured(VOICE_TTS_USAGE_CHECK="advisory"), fetch=Fetch(error="http_401"))
    await svc.narrate("hello", "English", user_key="u")
    fetch = Fetch()
    off, off_seen = service(configured(VOICE_TTS_USAGE_CHECK="off"), fetch=fetch)
    await off.narrate("hello", "English", user_key="u")
    assert len(seen) == 1 and len(off_seen) == 1 and fetch.calls == 0


async def test_provider_failure_keeps_charge_and_is_opaque():
    async def handler(request):
        return httpx.Response(500, text="secret body")

    svc, _ = service(handler=handler)
    with pytest.raises(NarrationError) as caught:
        await svc.narrate("hello", "English", user_key="u")
    assert caught.value.code == "provider"
    assert svc.usage._spent == 5 and svc.usage._pending == 0
    assert svc._active == 0
    assert (await svc.report()).day_chars == 5


async def test_third_concurrent_narration_is_busy_without_charge():
    release = asyncio.Event()

    async def handler(request):
        await release.wait()
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=MP3)

    svc, _ = service(handler=handler)
    first = asyncio.create_task(svc.narrate("one", "English", user_key="a"))
    second = asyncio.create_task(svc.narrate("two", "English", user_key="b"))
    await asyncio.sleep(0.01)
    with pytest.raises(NarrationError) as caught:
        await svc.narrate("three", "English", user_key="c")
    assert caught.value.code == "busy"
    assert (await svc.report()).day_chars == 6
    release.set()
    assert await first == MP3 and await second == MP3
    assert svc._active == 0


async def test_disabled_or_unconfigured_is_unavailable():
    for settings in (
        configured(VOICE_TTS_ENABLED=False),
        configured(VOICE_ELEVENLABS_API_KEY=SecretStr("")),
        configured(VOICE_TTS_VOICES=""),
        configured(VOICE_TTS_MAX_CHARS_PER_DAY=0),
    ):
        svc, seen = service(settings)
        assert not svc.available
        with pytest.raises(NarrationError):
            await svc.narrate("hello", "English", user_key="u")
        assert not seen


async def test_report_forces_fresh_usage_read():
    fetch = Fetch()
    svc, _ = service(fetch=fetch)
    await svc.narrate("hello", "English", user_key="u")
    report = await svc.report()
    assert fetch.calls == 2
    assert report.provider == usage() and report.provider_error is None
    fetch.error = "http_403"
    report = await svc.report()
    assert report.provider is None and report.provider_error == "http_403"


async def test_cantonese_always_uses_eleven_v4_with_yue_while_others_keep_configured_model():
    svc, seen = service(configured(VOICE_TTS_VOICES="English=voice_en,Cantonese=voice_yue"))
    assert svc.languages == ("English", "Cantonese")
    await svc.narrate("今日有三單新聞。", "Cantonese", user_key="u")
    await svc.narrate("Three stories today.", "English", user_key="u")
    cantonese, english = (json.loads(request.read()) for request in seen)
    assert seen[0].url.path == "/v1/text-to-speech/voice_yue"
    assert cantonese == {
        "text": "今日有三單新聞。",
        "model_id": "eleven_v4",
        "language_code": "yue",
    }
    assert english == {"text": "Three stories today.", "model_id": "eleven_multilingual_v2"}


def test_cantonese_model_is_pinned_even_when_another_model_is_configured():
    settings = configured(VOICE_TTS_MODEL="eleven_flash_v2_5")
    assert settings.model_for("Cantonese") == "eleven_v4"
    assert settings.model_for("Japanese") == "eleven_flash_v2_5"
