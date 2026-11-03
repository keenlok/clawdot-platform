import os
from datetime import UTC, datetime
from importlib import resources
from uuid import uuid4

import asyncpg
import pytest
from chatcore.events import ChatRef
from chatcore.skills import GateState, SkillSet
from chatcore.store import migrations
from chatcore.store.pg import PgDatabase
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_polls.models import Scope
from chatcore_polls.plugin import PollsPlugin
from chatcore_polls.settings import PollsSettings


class SettingsRepo:
    def __init__(self):
        self.rows = {}

    async def settings(self, scope):
        return {"polls_enabled": self.rows.get((scope.chat_key, scope.is_dm), False)}

    async def configure(self, scope, enabled):
        self.rows[scope.chat_key, scope.is_dm] = enabled


@pytest.mark.asyncio
async def test_poll_gate_distinguishes_dm_off_and_group_toggle():
    plugin = PollsPlugin(PollsSettings(), object(), FakePlatform(), clock=lambda: datetime.now(UTC))
    stores = memory_stores()
    stores.db = object()
    await plugin.on_startup(stores)
    repo = SettingsRepo()
    plugin.service.repo = lambda _stores: repo
    skills = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
    actor = user()
    group_a = ToolContext(GROUP, actor, False, plugin.platform, stores, turn=TurnState())
    dm_chat = ChatRef(actor.ref.platform, actor.ref.id)
    dm = ToolContext(dm_chat, actor, True, plugin.platform, stores, turn=TurnState())

    dm_gate = await skills.evaluate_skill("polls", dm)
    assert dm_gate.state is GateState.NOT_ALLOWED
    assert dm_gate.hints == ("Polls only work in groups.",)
    # Polls off: listing and closing existing polls still work; creating is hinted.
    off = await skills.evaluate_skill("polls", group_a)
    assert off.state is GateState.AVAILABLE
    assert off.offered == ("list_polls", "close_poll")
    assert off.hints == (
        "Polls are off in this group. A group setup manager or admin can turn them on: they can "
        "ask me to turn on polls, or send {cmd:polltools} on.",
    )

    await repo.configure(Scope(GROUP.key, False), True)
    on = await skills.evaluate_skill("polls", group_a)
    assert on.state is GateState.AVAILABLE
    assert on.offered == ("create_poll", "list_polls", "close_poll")
    assert on.hints == ()
    group_b = ChatRef(GROUP.platform, "other-group")
    other = ToolContext(group_b, actor, False, plugin.platform, stores, turn=TurnState())
    assert "create_poll" not in (await skills.evaluate_skill("polls", other)).offered


@pytest.mark.asyncio
async def test_configure_polls_gate_requires_group_and_manager():
    class Access:
        def __init__(self, managers):
            self.managers = managers

        async def refresh(self):
            return None

        def can_setup(self, actor, chat):
            return actor.key in self.managers

    actor = user()
    plugin = PollsPlugin(
        PollsSettings(), Access({actor.ref.key}), FakePlatform(), clock=lambda: datetime.now(UTC)
    )
    stores = memory_stores()
    skills = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
    group = ToolContext(GROUP, actor, False, plugin.platform, stores, turn=TurnState())
    dm = ToolContext(
        ChatRef(actor.ref.platform, actor.ref.id), actor, True, plugin.platform, stores
    )
    assert (await skills.evaluate_tool("configure_polls", group)).state is GateState.AVAILABLE
    assert (await skills.evaluate_tool("configure_polls", dm)).state is GateState.NOT_ALLOWED
    plugin.access.managers = set()
    assert (await skills.evaluate_tool("configure_polls", group)).state is GateState.NOT_ALLOWED


@pytest.mark.integration
@pytest.mark.skipif("TEST_PG_DSN" not in os.environ, reason="TEST_PG_DSN is not configured")
@pytest.mark.asyncio
async def test_poll_settings_row_toggle_is_scoped_by_group():
    dsn = os.environ["TEST_PG_DSN"]
    try:
        admin = await asyncpg.connect(dsn, timeout=2)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres unavailable: {type(exc).__name__}")
    schema = f"poll_gate_{uuid4().hex[:10]}"
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    db = PgDatabase(pool)
    try:
        await migrations.apply(
            db, [("chatcore_polls", resources.files("chatcore_polls.migrations"))]
        )
        plugin = PollsPlugin(
            PollsSettings(), object(), FakePlatform(), clock=lambda: datetime.now(UTC)
        )
        stores = memory_stores()
        await plugin.on_startup(stores)
        stores.db = db
        skill_set = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
        actor = user()
        scope_a = Scope(GROUP.key, False, "a")
        scope_b = Scope(GROUP.platform + ":other", False, "b")
        await plugin.service.repo(stores).configure(scope_a, True, 0)
        ctx_a = ToolContext(GROUP, actor, False, plugin.platform, stores, thread_id="b")
        ctx_b = ToolContext(ChatRef.parse(scope_b.chat_key), actor, False, plugin.platform, stores)
        assert "create_poll" in (await skill_set.evaluate_skill("polls", ctx_a)).offered
        assert "create_poll" not in (await skill_set.evaluate_skill("polls", ctx_b)).offered
    finally:
        await db.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
