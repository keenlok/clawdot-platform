from chatcore import tts
from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.events import ChatRef, User, UserRef
from chatcore.plugin import CommandContext
from chatcore.skill_router import SkillRouter
from chatcore.testing import FakePlatform, memory_stores
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_voice import VoicePlugin, VoiceSettings
from chatcore_voice.plugin import READ_ALOUD_INSTRUCTIONS
from chatcore_voice.service import NarrationError
from pydantic import SecretStr

MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"


def settings(**overrides):
    values = {
        "VOICE_TTS_ENABLED": True,
        "VOICE_ELEVENLABS_API_KEY": SecretStr("fake"),
        "VOICE_TTS_VOICES": "English=voice_en",
    }
    return VoiceSettings(**(values | overrides))


def make(platform=None, access=None, **overrides):
    platform = platform or FakePlatform(name="tg")
    access = access or AccessPolicy(["tg:group"], ["tg:owner"])
    return VoicePlugin(access, platform, settings=settings(**overrides)), platform


def ctx_for(platform, *, user="alice", chat="group", turn=None, thread_id=None):
    return ToolContext(
        ChatRef("tg", chat),
        User(UserRef("tg", user), display_name=user),
        False,
        platform,
        memory_stores(),
        thread_id=thread_id,
        turn=turn or TurnState(),
    )


async def call(plugin, ctx, **args):
    tool = next(tool for tool in plugin.tools() if tool.name == "narrate_text")
    args = {"text": "Today's headlines.", "language": "English", "title": "News"} | args
    return await ToolRegistry([tool]).call(tool.name, args, ctx)


def test_disabled_plugin_offers_nothing_and_does_not_need_tutor():
    plugin = VoicePlugin(AccessPolicy([], []), FakePlatform())
    assert plugin.tools() == [] and plugin.skills() == [] and plugin.tool_gates() == {}
    app = BotApp(
        FakePlatform(),
        stores=memory_stores(),
        access=AccessPolicy([], []),
        plugins=[plugin],
        bot_name="testbot",
    )
    assert app.skills.get("read_aloud") is None


def test_enabled_plugin_registers_skill_tool_and_gate():
    plugin, platform = make()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=plugin.access,
        plugins=[plugin],
        bot_name="testbot",
    )
    skill = app.skills.get("read_aloud")
    assert skill is not None and skill.tools == ("narrate_text",)
    assert len(READ_ALOUD_INSTRUCTIONS) <= 800
    assert set(plugin.tool_gates()) == {"narrate_text"}
    tool = plugin.tools()[0]
    assert tool.writes is False
    assert tool.parameters["properties"]["language"]["enum"] == ["English"]
    assert "3000" in tool.parameters["properties"]["text"]["description"]


async def test_success_sends_voice_with_caption_in_topic_once_per_turn():
    plugin, platform = make()
    calls = []

    async def narrate(text, language, *, user_key):
        calls.append((text, language, user_key))
        return MP3

    plugin.narration.narrate = narrate
    turn = TurnState()
    ctx = ctx_for(platform, turn=turn, thread_id="topic-3")
    result = await call(plugin, ctx)
    assert not result.is_error
    assert calls == [("Today's headlines.", "English", "tg:alice")]
    sent = platform.sent_voices[0]
    assert sent.caption == "🔊 News" and sent.thread_id == "topic-3"
    assert sent.data == MP3 and sent.filename == "narration.mp3"
    again = await call(plugin, ctx)
    assert again.is_error and len(calls) == 1


async def test_failure_consumes_turn_and_returns_safe_message():
    plugin, platform = make()

    async def fail(*args, **kwargs):
        raise NarrationError("quota")

    plugin.narration.narrate = fail
    turn = TurnState()
    result = await call(plugin, ctx_for(platform, turn=turn))
    assert result.is_error and "limit" in str(result.content).lower()
    assert turn.tool_attempts["narrate_text"] == 1
    assert not platform.sent_voices


async def test_invalid_args_and_denied_access_make_no_provider_call():
    plugin, platform = make()
    calls = []

    async def narrate(*args, **kwargs):
        calls.append(args)
        return MP3

    plugin.narration.narrate = narrate
    for args in ({"title": "two\nlines"}, {"title": ""}, {"text": "x" * 3001}):
        assert (await call(plugin, ctx_for(platform), **args)).is_error
    assert (await call(plugin, ctx_for(platform, chat="other"))).is_error
    assert not calls and not platform.sent_voices


async def test_access_is_rechecked_before_delivery():
    plugin, platform = make()

    async def narrate(*args, **kwargs):
        plugin.access.allowed_chats.discard("tg:group")
        return MP3

    plugin.narration.narrate = narrate
    result = await call(plugin, ctx_for(platform))
    assert result.is_error and not platform.sent_voices


async def test_gate_hides_tool_from_disallowed_chat():
    plugin, platform = make()
    gate = plugin.tool_gates()["narrate_text"]
    assert (await gate.user(ctx_for(platform))).state.name == "AVAILABLE"
    assert (await gate.user(ctx_for(platform, chat="other"))).state.name == "NOT_ALLOWED"


def command_ctx(platform, *, admin=True):
    return CommandContext(
        ChatRef("tg", "group"),
        User(UserRef("tg", "owner"), display_name="owner"),
        False,
        admin,
        platform,
        memory_stores(),
    )


async def test_voice_usage_command_is_admin_only_and_reports_without_secrets():
    plugin, platform = make()
    command = plugin.commands()[0]
    assert command.name == "voice" and command.admin_only

    async def fetch(client, *, key):
        return tts.SubscriptionUsage("active", 1_234, 100_000, 1_765_000_000, True, 0)

    plugin.narration.usage._fetch = fetch
    text = await command.handler(command_ctx(platform), "usage")
    assert "0 of 50,000" in text
    assert "1,234 of 100,000 used; 98,766 left" in text
    assert "Resets 2025-12-06" in text and "Overage billing is off." in text
    assert "fake" not in text and "voice_en" not in text


async def test_voice_usage_reports_unreadable_provider():
    plugin, platform = make()

    async def fetch(client, *, key):
        raise tts.TTSError("http_403")

    plugin.narration.usage._fetch = fetch
    text = await plugin.commands()[0].handler(command_ctx(platform), "usage")
    assert "Provider usage unavailable (http_403)." in text


async def test_voice_usage_when_unconfigured_and_usage_hint():
    plugin = VoicePlugin(AccessPolicy([], []), FakePlatform())
    command = plugin.commands()[0]
    assert await command.handler(command_ctx(FakePlatform()), "usage") == (
        "Voice narration is not configured."
    )
    assert "usage" in await command.handler(command_ctx(FakePlatform()), "")


async def test_plugin_close_closes_owned_client():
    plugin, _ = make()
    client = plugin.narration._client
    await plugin.aclose()
    assert client.is_closed


async def test_cantonese_language_and_read_aloud_phrases_route_by_keyword():
    plugin, platform = make(VOICE_TTS_VOICES="English=voice_en,Cantonese=voice_yue")
    assert plugin.tools()[0].parameters["properties"]["language"]["enum"] == [
        "English",
        "Cantonese",
    ]
    app = BotApp(
        platform, stores=memory_stores(), access=plugin.access, plugins=[plugin], bot_name="t"
    )
    router = SkillRouter(
        app.skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05
    )
    for phrase in ("將呢段新聞讀出嚟", "可唔可以念出嚟", "唸出嚟俾我聽", "read that out loud"):
        result = await router.route(phrase)
        assert "read_aloud" in [hit.skill for hit in result.hits], phrase
