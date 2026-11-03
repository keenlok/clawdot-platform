from datetime import UTC, datetime

from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.command_prompts import CommandReply
from chatcore.display_time_plugin import DisplayTimePlugin
from chatcore.events import ChatRef
from chatcore.plugin import CommandContext
from chatcore.store.base import Stores
from chatcore.store.memory import MemoryCache, MemoryCoreRepo
from chatcore.testing import GROUP, FakePlatform, message, user
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState


class AdminPlatform(FakePlatform):
    def __init__(self, results: list[bool | Exception]):
        super().__init__()
        self.results = results
        self.lookups = 0

    async def is_group_admin(self, chat, user_ref):
        self.lookups += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_context(platform, *, person=None, chat=GROUP, is_dm=False):
    person = person or user()
    stores = Stores(MemoryCoreRepo(), MemoryCache())
    return CommandContext(chat, person, is_dm, False, platform, stores), stores


async def test_group_timezone_setting_and_reset_apply_immediately_after_fresh_admin_checks():
    platform = AdminPlatform([True, True])
    plugin = DisplayTimePlugin(clock=lambda: datetime(2026, 11, 1, 1, tzinfo=UTC))
    ctx, _ = make_context(platform)
    timezone = next(command.handler for command in plugin.commands() if command.name == "timezone")

    assert await timezone(ctx, "Asia/Singapore") == "Group display time zone set to Asia/Singapore."
    assert await timezone(ctx, "reset") == "Display time zone reset; using labelled UTC fallback."
    assert platform.lookups == 2


async def test_group_setting_fails_closed_for_non_admin_lookup_error_and_revocation():
    plugin = DisplayTimePlugin()
    timezone = next(command.handler for command in plugin.commands() if command.name == "timezone")
    for result in [False, RuntimeError("private platform details"), True, False]:
        platform = AdminPlatform([result])
        ctx, stores = make_context(platform)
        reply = await timezone(ctx, "Asia/Singapore")
        if result is True:
            assert reply == "Group display time zone set to Asia/Singapore."
        else:
            assert reply == "Only a current group administrator can change this group setting."
        assert (await plugin.service(stores).resolve(GROUP, ctx.user.ref, is_dm=False))[0] == (
            "Asia/Singapore" if result is True else "UTC"
        )


async def test_group_setting_fails_closed_when_adapter_namespace_does_not_match():
    platform = AdminPlatform([True])
    platform.name = "other"
    plugin = DisplayTimePlugin()
    ctx, stores = make_context(platform)
    timezone = next(command.handler for command in plugin.commands() if command.name == "timezone")

    assert await timezone(ctx, "Asia/Singapore") == (
        "Only a current group administrator can change this group setting."
    )
    assert platform.lookups == 0
    assert await plugin.service(stores).resolve(GROUP, ctx.user.ref, is_dm=False) == (
        "UTC",
        "fallback",
    )


async def test_group_admin_revocation_is_checked_again_before_reset():
    platform = AdminPlatform([True, False])
    plugin = DisplayTimePlugin()
    ctx, stores = make_context(platform)
    timezone = next(command.handler for command in plugin.commands() if command.name == "timezone")

    assert await timezone(ctx, "Asia/Singapore") == "Group display time zone set to Asia/Singapore."
    assert await timezone(ctx, "reset") == (
        "Only a current group administrator can change this group setting."
    )
    assert await plugin.service(stores).resolve(GROUP, ctx.user.ref, is_dm=False) == (
        "Asia/Singapore",
        "group",
    )


async def test_dm_setting_rejects_an_adapter_namespace_mismatch():
    plugin = DisplayTimePlugin()
    person = user()
    dm = ChatRef(person.ref.platform, person.ref.id)
    platform = FakePlatform()
    platform.name = "other"
    ctx, _ = make_context(platform, person=person, chat=dm, is_dm=True)
    timezone = next(command.handler for command in plugin.commands() if command.name == "timezone")

    assert await timezone(ctx, "Europe/London") == (
        "Personal settings can only be changed in your own DM."
    )


async def test_group_addressed_timezone_command_uses_normal_bot_routing_without_confirmation():
    platform = AdminPlatform([True])
    person = user()
    stores = Stores(MemoryCoreRepo(), MemoryCache())
    plugin = DisplayTimePlugin()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[plugin],
        bot_name="bot",
        group_mentions_only=True,
    )

    await app.handle(message("/timezone@bot Asia/Singapore", sender=person))

    assert platform.texts == ["Group display time zone set to Asia/Singapore."]
    assert len(platform.sent) == 1 and platform.sent[0].action_id is None


