from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.events import ChatRef, User, UserRef
from chatcore.testing import FakePlatform, memory_stores
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_tutor.plugin import LANGUAGE_TUTOR_INSTRUCTIONS, TutorPlugin
from chatcore_tutor.settings import TutorSettings


def test_disabled_settings_preserve_instructions_tools_and_no_tts_tool():
    plugin = TutorPlugin(AccessPolicy([], []), FakePlatform())
    app = BotApp(
        FakePlatform(),
        stores=memory_stores(),
        access=AccessPolicy([], []),
        plugins=[plugin],
        bot_name="testbot",
    )
    assert app.skills.get("language_tutor") is not None
    assert plugin.settings.TUTOR_TTS_ENABLED is False
    assert plugin.skills()[0].instructions == LANGUAGE_TUTOR_INSTRUCTIONS
    assert "pronounce_tutor_text" not in {tool.name for tool in plugin.tools()}
    assert plugin.tool_gates() == {}
    assert len(plugin.skills()[0].instructions) <= 800


async def test_plugin_close_closes_its_owned_speech_client():
    plugin = TutorPlugin(AccessPolicy([], []), FakePlatform())
    client = plugin.speech._client
    await plugin.aclose()
    assert client.is_closed


async def test_pronounce_tool_counts_failure_and_never_attempts_twice_same_turn():
    from pydantic import SecretStr

    settings = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("fake"),
        TUTOR_TTS_CANTONESE_VOICE_ID="yue",
        TUTOR_TTS_MANDARIN_VOICE_ID="zh",
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
    )
    platform = FakePlatform(name="tg")
    access = AccessPolicy(["tg:group"], [])
    plugin = TutorPlugin(access, platform, settings=settings)

    calls = 0

    async def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        from chatcore_tutor.speech import SpeechError

        raise SpeechError("provider")

    plugin.speech.synthesize = fail
    tool = next(tool for tool in plugin.tools() if tool.name == "pronounce_tutor_text")
    registry = ToolRegistry([tool])
    turn = TurnState()
    actor = User(UserRef("tg", "alice"), display_name="Alice", username="alice")
    ctx = ToolContext(ChatRef("tg", "group"), actor, False, platform, memory_stores(), turn=turn)
    result = await registry.call(
        tool.name,
        {"text": "こんにちは", "language": "Japanese", "pronunciation": "こんにちは"},
        ctx,
    )
    assert result.is_error
    assert turn.tool_attempts[tool.name] == 1
    assert calls == 1
    result = await registry.call(
        tool.name,
        {"text": "こんにちは", "language": "Japanese", "pronunciation": "こんにちは"},
        ctx,
    )
    assert result.is_error
    assert turn.tool_attempts[tool.name] == 1
    fresh_turn = ToolContext(
        ctx.chat, ctx.user, ctx.is_dm, ctx.platform, ctx.stores, turn=TurnState()
    )
    result = await registry.call(
        tool.name,
        {"text": "こんにちは", "language": "Japanese", "pronunciation": "こんにちは"},
        fresh_turn,
    )
    assert result.is_error and fresh_turn.turn.tool_attempts[tool.name] == 1
    assert calls == 2
    await plugin.aclose()


async def test_success_sends_exact_topic_caption_and_audio_before_text(monkeypatch):
    from pydantic import SecretStr

    settings = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("fake"),
        TUTOR_TTS_CANTONESE_VOICE_ID="yue",
        TUTOR_TTS_MANDARIN_VOICE_ID="zh",
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
    )
    platform = FakePlatform(name="tg")
    plugin = TutorPlugin(AccessPolicy(["tg:group"], []), platform, settings=settings)
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"

    async def synthesize(*args, **kwargs):
        return audio

    plugin.speech.synthesize = synthesize
    tool = next(tool for tool in plugin.tools() if tool.name == "pronounce_tutor_text")
    ctx = ToolContext(
        ChatRef("tg", "group"),
        User(UserRef("tg", "alice"), display_name="Alice"),
        False,
        platform,
        memory_stores(),
        thread_id="topic-9",
        turn=TurnState(),
    )
    result = await ToolRegistry([tool]).call(
        tool.name,
        {"text": "こんにちは", "language": "Japanese", "pronunciation": "こんにちは"},
        ctx,
    )
    assert not result.is_error
    assert len(platform.sent_voices) == 1
    voice = platform.sent_voices[0]
    assert (voice.chat, voice.thread_id, voice.filename, voice.data, voice.caption) == (
        ctx.chat,
        "topic-9",
        "pronunciation.mp3",
        audio,
        "こんにちは\nこんにちは\nNormal",
    )
    assert len(plugin.skills()[0].instructions) <= 800
    assert "pronounce_tutor_text" in plugin.skills()[0].tools
    await plugin.aclose()


