"""Postgres administration integration tests use disposable schemas only."""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from importlib import resources

import asyncpg
import pytest
from chatcore.access import AccessPolicy
from chatcore.administration.pg import PgAdministrationStore
from chatcore.administration.store import AdminChange, StaleChangeError, global_scope
from chatcore.events import ChatRef, UserRef
from chatcore.limits import BudgetOverride, Limits, LimitsSettings, PgBudgetUsage
from chatcore.store import migrations
from chatcore.store.base import ChatSettings
from chatcore.store.memory import MemoryCache
from chatcore.store.pg import PgCoreRepo, PgDatabase

pytestmark = pytest.mark.integration
DSN = os.environ.get("TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test")


@pytest.fixture
async def db():
    schema = f"adm_{uuid.uuid4().hex[:10]}"
    try:
        pool = await asyncpg.create_pool(
            DSN, min_size=1, max_size=2, timeout=3, server_settings={"search_path": schema}
        )
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"Postgres not available: {type(exc).__name__}")
    database = PgDatabase(pool)
    await database.execute(f"CREATE SCHEMA {schema}")
    try:
        await migrations.apply(database, [("chatcore", resources.files("chatcore.migrations"))])
        yield database
    finally:
        await database.execute(f"DROP SCHEMA {schema} CASCADE")
        await database.close()


@pytest.mark.asyncio
async def test_postgres_store_persists_assignments_overrides_and_audit(db):
    owner, delegate = UserRef("tg", "1"), UserRef("tg", "2")
    group = ChatRef("tg", "-1001")
    first = PgAdministrationStore(db)
    second = PgAdministrationStore(db)
    await first.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("grant", group, user=delegate),
    )
    await first.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=1,
        change=AdminChange("disable", group),
    )
    snapshot = await second.snapshot()
    assert snapshot.revision == 2
    assert (delegate.key, group.key) in snapshot.assignments
    assert snapshot.overrides[group.key] is False
    assert [entry.revision for entry in await second.audit()] == [2, 1]
    with pytest.raises(TypeError):
        snapshot.overrides[group.key] = True


@pytest.mark.asyncio
async def test_postgres_disable_survives_access_policy_rebuild(db):
    owner = UserRef("tg", "1")
    group = ChatRef("tg", "-1001")
    store = PgAdministrationStore(db)
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("disable", group),
    )
    first = AccessPolicy([group.key], [], owners=[owner.key], store=store)
    await first.refresh()
    rebuilt = AccessPolicy([group.key], [], owners=[owner.key], store=PgAdministrationStore(db))
    await rebuilt.refresh()
    assert not first.is_chat_allowed(group)
    assert not rebuilt.is_chat_allowed(group)


@pytest.mark.asyncio
async def test_postgres_failed_authorization_and_stale_write_leave_no_audit(db):
    owner, delegate = UserRef("tg", "1"), UserRef("tg", "2")
    group = ChatRef("tg", "-1001")
    store = PgAdministrationStore(db)
    with pytest.raises(PermissionError):
        await store.apply(
            actor=delegate,
            owners=frozenset({owner.key}),
            expected_revision=0,
            change=AdminChange("enable", group),
        )
    assert (await store.snapshot()).revision == 0
    assert await store.audit() == ()
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("enable", group),
    )
    with pytest.raises(StaleChangeError):
        await store.apply(
            actor=owner,
            owners=frozenset({owner.key}),
            expected_revision=0,
            change=AdminChange("disable", group),
        )
    assert (await store.snapshot()).overrides[group.key] is True
    assert len(await store.audit()) == 1


@pytest.mark.asyncio
async def test_postgres_competing_writes_at_same_revision_commit_only_one(db):
    owner = UserRef("tg", "1")
    groups = [ChatRef("tg", "-1001"), ChatRef("tg", "-1002")]
    store = PgAdministrationStore(db)
    results = await asyncio.gather(
        *(
            store.apply(
                actor=owner,
                owners=frozenset({owner.key}),
                expected_revision=0,
                change=AdminChange("enable", group),
            )
            for group in groups
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, StaleChangeError) for result in results) == 1
    assert (await store.snapshot()).revision == 1
    assert len(await store.audit()) == 1


