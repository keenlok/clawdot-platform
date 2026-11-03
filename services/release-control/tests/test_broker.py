from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio
from chatcore_release_control.broker import (
    BrokerError,
    BrokerStore,
    VerifiedAdmissionEvidence,
    VerifiedMergeObservation,
)
from chatcore_release_control.ledger import (
    Decision,
    HostOperationBinding,
    HostReceipt,
    ReleaseLedger,
    ReleaseStatus,
    RunAttemptBinding,
)
from chatcore_release_control.policy import DeploymentIdentity, PolicyManifest, TrustedCheck

sys.path.insert(0, str(Path(__file__).parent))
pytest_plugins = ("test_ledger",)
from test_ledger import create_card, make_binding, make_command  # noqa: E402


@pytest_asyncio.fixture
async def broker_pool(ledger_pool):
    schema = Path(__file__).parents[1] / "src" / "chatcore_release_control" / "broker_schema.sql"
    await ledger_pool.execute(schema.read_text())
    worker_schema = (
        Path(__file__).parents[1] / "src" / "chatcore_release_control" / "worker_schema.sql"
    )
    await ledger_pool.execute(worker_schema.read_text())
    return ledger_pool


def _policy() -> PolicyManifest:
    workflow = ".github/workflows/release.yml"
    digest = "a" * 64
    return PolicyManifest(
        repository_id=101,
        owner_id=202,
        target="production",
        workflow_digests=((workflow, digest),),
        allowed_paths=("packages/plugin/src/plugin.py",),
        protected_paths=(workflow,),
        checks=(TrustedCheck("trusted checks", 77, workflow, digest),),
    )


def _initial_identity(sequence: int = 1) -> DeploymentIdentity:
    return DeploymentIdentity(
        git_sha="d" * 40,
        image_digest="sha256:" + "e" * 64,
        config_generation=4,
        config_digest="f" * 64,
        policy_digest=_policy().digest,
        target_sequence=sequence,
    )


def _policy_value() -> dict[str, object]:
    policy = _policy()
    return {
        "version": policy.version,
        "repository_id": policy.repository_id,
        "owner_id": policy.owner_id,
        "target": policy.target,
        "workflow_digests": dict(policy.workflow_digests),
        "allowed_paths": list(policy.allowed_paths),
        "protected_paths": list(policy.protected_paths),
        "checks": [
            {
                "name": check.name,
                "creator_app_id": check.creator_app_id,
                "workflow_path": check.workflow_path,
                "workflow_sha": check.workflow_digest,
            }
            for check in policy.checks
        ],
    }


async def _seed_target(pool, *, identity: DeploymentIdentity | None = None) -> None:
    policy = _policy()
    await pool.execute(
        "INSERT INTO release_control.target_queue (target) VALUES ($1) ON CONFLICT DO NOTHING",
        policy.target,
    )
    await pool.execute(
        """
        INSERT INTO release_control.broker_target_state
            (target, repository, telegram_owner_user_id, policy_digest, workflow_sha,
             policy_manifest, deployed_identity, generation)
        VALUES ($1, 'acme/plugin', 7, $2, $3, $4::jsonb, $5::jsonb, 1)
        """,
        policy.target,
        policy.digest,
        "a" * 40,
        json.dumps(_policy_value(), sort_keys=True, separators=(",", ":")),
        json.dumps(asdict(identity or _initial_identity()), sort_keys=True),
    )


async def _queued_card(pool):
    ledger = ReleaseLedger(pool)
    now = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    binding = make_binding(
        owner_id=202,
        target="production",
        policy_digest=_policy().digest,
        deployed_identity=_initial_identity(),
        review_bundle=_review_bundle(),
    )
    card = await create_card(ledger, binding=binding, owner_user_id=7, chat_id=7, now=now)
    await _seed_target(pool, identity=_initial_identity())
    await ledger.record_decision(
        make_command(card, decision=Decision.APPROVE, owner_user_id=7, chat_id=7),
        now=now + timedelta(seconds=1),
    )
    claim = await ledger.claim_next_dispatch(
        "production", "broker-test", now=now + timedelta(seconds=2)
    )
    assert claim is not None
    await ledger.acknowledge_dispatch(
        card.approval_id, fence=claim.fence, now=now + timedelta(seconds=3)
    )
    return ledger, card, claim.fence, now + timedelta(seconds=4)


