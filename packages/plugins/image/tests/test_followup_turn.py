"""Full agent turns for short image follow-ups (2026-11-14 investigation).

Normalized reconstruction of the reported turns: the exact utterances ("try again",
"iron man pls", "can u do so") with fake people, photos, provider and image service. The
original request metadata was not recovered, so reply relationships here are the supported
contexts, not a claim about the historical requests. Scripted completions prove routing,
context and execution wiring only; they do not prove a real model's choices.
"""

import base64
from dataclasses import replace
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
from chatcore_image.service import ImageError
from pydantic import SecretStr

JPEG = b"\xff\xd8\xff" + b"photo-bytes"
NEW_JPEG = b"\xff\xd8\xff" + b"new-photo-bytes"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x03\xe8\x00\x00\x03\xe8" + b"image"
ALICE = User(UserRef(PLATFORM, "alice"), "Alice", "alice")
BOB = User(UserRef(PLATFORM, "bob"), "Bob", "bob")
TEXT = Completion(Message.assistant("Which picture do you mean?"), "end_turn")


def _image_call(reference=None, call_id="call-1"):
    arguments = {"prompt": "Iron Man in a comic style", "size": "square", "title": "Iron Man"}
    if reference is not None:
        arguments["reference"] = reference
    return Completion(
        Message.assistant("", (ToolCall(call_id, "generate_image", arguments),)), "tool_use"
    )


DONE = Completion(Message.assistant("Here it is."), "end_turn")


def _build(responses, *, edit=True):
    stores = memory_stores()
    platform = FakePlatform()
    platform.files["photo-1"] = JPEG
    access = AccessPolicy([GROUP.key], [])
    plugin = ImagePlugin(
        access,
        platform,
        settings=ImageSettings(
            IMAGE_ENABLED=True, IMAGE_EDIT_ENABLED=edit, IMAGE_OPENAI_API_KEY=SecretStr("fake")
        ),
    )
    result = SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(PNG).decode())])
    client = SimpleNamespace(
        images=SimpleNamespace(
            edit=AsyncMock(return_value=result), generate=AsyncMock(return_value=result)
        )
    )
    plugin.service._client = client
    provider = ScriptedProvider(list(responses))
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
    )
    return app, platform, provider, client, plugin


def _photo(sender=ALICE, text="", *, mention=False, thread_id=None):
    return message(
        text,
        sender=sender,
        mention=mention,
        thread_id=thread_id,
        attachments=[Attachment("photo-1", AttachmentKind.PHOTO)],
    )


def _say(text, sender=ALICE, *, reply_to=None, bot=False, thread_id=None):
    incoming = message(text, sender=sender, mention=True, thread_id=thread_id)
    if reply_to is None:
        return incoming
    return replace(incoming, reply_to=MessageRef(GROUP, reply_to), reply_to_bot=bot)


def _offered(request):
    return {spec.name for spec in request["tools"]}


def _called(provider):
    return [
        call.name
        for request in provider.calls
        for item in request["messages"]
        if item.role == "assistant"
        for call in item.tool_calls
    ]


def _no_image(client, platform):
    client.images.edit.assert_not_awaited()
    client.images.generate.assert_not_awaited()
    assert platform.sent_photos == []


async def test_terse_request_replying_to_own_photo_reaches_one_edit_with_that_photo():
    app, platform, provider, client, _ = _build([_image_call(1), DONE])
    photo = _photo()
    await app.handle(photo)
    await app.handle(_say("iron man pls", reply_to=photo.message_id))
    first = provider.calls[0]
    assert "generate_image" in _offered(first)
    assert "Usable photo reference numbers this turn: 1." in first["system"]
    client.images.edit.assert_awaited_once()
    assert client.images.edit.await_args.kwargs["image"][1] == JPEG
    client.images.generate.assert_not_awaited()
    assert set(_called(provider)) == {"generate_image"}


async def test_terse_request_with_a_captioned_photo_is_offered_the_edit():
    app, platform, provider, client, _ = _build([_image_call(1), DONE])
    await app.handle(_photo(text="@testbot iron man pls", mention=True))
    assert "generate_image" in _offered(provider.calls[0])
    client.images.edit.assert_awaited_once()


@pytest.mark.parametrize("text", ["try again", "iron man pls", "can u do so"])
async def test_exact_short_phrases_without_any_context_offer_no_image_tool(text):
    app, platform, provider, client, _ = _build([TEXT])
    await app.handle(_say(text))
    first = provider.calls[0]
    assert "generate_image" not in _offered(first)
    # The skill stays discoverable, and trusted guidance forbids invented reasons.
    assert (
        "image_generation"
        in next(spec for spec in first["tools"] if spec.name == "use_skill").parameters[
            "properties"
        ]["name"]["enum"]
    )
    assert "wasn't attempted" in first["system"]
    _no_image(client, platform)


