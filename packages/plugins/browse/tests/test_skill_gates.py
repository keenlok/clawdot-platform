import os
from importlib import resources
from uuid import uuid4

import asyncpg
import pytest
from chatcore.access import AccessPolicy
from chatcore.events import ChatRef
from chatcore.skills import GateState, SkillSet
from chatcore.store import migrations
from chatcore.store.pg import PgDatabase
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_browse.plugin import BrowsePlugin
from chatcore_browse.repository import BrowseRepo
from chatcore_browse.settings import BrowseSettings
from chatcore_jobs.models import Scope


class SettingsDb:
    def __init__(self):
        self.rows = {}
        self.reads = []

    async def fetchrow(self, _query, chat_key, is_dm):
        self.reads.append((chat_key, is_dm))
        return self.rows.get((chat_key, is_dm))


@pytest.mark.asyncio
async def test_rendered_pages_offers_search_link_and_configure_is_permission_gated():
    actor = user()
    access = AccessPolicy([GROUP.key], [], owners=[actor.ref.key])
    platform = FakePlatform()
    plugin = BrowsePlugin(
        BrowseSettings(BROWSER_RENDER_URL="https://renderer.invalid"),
        access,
        platform,
    )
    stores = memory_stores()
    db = SettingsDb()
    db.rows[(GROUP.key, False)] = {"rendered_enabled": False, "revision": 1}
    stores.db = db
    await plugin.on_startup(stores)
    skill_set = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
    ctx = ToolContext(GROUP, actor, False, platform, stores, turn=TurnState())

    availability = await skill_set.evaluate_skill("rendered_pages", ctx)
    assert availability.state is GateState.AVAILABLE
    assert availability.offered == ("build_search_link",)
    assert availability.hints == (
        "Rendered page reading is off here. A group setup manager or admin can send "
        "{cmd:browsetools} on.",
    )
    assert db.reads[-1] == (GROUP.key, False)
    assert (
        await skill_set.evaluate_tool("configure_rendered_fetch", ctx)
    ).state is GateState.AVAILABLE

    access.owners = frozenset()
    assert (
        await skill_set.evaluate_tool("configure_rendered_fetch", ctx)
    ).state is GateState.NOT_ALLOWED
    dm = ToolContext(ChatRef(actor.ref.platform, actor.ref.id), actor, True, platform, stores)
    assert (
        await skill_set.evaluate_tool("configure_rendered_fetch", dm)
    ).state is GateState.NOT_ALLOWED
    await skill_set.evaluate_skill("rendered_pages", dm)
    assert db.reads[-1] == (dm.chat.key, True)


@pytest.mark.asyncio
async def test_group_without_saved_setting_is_offered_rendered_fetch_by_default():
    actor = user()
    platform = FakePlatform()
    plugin = BrowsePlugin(
        BrowseSettings(BROWSER_RENDER_URL="https://renderer.invalid"),
        AccessPolicy([GROUP.key], []),
        platform,
    )
    stores = memory_stores()
    stores.db = SettingsDb()
    await plugin.on_startup(stores)
    skill_set = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
    ctx = ToolContext(GROUP, actor, False, platform, stores, turn=TurnState())

    availability = await skill_set.evaluate_skill("rendered_pages", ctx)
    assert "fetch_rendered_page" in availability.offered
    assert availability.hints == ()


@pytest.mark.integration
@pytest.mark.skipif("TEST_PG_DSN" not in os.environ, reason="TEST_PG_DSN is not configured")
@pytest.mark.asyncio
async def test_rendered_settings_row_is_shared_across_threads():
    dsn = os.environ["TEST_PG_DSN"]
    try:
        admin = await asyncpg.connect(dsn, timeout=2)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres unavailable: {type(exc).__name__}")
    schema = f"browse_gate_{uuid4().hex[:10]}"
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    db = PgDatabase(pool)
    try:
        await migrations.apply(
            db, [("chatcore_browse", resources.files("chatcore_browse.migrations"))]
        )
        actor = user()
        settings = BrowseSettings(BROWSER_RENDER_URL="https://renderer.invalid")
        platform = FakePlatform()
        plugin = BrowsePlugin(
            settings, AccessPolicy([GROUP.key], [], owners=[actor.ref.key]), platform
        )
        stores = memory_stores()
        await plugin.on_startup(stores)
        stores.db = db
        skill_set = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
        scope = Scope(GROUP.key, False)
        await BrowseRepo(db, settings).configure(
            scope, enabled=True, actor=actor.ref.key, expected_revision=0
        )
        ctx = ToolContext(GROUP, actor, False, platform, stores, thread_id="another")
        assert (
            "fetch_rendered_page" in (await skill_set.evaluate_skill("rendered_pages", ctx)).offered
        )
    finally:
        await db.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
