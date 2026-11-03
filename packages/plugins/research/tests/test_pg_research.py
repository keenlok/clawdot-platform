"""Postgres durability checks; skipped automatically when the dev database is unavailable."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from importlib import resources

import asyncpg
import httpx
import pytest
import respx
from chatcore.access import AccessPolicy
from chatcore.events import ChatRef, UserRef
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message, ToolCall
from chatcore.store import migrations
from chatcore.store.pg import PgDatabase
from chatcore.testing import FakePlatform, ScriptedProvider, memory_stores
from chatcore_research.jobs import PgResearchJobs
from chatcore_research.plugin import ResearchPlugin
from chatcore_research.settings import ResearchSettings
from chatcore_research.worker import ResearchWorker
from chatcore_web import WebSettings

pytestmark = pytest.mark.integration
DSN = os.environ.get("TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test")


@pytest.fixture
async def research_db():
    try:
        admin = await asyncpg.connect(DSN, timeout=1)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("Postgres integration service is unavailable")
    schema = f"research_{uuid.uuid4().hex[:10]}"
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    db = PgDatabase(pool)
    try:
        await migrations.apply(
            db, [("chatcore_research", resources.files("chatcore_research.migrations"))]
        )
        yield db
    finally:
        await db.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _new_connection(db):
    schema = await db.fetchval("SELECT current_schema()")
    pool = await asyncpg.create_pool(
        DSN, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    return PgDatabase(pool)


async def test_pg_unique_owner_claim_fence_and_restart_lease(research_db):
    repo = PgResearchJobs(research_db)
    chat = ChatRef("fake", "group")
    alice = UserRef("fake", "alice")
    bob = UserRef("fake", "bob")
    job = await repo.create(chat, alice, "thread-a", "topic", (), is_dm=False)
    assert job is not None
    assert await repo.create(chat, alice, None, "second", (), is_dm=False) is None
    assert await repo.create(chat, bob, None, "other", (), is_dm=False) is not None
    now = datetime.now(UTC)
    first, second = await asyncio.gather(repo.claim(now, 2, 2), repo.claim(now, 2, 2))
    assert {first.owner.key, second.owner.key} == {alice.key, bob.key}

    stale = first if first.id == job.id else second
    await repo.add_tokens(stale, 7)
    changed = await repo.db.fetchrow("SELECT tokens_used FROM research_jobs WHERE id=$1", stale.id)
    assert changed["tokens_used"] == 7
    new_owner = await repo.claim(now + timedelta(seconds=3), 2, 2)
    assert new_owner is not None
    assert new_owner.id == stale.id
    await repo.add_tokens(stale, 100)
    unchanged = await repo.db.fetchrow(
        "SELECT tokens_used FROM research_jobs WHERE id=$1", stale.id
    )
    assert unchanged["tokens_used"] == 7
    assert ResearchSettings().max_attempts == 2


async def test_job_survives_worker_restart_and_reserved_send_is_not_replayed(research_db):
    now = datetime.now(UTC)
    owner = UserRef("fake", "restart-owner")
    chat = ChatRef("fake", "original-chat")
    first_repo = PgResearchJobs(research_db)
    created = await first_repo.create(chat, owner, "topic-9", "research topic", (), is_dm=False)
    first_claim = await first_repo.claim(now, 2, 2)
    assert first_claim.id == created.id
    assert first_claim.attempts == 1

    restarted_db = await _new_connection(research_db)
    try:
        restarted_repo = PgResearchJobs(restarted_db)
        second_claim = await restarted_repo.claim(now + timedelta(seconds=3), 2, 2)
        assert second_claim.id == created.id
        assert second_claim.attempts == 2
        assert second_claim.chat == chat
        assert second_claim.thread_id == "topic-9"
        assert await restarted_repo.reserve_delivery(second_claim)

        assert await restarted_repo.claim(now + timedelta(seconds=6), 2, 2) is None
        row = await research_db.fetchrow(
            "SELECT status,delivery_state FROM research_jobs WHERE id=$1", created.id
        )
        assert row["status"] == "completed"
        assert row["delivery_state"] == "unknown"
    finally:
        await restarted_db.close()


async def test_concurrent_claimers_cannot_claim_the_same_queued_row(research_db):
    repo = PgResearchJobs(research_db)
    job = await repo.create(
        ChatRef("fake", "one-claim"), UserRef("fake", "one-owner"), None, "topic", (), is_dm=False
    )

    first, second = await asyncio.gather(
        repo.claim(datetime.now(UTC), 60, 2), repo.claim(datetime.now(UTC), 60, 2)
    )

    assert sum(claim is not None for claim in (first, second)) == 1
    assert (first or second).id == job.id


async def test_release_and_finish_are_fenced_and_release_does_not_consume_attempt(research_db):
    repo = PgResearchJobs(research_db)
    created = await repo.create(
        ChatRef("fake", "release"), UserRef("fake", "release-owner"), None, "topic", (), is_dm=True
    )
    claimed = await repo.claim(datetime.now(UTC), 60, 2)

    assert claimed.is_dm is True
    assert await repo.release(claimed)
    released = await repo.db.fetchrow(
        "SELECT status,attempts FROM research_jobs WHERE id=$1", created.id
    )
    assert released["status"] == "queued"
    assert released["attempts"] == 0
    assert not await repo.finish(claimed, status="failed", delivery_state=None)
    reclaimed = await repo.claim(datetime.now(UTC), 60, 2)
    assert reclaimed.attempts == 1
    assert await repo.finish(reclaimed, status="completed", delivery_state="sent")


@respx.mock
async def test_restarted_plugin_worker_runs_research_and_delivers_to_original_thread(research_db):
    now = datetime.now(UTC)
    chat = ChatRef("fake", "restart-delivery")
    owner = UserRef("fake", "restart-delivery-owner")
    first_repo = PgResearchJobs(research_db)
    created = await first_repo.create(chat, owner, "topic-42", "long topic", (), is_dm=False)
    restarted_db = await _new_connection(research_db)
    settings = ResearchSettings(inline_timeout_s=10, long_timeout_s=60)
    first_started = asyncio.Event()
    gate = asyncio.Event()
    delivered = []

    class CrashedProvider:
        async def complete(self, *_args, **_kwargs):
            first_started.set()
            await gate.wait()

    first_stores = memory_stores()
    first_plugin = ResearchPlugin(
        settings,
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=CrashedProvider(),
        limits=Limits(first_stores.cache),
        access=AccessPolicy([chat.key], []),
        platform=FakePlatform(),
        sender=lambda *_args: None,
        jobs=first_repo,
        bot_name="test_bot",
        clock=lambda: now,
        lease_check_interval_s=60,
    )
    first_plugin.stores = first_stores
    first_worker = ResearchWorker(
        first_repo,
        first_plugin._run_job,
        settings=settings,
        clock=lambda: now,
    )
    killed = asyncio.create_task(first_worker.run_once())
    await first_started.wait()
    killed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await killed

    plugin = None
    worker = None
    try:
        restarted_repo = PgResearchJobs(restarted_db)
        stores = memory_stores()

        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Restart source",
                            "url": "https://restart.example/source",
                            "content": "confirmed",
                        }
                    ]
                },
            )
        )
        provider = ScriptedProvider(
            [
                Completion(
                    Message.assistant("", (ToolCall("s", "search_web", {"query": "restart"}),)),
                    "tool_use",
                ),
                Completion(Message.assistant("Restarted finding [S1]."), "end_turn"),
            ]
        )

        async def sender(chat_key, text, thread):
            delivered.append((chat_key, text, thread))
            return type("Sent", (), {"message_id": "message-1"})()

        plugin = ResearchPlugin(
            settings,
            web_settings=WebSettings(tavily_api_key="fake-key"),
            provider=provider,
            limits=Limits(stores.cache),
            access=AccessPolicy([chat.key], []),
            platform=FakePlatform(),
            sender=sender,
            jobs=restarted_repo,
            bot_name="test_bot",
            clock=lambda: now + timedelta(seconds=settings.lease_seconds + 1),
        )
        plugin.stores = stores
        worker = ResearchWorker(
            restarted_repo,
            plugin._run_job,
            settings=settings,
            clock=lambda: now + timedelta(seconds=settings.lease_seconds + 1),
        )

        assert await worker.run_once() == 1
        assert len(delivered) == 1
        assert delivered[0][0] == chat.key
        assert delivered[0][2] == "topic-42"
        assert delivered[0][1].startswith("Research: long topic")
        assert "Restarted finding [1]." in delivered[0][1]
        assert "1. Restart source — https://restart.example/source" in delivered[0][1]
        assert provider.calls
        row = await research_db.fetchrow(
            "SELECT attempts,status,delivery_state,report_message_id "
            "FROM research_jobs WHERE id=$1",
            created.id,
        )
        assert row["attempts"] == 2
        assert row["status"] == "completed"
        assert row["delivery_state"] == "sent"
        assert row["report_message_id"] == "message-1"
        assert row["attempts"] == 2
    finally:
        await first_worker.aclose()
        await first_plugin.aclose()
        if worker:
            await worker.aclose()
        if plugin:
            await plugin.aclose()
        await restarted_db.close()
