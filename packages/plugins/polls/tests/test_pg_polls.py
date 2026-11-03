"""Postgres integration tests; a disposable schema, never live chat APIs."""

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from uuid import uuid4

import asyncpg
import pytest
from chatcore.store import migrations
from chatcore.store.pg import PgDatabase
from chatcore.testing import user
from chatcore_polls.models import PollDraft, Scope
from chatcore_polls.repository import PollConflict, PollsRepo
from chatcore_polls.settings import PollsSettings

pytestmark = pytest.mark.integration
DSN = os.environ.get("TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test")
NOW = datetime(2026, 10, 31, tzinfo=UTC)
A = Scope("fake:a", False, "7")
B = Scope("fake:b", False, "7")
DRAFT = PollDraft("Dinner?", ("Ramen", "Tacos"), close_at=NOW)


@pytest.fixture
async def db():
    try:
        admin = await asyncpg.connect(DSN, timeout=2)
    except (OSError, asyncpg.PostgresError, TimeoutError):
        pytest.skip("Postgres is unavailable")
    schema = f"polls_{uuid4().hex[:10]}"
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    database = None
    try:
        pool = await asyncpg.create_pool(
            DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        database = PgDatabase(pool)
        await migrations.apply(database, [("polls", resources.files("chatcore_polls.migrations"))])
        yield database
    finally:
        if database:
            await database.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def prepare(db, scope=A, settings=None):
    repo = PollsRepo(db, settings or PollsSettings())
    await repo.configure(scope, True, 0)
    return repo


async def opened(repo, scope=A, actor=None, action=None, draft=DRAFT):
    poll, created = await repo.reserve(scope, actor or user(), draft, action, NOW)
    assert created
    await repo.mark_open(scope, poll.id, str(poll.number), f"p-{poll.id}")
    return await repo.get_by_number(scope, poll.number)


async def test_migration_and_numbering_under_concurrency(db):
    repo = await prepare(db, settings=PollsSettings(max_open_per_user=50))
    rows = await asyncio.gather(*(repo.reserve(A, user(), DRAFT, None, NOW) for _ in range(10)))
    assert sorted(p.number for p, _ in rows) == list(range(1, 11))
    assert len(await db.fetch("SELECT * FROM polls")) == 10


@pytest.mark.parametrize("per_chat,per_user", [(1, 5), (20, 1)])
async def test_quotas_are_atomic_across_topics(db, per_chat, per_user):
    repo = await prepare(
        db, settings=PollsSettings(max_open_per_chat=per_chat, max_open_per_user=per_user)
    )
    outcomes = await asyncio.gather(
        repo.reserve(A, user(), DRAFT, None, NOW),
        repo.reserve(replace(A, thread_id="8"), user(), DRAFT, None, NOW),
        return_exceptions=True,
    )
    assert sum(isinstance(o, PollConflict) for o in outcomes) == 1
    assert await db.fetchval("SELECT count(*) FROM polls") == 1


async def test_source_action_id_is_idempotent_but_cannot_cross_topic_or_creator(db):
    repo = await prepare(db)
    one, two = await asyncio.gather(
        *(repo.reserve(A, user(), DRAFT, "same", NOW) for _ in range(2))
    )
    assert one[0].id == two[0].id and sorted([one[1], two[1]]) == [False, True]
    for scope, actor in [(replace(A, thread_id="8"), user()), (A, user("bob"))]:
        with pytest.raises(PollConflict):
            await repo.reserve(scope, actor, DRAFT, "same", NOW)


async def test_scope_isolation_lookup_and_mutation(db):
    repo = await prepare(db)
    await repo.configure(B, True, 0)
    poll = await opened(repo)
    other = await opened(repo, B)
    assert [p.id for p in await repo.list_open(A)] == [poll.id]
    assert [p.id for p in await repo.list_open(B)] == [other.id]
    topic = replace(A, thread_id="8")
    assert await repo.list_open(topic) == []
    assert await repo.get_by_number(topic, poll.number) is None
    assert await repo.get_by_message(B, poll.platform_message_id) == other
    assert await repo.get_by_message(topic, poll.platform_message_id) is None
    await repo.mark_failed(B, poll.id)
    assert (await repo.get_by_number(A, poll.number)).status == "open"
    assert not await repo.replace_votes(replace(poll, scope=B), user(), (0,), NOW)
    assert await repo.votes(A, poll.id) == []
    assert await repo.claim_close(poll.id, NOW, 1, scope=B) == []


async def test_platform_lookup_only_matches_open_same_platform(db):
    repo = await prepare(db)
    poll = await opened(repo)
    assert await repo.find_open_by_platform_poll("fake", poll.platform_poll_id) == poll
    assert await repo.find_open_by_platform_poll("tg", poll.platform_poll_id) is None
    claim = (await repo.claim_close(poll.id, NOW, 1, scope=A))[0]
    await repo.finish_close(A, poll.id, claim.lease_token, (1, 0), 1, NOW)
    assert await repo.find_open_by_platform_poll("fake", poll.platform_poll_id) is None


async def test_replace_votes_is_atomic_revote_and_retraction(db):
    repo = await prepare(db)
    poll = await opened(repo, draft=replace(DRAFT, multiple=True))
    await repo.replace_votes(poll, user(), (0, 1), NOW)
    assert len(await repo.votes(A, poll.id)) == 2
    await repo.replace_votes(poll, user(), (1,), NOW)
    assert [v["option_index"] for v in await repo.votes(A, poll.id)] == [1]
    with pytest.raises(ValueError):
        await repo.replace_votes(poll, user(), (0, 9), NOW)
    assert [v["option_index"] for v in await repo.votes(A, poll.id)] == [1]
    assert await repo.votes(B, poll.id) == []
    await repo.replace_votes(poll, user(), (), NOW)
    assert await repo.votes(A, poll.id) == []


async def test_close_claim_concurrency_lease_expiry_and_fencing(db):
    repo = await prepare(db)
    poll = await opened(repo)
    claims = await asyncio.gather(*(repo.claim_close(None, NOW, 5) for _ in range(2)))
    assert sorted(map(len, claims)) == [0, 1]
    claim = next(c[0] for c in claims if c)
    assert await repo.claim_close(None, NOW + timedelta(seconds=59), 5) == []
    new = (await repo.claim_close(None, NOW + timedelta(seconds=60), 5))[0]
    assert new.lease_token != claim.lease_token
    assert not await repo.finish_close(A, poll.id, claim.lease_token, (1, 0), 1, NOW)
    assert await repo.record_close_failure(A, poll.id, claim.lease_token) is None
    assert await repo.finish_close(A, poll.id, new.lease_token, (1, 0), 1, NOW)
    assert not await repo.finish_close(A, poll.id, new.lease_token, (1, 0), 1, NOW)


async def test_future_manual_claim_and_failure_counter(db):
    repo = await prepare(db)
    poll = await opened(repo, draft=replace(DRAFT, close_at=NOW + timedelta(days=1)))
    assert await repo.claim_close(None, NOW, 5) == []
    claim = (await repo.claim_close(poll.id, NOW, 1, scope=A))[0]
    assert await repo.record_close_failure(A, poll.id, claim.lease_token) == 1
    assert await repo.claim_close(poll.id, NOW, 1, scope=A) == []


async def test_settings_revision_is_fenced_at_reservation(db):
    repo = await prepare(db)
    await repo.configure(A, False, 1)
    with pytest.raises(PollConflict):
        await repo.configure(A, True, 1)
    with pytest.raises(PollConflict):
        await repo.reserve(A, user(), DRAFT, None, NOW, expected_revision=1)
    assert await repo.list_open(A) == []


async def test_all_privacy_hooks_are_scoped_and_keep_active_polls(db):
    repo = await prepare(db)
    await repo.configure(B, True, 0)
    mine = await opened(repo)
    other = await opened(repo, actor=user("bob"))
    foreign = await opened(repo, B)
    await repo.replace_votes(other, user(), (0,), NOW)
    await repo.replace_votes(foreign, user(), (0,), NOW)
    await repo.purge_user(A.chat_key, user().ref.key)
    assert await repo.get_by_number(A, mine.number) is None
    assert await repo.votes(A, other.id) == []
    assert len(await repo.votes(B, foreign.id)) == 1
    claim = (await repo.claim_close(other.id, NOW, 1, scope=A))[0]
    await repo.finish_close(A, other.id, claim.lease_token, None, None, NOW)
    failed, _ = await repo.reserve(A, user(), DRAFT, None, NOW)
    await repo.mark_failed(A, failed.id)
    await repo.purge_older_than(NOW + timedelta(days=1))
    assert await repo.get_by_number(A, other.number) is None
    assert await repo.get_by_number(A, failed.number) is None
    assert await repo.get_by_number(B, foreign.number) is not None
    await repo.purge_chat(B.chat_key)
    assert await repo.list_open(B) == []
    assert not (await repo.settings(B))["polls_enabled"]
    assert (await repo.settings(A))["polls_enabled"]


async def test_missing_settings_default_off_and_manual_failure_is_due_after_lease(db):
    repo = PollsRepo(db, PollsSettings())
    assert await repo.settings(A) == {"polls_enabled": False, "revision": 0}
    with pytest.raises(PollConflict):
        await repo.reserve(A, user(), DRAFT, None, NOW)
    await repo.configure(A, True, 0)
    poll = await opened(repo, draft=replace(DRAFT, close_at=None))
    claim = (await repo.claim_close(poll.id, NOW, 1, scope=A))[0]
    await repo.record_close_failure(A, poll.id, claim.lease_token)
    assert await repo.claim_close(None, NOW + timedelta(seconds=59), 5) == []
    assert len(await repo.claim_close(None, NOW + timedelta(seconds=60), 5)) == 1


async def test_close_scan_filters_allowed_chats_before_limit(db):
    repo = await prepare(db)
    await repo.configure(B, True, 0)
    for i in range(5):
        await opened(
            repo, B, actor=user(str(i)), draft=replace(DRAFT, close_at=NOW - timedelta(days=1))
        )
    allowed = await opened(repo)
    claims = await repo.claim_close(None, NOW, 1, allowed_chat_keys=[A.chat_key])
    assert [poll.id for poll in claims] == [allowed.id]
    assert await repo.claim_close(None, NOW, 5, allowed_chat_keys=[]) == []


@pytest.mark.parametrize("per_chat,per_user", [(1, 5), (20, 1)])
@pytest.mark.parametrize("fresh_age_seconds", [0, 600])
async def test_stale_sending_sweep_fails_old_reservations_and_frees_quota(
    db, per_chat, per_user, fresh_age_seconds
):
    repo = await prepare(
        db, settings=PollsSettings(max_open_per_chat=per_chat, max_open_per_user=per_user)
    )
    await repo.configure(B, True, 0)
    old, _ = await repo.reserve(A, user(), DRAFT, "abandoned", NOW - timedelta(seconds=601))
    fresh, _ = await repo.reserve(
        B, user(), DRAFT, None, NOW - timedelta(seconds=fresh_age_seconds)
    )
    with pytest.raises(PollConflict):
        await repo.reserve(A, user(), DRAFT, None, NOW)

    assert await repo.fail_stale_sending(NOW) == 1
    failed = await repo.get_by_number(A, old.number)
    assert failed.status == "failed" and failed.closed_at == NOW
    assert await repo.get_by_number(B, fresh.number) == fresh
    existing, created = await repo.reserve(A, user(), DRAFT, "abandoned", NOW)
    assert existing == failed and not created
    replacement, created = await repo.reserve(A, user(), DRAFT, None, NOW)
    assert created and replacement.status == "sending"
    assert await repo.fail_stale_sending(NOW) == 0


async def test_stale_sending_sweep_keeps_other_statuses_unchanged(db):
    repo = await prepare(db)
    poll = await opened(repo)
    assert await repo.fail_stale_sending(NOW + timedelta(minutes=11)) == 0
    assert await repo.get_by_number(A, poll.number) == poll