@pytest.mark.parametrize("text", ["try again", "can u do so"])
async def test_retry_replying_to_a_tracked_generated_photo_edits_that_output(text):
    app, platform, provider, client, _ = _build(
        [_image_call(1), DONE, _image_call(1, "call-2"), DONE]
    )
    await app.handle(_photo(text="@testbot make this photo a watercolour", mention=True))
    output = platform.sent_photos[-1]
    await app.handle(_say(text, sender=BOB, reply_to=output.ref.message_id, bot=True))
    assert "generate_image" in _offered(provider.calls[2])
    assert client.images.edit.await_count == 2
    assert client.images.edit.await_args.kwargs["image"][1] == output.data
    stored = await app.stores.repo.get_message(GROUP, platform.sent_photos[-1].ref.message_id)
    assert stored.attachment_requester_key == BOB.ref.key


async def test_retry_replying_to_own_text_request_is_offered_text_generation():
    app, platform, provider, client, plugin = _build([TEXT] * 4 + [_image_call(), DONE])
    plugin.service.generate = AsyncMock(side_effect=[PNG])
    request = _say("draw a cat wearing a space helmet")
    await app.handle(request)  # scripted: the model answered in text, no image was attempted
    # Let the requester's sticky state run out, so the retry depends on the reply alone.
    for filler in ("hello there", "hello again", "hello once more"):
        await app.handle(_say(filler))
    assert "generate_image" not in _offered(provider.calls[3])
    await app.handle(_say("try again", reply_to=request.message_id))
    assert "generate_image" in _offered(provider.calls[4])
    plugin.service.generate.assert_awaited_once()
    assert plugin.service.generate.await_args.kwargs["reference"] is None
    assert len(platform.sent_photos) == 1


@pytest.mark.parametrize(
    "earlier", ["please withdraw a payment", "overdraw my account today", "what is the weather"]
)
async def test_retry_replying_to_own_non_image_request_offers_no_image_tool(earlier):
    app, platform, provider, client, _ = _build([TEXT, TEXT])
    request = _say(earlier)
    await app.handle(request)
    assert "generate_image" not in _offered(provider.calls[0])
    await app.handle(_say("try again", reply_to=request.message_id))
    assert "generate_image" not in _offered(provider.calls[1])
    assert "## image_generation" not in provider.calls[1]["system"]
    _no_image(client, platform)


NOT_IMAGE_ACTIONS = [
    "is this watercolour?",
    "what cartoon is this?",
    "describe this anime",
    "translate this cartoon",
    "please explain this anime style",
    "thanks",
    "can you edit photos?",
    "look at my lunch",
    "what is this?",
    "when was this taken",
]


def _assert_offered_without_action(request):
    """The tool and its restraint guidance are present; nothing here runs a generation."""
    assert "generate_image" in _offered(request)
    assert "## image_generation" in request["system"]
    assert "Usable photo reference numbers this turn: 1." in request["system"]
    assert "A photo alone is not a request" in request["system"]
    assert "Answer questions, translation requests and thanks normally." in request["system"]
    assert "Generate only when the person explicitly asks for an image." in request["system"]


@pytest.mark.parametrize("text", NOT_IMAGE_ACTIONS)
async def test_photo_reply_without_an_image_action_is_offered_but_never_auto_generates(text):
    # Scripted no-action completion: proves the platform makes no image call by itself. It
    # does not prove a real model stays quiet.
    app, platform, provider, client, _ = _build([TEXT])
    photo = _photo()
    await app.handle(photo)
    await app.handle(_say(text, reply_to=photo.message_id))
    assert len(provider.calls) == 1
    _assert_offered_without_action(provider.calls[0])
    assert _called(provider) == []
    _no_image(client, platform)


@pytest.mark.parametrize("caption", ["@testbot", "@testbot what is this?", "@testbot 翻译一下"])
async def test_captioned_or_bare_photo_is_offered_but_never_auto_generates(caption):
    app, platform, provider, client, _ = _build([TEXT])
    await app.handle(_photo(text=caption, mention=True))
    assert len(provider.calls) == 1
    _assert_offered_without_action(provider.calls[0])
    assert _called(provider) == []
    _no_image(client, platform)


async def test_polite_edit_question_about_a_replied_photo_is_offered_the_edit():
    app, platform, provider, client, _ = _build([_image_call(1), DONE])
    photo = _photo()
    await app.handle(photo)
    await app.handle(_say("can you make this a cartoon?", reply_to=photo.message_id))
    assert "generate_image" in _offered(provider.calls[0])
    client.images.edit.assert_awaited_once()