def _run(*, attempt: int = 1) -> RunAttemptBinding:
    return RunAttemptBinding(
        repository_id=101,
        run_id=5001,
        attempt=attempt,
        workflow_ref="acme/plugin/.github/workflows/release.yml@refs/heads/main",
        workflow_sha="a" * 40,
        check_run_id=6001,
    )


def _review_bundle() -> dict[str, object]:
    return {
        "complete_delta_reviewed": True,
        "deployed_commit": "d" * 40,
        "base_commit": "a" * 40,
        "head_commit": "b" * 40,
        "deployed_tree": "c" * 40,
        "base_tree": "c" * 40,
        "head_tree": "c" * 40,
        "policy_digest": _policy().digest,
        "deployed_to_base": [],
        "base_to_head": [{"path": "packages/plugin/src/plugin.py", "change_type": "modified"}],
        "deployed_to_head": [{"path": "packages/plugin/src/plugin.py", "change_type": "modified"}],
        "check_evidence_digest": "6" * 64,
    }


async def _admitted_release(pool):
    _, card, fence, now = await _queued_card(pool)
    store = BrokerStore(pool)
    run = _run()
    lease = await store.acquire_merge_lease(
        card.approval_id, dispatch_fence=fence, run=run, now=now
    )
    facts = VerifiedAdmissionEvidence(
        deployed_identity=_initial_identity(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        head_tree="c" * 40,
        current_main_sha="a" * 40,
        expected_merge_tree="9" * 40,
        evidence_digest="6" * 64,
        complete_delta_reviewed=True,
    )
    await store.admit_merge(
        card.approval_id,
        dispatch_fence=fence,
        lease_id=lease.lease_id,
        run=run,
        evidence=facts,
        now=now,
    )
    return store, card, fence, lease, run, now


async def _merged_release(pool):
    store, card, fence, lease, run, now = await _admitted_release(pool)
    await store.record_merge_observation(
        card.approval_id,
        dispatch_fence=fence,
        lease_id=lease.lease_id,
        run=run,
        observation=VerifiedMergeObservation(
            commit="f" * 40,
            parents=("a" * 40, "b" * 40),
            tree="9" * 40,
            expected_tree="9" * 40,
            observed_at=now + timedelta(seconds=2),
        ),
        now=now + timedelta(seconds=3),
    )
    operation = HostOperationBinding(
        operation_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        baseline=_initial_identity(),
        payload=DeploymentIdentity(
            git_sha="f" * 40,
            image_digest="sha256:" + "8" * 64,
            config_generation=4,
            config_digest="f" * 64,
            policy_digest=_policy().digest,
            target_sequence=card.sequence,
        ),
    )
    return store, card, fence, lease, run, now + timedelta(seconds=4), operation


@pytest.mark.asyncio
async def test_broker_schema_is_explicit_and_readiness_requires_bootstrapped_target(broker_pool):
    store = BrokerStore(broker_pool)
    assert await store.schema_ready() is True
    assert await store.target_ready("production") is False

    await broker_pool.execute("DROP TABLE release_control.broker_release_revocation CASCADE")
    assert await store.schema_ready() is False
    schema = Path(__file__).parents[1] / "src" / "chatcore_release_control" / "broker_schema.sql"
    await broker_pool.execute(schema.read_text())
    assert await store.schema_ready() is True

    await _seed_target(broker_pool)

    state = await store.get_target_state("production")
    assert state.identity == _initial_identity()
    assert state.repository == "acme/plugin"
    assert state.telegram_owner_user_id == 7
    assert state.policy.digest == _policy().digest
    assert state.reconciliation_required is False


@pytest.mark.asyncio
async def test_broker_context_uses_distinct_telegram_and_github_owner_namespaces(broker_pool):
    ledger = ReleaseLedger(broker_pool)
    binding = make_binding(
        owner_id=202,
        target="production",
        policy_digest=_policy().digest,
        deployed_identity=_initial_identity(),
        review_bundle=_review_bundle(),
    )
    card = await create_card(ledger, binding=binding, owner_user_id=7, chat_id=7)
    await _seed_target(broker_pool, identity=_initial_identity())
    await ledger.record_decision(
        make_command(card, decision=Decision.APPROVE, owner_user_id=7, chat_id=7)
    )
    claim = await ledger.claim_next_dispatch("production", "broker-test")
    assert claim is not None
    await ledger.acknowledge_dispatch(card.approval_id, fence=claim.fence)

    context = await BrokerStore(broker_pool).get_release_context(card.approval_id)

    assert context.owner_id == 202
    assert context.telegram_owner_user_id == 7
    assert asdict(context.deployed_identity) == asdict(_initial_identity())


@pytest.mark.asyncio
async def test_missing_broker_schema_fails_closed_without_automatic_migration(ledger_pool):
    store = BrokerStore(ledger_pool)
    assert await store.schema_ready() is False
    with pytest.raises(BrokerError) as error:
        await store.get_target_state("production")
    assert error.value.code == "schema_unavailable"


@pytest.mark.asyncio
async def test_merge_lease_binds_one_run_attempt_under_existing_p05_fence(broker_pool):
    _, card, fence, now = await _queued_card(broker_pool)
    store = BrokerStore(broker_pool)

    first = await store.acquire_merge_lease(
        card.approval_id, dispatch_fence=fence, run=_run(), now=now
    )
    replay = await store.acquire_merge_lease(
        card.approval_id, dispatch_fence=fence, run=_run(), now=now
    )
    context = await store.get_release_context(card.approval_id, now=now)

    assert first.lease_id == replay.lease_id
    assert context.merge_lease["lease_id"] == str(first.lease_id)
    assert context.dispatch_fence == fence
    row = await broker_pool.fetchrow(
        "SELECT run_attempt, workflow_ref, check_run_id "
        "FROM release_control.release_card WHERE id=$1",
        card.approval_id,
    )
    assert (row["run_attempt"], row["workflow_ref"], row["check_run_id"]) == (
        1,
        _run().workflow_ref,
        6001,
    )


@pytest.mark.asyncio
async def test_legacy_card_cannot_bind_run_when_sequence_does_not_advance(broker_pool):
    _, card, fence, now = await _queued_card(broker_pool)
    async with broker_pool.acquire() as connection:
        await connection.execute(
            "ALTER TABLE release_control.release_card "
            "DISABLE TRIGGER release_card_binding_immutable"
        )
        try:
            await connection.execute(
                "UPDATE release_control.release_card SET sequence=1 WHERE id=$1",
                card.approval_id,
            )
        finally:
            await connection.execute(
                "ALTER TABLE release_control.release_card "
                "ENABLE TRIGGER release_card_binding_immutable"
            )

    with pytest.raises(BrokerError) as error:
        await BrokerStore(broker_pool).acquire_merge_lease(
            card.approval_id, dispatch_fence=fence, run=_run(), now=now
        )

    assert error.value.code == "reconciliation_required"


@pytest.mark.asyncio
async def test_old_run_attempt_cannot_replace_bound_attempt_or_reacquire_lease(broker_pool):
    _, card, fence, now = await _queued_card(broker_pool)
    store = BrokerStore(broker_pool)
    await store.acquire_merge_lease(card.approval_id, dispatch_fence=fence, run=_run(), now=now)

    with pytest.raises(BrokerError) as error:
        await store.acquire_merge_lease(
            card.approval_id, dispatch_fence=fence, run=_run(attempt=2), now=now
        )

    assert error.value.code == "binding_mismatch"


@pytest.mark.asyncio
async def test_any_attempt_of_retired_run_cannot_claim_dispatch_fence(broker_pool):
    _, card, fence, now = await _queued_card(broker_pool)
    run = _run(attempt=2)
    await broker_pool.execute(
        """INSERT INTO release_control.release_run_history
           (approval_id,dispatch_fence,repository_id,run_id,run_attempt,workflow_ref,
            workflow_sha,check_run_id,run_bound_at,conclusion,observed_at)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'cancelled',$10)""",
        card.approval_id,
        fence,
        run.repository_id,
        run.run_id,
        run.attempt - 1,
        run.workflow_ref,
        run.workflow_sha,
        run.check_run_id,
        now - timedelta(seconds=1),
        now,
    )

    with pytest.raises(BrokerError) as denied:
        await BrokerStore(broker_pool).acquire_merge_lease(
            card.approval_id,
            dispatch_fence=fence,
            run=run,
            now=now,
        )

    assert denied.value.code == "binding_mismatch"


@pytest.mark.asyncio
async def test_dispatch_nonce_lookup_requires_the_current_active_fence(broker_pool):
    _, card, fence, now = await _queued_card(broker_pool)
    expected = UUID("22222222-2222-4222-8222-222222222222")
    await broker_pool.execute(
        """INSERT INTO release_control.broker_dispatch_intent
           (approval_id,dispatch_fence,dispatch_id,request_digest,state,created_at,updated_at)
           VALUES($1,$2,$3,$4,'accepted',$5,$5)""",
        card.approval_id,
        fence,
        expected,
        b"x" * 32,
        now,
    )

    store = BrokerStore(broker_pool)
    assert await store.get_active_dispatch_id(card.approval_id, fence) == expected
    with pytest.raises(BrokerError) as stale:
        await store.get_active_dispatch_id(card.approval_id, fence + 1)
    assert stale.value.code == "admission_denied"


@pytest.mark.asyncio
async def test_merge_lease_expiry_blocks_new_admission_and_wrong_run_cannot_audit(broker_pool):
    store, card, fence, _, run, now = await _admitted_release(broker_pool)
    context = await store.get_release_context(card.approval_id, now=now)
    lease_id = UUID(context.merge_lease["lease_id"])
    replay = await store.admit_merge(
        card.approval_id,
        dispatch_fence=fence,
        lease_id=lease_id,
        run=run,
        evidence=VerifiedAdmissionEvidence(
            deployed_identity=_initial_identity(),
            base_sha="a" * 40,
            head_sha="b" * 40,
            head_tree="c" * 40,
            current_main_sha="a" * 40,
            expected_merge_tree="9" * 40,
            evidence_digest="6" * 64,
            complete_delta_reviewed=True,
        ),
        now=now + timedelta(seconds=91),
    )
    assert replay.admitted is True and replay.may_start is False

    wrong_run = RunAttemptBinding(
        repository_id=101,
        run_id=5001,
        attempt=2,
        workflow_ref=run.workflow_ref,
        workflow_sha=run.workflow_sha,
        check_run_id=6002,
    )
    with pytest.raises(BrokerError) as denied:
        await store.record_merge_observation(
            card.approval_id,
            dispatch_fence=fence,
            lease_id=lease_id,
            run=wrong_run,
            observation=VerifiedMergeObservation(
                commit="f" * 40,
                parents=("a" * 40, "b" * 40),
                tree="9" * 40,
                expected_tree="9" * 40,
                observed_at=now + timedelta(seconds=2),
            ),
            now=now + timedelta(seconds=3),
        )
    assert denied.value.code == "stale_fence"


@pytest.mark.asyncio
async def test_ordinary_role_cannot_read_or_write_broker_state(broker_pool):
    role = f"ordinary_broker_{UUID(int=1).hex}"
    assert re.fullmatch(r"ordinary_broker_[a-f0-9]+", role)
    async with broker_pool.acquire() as connection:
        await connection.execute(f'CREATE ROLE "{role}" NOLOGIN')
        try:
            await connection.execute(f'SET ROLE "{role}"')
            for table in (
                "broker_target_state",
                "broker_policy_snapshot",
                "broker_merge_lease",
                "broker_merge_admission",
                "broker_merge_observation",
                "broker_switch_operation",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.fetchval(f"SELECT count(*) FROM release_control.{table}")
        finally:
            await connection.execute("RESET ROLE")
            await connection.execute(f'DROP ROLE "{role}"')


@pytest.mark.asyncio
async def test_merge_admission_requires_full_current_d_and_fresh_check_evidence(broker_pool):
    _, card, fence, now = await _queued_card(broker_pool)
    store = BrokerStore(broker_pool)
    run = _run()
    lease = await store.acquire_merge_lease(
        card.approval_id, dispatch_fence=fence, run=run, now=now
    )
    facts = VerifiedAdmissionEvidence(
        deployed_identity=_initial_identity(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        head_tree="c" * 40,
        current_main_sha="a" * 40,
        expected_merge_tree="9" * 40,
        evidence_digest="6" * 64,
        complete_delta_reviewed=True,
    )

    admission = await store.admit_merge(
        card.approval_id,
        dispatch_fence=fence,
        lease_id=lease.lease_id,
        run=run,
        evidence=facts,
        now=now,
    )
    replay = await store.admit_merge(
        card.approval_id,
        dispatch_fence=fence,
        lease_id=lease.lease_id,
        run=run,
        evidence=facts,
        now=now + timedelta(seconds=5),
    )

    assert admission.admitted is True
    assert admission.may_start is True
    assert replay.lease_id == admission.lease_id
    assert replay.expected_merge_tree == "9" * 40
    row = await broker_pool.fetchrow(
        "SELECT status FROM release_control.release_card WHERE id=$1", card.approval_id
    )
    assert row["status"] == "merge_authorized"


@pytest.mark.asyncio
async def test_same_git_sha_with_different_image_or_config_cannot_admit(broker_pool):
    _, card, fence, now = await _queued_card(broker_pool)
    store = BrokerStore(broker_pool)
    run = _run()
    lease = await store.acquire_merge_lease(
        card.approval_id, dispatch_fence=fence, run=run, now=now
    )
    different_deployment = DeploymentIdentity(
        git_sha=_initial_identity().git_sha,
        image_digest="sha256:" + "8" * 64,
        config_generation=4,
        config_digest="f" * 64,
        policy_digest=_policy().digest,
        target_sequence=card.sequence,
    )
    facts = VerifiedAdmissionEvidence(
        deployed_identity=different_deployment,
        base_sha="a" * 40,
        head_sha="b" * 40,
        head_tree="c" * 40,
        current_main_sha="a" * 40,
        expected_merge_tree="9" * 40,
        evidence_digest="6" * 64,
        complete_delta_reviewed=True,
    )

    with pytest.raises(BrokerError) as error:
        await store.admit_merge(
            card.approval_id,
            dispatch_fence=fence,
            lease_id=lease.lease_id,
            run=run,
            evidence=facts,
            now=now,
        )

    assert error.value.code == "admission_denied"


@pytest.mark.asyncio
async def test_verified_merge_result_is_idempotent_and_records_exact_parents(broker_pool):
    store, card, fence, lease, run, now = await _admitted_release(broker_pool)
    observation = VerifiedMergeObservation(
        commit="f" * 40,
        parents=("a" * 40, "b" * 40),
        tree="9" * 40,
        expected_tree="9" * 40,
        observed_at=now + timedelta(seconds=2),
    )

    first = await store.record_merge_observation(
        card.approval_id,
        dispatch_fence=fence,
        lease_id=lease.lease_id,
        run=run,
        observation=observation,
        now=now + timedelta(seconds=3),
    )
    replay = await store.record_merge_observation(
        card.approval_id,
        dispatch_fence=fence,
        lease_id=lease.lease_id,
        run=run,
        observation=observation,
        now=now + timedelta(seconds=4),
    )

    assert first["status"] == replay["status"] == "merged_verified"
    row = await broker_pool.fetchrow(
        """SELECT merge_commit, merge_parent_base, merge_parent_head, merge_anomaly, status
           FROM release_control.release_card WHERE id=$1""",
        card.approval_id,
    )
    assert row["merge_commit"] == "f" * 40
    assert (row["merge_parent_base"], row["merge_parent_head"]) == ("a" * 40, "b" * 40)
    assert row["merge_anomaly"] is None
    assert row["status"] == "merged_verified"


@pytest.mark.asyncio
async def test_arbitrary_parent_merge_is_audited_without_fabricated_two_parent_evidence(
    broker_pool,
):
    store, card, fence, lease, run, now = await _admitted_release(broker_pool)
    observation = VerifiedMergeObservation(
        commit="f" * 40,
        parents=("a" * 40, "b" * 40, "c" * 40),
        tree="8" * 40,
        expected_tree="9" * 40,
        observed_at=now + timedelta(seconds=2),
    )

    result = await store.record_merge_observation(
        card.approval_id,
        dispatch_fence=fence,
        lease_id=lease.lease_id,
        run=run,
        observation=observation,
        now=now + timedelta(seconds=3),
    )
    row = await broker_pool.fetchrow(
        """SELECT merge_recorded_at, merge_parent_base, merge_parent_head,
                  merge_anomaly, status FROM release_control.release_card WHERE id=$1""",
        card.approval_id,
    )
    state = await broker_pool.fetchrow(
        "SELECT reconciliation_required FROM release_control.broker_target_state "
        "WHERE target='production'"
    )
    observations = await broker_pool.fetch(
        "SELECT parents FROM release_control.broker_merge_observation WHERE approval_id=$1",
        card.approval_id,
    )

    assert result["status"] == "merged_blocked"
    assert row["status"] == "merged_blocked"
    assert row["merge_recorded_at"] is None
    assert row["merge_parent_base"] is None and row["merge_parent_head"] is None
    assert json.loads(row["merge_anomaly"])["parents"] == ["a" * 40, "b" * 40, "c" * 40]
    assert json.loads(observations[0]["parents"]) == ["a" * 40, "b" * 40, "c" * 40]
    assert state["reconciliation_required"] is True


@pytest.mark.asyncio
async def test_switch_preparation_binds_exact_payload_but_grants_no_switch_authority(broker_pool):
    store, card, fence, _, _, now, operation = await _merged_release(broker_pool)

    prepared = await store.prepare_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation=operation,
        actual_identity=operation.baseline,
        now=now,
    )
    context = await store.get_release_context(card.approval_id, now=now)
    row = await broker_pool.fetchrow(
        "SELECT status, host_operation_id FROM release_control.release_card WHERE id=$1",
        card.approval_id,
    )

    assert prepared.operation_id == operation.operation_id
    assert row["status"] == "merged_verified"
    assert row["host_operation_id"] == operation.operation_id
    assert context.host_receipt is None


@pytest.mark.asyncio
async def test_switch_admission_uses_m_current_main_and_expires_start_permission(broker_pool):
    store, card, fence, _, _, now, operation = await _merged_release(broker_pool)
    await store.prepare_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation=operation,
        actual_identity=operation.baseline,
        now=now,
    )

    first = await store.admit_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation_id=operation.operation_id,
        current_main_sha="f" * 40,
        actual_identity=operation.baseline,
        now=now,
    )
    replay = await store.admit_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation_id=operation.operation_id,
        current_main_sha="0" * 40,
        actual_identity=operation.baseline,
        now=now + timedelta(minutes=3),
    )

    assert first.admitted is True and first.may_start is True
    assert replay.admitted is True and replay.may_start is False
    assert replay.operation_id == first.operation_id


