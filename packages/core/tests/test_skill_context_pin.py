from datetime import UTC, datetime

import pytest
from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin, ConversationStore
from chatcore.limits import Limits
from chatcore.plugin import BasePlugin
from chatcore.skill_turn import SkillSettings, SkillTurn
from chatcore.skills import Gate, GateState, Skill, SkillsMode, ToolGate
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.tools.registry import Tool, ToolContext, ToolResult


class HookPlugin(BasePlugin):
    name = "hook"

    def __init__(self, *, pin=None, context=None, gate_state=GateState.AVAILABLE):
        self.pin_hook = pin
        self.context_hook = context
        self.gate_state = gate_state
        self.pin_calls = 0
        self.context_calls = 0

    def tools(self):
        async def handler(ctx):
            return ToolResult("ok")

        return [Tool("hook_tool", "Hook tool", {"type": "object", "properties": {}}, handler)]

    def skills(self):
        async def pin(ctx):
            self.pin_calls += 1
            return await self.pin_hook(ctx) if self.pin_hook else False

        async def context(ctx):
            self.context_calls += 1
            return await self.context_hook(ctx) if self.context_hook else None

        return [
            Skill(
                "hook_skill",
                "Hook skill",
                ("hook one", "hook two", "hook three", "hook four"),
                ("hook_tool",),
                instructions="Base instructions",
                pin=pin if self.pin_hook else None,
                context=context if self.context_hook else None,
            ),
            Skill(
                "other_skill",
                "Other skill",
                ("other one", "other two", "other three", "other four"),
                ("hook_tool",),
            ),
        ]

    def tool_gates(self):
        async def gate(chat, is_dm):
            return Gate(
                self.gate_state,
                "not available" if self.gate_state is not GateState.AVAILABLE else "",
            )

        return {"hook_tool": ToolGate(scope=gate)}


class Routed:
    def __init__(self, names):
        self.names = names

    async def route(self, text):
        from types import SimpleNamespace

        return SimpleNamespace(hits=[SimpleNamespace(skill=name) for name in self.names])


async def _turn(plugin, routed=(), *, max_active=3, settings=None):
    stores = memory_stores()
    incoming = message("unrelated")
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
    )
    ctx = ToolContext(incoming.chat, incoming.user, False, app.platform, stores)
    turn = await SkillTurn.build(
        app=app,
        ctx=ctx,
        message=incoming,
        text="unrelated",
        settings=settings
        or SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}), max_active=max_active),
        router=Routed(routed),
        store=ConversationStore(),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return app, stores, incoming, ctx, turn


@pytest.mark.asyncio
async def test_pin_precedes_router_hit_and_survives_max_active():
    plugin = HookPlugin(pin=lambda ctx: _true())
    app, _, _, _, turn = await _turn(plugin, ["other_skill"], max_active=1)
    assert turn.active == ["hook_skill"]
    assert turn._fresh == {"hook_skill", "other_skill"}
    assert plugin.pin_calls == 1


async def _true():
    return True


@pytest.mark.asyncio
async def test_pin_error_is_false_and_logged_by_class(caplog):
    async def broken(ctx):
        raise RuntimeError("do not log this")

    plugin = HookPlugin(pin=broken)
    _, _, _, _, turn = await _turn(plugin)
    assert turn.active == []
    assert "skill pin failed skill=hook_skill error=RuntimeError" in caplog.text
    assert "do not log this" not in caplog.text


@pytest.mark.asyncio
async def test_pin_is_not_called_in_off_mode():
    plugin = HookPlugin(pin=lambda ctx: _true())
    _, _, _, _, turn = await _turn(
        plugin, settings=SkillSettings(mode=SkillsMode.OFF, max_active=1)
    )
    assert turn.active == []
    assert plugin.pin_calls == 0


def test_skill_rejects_non_callable_context_hook():
    class InvalidPlugin(HookPlugin):
        def skills(self):
            return [
                Skill(
                    "bad_skill",
                    "Bad skill",
                    ("bad one", "bad two", "bad three", "bad four"),
                    ("hook_tool",),
                    context="not callable",
                )
            ]

    with pytest.raises(ValueError, match="context must be callable"):
        BotApp(
            FakePlatform(),
            stores=memory_stores(),
            access=AccessPolicy([GROUP.key], []),
            plugins=[InvalidPlugin()],
            bot_name="testbot",
        )


