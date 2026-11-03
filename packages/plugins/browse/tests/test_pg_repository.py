"""Postgres integration coverage; skipped when TEST_PG_DSN is unavailable."""

import os
import uuid
from importlib import resources

import asyncpg
import pytest
from chatcore.store import migrations
from chatcore.store.pg import PgDatabase
from chatcore_browse.repository import BrowseConflict, BrowseRepo
from chatcore_browse.settings import BrowseSettings
from chatcore_jobs.models import Scope

pytestmark = pytest.mark.integration
DSN = os.environ.get("TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test")


@pytest.fixture
async def db():
    try:
        admin = await asyncpg.connect(DSN, timeout=1)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres unavailable: {type(exc).__name__}")
    schema = f"browse_{uuid.uuid4().hex[:10]}"
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        DSN, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    database = PgDatabase(pool)
    try:
        await migrations.apply(
            database,
            [
                (
                    "chatcore_browse",
                    resources.files("chatcore_browse.migrations"),
                )
            ],
        )
        yield database
    finally:
        await database.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_settings_revision_isolation_and_chat_purge(db):
    repo = BrowseRepo(db, BrowseSettings())
    first, other = Scope("tg:-100", False), Scope("tg:-200", False)
    assert (await repo.settings(first))["rendered_enabled"] is True
    result = await repo.configure(first, enabled=True, actor="tg:7", expected_revision=0)
    assert result["rendered_enabled"] is True
    assert result["revision"] == 1
    await repo.configure(other, enabled=True, actor="tg:7", expected_revision=0)
    assert (await repo.settings(other))["rendered_enabled"] is True
    with pytest.raises(BrowseConflict):
        await repo.configure(first, enabled=False, actor="tg:7", expected_revision=0)
    await repo.purge_user(first.chat_key, "tg:7")
    assert (await repo.settings(first))["updated_by"] is None
    assert (await repo.settings(other))["updated_by"] == "tg:7"
    await repo.purge_chat(first.chat_key)
    assert (await repo.settings(first))["revision"] == 0


async def test_explicit_group_optout_persists_and_new_groups_default_on(db):
    repo = BrowseRepo(db, BrowseSettings())
    scope = Scope("tg:optout", False)
    result = await repo.configure(scope, enabled=False, actor="tg:7", expected_revision=0)
    assert result["rendered_enabled"] is False
    assert result["revision"] == 1
    assert (await repo.settings(scope))["rendered_enabled"] is False
    assert (await repo.settings(Scope("tg:new", False)))["rendered_enabled"] is True