@pytest.mark.asyncio
async def test_owner_revocation_before_switch_admission_denies_operation(broker_pool):
    store, card, fence, _, _, now, operation = await _merged_release(broker_pool)
    await store.prepare_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation=operation,
        actual_identity=operation.baseline,
        now=now,
    )
    await broker_pool.execute(
        "UPDATE release_control.broker_target_state SET telegram_owner_user_id=8 "
        "WHERE target='production'"
    )

    with pytest.raises(BrokerError) as denied:
        await store.admit_switch(
            card.approval_id,
            dispatch_fence=fence,
            operation_id=operation.operation_id,
            current_main_sha="f" * 40,
            actual_identity=operation.baseline,
            now=now,
        )

    assert denied.value.code == "admission_denied"


@pytest.mark.asyncio
async def test_owner_revocation_after_admission_allows_only_same_operation_start(broker_pool):
    store, card, fence, _, _, now, operation = await _merged_release(broker_pool)
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
    await broker_pool.execute(
        "UPDATE release_control.broker_target_state SET telegram_owner_user_id=8 "
        "WHERE target='production'"
    )

    started = await store.start_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation_id=operation.operation_id,
        now=now + timedelta(seconds=1),
    )

    assert started.admitted is True
    operation_state = await store.get_switch_operation(card.approval_id)
    assert operation_state["state"] == "started"


