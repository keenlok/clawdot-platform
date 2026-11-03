from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.llm.types import Completion, ImagePart, Message
from chatcore.plugin import BasePlugin
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message, user


class Hooks(BasePlugin):
    name = "hooks"

    def __init__(self, *, fail=False):
        self.before = []
        self.after = []
        self.fail = fail

    async def before_turn(self, incoming, text, stores):
        self.before.append(text)
        if self.fail:
            raise RuntimeError("private user text")
        return "outside context"

    async def after_turn(self, incoming, text, stores):
        self.after.append(text)
        if self.fail:
            raise RuntimeError("private user text")


def app_for(provider, hook):
    stores = memory_stores()
    conversation = ConversationPlugin()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[conversation, hook],
        bot_name="bot",
        responder=AgentResponder(
            provider,
            system_prompt="rules",
            settings=AgentSettings(recent_context_messages=0),
            limits=type(
                "Limits",
                (),
                {
                    "check": _no_limit,
                    "record": _record,
                    "budget_exhausted": _no_budget,
                },
            )(),
        ),
    )
    return app, conversation


async def _no_limit(*_args):
    return None


async def _record(*_args):
    return None


async def _no_budget(*_args):
    return False


async def test_before_hook_is_untrusted_and_follows_summary():
    hook = Hooks()
    provider = ScriptedProvider([Completion(Message.assistant("answer"), "end_turn")])
    app, conversation = app_for(provider, hook)
    await conversation.store.save_summary(GROUP, None, "rolling summary", 1)
    incoming = message("@bot question", mention=True)
    await app.handle(incoming)
    texts = [item.text for item in provider.calls[0]["messages"]]
    summary_index = next(i for i, text in enumerate(texts) if "rolling summary" in text)
    hook_index = next(i for i, text in enumerate(texts) if "outside context" in text)
    prompt_index = next(i for i, text in enumerate(texts) if "question" in text)
    assert summary_index < hook_index < prompt_index
    assert texts[hook_index].startswith("<untrusted-u") and texts[hook_index].endswith(">")
    assert hook.after == ["question"]
    stored = await conversation.store.recent(GROUP, None)
    assert all(row.message_id == incoming.message_id for row in stored)


async def test_failing_turn_hooks_do_not_break_reply_and_bot_never_runs_hooks(caplog):
    hook = Hooks(fail=True)
    provider = ScriptedProvider([Completion(Message.assistant("answer"), "end_turn")])
    app, _conversation = app_for(provider, hook)
    await app.handle(message("@bot question", mention=True))
    assert app.platform.texts == ["answer"]
    assert "RuntimeError" in caplog.text
    assert "private user text" not in caplog.text
    assert hook.before == ["question"]
    assert hook.after == ["question"]
    await app.responder.respond(
        message(
            "bot message",
            sender=user("bot", is_bot=True),
        ),
        "bot message",
        app,
    )
    assert hook.before == ["question"]


async def test_tools_receive_current_and_human_reply_images_with_main_vision_off():
    from dataclasses import replace

    from chatcore.events import Attachment, AttachmentKind, MessageRef
    from chatcore.llm.types import ToolCall
    from chatcore.tools.registry import Tool, ToolResult

    seen = []
    invocations = []

    async def inspect(ctx):
        invocations.append(ctx.turn.attachments)
        seen.extend(ctx.turn.attachments)
        return ToolResult("saw images")

    plugin = BasePlugin()
    plugin.name = "image-inspector"
    plugin.tools = lambda: [
        Tool("inspect_images", "inspect", {"type": "object", "properties": {}}, inspect)
    ]
    stores = memory_stores()
    prior = message("old receipt")
    reply_image = Attachment("reply", AttachmentKind.DOCUMENT, mime_type="image/png")
    prior = replace(prior, attachments=(reply_image,))
    await stores.repo.log_message(prior)
    current_image = Attachment("current", AttachmentKind.PHOTO)
    platform = FakePlatform(files={"current": b"\xff\xd8\xffx", "reply": b"\x89PNG\r\n\x1a\nx"})
    provider = ScriptedProvider(
        [
            Completion(
                Message.assistant("", (ToolCall("call", "inspect_images", {}),)), "tool_use"
            ),
            Completion(Message.assistant("done"), "end_turn"),
        ]
    )
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[plugin],
        bot_name="bot",
        responder=AgentResponder(
            provider,
            system_prompt="rules",
            settings=AgentSettings(vision=False),
            limits=type(
                "Limits",
                (),
                {
                    "check": _no_limit,
                    "record": _record,
                    "budget_exhausted": _no_budget,
                },
            )(),
        ),
    )
    incoming = replace(
        message("@bot read it", mention=True, attachments=[current_image]),
        reply_to=MessageRef(GROUP, prior.message_id),
    )
    await app.handle(incoming)
    assert seen == [current_image, reply_image]
    assert invocations == [(current_image, reply_image)]
    assert len(provider.calls) == 2
    sent_messages = provider.calls[0]["messages"]
    assert not any(isinstance(part, ImagePart) for m in sent_messages for part in m.content)
    assert any("2 image attachment(s)" in m.text for m in sent_messages)


async def test_bot_reply_images_are_not_exposed_to_tools():
    from dataclasses import replace

    from chatcore.events import Attachment, AttachmentKind, MessageRef
    from chatcore.llm.types import ToolCall
    from chatcore.tools.registry import Tool, ToolResult

    seen = []
    invocations = []

    async def inspect(ctx):
        invocations.append(ctx.turn.attachments)
        seen.extend(ctx.turn.attachments)
        return ToolResult("saw images")

    plugin = BasePlugin()
    plugin.name = "image-inspector"
    plugin.tools = lambda: [
        Tool("inspect_images", "inspect", {"type": "object", "properties": {}}, inspect)
    ]
    stores = memory_stores()
    prior = replace(
        message("bot reply", sender=user("bot", is_bot=True)),
        attachments=(Attachment("bot-image", AttachmentKind.PHOTO),),
    )
    await stores.repo.log_message(prior)
    provider = ScriptedProvider(
        [
            Completion(
                Message.assistant("", (ToolCall("call", "inspect_images", {}),)), "tool_use"
            ),
            Completion(Message.assistant("done"), "end_turn"),
        ]
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[plugin],
        bot_name="bot",
        responder=AgentResponder(
            provider,
            system_prompt="rules",
            settings=AgentSettings(vision=False),
            limits=type(
                "Limits",
                (),
                {
                    "check": _no_limit,
                    "record": _record,
                    "budget_exhausted": _no_budget,
                },
            )(),
        ),
    )
    incoming = replace(
        message("@bot question", mention=True), reply_to=MessageRef(GROUP, prior.message_id)
    )
    await app.handle(incoming)
    assert len(provider.calls) == 2
    assert invocations == [()]
    assert seen == []
    assert not any("image attachment(s)" in m.text for m in provider.calls[0]["messages"])
