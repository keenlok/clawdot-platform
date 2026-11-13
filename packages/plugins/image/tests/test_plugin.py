from unittest.mock import AsyncMock

import pytest
from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.events import Attachment, AttachmentKind, ChatRef, User, UserRef
from chatcore.images import ImageUnavailable
from chatcore.llm.types import ImagePart
from chatcore.platform import PlatformError
from chatcore.skill_router import SkillRouter
from chatcore.skills import SkillSet
from chatcore.testing import FakePlatform, memory_stores, message, user
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_image import ImagePlugin, ImageSettings
from chatcore_image.plugin import IMAGE_EDIT_INSTRUCTIONS, IMAGE_GENERATION_INSTRUCTIONS
from chatcore_image.service import ImageError
from pydantic import SecretStr


def enabled(**kwargs):
    return ImageSettings(IMAGE_ENABLED=True, IMAGE_OPENAI_API_KEY=SecretStr("fake"), **kwargs)


def context(platform, *, turn=None, is_dm=False):
    return ToolContext(
        ChatRef(platform.name, "group"),
        User(UserRef(platform.name, "alice"), display_name="Alice"),
        is_dm,
        platform,
        memory_stores(),
        turn=turn or TurnState(),
    )


async def test_offers_and_delivers_inline_photo():
    platform = FakePlatform()
    plugin = ImagePlugin(AccessPolicy(["fake:group"], []), platform, settings=enabled())
    plugin.service.generate = AsyncMock(return_value=b"png")
    assert len(plugin.tools()) == 1 and len(plugin.skills()) == 1
    result = await ToolRegistry(plugin.tools()).call(
        "generate_image", {"prompt": "A cat", "size": "square", "title": " Cat "}, context(platform)
    )
    assert not result.is_error
    assert platform.sent_photos[0].caption == "🖼 Cat"
    assert not platform.sent_files


async def test_edit_uses_requesters_photo_and_is_opt_in(monkeypatch):
    from chatcore_image import plugin as module

    platform = FakePlatform(files={"photo": b"\xff\xd8\xffjpeg"})
    base = ImagePlugin(AccessPolicy(["fake:group"], []), platform, settings=enabled())
    base.service.generate = AsyncMock(return_value=b"png")
    download = AsyncMock()
    monkeypatch.setattr(module, "download_image", download)
    assert "reference" not in base.tools()[0].parameters["properties"]
    denied = await base.tools()[0].handler(
        context(platform), prompt="change style", size="square", title="Cat", reference=1
    )
    assert denied.content == "Editing photos isn't enabled here."
    download.assert_not_awaited()
    base.service.generate.assert_not_awaited()
    assert base.skills()[0].instructions == IMAGE_GENERATION_INSTRUCTIONS

    plugin = ImagePlugin(
        AccessPolicy(["fake:group"], []),
        platform,
        settings=enabled(IMAGE_EDIT_ENABLED=True),
    )
    plugin.service.generate = AsyncMock(return_value=b"png")
    download.return_value = ImagePart(b"\xff\xd8\xffjpeg", "image/jpeg")
    attachment = Attachment("photo", AttachmentKind.PHOTO)
    turn = TurnState(attachments=(attachment,), attachment_owners=("fake:alice",))
    result = await ToolRegistry(plugin.tools()).call(
        "generate_image",
        {"prompt": "watercolour", "size": "square", "title": "Cat", "reference": 1},
        context(platform, turn=turn),
    )
    assert not result.is_error
    plugin.service.generate.assert_awaited_once()
    assert plugin.service.generate.await_args.kwargs["reference"].media_type == "image/jpeg"
    assert platform.sent_photos[-1].caption == "🖼 Cat"
    assert len(IMAGE_GENERATION_INSTRUCTIONS) < 800
    assert IMAGE_GENERATION_INSTRUCTIONS == (
        "Generate only when the person explicitly asks for an image. Write one self-contained "
        "prompt that restates the whole picture, resolving references from the conversation. "
        "Never put private individuals' names or personal details in the prompt. Call "
        "generate_image once, then reply in one or two sentences saying what was made. If it "
        "fails, say so and offer to adjust the description."
    )
    assert len(IMAGE_GENERATION_INSTRUCTIONS + " " + IMAGE_EDIT_INSTRUCTIONS) < 800
    assert (
        plugin.skills()[0].instructions
        == IMAGE_GENERATION_INSTRUCTIONS + " " + IMAGE_EDIT_INSTRUCTIONS
    )


