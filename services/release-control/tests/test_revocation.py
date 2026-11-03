from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from chatcore_release_control.broker import (
    BrokerError,
    BrokerStore,
    VerifiedAdmissionEvidence,
)
from chatcore_release_control.ledger import HostReceipt, ReleaseLedger, ReleaseStatus
from chatcore_release_control.revocation import (
    PostgresReleaseRevocationStore,
    RevocationError,
    is_revoked,
    request_digest,
)

sys.path.insert(0, str(Path(__file__).parent))
pytest_plugins = ("test_broker",)
from test_broker import (  # noqa: E402
    _admitted_release,
    _initial_identity,
    _merged_release,
    _queued_card,
    _run,
)
from test_ledger import create_card, make_binding  # noqa: E402


@pytest.mark.asyncio
async def test_revocation_is_durable_idempotent_and_append_only(broker_pool):
    _, card, _, now = await _queued_card(broker_pool)
    store = PostgresReleaseRevocationStore(broker_pool)
    update_id = 99001

    recorded = await store.revoke(update_id, card.approval_id, 7, now=now)
    replay = await store.revoke(update_id, card.approval_id, 7, now=now)

    assert replay == recorded
    assert recorded.request_digest == request_digest(update_id, card.approval_id, 7)
    assert recorded.receipt_id
    assert (
        await broker_pool.fetchval(
            "SELECT count(*) FROM release_control.broker_release_revocation WHERE approval_id=$1",
            card.approval_id,
        )
        == 1
    )
    async with broker_pool.acquire() as connection:
        assert await is_revoked(connection, card.approval_id)
        with pytest.raises(asyncpg.PostgresError):
            await connection.execute(
                "UPDATE release_control.broker_release_revocation "
                "SET recorded_at=$2 WHERE update_id=$1",
                update_id,
                now,
            )
        with pytest.raises(asyncpg.PostgresError):
            await connection.execute(
                "DELETE FROM release_control.broker_release_revocation WHERE update_id=$1",
                update_id,
            )


@pytest.mark.asyncio
async def test_revocation_rejects_wrong_live_owner_and_update_reuse(broker_pool):
    _, card, _, now = await _queued_card(broker_pool)
    store = PostgresReleaseRevocationStore(broker_pool)

    with pytest.raises(RevocationError):
        await store.revoke(99002, card.approval_id, 8, now=now)
    async with broker_pool.acquire() as connection:
        assert not await is_revoked(connection, card.approval_id)

    await store.revoke(99003, card.approval_id, 7, now=now)
    other = await create_card(
        ReleaseLedger(broker_pool),
        binding=make_binding(),
        owner_user_id=7,
        chat_id=7,
        message_id=7002,
        now=now,
    )
    with pytest.raises(RevocationError) as conflict:
        await store.revoke(99003, other.approval_id, 7, now=now)
    assert conflict.value.code == "revocation_conflict"


@pytest.mark.asyncio
async def test_second_update_appends_without_replacing_original_revocation(broker_pool):
    _, card, _, now = await _queued_card(broker_pool)
    store = PostgresReleaseRevocationStore(broker_pool)

    first = await store.revoke(99004, card.approval_id, 7, now=now)
    second = await store.revoke(
        99005, card.approval_id, 7, now=datetime(2026, 10, 31, 11, 0, tzinfo=UTC)
    )

    assert first.receipt_id != second.receipt_id
    assert first.request_digest != second.request_digest
    assert (
        await broker_pool.fetchval(
            "SELECT count(*) FROM release_control.broker_release_revocation WHERE approval_id=$1",
            card.approval_id,
        )
        == 2
    )


