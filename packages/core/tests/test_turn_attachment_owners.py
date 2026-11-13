from dataclasses import replace

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.events import Attachment, AttachmentKind, ChatRef, MessageRef
from chatcore.llm.types import Completion, Message, ToolCall
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message, user
from chatcore.tools.registry import Tool, ToolResult


async def _no_limit(*_args):
    return None


async def _record(*_args):
    return None


async def _no_budget(*_args):
    return False


def _app(platform, provider):
    return BotApp(
        platform,
        stores=memory_stores(),
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin()],
        bot_name="bot",
        responder=AgentResponder(
            provider,
            system_prompt="rules",
            settings=AgentSettings(vision=True, recent_context_messages=0),
            limits=type(
                "Limits",
                (),
                {"check": _no_limit, "record": _record, "budget_exhausted": _no_budget},
            )(),
        ),
    )


async def _capture(incoming, *, previous=(), expected_reply_ids=frozenset()):
    ids = {item.id: b"\xff\xd8\xffjpeg" for item in incoming.attachments}
    for row in previous:
        ids.update({item.id: b"\xff\xd8\xffjpeg" for item in row.attachments})
    platform = FakePlatform(files=ids)
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("", (ToolCall("one", "capture_turn", {}),)), "tool_use"),
            Completion(Message.assistant("done"), "end_turn"),
        ]
    )
    app = _app(platform, provider)
    captured = {}

    async def capture(ctx):
        captured["attachments"] = ctx.turn.attachments
        captured["owners"] = ctx.turn.attachment_owners
        captured["reply_ids"] = ctx.turn.explicit_reply_attachment_ids
        return ToolResult("captured")

    app.tools.register(
        Tool("capture_turn", "Capture turn", {"type": "object", "properties": {}}, capture)
    )
    for row in previous:
        await app.stores.repo.log_message(row)
    await app.handle(incoming)
    assert len(captured["attachments"]) == len(captured["owners"])
    assert captured["reply_ids"] == expected_reply_ids
    return tuple(zip(captured["attachments"], captured["owners"], strict=True))


async def test_current_message_photo_has_sender_owner():
    photo = Attachment("current", AttachmentKind.PHOTO)
    incoming = message("@bot use it", mention=True, attachments=[photo])
    assert await _capture(incoming) == ((photo, "fake:alice"),)


async def test_reply_photo_owners_follow_current_then_replied_order():
    current = Attachment("current", AttachmentKind.PHOTO)
    replied = Attachment("replied", AttachmentKind.PHOTO)
    old = message("photo", sender=user("bob"), attachments=[replied])
    incoming = replace(
        message("@bot edit", mention=True, attachments=[current]),
        reply_to=MessageRef(GROUP, old.message_id),
    )
    assert await _capture(incoming, previous=(old,), expected_reply_ids={"replied"}) == (
        (current, "fake:alice"),
        (replied, "fake:bob"),
    )


async def test_reply_to_bot_photo_preserves_order_and_explicit_provenance():
    own = Attachment("own", AttachmentKind.PHOTO)
    bot_photo = Attachment("bot-photo", AttachmentKind.PHOTO)
    old = message("bot image", sender=user("bot", is_bot=True), attachments=[bot_photo])
    incoming = replace(
        message("@bot edit", mention=True, attachments=[own]),
        reply_to=MessageRef(GROUP, old.message_id),
    )
    assert await _capture(incoming, previous=(old,), expected_reply_ids={"bot-photo"}) == (
        (own, "fake:alice"),
        (bot_photo, "fake:bot"),
    )


async def test_reply_to_another_thread_or_chat_adds_no_replied_photos():
    current = Attachment("current", AttachmentKind.PHOTO)
    old_photo = Attachment("old", AttachmentKind.PHOTO)
    for old_chat, old_thread, reply_ref in (
        (GROUP, "other-thread", MessageRef(GROUP, "old", "current-thread")),
        (ChatRef("fake", "elsewhere"), None, MessageRef(ChatRef("fake", "elsewhere"), "old")),
    ):
        old = replace(
            message("old", chat=old_chat, thread_id=old_thread, attachments=[old_photo]),
            message_id="old",
        )
        incoming = replace(
            message("@bot edit", mention=True, thread_id="current-thread", attachments=[current]),
            reply_to=reply_ref,
        )
        assert await _capture(incoming, previous=(old,)) == ((current, "fake:alice"),)


async def test_no_photos_and_non_image_documents_are_excluded():
    no_photo = await _capture(message("@bot hello", mention=True))
    assert no_photo == ()
    document = Attachment("doc", AttachmentKind.DOCUMENT, mime_type="application/pdf")
    assert await _capture(message("@bot use it", mention=True, attachments=[document])) == ()