def _load_image_skill(call_id="load-1"):
    return Completion(
        Message.assistant("", (ToolCall(call_id, "use_skill", {"name": "image_generation"}),)),
        "tool_use",
    )


def _tool_texts(request):
    return [item.text for item in request["messages"] if item.role == "tool"]


async def test_dynamically_loaded_skill_without_a_photo_gets_the_no_substitute_note():
    app, platform, provider, client, _ = _build([_load_image_skill(), TEXT])
    await app.handle(_say("the usual, like before"))
    first, second = provider.calls
    assert "generate_image" not in _offered(first)
    assert "## image_generation" not in first["system"]
    (loaded,) = _tool_texts(second)
    assert loaded.startswith("Loaded image_generation: tools generate_image are available")
    assert "No usable photo is attached or directly replied to this turn" in loaded
    assert "never make a text-only substitute" in loaded
    assert "generate_image" in _offered(second)
    _no_image(client, platform)


async def test_unlisted_wording_with_a_replied_photo_is_offered_from_the_first_step():
    # Before the photo-context change this wording needed a use_skill load first.
    app, platform, provider, client, _ = _build([_image_call(1), DONE])
    photo = _photo()
    await app.handle(photo)
    await app.handle(_say("the usual, like before", reply_to=photo.message_id))
    first = provider.calls[0]
    assert "generate_image" in _offered(first)
    assert "Usable photo reference numbers this turn: 1." in first["system"]
    assert "No usable photo" not in first["system"]
    assert _called(provider) == ["generate_image"]
    client.images.edit.assert_awaited_once()
    assert client.images.edit.await_args.kwargs["image"][1] == JPEG
    client.images.generate.assert_not_awaited()


async def test_dynamically_loaded_skill_with_editing_off_ignores_a_replied_photo():
    app, platform, provider, client, _ = _build([_load_image_skill(), TEXT], edit=False)
    photo = _photo()
    await app.handle(photo)
    await app.handle(_say("the usual, like before", reply_to=photo.message_id))
    first, second = provider.calls
    assert "generate_image" not in _offered(first)
    (loaded,) = _tool_texts(second)
    assert "Usable photo reference" not in loaded
    spec = next(spec for spec in second["tools"] if spec.name == "generate_image")
    assert "reference" not in spec.parameters["properties"]
    _no_image(client, platform)


async def test_dynamically_loaded_skill_with_editing_off_adds_no_reference_note():
    app, platform, provider, client, _ = _build([_load_image_skill(), TEXT], edit=False)
    await app.handle(_say("the usual, like before"))
    (loaded,) = _tool_texts(provider.calls[1])
    assert loaded.startswith("Loaded image_generation:")
    assert "photo" not in loaded.split("next step.", 1)[1].casefold() or "reference" not in loaded
    _no_image(client, platform)


async def test_retry_replying_to_another_persons_request_is_not_inherited():
    app, platform, provider, client, _ = _build([TEXT, TEXT])
    request = _say("draw a cat wearing a space helmet", sender=BOB)
    await app.handle(request)
    await app.handle(_say("try again", sender=ALICE, reply_to=request.message_id))
    assert "generate_image" not in _offered(provider.calls[1])
    _no_image(client, platform)


async def test_sticky_image_skill_is_requester_local_and_survives_another_speaker():
    app, platform, provider, client, _ = _build([TEXT, TEXT, TEXT])
    await app.handle(_say("draw a cat wearing a space helmet", sender=ALICE))
    await app.handle(_say("try again", sender=BOB))
    await app.handle(_say("try again", sender=ALICE))
    assert "generate_image" in _offered(provider.calls[0])
    assert "generate_image" not in _offered(provider.calls[1])
    assert "generate_image" in _offered(provider.calls[2])
    _no_image(client, platform)


async def test_photo_in_another_topic_is_not_edit_context():
    app, platform, provider, client, _ = _build([TEXT])
    photo = _photo(thread_id="7")
    await app.handle(photo)
    await app.handle(_say("iron man pls", reply_to=photo.message_id))
    assert "generate_image" not in _offered(provider.calls[0])
    _no_image(client, platform)


async def test_other_members_photo_reached_through_a_text_reply_is_not_edit_context():
    app, platform, provider, client, _ = _build([TEXT])
    photo = _photo(sender=BOB)
    await app.handle(photo)
    await app.stores.repo.log_bot_message(GROUP, "text-only", "An answer.", thread_id=None)
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
    await app.handle(_say("iron man pls", reply_to="text-only", bot=True))
    assert "generate_image" not in _offered(provider.calls[0])
    _no_image(client, platform)


