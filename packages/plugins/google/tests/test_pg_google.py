"""Disposable-schema Postgres checks; no live Google or chat calls."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from importlib import resources
from uuid import uuid4

import asyncpg
import pytest
from chatcore.store import migrations
from chatcore.store.pg import PgDatabase
from chatcore_google.repository import GoogleRepository

pytestmark = pytest.mark.integration
DSN = os.environ.get("TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test")


@pytest.fixture
async def google_db():
    try:
        admin = await asyncpg.connect(DSN, timeout=2)
    except (OSError, TimeoutError, asyncpg.PostgresError):
        pytest.skip("Local development Postgres is unavailable")
    schema = "google_" + uuid4().hex[:12]
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = None
    try:
        pool = await asyncpg.create_pool(
            DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        db = PgDatabase(pool)
        await migrations.apply(db, [("google", resources.files("chatcore_google.migrations"))])
        yield db
    finally:
        if pool:
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_settings_and_reservations_survive_new_repository_and_reject_replay(google_db):
    repo = GoogleRepository(google_db)
    assert await repo.settings("fake:a") == (True, 0)
    await repo.configure("fake:a", False, 0)
    restarted = GoogleRepository(google_db)
    assert await restarted.settings("fake:a") == (False, 1)
    assert await restarted.settings("fake:b") == (True, 0)
    with pytest.raises(ValueError):
        await restarted.reserve("fake:a", "old", "fake:user", 1, 10, 0)
    await restarted.configure("fake:a", True, 1)
    await restarted.reserve("fake:a", "once", "fake:user", 1, 10, 2)
    with pytest.raises(ValueError, match="handled|uncertain"):
        await GoogleRepository(google_db).reserve("fake:a", "once", "fake:user", 1, 10, 2)


async def test_concurrent_daily_quota_is_atomic_and_scoped(google_db):
    repo = GoogleRepository(google_db)
    results = await asyncio.gather(
        *[repo.reserve("fake:a", f"action-{i}", "fake:user", 2, 3, 0) for i in range(2)],
        return_exceptions=True,
    )
    assert sum(isinstance(value, ValueError) for value in results) == 1
    await repo.reserve("fake:b", "other", "fake:user", 3, 3, 0)
    assert await google_db.fetchval("SELECT count(*) FROM google_actions") == 2


async def test_links_and_privacy_hooks_cannot_cross_chat_or_user(google_db):
    repo = GoogleRepository(google_db)
    await repo.link("fake:a", "file", "trip-a", "fake:alice")
    await repo.link("fake:b", "file", "trip-b", "fake:alice")
    await repo.reserve("fake:a", "a", "fake:alice", 1, 5, 0)
    await repo.reserve("fake:a", "b", "fake:bob", 1, 5, 0)
    assert await repo.linked_trip("fake:a", "file") == "trip-a"
    await repo.purge("fake:a", "fake:alice")
    assert await repo.linked_trip("fake:a", "file") is None
    assert await repo.linked_trip("fake:b", "file") == "trip-b"
    assert await google_db.fetchval("SELECT count(*) FROM google_actions") == 1
    await repo.purge_older_than(datetime.now(UTC) + timedelta(seconds=1))
    assert await google_db.fetchval("SELECT count(*) FROM google_actions") == 0
    await repo.purge("fake:b")
    assert await google_db.fetchval("SELECT count(*) FROM google_itinerary_links") == 0
