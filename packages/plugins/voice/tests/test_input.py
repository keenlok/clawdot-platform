from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from chatcore.events import Attachment, AttachmentKind, ChatRef, IncomingMessage, User, UserRef
from chatcore.testing import FakePlatform
from chatcore_voice.input import TranscriptionService, strip_wake_word
from chatcore_voice.settings import VoiceSettings
from pydantic import SecretStr


def test_wake_word_matches_normalized_spelling_and_strips_phrase():
    assert (
        strip_wake_word("Hey clawdot, what's the weather", ("hey claw dot",))
        == "what's the weather"
    )
    assert strip_wake_word("claw dot what's next", ("clawdot",)) == "what's next"


def test_wake_word_must_start_within_first_five_words():
    assert strip_wake_word("one two three four five hey clawdot", ("hey clawdot",)) is None


def _message(attachment):
    return IncomingMessage(
        ChatRef("tg", "123"),
        "1",
        User(UserRef("tg", "123"), "Person"),
        "",
        True,
        attachments=(attachment,),
    )


@pytest.mark.asyncio
async def test_over_cap_note_is_refused_before_download():
    platform = FakePlatform()
    settings = VoiceSettings(
        VOICE_STT_ENABLED=True,
        VOICE_STT_ELEVENLABS_API_KEY=SecretStr("fake"),
        VOICE_STT_MAX_SECONDS_PER_USER_DAY=2,
        VOICE_STT_USAGE_CHECK="off",
    )
    service = TranscriptionService(settings)
    message = _message(Attachment("voice", AttachmentKind.VOICE, duration_s=3))
    result, error = await service.transcribe(
        message, SimpleNamespace(platform=platform), message.attachments[0]
    )
    assert result is message and error == "stt_quota"
    assert not platform.files
    await service.aclose()


@pytest.mark.asyncio
async def test_successful_transcription_uses_mocked_provider():
    platform = FakePlatform(files={"voice": b"audio"})
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"text": "hello", "language_code": "en"})
        )
    )
    settings = VoiceSettings(
        VOICE_STT_ENABLED=True,
        VOICE_STT_ELEVENLABS_API_KEY=SecretStr("fake"),
        VOICE_STT_USAGE_CHECK="off",
    )
    service = TranscriptionService(settings, client=client)
    message = _message(Attachment("voice", AttachmentKind.VOICE, duration_s=1))
    result, error = await service.transcribe(
        message, SimpleNamespace(platform=platform), message.attachments[0]
    )
    assert error is None
    assert result.text == "hello" and result.voice is not None
    assert result.voice.language == "en" and result.voice.source == "self"
    await service.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_unknown_duration_with_exhausted_global_cap_skips_download():
    platform = FakePlatform(files={"voice": b"audio"})
    platform.download_calls = 0
    download = platform.download

    async def counted_download(*args, **kwargs):
        platform.download_calls += 1
        return await download(*args, **kwargs)

    platform.download = counted_download
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("provider called"))
    )
    service = TranscriptionService(
        VoiceSettings(
            VOICE_STT_ENABLED=True,
            VOICE_STT_ELEVENLABS_API_KEY=SecretStr("fake"),
            VOICE_STT_MAX_SECONDS_PER_DAY=0,
            VOICE_STT_USAGE_CHECK="off",
        ),
        client=client,
    )
    message = _message(Attachment("voice", AttachmentKind.VOICE))
    _, error = await service.transcribe(
        message, SimpleNamespace(platform=platform), message.attachments[0]
    )
    assert error == "stt_quota" and platform.download_calls == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_unknown_wake_duration_with_exhausted_chat_allowance_skips_download():
    platform = FakePlatform(files={"voice": b"audio"})
    downloads = []
    original_download = platform.download

    async def download(*args, **kwargs):
        downloads.append(args[0])
        return await original_download(*args, **kwargs)

    platform.download = download
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("provider called"))
    )
    settings = VoiceSettings(
        VOICE_STT_ENABLED=True,
        VOICE_STT_ELEVENLABS_API_KEY=SecretStr("fake"),
        VOICE_STT_USAGE_CHECK="off",
    )
    service = TranscriptionService(settings, client=client)
    message = _message(Attachment("voice", AttachmentKind.VOICE))
    day = datetime.now(UTC).date().isoformat()
    service.daily[(day, f"chat:{message.chat.key}")] = settings.VOICE_STT_MAX_SECONDS_PER_DAY
    _, error = await service.transcribe(
        message, SimpleNamespace(platform=platform), message.attachments[0], wake_words=("hey dot",)
    )
    assert error == "stt_quota" and downloads == []
    await client.aclose()


@pytest.mark.asyncio
async def test_wake_estimate_over_wake_limit_skips_provider():
    platform = FakePlatform(files={"voice": b"x" * (61 * 16_000)})
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("provider called"))
    )
    service = TranscriptionService(
        VoiceSettings(
            VOICE_STT_ENABLED=True,
            VOICE_STT_ELEVENLABS_API_KEY=SecretStr("fake"),
            VOICE_STT_USAGE_CHECK="off",
        ),
        client=client,
    )
    message = _message(Attachment("voice", AttachmentKind.VOICE))
    _, error = await service.transcribe(
        message, SimpleNamespace(platform=platform), message.attachments[0], wake_words=("hey dot",)
    )
    assert error == "wake_long"
    await client.aclose()


@pytest.mark.asyncio
async def test_replied_voice_transcript_is_added_without_losing_typed_request():
    platform = FakePlatform(files={"voice": b"audio"})
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"text": "heard"}))
    )
    service = TranscriptionService(
        VoiceSettings(
            VOICE_STT_ENABLED=True,
            VOICE_STT_ELEVENLABS_API_KEY=SecretStr("fake"),
            VOICE_STT_USAGE_CHECK="off",
        ),
        client=client,
    )
    message = _message(Attachment("voice", AttachmentKind.VOICE, duration_s=1))
    message = replace(message, text="What did they say?")
    result, error = await service.transcribe(
        message, SimpleNamespace(platform=platform), message.attachments[0], source="replied"
    )
    assert error is None and result.voice is not None
    assert result.voice.transcript == "heard"
    assert result.text == "What did they say?\n\n[voice transcript: heard]"
    await service.aclose()
    await client.aclose()
