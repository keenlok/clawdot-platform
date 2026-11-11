from unittest.mock import AsyncMock

from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.events import ChatRef, User, UserRef
from chatcore.platform import PlatformError
from chatcore.skill_router import SkillRouter
from chatcore.skills import SkillSet
from chatcore.testing import FakePlatform, memory_stores, message, user
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_image import ImagePlugin, ImageSettings
from chatcore_image.plugin import IMAGE_GENERATION_INSTRUCTIONS
from chatcore_image.service import ImageError
from pydantic import SecretStr


def enabled():
    return ImageSettings(IMAGE_ENABLED=True, IMAGE_OPENAI_API_KEY=SecretStr("fake"))


def context(platform, *, turn=None):
    return ToolContext(
        ChatRef(platform.name, "group"),
        User(UserRef(platform.name, "alice"), display_name="Alice"),
        False,
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
        "generate_image", {"prompt": "valid", "size": "square", "title": "Title"}, ctx
    )
    assert "already attempted" in again.content
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
    for text in ("draw me a cat", "画一只狗"):
        result = await router.route(text)
        assert [hit.skill for hit in result.hits] == ["image_generation"]
    for text in ("推荐一部动画", "who won the lucky draw", "withdraw 50 dollars"):
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