@pytest.mark.asyncio
async def test_postgres_restrictions_limits_and_moderators_persist_with_audit_details(db):
    owner, moderator, target = (UserRef("tg", key) for key in ("1", "2", "3"))
    group = ChatRef("tg", "-1001")
    owners = frozenset({owner.key})
    first = PgAdministrationStore(db)
    await first.apply(
        actor=owner,
        owners=owners,
        expected_revision=0,
        change=AdminChange("moderator_grant", group, moderator),
    )
    await first.apply(
        actor=moderator,
        owners=owners,
        expected_revision=1,
        change=AdminChange("block", group, target),
    )
    pause_started = datetime.now(UTC)
    await first.apply(
        actor=owner,
        owners=owners,
        expected_revision=2,
        change=AdminChange("pause", global_scope("tg"), target, 3600),
    )
    await first.apply(
        actor=owner,
        owners=owners,
        expected_revision=3,
        change=AdminChange("limit", global_scope("tg"), target, 50),
    )
    restarted = PgAdministrationStore(db)
    snapshot = await restarted.snapshot()
    assert snapshot.revision == 4
    assert (moderator.key, group.key) in snapshot.moderators
    block = snapshot.restrictions[(target.key, group.key)]
    assert block.kind == "block" and block.until is None
    pause = snapshot.restrictions[(target.key, "tg:*")]
    expected_until = pause_started + timedelta(seconds=3600)
    assert pause.kind == "pause" and pause.until is not None
    assert abs((pause.until - expected_until).total_seconds()) < 1
    assert snapshot.user_limits[target.key] == 50
    rows = await restarted.audit()
    assert next(row for row in rows if row.operation == "pause").detail.startswith("until=")
    assert next(row for row in rows if row.operation == "limit").detail == "limit=50"

    await restarted.apply(
        actor=moderator,
        owners=owners,
        expected_revision=4,
        change=AdminChange("unblock", group, target),
    )
    await restarted.apply(
        actor=owner,
        owners=owners,
        expected_revision=5,
        change=AdminChange("limit_reset", global_scope("tg"), target),
    )
    await restarted.apply(
        actor=owner,
        owners=owners,
        expected_revision=6,
        change=AdminChange("moderator_revoke", group, moderator),
    )
    third = await PgAdministrationStore(db).snapshot()
    assert (target.key, group.key) not in third.restrictions
    assert (target.key, "tg:*") in third.restrictions
    assert target.key not in third.user_limits
    assert (moderator.key, group.key) not in third.moderators


@pytest.mark.asyncio
async def test_postgres_target_failure_does_not_bump_revision_or_audit(db):
    owner = UserRef("tg", "1")
    group = ChatRef("tg", "-1001")
    store = PgAdministrationStore(db)
    with pytest.raises(ValueError):
        await store.apply(
            actor=owner,
            owners=frozenset({owner.key}),
            expected_revision=0,
            change=AdminChange("block", group, owner),
        )
    assert (await store.snapshot()).revision == 0
    assert await store.audit() == ()


@pytest.mark.asyncio
async def test_postgres_delegate_scope_and_revoke_regrant_stale_confirmation(db):
    owner, delegate = UserRef("tg", "1"), UserRef("tg", "2")
    group, other = ChatRef("tg", "-1001"), ChatRef("tg", "-1002")
    owners = frozenset({owner.key})
    store = PgAdministrationStore(db)
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=0,
        change=AdminChange("grant", group, user=delegate),
    )
    with pytest.raises(PermissionError):
        await store.apply(
            actor=delegate,
            owners=owners,
            expected_revision=1,
            change=AdminChange("enable", other),
        )
    await store.apply(
        actor=delegate,
        owners=owners,
        expected_revision=1,
        change=AdminChange("enable", group),
    )
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=2,
        change=AdminChange("revoke", group, user=delegate),
    )
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=3,
        change=AdminChange("grant", group, user=delegate),
    )
    with pytest.raises(StaleChangeError):
        await store.apply(
            actor=delegate,
            owners=owners,
            expected_revision=2,
            change=AdminChange("disable", group),
        )
    snapshot = await store.snapshot()
    assert snapshot.revision == 4
    assert snapshot.overrides[group.key] is True


