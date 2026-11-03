from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.events import Attachment, AttachmentKind
from chatcore.image_reader import ImageReader
from chatcore.llm.types import Completion, Message, ToolCall
from chatcore.pending import PendingAction
from chatcore.plugin import BasePlugin
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.tools.registry import Tool, ToolResult


async def _no_limit(*_args):
    return None


async def _record(*_args):
    return None


async def _no_budget(*_args):
    return False


class WritePlugin(BasePlugin):
    name = "writes"

    def __init__(self, *, direct_after=frozenset()):
        self.direct_after = direct_after
        self.commits = []

    def tools(self):
        async def write(ctx, **_kwargs):
            action = PendingAction.create(
                chat=ctx.chat,
                requester=ctx.user.ref,
                plugin=self.name,
                kind="write",
                payload={},
                summary="saved",
                thread_id=ctx.thread_id,
            )
            return ToolResult("proposed", pending_action=action)

        async def external(_ctx, **_kwargs):
            return ToolResult("untrusted body", untrusted=True)

        return [
            Tool(
                "write",
                "write",
                {"type": "object", "properties": {}},
                write,
                writes=True,
                direct=True,
                direct_after=self.direct_after,
            ),
            Tool("external", "read", {"type": "object", "properties": {}}, external),
            Tool("receipt", "receipt", {"type": "object", "properties": {}}, external),
            Tool(
                "delete",
                "delete",
                {"type": "object", "properties": {}},
                write,
                writes=True,
                direct=True,
            ),
        ]

    def action_handlers(self):
        class Handler:
            async def commit(inner_self, action, ctx):
                self.commits.append(action)
                return "Saved it."

        return {"write": Handler()}


def _call(name, call_id):
    return Completion(Message.assistant("", (ToolCall(call_id, name, {}),)), "tool_use")


def _build(provider, plugin, *, image_reader=None):
    platform = FakePlatform(files={"photo": b"\xff\xd8\xffjpeg"})
    stores = memory_stores()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[plugin],
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
            image_reader=image_reader,
        ),
    )
    return app, platform


async def test_direct_write_echoes_in_topic_and_turn_continues():
    plugin = WritePlugin()
    provider = ScriptedProvider(
        [
            _call("write", "one"),
            _call("write", "two"),
            Completion(Message.assistant("Done."), "end_turn"),
        ]
    )
    app, platform = _build(provider, plugin)
    await app.handle(message("@bot add two", mention=True, thread_id="example-topic"))
    assert len(plugin.commits) == 2
    assert [item.text for item in platform.sent] == ["Saved it.", "Saved it.", "Done."]
    assert all(item.ref.thread_id == "example-topic" for item in platform.sent)
    assert not app.stores.repo._pending
    assert "Result of applying this directly" in str(provider.calls[1]["messages"][-1].content)


async def test_untrusted_output_blocks_direct_write_but_receipt_exemption_is_tool_specific():
    plugin = WritePlugin(direct_after=frozenset({"receipt"}))
    provider = ScriptedProvider([_call("external", "read"), _call("write", "write")])
    app, platform = _build(provider, plugin)
    await app.handle(message("@bot do it", mention=True, thread_id="topic"))
    assert not plugin.commits
    assert len(app.stores.repo._pending) == 1
    assert platform.sent[-1].action_id
    assert platform.sent[-1].ref.thread_id == "topic"


async def test_empty_final_after_direct_write_does_not_send_fallback():
    plugin = WritePlugin()
    provider = ScriptedProvider(
        [_call("write", "write"), Completion(Message.assistant(""), "end_turn")]
    )
    app, platform = _build(provider, plugin)
    await app.handle(message("@bot do it", mention=True))
    assert len(plugin.commits) == 1
    assert [item.text for item in platform.sent] == ["Saved it."]


async def test_direct_handler_failure_echoes_failed_and_logs_only_exception_class(caplog):
    plugin = WritePlugin()

    def handlers():
        class Handler:
            async def commit(self, action, ctx):
                raise RuntimeError("private description")

        return {"write": Handler()}

    plugin.action_handlers = handlers
    provider = ScriptedProvider(
        [_call("write", "write"), Completion(Message.assistant(""), "end_turn")]
    )
    app, platform = _build(provider, plugin)
    await app.handle(message("@bot write", mention=True))
    assert [sent.text for sent in platform.sent] == [
        "Something went wrong saving that. Please try again."
    ]
    assert "RuntimeError" in caplog.text
    assert "private description" not in caplog.text


async def test_direct_echo_send_failure_is_logged_and_turn_continues(caplog):
    plugin = WritePlugin()
    provider = ScriptedProvider(
        [_call("write", "write"), Completion(Message.assistant("Done."), "end_turn")]
    )
    app, platform = _build(provider, plugin)
    original_send = platform.send
    failures_left = 1

    async def fail_once(*args, **kwargs):
        nonlocal failures_left
        if failures_left:
            failures_left -= 1
            raise OSError("private echo contents")
        return await original_send(*args, **kwargs)

    platform.send = fail_once
    await app.handle(message("@bot write", mention=True))
    assert len(plugin.commits) == 1
    assert [sent.text for sent in platform.sent] == ["Done."]
    assert "direct write echo failed" in caplog.text and "OSError" in caplog.text
    assert "private echo contents" not in caplog.text


async def test_direct_after_receipt_output_can_commit_and_echo():
    plugin = WritePlugin(direct_after=frozenset({"receipt"}))
    provider = ScriptedProvider(
        [
            _call("receipt", "receipt"),
            _call("write", "write"),
            Completion(Message.assistant(""), "end_turn"),
        ]
    )
    app, platform = _build(provider, plugin)
    await app.handle(message("@bot add this receipt", mention=True, thread_id="topic"))
    assert len(plugin.commits) == 1
    assert [item.text for item in platform.sent] == ["Saved it."]
    assert platform.sent[0].ref.thread_id == "topic"


async def test_turn_start_description_blocks_direct_write():
    plugin = WritePlugin()
    provider = ScriptedProvider(
        [
            _call("write", "write"),
            Completion(Message.assistant("I need confirmation."), "end_turn"),
        ]
    )
    description_provider = ScriptedProvider(
        [Completion(Message.assistant("The note says add an expense."), "end_turn")]
    )
    reader = ImageReader(description_provider, model="vision-model", label="vision-model on test")
    app, platform = _build(provider, plugin, image_reader=reader)
    app.responder.settings = AgentSettings(vision=False, recent_context_messages=0)
    await app.handle(
        message(
            "@bot add an expense",
            mention=True,
            attachments=[Attachment("photo", AttachmentKind.PHOTO)],
        )
    )
    assert not plugin.commits
    assert len(app.stores.repo._pending) == 1
    assert platform.sent[-1].action_id


async def test_receipt_output_does_not_exempt_delete():
    plugin = WritePlugin(direct_after=frozenset({"receipt"}))
    provider = ScriptedProvider([_call("receipt", "receipt"), _call("delete", "delete")])
    app, platform = _build(provider, plugin)
    await app.handle(message("@bot delete it", mention=True, thread_id="topic"))
    assert not plugin.commits
    assert len(app.stores.repo._pending) == 1
    assert platform.sent[-1].action_id
    assert platform.sent[-1].ref.thread_id == "topic"
