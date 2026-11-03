import os
from datetime import UTC, datetime
from importlib import resources
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.events import ChatRef
from chatcore.skills import GateState, SkillSet, render_hint
from chatcore.store import migrations
from chatcore.store.pg import PgDatabase
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_jobs.models import Scope
from chatcore_jobs.plugin import JobsPlugin
from chatcore_jobs.repository import JobKind
from chatcore_jobs.settings import JobsSettings


class SettingsRepo:
    def __init__(self):
        self.rows = {}

    async def settings(self, scope):
        return dict(
            self.rows.get(
                (scope.chat_key, scope.is_dm),
                {
                    "reminders_enabled": False,
                    "watches_enabled": False,
                },
            )
        )

    async def configure(self, scope, kind, enabled):
        row = await self.settings(scope)
        row[f"{kind}_enabled"] = enabled
        self.rows[scope.chat_key, scope.is_dm] = row
        return row


class AdminPlatform(FakePlatform):
    async def is_group_admin(self, chat, actor):
        return False


@pytest.fixture
async def setup():
    actor = user()
    access = AccessPolicy([GROUP.key], [], owners=[actor.ref.key])
    platform = AdminPlatform()
    plugin = JobsPlugin(
        JobsSettings(), access, platform, sender=AsyncMock(), clock=lambda: datetime.now(UTC)
    )
    stores = memory_stores()
    await plugin.on_startup(stores)
    stores.db = object()
    repo = SettingsRepo()
    plugin.service.repo = lambda _stores: repo
    registry = ToolRegistry(plugin.tools())
    skills = SkillSet.build([plugin], registry)
    ctx = ToolContext(GROUP, actor, False, platform, stores, thread_id="thread-a", turn=TurnState())
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[plugin],
        bot_name="bot",
        group_mentions_only=True,
    )
    return plugin, repo, skills, ctx, app, access, actor


@pytest.mark.asyncio
async def test_reminders_off_keeps_list_cancel_and_reads_toggle_across_threads(setup):
    plugin, repo, skills, ctx, app, _access, _actor = setup
    availability = await skills.evaluate_skill("reminders", ctx)
    assert availability.state is GateState.AVAILABLE
    assert availability.offered == ("list_reminders", "cancel_reminder")
    assert (await skills.evaluate_tool("restore_reminder", ctx)).state is GateState.OFF
    assert len(availability.hints) == 1
    hint = render_hint(availability.hints[0], lambda name: app.command_form(name, is_dm=False))
    assert "/jobtools@bot reminders on" in hint
    await repo.configure(Scope(GROUP.key, False), "reminders", True)
    next_turn = ToolContext(
        GROUP, ctx.user, False, ctx.platform, ctx.stores, thread_id="thread-b", turn=TurnState()
    )
    updated = await skills.evaluate_skill("reminders", next_turn)
    assert updated.offered == (
        "create_reminder",
        "list_reminders",
        "cancel_reminder",
        "restore_reminder",
    )
    assert (await skills.evaluate_tool("restore_reminder", next_turn)).state is GateState.AVAILABLE
    assert not updated.hints


@pytest.mark.asyncio
async def test_setup_manager_can_request_jobs_configuration_but_member_and_dm_cannot(setup):
    _plugin, _repo, skills, ctx, _app, access, actor = setup
    gate = await skills.evaluate_tool("configure_jobs", ctx)
    assert gate.state is GateState.AVAILABLE
    access.owners = frozenset()
    member = await skills.evaluate_tool("configure_jobs", ctx)
    assert member.state is GateState.NOT_ALLOWED
    dm = ToolContext(
        ChatRef(actor.ref.platform, actor.ref.id),
        actor,
        True,
        ctx.platform,
        ctx.stores,
        turn=TurnState(),
    )
    assert (await skills.evaluate_tool("configure_jobs", dm)).state is GateState.NOT_ALLOWED


@pytest.mark.asyncio
async def test_watches_offer_plain_variant_when_rendered_fetch_is_off(setup):
    plugin, repo, _skills, _ctx, _app, _access, _actor = setup
    plugin.rendered_fetcher = object()
    plugin.rendered_allowed = lambda _scope: _false()
    assert (await _skills.evaluate_tool("restore_website_watch", _ctx)).state is GateState.OFF
    await repo.configure(Scope(GROUP.key, False), "watches", True)
    skill_set = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
    availability = await skill_set.evaluate_skill("watches", _ctx)
    assert "create_website_watch" in availability.offered
    assert (
        await skill_set.evaluate_tool("restore_website_watch", _ctx)
    ).state is GateState.AVAILABLE
    assert availability.variant_hints == (
        "Rendered watches are off in this chat; plain watches are available.",
    )


async def _false():
    return False


@pytest.mark.integration
@pytest.mark.skipif("TEST_PG_DSN" not in os.environ, reason="TEST_PG_DSN is not configured")
@pytest.mark.asyncio
async def test_settings_row_toggle_is_shared_across_threads():
    dsn = os.environ["TEST_PG_DSN"]
    try:
        admin = await asyncpg.connect(dsn, timeout=2)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres unavailable: {type(exc).__name__}")
    schema = f"job_gate_{uuid4().hex[:10]}"
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    db = PgDatabase(pool)
    try:
        await migrations.apply(db, [("chatcore_jobs", resources.files("chatcore_jobs.migrations"))])
        actor = user()
        access = AccessPolicy([GROUP.key], [], owners=[actor.ref.key])
        platform = AdminPlatform()
        plugin = JobsPlugin(JobsSettings(), access, platform, sender=AsyncMock())
        stores = memory_stores()
        await plugin.on_startup(stores)
        stores.db = db
        skill_set = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
        thread_a = ToolContext(GROUP, actor, False, platform, stores, thread_id="a")
        thread_b = ToolContext(GROUP, actor, False, platform, stores, thread_id="b")
        assert (await skill_set.evaluate_skill("reminders", thread_a)).offered == (
            "list_reminders",
            "cancel_reminder",
        )
        await plugin.service.repo(stores).configure(
            Scope(GROUP.key, False, "a"), kind=JobKind.REMINDER, enabled=True, expected_revision=0
        )
        assert "create_reminder" in (await skill_set.evaluate_skill("reminders", thread_b)).offered
    finally:
        await db.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
