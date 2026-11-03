"""Stage-4 conversation tools: persona read, confirmed context reset and confirmed /new."""

from dataclasses import replace

from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin, ConversationStore
from chatcore.events import ChatRef
from chatcore.plugin import CommandContext
from chatcore.skills import GateState
from chatcore.store.base import ChatSettings
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore.threads import ThreadSettings, new_conversation_key
from chatcore.tools.registry import ToolContext, TurnState

MODERATOR = user("mod")


def build(*, threads=True):
    stores = memory_stores()
    plugin = ConversationPlugin(
        ConversationStore(),
        can_reset=lambda actor, _chat: actor == MODERATOR.ref,
        bot_name="testbot",
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[plugin],
        bot_name="testbot",
        group_mentions_only=True,
        conversation_threads=ThreadSettings(continue_window_s=3600) if threads else None,
    )
    return app, plugin, stores


def tool_ctx(app, who, *, chat=GROUP, is_dm=False, thread_id="t1"):
    return ToolContext(
        chat, who, is_dm, app.platform, app.stores, thread_id=thread_id, turn=TurnState()
    )


def command_ctx(app, who, *, chat=GROUP, is_dm=False, thread_id="t1"):
    return app.command_context(chat, who, is_dm=is_dm, thread_id=thread_id)


async def test_tools_and_skill_are_registered_only_with_threads():
    app, _plugin, _ = build()
    assert {
        "get_persona",
        "set_persona",
        "request_context_reset",
        "start_new_conversation",
    } <= set(tool.name for tool in app.tools.available(is_admin=False))
    assert app.skills.get("conversation").tools == (
        "get_persona",
        "set_persona",
        "request_context_reset",
        "start_new_conversation",
    )
    bare, _plugin, _ = build(threads=False)
    assert bare.tools.get("start_new_conversation") is None
    assert bare.skills.get("conversation").tools == (
        "get_persona",
        "set_persona",
        "request_context_reset",
    )
    for name in ("request_context_reset", "start_new_conversation"):
        tool = app.tools.get(name)
        assert tool.writes and not tool.direct


async def test_get_persona_is_untrusted_and_capped():
    app, _plugin, stores = build()
    ctx = tool_ctx(app, user())
    empty = await app.tools.call("get_persona", {}, ctx)
    assert empty.content == "No persona is set for this chat." and not empty.untrusted
    await stores.repo.save_chat_settings(
        replace(ChatSettings(GROUP), persona="x" * 2000), updated_by=MODERATOR.ref
    )
    result = await app.tools.call("get_persona", {}, ctx)
    assert result.untrusted and ctx.turn.read_untrusted
    assert len(result.content) == len("Current persona: ") + 600


async def test_reset_gate_hint_points_non_moderators_to_new():
    app, _plugin, _ = build()
    denied = await app.skills.evaluate_tool("request_context_reset", tool_ctx(app, user()))
    assert denied.state is GateState.NOT_ALLOWED and "{cmd:new}" in denied.hint
    assert app.command_form("new", is_dm=False) == "/new@testbot"
    allowed = await app.skills.evaluate_tool("request_context_reset", tool_ctx(app, MODERATOR))
    assert allowed.state is GateState.AVAILABLE
    dm = ChatRef(GROUP.platform, "alice")
    in_dm = await app.skills.evaluate_tool(
        "request_context_reset", tool_ctx(app, user(), chat=dm, is_dm=True)
    )
    assert in_dm.state is GateState.AVAILABLE
    bare, _plugin, _ = build(threads=False)
    hint = (await bare.skills.evaluate_tool("request_context_reset", tool_ctx(bare, user()))).hint
    assert "{cmd:new}" not in hint


async def test_reset_is_proposed_then_rechecked_and_applied_on_confirm():
    app, plugin, _ = build()
    refused = await app.tools.call("request_context_reset", {}, tool_ctx(app, user()))
    assert refused.is_error and refused.pending_action is None

    result = await app.tools.call("request_context_reset", {}, tool_ctx(app, MODERATOR))
    action = result.pending_action
    assert action is not None and action.handler_key == "conversation.reset"
    assert "this topic" in result.content
    assert await plugin.store.reset_point(GROUP, "t1") is None

    handler = plugin.action_handlers()["reset"]
    # Another member cannot commit it, and a different topic does not match.
    assert "Nothing was changed" in await handler.commit(action, command_ctx(app, user()))
    other_topic = command_ctx(app, MODERATOR, thread_id="t2")
    assert "Nothing was changed" in await handler.commit(action, other_topic)
    assert await plugin.store.reset_point(GROUP, "t1") is None

    reply = await handler.commit(action, command_ctx(app, MODERATOR))
    assert reply.startswith("Context reset. I'll ignore earlier conversation in this topic")
    assert (await plugin.store.reset_point(GROUP, "t1")).generation == 1