async def test_enabled_botapp_builds_real_gate_and_calls_mock_provider_to_voice():
    import httpx
    from chatcore.skills import GateState
    from chatcore_tutor.speech import SpeechService
    from pydantic import SecretStr

    settings = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("mock-key"),
        TUTOR_TTS_CANTONESE_VOICE_ID="yue",
        TUTOR_TTS_MANDARIN_VOICE_ID="zh",
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
    )
    platform = FakePlatform()
    access = AccessPolicy(["fake:group"], [])
    plugin = TutorPlugin(access, platform, settings=settings)
    await plugin.aclose()
    requests = []
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"

    async def provider(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=audio)

    plugin.speech = SpeechService(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    app = BotApp(
        platform, stores=memory_stores(), access=access, plugins=[plugin], bot_name="testbot"
    )
    skill = app.skills.get("language_tutor")
    assert skill and len(skill.examples) <= 12 and len(skill.instructions) <= 800
    ctx = ToolContext(
        ChatRef("fake", "group"),
        __import__("chatcore.testing", fromlist=["user"]).user(),
        False,
        platform,
        app.stores,
        thread_id="topic-3",
        turn=TurnState(),
    )
    gate = await app.skills.evaluate_tool("pronounce_tutor_text", ctx)
    assert gate.state is GateState.AVAILABLE
    denied_ctx = ToolContext(
        ChatRef("other", "group"),
        User(UserRef("other", "alice"), display_name="Alice"),
        False,
        platform,
        app.stores,
        turn=TurnState(),
    )
    denied_gate = await app.skills.evaluate_tool("pronounce_tutor_text", denied_ctx)
    assert denied_gate.state is GateState.NOT_ALLOWED
    result = await app.tools.call(
        "pronounce_tutor_text",
        {"text": "こんにちは", "language": "Japanese", "pronunciation": "こんにちは"},
        ctx,
    )
    assert not result.is_error and len(requests) == 1
    assert len(platform.sent_voices) == 1
    assert platform.sent_voices[0].thread_id == "topic-3"
    assert requests[0].url.params.get_list("output_format") == ["mp3_44100_128"]
    await plugin.aclose()


def test_partial_voice_configuration_and_zero_budget_control_advertising():
    from pydantic import SecretStr

    platform = FakePlatform()
    partial = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("mock"),
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
    )
    plugin = TutorPlugin(AccessPolicy([], []), platform, settings=partial)
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=AccessPolicy([], []),
        plugins=[plugin],
        bot_name="testbot",
    )
    skill = app.skills.get("language_tutor")
    assert skill and "pronounce_tutor_text" in skill.tools
    assert len(skill.examples) <= 12 and len(skill.instructions) <= 800

    disabled = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("mock"),
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
        TUTOR_TTS_MAX_CHARS_PER_DAY=0,
    )
    plugin = TutorPlugin(AccessPolicy([], []), platform, settings=disabled)
    assert "pronounce_tutor_text" not in {tool.name for tool in plugin.tools()}
    assert plugin.skills()[0].instructions == LANGUAGE_TUTOR_INSTRUCTIONS