@pytest.mark.asyncio
async def test_unavailable_pinned_skill_stays_unavailable():
    plugin = HookPlugin(pin=lambda ctx: _true(), gate_state=GateState.OFF)
    _, _, _, _, turn = await _turn(plugin)
    assert turn.active == ["hook_skill"]
    assert "hook_tool" not in turn.offered


@pytest.mark.asyncio
async def test_unconfigured_pinned_skill_is_not_activated():
    plugin = HookPlugin(pin=lambda ctx: _true(), gate_state=GateState.UNCONFIGURED)
    _, _, _, _, turn = await _turn(plugin)
    assert turn.active == []
    assert "hook_tool" not in turn.offered


@pytest.mark.asyncio
async def test_skill_context_is_appended_only_for_active_skill():
    async def context(ctx):
        return "Trusted context"

    plugin = HookPlugin(context=context)
    stores = memory_stores()
    responder = AgentResponder(
        ScriptedProvider([]),
        system_prompt="legacy",
        skills_prompt="skills",
        settings=AgentSettings(
            skills=SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}))
        ),
        limits=Limits(stores.cache),
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        responder=responder,
        bot_name="testbot",
    )
    incoming = message("hi")
    ctx = ToolContext(incoming.chat, incoming.user, False, app.platform, stores)
    turn = SkillTurn(
        mode=SkillsMode.ON,
        active=["hook_skill"],
        loaded=[],
        offered={},
        hints=[],
        variant_hints=[],
        step_cap=8,
    )
    system, *_ = await responder._context(
        incoming,
        "hi",
        app,
        ConversationStore(),
        "tag",
        skill_turn=turn,
        tool_ctx=ctx,
    )
    assert "## hook_skill\nBase instructions\nTrusted context" in system
    assert plugin.context_calls == 1


@pytest.mark.asyncio
async def test_skill_context_failure_and_long_text_are_skipped(caplog):
    async def broken(ctx):
        raise ValueError("private")

    plugin = HookPlugin(context=broken)
    _, stores, incoming, ctx, _ = await _turn(plugin)
    responder = AgentResponder(
        ScriptedProvider([]),
        system_prompt="legacy",
        skills_prompt="skills",
        settings=AgentSettings(),
        limits=Limits(stores.cache),
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        responder=responder,
        bot_name="testbot",
    )
    turn = SkillTurn(SkillsMode.ON, ["hook_skill"], [], {}, [], [], 8)
    system, *_ = await responder._context(
        incoming, "hi", app, ConversationStore(), "tag", skill_turn=turn, tool_ctx=ctx
    )
    assert "private" not in system
    assert "error=ValueError" in caplog.text

    async def too_long(ctx):
        return "x" * 401

    plugin = HookPlugin(context=too_long)
    _, stores, incoming, ctx, _ = await _turn(plugin)
    responder = AgentResponder(
        ScriptedProvider([]),
        system_prompt="legacy",
        skills_prompt="skills",
        settings=AgentSettings(),
        limits=Limits(stores.cache),
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        responder=responder,
        bot_name="testbot",
    )
    turn = SkillTurn(SkillsMode.ON, ["hook_skill"], [], {}, [], [], 8)
    system, *_ = await responder._context(
        incoming, "hi", app, ConversationStore(), "tag", skill_turn=turn, tool_ctx=ctx
    )
    assert "x" * 401 not in system
    assert "skill context too long skill=hook_skill chars=401" in caplog.text


@pytest.mark.asyncio
async def test_skill_context_is_not_called_for_inactive_skill():
    async def context(ctx):
        return "Trusted context"

    plugin = HookPlugin(context=context)
    _, stores, incoming, ctx, _ = await _turn(plugin)
    responder = AgentResponder(
        ScriptedProvider([]),
        system_prompt="legacy",
        skills_prompt="skills",
        settings=AgentSettings(),
        limits=Limits(stores.cache),
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        responder=responder,
        bot_name="testbot",
    )
    turn = SkillTurn(SkillsMode.ON, [], [], {}, [], [], 8)
    system, *_ = await responder._context(
        incoming, "hi", app, ConversationStore(), "tag", skill_turn=turn, tool_ctx=ctx
    )
    assert "Trusted context" not in system
    assert plugin.context_calls == 0