async def test_reset_commit_rechecks_permission():
    app, plugin, _ = build()
    action = (
        await app.tools.call("request_context_reset", {}, tool_ctx(app, MODERATOR))
    ).pending_action
    plugin.can_reset = lambda _actor, _chat: False
    reply = await plugin.action_handlers()["reset"].commit(action, command_ctx(app, MODERATOR))
    assert "Nothing was changed" in reply
    assert await plugin.store.reset_point(GROUP, "t1") is None


async def test_start_new_conversation_sets_marker_only_after_confirm():
    app, plugin, stores = build()
    alice = user()
    gate = await app.skills.evaluate_tool(
        "start_new_conversation",
        tool_ctx(app, alice, chat=ChatRef(GROUP.platform, "alice"), is_dm=True),
    )
    assert gate.state is GateState.NOT_ALLOWED
    result = await app.tools.call("start_new_conversation", {}, tool_ctx(app, alice))
    action = result.pending_action
    assert action is not None and action.handler_key == "conversation.new"
    key = new_conversation_key(GROUP.key, "t1", alice.ref.key)
    assert await stores.cache.get(key) is None

    handler = plugin.action_handlers()["new"]
    assert "Nothing was changed" in await handler.commit(action, command_ctx(app, MODERATOR))
    assert await stores.cache.get(key) is None
    reply = await handler.commit(action, command_ctx(app, alice))
    assert reply == "Okay, your next message to me starts a new conversation."
    assert await stores.cache.get(key) == b"1"
    # Only the requester's marker is set.
    assert await stores.cache.get(new_conversation_key(GROUP.key, "t1", MODERATOR.ref.key)) is None


async def test_new_command_and_tool_share_the_marker_key():
    app, _plugin, stores = build()
    alice = user()
    ctx: CommandContext = command_ctx(app, alice)
    await app.commands["new"].handler(ctx, "")
    assert await stores.cache.get(new_conversation_key(GROUP.key, "t1", alice.ref.key)) == b"1"


def _agent_app(provider):
    from chatcore.agent import AgentResponder, AgentSettings
    from chatcore.limits import Limits
    from chatcore.skill_turn import SkillSettings
    from chatcore.skills import SkillsMode

    app, plugin, stores = build()
    app.responder = AgentResponder(
        provider,
        system_prompt="legacy",
        settings=AgentSettings(
            skills=SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword")
        ),
        limits=Limits(stores.cache),
    )
    return app, plugin


async def test_on_mode_routes_reset_request_through_confirmation_end_to_end():
    from chatcore.events import ButtonPress
    from chatcore.llm.types import Completion, Message, ToolCall
    from chatcore.testing import ScriptedProvider, message

    provider = ScriptedProvider(
        [
            Completion(
                Message.assistant("", (ToolCall("c1", "request_context_reset", {}),)),
                "tool_use",
            ),
            Completion(Message.assistant("Please confirm the reset."), "end_turn"),
        ]
    )
    app, plugin = _agent_app(provider)
    await app.handle(message("@testbot please reset your context", sender=MODERATOR, mention=True))
    offered = {tool.name for tool in provider.calls[0]["tools"]}
    assert "request_context_reset" in offered
    prompt = next(sent for sent in app.platform.sent if sent.action_id)
    assert prompt.text.startswith("Reset my context in this chat")
    assert await plugin.store.reset_point(GROUP, None) is None

    await app.handle(ButtonPress(GROUP, MODERATOR, prompt.action_id, "confirm", prompt.ref))
    assert (await plugin.store.reset_point(GROUP, None)).generation == 1
    assert app.platform.texts[-1].startswith("Context reset.")


async def test_on_mode_hides_reset_from_members_and_hints_new():
    from chatcore.llm.types import Completion, Message
    from chatcore.testing import ScriptedProvider, message

    provider = ScriptedProvider([Completion(Message.assistant("Ask a moderator."), "end_turn")])
    app, _plugin = _agent_app(provider)
    await app.handle(message("@testbot reset your context", sender=user(), mention=True))
    offered = {tool.name for tool in provider.calls[0]["tools"]}
    assert "request_context_reset" not in offered
    assert "start_new_conversation" in offered
    assert "/new@testbot" in provider.calls[0]["system"]


async def test_set_persona_applies_immediately_for_members():
    app, _plugin, _ = build()
    assert (await app.tools.call("set_persona", {"persona": " "}, tool_ctx(app, user()))).is_error
    too_long = await app.tools.call("set_persona", {"persona": "x" * 501}, tool_ctx(app, user()))
    assert too_long.is_error
    assert (await app.stores.repo.get_chat_settings(GROUP)).persona is None

    result = await app.tools.call("set_persona", {"persona": "Be formal"}, tool_ctx(app, user()))
    assert not result.is_error and result.pending_action is None
    assert (await app.stores.repo.get_chat_settings(GROUP)).persona == "Be formal"
