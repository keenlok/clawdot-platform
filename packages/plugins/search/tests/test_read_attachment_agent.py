from __future__ import annotations

import base64
import json
from dataclasses import replace

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.embed import FakeEmbedder
from chatcore.events import Attachment, AttachmentKind, ChatRef
from chatcore.image_reader import ImageReader
from chatcore.llm.types import Completion, ImagePart, Message, ToolCall, Usage
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message, user
from chatcore_search import SearchPlugin

PHOTO_BYTES = b"\xff\xd8\xfffake-jpeg-data"
PHOTO_FILE_ID = "photo-file"
PHOTO_MESSAGE_ID = "photo-A"


async def _no_limit(*_args):
    return None


async def _record(*_args):
    return None


async def _no_budget(*_args):
    return False


def _walk_values(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_values(child)


async def _run_read_flow(
    *,
    vision: bool = True,
    request_thread: str = "topic",
    attachment_chat: ChatRef = GROUP,
    image_reader=None,
    request_attachments=(),
    read_message_id=PHOTO_MESSAGE_ID,
    photo_bytes=PHOTO_BYTES,
):
    stores = memory_stores()
    conversation = ConversationPlugin()
    search = SearchPlugin(FakeEmbedder())
    platform = FakePlatform(files={PHOTO_FILE_ID: photo_bytes})
    if request_attachments:
        platform.files[request_attachments[0].id] = PHOTO_BYTES
    platform.download_count = 0
    original_download = platform.download

    async def tracked_download(*args, **kwargs):
        platform.download_count += 1
        return await original_download(*args, **kwargs)

    platform.download = tracked_download
    provider = ScriptedProvider(
        [
            Completion(
                Message.assistant(
                    "",
                    (
                        ToolCall(
                            "read-photo",
                            "read_attachment",
                            {"message_id": read_message_id},
                        ),
                    ),
                ),
                "tool_use",
            ),
            Completion(Message.assistant("I can see the photo."), "end_turn"),
        ]
    )

    class Limits:
        def __init__(self):
            self.recorded = []

        async def check(self, *_args):
            return None

        async def record(self, tokens):
            self.recorded.append(tokens)

        async def budget_exhausted(self):
            return False

    limits = Limits()
    responder = AgentResponder(
        provider,
        system_prompt="rules",
        settings=AgentSettings(vision=vision, recent_context_messages=10),
        limits=limits,
        image_reader=image_reader,
    )
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[conversation, search],
        bot_name="bot",
        responder=responder,
    )
    platform.cache_increment_keys = []
    original_incr = stores.cache.incr

    async def tracked_incr(key, *, ttl_s):
        platform.cache_increment_keys.append(key)
        return await original_incr(key, ttl_s=ttl_s)

    stores.cache.incr = tracked_incr
    earlier = replace(
        message("Earlier shared photo", sender=user("alice"), thread_id="topic"),
        chat=attachment_chat,
        message_id=PHOTO_MESSAGE_ID,
        attachments=(Attachment(PHOTO_FILE_ID, AttachmentKind.PHOTO),),
    )
    await stores.repo.log_message(earlier)
    later = message(
        "@bot what is on this list?",
        sender=user("bob"),
        mention=True,
        thread_id=request_thread,
        attachments=request_attachments,
    )
    await app.handle(later)
    return app, conversation, provider


async def test_real_read_attachment_plugin_views_earlier_photo_and_keeps_turn_text_only():
    _app, conversation, provider = await _run_read_flow()

    first_call_text = "\n".join(item.text for item in provider.calls[0]["messages"])
    assert '"attachments":[{"index":0,"kind":"photo"}]' in first_call_text
    assert f'"message_id":"{PHOTO_MESSAGE_ID}"' in first_call_text

    second_messages = provider.calls[1]["messages"]
    tool_result, image_message = second_messages[-2:]
    assert tool_result.role.value == "tool"
    assert not tool_result.is_error
    image_parts = [part for part in image_message.content if isinstance(part, ImagePart)]
    assert len(image_parts) == 1
    assert image_parts[0].data == PHOTO_BYTES

    turns = await conversation.store.recent(GROUP, "topic")
    for turn in turns:
        assert not any(
            isinstance(value, (ImagePart, bytes, bytearray)) for value in _walk_values(turn.content)
        )
        serialized = json.dumps(turn.content)
        assert base64.b64encode(PHOTO_BYTES).decode() not in serialized