async def test_dm_settings_are_self_owned_and_explicit_time_zone_is_one_off():
    now = datetime(2026, 11, 1, 1, tzinfo=UTC)
    plugin = DisplayTimePlugin(clock=lambda: now)
    person = user()
    dm = ChatRef(person.ref.platform, person.ref.id)
    ctx, stores = make_context(FakePlatform(), person=person, chat=dm, is_dm=True)
    commands = {command.name: command.handler for command in plugin.commands()}

    assert await commands["timezone"](ctx, "Europe/London") == (
        "Your personal display time zone set to Europe/London."
    )
    assert await commands["time"](ctx, "Asia/Singapore") == (
        "2026-11-01 09:00:00 Asia/Singapore (UTC+08:00)"
    )
    assert await plugin.service(stores).resolve(dm, person.ref, is_dm=True) == (
        "Europe/London",
        "personal",
    )

    bad_ctx, _ = make_context(
        FakePlatform(), person=person, chat=ChatRef(person.ref.platform, "someone-else"), is_dm=True
    )
    assert await commands["timezone"](bad_ctx, "Europe/London") == (
        "Personal settings can only be changed in your own DM."
    )


async def test_read_time_tools_are_read_only_and_group_context_never_discloses_dm_preference():
    plugin = DisplayTimePlugin(clock=lambda: datetime(2026, 11, 1, 1, tzinfo=UTC))
    person = user()
    dm = ChatRef(person.ref.platform, person.ref.id)
    stores = Stores(MemoryCoreRepo(), MemoryCache())
    service = plugin.service(stores)
    await service.set_zone(dm, person.ref, is_dm=True, zone="Europe/London")
    group_ctx = ToolContext(GROUP, person, False, FakePlatform(), stores)
    tools = {tool.name: tool for tool in plugin.tools()}

    current = await tools["get_current_time"].handler(group_ctx, time_zone="Asia/Singapore")
    effective = await tools["get_display_time_zone"].handler(group_ctx)

    assert current.content == "2026-11-01 09:00:00 Asia/Singapore (UTC+08:00)"
    assert effective.content == "UTC (labelled fallback)"
    assert all(not tools[name].writes for name in ("get_current_time", "get_display_time_zone"))
    assert not tools["set_display_time_zone"].writes
    assert not tools["reset_display_time_zone"].writes
    assert "explicit request" in tools["set_display_time_zone"].description
    assert "Reset" in tools["reset_display_time_zone"].description
    assert "get_current_time" in tools["reset_display_time_zone"].description


def tool_context(platform, stores, *, person=None, chat=GROUP, is_dm=False, turn=None):
    person = person or user()
    return ToolContext(
        chat,
        person,
        is_dm,
        platform,
        stores,
        turn=turn or TurnState(),
    )


async def test_tool_set_and_reset_use_fresh_group_admin_checks_and_apply_immediately():
    plugin = DisplayTimePlugin()
    platform = AdminPlatform([True, True])
    ctx, stores = make_context(platform)
    tool_ctx = tool_context(platform, stores, person=ctx.user, chat=ctx.chat)
    tools = ToolRegistry(plugin.tools())

    result = await tools.call("set_display_time_zone", {"time_zone": "Asia/Singapore"}, tool_ctx)
    assert result.content == "Group display time zone set to Asia/Singapore."
    assert not result.is_error and result.pending_action is None
    assert not plugin.tools()[2].writes
    assert platform.lookups == 1
    assert await plugin.service(stores).resolve(GROUP, ctx.user.ref, is_dm=False) == (
        "Asia/Singapore",
        "group",
    )

    reset = await tools.call("reset_display_time_zone", {}, tool_ctx)
    assert reset.content == "Display time zone reset; using labelled UTC fallback."
    assert not reset.is_error and reset.pending_action is None
    assert platform.lookups == 2
    assert await plugin.service(stores).resolve(GROUP, ctx.user.ref, is_dm=False) == (
        "UTC",
        "fallback",
    )


async def test_tool_mutations_fail_closed_for_non_admin_and_lookup_error():
    plugin = DisplayTimePlugin()
    for response in (False, RuntimeError("private admin error")):
        platform = AdminPlatform([response])
        ctx, stores = make_context(platform)
        tool_ctx = tool_context(platform, stores, person=ctx.user, chat=ctx.chat)
        result = await ToolRegistry(plugin.tools()).call(
            "set_display_time_zone", {"time_zone": "Asia/Singapore"}, tool_ctx
        )

        assert result.is_error and result.pending_action is None
        assert "current group administrator" in result.content
        assert await plugin.service(stores).resolve(GROUP, ctx.user.ref, is_dm=False) == (
            "UTC",
            "fallback",
        )


async def test_tool_reset_checks_revoked_group_admin_role_again():
    plugin = DisplayTimePlugin()
    platform = AdminPlatform([True, False])
    ctx, stores = make_context(platform)
    tool_ctx = tool_context(platform, stores, person=ctx.user, chat=ctx.chat)
    tools = ToolRegistry(plugin.tools())
    await tools.call("set_display_time_zone", {"time_zone": "Asia/Singapore"}, tool_ctx)

    result = await tools.call("reset_display_time_zone", {}, tool_ctx)

    assert result.is_error and result.pending_action is None
    assert platform.lookups == 2
    assert await plugin.service(stores).resolve(GROUP, ctx.user.ref, is_dm=False) == (
        "Asia/Singapore",
        "group",
    )


