"""Full agent turns for photo edits in a group: what the model is offered, and the edit path.

Regression for the live sb_bot report (2026-11-12): a group member sent their photo with
"make this photo a watercolour" (as a caption, and as a reply to their own photo). The model
called ``generate_image`` once without ``reference`` because the offered tool text described
text-only generation. These tests pin the offered text and run the turn end to end.
"""

import base64
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.events import Attachment, AttachmentKind, MessageRef, User, UserRef
from chatcore.limits import Limits
from chatcore.llm.types import Completion, ImagePart, Message, ToolCall
from chatcore.skill_turn import SkillSettings
from chatcore.skills import SkillsMode
from chatcore.testing import GROUP, PLATFORM, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore_image import ImagePlugin, ImageSettings
from pydantic import SecretStr

REQUEST = "@testbot make this photo a watercolour"
JPEG = b"\xff\xd8\xff" + b"photo-bytes"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x03\xe8\x00\x00\x03\xe8" + b"image"
ALICE = User(UserRef(PLATFORM, "alice"), "Alice", "alice")
BOB = User(UserRef(PLATFORM, "bob"), "Bob", "bob")


def _edit_call(reference=1):
    return Completion(
        Message.assistant(
            "",
            (
                ToolCall(
                    "call-1",
                    "generate_image",
                    {
                        "prompt": "The same photo as a soft watercolour painting",
                        "size": "square",
                        "title": "Watercolour",
                        "reference": reference,
                    },
                ),
            ),
        ),
        "tool_use",
    )


def _build():
    stores = memory_stores()
    platform = FakePlatform()
    platform.files["photo-1"] = JPEG
    access = AccessPolicy([GROUP.key], [])
    plugin = ImagePlugin(
        access,
        platform,
        settings=ImageSettings(
            IMAGE_ENABLED=True,
            IMAGE_EDIT_ENABLED=True,
            IMAGE_OPENAI_API_KEY=SecretStr("fake"),
        ),
    )
    result = SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(PNG).decode())])
    client = SimpleNamespace(
        images=SimpleNamespace(edit=AsyncMock(return_value=result), generate=AsyncMock())
    )
    plugin.service._client = client
    provider = ScriptedProvider(
        [_edit_call(), Completion(Message.assistant("Here it is as a watercolour."), "end_turn")]
    )
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
    )
    app.responder = AgentResponder(
        provider,
        system_prompt="You are a helpful bot.",
        settings=AgentSettings(
            skills=SkillSettings(
                mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword"
            ),
            vision=True,
        ),
        limits=Limits(stores.cache),
        clock=lambda: datetime(2026, 11, 12, tzinfo=UTC),
    )
    return app, platform, provider, client


def _photo(sender=ALICE, text=REQUEST, *, mention=True):
    return message(
        text,
        sender=sender,
        mention=mention,
        attachments=[Attachment("photo-1", AttachmentKind.PHOTO)],
    )


def _assert_offered_edit(request):
    tool = next(spec for spec in request["tools"] if spec.name == "generate_image")
    assert "photo" in tool.description and "reference" in tool.description
    reference = tool.parameters["properties"]["reference"]["description"]
    assert "replied to" in reference and "1" in reference
    assert "call generate_image with reference" in request["system"]
    assert "Never say you can't" in request["system"]
    last = request["messages"][-1]
    assert [part for part in last.content if isinstance(part, ImagePart)] == [
        ImagePart(JPEG, "image/jpeg")
    ]


async def _assert_edited(platform, client):
    client.images.generate.assert_not_awaited()
    client.images.edit.assert_awaited_once()
    assert client.images.edit.await_args.kwargs["image"] == ("reference.jpg", JPEG, "image/jpeg")
    assert [photo.caption for photo in platform.sent_photos] == ["🖼 Watercolour"]


async def test_group_photo_with_caption_is_edited():
    app, platform, provider, client = _build()
    await app.handle(_photo())
    _assert_offered_edit(provider.calls[0])
    await _assert_edited(platform, client)


async def test_group_reply_to_own_photo_is_edited():
    app, platform, provider, client = _build()
    photo = _photo(text="", mention=False)
    await app.handle(photo)
    assert provider.calls == []
    request = replace(
        message(REQUEST, sender=ALICE, mention=True),
        reply_to=MessageRef(GROUP, photo.message_id),
    )
    await app.handle(request)
    _assert_offered_edit(provider.calls[0])
    await _assert_edited(platform, client)


async def test_group_reply_to_another_members_photo_is_refused():
    app, platform, provider, client = _build()
    photo = _photo(sender=BOB, text="", mention=False)
    await app.handle(photo)
    request = replace(
        message(REQUEST, sender=ALICE, mention=True),
        reply_to=MessageRef(GROUP, photo.message_id),
    )
    await app.handle(request)
    tool_results = [
        part.text
        for call in provider.calls[1:]
        for msg in call["messages"]
        if msg.role == "tool"
        for part in msg.content
        if hasattr(part, "text")
    ]
    assert any("In groups I can only use your own photos." in text for text in tool_results)
    client.images.edit.assert_not_awaited()
    client.images.generate.assert_not_awaited()
    assert platform.sent_photos == []