@pytest.mark.asyncio
async def test_postgres_settings_updates_preserve_other_field_and_actor(db):
    owner, delegate = UserRef("tg", "1"), UserRef("tg", "2")
    group = ChatRef("tg", "-1001")
    owners = frozenset({delegate.key})
    admin = PgAdministrationStore(db)
    repo = PgCoreRepo(db)
    await repo.save_chat_settings(
        ChatSettings(group, persona="Keep this persona"), updated_by=owner
    )
    await admin.apply(
        actor=delegate,
        owners=owners,
        expected_revision=0,
        change=AdminChange("chatmode", group, value="on"),
    )
    saved = await repo.get_chat_settings(group)
    assert saved.persona == "Keep this persona"
    assert saved.chatmode is True
    row = await db.fetchrow("SELECT updated_by FROM chat_settings WHERE chat_key=$1", group.key)
    assert row["updated_by"] == delegate.key
    await admin.apply(
        actor=delegate,
        owners=owners,
        expected_revision=1,
        change=AdminChange("persona", group, value="Updated persona"),
    )
    saved = await repo.get_chat_settings(group)
    assert saved.persona == "Updated persona"
    assert saved.chatmode is True
    await admin.apply(
        actor=delegate,
        owners=owners,
        expected_revision=2,
        change=AdminChange("persona_reset", group),
    )
    saved = await repo.get_chat_settings(group)
    assert saved.persona is None
    assert saved.chatmode is True


@pytest.mark.asyncio
async def test_postgres_audit_filters_group_and_limit(db):
    owner = UserRef("tg", "1")
    group, other = ChatRef("tg", "-1001"), ChatRef("tg", "-1002")
    store = PgAdministrationStore(db)
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("enable", group),
    )
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=1,
        change=AdminChange("disable", other),
    )
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=2,
        change=AdminChange("disable", group),
    )
    scoped = await store.audit(groups=frozenset({group.key}), limit=1)
    assert len(scoped) == 1
    assert scoped[0].group == group.key and scoped[0].revision == 3
    assert await store.audit(limit=0) == ()


@pytest.mark.asyncio
async def test_missing_revision_singleton_fails_closed_for_all_store_operations(db):
    store = PgAdministrationStore(db)
    await db.execute("DELETE FROM administration_revision WHERE singleton=true")
    with pytest.raises(RuntimeError, match="revision row is missing"):
        await store.snapshot()
    with pytest.raises(RuntimeError, match="revision row is missing"):
        await store.audit()
    with pytest.raises(RuntimeError, match="revision row is missing"):
        await store.apply(
            actor=UserRef("tg", "1"),
            owners=frozenset({"tg:1"}),
            expected_revision=0,
            change=AdminChange("enable", ChatRef("tg", "-1001")),
        )


@pytest.mark.parametrize("operation", ["enable", "chatmode"])
@pytest.mark.asyncio
async def test_postgres_audit_failure_rolls_back_settings_revision_and_permissions(db, operation):
    group, owner = ChatRef("tg", "-1001"), UserRef("tg", "1")

    class FailingTransaction:
        def __init__(self, tx):
            self.tx = tx

        async def execute(self, sql, *args):
            if "INSERT INTO administration_audit" in sql:
                raise RuntimeError("injected audit failure")
            return await self.tx.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.tx, name)

    class FailingDatabase:
        def __init__(self, base):
            self.base = base

        @asynccontextmanager
        async def transaction(self):
            async with self.base.transaction() as tx:
                yield FailingTransaction(tx)

    change = AdminChange(operation, group, value="on" if operation == "chatmode" else None)
    with pytest.raises(RuntimeError, match="injected"):
        await PgAdministrationStore(FailingDatabase(db)).apply(
            actor=owner, owners=frozenset({owner.key}), expected_revision=0, change=change
        )
    store = PgAdministrationStore(db)
    assert (await store.snapshot()).revision == 0
    assert (await store.snapshot()).overrides == {}
    assert await store.audit() == ()
    assert (await PgCoreRepo(db).get_chat_settings(group)).chatmode is False