def test_offered_text_is_unchanged_when_editing_is_off_and_points_to_reference_when_on():
    platform = FakePlatform()
    off = ImagePlugin(AccessPolicy(["fake:group"], []), platform, settings=enabled()).tools()[0]
    assert off.description == (
        "Create one requested image from a text description and send it to the chat."
    )
    assert set(off.parameters["properties"]) == {"prompt", "size", "title"}

    on = ImagePlugin(
        AccessPolicy(["fake:group"], []), platform, settings=enabled(IMAGE_EDIT_ENABLED=True)
    ).tools()[0]
    assert "changing a photo the person sent or replied to" in on.description
    assert "(pass reference)" in on.description
    reference = on.parameters["properties"]["reference"]["description"]
    assert reference.startswith("Pass this whenever the person asks to change")
    assert "a single photo is 1" in reference
    assert "call generate_image with reference" in IMAGE_EDIT_INSTRUCTIONS
    assert "Never say you can't use it" in IMAGE_EDIT_INSTRUCTIONS
    assert "Never use unrelated photos." in IMAGE_EDIT_INSTRUCTIONS
    assert "In groups, anyone may edit a directly replied-to photo." in IMAGE_EDIT_INSTRUCTIONS


async def test_group_rejects_unproven_other_members_photo_before_download(monkeypatch):
    from chatcore_image import plugin as module

    platform = FakePlatform()
    plugin = ImagePlugin(
        AccessPolicy(["fake:group"], []), platform, settings=enabled(IMAGE_EDIT_ENABLED=True)
    )
    plugin.service.generate = AsyncMock(return_value=b"png")
    download = AsyncMock()
    monkeypatch.setattr(module, "download_image", download)
    turn = TurnState(
        attachments=(Attachment("other", AttachmentKind.PHOTO),),
        attachment_owners=("fake:bob",),
    )
    result = await ToolRegistry(plugin.tools()).call(
        "generate_image",
        {"prompt": "style", "size": "square", "title": "Photo", "reference": 1},
        context(platform, turn=turn),
    )
    assert result.content == "In groups, reply directly to the photo you want to edit."
    download.assert_not_awaited()
    plugin.service.generate.assert_not_awaited()


async def test_dm_allows_reply_photo_owned_by_another_person(monkeypatch):
    from chatcore_image import plugin as module

    monkeypatch.setattr(module, "may_generate", AsyncMock(return_value=True))

    platform = FakePlatform()
    plugin = ImagePlugin(
        AccessPolicy(["fake:group"], []), platform, settings=enabled(IMAGE_EDIT_ENABLED=True)
    )
    plugin.service.generate = AsyncMock(return_value=b"png")
    part = ImagePart(b"\xff\xd8\xffjpeg", "image/jpeg")
    download = AsyncMock(return_value=part)
    monkeypatch.setattr(module, "download_image", download)
    turn = TurnState(
        attachments=(Attachment("reply", AttachmentKind.PHOTO),),
        attachment_owners=("fake:bob",),
    )
    result = await ToolRegistry(plugin.tools()).call(
        "generate_image",
        {"prompt": "style", "size": "square", "title": "Photo", "reference": 1},
        context(platform, turn=turn, is_dm=True),
    )
    assert not result.is_error
    download.assert_awaited_once()
    plugin.service.generate.assert_awaited_once()
    assert plugin.service.generate.await_args.kwargs["reference"] == part