@pytest.mark.asyncio
async def test_skill_context_is_never_called_in_off_mode():
    async def context(ctx):
        return "Trusted context"

    plugin = HookPlugin(context=context)
    stores = memory_stores()
    responder = AgentResponder(
        ScriptedProvider([]),
        system_prompt="legacy",
        settings=AgentSettings(),
        limits=Limits(stores.cache),
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        responder=responder,
        bot_name="testbot",
    )
    incoming = message("hi")
    turn = SkillTurn(SkillsMode.OFF, ["hook_skill"], [], {}, [], [], 8)
    await responder._context(
        incoming,
        "hi",
        app,
        ConversationStore(),
        "tag",
        skill_turn=turn,
        tool_ctx=ToolContext(incoming.chat, incoming.user, False, app.platform, stores),
    )
    assert plugin.context_calls == 0


class RecordingPlugin(HookPlugin):
    """Records the photo and routing facts seen by the pin, context hook and tool handler."""

    def __init__(self):
        self.seen = {}

        async def pin(ctx):
            self.seen["pin"] = _facts(ctx)
            return True

        async def context(ctx):
            self.seen["context"] = _facts(ctx)
            return None

        super().__init__(pin=pin, context=context)

    def tools(self):
        async def handler(ctx):
            self.seen["handler"] = _facts(ctx)
            return ToolResult("ok")

        return [Tool("hook_tool", "Hook tool", {"type": "object", "properties": {}}, handler)]


def _facts(ctx):
    return (
        tuple(item.id for item in ctx.turn.attachments),
        ctx.turn.attachment_owners,
        ctx.turn.explicit_reply_attachment_ids,
        ctx.turn.routing,
    )


def _recording_app(plugin, calls=1):
    from chatcore.llm.types import Completion, Message, ToolCall

    stores = memory_stores()
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("", (ToolCall("c1", "hook_tool", {}),)), "tool_use"),
            Completion(Message.assistant("done"), "end_turn"),
        ]
        * calls
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
    )
    app.responder = AgentResponder(
        provider,
        system_prompt="legacy",
        settings=AgentSettings(
            skills=SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}))
        ),
        limits=Limits(stores.cache),
    )
    return app


@pytest.mark.asyncio
async def test_pin_sees_the_same_validated_reply_photo_metadata_as_the_handler():
    from dataclasses import replace

    from chatcore.events import Attachment, AttachmentKind, MessageRef, User, UserRef
    from chatcore.testing import PLATFORM
    from chatcore.tools.registry import RoutingContext

    plugin = RecordingPlugin()
    app = _recording_app(plugin)
    bob = User(UserRef(PLATFORM, "bob"), "Bob", "bob")
    photo = message("", sender=bob, attachments=[Attachment("photo-1", AttachmentKind.PHOTO)])
    await app.handle(photo)
    assert plugin.seen == {}
    request = replace(
        message("iron man pls", mention=True), reply_to=MessageRef(GROUP, photo.message_id)
    )
    await app.handle(request)
    assert plugin.seen["pin"] == plugin.seen["context"] == plugin.seen["handler"]
    ids, owners, explicit, routing = plugin.seen["pin"]
    assert ids == ("photo-1",)
    assert owners == (bob.ref.key,)
    assert explicit == frozenset({"photo-1"})
    assert routing == RoutingContext(
        text="iron man pls", reply_author=bob.ref.key, reply_text="", reply_bot_image=False
    )


@pytest.mark.asyncio
async def test_routing_context_carries_the_replied_request_but_never_another_topic():
    from dataclasses import replace

    from chatcore.events import MessageRef
    from chatcore.tools.registry import RoutingContext

    plugin = RecordingPlugin()
    app = _recording_app(plugin, calls=3)
    first = message("draw a cat", mention=True)
    await app.handle(first)
    await app.handle(
        replace(message("try again", mention=True), reply_to=MessageRef(GROUP, first.message_id))
    )
    assert plugin.seen["pin"][3] == RoutingContext(
        text="try again", reply_author=first.user.ref.key, reply_text="draw a cat"
    )
    await app.handle(
        replace(
            message("try again", mention=True, thread_id="7"),
            reply_to=MessageRef(GROUP, first.message_id),
        )
    )
    assert plugin.seen["pin"][3] == RoutingContext(text="try again")