@pytest.mark.asyncio
async def test_start_deadline_prevents_switch_and_recovery_is_bounded(broker_pool):
    store, card, fence, _, _, now, operation = await _merged_release(broker_pool)
    await store.prepare_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation=operation,
        actual_identity=operation.baseline,
        now=now,
    )
    admitted = await store.admit_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation_id=operation.operation_id,
        current_main_sha="f" * 40,
        actual_identity=operation.baseline,
        now=now,
    )
    with pytest.raises(BrokerError) as expired:
        await store.start_switch(
            card.approval_id,
            dispatch_fence=fence,
            operation_id=operation.operation_id,
            now=admitted.start_deadline + timedelta(seconds=1),
        )
    assert expired.value.code == "admission_denied"

    await store.start_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation_id=operation.operation_id,
        now=now + timedelta(seconds=1),
    )
    with pytest.raises(BrokerError) as recovery_expired:
        await store.start_switch(
            card.approval_id,
            dispatch_fence=fence,
            operation_id=operation.operation_id,
            recovery=True,
            now=admitted.recovery_deadline + timedelta(seconds=1),
        )
    assert recovery_expired.value.code == "admission_denied"


@pytest.mark.asyncio
async def test_healthy_receipt_requires_exact_payload_and_replay_does_not_revert_newer_state(
    broker_pool,
):
    store, card, fence, _, _, now, operation = await _merged_release(broker_pool)
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
        receipt_id="host-receipt-1",
        fence=fence,
        status=ReleaseStatus.HEALTHY,
        merge_commit="f" * 40,
        observed_identity=operation.payload,
        observed_at=now + timedelta(seconds=2),
    )

    saved = await store.record_host_receipt(
        card.approval_id, receipt, actual_identity=operation.payload, now=now + timedelta(seconds=3)
    )
    newer = DeploymentIdentity(
        git_sha="f" * 40,
        image_digest="sha256:" + "9" * 64,
        config_generation=5,
        config_digest="7" * 64,
        policy_digest=_policy().digest,
        target_sequence=card.sequence + 1,
    )
    await broker_pool.execute(
        "UPDATE release_control.broker_target_state "
        "SET deployed_identity=$1::jsonb,generation=generation+1 "
        "WHERE target='production'",
        json.dumps(asdict(newer), sort_keys=True),
    )
    replay = await store.record_host_receipt(
        card.approval_id, receipt, actual_identity=operation.payload, now=now + timedelta(minutes=4)
    )
    state = await store.get_target_state("production")

    assert saved.digest == replay.digest
    assert state.identity == newer