@pytest.mark.parametrize(
    ("attachments", "owners", "reference", "expected"),
    [
        ((), (), 1, "There's no photo to use. Send it with your request or reply to it."),
        (
            (Attachment("a", AttachmentKind.PHOTO),),
            ("fake:alice",),
            0,
            "There are 1 images; pick 1–1.",
        ),
        (
            (Attachment("a", AttachmentKind.PHOTO), Attachment("b", AttachmentKind.PHOTO)),
            ("fake:alice", "fake:alice"),
            3,
            "There are 2 images; pick 1–2.",
        ),
        (
            (Attachment("a", AttachmentKind.PHOTO),),
            ("fake:alice",),
            True,
            "The photo number must be a whole number.",
        ),
        (
            (Attachment("a", AttachmentKind.PHOTO),),
            ("fake:alice",),
            "1",
            "The photo number must be a whole number.",
        ),
        (
            (Attachment("a", AttachmentKind.PHOTO),),
            ("fake:alice",),
            1.5,
            "The photo number must be a whole number.",
        ),
        (
            (Attachment("a", AttachmentKind.PHOTO),),
            (),
            1,
            "There's no photo to use. Send it with your request or reply to it.",
        ),
    ],
)
async def test_reference_refusals_do_not_download_or_generate(
    monkeypatch, attachments, owners, reference, expected
):
    from chatcore_image import plugin as module

    platform = FakePlatform()
    plugin = ImagePlugin(
        AccessPolicy(["fake:group"], []), platform, settings=enabled(IMAGE_EDIT_ENABLED=True)
    )
    plugin.service.generate = AsyncMock(return_value=b"png")
    download = AsyncMock()
    monkeypatch.setattr(module, "download_image", download)
    turn = TurnState(attachments=attachments, attachment_owners=owners)
    result = await ToolRegistry(plugin.tools()).call(
        "generate_image",
        {"prompt": "style", "size": "square", "title": "Photo", "reference": reference},
        context(platform, turn=turn),
    )
    assert result.content == expected
    download.assert_not_awaited()
    plugin.service.generate.assert_not_awaited()


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("too_large", "That photo is over 5 MB."),
        ("unsupported_format", "I can only use JPEG, PNG or WebP photos."),
        ("download_failed", "I couldn't download that photo. Please send it again."),
    ],
)
async def test_download_errors_do_not_call_service(monkeypatch, reason, expected):
    from chatcore_image import plugin as module

    platform = FakePlatform()
    plugin = ImagePlugin(
        AccessPolicy(["fake:group"], []), platform, settings=enabled(IMAGE_EDIT_ENABLED=True)
    )
    plugin.service.generate = AsyncMock(return_value=b"png")
    download = AsyncMock(side_effect=ImageUnavailable(reason))
    monkeypatch.setattr(module, "download_image", download)
    turn = TurnState(
        attachments=(Attachment("a", AttachmentKind.PHOTO),),
        attachment_owners=("fake:alice",),
    )
    result = await ToolRegistry(plugin.tools()).call(
        "generate_image",
        {"prompt": "style", "size": "square", "title": "Photo", "reference": 1},
        context(platform, turn=turn),
    )
    assert result.content == expected
    download.assert_awaited_once()
    plugin.service.generate.assert_not_awaited()


async def test_file_only_platform_delivers_document_and_no_capability_hides_tool():
    class FileOnly:
        name = "fake"
        send_file = FakePlatform.send_file

    platform = FileOnly()
    platform.sent_files = []
    platform._ids = iter(range(10))
    plugin = ImagePlugin(AccessPolicy(["fake:group"], []), platform, settings=enabled())
    plugin.service.generate = AsyncMock(return_value=b"png")
    result = await ToolRegistry(plugin.tools()).call(
        "generate_image", {"prompt": "A cat", "size": "square", "title": "Cat"}, context(platform)
    )
    assert not result.is_error and platform.sent_files[0].caption == "🖼 Cat"

    class Neither:
        name = "fake"

    assert not ImagePlugin(AccessPolicy(["fake:group"], []), Neither(), settings=enabled()).tools()


async def test_validation_and_once_per_turn_after_failure():
    platform = FakePlatform()
    plugin = ImagePlugin(AccessPolicy(["fake:group"], []), platform, settings=enabled())
    plugin.service.generate = AsyncMock(side_effect=ImageError("quota"))
    tool = plugin.tools()[0]
    turn = TurnState()
    ctx = context(platform, turn=turn)
    registry = ToolRegistry([tool])
    result = await registry.call(
        "generate_image", {"prompt": "\x01bad", "size": "square", "title": "Title"}, ctx
    )
    assert result.is_error and not plugin.service.generate.await_count
    result = await registry.call(
        "generate_image", {"prompt": "valid", "size": "square", "title": "Title"}, ctx
    )
    assert result.is_error and plugin.service.generate.await_count == 1
    again = await registry.call(
        "generate_image", {"prompt": "", "size": "square", "title": "Title"}, ctx
    )
    assert again.content == "An image was already attempted this turn."
    assert len(IMAGE_GENERATION_INSTRUCTIONS) < 800
    assert all(
        name in vars(__import__("chatcore_image")) for name in __import__("chatcore_image").__all__
    )


async def test_title_and_size_are_validated_without_generation():
    platform = FakePlatform()
    plugin = ImagePlugin(AccessPolicy(["fake:group"], []), platform, settings=enabled())
    plugin.service.generate = AsyncMock(return_value=b"png")
    tool = plugin.tools()[0]
    registry = ToolRegistry([tool])
    for title, size in (("bad\ntitle", "square"), ("x" * 121, "square"), ("Cat", "panorama")):
        result = await registry.call(
            "generate_image", {"prompt": "valid", "size": size, "title": title}, context(platform)
        )
        assert result.is_error
    assert not plugin.service.generate.await_count


