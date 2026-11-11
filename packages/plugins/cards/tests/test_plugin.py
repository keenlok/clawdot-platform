from chatcore.access import AccessPolicy
from chatcore.cards import CardError
from chatcore.events import ChatRef, User, UserRef
from chatcore.testing import FakePlatform, memory_stores
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_cards import CardsPlugin, CardsSettings
from chatcore_cards.plugin import VISUAL_SUMMARY_INSTRUCTIONS


class Service:
    available = True

    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail
        self.pages = [b"png"]

    async def render(self, card, *, user_key):
        self.calls += 1
        if self.fail:
            raise CardError("busy")
        return self.pages

    async def aclose(self):
        pass


async def test_skill_and_handler_attempt_delivery_once():
    platform = FakePlatform("tg")
    service = Service()
    plugin = CardsPlugin(
        AccessPolicy(["tg:group"], []),
        platform,
        settings=CardsSettings(CARDS_ENABLED=True, CARDS_RENDER_URL="http://renderer"),
        service=service,
    )
    plugin.service.available = True
    assert plugin.skills()[0].max_steps == 6
    assert plugin.skills()[0].max_output_tokens == 12288
    assert len(VISUAL_SUMMARY_INSTRUCTIONS) <= 800
    ctx = ToolContext(
        ChatRef("tg", "group"),
        User(UserRef("tg", "user"), display_name="Alex"),
        False,
        platform,
        memory_stores(),
        turn=TurnState(),
    )
    tool = plugin.tools()[0]
    args = {"title": "Summary", "blocks": [], "caption": "Summary"}
    result = await ToolRegistry([tool]).call(tool.name, args, ctx)
    assert not result.is_error
    assert platform.sent_photos[0].caption == "📋 Summary"
    result = await ToolRegistry([tool]).call(tool.name, args, ctx)
    assert result.is_error
    assert service.calls == 1


async def test_failed_render_consumes_the_only_attempt_in_a_turn():
    platform = FakePlatform("tg")
    service = Service(fail=True)
    plugin = CardsPlugin(
        AccessPolicy(["tg:group"], []),
        platform,
        settings=CardsSettings(CARDS_ENABLED=True, CARDS_RENDER_URL="http://renderer"),
        service=service,
    )
    ctx = ToolContext(
        ChatRef("tg", "group"),
        User(UserRef("tg", "user"), display_name="Alex"),
        False,
        platform,
        memory_stores(),
        turn=TurnState(),
    )
    tool = plugin.tools()[0]
    args = {"title": "Summary", "blocks": [], "caption": "Summary"}
    assert (await ToolRegistry([tool]).call(tool.name, args, ctx)).is_error
    assert (await ToolRegistry([tool]).call(tool.name, args, ctx)).is_error
    assert service.calls == 1


async def test_invalid_card_or_caption_never_calls_renderer():
    platform, service = FakePlatform("tg"), Service()
    plugin = CardsPlugin(
        AccessPolicy(["tg:group"], []),
        platform,
        settings=CardsSettings(CARDS_ENABLED=True, CARDS_RENDER_URL="http://renderer"),
        service=service,
    )
    tool = plugin.tools()[0]
    for args in (
        {"title": "", "blocks": [], "caption": "Valid"},
        {"title": "Good", "blocks": [], "caption": "line\nbreak"},
        {"title": "Good", "blocks": [], "caption": "line\u2028break"},
        {"title": "Good", "blocks": [], "caption": "line\u2029break"},
    ):
        ctx = ToolContext(
            ChatRef("tg", "group"),
            User(UserRef("tg", "user"), display_name="Alex"),
            False,
            platform,
            memory_stores(),
            turn=TurnState(),
        )
        assert (await ToolRegistry([tool]).call(tool.name, args, ctx)).is_error
    assert service.calls == 0


async def test_permission_revoked_during_render_prevents_delivery(monkeypatch):
    from chatcore_cards import plugin as module

    decisions = iter([True, False])

    async def allowed(*args):
        return next(decisions)

    monkeypatch.setattr(module, "may_send_card", allowed)
    platform, service = FakePlatform("tg"), Service()
    plugin = CardsPlugin(
        AccessPolicy(["tg:group"], []),
        platform,
        settings=CardsSettings(CARDS_ENABLED=True, CARDS_RENDER_URL="http://renderer"),
        service=service,
    )
    ctx = ToolContext(
        ChatRef("tg", "group"),
        User(UserRef("tg", "user"), display_name="Alex"),
        False,
        platform,
        memory_stores(),
        turn=TurnState(),
    )
    tool = plugin.tools()[0]
    result = await ToolRegistry([tool]).call(
        tool.name, {"title": "Summary", "blocks": [], "caption": "Summary"}, ctx
    )
    assert result.content == "Card delivery is no longer allowed."
    assert not platform.sent_photos and not platform.sent_files


def test_tools_skills_and_gates_are_hidden_when_unavailable_or_undeliverable():
    class NoDelivery:
        name = "tg"

    class DisabledService:
        available = False

        async def aclose(self):
            pass

    access = AccessPolicy(["tg:group"], [])
    configured = CardsSettings(CARDS_ENABLED=True, CARDS_RENDER_URL="http://renderer")
    disabled = CardsPlugin(
        access, FakePlatform("tg"), settings=configured, service=DisabledService()
    )
    unsupported = CardsPlugin(access, NoDelivery(), settings=configured, service=Service())
    for plugin in (disabled, unsupported):
        assert plugin.tools() == []
        assert plugin.skills() == []
        assert plugin.tool_gates() == {}


async def test_photo_album_and_file_only_delivery_branches():
    access = AccessPolicy(["tg:group"], [])
    settings = CardsSettings(CARDS_ENABLED=True, CARDS_RENDER_URL="http://renderer")
    user = User(UserRef("tg", "user"), display_name="Alex")

    async def run(platform, pages):
        service = Service()
        service.pages = pages
        plugin = CardsPlugin(access, platform, settings=settings, service=service)
        ctx = ToolContext(
            ChatRef("tg", "group"), user, False, platform, memory_stores(), turn=TurnState()
        )
        tool = plugin.tools()[0]
        return await ToolRegistry([tool]).call(
            tool.name, {"title": "Summary", "blocks": [], "caption": "Caption"}, ctx
        ), service

    albums = FakePlatform("tg")
    result, service = await run(albums, [b"a", b"b"])
    assert not result.is_error and service.calls == 1
    assert albums.sent_albums[0][2] == "📋 Caption"

    class PhotoOnly:
        name = "tg"

        def __init__(self):
            self.photos = []

        async def send_photo(self, chat, filename, data, *, caption=None, thread_id=None):
            self.photos.append((filename, data, caption))

    photo_only = PhotoOnly()
    result, _ = await run(photo_only, [b"a", b"b"])
    assert not result.is_error
    assert [item[2] for item in photo_only.photos] == ["📋 Caption", None]

    class FileOnly:
        name = "tg"

        def __init__(self):
            self.files = []

        async def send_file(self, chat, filename, data, *, caption=None, thread_id=None):
            self.files.append((filename, data, caption))

    file_only = FileOnly()
    result, _ = await run(file_only, [b"a"])
    assert not result.is_error and file_only.files[0][2] == "📋 Caption"


def test_plugin_exports_resolve_and_default_off():
    import chatcore_cards
    from chatcore_cards import __all__

    assert all(hasattr(chatcore_cards, name) for name in __all__)
    plugin = CardsPlugin(AccessPolicy([], []), FakePlatform("tg"))
    assert plugin.tools() == []