async def test_partial_language_voice_rejects_unconfigured_selection_without_http():
    import httpx
    from chatcore_tutor.speech import SpeechService
    from pydantic import SecretStr

    settings = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("mock"),
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
    )
    calls = 0

    async def provider(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"bad")

    platform = FakePlatform()
    plugin = TutorPlugin(AccessPolicy(["fake:group"], []), platform, settings=settings)
    await plugin.speech.aclose()
    plugin.speech = SpeechService(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    tool = next(item for item in plugin.tools() if item.name == "pronounce_tutor_text")
    ctx = ToolContext(
        ChatRef("fake", "group"),
        __import__("chatcore.testing", fromlist=["user"]).user(),
        False,
        platform,
        memory_stores(),
        turn=TurnState(),
    )
    result = await ToolRegistry([tool]).call(
        tool.name,
        {"text": "你好", "language": "Cantonese", "pronunciation": "nei5 hou2"},
        ctx,
    )
    assert result.is_error and calls == 0 and ctx.turn.tool_attempts[tool.name] == 1
    await plugin.aclose()


async def test_slow_missing_ffmpeg_fails_before_provider_and_counts_attempt():
    import httpx
    from chatcore_tutor.speech import SpeechService
    from pydantic import SecretStr

    settings = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("fake"),
        TUTOR_TTS_CANTONESE_VOICE_ID="yue",
        TUTOR_TTS_MANDARIN_VOICE_ID="zh",
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
    )
    platform = FakePlatform(name="tg")
    plugin = TutorPlugin(AccessPolicy(["tg:group"], []), platform, settings=settings)
    calls = 0

    async def provider(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"audio")

    await plugin.speech.aclose()
    plugin.speech = SpeechService(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
        ffmpeg_checker=lambda: False,
    )
    tool = next(tool for tool in plugin.tools() if tool.name == "pronounce_tutor_text")
    ctx = ToolContext(
        ChatRef("tg", "group"),
        User(UserRef("tg", "alice"), display_name="Alice"),
        False,
        platform,
        memory_stores(),
        turn=TurnState(),
    )
    result = await ToolRegistry([tool]).call(
        tool.name,
        {
            "text": "こんにちは",
            "language": "Japanese",
            "pronunciation": "こんにちは",
            "pace": "slow",
        },
        ctx,
    )
    assert result.is_error and calls == 0
    assert ctx.turn.tool_attempts[tool.name] == 1
    await plugin.aclose()


async def test_denied_and_mismatched_requests_make_no_provider_call():
    from pydantic import SecretStr

    settings = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("fake"),
        TUTOR_TTS_CANTONESE_VOICE_ID="yue",
        TUTOR_TTS_MANDARIN_VOICE_ID="zh",
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
    )
    platform = FakePlatform(name="tg")
    plugin = TutorPlugin(AccessPolicy([], []), platform, settings=settings)
    calls = 0

    async def synthesize(*args, **kwargs):
        nonlocal calls
        calls += 1
        return b"audio"

    plugin.speech.synthesize = synthesize
    tool = next(tool for tool in plugin.tools() if tool.name == "pronounce_tutor_text")
    registry = ToolRegistry([tool])
    actor = User(UserRef("tg", "alice"), display_name="Alice")
    invalid_ctx = ToolContext(
        ChatRef("tg", "group"), actor, False, platform, memory_stores(), turn=TurnState()
    )
    invalid = await registry.call(
        tool.name,
        {"text": "こんにちは", "language": ["Japanese"], "pronunciation": "こんにちは"},
        invalid_ctx,
    )
    assert invalid.is_error and not invalid_ctx.turn.tool_attempts

    denied = await registry.call(
        tool.name,
        {"text": "こんにちは", "language": "Japanese", "pronunciation": "こんにちは"},
        invalid_ctx,
    )
    assert denied.is_error and calls == 0 and not invalid_ctx.turn.tool_attempts
    await plugin.aclose()