async def test_non_string_tool_arguments_are_validation_errors():
    platform = FakePlatform()
    plugin = ImagePlugin(AccessPolicy(["fake:group"], []), platform, settings=enabled())
    plugin.service.generate = AsyncMock(return_value=b"png")
    registry = ToolRegistry(plugin.tools())
    for arguments in (
        {"prompt": None, "size": "square", "title": "Cat"},
        {"prompt": "cat", "size": "square", "title": 5},
        {"prompt": "cat", "size": [], "title": "Cat"},
    ):
        result = await registry.call("generate_image", arguments, context(platform))
        assert result.is_error
    assert not plugin.service.generate.await_count


async def test_initial_access_denial_skips_generation():
    platform = FakePlatform()
    plugin = ImagePlugin(AccessPolicy([], []), platform, settings=enabled())
    plugin.service.generate = AsyncMock(return_value=b"png")
    result = await ToolRegistry(plugin.tools()).call(
        "generate_image",
        {"prompt": "cat", "size": "square", "title": "Cat"},
        context(platform),
    )
    assert result.is_error
    assert not plugin.service.generate.await_count


async def test_photo_failure_does_not_fall_back_to_document():
    class PhotoFailure(FakePlatform):
        async def send_photo(self, *args, **kwargs):
            raise PlatformError("PhotoTooLarge")

    platform = PhotoFailure()
    plugin = ImagePlugin(AccessPolicy(["fake:group"], []), platform, settings=enabled())
    plugin.service.generate = AsyncMock(return_value=b"png")
    result = await ToolRegistry(plugin.tools()).call(
        "generate_image",
        {"prompt": "cat", "size": "square", "title": "Cat"},
        context(platform),
    )
    assert result.content == "Could not deliver the image."
    assert result.is_error
    assert not platform.sent_files


async def test_skill_keywords_avoid_unrelated_draw_and_chinese_requests():
    platform = FakePlatform()
    plugin = ImagePlugin(AccessPolicy(["fake:group"], []), platform, settings=enabled())
    skill = plugin.skills()[0]
    skillset = SkillSet(
        ToolRegistry(plugin.tools()), {skill.name: (skill, plugin.name)}, {}, {}, (), ()
    )
    router = SkillRouter(skillset, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05)
    for text in (
        "draw me a cat",
        "画一只狗",
        "make this photo a watercolour",
        "turn this into a cartoon",
        "把这张照片变成卡通",
    ):
        result = await router.route(text)
        assert [hit.skill for hit in result.hits] == ["image_generation"]
    for text in (
        "推荐一部动画",
        "who won the lucky draw",
        "withdraw 50 dollars",
        "make this shorter",
        "把会议改成明天",
        "this photo is nice",
    ):
        assert not (await router.route(text)).hits


async def test_permission_recheck_prevents_delivery(monkeypatch):
    from chatcore_image import plugin as module

    checks = iter([True, False])

    async def allowed(*_):
        return next(checks)

    monkeypatch.setattr(module, "may_generate", allowed)
    platform = FakePlatform()
    plugin = ImagePlugin(AccessPolicy(["fake:group"], []), platform, settings=enabled())
    plugin.service.generate = AsyncMock(return_value=b"png")
    result = await ToolRegistry(plugin.tools()).call(
        "generate_image", {"prompt": "cat", "size": "square", "title": "Cat"}, context(platform)
    )
    assert result.is_error and "no longer allowed" in result.content
    assert not platform.sent_photos


async def test_usage_command_dispatch_refuses_non_admin_and_reports_to_admin():
    platform = FakePlatform()
    access = AccessPolicy(["fake:group"], ["fake:admin"])
    plugin = ImagePlugin(access, platform, settings=enabled())
    plugin.service.report = AsyncMock(
        return_value={"total": 3, "daily_limit": 50, "model": "test-model", "quality": "high"}
    )
    command = plugin.commands()[0]
    assert command.name == "image" and command.admin_only
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[plugin],
        bot_name="testbot",
    )
    await app.handle(message("/image usage", sender=user("alice")))
    assert not platform.texts
    await app.handle(message("/image usage", sender=user("admin")))
    assert platform.texts == ["Images today (UTC): 3 of 50.\nModel: test-model, quality: high."]
    plugin.service.report.assert_awaited_once()