@pytest.mark.parametrize("text", ["iron man pls", "use this face", "what is this?"])
async def test_editing_disabled_never_treats_a_photo_as_edit_context(text):
    app, platform, provider, client, _ = _build([TEXT], edit=False)
    photo = _photo()
    await app.handle(photo)
    await app.handle(_say(text, reply_to=photo.message_id))
    assert "generate_image" not in _offered(provider.calls[0])
    _no_image(client, platform)


async def test_photo_does_not_override_a_denied_user_gate(monkeypatch):
    async def denied(access, ctx):
        return False

    monkeypatch.setattr("chatcore_image.gates.may_generate", denied)
    app, platform, provider, client, _ = _build([TEXT])
    await app.handle(_photo(text="@testbot use this face", mention=True))
    assert len(provider.calls) == 1
    assert "generate_image" not in _offered(provider.calls[0])
    _no_image(client, platform)


async def test_new_photo_replying_to_a_text_answer_does_not_gain_the_earlier_output():
    app, platform, provider, client, _ = _build([_image_call(1), DONE, _image_call(1, "c2"), DONE])
    await app.handle(_photo(text="@testbot make this photo a watercolour", mention=True))
    output = platform.sent_photos[-1]
    answer = platform.sent[-1]
    platform.files["photo-2"] = NEW_JPEG
    fresh = replace(
        message(
            "@testbot use this face",
            sender=ALICE,
            mention=True,
            attachments=[Attachment("photo-2", AttachmentKind.PHOTO)],
        ),
        reply_to=answer.ref,
        reply_to_bot=True,
    )
    await app.handle(fresh)
    request = provider.calls[2]
    assert "generate_image" in _offered(request)
    stored_output = await app.stores.repo.get_message(GROUP, output.ref.message_id)
    output_ids = {item.id for item in stored_output.attachments}
    assert output_ids
    assert client.images.edit.await_count == 2
    # Reference 1 is the photo sent now; the generated output is not a reference at all.
    assert client.images.edit.await_args.kwargs["image"][1] == NEW_JPEG
    seen = [part.data for part in request["messages"][-1].content if isinstance(part, ImagePart)]
    assert seen[0] == NEW_JPEG
    assert output.data not in seen
    assert output.data != JPEG
    assert "Usable photo reference numbers this turn: 1." in request["system"]


async def test_new_photo_replying_to_a_plain_text_answer_is_offered_with_only_that_photo():
    # No earlier image turn, so nothing is inherited: the photo alone makes the skill active.
    app, platform, provider, client, _ = _build([_image_call(1), DONE])
    await app.stores.repo.log_bot_message(GROUP, "text-only", "An answer.", thread_id=None)
    platform.files["photo-2"] = NEW_JPEG
    await app.handle(
        replace(
            message(
                "@testbot use this face",
                sender=ALICE,
                mention=True,
                attachments=[Attachment("photo-2", AttachmentKind.PHOTO)],
            ),
            reply_to=MessageRef(GROUP, "text-only"),
            reply_to_bot=True,
        )
    )
    request = provider.calls[0]
    assert "generate_image" in _offered(request)
    assert "Usable photo reference numbers this turn: 1." in request["system"]
    client.images.edit.assert_awaited_once()
    assert client.images.edit.await_args.kwargs["image"][1] == NEW_JPEG
    client.images.generate.assert_not_awaited()


async def test_inherited_skill_without_a_photo_asks_for_one_instead_of_substituting():
    app, platform, provider, client, _ = _build([_image_call(1), DONE, TEXT])
    await app.handle(_photo(text="@testbot make this photo a watercolour", mention=True))
    assert "Usable photo reference numbers this turn: 1." in provider.calls[0]["system"]
    await app.handle(_say("now make it anime"))
    follow_up = provider.calls[2]
    assert "generate_image" in _offered(follow_up)  # inherited, requester-local
    assert "No usable photo is attached or directly replied to" in follow_up["system"]
    assert "text-only substitute" in follow_up["system"]
    assert client.images.edit.await_count == 1
    client.images.generate.assert_not_awaited()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("quota", "Image limit reached for today."),
        ("busy", "Image generation is busy"),
        ("refused", "refused this prompt"),
        ("unavailable", "unavailable right now"),
    ],
)
async def test_real_failures_reach_the_model_as_their_own_distinct_result(code, expected):
    app, platform, provider, client, plugin = _build(
        [_image_call(1), _image_call(1, "call-2"), DONE]
    )
    plugin.service.generate = AsyncMock(side_effect=ImageError(code))
    photo = _photo()
    await app.handle(photo)
    await app.handle(_say("iron man pls", reply_to=photo.message_id))
    results = [item.text for item in provider.calls[-1]["messages"] if item.role == "tool"]
    assert expected in results[0]
    assert sum(expected in text for text in results) == 1
    assert "An image was already attempted this turn." in results[1]
    plugin.service.generate.assert_awaited_once()
    assert platform.sent_photos == []
