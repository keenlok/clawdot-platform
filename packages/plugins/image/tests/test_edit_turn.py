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

import pytest
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


def _build(responses=None):
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
        list(responses)
        if responses is not None
        else [
            _edit_call(),
            Completion(Message.assistant("Here it is as a watercolour."), "end_turn"),
        ]
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
    assert "Report actual tool errors" in request["system"]
    assert "don't invent format or capability errors" in request["system"]
    assert "Usable photo reference numbers this turn: 1." in request["system"]
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


async def test_group_reply_to_another_members_photo_is_edited():
    app, platform, provider, client = _build()
    photo = _photo(sender=BOB, text="", mention=False)
    await app.handle(photo)
    await app.handle(
        replace(
            message(REQUEST, sender=ALICE, mention=True),
            reply_to=MessageRef(GROUP, photo.message_id),
        )
    )
    await _assert_edited(platform, client)
    assert "In groups, anyone may edit a directly replied-to photo." in provider.calls[0]["system"]


async def test_successive_replies_edit_the_actual_generated_photo():
    app, platform, provider, client = _build()
    await app.handle(_photo())
    first = platform.sent_photos[-1]
    original = (await app.stores.repo.recent_messages(GROUP, limit=20))[0]
    conversation_store = app.responder._store(app)
    conversation = await conversation_store.create_conversation(GROUP, None, ALICE.ref)
    stored = await app.stores.repo.get_message(GROUP, first.ref.message_id)
    assert stored is not None, "Generated photos must be recorded for replies"
    assert stored.is_bot
    assert stored.attachment_requester_key == ALICE.ref.key
    assert stored.attachments
    for version in (b"second", b"third"):
        provider.responses.extend(
            [_edit_call(), Completion(Message.assistant("Edited."), "end_turn")]
        )
        prior = platform.sent_photos[-1]
        await conversation_store.map_messages(
            GROUP,
            conversation.id,
            [prior.ref.message_id],
            primary_message_id=original.message_id,
            is_bot=True,
            user=ALICE.ref,
        )
        client.images.edit.return_value = SimpleNamespace(
            data=[SimpleNamespace(b64_json=base64.b64encode(PNG + version).decode())]
        )
        await app.handle(
            replace(
                message(REQUEST, sender=ALICE, mention=True),
                reply_to=prior.ref,
                reply_to_bot=True,
            )
        )
        assert client.images.edit.await_args.kwargs["image"][1] == prior.data
        assert len(platform.sent_photos) == client.images.edit.await_count


async def test_any_group_member_can_edit_a_generated_photo():
    app, platform, provider, client = _build()
    await app.handle(_photo())
    original = platform.sent_photos[0]
    provider.responses.extend([_edit_call(), Completion(Message.assistant("Edited."), "end_turn")])
    await app.handle(
        replace(
            message(REQUEST, sender=BOB, mention=True),
            reply_to=original.ref,
            reply_to_bot=True,
        )
    )
    assert client.images.edit.await_count == 2
    assert client.images.edit.await_args.kwargs["image"][1] == original.data
    assert len(platform.sent_photos) == 2


async def test_forgetting_requester_removes_generated_photo_references():
    app, platform, provider, client = _build()
    await app.handle(_photo())
    ref = platform.sent_photos[0].ref
    assert await app.stores.repo.get_message(GROUP, ref.message_id) is not None
    await app.stores.repo.purge_user(GROUP, ALICE.ref)
    assert await app.stores.repo.get_message(GROUP, ref.message_id) is None


async def test_record_failure_reports_sent_photo_without_retry(monkeypatch):
    app, platform, provider, client = _build()
    original = app.stores.repo.log_bot_message

    async def failing_record(*args, **kwargs):
        if kwargs.get("attachments"):
            raise RuntimeError("storage unavailable")
        await original(*args, **kwargs)

    monkeypatch.setattr(app.stores.repo, "log_bot_message", failing_record)
    await app.handle(_photo())
    assert len(platform.sent_photos) == 1
    assert client.images.edit.await_count == 1
    texts = [
        part.text
        for msg in provider.calls[-1]["messages"]
        if msg.role == "tool"
        for part in msg.content
        if hasattr(part, "text")
    ]
    assert any("Image sent successfully" in text for text in texts)


async def test_missing_generated_file_never_substitutes_original():
    app, platform, provider, client = _build()
    await app.handle(_photo())
    stored = await app.stores.repo.get_message(GROUP, platform.sent_photos[0].ref.message_id)
    assert stored is not None
    platform.files.pop(stored.attachments[0].id)
    provider.responses.extend(
        [_edit_call(), Completion(Message.assistant("Upload it again."), "end_turn")]
    )
    await app.handle(
        replace(
            message(REQUEST, sender=ALICE, mention=True),
            reply_to=platform.sent_photos[0].ref,
            reply_to_bot=True,
        )
    )
    assert client.images.edit.await_count == 1
    assert len(platform.sent_photos) == 1


async def test_directly_replied_stored_bot_photo_needs_no_owner_attribution():
    app, platform, provider, client = _build()
    await app.stores.repo.log_bot_message(
        GROUP,
        "unattributed",
        "A photo",
        thread_id=None,
        attachments=(Attachment("photo-1", AttachmentKind.PHOTO),),
    )
    await app.handle(
        replace(
            message(REQUEST, sender=ALICE, mention=True),
            reply_to=MessageRef(GROUP, "unattributed"),
            reply_to_bot=True,
        )
    )
    await _assert_edited(platform, client)


