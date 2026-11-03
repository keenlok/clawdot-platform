from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from chatcore.access import AccessPolicy
from chatcore.events import ChatRef, User, UserRef
from chatcore.plugin import CommandContext
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_jobs.plugin import JobsPlugin
from chatcore_jobs.settings import JobsSettings


class Platform:
    name = "tg"
    capabilities = SimpleNamespace(markup="plain", max_message_len=4096)

    async def is_group_admin(self, chat, user):
        return True


class Repo:
    async def settings(self, scope):
        return {
            "reminders_enabled": True,
            "watches_enabled": True,
            "max_jobs_per_user": 10,
            "max_jobs_per_chat": 100,
            "minimum_interval_seconds": 300,
            "revision": 2,
        }

    async def list_jobs(self, scope, actor, kind, *, all_members=False):
        return []


@pytest.fixture
async def plugin_ctx():
    platform = Platform()
    plugin = JobsPlugin(
        JobsSettings(),
        AccessPolicy(["tg:-100"], []),
        platform,
        sender=AsyncMock(),
        clock=lambda: datetime(2026, 10, 31, tzinfo=UTC),
    )
    plugin.service.repo = lambda _stores: Repo()
    user = User(UserRef("tg", "7"), "Ada")
    stores = SimpleNamespace(db=object())
    tools_ctx = ToolContext(ChatRef("tg", "-100"), user, False, platform, stores, "7", TurnState())
    confirm = SimpleNamespace(request=AsyncMock())
    command_ctx = CommandContext(
        ChatRef("tg", "-100"), user, False, False, platform, stores, "7", confirm=confirm
    )
    return plugin, tools_ctx, command_ctx, confirm


@pytest.mark.asyncio
async def test_only_job_actions_are_direct_writes_and_admin_role_is_separate(plugin_ctx):
    plugin, ctx, _, _ = plugin_ctx
    tools = {tool.name: tool for tool in plugin.tools()}
    assert all(
        tools[name].writes
        for name in (
            "create_reminder",
            "cancel_reminder",
            "create_website_watch",
            "cancel_website_watch",
            "restore_reminder",
            "restore_website_watch",
            "configure_jobs",
        )
    )
    direct = {name for name, tool in tools.items() if tool.direct}
    assert direct == {
        "create_reminder",
        "cancel_reminder",
        "restore_reminder",
        "create_website_watch",
        "cancel_website_watch",
        "restore_website_watch",
    }
    assert tools["configure_jobs"].admin_only is False
    registry = ToolRegistry(tools.values())
    result = await registry.call(
        "create_reminder",
        {
            "text": "call mom",
            "local_time": "2026-11-01T09:00:00",
            "timezone": "Asia/Singapore",
        },
        ctx,
    )
    assert result.pending_action is not None
    assert result.pending_action.thread_id == "7"


@pytest.mark.asyncio
async def test_watch_tool_rejects_unseen_url_and_bool_interval(plugin_ctx):
    plugin, ctx, _, _ = plugin_ctx
    tools = {tool.name: tool for tool in plugin.tools()}
    result = await tools["create_website_watch"].handler(
        ctx, url="https://example.com", interval_seconds=300
    )
    assert result.is_error
    ctx.turn.seen_urls.add("https://example.com")
    result = await tools["create_website_watch"].handler(
        ctx, url="https://example.com", interval_seconds=True
    )
    assert result.is_error


@pytest.mark.asyncio
async def test_commands_are_registered_and_write_commands_request_confirmation(plugin_ctx):
    plugin, _, ctx, confirm = plugin_ctx
    commands = {command.name: command for command in plugin.commands()}
    assert set(commands) == {
        "remind",
        "reminders",
        "cancelreminder",
        "watch",
        "watchrendered",
        "watches",
        "cancelwatch",
        "jobtools",
    }
    assert (
        await commands["remind"].handler(ctx, "2026-11-01T09:00:00 Asia/Singapore call mom") is None
    )
    confirm.request.assert_awaited_once()
    action = confirm.request.await_args.args[0]
    assert action.thread_id == "7"
    assert action.payload["scope"]["thread_id"] == "7"