@pytest.mark.asyncio
async def test_bridge_role_can_revoke_without_mutation_rights(broker_pool):
    _, card, _, now = await _queued_card(broker_pool)
    # Roles are cluster-wide while ledger_pool creates a fresh database per test.
    # The maintenance-role suite may already have provisioned these same groups.
    await broker_pool.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='release_control_bridge') THEN
                CREATE ROLE release_control_bridge NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='release_control_broker') THEN
                CREATE ROLE release_control_broker NOLOGIN;
            END IF;
        END $$;
    """)
    grant_sql = (
        Path(__file__).parents[1] / "src" / "chatcore_release_control" / "revocation_schema.sql"
    ).read_text()
    await broker_pool.execute(grant_sql)

    class _RoleAcquire:
        async def __aenter__(self):
            self.connection = await broker_pool.acquire()
            await self.connection.execute("SET ROLE release_control_bridge")
            return self.connection

        async def __aexit__(self, *_error):
            await self.connection.execute("RESET ROLE")
            await broker_pool.release(self.connection)

    class _RolePool:
        def acquire(self):
            return _RoleAcquire()

    bridge_store = PostgresReleaseRevocationStore(_RolePool())
    assert await bridge_store.ready() is True
    receipt = await bridge_store.revoke(99006, card.approval_id, 7, now=now)
    assert receipt.approval_id == card.approval_id


@pytest.mark.asyncio
async def test_revocation_blocks_fresh_merge_lease_and_merge_admission(broker_pool):
    _, card, fence, now = await _queued_card(broker_pool)
    store = BrokerStore(broker_pool)
    run = _run()
    await PostgresReleaseRevocationStore(broker_pool).revoke(99007, card.approval_id, 7, now=now)

    with pytest.raises(BrokerError, match="admission_denied"):
        await store.acquire_merge_lease(card.approval_id, dispatch_fence=fence, run=run, now=now)


@pytest.mark.asyncio
async def test_revocation_lock_wins_race_with_new_merge_lease(broker_pool):
    _, card, fence, now = await _queued_card(broker_pool)
    store = BrokerStore(broker_pool)
    async with broker_pool.acquire() as connection:
        async with connection.transaction():
            await connection.fetchrow(
                "SELECT * FROM release_control.lock_release_revocation_context($1)",
                card.approval_id,
            )
            task = asyncio.create_task(
                store.acquire_merge_lease(
                    card.approval_id,
                    dispatch_fence=fence,
                    run=_run(),
                    now=now,
                )
            )
            await asyncio.sleep(0.05)
            assert not task.done(), "admission did not serialize behind revocation context locks"
            await connection.execute(
                "INSERT INTO release_control.broker_release_revocation "
                "(update_id,approval_id,target,owner_user_id,request_digest,"
                "receipt_id,recorded_at) "
                "VALUES ($1,$2,'production',7,$3,$4,$5)",
                99012,
                card.approval_id,
                bytes.fromhex(request_digest(99012, card.approval_id, 7)),
                uuid4(),
                now,
            )
    with pytest.raises(BrokerError, match="admission_denied"):
        await task


@pytest.mark.asyncio
async def test_revocation_blocks_merge_admission_after_lease(broker_pool):
    _, card, fence, now = await _queued_card(broker_pool)
    store = BrokerStore(broker_pool)
    run = _run()
    lease = await store.acquire_merge_lease(
        card.approval_id, dispatch_fence=fence, run=run, now=now
    )
    evidence = VerifiedAdmissionEvidence(
        deployed_identity=_initial_identity(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        head_tree="c" * 40,
        current_main_sha="a" * 40,
        expected_merge_tree="9" * 40,
        evidence_digest="6" * 64,
        complete_delta_reviewed=True,
    )
    await PostgresReleaseRevocationStore(broker_pool).revoke(99008, card.approval_id, 7, now=now)
    with pytest.raises(BrokerError, match="admission_denied"):
        await store.admit_merge(
            card.approval_id,
            dispatch_fence=fence,
            lease_id=lease.lease_id,
            run=run,
            evidence=evidence,
            now=now,
        )


@pytest.mark.asyncio
async def test_revocation_preserves_exact_already_admitted_merge_retry(broker_pool):
    store, card, fence, lease, run, now = await _admitted_release(broker_pool)
    await PostgresReleaseRevocationStore(broker_pool).revoke(99009, card.approval_id, 7, now=now)
    evidence = VerifiedAdmissionEvidence(
        deployed_identity=_initial_identity(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        head_tree="c" * 40,
        current_main_sha="a" * 40,
        expected_merge_tree="9" * 40,
        evidence_digest="6" * 64,
        complete_delta_reviewed=True,
    )

    retry = await store.admit_merge(
        card.approval_id,
        dispatch_fence=fence,
        lease_id=lease.lease_id,
        run=run,
        evidence=evidence,
        now=now + timedelta(seconds=1),
    )
    assert retry.admitted is True


@pytest.mark.asyncio
async def test_revocation_blocks_first_switch_admission_but_allows_started_operation_receipt(
    broker_pool,
):
    store, card, fence, _lease, _run, now, operation = await _merged_release(broker_pool)
    await store.prepare_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation=operation,
        actual_identity=operation.baseline,
        now=now,
    )
    await PostgresReleaseRevocationStore(broker_pool).revoke(99010, card.approval_id, 7, now=now)
    with pytest.raises(BrokerError, match="admission_denied"):
        await store.admit_switch(
            card.approval_id,
            dispatch_fence=fence,
            operation_id=operation.operation_id,
            current_main_sha="f" * 40,
            actual_identity=operation.baseline,
            now=now,
        )


@pytest.mark.asyncio
async def test_revocation_preserves_started_operation_receipt_after_admission(broker_pool):
    store, card, fence, _lease, _run, now, operation = await _merged_release(broker_pool)
    await store.prepare_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation=operation,
        actual_identity=operation.baseline,
        now=now,
    )
    await store.admit_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation_id=operation.operation_id,
        current_main_sha="f" * 40,
        actual_identity=operation.baseline,
        now=now,
    )
    await PostgresReleaseRevocationStore(broker_pool).revoke(99011, card.approval_id, 7, now=now)
    await store.start_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation_id=operation.operation_id,
        now=now + timedelta(seconds=1),
    )
    receipt = HostReceipt(
        approval_id=card.approval_id,
        target="production",
        operation_id=operation.operation_id,
        receipt_id="post-revocation-host-receipt",
        fence=fence,
        status=ReleaseStatus.HEALTHY,
        merge_commit="f" * 40,
        observed_identity=operation.payload,
        observed_at=now + timedelta(seconds=2),
    )
    saved = await store.record_host_receipt(
        card.approval_id,
        receipt,
        actual_identity=operation.payload,
        now=now + timedelta(seconds=3),
    )
    assert saved == receipt
