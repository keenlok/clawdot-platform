"""Audio plugins must pass the real Telegram adapter's voice checks, not only FakePlatform."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from chatcore.access import AccessPolicy
from chatcore.events import ChatRef, User, UserRef
from chatcore.testing import memory_stores
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_telegram import TelegramPlatform, TelegramSettings
from chatcore_voice import VoicePlugin, VoiceSettings
from pydantic import SecretStr

HEADER = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"


def telegram(bot):
    platform = TelegramPlatform(TelegramSettings(bot_token=SecretStr("fake-token")))
    platform._application = SimpleNamespace(bot=bot)
    return platform


async def test_voice_narration_at_its_byte_cap_is_delivered_by_telegram():
    from chatcore_voice.service import MAX_AUDIO_BYTES

    bot = AsyncMock()
    bot.send_voice.return_value = SimpleNamespace(message_id=5)
    platform = telegram(bot)
    plugin = VoicePlugin(
        AccessPolicy(["tg:group"], []),
        platform,
        settings=VoiceSettings(
            VOICE_TTS_ENABLED=True,
            VOICE_ELEVENLABS_API_KEY=SecretStr("fake"),
            VOICE_TTS_VOICES="English=voice_en",
        ),
    )
    audio = HEADER + b"\x00" * (MAX_AUDIO_BYTES - len(HEADER))

    async def narrate(text, language, *, user_key):
        return audio

    plugin.narration.narrate = narrate
    tool = plugin.tools()[0]
    ctx = ToolContext(
        ChatRef("tg", "group"),
        User(UserRef("tg", "alice"), display_name="alice"),
        False,
        platform,
        memory_stores(),
        turn=TurnState(),
    )
    try:
        result = await ToolRegistry([tool]).call(
            tool.name, {"text": "Headlines.", "language": "English", "title": "News"}, ctx
        )
        assert not result.is_error, result.content
        bot.send_voice.assert_awaited_once()
        assert bot.send_voice.await_args.kwargs["caption"] == "🔊 News"
    finally:
        await plugin.aclose()


def test_tutor_audio_fits_the_telegram_voice_cap():
    from chatcore_telegram import MAX_VOICE_BYTES
    from chatcore_tutor.audio import MAX_AUDIO_BYTES

    assert MAX_AUDIO_BYTES <= MAX_VOICE_BYTES
