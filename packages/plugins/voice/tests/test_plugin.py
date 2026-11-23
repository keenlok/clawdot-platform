from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from chatcore import tts
from chatcore.access import AccessPolicy
from chatcore.administration.store import AccessSnapshot, Restriction
from chatcore.app import BotApp
from chatcore.events import (
    Attachment,
    AttachmentKind,
    ChatRef,
    IncomingMessage,
    MessageRef,
    User,
    UserRef,
)
from chatcore.plugin import BasePlugin, CommandContext
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
    assert set(plugin.tool_gates()) == {"narrate_text", "voice_settings"}
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


async def test_read_aloud_pins_voice_turn_when_reply_setting_is_on():
    plugin, platform = make()
    ctx = ctx_for(platform)
    ctx.source_message_id = "message-1"
    plugin._voice_turn_ids[(ctx.chat.key, "message-1")] = None
    skill = next(skill for skill in plugin.skills() if skill.name == "read_aloud")
    assert await skill.pin(ctx)


async def test_read_aloud_does_not_pin_when_voice_reply_is_off():
    plugin, platform = make()
    ctx = ctx_for(platform)

    class SettingsDB:
        async def fetchrow(self, sql, chat_key):
            return {"voice_reply": False}

    ctx.stores.db = SettingsDB()
    ctx.source_message_id = "message-2"
    plugin._voice_turn_ids[(ctx.chat.key, "message-2")] = None
    skill = next(skill for skill in plugin.skills() if skill.name == "read_aloud")
    assert not await skill.pin(ctx)


async def test_voice_settings_command_denies_group_non_admin_and_allows_dm_user():
    plugin, platform = make()

    class SettingsDB:
        async def fetchrow(self, sql, chat_key):
            return None

        async def execute(self, sql, *args):
            return "INSERT 0 1"

    stores = memory_stores()
    stores.db = SettingsDB()
    denied = await plugin._update_settings(
        stores,
        ChatRef("tg", "group"),
        User(UserRef("tg", "alice"), "alice"),
        platform,
        is_dm=False,
        is_admin=False,
        setting="reply",
        value="off",
    )
    assert "Only a group admin" in denied
    allowed = await plugin._update_settings(
        stores,
        ChatRef("tg", "owner"),
        User(UserRef("tg", "owner"), "owner"),
        platform,
        is_dm=True,
        is_admin=False,
        setting="reply",
        value="off",
    )
    assert allowed == "Voice reply set to off."


