"""Image plugins must pass the real Telegram photo checks, not only FakePlatform."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from chatcore.access import AccessPolicy
from chatcore.events import Attachment, AttachmentKind, ChatRef, User, UserRef
from chatcore.testing import memory_stores
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_image import ImagePlugin, ImageSettings
from chatcore_image.service import MAX_IMAGE_BYTES
from chatcore_telegram import MAX_PHOTO_BYTES, TelegramPlatform, TelegramSettings
from pydantic import SecretStr


async def test_maximum_image_is_sent_through_telegram_photo_method():
    bot = AsyncMock()
    bot.send_photo.return_value = SimpleNamespace(message_id=5)
    platform = TelegramPlatform(TelegramSettings(bot_token=SecretStr("fake-token")))
    platform._application = SimpleNamespace(bot=bot)
    plugin = ImagePlugin(
        AccessPolicy(["tg:group"], []),
        platform,
        settings=ImageSettings(IMAGE_ENABLED=True, IMAGE_OPENAI_API_KEY=SecretStr("fake")),
    )
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x03\xe8\x00\x00\x03\xe8" + b"x" * (
        MAX_IMAGE_BYTES - 24
    )

    async def generate(*args, **kwargs):
        return png

    plugin.service.generate = generate
    context = ToolContext(
        ChatRef("tg", "group"),
        User(UserRef("tg", "alice"), display_name="Alice"),
        False,
        platform,
        memory_stores(),
        turn=TurnState(),
    )
    try:
        tool = plugin.tools()[0]
        result = await ToolRegistry([tool]).call(
            tool.name, {"prompt": "A cat", "size": "square", "title": "Cat"}, context
        )
        assert not result.is_error, result.content
        bot.send_photo.assert_awaited_once()
        assert bot.send_photo.await_args.kwargs["caption"] == "🖼 Cat"
    finally:
        await plugin.aclose()


async def test_edit_is_sent_through_telegram_photo_method():
    bot = AsyncMock()
    bot.send_photo.return_value = SimpleNamespace(message_id=6)
    platform = TelegramPlatform(TelegramSettings(bot_token=SecretStr("fake-token")))
    platform._application = SimpleNamespace(bot=bot)
    plugin = ImagePlugin(
        AccessPolicy(["tg:group"], []),
        platform,
        settings=ImageSettings(
            IMAGE_ENABLED=True,
            IMAGE_EDIT_ENABLED=True,
            IMAGE_OPENAI_API_KEY=SecretStr("fake"),
        ),
    )
    # A PNG header with 1000x1000 IHDR dimensions, which Telegram send_photo validates.
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x03\xe8\x00\x00\x03\xe8" + b"image"
    plugin.service.generate = AsyncMock(return_value=png)
    attachment = Attachment("photo", AttachmentKind.PHOTO)
    context = ToolContext(
        ChatRef("tg", "group"),
        User(UserRef("tg", "alice"), display_name="Alice"),
        False,
        platform,
        memory_stores(),
        turn=TurnState(
            attachments=(attachment,),
            attachment_owners=("tg:alice",),
            downloads={"photo": b"\xff\xd8\xffjpeg"},
        ),
    )
    try:
        tool = plugin.tools()[0]
        result = await ToolRegistry([tool]).call(
            tool.name,
            {"prompt": "watercolour", "size": "square", "title": "Cat", "reference": 1},
            context,
        )
        assert not result.is_error, result.content
        bot.send_photo.assert_awaited_once()
        assert bot.send_photo.await_args.kwargs["caption"] == "🖼 Cat"
    finally:
        await plugin.aclose()


def test_image_byte_cap_fits_telegram_photo_cap():
    assert MAX_IMAGE_BYTES <= MAX_PHOTO_BYTES