async def test_output_in_another_topic_is_not_an_edit_reference():
    app, platform, provider, client = _build()
    await app.stores.repo.log_bot_message(
        GROUP,
        "other-topic",
        "A photo",
        thread_id="7",
        attachments=(Attachment("photo-1", AttachmentKind.PHOTO),),
        attachment_requester_key=ALICE.ref.key,
    )
    await app.handle(
        replace(
            message(REQUEST, sender=ALICE, mention=True),
            reply_to=MessageRef(GROUP, "other-topic"),
            reply_to_bot=True,
        )
    )
    client.images.edit.assert_not_awaited()
    assert platform.sent_photos == []


async def test_cross_chat_reply_is_not_an_edit_reference():
    from chatcore.events import ChatRef

    app, platform, provider, client = _build()
    await app.stores.repo.log_bot_message(
        GROUP,
        "output",
        "A photo",
        thread_id=None,
        attachments=(Attachment("photo-1", AttachmentKind.PHOTO),),
        attachment_requester_key=ALICE.ref.key,
    )
    await app.handle(
        replace(
            message(REQUEST, sender=ALICE, mention=True),
            reply_to=MessageRef(ChatRef(PLATFORM, "other"), "output"),
            reply_to_bot=True,
        )
    )
    client.images.edit.assert_not_awaited()
    assert platform.sent_photos == []


@pytest.mark.parametrize("reference,expected", [(1, JPEG), (2, PNG)])
async def test_current_photo_precedes_replied_output_in_reference_numbers(reference, expected):
    app, platform, provider, client = _build()
    await app.handle(_photo())
    prior = platform.sent_photos[-1]
    provider.responses.extend(
        [_edit_call(reference), Completion(Message.assistant("Edited."), "end_turn")]
    )
    await app.handle(replace(_photo(), reply_to=prior.ref, reply_to_bot=True))
    assert client.images.edit.await_count == 2
    assert client.images.edit.await_args.kwargs["image"][1] == expected


async def test_other_members_photo_from_text_reply_mapping_is_still_rejected():
    app, platform, provider, client = _build()
    photo = _photo(sender=BOB, text="", mention=False)
    await app.handle(photo)
    await app.stores.repo.log_bot_message(GROUP, "text-only", "Here is an answer.", thread_id=None)
    store = app.responder._store(app)
    conversation = await store.create_conversation(GROUP, None, BOB.ref)
    await store.map_messages(
        GROUP,
        conversation.id,
        ["text-only"],
        primary_message_id=photo.message_id,
        is_bot=True,
        user=BOB.ref,
    )
    await app.handle(
        replace(
            message(REQUEST, sender=ALICE, mention=True),
            reply_to=MessageRef(GROUP, "text-only"),
            reply_to_bot=True,
        )
    )
    client.images.edit.assert_not_awaited()
    client.images.generate.assert_not_awaited()
    assert platform.sent_photos == []


# Normalized phrases from docs/plans/2026-11-14-image-routing-context-reassessment.md. None has
# a router keyword, a listed edit verb plus target, or "please"; two use "when"; one is long.
PLAN_CASES = [
    "generate a photo of someone climbing a tower",
    "ok now make sure u use his face when u generate the Titan",
    "generate it now!!",
    "use this face",
    "ok now use my face instead",
    "make him look like he's scaling the tower, preserve his face and body when u generate "
    "the Titan",
]
LONG_REQUEST = (
    "take the person in this photo and put them on a quiet beach at sunset with soft warm "
    "light, keep the face and clothes exactly as they are, and paint the sky in gentle "
    "orange and pink tones without adding anyone else"
)


@pytest.mark.parametrize("text", [*PLAN_CASES, LONG_REQUEST])
async def test_edit_paraphrase_with_a_captioned_photo_reaches_the_edit_with_that_photo(text):
    app, platform, provider, client = _build()
    await app.handle(_photo(text="@testbot " + text))
    _assert_offered_edit(provider.calls[0])
    await _assert_edited(platform, client)


@pytest.mark.parametrize("text", PLAN_CASES)
async def test_edit_paraphrase_replying_to_another_members_photo_reaches_the_edit(text):
    app, platform, provider, client = _build()
    photo = _photo(sender=BOB, text="", mention=False)
    await app.handle(photo)
    await app.handle(
        replace(
            message("@testbot " + text, sender=ALICE, mention=True),
            reply_to=MessageRef(GROUP, photo.message_id),
        )
    )
    _assert_offered_edit(provider.calls[0])
    await _assert_edited(platform, client)


async def test_long_request_is_over_the_old_length_cap():
    assert len(LONG_REQUEST) > 160


async def test_scripted_content_refusal_makes_no_image_call_and_keeps_availability_guidance():
    # Wiring only: a scripted refusal shows that a text answer on a photo turn runs nothing and
    # that the policy and "name the reason" guidance reach the model. It says nothing about
    # whether a real model refuses, or how it words it.
    refusal = Completion(
        Message.assistant("I won't put a real person into a violent scene."), "end_turn"
    )
    app, platform, provider, client = _build([refusal])
    await app.handle(
        _photo(text="@testbot make it an anime scene where soldiers attack the person")
    )
    assert len(provider.calls) == 1
    request = provider.calls[0]
    _assert_offered_edit(request)
    system = request["system"]
    assert "sexual, violent, humiliating or deceptive" in system
    assert "explain content refusals" in system
    assert "wasn't attempted" in system
    client.images.edit.assert_not_awaited()
    client.images.generate.assert_not_awaited()
    assert platform.sent_photos == []
    assert platform.texts[-1] == "I won't put a real person into a violent scene."