@pytest.mark.asyncio
async def test_routing_context_drops_a_replied_request_from_before_a_reset():
    from dataclasses import replace
    from datetime import timedelta

    from chatcore.events import MessageRef
    from chatcore.tools.registry import RoutingContext

    plugin = RecordingPlugin()
    app = _recording_app(plugin, calls=2)
    first = message("draw a cat", mention=True)
    await app.handle(first)
    store = app.responder._store(app)
    stored = await app.stores.repo.get_message(GROUP, first.message_id)
    await store.reset(GROUP, None, at=stored.sent_at + timedelta(seconds=1))
    await app.handle(
        replace(message("try again", mention=True), reply_to=MessageRef(GROUP, first.message_id))
    )
    assert plugin.seen["pin"][3] == RoutingContext(text="try again")


# use_skill must render the same per-turn context an initially active skill gets.


@pytest.mark.asyncio
async def test_use_skill_appends_the_context_hook_after_the_instructions():
    async def context(ctx):
        return "Trusted context"

    plugin = HookPlugin(context=context)
    _, _, _, ctx, turn = await _turn(plugin)
    assert turn.active == [] and plugin.context_calls == 0
    result = await turn.use_skill({"name": "hook_skill"}, ctx)
    assert not result.is_error
    assert "Base instructions\nTrusted context" in result.content
    assert result.content.startswith(
        "Loaded hook_skill: tools hook_tool are available from your next step."
    )
    assert plugin.context_calls == 1
    assert not result.untrusted and not result.persist_content


@pytest.mark.asyncio
async def test_use_skill_drops_oversized_context_and_logs_only_its_length(caplog):
    async def too_long(ctx):
        return "x" * 401

    plugin = HookPlugin(context=too_long)
    _, _, _, ctx, turn = await _turn(plugin)
    result = await turn.use_skill({"name": "hook_skill"}, ctx)
    assert not result.is_error
    assert "Base instructions" in result.content
    assert "xxx" not in result.content
    assert "skill context too long skill=hook_skill chars=401" in caplog.text
    assert "hook_tool" in turn.offered


@pytest.mark.asyncio
async def test_use_skill_ignores_a_failing_context_hook_and_logs_only_its_class(caplog):
    async def broken(ctx):
        raise ValueError("private detail")

    plugin = HookPlugin(context=broken)
    _, _, _, ctx, turn = await _turn(plugin)
    result = await turn.use_skill({"name": "hook_skill"}, ctx)
    assert not result.is_error
    assert "Base instructions" in result.content
    assert "private detail" not in result.content
    assert "private detail" not in caplog.text
    assert "skill context failed skill=hook_skill error=ValueError" in caplog.text
    assert "hook_tool" in turn.offered


@pytest.mark.asyncio
async def test_use_skill_accepts_exactly_400_context_characters_and_skips_non_text():
    async def at_limit(ctx):
        return "y" * 400

    plugin = HookPlugin(context=at_limit)
    _, _, _, ctx, turn = await _turn(plugin)
    result = await turn.use_skill({"name": "hook_skill"}, ctx)
    assert "y" * 400 in result.content

    async def not_text(ctx):
        return 7

    plugin = HookPlugin(context=not_text)
    _, _, _, ctx, turn = await _turn(plugin)
    result = await turn.use_skill({"name": "hook_skill"}, ctx)
    assert not result.is_error and result.content.rstrip().endswith("Base instructions")


@pytest.mark.asyncio
async def test_use_skill_does_not_run_the_context_hook_for_a_refused_skill():
    async def context(ctx):
        return "Trusted context"

    plugin = HookPlugin(context=context, gate_state=GateState.OFF)
    _, _, _, ctx, turn = await _turn(plugin)
    result = await turn.use_skill({"name": "hook_skill"}, ctx)
    assert result.is_error
    assert "Trusted context" not in result.content
    assert plugin.context_calls == 0