@pytest.mark.asyncio
async def test_settings_authorization_runs_through_command_and_tool_handlers():
    class NativeAdminPlatform(FakePlatform):
        group_admins: set[str] = set()

        async def is_group_admin(self, chat, user_ref):
            return user_ref.key in self.group_admins

    platform = NativeAdminPlatform(name="tg")
    platform.members["tg:group"] = {"tg:person", "tg:native-admin", "tg:dm-person"}
    platform.group_admins = {"tg:native-admin"}
    access = AccessPolicy(["tg:group"], [])
    plugin, _ = make(platform, access=access)

    class SettingsDB:
        def __init__(self):
            self.chats = []

        async def fetchrow(self, sql, chat_key):
            return None

        async def execute(self, sql, *args):
            self.chats.append(args[0])
            return "INSERT 0 1"

    stores = memory_stores()
    stores.db = SettingsDB()
    command = plugin.commands()[0]
    group_user = User(UserRef("tg", "person"), "person")
    cmd_ctx = CommandContext(ChatRef("tg", "group"), group_user, False, False, platform, stores)
    assert "Only a group admin" in await command.handler(cmd_ctx, "reply off")

    tool = next(item for item in plugin.tools() if item.name == "voice_settings")
    tool_ctx = ToolContext(
        ChatRef("tg", "group"), group_user, False, platform, stores, turn=TurnState()
    )
    denied = await ToolRegistry([tool]).call(
        "voice_settings", {"setting": "reply", "value": "off"}, tool_ctx
    )
    assert denied.is_error and "Only a group admin" in str(denied.content)

    admin = User(UserRef("tg", "native-admin"), "native-admin")
    admin_ctx = CommandContext(ChatRef("tg", "group"), admin, False, False, platform, stores)
    assert await command.handler(admin_ctx, "reply off") == "Voice reply set to off."
    admin_tool_ctx = ToolContext(
        ChatRef("tg", "group"), admin, False, platform, stores, turn=TurnState()
    )
    allowed_tool = await tool.handler(admin_tool_ctx, setting="reply", value="on")
    assert not allowed_tool.is_error

    dm_user = User(UserRef("tg", "dm-person"), "dm-person")
    dm_ctx = CommandContext(ChatRef("tg", "dm-person"), dm_user, True, False, platform, stores)
    assert await command.handler(dm_ctx, "transcript on") == "Voice transcript set to on."
    dm_tool_ctx = ToolContext(
        ChatRef("tg", "dm-person"), dm_user, True, platform, stores, turn=TurnState()
    )
    dm_allowed_tool = await tool.handler(dm_tool_ctx, setting="transcribe", value="off")
    assert not dm_allowed_tool.is_error

    other_chat_ctx = CommandContext(ChatRef("tg", "other"), dm_user, False, False, platform, stores)
    assert "Only a group admin" in await command.handler(other_chat_ctx, "reply off")
    cross_chat_tool_ctx = ToolContext(
        ChatRef("tg", "other"), dm_user, False, platform, stores, turn=TurnState()
    )
    cross_chat = await tool.handler(cross_chat_tool_ctx, setting="reply", value="off")
    assert cross_chat.is_error
    assert stores.db.chats == [
        "tg:group",
        "tg:group",
        "tg:dm-person",
        "tg:dm-person",
    ]


@pytest.mark.asyncio
async def test_wakeword_command_preserves_quoted_alternates_and_rejects_unbalanced_quotes():
    plugin, platform = make(access=AccessPolicy(["tg:group"], []))

    class SettingsDB:
        def __init__(self):
            self.args = None

        async def fetchrow(self, sql, chat_key):
            return None

        async def execute(self, sql, *args):
            self.args = args
            return "INSERT 0 1"

    stores = memory_stores()
    stores.db = SettingsDB()
    ctx = command_ctx(platform)
    ctx.stores = stores
    command = plugin.commands()[0]
    result = await command.handler(ctx, 'wakeword "hey dot" "claw dot"')
    assert result == "Wake-word setting updated."
    assert stores.db.args[2] == ["hey dot", "claw dot"]
    assert "Unbalanced quotes" in await command.handler(ctx, 'wakeword "hey dot')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_dm", "setting", "expected"),
    [
        (True, None, ""),
        (True, True, "🎙 heard"),
        (True, False, ""),
        (False, None, "🎙 heard"),
        (False, True, "🎙 heard"),
        (False, False, ""),
    ],
)
async def test_transcript_display_setting_defaults_by_chat_kind(is_dm, setting, expected):
    plugin, _ = make(access=AccessPolicy([], []))

    class SettingsDB:
        async def fetchrow(self, sql, chat_key):
            return {"show_transcript": setting}

    stores = memory_stores()
    stores.db = SettingsDB()
    result = await plugin.transcript_prefix(
        stores, ChatRef("tg", "chat"), is_dm=is_dm, transcript="recognized words"
    )
    assert ("🎙 heard" in result) == bool(expected)


@pytest.mark.asyncio
async def test_voice_panel_lists_every_setting_and_text_commands():
    plugin, platform = make(access=AccessPolicy([], []))

    class SettingsDB:
        async def fetchrow(self, sql, chat_key):
            return {
                "transcribe": False,
                "wake_word_enabled": True,
                "wake_words": ["hey dot", "claw dot"],
                "voice_reply": True,
                "show_transcript": None,
            }

    ctx = command_ctx(platform)
    ctx.stores.db = SettingsDB()
    panel = await plugin.commands()[0].handler(ctx, "")
    for value in (
        "transcription: off",
        "wake word: on",
        "hey dot",
        "claw dot",
        "transcript display",
        "transcript on|off",
    ):
        assert value in panel