async def test_revocation_after_generation_prevents_voice_delivery():
    from pydantic import SecretStr

    settings = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("fake"),
        TUTOR_TTS_CANTONESE_VOICE_ID="yue",
        TUTOR_TTS_MANDARIN_VOICE_ID="zh",
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
    )
    access = AccessPolicy(["tg:group"], [])
    platform = FakePlatform(name="tg")
    plugin = TutorPlugin(access, platform, settings=settings)

    async def revoke(*args, **kwargs):
        access.bootstrap_chats = frozenset()
        return b"audio"

    plugin.speech.synthesize = revoke
    tool = next(tool for tool in plugin.tools() if tool.name == "pronounce_tutor_text")
    ctx = ToolContext(
        ChatRef("tg", "group"),
        User(UserRef("tg", "alice"), display_name="Alice"),
        False,
        platform,
        memory_stores(),
        turn=TurnState(),
    )
    result = await ToolRegistry([tool]).call(
        tool.name,
        {"text": "こんにちは", "language": "Japanese", "pronunciation": "こんにちは"},
        ctx,
    )
    assert result.is_error and not platform.sent_voices
    assert ctx.turn.tool_attempts[tool.name] == 1
    await plugin.aclose()


async def test_context_voice_platform_receives_clip_and_mismatched_namespaces_are_denied():
    import httpx
    from chatcore_tutor.speech import SpeechService
    from pydantic import SecretStr

    settings = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("mock"),
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
    )
    plugin_platform, context_platform = FakePlatform(), FakePlatform()
    access = AccessPolicy(["fake:group"], [])
    plugin = TutorPlugin(access, plugin_platform, settings=settings)
    await plugin.speech.aclose()
    calls = 0
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"

    async def provider(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=audio)

    plugin.speech = SpeechService(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    tool = next(item for item in plugin.tools() if item.name == "pronounce_tutor_text")
    actor = __import__("chatcore.testing", fromlist=["user"]).user()
    args = {"text": "こんにちは", "language": "Japanese", "pronunciation": "こんにちは"}
    ctx = ToolContext(
        ChatRef("fake", "group"), actor, False, context_platform, memory_stores(), turn=TurnState()
    )
    result = await ToolRegistry([tool]).call(tool.name, args, ctx)
    assert not result.is_error and calls == 1
    assert not plugin_platform.sent_voices and len(context_platform.sent_voices) == 1

    foreign_ctx = ToolContext(
        ChatRef("other", "group"),
        User(UserRef("other", "alice"), display_name="Alice"),
        False,
        context_platform,
        memory_stores(),
        turn=TurnState(),
    )
    denied = await ToolRegistry([tool]).call(tool.name, args, foreign_ctx)
    assert denied.is_error and calls == 1 and not foreign_ctx.turn.tool_attempts

    from chatcore_telegram.bridge import BridgeTelegramPlatform

    class BridgeStub:
        async def request(self, operation, payload):
            return {}

        async def poll_events(self, wait_ms=0):
            return []

        async def ack_events(self, relay_ids):
            return None

    bridge = BridgeTelegramPlatform(BridgeStub())
    bridge_ctx = ToolContext(
        ChatRef("tg", "group"),
        User(UserRef("tg", "alice"), display_name="Alice"),
        False,
        bridge,
        memory_stores(),
        turn=TurnState(),
    )
    bridge_result = await ToolRegistry([tool]).call(tool.name, args, bridge_ctx)
    assert bridge_result.is_error and calls == 1 and not bridge_ctx.turn.tool_attempts
    await plugin.aclose()


async def test_dm_restriction_blocks_before_provider_call():
    from datetime import UTC, datetime

    import httpx
    from chatcore.administration.store import AccessSnapshot, Restriction
    from chatcore_tutor.speech import SpeechService
    from pydantic import SecretStr

    settings = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("mock"),
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
    )
    platform = FakePlatform(members={"fake:group": {"fake:alice"}})
    access = AccessPolicy(["fake:group"], [])
    actor = User(UserRef("fake", "alice"), display_name="Alice")
    access._snapshot = AccessSnapshot(
        1,
        {},
        frozenset(),
        restrictions={
            (actor.ref.key, "fake:*"): Restriction(
                actor.ref.key, "fake:*", "block", None, "fake:admin", datetime.now(UTC)
            )
        },
    )
    plugin = TutorPlugin(access, platform, settings=settings)
    await plugin.speech.aclose()
    calls = 0

    async def provider(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"bad")

    plugin.speech = SpeechService(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    tool = next(item for item in plugin.tools() if item.name == "pronounce_tutor_text")
    ctx = ToolContext(
        ChatRef("fake", "alice"), actor, True, platform, memory_stores(), turn=TurnState()
    )
    result = await ToolRegistry([tool]).call(
        tool.name,
        {"text": "こんにちは", "language": "Japanese", "pronunciation": "こんにちは"},
        ctx,
    )
    assert result.is_error and calls == 0 and not ctx.turn.tool_attempts
    await plugin.aclose()


async def test_concurrent_same_turn_calls_make_one_provider_attempt():
    import asyncio

    import httpx
    from chatcore_tutor.speech import SpeechService
    from pydantic import SecretStr

    settings = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("mock"),
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
    )
    access = AccessPolicy(["fake:group"], [])
    platform = FakePlatform()
    plugin = TutorPlugin(access, platform, settings=settings)
    await plugin.speech.aclose()
    gate = asyncio.Event()
    both_refreshing = asyncio.Event()
    refreshes = 0
    requests = 0
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"

    async def refresh():
        nonlocal refreshes
        refreshes += 1
        if refreshes == 2:
            both_refreshing.set()
        await gate.wait()

    access.refresh = refresh

    async def provider(request):
        nonlocal requests
        requests += 1
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=audio)

    plugin.speech = SpeechService(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    tool = next(item for item in plugin.tools() if item.name == "pronounce_tutor_text")
    ctx = ToolContext(
        ChatRef("fake", "group"),
        __import__("chatcore.testing", fromlist=["user"]).user(),
        False,
        platform,
        memory_stores(),
        turn=TurnState(),
    )
    registry = ToolRegistry([tool])
    arguments = {"text": "こんにちは", "language": "Japanese", "pronunciation": "こんにちは"}
    first = asyncio.create_task(registry.call(tool.name, arguments, ctx))
    second = asyncio.create_task(registry.call(tool.name, arguments, ctx))
    await both_refreshing.wait()
    gate.set()
    results = await asyncio.gather(first, second)
    assert sum(not result.is_error for result in results) == 1
    assert requests == 1 and len(platform.sent_voices) == 1
    await plugin.aclose()


def test_language_format_is_canonical_without_script_or_tone_heuristics():
    from chatcore_tutor.plugin import _valid_pronunciation_request

    assert _valid_pronunciation_request("吗", "Mandarin Chinese", "ma", "normal")
    assert _valid_pronunciation_request("𠮷", "Japanese", "jí", "normal")
    assert _valid_pronunciation_request("brand 42", "Cantonese", "brand forty two", "normal")
    assert _valid_pronunciation_request("𠮷" * 120, "Japanese", "ma" * 120, "normal")
    assert not _valid_pronunciation_request("𠮷" * 121, "Japanese", "ma", "normal")
    assert _valid_pronunciation_request("日", "Japanese", "ma" * 120, "normal")
    assert not _valid_pronunciation_request("日", "Japanese", "ma" * 121, "normal")
    assert not _valid_pronunciation_request("𠮷\ud800", "Japanese", "き", "normal")
    assert not _valid_pronunciation_request("こんにちは", "japanese", "こんにちは", "normal")


async def test_enabled_router_handles_natural_audio_requests_and_sticky_slow_followup():
    from chatcore.agent import AgentResponder, AgentSettings
    from chatcore.conversation import ConversationPlugin, ConversationStore
    from chatcore.limits import Limits
    from chatcore.llm.types import Completion, Message, ToolCall
    from chatcore.skill_router import SkillRouter
    from chatcore.skill_turn import SkillSettings
    from chatcore.skills import SkillsMode
    from chatcore.testing import GROUP, ScriptedProvider, message
    from pydantic import SecretStr

    settings = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("mock"),
        TUTOR_TTS_CANTONESE_VOICE_ID="yue",
        TUTOR_TTS_MANDARIN_VOICE_ID="zh",
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
    )
    platform = FakePlatform()
    access = AccessPolicy([GROUP.key], [])
    plugin = TutorPlugin(access, platform, settings=settings)
    calls = []

    async def synthesize(text, language, *, user_key, pace):
        calls.append((text, language, pace))
        return b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"

    plugin.speech.synthesize = synthesize
    tool_name = "pronounce_tutor_text"
    provider = ScriptedProvider(
        [
            Completion(
                Message.assistant(
                    "",
                    (
                        ToolCall(
                            "normal",
                            tool_name,
                            {
                                "text": "こんにちは",
                                "language": "Japanese",
                                "pronunciation": "こんにちは",
                                "pace": "normal",
                            },
                        ),
                    ),
                ),
                "tool_use",
            ),
            Completion(Message.assistant("Done."), "end_turn"),
            Completion(
                Message.assistant(
                    "",
                    (
                        ToolCall(
                            "slow",
                            tool_name,
                            {
                                "text": "こんにちは",
                                "language": "Japanese",
                                "pronunciation": "こんにちは",
                                "pace": "slow",
                            },
                        ),
                    ),
                ),
                "tool_use",
            ),
            Completion(Message.assistant("Done slowly."), "end_turn"),
        ]
    )
    stores = memory_stores()
    conversation = ConversationStore()
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[ConversationPlugin(conversation), plugin],
        bot_name="testbot",
    )
    skill_settings = SkillSettings(
        mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword", sticky_turns=2
    )
    responder = AgentResponder(
        provider,
        system_prompt="legacy",
        settings=AgentSettings(skills=skill_settings),
        limits=Limits(stores.cache),
    )
    router = SkillRouter(
        app.skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05
    )
    for phrase in (
        "Please pronounce this in Japanese",
        "Please say this aloud in Japanese",
        "how to sound",
        "voice pls",
        "sound please",
        "i want sound clip",
        "please send an audio clip",
        "讀畀我聽",
        "请读给我听",
        "この文を音声で聞きたい",
    ):
        result = await router.route(phrase)
        assert "language_tutor" in [hit.skill for hit in result.hits], phrase
    first = message("sound please")
    await stores.repo.log_message(first)
    await responder.respond(first, first.text, app)
    followup = message("slower please", sender=first.user)
    await stores.repo.log_message(followup)
    await responder.respond(followup, followup.text, app)
    assert calls == [("こんにちは", "Japanese", "normal"), ("こんにちは", "Japanese", "slow")]
    assert all(any(tool.name == tool_name for tool in call["tools"]) for call in provider.calls)
    await plugin.aclose()


async def test_lone_surrogate_annotation_is_safe_and_never_attempted_or_paid():
    import httpx
    from chatcore_tutor.speech import SpeechService
    from pydantic import SecretStr

    settings = TutorSettings(
        TUTOR_TTS_ENABLED=True,
        ELEVENLABS_API_KEY=SecretStr("mock"),
        TUTOR_TTS_JAPANESE_VOICE_ID="ja",
    )
    calls = 0

    async def provider(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"bad")

    platform = FakePlatform()
    plugin = TutorPlugin(AccessPolicy(["fake:group"], []), platform, settings=settings)
    await plugin.speech.aclose()
    plugin.speech = SpeechService(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    tool = next(item for item in plugin.tools() if item.name == "pronounce_tutor_text")
    ctx = ToolContext(
        ChatRef("fake", "group"),
        __import__("chatcore.testing", fromlist=["user"]).user(),
        False,
        platform,
        memory_stores(),
        turn=TurnState(),
    )
    result = await ToolRegistry([tool]).call(
        tool.name,
        {"text": "こんにちは", "language": "Japanese", "pronunciation": "\ud800"},
        ctx,
    )
    assert result.is_error and calls == 0 and not ctx.turn.tool_attempts
    await plugin.aclose()