@pytest.mark.asyncio
async def test_postgres_budget_operations_round_trip_with_audit_and_check_constraints(db):
    owner = UserRef("tg", "1")
    store = PgAdministrationStore(db)
    scope = global_scope("tg")
    day = date.today()
    changes = [
        AdminChange("budget_set", scope, value=150_000),
        AdminChange("budget_extra", scope, value=10_000, day=day),
        AdminChange("budget_set", scope, value=160_000),
        AdminChange("budget_reset", scope),
        AdminChange("budget_set", scope, value=170_000),
        AdminChange("budget_extra_reset", scope),
    ]
    expected_states = [
        (150_000, None, None),
        (150_000, 10_000, day),
        (160_000, 10_000, day),
        (None, 10_000, day),
        (170_000, 10_000, day),
        (170_000, None, None),
    ]
    for revision, change in enumerate(changes):
        await store.apply(
            actor=owner,
            owners=frozenset({owner.key}),
            expected_revision=revision,
            change=change,
        )
        snapshot = await store.snapshot()
        daily, extra, extra_day = expected_states[revision]
        assert snapshot.budget == BudgetOverride(
            daily,
            extra,
            extra_day,
            owner.key,
            snapshot.budget.set_at,
        )
        assert snapshot.budget.set_by == owner.key
        assert snapshot.budget.set_at is not None
        assert snapshot.revision == revision + 1
    assert [entry.detail for entry in reversed(await store.audit())] == [
        "budget=150000",
        f"extra=10000 day={day.isoformat()}",
        "budget=160000",
        "budget=default",
        "budget=170000",
        "extra=none",
    ]
    invalid_updates = [
        ("daily_tokens", 99_999, None),
        ("daily_tokens", 100_000_001, None),
        ("extra_tokens", 9_999, day),
        ("extra_tokens", 100_000_001, day),
        ("extra_tokens", 10_000, None),
    ]
    for column, value, extra_day in invalid_updates:
        with pytest.raises(asyncpg.CheckViolationError):
            await db.execute(
                f"UPDATE administration_budget SET {column}=$1, extra_day=$2 WHERE singleton=true",
                value,
                extra_day,
            )


@pytest.mark.asyncio
async def test_postgres_budget_usage_save_is_monotonic(db):
    usage = PgBudgetUsage(db)
    day = date(2026, 10, 31)
    await usage.save(day, 10)
    await usage.save(day, 5)
    assert await usage.load(day) == 10


@pytest.mark.asyncio
async def test_limits_usage_mirror_restores_after_cache_loss_and_continues(db):
    cache = MemoryCache()
    usage = PgBudgetUsage(db)
    limits = Limits(
        cache,
        LimitsSettings(BUDGET_TZ="UTC"),
        usage_store=usage,
    )
    assert await limits.record(1_000) == 1_000
    assert await limits.record(500) == 1_500
    day = limits.day()
    key = f"budget:{day.isoformat()}"
    await cache.delete(key)
    assert await limits.budget_used() == 1_500
    assert await cache.get(key) == b"1500"
    assert await limits.record(10) == 1_510
    assert await usage.load(day) == 1_510


async def test_postgres_trust_admission_block_and_restart(db):
    owner, friend = UserRef("tg", "1"), UserRef("tg", "2")
    owners, group = frozenset({owner.key}), ChatRef("tg", "-1001")
    store = PgAdministrationStore(db)
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=0,
        change=AdminChange("trust", global_scope("tg"), friend),
    )

    async def admit():
        return await store.admit_group(
            group=group,
            inviter=friend,
            owners=owners,
            bootstrap_chats=frozenset(),
            event_key="1:100",
        )

    assert sorted(await asyncio.gather(admit(), admit())) == ["admitted", "duplicate"]
    restarted = PgAdministrationStore(db)
    snapshot = await restarted.snapshot()
    assert snapshot.revision == 2
    assert snapshot.trusted_inviters == frozenset({friend.key})
    assert snapshot.admissions[group.key].inviter == friend.key
    assert len(await restarted.audit()) == 2
    assert await admit() == "duplicate"
    await restarted.apply(
        actor=owner, owners=owners, expected_revision=2, change=AdminChange("blockgroup", group)
    )
    assert group.key in (await store.snapshot()).blocked_groups
    with pytest.raises(PermissionError, match="blocked"):
        await store.apply(
            actor=owner, owners=owners, expected_revision=3, change=AdminChange("enable", group)
        )
    assert await admit() == "denied"
    await store.apply(
        actor=owner, owners=owners, expected_revision=3, change=AdminChange("unblockgroup", group)
    )
    assert await admit() == "denied"
    assert (await restarted.snapshot()).overrides[group.key] is False