@pytest.mark.asyncio
async def test_chat_without_voice_settings_row_uses_defaults():
    plugin, _ = make(access=AccessPolicy([], []))

    class SettingsDB:
        async def fetchrow(self, sql, chat_key):
            return None

    defaults = await plugin._chat_settings(SimpleNamespace(db=SettingsDB()), ChatRef("tg", "chat"))
    assert defaults == {
        "transcribe": True,
        "wake_word_enabled": False,
        "wake_words": [],
        "voice_reply": True,
        "show_transcript": None,
    }


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
    assert command.name == "voice" and not command.admin_only

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


@pytest.mark.asyncio
async def test_restricted_voice_message_skips_io_and_sends_notice():
    platform = FakePlatform(name="tg", files={"voice": b"audio"})
    downloads = []
    original_download = platform.download

    async def download(*args, **kwargs):
        downloads.append(args[0])
        return await original_download(*args, **kwargs)

    platform.download = download
    provider_calls = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                provider_calls.append(request),
                httpx.Response(200, json={"text": "private"}),
            )[1]
        )
    )
    platform.members["tg:group"] = {"tg:alice"}
    access = AccessPolicy(["tg:group"], [], owners=["tg:root"])
    plugin = VoicePlugin(
        access,
        platform,
        settings=VoiceSettings(
            VOICE_STT_ENABLED=True,
            VOICE_STT_ELEVENLABS_API_KEY=SecretStr("fake"),
            VOICE_STT_USAGE_CHECK="off",
        ),
    )
    plugin.transcription.client = client
    app = BotApp(platform, stores=memory_stores(), access=access, plugins=[plugin], bot_name="bot")
    alice = User(UserRef("tg", "alice"), "alice")
    app.access._snapshot = AccessSnapshot(
        1,
        {},
        frozenset(),
        restrictions={
            (alice.ref.key, "tg:*"): Restriction(
                alice.ref.key, "tg:*", "block", None, "tg:root", datetime.now(UTC)
            )
        },
    )
    voice = IncomingMessage(
        ChatRef("tg", "alice"),
        "voice-1",
        alice,
        "",
        True,
        attachments=(Attachment("voice", AttachmentKind.VOICE, duration_s=1),),
    )
    await app.handle(voice)
    assert downloads == [] and provider_calls == []
    assert platform.texts == ["You can't use this bot here right now."]
    await client.aclose()


@pytest.mark.asyncio
async def test_long_dm_refusal_stops_routing_and_logs_the_empty_message():
    platform = FakePlatform(name="tg")
    access = AccessPolicy([], [], owners=["tg:alice"])
    plugin = VoicePlugin(
        access,
        platform,
        settings=VoiceSettings(
            VOICE_STT_ENABLED=True,
            VOICE_STT_ELEVENLABS_API_KEY=SecretStr("fake"),
            VOICE_STT_USAGE_CHECK="off",
        ),
    )

    class Responder:
        def __init__(self):
            self.calls = []

        async def respond(self, message, text, app):
            self.calls.append((message, text))

    responder = Responder()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[plugin],
        responder=responder,
        bot_name="bot",
    )
    note = IncomingMessage(
        ChatRef("tg", "alice"),
        "too-long",
        User(UserRef("tg", "alice"), "alice"),
        "",
        True,
        attachments=(Attachment("voice", AttachmentKind.VOICE, duration_s=121),),
    )
    await app.handle(note)
    logged = await app.stores.repo.get_message(note.chat, note.message_id)
    assert len(platform.sent) == 1 and "2:01" in platform.sent[0].text
    assert responder.calls == []
    assert logged is not None and logged.text == ""


