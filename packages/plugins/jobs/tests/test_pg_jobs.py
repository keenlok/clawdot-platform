"""Postgres integration coverage for durable job scopes and quota serialization."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from importlib import resources
from types import SimpleNamespace

import asyncpg
import pytest
from chatcore.access import AccessPolicy
from chatcore.events import ChatRef, User, UserRef
from chatcore.plugin import CommandContext
from chatcore.store import migrations
from chatcore.store.pg import PgDatabase
from chatcore.tools.registry import ToolContext
from chatcore_jobs.models import Actor, JobKind, Scope
from chatcore_jobs.repository import JobConflict, JobsRepo
from chatcore_jobs.service import JobsService
from chatcore_jobs.settings import JobsSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif("TEST_PG_DSN" not in os.environ, reason="TEST_PG_DSN is not configured"),
]
DSN = os.environ.get("TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test")


@pytest.fixture
async def db():
    schema = f"jobs_{uuid.uuid4().hex[:10]}"
    try:
        admin = await asyncpg.connect(DSN, timeout=2)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres is unavailable: {type(exc).__name__}")
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    database = PgDatabase(pool)
    try:
        await migrations.apply(
            database, [("chatcore_jobs", resources.files("chatcore_jobs.migrations"))]
        )
        yield database
    finally:
        await database.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_rendered_migration_index_and_active_count(db):
    index = await db.fetchval(
        "SELECT to_regclass('scheduled_jobs_active_rendered_watch_mode_idx') IS NOT NULL"
    )
    assert index is True
    settings = JobsSettings(max_rendered_watches_global=4)
    repo = JobsRepo(db, settings)
    scope = Scope("tg:-100", False)
    actor = Actor("tg:7")
    await db.execute(
        "INSERT INTO job_settings (chat_key,is_dm,watches_enabled) VALUES ($1,false,true)",
        scope.chat_key,
    )
    await repo.create(
        scope,
        actor,
        "rendered-active",
        JobKind.WATCH,
        {"url": "https://example.com", "interval_seconds": 3600, "mode": "rendered"},
        datetime.now(UTC),
        revision=0,
        now=datetime.now(UTC),
    )
    assert await repo.count_active_rendered_watches() == 1
    await db.execute(
        "UPDATE scheduled_jobs SET status='cancelled' WHERE source_action_id=$1",
        "rendered-active",
    )
    assert await repo.count_active_rendered_watches() == 0


async def test_global_rendered_cap_is_checked_inside_serialized_create(db):
    settings = JobsSettings(max_rendered_watches_global=1)
    repo = JobsRepo(db, settings)
    now = datetime.now(UTC)
    scopes = [Scope("tg:-101", False), Scope("tg:-102", False)]
    actor = Actor("tg:7")
    for scope in scopes:
        await db.execute(
            "INSERT INTO job_settings (chat_key,is_dm,watches_enabled) VALUES ($1,false,true)",
            scope.chat_key,
        )

    async def create(scope, action):
        try:
            await repo.create(
                scope,
                actor,
                action,
                JobKind.WATCH,
                {"url": "https://example.com", "interval_seconds": 3600, "mode": "rendered"},
                now,
                revision=0,
                now=now,
            )
            return True
        except JobConflict:
            return False

    outcomes = await asyncio.gather(
        create(scopes[0], "rendered-a"), create(scopes[1], "rendered-b")
    )
    assert sum(outcomes) == 1
    assert await repo.count_active_rendered_watches() == 1


def test_job_repo_requires_database():
    with pytest.raises(TypeError):
        JobsRepo(object())


async def test_jobs_are_scoped_by_chat_dm_owner_and_topic(db):
    settings = JobsSettings(dm_enabled=True)
    repo = JobsRepo(db, settings)
    group_a, group_b, dm = (
        Scope("tg:-100", False, "7"),
        Scope("tg:-200", False, "7"),
        Scope("tg:7", True),
    )
    alice, bob = Actor("tg:7"), Actor("tg:8")
    for scope in (group_a, group_b):
        await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    await repo.create(
        group_a,
        alice,
        "a1",
        JobKind.REMINDER,
        {"text": "secret"},
        datetime.now(UTC) + timedelta(hours=1),
        revision=1,
        now=datetime.now(UTC),
    )
    await repo.create(
        group_b,
        alice,
        "b1",
        JobKind.REMINDER,
        {"text": "other"},
        datetime.now(UTC) + timedelta(hours=1),
        revision=1,
        now=datetime.now(UTC),
    )
    await repo.create(
        dm,
        alice,
        "d1",
        JobKind.REMINDER,
        {"text": "dm"},
        datetime.now(UTC) + timedelta(hours=1),
        revision=0,
        now=datetime.now(UTC),
    )
    assert [j.payload["text"] for j in await repo.list_jobs(group_a, alice, JobKind.REMINDER)] == [
        "secret"
    ]
    assert await repo.list_jobs(Scope("tg:-100", False, "8"), alice, JobKind.REMINDER) == []
    assert await repo.list_jobs(group_a, bob, JobKind.REMINDER) == []
    foreign_id = (await repo.list_jobs(group_b, alice, JobKind.REMINDER))[0].id
    assert not await repo.cancel(group_a, alice, JobKind.REMINDER, foreign_id)
    assert (await repo.list_jobs(group_b, alice, JobKind.REMINDER))[0].status.value == "active"
    assert await repo.list_jobs(group_a, alice, JobKind.REMINDER) != await repo.list_jobs(
        dm, alice, JobKind.REMINDER
    )


async def test_remaining_quota_slot_serializes_confirmed_creations(db):
    repo = JobsRepo(db, JobsSettings(max_jobs_global=1))
    scope = Scope("tg:-100", False)
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    due = datetime.now(UTC) + timedelta(hours=1)

    async def create(action: str):
        try:
            return await repo.create(
                scope,
                Actor(f"tg:{action}"),
                action,
                JobKind.REMINDER,
                {"text": action},
                due,
                revision=1,
                now=datetime.now(UTC),
            )
        except JobConflict:
            return None

    results = await asyncio.gather(create("one"), create("two"))
    assert sum(result is not None for result in results) == 1


async def test_same_action_id_is_idempotent(db):
    repo = JobsRepo(db)
    scope = Scope("tg:-100", False)
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    due = datetime.now(UTC) + timedelta(hours=1)
    first = await repo.create(
        scope,
        Actor("tg:7"),
        "same",
        JobKind.REMINDER,
        {"text": "x"},
        due,
        revision=1,
        now=datetime.now(UTC),
    )
    second = await repo.create(
        scope,
        Actor("tg:7"),
        "same",
        JobKind.REMINDER,
        {"text": "x"},
        due,
        revision=1,
        now=datetime.now(UTC),
    )
    assert first.id == second.id


async def test_user_cancel_restore_is_scoped_and_clamps_next_run(db):
    repo = JobsRepo(db)
    scope = Scope("tg:-100", False, "7")
    actor = Actor("tg:7")
    other = Scope("tg:-200", False, "7")
    await repo.configure(scope, kind=JobKind.WATCH, enabled=True)
    await repo.configure(other, kind=JobKind.WATCH, enabled=True)
    now = datetime.now(UTC)
    job = await repo.create(
        scope,
        actor,
        "watch-to-restore",
        JobKind.WATCH,
        {"url": "https://example.com", "interval_seconds": 300},
        now - timedelta(hours=1),
        revision=1,
        now=now,
    )
    assert await repo.cancel(scope, actor, JobKind.WATCH, job.id, expected_revision=job.revision)
    cancelled = await repo.get_job(scope, job.id)
    assert cancelled.last_delivery_state == "cancelled_by_user"
    assert (
        await repo.restore(
            other,
            actor,
            JobKind.WATCH,
            job.id,
            all_members=False,
            expected_revision=cancelled.revision,
            revision=1,
            now=now,
        )
        is None
    )
    for wrong_scope, wrong_kind in (
        (Scope(scope.chat_key, False, "8"), JobKind.WATCH),
        (scope, JobKind.REMINDER),
    ):
        assert (
            await repo.restore(
                wrong_scope,
                actor,
                wrong_kind,
                job.id,
                all_members=False,
                expected_revision=cancelled.revision,
                revision=1,
                now=now,
            )
            is None
        )
    restored = await repo.restore(
        scope,
        actor,
        JobKind.WATCH,
        job.id,
        all_members=False,
        expected_revision=cancelled.revision,
        revision=1,
        now=now,
    )
    assert restored.status.value == "active"
    assert restored.next_run_at == now
    assert restored.last_delivery_state is None and restored.terminal_at is None


async def test_concurrent_restores_share_the_create_quota_lock(db):
    initial = JobsRepo(db, JobsSettings(max_jobs_global=2))
    limited = JobsRepo(db, JobsSettings(max_jobs_global=1))
    now = datetime.now(UTC)
    scopes = [Scope("tg:-301", False), Scope("tg:-302", False)]
    actor = Actor("tg:7")
    cancelled = []
    for index, scope in enumerate(scopes):
        await initial.configure(scope, kind=JobKind.WATCH, enabled=True)
        job = await initial.create(
            scope,
            actor,
            f"restore-quota-{index}",
            JobKind.WATCH,
            {"url": "https://example.com", "interval_seconds": 300},
            now - timedelta(hours=1),
            revision=1,
            now=now,
        )
        await initial.cancel(scope, actor, JobKind.WATCH, job.id)
        cancelled.append(await initial.get_job(scope, job.id))

    async def restore(index):
        job = cancelled[index]
        return await limited.restore(
            scopes[index],
            actor,
            JobKind.WATCH,
            job.id,
            all_members=False,
            expected_revision=job.revision,
            revision=1,
            now=now,
        )

    outcomes = await asyncio.gather(restore(0), restore(1), return_exceptions=True)
    assert sum(not isinstance(item, Exception) and item is not None for item in outcomes) == 1
    assert await db.fetchval("SELECT count(*) FROM scheduled_jobs WHERE status='active'") == 1


async def test_manager_restore_counts_job_owner_quota(db):
    scope = Scope("tg:-303", False)
    owner, manager = Actor("tg:7"), Actor("tg:8")
    now = datetime.now(UTC)
    initial = JobsRepo(db, JobsSettings(max_jobs_per_user=2))
    await initial.configure(scope, kind=JobKind.REMINDER, enabled=True)
    active = await initial.create(
        scope,
        owner,
        "owner-active",
        JobKind.REMINDER,
        {"text": "active"},
        now + timedelta(days=2),
        revision=1,
        now=now,
    )
    cancelled = await initial.create(
        scope,
        owner,
        "owner-cancelled",
        JobKind.REMINDER,
        {"text": "cancelled"},
        now + timedelta(days=2),
        revision=1,
        now=now,
    )
    assert active.status.value == "active"
    assert await initial.cancel(scope, owner, JobKind.REMINDER, cancelled.id)
    cancelled = await initial.get_job(scope, cancelled.id)
    limited = JobsRepo(db, JobsSettings(max_jobs_per_user=1))

    with pytest.raises(JobConflict):
        await limited.restore(
            scope,
            manager,
            JobKind.REMINDER,
            cancelled.id,
            all_members=True,
            expected_revision=cancelled.revision,
            revision=1,
            now=now,
        )


async def test_restore_window_is_checked_at_commit(db):
    window_seconds = 60
    repo = JobsRepo(db, JobsSettings(restore_window_seconds=window_seconds))
    scope, owner = Scope("tg:-304", False), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    now = datetime.now(UTC)
    job = await repo.create(
        scope,
        owner,
        "restore-window",
        JobKind.REMINDER,
        {"text": "window"},
        now + timedelta(days=2),
        revision=1,
        now=now,
    )
    assert await repo.cancel(scope, owner, JobKind.REMINDER, job.id)
    cancelled = await repo.get_job(scope, job.id)
    proposed_at = cancelled.terminal_at + timedelta(seconds=window_seconds - 1)
    committed_at = cancelled.terminal_at + timedelta(seconds=window_seconds + 1)

    class Platform:
        name = "tg"

        async def is_member(self, _chat, _user):
            return True

        async def is_group_admin(self, _chat, _user):
            return False

    current_time = proposed_at
    platform = Platform()
    access = AccessPolicy([scope.chat_key], [])
    service = JobsService(
        JobsSettings(restore_window_seconds=window_seconds),
        access,
        platform,
        clock=lambda: current_time,
    )
    service.repo = lambda _stores: repo
    chat = ChatRef.parse(scope.chat_key)
    user = User(UserRef.parse(owner.user_key), "Owner")
    stores = SimpleNamespace(db=db, extra={})
    tool_ctx = ToolContext(chat, user, False, platform, stores)
    action = await service.request_restore(tool_ctx, JobKind.REMINDER, str(job.id))
    assert action is not None
    assert proposed_at < cancelled.terminal_at + timedelta(seconds=window_seconds)

    current_time = committed_at
    command_ctx = CommandContext(chat, user, False, False, platform, stores)
    echo = await service.commit(action, command_ctx)
    assert echo == "No matching cancelled job was found."
    assert committed_at >= cancelled.terminal_at + timedelta(seconds=window_seconds)


async def _due_job(repo, scope, actor, action, *, text="x"):
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True) if action == "due-0" else None
    return await repo.create(
        scope,
        actor,
        action,
        JobKind.REMINDER,
        {"text": text},
        datetime.now(UTC) - timedelta(seconds=1),
        revision=1,
        now=datetime.now(UTC),
    )


async def test_two_workers_claim_disjoint_rows_with_global_bound(db):
    repo_a = JobsRepo(db, JobsSettings(worker_concurrency=4))
    repo_b = JobsRepo(db, JobsSettings(worker_concurrency=4))
    scope = Scope("tg:-100", False)
    await repo_a.configure(scope, kind=JobKind.REMINDER, enabled=True)
    for index in range(4):
        await repo_a.create(
            scope,
            Actor(f"tg:{index}"),
            f"claim-{index}",
            JobKind.REMINDER,
            {"text": "x"},
            datetime.now(UTC) - timedelta(seconds=1),
            revision=1,
            now=datetime.now(UTC),
        )
    now = datetime.now(UTC)
    first, second = await asyncio.gather(repo_a.claim_due(now, 2), repo_b.claim_due(now, 2))
    ids = [claim.job.id for claim in first + second]
    assert len(ids) == 4
    assert len(set(ids)) == 4


async def test_expired_claim_is_refenced_and_stale_finalizer_is_rejected(db):
    repo = JobsRepo(db, JobsSettings(worker_concurrency=1, lease_seconds=60))
    scope = Scope("tg:-100", False)
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    await repo.create(
        scope,
        Actor("tg:7"),
        "fenced",
        JobKind.REMINDER,
        {"text": "x"},
        datetime.now(UTC) - timedelta(seconds=1),
        revision=1,
        now=datetime.now(UTC),
    )
    first = (await repo.claim_due(datetime.now(UTC)))[0]
    second = (await repo.claim_due(datetime.now(UTC) + timedelta(seconds=61)))[0]
    assert first.token != second.token
    assert not await repo.finish_run(first, now=datetime.now(UTC), next_run_at=None)


async def test_cancellation_before_reservation_prevents_delivery(db):
    repo = JobsRepo(db, JobsSettings())
    scope, actor = Scope("tg:-100", False, "7"), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    await repo.create(
        scope,
        actor,
        "cancel-before",
        JobKind.REMINDER,
        {"text": "x"},
        datetime.now(UTC) - timedelta(seconds=1),
        revision=1,
        now=datetime.now(UTC),
    )
    claim = (await repo.claim_due(datetime.now(UTC)))[0]
    assert await repo.cancel(scope, actor, JobKind.REMINDER, claim.job.id)
    assert not await repo.reserve_delivery(
        claim, "Reminder", "reminder", now=datetime.now(UTC), next_run_at=None
    )


async def test_cancel_after_reservation_prevents_unreserved_delivery(db):
    repo = JobsRepo(db, JobsSettings())
    scope, actor = Scope("tg:-100", False, "7"), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    await repo.create(
        scope,
        actor,
        "cancel-after-reserve",
        JobKind.REMINDER,
        {"text": "x"},
        datetime.now(UTC) - timedelta(seconds=1),
        revision=1,
        now=datetime.now(UTC),
    )
    claim = (await repo.claim_due(datetime.now(UTC)))[0]
    assert await repo.reserve_delivery(
        claim, "Reminder", "reminder", now=datetime.now(UTC), next_run_at=None
    )
    assert await repo.cancel(scope, actor, JobKind.REMINDER, claim.job.id)
    assert not await repo.delivery_allowed(claim, "reminder")
    assert await repo.finish_delivery(claim, "reminder", status="suppressed", now=datetime.now(UTC))


async def test_saved_group_limits_clamp_when_global_ceilings_drop(db):
    high = JobsRepo(db)
    scope = Scope("tg:-100", False)
    await high.configure(scope, kind=JobKind.REMINDER, enabled=True)
    from chatcore_jobs.settings import GroupLimits

    await high.configure(
        scope,
        kind=JobKind.REMINDER,
        limits=GroupLimits(
            max_jobs_per_user=10, max_jobs_per_chat=100, minimum_interval_seconds=300
        ),
    )
    low = JobsRepo(db, JobsSettings(max_jobs_per_user=2, max_jobs_per_chat=2))
    effective = await low.settings(scope)
    assert effective["max_jobs_per_user"] == 2
    assert effective["max_jobs_per_chat"] == 2


async def test_user_quota_is_per_topic_while_chat_quota_is_shared(db):
    repo = JobsRepo(db, JobsSettings(max_jobs_per_user=1, max_jobs_per_chat=3))
    base = Scope("tg:-100", False, "7")
    other_topic = Scope("tg:-100", False, "8")
    await repo.configure(base, kind=JobKind.REMINDER, enabled=True)
    for topic, action in ((base, "topic-7"), (other_topic, "topic-8")):
        await repo.create(
            topic,
            Actor("tg:7"),
            action,
            JobKind.REMINDER,
            {"text": action},
            datetime.now(UTC) + timedelta(hours=1),
            revision=1,
            now=datetime.now(UTC),
        )
    assert len(await repo.list_jobs(base, Actor("tg:7"), JobKind.REMINDER)) == 1
    assert len(await repo.list_jobs(other_topic, Actor("tg:7"), JobKind.REMINDER)) == 1


async def test_reserved_send_keeps_global_worker_slot_until_outcome(db):
    repo = JobsRepo(db, JobsSettings(worker_concurrency=1))
    scope = Scope("tg:-100", False)
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    due = datetime.now(UTC) - timedelta(seconds=1)
    for key in ("slot-a", "slot-b"):
        await repo.create(
            scope,
            Actor(f"tg:{key}"),
            key,
            JobKind.REMINDER,
            {"text": key},
            due,
            revision=1,
            now=datetime.now(UTC),
        )
    now = datetime.now(UTC)
    first = (await repo.claim_due(now))[0]
    assert await repo.reserve_delivery(first, "Reminder", "reminder", now=now, next_run_at=None)
    assert await repo.claim_due(now) == []
    assert await repo.finish_delivery(
        first, "reminder", status="sent", terminal_status="completed", now=now
    )
    assert len(await repo.claim_due(now)) == 1


async def test_stale_reserved_reminder_becomes_unknown_and_is_never_replayed(db):
    repo = JobsRepo(db, JobsSettings(worker_concurrency=1, lease_seconds=60))
    scope, actor = Scope("tg:-100", False), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    now = datetime.now(UTC)
    await repo.create(
        scope,
        actor,
        "crash-after-reservation",
        JobKind.REMINDER,
        {"text": "x"},
        now - timedelta(seconds=1),
        revision=1,
        now=now,
    )
    claim = (await repo.claim_due(now))[0]
    assert await repo.reserve_delivery(claim, "Reminder", "reminder", now=now, next_run_at=None)
    assert await repo.claim_due(now + timedelta(seconds=61)) == []
    listed = await repo.list_jobs(scope, actor, JobKind.REMINDER)
    assert listed[0].status.value == "failed"
    assert listed[0].last_delivery_state == "unknown"


async def test_malformed_historical_payload_fails_closed_without_execution(db):
    repo = JobsRepo(db)
    scope, actor = Scope("tg:-100", False), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    now = datetime.now(UTC)
    job = await repo.create(
        scope,
        actor,
        "malformed",
        JobKind.REMINDER,
        {"text": "x"},
        now - timedelta(seconds=1),
        revision=1,
        now=now,
    )
    await db.execute("UPDATE scheduled_jobs SET payload='{}'::jsonb WHERE id=$1", job.id)
    assert await repo.list_jobs(scope, actor, JobKind.REMINDER) == []
    assert await repo.claim_due(now) == []
    row = await db.fetchrow(
        "SELECT status,last_delivery_state FROM scheduled_jobs WHERE id=$1", job.id
    )
    assert row["status"] == "failed"
    assert row["last_delivery_state"] == "invalid_payload"


async def test_user_purge_only_removes_matching_chat_scope(db):
    repo = JobsRepo(db)
    alice = Actor("tg:7")
    chat_a, chat_b = Scope("tg:-100", False), Scope("tg:-200", False)
    await repo.configure(chat_a, kind=JobKind.REMINDER, enabled=True)
    await repo.configure(chat_b, kind=JobKind.REMINDER, enabled=True)
    due = datetime.now(UTC) + timedelta(hours=1)
    for scope, action in ((chat_a, "purge-a"), (chat_b, "purge-b")):
        await repo.create(
            scope,
            alice,
            action,
            JobKind.REMINDER,
            {"text": action},
            due,
            revision=1,
            now=datetime.now(UTC),
        )
    await repo.purge_user(chat_a.chat_key, alice.user_key)
    assert await repo.list_jobs(chat_a, alice, JobKind.REMINDER) == []
    assert len(await repo.list_jobs(chat_b, alice, JobKind.REMINDER)) == 1


async def test_disable_after_claim_cancels_before_reservation(db):
    repo = JobsRepo(db)
    scope, actor = Scope("tg:-100", False, "7"), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    await repo.create(
        scope,
        actor,
        "disable-after-claim",
        JobKind.REMINDER,
        {"text": "x"},
        datetime.now(UTC) - timedelta(seconds=1),
        revision=1,
        now=datetime.now(UTC),
    )
    claim = (await repo.claim_due(datetime.now(UTC)))[0]
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=False)
    assert not await repo.reserve_delivery(
        claim, "Reminder", "reminder", now=datetime.now(UTC), next_run_at=None
    )


async def test_unknown_old_occurrence_does_not_clear_new_watch_lease(db):
    repo = JobsRepo(db, JobsSettings(worker_concurrency=2, lease_seconds=60))
    scope, actor = Scope("tg:-100", False), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.WATCH, enabled=True)
    t0 = datetime.now(UTC).replace(microsecond=0)
    await repo.create(
        scope,
        actor,
        "old-watch-delivery",
        JobKind.WATCH,
        {"url": "https://example.com", "interval_seconds": 300},
        t0,
        revision=1,
        now=t0 - timedelta(seconds=1),
    )
    first = (await repo.claim_due(t0))[0]
    assert await repo.reserve_delivery(
        first,
        "Website text changed",
        "watch_changed",
        now=t0,
        next_run_at=t0 + timedelta(seconds=300),
    )
    t300 = t0 + timedelta(seconds=300)
    recovered = await repo.claim_due(t300)
    assert len(recovered) == 1
    assert recovered[0].token != first.token
    assert recovered[0].lease_until == t300 + timedelta(seconds=60)
    assert await repo.claim_due(t300 + timedelta(seconds=1)) == []
    persisted = await db.fetchrow(
        "SELECT lease_token,lease_until FROM scheduled_jobs WHERE id=$1", first.job.id
    )
    assert persisted["lease_token"] == recovered[0].token
    assert persisted["lease_until"] == recovered[0].lease_until


async def test_reserved_reminder_recovers_at_exact_lease_expiry_without_new_occurrence(db):
    repo = JobsRepo(db, JobsSettings(worker_concurrency=1, lease_seconds=60))
    scope, actor = Scope("tg:-100", False), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    t0 = datetime.now(UTC).replace(microsecond=0)
    job = await repo.create(
        scope,
        actor,
        "reserved-at-expiry",
        JobKind.REMINDER,
        {"text": "once"},
        t0,
        revision=1,
        now=t0,
    )
    claim = (await repo.claim_due(t0))[0]
    assert await repo.reserve_delivery(
        claim,
        "Reminder: once",
        "reminder",
        now=t0,
        next_run_at=None,
        terminal_status="completed",
    )

    assert await repo.claim_due(t0 + timedelta(seconds=60)) == []
    persisted = await repo.get_job(scope, job.id)
    delivery = await db.fetchrow(
        "SELECT status FROM job_deliveries WHERE job_id=$1 AND occurrence=0", job.id
    )
    assert persisted.status.value == "failed"
    assert persisted.last_delivery_state == "unknown"
    assert delivery["status"] == "unknown"


async def test_unresolved_reservation_holds_global_slot_past_claim_expiry(db):
    repo = JobsRepo(db, JobsSettings(worker_concurrency=1, lease_seconds=60))
    scope, actor = Scope("tg:-100", False), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    t0 = datetime.now(UTC).replace(microsecond=0)
    created = []
    for action in ("slow-reservation", "waiting-job"):
        created.append(
            await repo.create(
                scope,
                actor,
                action,
                JobKind.REMINDER,
                {"text": action},
                t0,
                revision=1,
                now=t0,
            )
        )
    claimed = (await repo.claim_due(t0))[0]
    assert await repo.reserve_delivery(
        claimed,
        "Reminder",
        "reminder",
        now=t0 + timedelta(seconds=30),
        next_run_at=None,
        terminal_status="completed",
    )

    # The claim lease expired at t+60, but the reservation's conservative
    # unknown-outcome window ends at t+90. It must hold the global worker slot.
    assert await repo.claim_due(t0 + timedelta(seconds=60)) == []
    after_recovery = await repo.claim_due(t0 + timedelta(seconds=90))
    assert len(after_recovery) == 1
    assert after_recovery[0].job.id != claimed.job.id


@pytest.mark.parametrize("outcome", ["suppressed", "sent", "unknown"])
async def test_delivery_finish_never_undoes_cancellation(db, outcome):
    repo = JobsRepo(db)
    scope, actor = Scope("tg:-100", False), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    now = datetime.now(UTC)
    job = await repo.create(
        scope,
        actor,
        "cancel-before-late-finish",
        JobKind.REMINDER,
        {"text": "x"},
        now - timedelta(seconds=1),
        revision=1,
        now=now,
    )
    claim = (await repo.claim_due(now))[0]
    assert await repo.reserve_delivery(claim, "Reminder", "reminder", now=now, next_run_at=None)
    assert await repo.cancel(scope, actor, JobKind.REMINDER, job.id)
    assert await repo.finish_delivery(
        claim, "reminder", status=outcome, terminal_status="completed", now=now
    )
    persisted = await repo.get_job(scope, job.id)
    assert persisted.status.value == "cancelled"


async def test_blocked_watch_reservation_recovers_as_terminal_after_restart(db):
    repo = JobsRepo(db, JobsSettings(worker_concurrency=1, lease_seconds=60))
    scope, actor = Scope("tg:-100", False), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.WATCH, enabled=True)
    now = datetime.now(UTC)
    job = await repo.create(
        scope,
        actor,
        "blocked-crash",
        JobKind.WATCH,
        {"url": "https://example.com", "interval_seconds": 300},
        now - timedelta(seconds=1),
        revision=1,
        now=now,
    )
    claim = (await repo.claim_due(now))[0]
    assert await repo.reserve_delivery(
        claim,
        "Safe blocked reason",
        "watch_blocked",
        now=now,
        next_run_at=None,
        terminal_status="failed",
    )
    restarted = JobsRepo(db, JobsSettings(worker_concurrency=1, lease_seconds=60))
    fresh_worker = __import__("chatcore_jobs.worker", fromlist=["JobsWorker"]).JobsWorker(
        restarted,
        JobsSettings(worker_concurrency=1, lease_seconds=60),
        authorize=lambda _job: _allow(),
        sender=lambda *_: _send(),
        fetcher=lambda *_: _fetch(),
    )
    sends, fetches = [], []

    async def send(*args):
        sends.append(args)
        return "unexpected"

    async def fetch(*args):
        fetches.append(args)
        raise AssertionError("terminal blocked job must not fetch after restart")

    fresh_worker.sender, fresh_worker.fetcher = send, fetch
    await fresh_worker.run_once(now + timedelta(seconds=61))
    assert not sends and not fetches
    persisted = await restarted.get_job(scope, job.id)
    assert persisted.status.value == "failed"


async def test_cancel_during_blocked_watch_pre_send_finalizes_reservation(db):
    from chatcore.security.net_guard import BlockedURL
    from chatcore_jobs.worker import JobsWorker

    settings = JobsSettings(worker_concurrency=1, lease_seconds=60)
    repo = JobsRepo(db, settings)
    scope, actor = Scope("tg:-100", False), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.WATCH, enabled=True)
    now = datetime.now(UTC).replace(microsecond=0)
    job = await repo.create(
        scope,
        actor,
        "cancel-blocked-pre-send",
        JobKind.WATCH,
        {"url": "https://example.com", "interval_seconds": 300},
        now,
        revision=1,
        now=now,
    )
    pre_send_authorization = asyncio.Event()
    authorize_calls = 0
    fetches, sends = [], []

    async def authorize(_job):
        nonlocal authorize_calls
        authorize_calls += 1
        if authorize_calls == 3:
            pre_send_authorization.set()
            await asyncio.Future()
        return True

    async def fetch(*_args):
        fetches.append(True)
        raise BlockedURL("private detail")

    async def send(*args):
        sends.append(args)
        return "unexpected"

    worker = JobsWorker(
        repo,
        settings,
        authorize=authorize,
        sender=send,
        fetcher=fetch,
    )
    task = asyncio.create_task(worker.run_once(now))
    await pre_send_authorization.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    delivery = await db.fetchrow(
        "SELECT status,terminal_status FROM job_deliveries WHERE job_id=$1", job.id
    )
    assert delivery["status"] == "suppressed"
    assert delivery["terminal_status"] == "failed"
    assert (await repo.get_job(scope, job.id)).status.value == "failed"

    fresh = JobsWorker(
        JobsRepo(db, settings),
        settings,
        authorize=lambda _job: _allow(),
        sender=send,
        fetcher=fetch,
    )
    assert await fresh.run_once(now + timedelta(seconds=1)) == 0
    assert len(fetches) == 1
    assert not sends


async def _allow():
    return True


async def _send():
    return "unexpected"


async def _fetch():
    raise AssertionError("must not fetch")


async def test_job_retention_uses_terminal_time_and_prunes_old_active_watch_deliveries(db):
    from chatcore.access import AccessPolicy
    from chatcore_jobs.plugin import JobsPlugin

    repo = JobsRepo(db, JobsSettings(retention_days=30))
    scope, actor = Scope("tg:-100", False), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.REMINDER, enabled=True)
    await repo.configure(scope, kind=JobKind.WATCH, enabled=True)
    now = datetime.now(UTC)
    old_terminal = await repo.create(
        scope,
        actor,
        "old-terminal",
        JobKind.REMINDER,
        {"text": "old"},
        now + timedelta(days=1),
        revision=2,
        now=now - timedelta(days=365),
    )
    recent_terminal = await repo.create(
        scope,
        actor,
        "recent-terminal",
        JobKind.REMINDER,
        {"text": "recent"},
        now + timedelta(days=1),
        revision=2,
        now=now - timedelta(days=365),
    )
    active_watch = await repo.create(
        scope,
        actor,
        "active-watch",
        JobKind.WATCH,
        {"url": "https://example.com", "interval_seconds": 300},
        now + timedelta(days=1),
        revision=2,
        now=now - timedelta(days=365),
    )
    await db.execute(
        "UPDATE scheduled_jobs SET status='completed',terminal_at=$2 WHERE id=$1",
        old_terminal.id,
        now - timedelta(days=31),
    )
    await db.execute(
        "UPDATE scheduled_jobs SET status='completed',terminal_at=$2 WHERE id=$1",
        recent_terminal.id,
        now - timedelta(days=5),
    )
    await db.execute("UPDATE scheduled_jobs SET occurrence=3 WHERE id=$1", active_watch.id)
    await db.execute(
        """INSERT INTO job_deliveries
           (chat_key,job_id,occurrence,notification_kind,status,content,claim_token,reserved_at,finished_at)
           VALUES($1,$2,0,'watch_changed','sent','old',gen_random_uuid(),$3,$3),
                 ($1,$2,2,'watch_changed','sent','latest',gen_random_uuid(),$4,$4)""",
        scope.chat_key,
        active_watch.id,
        now - timedelta(days=31),
        now - timedelta(days=1),
    )

    class Platform:
        name = "tg"

    plugin = JobsPlugin(
        JobsSettings(retention_days=30),
        AccessPolicy([scope.chat_key], []),
        Platform(),
        sender=lambda *_: _send(),
    )
    await plugin.purge_older_than(now - timedelta(days=365), SimpleNamespace(db=db))
    assert await repo.get_job(scope, old_terminal.id) is None
    assert (await repo.get_job(scope, recent_terminal.id)).status.value == "completed"
    assert (await repo.get_job(scope, active_watch.id)).status.value == "active"
    deliveries = await db.fetch(
        "SELECT occurrence FROM job_deliveries WHERE job_id=$1 ORDER BY occurrence",
        active_watch.id,
    )
    assert [row["occurrence"] for row in deliveries] == [2]


async def test_interval_floor_change_cancels_old_jobs_and_rejects_new_fast_jobs(db):
    from chatcore_jobs.settings import GroupLimits

    repo = JobsRepo(db)
    scope, actor = Scope("tg:-100", False), Actor("tg:7")
    await repo.configure(scope, kind=JobKind.WATCH, enabled=True)
    now = datetime.now(UTC)
    job = await repo.create(
        scope,
        actor,
        "interval-floor",
        JobKind.WATCH,
        {"url": "https://example.com", "interval_seconds": 300},
        now + timedelta(seconds=300),
        revision=1,
        now=now,
    )
    changed = await repo.configure(
        scope,
        kind=JobKind.WATCH,
        limits=GroupLimits(
            max_jobs_per_user=10, max_jobs_per_chat=100, minimum_interval_seconds=900
        ),
        expected_revision=1,
    )
    cancelled = await repo.get_job(scope, job.id)
    assert cancelled.status.value == "cancelled"
    assert cancelled.last_delivery_state == "interval_floor_raised"
    with pytest.raises(JobConflict, match="interval"):
        await repo.create(
            scope,
            actor,
            "new-fast-job",
            JobKind.WATCH,
            {"url": "https://example.com", "interval_seconds": 300},
            now + timedelta(seconds=300),
            revision=changed["revision"],
            now=now,
        )


async def test_global_floor_change_suppresses_existing_watch_dispatch(db):
    from chatcore.access import AccessPolicy
    from chatcore_jobs.plugin import JobsPlugin

    scope, actor = Scope("tg:-100", False), Actor("tg:7")
    repo = JobsRepo(db, JobsSettings(minimum_watch_interval_seconds=300))
    await repo.configure(scope, kind=JobKind.WATCH, enabled=True)
    now = datetime.now(UTC)
    job = await repo.create(
        scope,
        actor,
        "global-floor-watch",
        JobKind.WATCH,
        {"url": "https://example.com", "interval_seconds": 300},
        now - timedelta(seconds=1),
        revision=1,
        now=now,
    )

    class Platform:
        name = "tg"

    settings = JobsSettings(minimum_watch_interval_seconds=900)
    plugin = JobsPlugin(
        settings, AccessPolicy([scope.chat_key], []), Platform(), sender=lambda *_: _send()
    )
    fetches = []

    async def fetch(*args):
        fetches.append(args)
        raise AssertionError("obsolete interval must be cancelled before fetching")

    from chatcore_jobs.worker import JobsWorker

    worker = JobsWorker(
        repo,
        settings,
        authorize=lambda value: plugin._authorize_job(value, repo),
        sender=lambda *_: _send(),
        fetcher=fetch,
    )
    await worker.run_once(now)
    assert not fetches
    assert (await repo.get_job(scope, job.id)).status.value == "cancelled"


async def test_worker_refreshes_dynamic_allowlist_before_authorizing_batch(db):
    from chatcore.access import AccessPolicy
    from chatcore.administration.store import AdminChange, MemoryAdministrationStore
    from chatcore.events import ChatRef, UserRef
    from chatcore_jobs.plugin import JobsPlugin
    from chatcore_jobs.worker import JobsWorker

    scope, actor = Scope("tg:-100", False), Actor("tg:7")
    repo = JobsRepo(db)
    await repo.configure(scope, kind=JobKind.WATCH, enabled=True)
    now = datetime.now(UTC)
    job = await repo.create(
        scope,
        actor,
        "allowlist-revoked-watch",
        JobKind.WATCH,
        {"url": "https://example.com", "interval_seconds": 300},
        now - timedelta(seconds=1),
        revision=1,
        now=now,
    )
    owner, group = UserRef("tg", "1"), ChatRef("tg", "-100")
    store = MemoryAdministrationStore()
    enabled = await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("enable", group),
    )
    access = AccessPolicy([group.key], [], store=store)
    await access.refresh()
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=enabled.revision,
        change=AdminChange("disable", group),
    )

    class Platform:
        name = "tg"

    settings = JobsSettings()
    plugin = JobsPlugin(settings, access, Platform(), sender=lambda *_: _send())
    fetches, sends = [], []

    async def fetch(*args):
        fetches.append(args)
        raise AssertionError("revoked group must not fetch")

    async def send(*args):
        sends.append(args)
        return "unexpected"

    worker = JobsWorker(
        repo,
        settings,
        authorize=lambda value: plugin._authorize_job(value, repo),
        sender=send,
        fetcher=fetch,
        refresh_access=access.refresh,
    )
    await worker.run_once(now)
    assert not fetches and not sends
    assert (await repo.get_job(scope, job.id)).status.value == "cancelled"


async def test_fresh_worker_and_database_pool_recover_pre_reservation_but_never_replay_unknown(db):
    from chatcore_jobs.worker import JobsWorker

    settings = JobsSettings(worker_concurrency=1, lease_seconds=60)
    repo_a = JobsRepo(db, settings)
    scope, actor = Scope("tg:-100", False), Actor("tg:7")
    await repo_a.configure(scope, kind=JobKind.REMINDER, enabled=True)
    now = datetime.now(UTC)
    pending = await repo_a.create(
        scope,
        actor,
        "worker-restart-before-reservation",
        JobKind.REMINDER,
        {"text": "deliver after restart"},
        now - timedelta(seconds=1),
        revision=1,
        now=now,
    )
    worker_a = JobsWorker(
        repo_a, settings, authorize=lambda _job: _allow(), sender=lambda *_: _send()
    )
    assert worker_a.repo is repo_a

    async def abandon_after_claim(claim, _now):
        assert claim.job.id == pending.id

    worker_a._run_claim = abandon_after_claim
    assert await worker_a.run_once(now) == 1
    claimed_row = await db.fetchrow(
        "SELECT lease_token FROM scheduled_jobs WHERE id=$1", pending.id
    )
    assert claimed_row["lease_token"] is not None

    schema = await db.fetchval("SELECT current_schema()")
    pool_b = await asyncpg.create_pool(
        DSN, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    db_b = PgDatabase(pool_b)
    sends = []

    async def send(*args):
        sends.append(args)
        return "delivered"

    try:
        repo_b = JobsRepo(db_b, settings)
        worker_b = JobsWorker(
            repo_b,
            settings,
            authorize=lambda _job: _allow(),
            sender=send,
        )
        assert await worker_b.run_once(now + timedelta(seconds=61)) == 1
        assert len(sends) == 1
        assert (await repo_b.get_job(scope, pending.id)).status.value == "completed"
        worker_c = JobsWorker(
            JobsRepo(db_b, settings),
            settings,
            authorize=lambda _job: _allow(),
            sender=send,
        )
        assert await worker_c.run_once(now + timedelta(seconds=62)) == 0
        assert len(sends) == 1

        uncertain = await repo_b.create(
            scope,
            actor,
            "worker-restart-after-reservation",
            JobKind.REMINDER,
            {"text": "uncertain"},
            now + timedelta(seconds=1),
            revision=1,
            now=now,
        )
        reserved_claim = (await repo_b.claim_due(now + timedelta(seconds=2)))[0]
        assert reserved_claim.job.id == uncertain.id
        assert await repo_b.reserve_delivery(
            reserved_claim,
            "Reminder: uncertain",
            "reminder",
            now=now + timedelta(seconds=2),
            next_run_at=None,
            terminal_status="completed",
        )
        sends_before = len(sends)
        fresh_worker = JobsWorker(
            JobsRepo(db_b, settings),
            settings,
            authorize=lambda _job: _allow(),
            sender=send,
        )
        assert await fresh_worker.run_once(now + timedelta(seconds=63)) == 0
        assert len(sends) == sends_before
        uncertain_after_restart = await JobsRepo(db_b, settings).get_job(scope, uncertain.id)
        assert uncertain_after_restart.status.value == "failed"
    finally:
        await db_b.close()


async def test_disable_reports_exact_permanent_cancellation_count_across_topics(db):
    from chatcore_jobs.settings import GroupLimits

    repo = JobsRepo(db)
    group = Scope("tg:-100", False)
    await repo.configure(group, kind=JobKind.REMINDER, enabled=True)
    now = datetime.now(UTC)
    jobs = []
    for index, thread_id in enumerate((None, "7", "8")):
        scope = Scope(group.chat_key, False, thread_id)
        jobs.append(
            await repo.create(
                scope,
                Actor(f"tg:{index + 7}"),
                f"disable-count-{index}",
                JobKind.REMINDER,
                {"text": "will be cancelled"},
                now + timedelta(hours=1),
                revision=1,
                now=now,
            )
        )
    assert await repo.count_active(group, JobKind.REMINDER) == 3
    result = await repo.configure(
        group,
        kind=JobKind.REMINDER,
        enabled=False,
        limits=GroupLimits(
            max_jobs_per_user=10, max_jobs_per_chat=100, minimum_interval_seconds=300
        ),
        expected_revision=1,
    )
    assert result["_cancelled_jobs"] == 3
    persisted = [
        await repo.get_job(Scope(group.chat_key, False, job.scope.thread_id), job.id)
        for job in jobs
    ]
    assert all(job.status.value == "cancelled" for job in persisted)
    assert await repo.count_active(group, JobKind.REMINDER) == 0