async def test_real_read_attachment_rejects_photo_from_another_topic():
    _app, _conversation, provider = await _run_read_flow(request_thread="other")

    tool_result = next(item for item in provider.calls[1]["messages"] if item.role.value == "tool")
    assert tool_result.is_error
    assert tool_result.text == "attachment unavailable: message is in a different topic"
    assert not any(
        isinstance(part, ImagePart)
        for item in provider.calls[1]["messages"]
        for part in item.content
    )


async def test_real_read_attachment_rejects_photo_when_vision_is_off():
    _app, _conversation, provider = await _run_read_flow(vision=False)

    tool_result = next(item for item in provider.calls[1]["messages"] if item.role.value == "tool")
    assert tool_result.is_error
    assert tool_result.text == "attachment unavailable: image viewing is not enabled"
    assert not any(
        isinstance(part, ImagePart)
        for item in provider.calls[1]["messages"]
        for part in item.content
    )


async def test_real_read_attachment_rejects_photo_from_another_chat():
    other_chat = ChatRef(GROUP.platform, "another-group")
    _app, _conversation, provider = await _run_read_flow(attachment_chat=other_chat)

    tool_result = next(item for item in provider.calls[1]["messages"] if item.role.value == "tool")
    assert tool_result.is_error
    assert tool_result.text == "attachment unavailable: message not found in this chat"
    assert not any(
        isinstance(part, ImagePart)
        for item in provider.calls[1]["messages"]
        for part in item.content
    )


async def test_real_read_attachment_describes_photo_in_text_only_turn():
    description_provider = ScriptedProvider(
        [Completion(Message.assistant("A receipt with total $4.00."), "end_turn")]
    )
    reader = ImageReader(description_provider, model="vision-model", label="vision-model on test")
    _app, conversation, provider = await _run_read_flow(vision=False, image_reader=reader)

    result = next(item for item in provider.calls[1]["messages"] if item.role.value == "tool")
    assert not result.is_error
    assert "Description of the image from message photo-A" in result.text
    assert "A receipt with total $4.00." in result.text
    assert result.text.startswith("<untrusted-u") and result.text.endswith(">")
    assert len(description_provider.calls) == 1
    stored = await conversation.store.recent(GROUP, "topic")
    assert all("A receipt with total" not in turn.content.get("text", "") for turn in stored)


async def test_read_attachment_incomplete_description_records_usage():
    description_provider = ScriptedProvider(
        [Completion(Message.assistant("partial"), "max_tokens", Usage(100, 50))]
    )
    reader = ImageReader(description_provider, model="vision-model", label="vision-model on test")
    app, _conversation, provider = await _run_read_flow(vision=False, image_reader=reader)
    result = next(item for item in provider.calls[1]["messages"] if item.role.value == "tool")
    assert result.is_error and "image description failed" in result.text
    assert 150 in app.responder.limits.recorded


async def test_read_attachment_describe_reuses_turn_download_cache():
    description_provider = ScriptedProvider([Completion(Message.assistant("A photo."), "end_turn")])
    reader = ImageReader(description_provider, model="vision-model", label="vision-model on test")
    app, _conversation, _provider = await _run_read_flow(
        vision=False,
        image_reader=reader,
        request_attachments=(Attachment(PHOTO_FILE_ID, AttachmentKind.PHOTO),),
        read_message_id=None,
    )
    assert app.platform.download_count == 1


async def test_invalid_read_attachment_image_does_not_consume_daily_allowance():
    vision = ScriptedProvider([])
    reader = ImageReader(vision, model="vision-model", label="vision-model on test")
    app, _conversation, provider = await _run_read_flow(
        vision=False, image_reader=reader, photo_bytes=b"GIF89a"
    )
    assert provider.calls
    assert app.platform.cache_increment_keys == []
    assert not vision.calls