@pytest.mark.asyncio
async def test_jobtools_rejects_unknown_or_duplicate_settings(plugin_ctx):
    plugin, _, ctx, confirm = plugin_ctx
    command = {item.name: item for item in plugin.commands()}["jobtools"]
    assert "unique" in await command.handler(ctx, "watches on admin=true")
    assert "unique" in await command.handler(
        ctx, "watches on max_jobs_per_chat=2 max_jobs_per_chat=3"
    )
    assert not confirm.request.await_count


@pytest.mark.asyncio
async def test_rendered_authorization_suppresses_when_group_disables_browse(plugin_ctx):
    from chatcore_jobs.models import Actor, Job, JobKind, JobStatus, Scope

    plugin, _, _, _ = plugin_ctx

    class BrowseOff:
        async def __call__(self, _scope):
            return False

    async def rendered_fetch(*_args):
        return None

    plugin.rendered_allowed = BrowseOff()
    plugin.rendered_fetcher = rendered_fetch

    class JobsRepoFake:
        async def settings(self, _scope):
            return {
                "reminders_enabled": True,
                "watches_enabled": True,
                "minimum_interval_seconds": 300,
            }

    plugin.service.rendered_allowed = plugin.rendered_allowed
    plugin.service.rendered_available = True
    watched = Job(
        id=uuid4(),
        scope=Scope("tg:-100", False, "7"),
        owner=Actor("tg:7"),
        kind=JobKind.WATCH,
        status=JobStatus.ACTIVE,
        payload={"url": "https://example.com", "interval_seconds": 3600, "mode": "rendered"},
        created_at=datetime(2026, 10, 31, tzinfo=UTC),
        next_run_at=datetime(2026, 10, 31, tzinfo=UTC),
    )
    assert not await plugin._authorize_job(watched, JobsRepoFake())


@pytest.mark.asyncio
async def test_rendered_authorization_suppresses_when_interval_window_is_empty(plugin_ctx):
    from chatcore_jobs.models import Actor, Job, JobKind, JobStatus, Scope

    plugin, _, _, _ = plugin_ctx

    async def rendered_allowed(_scope):
        return True

    async def rendered_fetch(*_args):
        return None

    class JobsRepoFake:
        async def settings(self, _scope):
            return {
                "reminders_enabled": True,
                "watches_enabled": True,
                "minimum_interval_seconds": 300,
            }

    plugin.settings = JobsSettings(maximum_watch_interval_seconds=600)
    plugin.rendered_allowed = rendered_allowed
    plugin.rendered_fetcher = rendered_fetch
    watched = Job(
        id=uuid4(),
        scope=Scope("tg:-100", False, "7"),
        owner=Actor("tg:7"),
        kind=JobKind.WATCH,
        status=JobStatus.ACTIVE,
        payload={"url": "https://example.com", "interval_seconds": 600, "mode": "rendered"},
        created_at=datetime(2026, 10, 31, tzinfo=UTC),
        next_run_at=datetime(2026, 10, 31, tzinfo=UTC),
    )
    assert not await plugin._authorize_job(watched, JobsRepoFake())


@pytest.mark.asyncio
async def test_bare_cancel_and_jobtools_ask_for_input(plugin_ctx):
    plugin, _, ctx, confirm = plugin_ctx
    commands = {command.name: command for command in plugin.commands()}
    assert await commands["cancelreminder"].handler(ctx, "") == (
        "You have no active reminders to cancel."
    )
    assert await commands["cancelwatch"].handler(ctx, " ") == (
        "You have no active watches to cancel."
    )
    asked = await commands["jobtools"].handler(ctx, "")
    assert asked.awaits_reply and "reminders off" in asked.text
    assert not confirm.request.await_count