@pytest.mark.asyncio
async def test_maintenance_reconciliation_requires_role_and_advances_sequence_atomically(
    broker_pool,
):
    role = "release_control_maintenance"
    await _seed_target(broker_pool)
    await broker_pool.execute(
        "UPDATE release_control.broker_target_state SET reconciliation_required=TRUE "
        "WHERE target='production'"
    )
    async with broker_pool.acquire() as connection:
        role_exists = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=$1)", role
        )
        if role_exists:
            await connection.execute(f'DROP OWNED BY "{role}"')
            await connection.execute(f'REVOKE "{role}" FROM CURRENT_USER')
            await connection.execute(f'DROP ROLE "{role}"')
        await connection.execute(f'CREATE ROLE "{role}" NOLOGIN')
        await connection.execute(f'GRANT "{role}" TO CURRENT_USER')
        await connection.execute(f'GRANT USAGE ON SCHEMA release_control TO "{role}"')
        await connection.execute(
            f"GRANT SELECT, UPDATE ON release_control.target_queue, "
            f"release_control.broker_target_state, release_control.release_card, "
            f'release_control.broker_switch_operation TO "{role}"'
        )
    store = BrokerStore(broker_pool)
    repaired_identity = DeploymentIdentity(
        git_sha="d" * 40,
        image_digest="sha256:" + "e" * 64,
        config_generation=5,
        config_digest="1" * 64,
        policy_digest=_policy().digest,
        target_sequence=4,
    )
    try:
        with pytest.raises(BrokerError) as denied:
            await store.maintenance_reconcile_target(
                "production",
                actual_identity=repaired_identity,
                current_main_sha=repaired_identity.git_sha,
                actual_healthy=True,
                expected_generation=1,
            )
        assert denied.value.code == "caller_denied"

        class _ConnectionContext:
            async def __aenter__(self):
                self.connection = await broker_pool.acquire()
                await self.connection.execute(f'SET ROLE "{role}"')
                return self.connection

            async def __aexit__(self, *_exc):
                await self.connection.execute("RESET ROLE")
                await broker_pool.release(self.connection)

        class _RolePool:
            def acquire(self):
                return _ConnectionContext()

        repaired = await BrokerStore(_RolePool()).maintenance_reconcile_target(
            "production",
            actual_identity=repaired_identity,
            current_main_sha=repaired_identity.git_sha,
            actual_healthy=True,
            expected_generation=1,
        )
        queue = await broker_pool.fetchrow(
            "SELECT next_sequence, active_approval_id FROM release_control.target_queue "
            "WHERE target='production'"
        )
        assert repaired.identity == repaired_identity
        assert repaired.generation == 2
        assert repaired.reconciliation_required is False
        assert queue["next_sequence"] == repaired_identity.target_sequence + 1
        assert queue["active_approval_id"] is None
    finally:
        async with broker_pool.acquire() as connection:
            await connection.execute(f'DROP OWNED BY "{role}"')
            await connection.execute(f'REVOKE "{role}" FROM CURRENT_USER')
            await connection.execute(f'DROP ROLE IF EXISTS "{role}"')
