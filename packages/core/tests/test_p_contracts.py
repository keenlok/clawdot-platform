from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.events import BotAddedToChat, ChatRef
from chatcore.platform import ChatPlatform
from chatcore.plugin import BasePlugin, Plugin
from chatcore.testing import FakePlatform, memory_stores
from chatcore_console import ConsolePlatform
from chatcore_telegram import TelegramPlatform


def test_phase_p_plugins_and_platform_pin_contracts():
    class P(BasePlugin):
        name = "p"

    assert isinstance(P(), Plugin)
    fake = FakePlatform()
    assert isinstance(fake, ChatPlatform)
    assert fake.capabilities.pin is True
    assert hasattr(fake, "pin")
    console = ConsolePlatform(bot_name="bot")
    assert console.capabilities.pin is False
    assert TelegramPlatform.capabilities.pin is True
    assert callable(getattr(TelegramPlatform, "pin", None))


async def test_bot_added_calls_plugin_hook_only_for_allowed_chat():
    class P(BasePlugin):
        name = "recording"

        def __init__(self):
            self.chats = []

        async def on_bot_added(self, chat, platform, stores):
            self.chats.append(chat)

    plugin = P()
    allowed = ChatRef("fake", "group")
    other = ChatRef("fake", "other")
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=AccessPolicy([allowed.key], []),
        plugins=[plugin],
        bot_name="bot",
    )
    await app.handle(BotAddedToChat(allowed))
    await app.handle(BotAddedToChat(other))
    assert plugin.chats == [allowed]
    assert platform.left == [other]