@pytest.mark.asyncio
async def test_provider_error_notice_stops_routing():
    platform = FakePlatform(name="tg")
    access = AccessPolicy([], [], owners=["tg:alice"])
    plugin = VoicePlugin(
        access,
        platform,
        settings=VoiceSettings(
            VOICE_STT_ENABLED=True,
            VOICE_STT_ELEVENLABS_API_KEY=SecretStr("fake"),
            VOICE_STT_USAGE_CHECK="off",
        ),
    )
    stt_calls = []

    async def provider_error(message, app, attachment, **kwargs):
        stt_calls.append(attachment)
        return message, "provider"

    plugin.transcription.transcribe = provider_error

    class Responder:
        def __init__(self):
            self.calls = []

        async def respond(self, message, text, app):
            self.calls.append((message, text))

    responder = Responder()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[plugin],
        responder=responder,
        bot_name="bot",
    )
    note = IncomingMessage(
        ChatRef("tg", "alice"),
        "provider-error",
        User(UserRef("tg", "alice"), "alice"),
        "",
        True,
        attachments=(Attachment("voice", AttachmentKind.VOICE, duration_s=3),),
    )
    await app.handle(note)
    logged = await app.stores.repo.get_message(note.chat, note.message_id)
    assert len(platform.sent) == 1 and "aren't working right now" in platform.sent[0].text
    assert len(stt_calls) == 1 and responder.calls == []
    assert logged is not None and logged.text == ""


@pytest.mark.asyncio
async def test_unconfigured_stt_does_not_stop_dm_routing():
    platform = FakePlatform(name="tg")
    access = AccessPolicy([], [], owners=["tg:alice"])
    plugin = VoicePlugin(access, platform, settings=VoiceSettings(VOICE_STT_ENABLED=False))

    class Responder:
        def __init__(self):
            self.calls = []

        async def respond(self, message, text, app):
            self.calls.append((message, text))

    responder = Responder()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[plugin],
        responder=responder,
        bot_name="bot",
    )
    note = IncomingMessage(
        ChatRef("tg", "alice"),
        "unconfigured",
        User(UserRef("tg", "alice"), "alice"),
        "",
        True,
        attachments=(Attachment("voice", AttachmentKind.VOICE, duration_s=3),),
    )
    await app.handle(note)
    assert len(responder.calls) == 1


@pytest.mark.asyncio
async def test_wake_miss_stores_empty_text_and_never_logs_transcript(caplog):
    platform = FakePlatform(name="tg", files={"voice": b"audio"})
    access = AccessPolicy(["tg:group"], [])
    plugin = VoicePlugin(
        access,
        platform,
        settings=VoiceSettings(
            VOICE_STT_ENABLED=True,
            VOICE_STT_ELEVENLABS_API_KEY=SecretStr("fake"),
            VOICE_STT_USAGE_CHECK="off",
        ),
    )
    provider_calls = []

    def transcribe(request):
        provider_calls.append(request)
        return httpx.Response(200, json={"text": "SECRET_TRANSCRIPT_SENTINEL"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(transcribe))
    plugin.transcription.client = client

    class Observer(BasePlugin):
        name = "wake_miss_observer"

        def __init__(self):
            self.messages = []

        async def on_message(self, message, stores):
            self.messages.append(message)

    observer = Observer()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[plugin, observer],
        bot_name="bot",
    )

    class SettingsDB:
        async def fetchrow(self, sql, chat_key):
            return {
                "transcribe": True,
                "wake_word_enabled": True,
                "wake_words": ["hey dot"],
                "voice_reply": True,
                "show_transcript": None,
            }

    app.stores.db = SettingsDB()
    voice = IncomingMessage(
        ChatRef("tg", "group"),
        "voice-miss",
        User(UserRef("tg", "alice"), "alice"),
        "",
        False,
        attachments=(Attachment("voice", AttachmentKind.VOICE, duration_s=1),),
    )
    with caplog.at_level("DEBUG", logger="chatcore_voice"):
        await app.handle(voice)
    stored = await app.stores.repo.get_message(voice.chat, voice.message_id)
    assert len(provider_calls) == 1
    assert stored.text == ""
    assert "SECRET_TRANSCRIPT_SENTINEL" not in caplog.text
    assert len(observer.messages) == 1
    await client.aclose()