@pytest.mark.parametrize("operation", ["untrust", "disable", "blockgroup"])
async def test_postgres_admission_waits_for_current_permissions_under_revision_lock(db, operation):
    owner, friend = UserRef("tg", "1"), UserRef("tg", "2")
    owners, group = frozenset({owner.key}), ChatRef("tg", "-1001")
    store = PgAdministrationStore(db)
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=0,
        change=AdminChange("trust", global_scope("tg"), friend),
    )
    # Hold the same lock used by confirmed mutations. Admission must not authorize
    # from the earlier snapshot while a revoke/block is committing.
    async with db.transaction() as tx:
        await tx.fetchval(
            "SELECT revision FROM administration_revision WHERE singleton=true FOR UPDATE"
        )
        task = asyncio.create_task(
            store.admit_group(
                group=group,
                inviter=friend,
                owners=owners,
                bootstrap_chats=frozenset(),
                event_key="1:101",
            )
        )
        await asyncio.sleep(0.05)
        assert not task.done()
        if operation == "untrust":
            await tx.execute(
                "DELETE FROM administration_trusted_inviters WHERE user_key=$1", friend.key
            )
        else:
            await tx.execute(
                "INSERT INTO administration_groups(chat_key,enabled,updated_by) "
                "VALUES($1,false,$2)",
                group.key,
                owner.key,
            )
            if operation == "blockgroup":
                await tx.execute(
                    "INSERT INTO administration_group_blocks(chat_key,set_by) VALUES($1,$2)",
                    group.key,
                    owner.key,
                )
        await tx.execute(
            "UPDATE administration_revision SET revision=revision+1 WHERE singleton=true"
        )
    assert await task == "denied"
    assert len(await store.audit()) == 1
    assert not (await store.snapshot()).admissions


async def test_postgres_admission_audit_failure_rolls_back_all_state(db):
    owner, group = UserRef("tg", "1"), ChatRef("tg", "-1001")

    class FailingTransaction:
        def __init__(self, tx):
            self.tx = tx

        async def execute(self, sql, *args):
            if "INSERT INTO administration_audit" in sql:
                raise RuntimeError("injected audit failure")
            return await self.tx.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.tx, name)

    class FailingDatabase:
        @asynccontextmanager
        async def transaction(self):
            async with db.transaction() as tx:
                yield FailingTransaction(tx)

    with pytest.raises(RuntimeError, match="injected"):
        await PgAdministrationStore(FailingDatabase()).admit_group(
            group=group,
            inviter=owner,
            owners=frozenset({owner.key}),
            bootstrap_chats=frozenset(),
            event_key="1:102",
        )
    snapshot = await PgAdministrationStore(db).snapshot()
    assert snapshot.revision == 0 and not snapshot.admissions and not snapshot.overrides
    assert await db.fetchval("SELECT count(*) FROM administration_join_events") == 0


@pytest.mark.parametrize("restriction", ["block", "pause"])
async def test_postgres_admission_respects_restrictions_and_revocation_persists(db, restriction):
    owner, friend = UserRef("tg", "1"), UserRef("tg", "2")
    group, owners = ChatRef("tg", "-1001"), frozenset({owner.key})
    store = PgAdministrationStore(db)
    changes = [
        AdminChange("trust", global_scope("tg"), friend),
        AdminChange(
            restriction, global_scope("tg"), friend, 60 if restriction == "pause" else None
        ),
    ]
    for revision, change in enumerate(changes):
        await store.apply(actor=owner, owners=owners, expected_revision=revision, change=change)
    assert (
        await store.admit_group(
            group=group, inviter=friend, owners=owners, bootstrap_chats=frozenset()
        )
        == "denied"
    )
    if restriction == "pause":
        await db.execute("UPDATE administration_restrictions SET until=now()-interval '1 second'")
    else:
        await store.apply(
            actor=owner,
            owners=owners,
            expected_revision=2,
            change=AdminChange("unblock", global_scope("tg"), friend),
        )
    assert (
        await store.admit_group(
            group=group, inviter=friend, owners=owners, bootstrap_chats=frozenset()
        )
        == "admitted"
    )
    revision = (await store.snapshot()).revision
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=revision,
        change=AdminChange("untrust", global_scope("tg"), friend),
    )
    restarted = PgAdministrationStore(db)
    snapshot = await restarted.snapshot()
    assert not snapshot.trusted_inviters
    assert snapshot.overrides[group.key]
    assert (
        await restarted.admit_group(
            group=ChatRef("tg", "-1002"), inviter=friend, owners=owners, bootstrap_chats=frozenset()
        )
        == "denied"
    )