async def test_tool_setting_is_self_owned_in_dm_and_rejects_wrong_dm():
    plugin = DisplayTimePlugin()
    person = user()
    dm = ChatRef(person.ref.platform, person.ref.id)
    platform = FakePlatform()
    _, stores = make_context(platform, person=person, chat=dm, is_dm=True)
    registry = ToolRegistry(plugin.tools())

    saved = await registry.call(
        "set_display_time_zone",
        {"time_zone": "Europe/London"},
        tool_context(platform, stores, person=person, chat=dm, is_dm=True),
    )
    assert saved.content == "Your personal display time zone set to Europe/London."
    assert not saved.is_error and saved.pending_action is None
    assert await plugin.service(stores).resolve(dm, person.ref, is_dm=True) == (
        "Europe/London",
        "personal",
    )

    foreign = await registry.call(
        "set_display_time_zone",
        {"time_zone": "Asia/Tokyo"},
        tool_context(
            platform,
            stores,
            person=person,
            chat=ChatRef(person.ref.platform, "other"),
            is_dm=True,
        ),
    )
    assert foreign.is_error and foreign.pending_action is None
    assert await plugin.service(stores).resolve(dm, person.ref, is_dm=True) == (
        "Europe/London",
        "personal",
    )


async def test_tool_setting_rejects_invalid_zone_and_arbitrary_scope_args():
    plugin = DisplayTimePlugin()
    platform = AdminPlatform([True])
    ctx, stores = make_context(platform)
    tool_ctx = tool_context(platform, stores, person=ctx.user, chat=ctx.chat)
    registry = ToolRegistry(plugin.tools())

    invalid = await registry.call("set_display_time_zone", {"time_zone": "EST"}, tool_ctx)
    foreign_scope = await registry.call(
        "set_display_time_zone",
        {"time_zone": "Asia/Tokyo", "chat_key": "fake:other"},
        tool_ctx,
    )

    assert invalid.is_error and "installed IANA" in invalid.content
    assert foreign_scope.is_error and "chat_key" in foreign_scope.content
    assert platform.lookups == 0
    assert await plugin.service(stores).resolve(GROUP, ctx.user.ref, is_dm=False) == (
        "UTC",
        "fallback",
    )


async def test_tool_mutations_after_untrusted_reads_require_a_separate_message():
    plugin = DisplayTimePlugin()
    platform = AdminPlatform([True])
    ctx, stores = make_context(platform)
    turn = TurnState(read_untrusted=True)
    tool_ctx = tool_context(platform, stores, person=ctx.user, chat=ctx.chat, turn=turn)

    result = await ToolRegistry(plugin.tools()).call(
        "set_display_time_zone", {"time_zone": "Asia/Singapore"}, tool_ctx
    )

    assert result.is_error and result.pending_action is None
    assert "separate message" in result.content
    assert platform.lookups == 0
    assert await plugin.service(stores).resolve(GROUP, ctx.user.ref, is_dm=False) == (
        "UTC",
        "fallback",
    )


async def test_invalid_zone_is_rejected_without_setting():
    plugin = DisplayTimePlugin()
    person = user()
    dm = ChatRef(person.ref.platform, person.ref.id)
    ctx, stores = make_context(FakePlatform(), person=person, chat=dm, is_dm=True)
    timezone = next(command.handler for command in plugin.commands() if command.name == "timezone")

    result = await timezone(ctx, "PST")
    assert isinstance(result, CommandReply) and result.awaits_reply
    assert "installed IANA" in result.text and "Reply to this message" in result.text
    assert await plugin.service(stores).resolve(dm, person.ref, is_dm=True) == ("UTC", "fallback")


async def test_privacy_hooks_delete_personal_preferences_and_chat_purge_deletes_shared_setting():
    plugin = DisplayTimePlugin()
    person = user()
    dm = ChatRef(person.ref.platform, person.ref.id)
    stores = Stores(MemoryCoreRepo(), MemoryCache())
    service = plugin.service(stores)
    await service.set_zone(dm, person.ref, is_dm=True, zone="Europe/London")
    await service.set_zone(GROUP, person.ref, is_dm=False, zone="Asia/Singapore")

    await plugin.purge_user(dm, person.ref, stores)
    assert await service.resolve(dm, person.ref, is_dm=True) == ("UTC", "fallback")
    assert await service.resolve(GROUP, person.ref, is_dm=False) == ("Asia/Singapore", "group")

    await plugin.purge_chat(GROUP, stores)
    assert await service.resolve(GROUP, person.ref, is_dm=False) == ("UTC", "fallback")