class ReactingPlatform(FakePlatform):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.reactions = []

    async def react(self, message, emoji):
        self.reactions.append((message.message_id, emoji))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "transcript"),
    [(200, "just chatting about lunch"), (500, None), (200, "hey dot what's the time")],
)
async def test_wake_mode_unaddressed_notes_stay_silent_until_the_wake_word(status, transcript):
    platform = ReactingPlatform(name="tg", files={"voice": b"audio"})
    access = AccessPolicy(["tg:group"], [])
    plugin = VoicePlugin(
        access,
        platform,
        settings=VoiceSettings(
            VOICE_STT_ENABLED=True,
            VOICE_STT_ELEVENLABS_API_KEY=SecretStr("fake"),
            VOICE_STT_USAGE_CHECK="off",
        ),
    )

    def transcribe(request):
        if status != 200:
            return httpx.Response(status, json={"detail": "boom"})
        return httpx.Response(200, json={"text": transcript})

    client = httpx.AsyncClient(transport=httpx.MockTransport(transcribe))
    plugin.transcription.client = client
    app = BotApp(platform, stores=memory_stores(), access=access, plugins=[plugin], bot_name="bot")

    class SettingsDB:
        async def fetchrow(self, sql, chat_key):
            return {
                "transcribe": True,
                "wake_word_enabled": True,
                "wake_words": ["hey dot"],
                "voice_reply": True,
                "show_transcript": None,
            }

    app.stores.db = SettingsDB()
    note = IncomingMessage(
        ChatRef("tg", "group"),
        "voice-wake",
        User(UserRef("tg", "alice"), "alice"),
        "",
        False,
        attachments=(Attachment("voice", AttachmentKind.VOICE, duration_s=3),),
    )
    result = await plugin.transform_incoming(note, app)

    if transcript and transcript.startswith("hey dot"):
        # The wake word matched: the note is addressed, and only now does the bot react.
        assert result is not None and result.mentions_bot
        assert platform.reactions == [("voice-wake", "👂")]
    else:
        # A miss or provider error on an unaddressed note: no reaction, no refusal, routing goes on.
        assert result is note
        assert platform.reactions == []
        assert platform.texts == []
    await client.aclose()


@pytest.mark.asyncio
async def test_forwarded_dm_skips_but_mention_reply_transcribes_forwarded_note():
    plugin, platform = make(
        access=AccessPolicy([], []),
        VOICE_STT_ENABLED=True,
        VOICE_STT_ELEVENLABS_API_KEY=SecretStr("fake"),
    )
    calls = []

    class StubTranscription:
        available = True

        async def transcribe(self, message, app, attachment, **kwargs):
            calls.append((message, attachment, kwargs))
            return replace(message, text="request\n\n[voice transcript: heard]"), None

    plugin.transcription = StubTranscription()
    forwarded_note = IncomingMessage(
        ChatRef("tg", "alice"),
        "forwarded-note",
        User(UserRef("tg", "alice"), "alice"),
        "",
        True,
        attachments=(Attachment("voice", AttachmentKind.VOICE, duration_s=1),),
        forwarded=True,
    )

    class Repo:
        async def get_message(self, chat, message_id):
            return replace(forwarded_note, forwarded=False)

    app = SimpleNamespace(stores=SimpleNamespace(repo=Repo(), db=None))
    result = await plugin.transform_incoming(forwarded_note, app)
    assert result is forwarded_note and calls == []
    reply = replace(
        forwarded_note,
        message_id="reply",
        text="What did they say?",
        attachments=(),
        reply_to=MessageRef(forwarded_note.chat, forwarded_note.message_id),
        mentions_bot=True,
        forwarded=False,
    )
    transformed = await plugin.transform_incoming(reply, app)
    assert len(calls) == 1 and calls[0][2]["source"] == "replied"
    assert transformed.text == "request\n\n[voice transcript: heard]"
