from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.events import ChatRef, MessageRef
from chatcore.llm.types import Completion, Message, ToolCall
from chatcore.pending import PendingAction
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message, user
from chatcore.tools.registry import Tool, ToolResult


class Handler:
    async def commit(self, action, ctx):
        return "saved"


class NoLimits:
    async def check(self, *_args):
        return None

    async def record(self, *_args):
        return None

    async def budget_exhausted(self, *_args):
        return False


def _action(chat=GROUP):
    return PendingAction.create(
        chat=chat,
        requester=user().ref,
        plugin="writer",
        kind="create",
        payload={},
        summary="Create item",
    )


@pytest.mark.parametrize("cross_chat", [False, True])
@pytest.mark.parametrize("buttons", [False, True])
async def test_request_reply_to_is_only_used_for_same_chat(cross_chat, buttons):
    platform = FakePlatform()
    platform.capabilities = replace(platform.capabilities, buttons=buttons)
    repo = memory_stores().repo
    from chatcore.confirm import ConfirmService

    service = ConfirmService(platform, repo, {"writer.create": Handler()})
    dm = ChatRef(GROUP.platform, user().ref.id)
    reply = MessageRef(dm if cross_chat else GROUP, "incoming")

    await service.request(_action(), user(), reply_to=reply)

    sent = platform.sent[-1]
    assert sent.chat == GROUP
    assert sent.reply_to == (None if cross_chat else reply)
    if buttons:
        assert sent.action_id is not None
    else:
        assert "reply YES" in sent.text


async def test_agent_requests_confirmation_for_group_action_from_dm(caplog):
    group_action = _action()
    call = ToolCall("write", "write_something", {})
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("", (call,)), "tool_use"),
            Completion(Message.assistant("Could not ask for confirmation."), "end_turn"),
        ]
    )

    class TelegramLikePlatform(FakePlatform):
        async def confirm(
            self, chat, prompt, action_id, requester, *, reply_to=None, thread_id=None
        ):
            if reply_to is not None and reply_to.chat != chat:
                raise ValueError("cross-chat reply")
            return await super().confirm(
                chat, prompt, action_id, requester, reply_to=reply_to, thread_id=thread_id
            )

    platform = TelegramLikePlatform()
    platform.members[GROUP.key] = {user().ref.key}
    stores = memory_stores()
    conversation = ConversationPlugin()

    async def request_write(ctx):
        return ToolResult("write requested", pending_action=group_action)

    responder = AgentResponder(
        provider,
        system_prompt="rules",
        settings=AgentSettings(recent_context_messages=0),
        limits=NoLimits(),
    )
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[conversation],
        bot_name="bot",
        responder=responder,
    )
    app.tools.register(
        Tool(
            "write_something",
            "write",
            {"type": "object", "properties": {}},
            request_write,
            writes=True,
        )
    )
    app.confirm.handlers["writer.create"] = Handler()
    dm_message = replace(message("start group trip", sender=user(), dm=True), message_id="dm1")

    with caplog.at_level(logging.WARNING, logger="chatcore.agent"):
        await app.handle(dm_message)

    assert platform.sent[0].chat == GROUP
    assert platform.sent[0].reply_to is None
    # A requested confirmation ends the turn; only a failed request goes back to the model.
    assert len(provider.calls) == 1
    assert "confirmation request failed" not in caplog.text
    rows = await responder._store(app).recent(dm_message.chat, dm_message.thread_id)
    tool_rows = [str(row.content) for row in rows if row.role == "tool"]
    assert len(tool_rows) == 1
    assert "Confirmation was requested; nothing has been saved yet." in tool_rows[0]
    assert "Could not request confirmation." not in tool_rows[0]
