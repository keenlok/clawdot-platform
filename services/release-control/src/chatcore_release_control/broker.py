"""Fenced, transactional broker state for protected release admission.

This module never installs schema and does not treat a persisted receipt as proof of
an external effect. Callers must supply independently verified identities/evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from .ledger import HostOperationBinding, HostReceipt, LedgerError, ReleaseStatus, RunAttemptBinding
from .policy import DeploymentIdentity, PolicyManifest
from .revocation import is_revoked

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TARGET = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_MAX_PARENTS = 20
MERGE_LEASE_SECONDS = 90
MERGE_START_SECONDS = 60
SWITCH_START_SECONDS = 120
SWITCH_RECOVERY_SECONDS = 600


class BrokerError(RuntimeError):
    """Fixed public broker codes without SQL, upstream or credential diagnostics."""

    _CODES = frozenset(
        {
            "invalid_input",
            "caller_denied",
            "schema_unavailable",
            "target_unavailable",
            "reconciliation_required",
            "stale_fence",
            "admission_denied",
            "binding_mismatch",
            "evidence_denied",
            "lease_expired",
            "operation_conflict",
            "host_state_unavailable",
            "storage_error",
        }
    )

    def __init__(self, code: str = "storage_error") -> None:
        self.code = code if code in self._CODES else "storage_error"
        super().__init__(self.code)


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise BrokerError("invalid_input") from None


def _json(value: object) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, RecursionError):
            raise BrokerError("storage_error") from None
    return value


def _identity(value: object) -> DeploymentIdentity:
    try:
        data = _json(value)
        if not isinstance(data, dict) or set(data) != {
            "git_sha",
            "image_digest",
            "config_generation",
            "config_digest",
            "policy_digest",
            "target_sequence",
        }:
            raise ValueError
        return DeploymentIdentity(**data)
    except Exception:
        raise BrokerError("storage_error") from None


def _policy(value: object) -> PolicyManifest:
    try:
        data = _json(value)
        return PolicyManifest.from_json(_canonical(data))
    except Exception:
        raise BrokerError("storage_error") from None


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BrokerError("invalid_input")
    return value.astimezone(UTC)


def _time_value(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TargetState:
    target: str
    repository: str
    telegram_owner_user_id: int
    policy: PolicyManifest
    workflow_sha: str
    identity: DeploymentIdentity
    generation: int
    reconciliation_required: bool
    pending_operation_id: UUID | None


@dataclass(frozen=True)
class ReleaseContext:
    approval_id: UUID
    repository: str
    repository_id: int
    owner_id: int
    telegram_owner_user_id: int
    pr_number: int
    target: str
    sequence: int
    dispatch_fence: int
    deployed_identity: DeploymentIdentity
    base_sha: str
    head_sha: str
    head_tree: str
    expected_merge_tree: str | None
    policy_digest: str
    policy_manifest: PolicyManifest
    review_bundle: dict[str, Any]
    review_bundle_digest: str
    workflow_sha: str
    status: ReleaseStatus
    merge_commit: str | None
    merge_tree: str | None
    merge_parents: tuple[str, ...] | None
    host_receipt: dict[str, Any] | None
    merge_lease: dict[str, Any] | None

    def to_json_value(self) -> dict[str, Any]:
        return {
            "approval_id": str(self.approval_id),
            "repository": self.repository,
            "repository_id": self.repository_id,
            "owner_id": self.owner_id,
            "pr_number": self.pr_number,
            "target": self.target,
            "sequence": self.sequence,
            "dispatch_fence": self.dispatch_fence,
            "deployed_identity": asdict(self.deployed_identity),
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "head_tree": self.head_tree,
            "policy_digest": self.policy_digest,
            "policy_manifest": _policy_json(self.policy_manifest),
            "review_bundle": self.review_bundle,
            "review_bundle_digest": self.review_bundle_digest,
            "workflow_sha": self.workflow_sha,
            "status": self.status.value,
            "merge_commit": self.merge_commit,
            "merge_tree": self.merge_tree,
            "merge_parents": list(self.merge_parents) if self.merge_parents is not None else None,
            "host_receipt": self.host_receipt,
            "merge_lease": self.merge_lease,
        }


@dataclass(frozen=True)
class MergeLease:
    lease_id: UUID
    approval_id: UUID
    target: str
    dispatch_fence: int
    run: RunAttemptBinding
    created_at: datetime
    expires_at: datetime
    admitted_at: datetime | None = None
    start_deadline: datetime | None = None


@dataclass(frozen=True)
class VerifiedAdmissionEvidence:
    """Evidence assembled by the trusted Git/P04 verifier, never from request JSON."""

    deployed_identity: DeploymentIdentity
    base_sha: str
    head_sha: str
    head_tree: str
    current_main_sha: str
    expected_merge_tree: str
    evidence_digest: str
    complete_delta_reviewed: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.deployed_identity, DeploymentIdentity)
            or any(
                not isinstance(value, str) or not _SHA.fullmatch(value)
                for value in (
                    self.base_sha,
                    self.head_sha,
                    self.head_tree,
                    self.current_main_sha,
                    self.expected_merge_tree,
                )
            )
            or not isinstance(self.evidence_digest, str)
            or not _DIGEST.fullmatch(self.evidence_digest)
            or type(self.complete_delta_reviewed) is not bool
        ):
            raise BrokerError("evidence_denied")


@dataclass(frozen=True)
class MergeAdmission:
    approval_id: UUID
    lease_id: UUID
    dispatch_fence: int
    expires_at: datetime
    admitted: bool
    may_start: bool
    deployed_identity: DeploymentIdentity
    base_sha: str
    head_sha: str
    head_tree: str
    policy_digest: str
    expected_merge_tree: str
    run: RunAttemptBinding

    def to_json_value(self) -> dict[str, Any]:
        return {
            "approval_id": str(self.approval_id),
            "lease_id": str(self.lease_id),
            "dispatch_fence": self.dispatch_fence,
            "expires_at": self.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "admitted": self.admitted,
            "may_start": self.may_start,
            "deployed_identity": asdict(self.deployed_identity),
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "head_tree": self.head_tree,
            "policy_digest": self.policy_digest,
            "expected_merge_tree": self.expected_merge_tree,
            "run_id": self.run.run_id,
            "run_attempt": self.run.attempt,
            "check_run_id": self.run.check_run_id,
        }


@dataclass(frozen=True)
class VerifiedMergeObservation:
    commit: str
    parents: tuple[str, ...]
    tree: str
    expected_tree: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.commit, str)
            or not _SHA.fullmatch(self.commit)
            or not isinstance(self.parents, tuple)
            or len(self.parents) > _MAX_PARENTS
            or any(
                not isinstance(parent, str) or not _SHA.fullmatch(parent) for parent in self.parents
            )
            or not isinstance(self.tree, str)
            or not _SHA.fullmatch(self.tree)
            or not isinstance(self.expected_tree, str)
            or not _SHA.fullmatch(self.expected_tree)
        ):
            raise BrokerError("evidence_denied")
        _utc(self.observed_at)


@dataclass(frozen=True)
class SwitchAdmission:
    approval_id: UUID
    target: str
    operation_id: UUID
    dispatch_fence: int
    admitted: bool
    may_start: bool
    start_deadline: datetime
    recovery_deadline: datetime
    baseline_identity: DeploymentIdentity
    payload_identity: DeploymentIdentity

    def to_json_value(self) -> dict[str, Any]:
        return {
            "approval_id": str(self.approval_id),
            "target": self.target,
            "operation_id": str(self.operation_id),
            "dispatch_fence": self.dispatch_fence,
            "admitted": self.admitted,
            "may_start": self.may_start,
            "start_deadline": self.start_deadline.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "recovery_deadline": self.recovery_deadline.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "baseline_identity": asdict(self.baseline_identity),
            "payload_identity": asdict(self.payload_identity),
        }


def _policy_json(policy: PolicyManifest) -> dict[str, Any]:
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


class BrokerStore:
    """Transactional SQL core; schema installation and target bootstrap are external."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        if pool is None:
            raise BrokerError("invalid_input")
        self._pool = pool

    @asynccontextmanager
    async def _connection(self):
        try:
            async with self._pool.acquire() as connection:
                yield connection
        except BrokerError:
            raise
        except Exception:
            raise BrokerError("storage_error") from None

    async def schema_ready(self) -> bool:
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    """SELECT version FROM release_control.broker_schema_version
                       WHERE singleton IS TRUE"""
                )
                if row is None or row["version"] != 1:
                    return False
                expected = {
                    "broker_target_state",
                    "broker_policy_snapshot",
                    "broker_merge_lease",
                    "broker_merge_admission",
                    "broker_merge_observation",
                    "broker_switch_operation",
                    "broker_release_revocation",
                }
                rows = await connection.fetch(
                    """SELECT tablename FROM pg_catalog.pg_tables
                       WHERE schemaname='release_control'"""
                )
                return expected <= {row["tablename"] for row in rows}
        except Exception:
            return False

    async def target_ready(self, target: str) -> bool:
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            return False
        try:
            state = await self.get_target_state(target)
            return not state.reconciliation_required
        except BrokerError:
            return False

    async def maintenance_reconcile_target(
        self,
        target: str,
        *,
        actual_identity: DeploymentIdentity,
        current_main_sha: str,
        actual_healthy: bool,
        expected_generation: int,
        now: datetime | None = None,
    ) -> TargetState:
        """Reconcile an inspected target from a separately granted maintenance DB role.

        This operation is deliberately absent from the ASGI route table. The caller must
        hold the `release_control_maintenance` database role and supply fresh protected
        host/GitHub observations gathered outside this short database transaction.
        """
        current = _utc(now or datetime.now(UTC))
        if (
            not isinstance(target, str)
            or not _TARGET.fullmatch(target)
            or not isinstance(actual_identity, DeploymentIdentity)
            or not isinstance(current_main_sha, str)
            or not _SHA.fullmatch(current_main_sha)
            or type(actual_healthy) is not bool
            or type(expected_generation) is not int
            or expected_generation <= 0
        ):
            raise BrokerError("invalid_input")
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    role = await connection.fetchval(
                        """SELECT current_user = 'release_control_maintenance'
                                  AND NOT COALESCE((SELECT rolsuper FROM pg_catalog.pg_roles
                                                    WHERE rolname=current_user), TRUE)"""
                    )
                    if role is not True:
                        raise BrokerError("caller_denied")
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        target,
                    )
                    state = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_target_state "
                        "WHERE target=$1 FOR UPDATE",
                        target,
                    )
                    if queue is None or state is None:
                        raise BrokerError("target_unavailable")
                    if state["generation"] != expected_generation:
                        raise BrokerError("stale_fence")
                    if (
                        not state["reconciliation_required"]
                        or not actual_healthy
                        or current_main_sha != actual_identity.git_sha
                        or actual_identity.policy_digest != state["policy_digest"]
                    ):
                        raise BrokerError("admission_denied")

                    pending_id = state["pending_operation_id"]
                    terminal_recovery = False
                    if pending_id is not None:
                        operation = await connection.fetchrow(
                            "SELECT * FROM release_control.broker_switch_operation "
                            "WHERE operation_id=$1 FOR UPDATE",
                            pending_id,
                        )
                        terminal_recovery = bool(
                            operation is not None
                            and operation["state"] == ReleaseStatus.RECOVERY_FAILED.value
                            and operation["receipt_id"] is not None
                        )
                        if not terminal_recovery:
                            raise BrokerError("reconciliation_required")

                    active_id = queue["active_approval_id"]
                    if active_id is not None:
                        card = await connection.fetchrow(
                            "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                            active_id,
                        )
                        anomaly_merged = False
                        if card is not None and card["merge_anomaly"] is not None:
                            anomaly = _json(card["merge_anomaly"])
                            anomaly_merged = bool(
                                card["status"] == ReleaseStatus.MERGED_BLOCKED.value
                                and isinstance(anomaly, dict)
                                and anomaly.get("merge_commit") == current_main_sha
                            )
                        recovery_merged = bool(
                            terminal_recovery
                            and card is not None
                            and card["status"] == ReleaseStatus.RECOVERY_FAILED.value
                            and card["host_receipt_id"] is not None
                            and card["merge_commit"] == current_main_sha
                        )
                        if (
                            card is None
                            or card["fence"] != queue["active_fence"]
                            or card["sequence"] > actual_identity.target_sequence
                            or not (anomaly_merged or recovery_merged)
                        ):
                            raise BrokerError("reconciliation_required")

                    await connection.execute(
                        """UPDATE release_control.broker_target_state
                           SET deployed_identity=$2::jsonb,generation=generation+1,
                               pending_operation_id=NULL,reconciliation_required=FALSE,updated_at=$3
                           WHERE target=$1""",
                        target,
                        _canonical(asdict(actual_identity)),
                        current,
                    )
                    await connection.execute(
                        """UPDATE release_control.target_queue
                           SET next_sequence=GREATEST(next_sequence,$2),
                               active_approval_id=NULL,active_fence=NULL,active_until=NULL
                           WHERE target=$1""",
                        target,
                        actual_identity.target_sequence + 1,
                    )
                    row = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_target_state WHERE target=$1",
                        target,
                    )
                    return TargetState(
                        target=target,
                        repository=row["repository"],
                        telegram_owner_user_id=row["telegram_owner_user_id"],
                        policy=_policy(row["policy_manifest"]),
                        workflow_sha=row["workflow_sha"],
                        identity=_identity(row["deployed_identity"]),
                        generation=row["generation"],
                        reconciliation_required=row["reconciliation_required"],
                        pending_operation_id=row["pending_operation_id"],
                    )
            except BrokerError:
                raise
            except Exception:
                raise BrokerError("storage_error") from None

    async def get_target_state(self, target: str) -> TargetState:
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            raise BrokerError("invalid_input")
        if not await self.schema_ready():
            raise BrokerError("schema_unavailable")
        async with self._connection() as connection:
            try:
                row = await connection.fetchrow(
                    "SELECT * FROM release_control.broker_target_state WHERE target=$1", target
                )
                if row is None:
                    raise BrokerError("target_unavailable")
                operation_id = row["pending_operation_id"]
                return TargetState(
                    target=target,
                    repository=row["repository"],
                    telegram_owner_user_id=row["telegram_owner_user_id"],
                    policy=_policy(row["policy_manifest"]),
                    workflow_sha=row["workflow_sha"],
                    identity=_identity(row["deployed_identity"]),
                    generation=row["generation"],
                    reconciliation_required=row["reconciliation_required"],
                    pending_operation_id=operation_id,
                )
            except BrokerError:
                raise
            except Exception:
                raise BrokerError("storage_error") from None

    async def find_active_release_for_run(
        self, target: str, *, run_id: int, run_attempt: int, check_run_id: int
    ) -> UUID:
        if (
            not isinstance(target, str)
            or not _TARGET.fullmatch(target)
            or any(
                type(value) is not int or value <= 0
                for value in (run_id, run_attempt, check_run_id)
            )
        ):
            raise BrokerError("invalid_input")
        async with self._connection() as connection:
            try:
                row = await connection.fetchrow(
                    """SELECT c.id FROM release_control.release_card c
                       JOIN release_control.target_queue q ON q.target=c.target
                       WHERE c.target=$1 AND c.run_id=$2 AND c.run_attempt=$3
                         AND c.check_run_id=$4 AND c.id=q.active_approval_id
                         AND c.fence=q.active_fence""",
                    target,
                    run_id,
                    run_attempt,
                    check_run_id,
                )
                if row is None:
                    raise BrokerError("caller_denied")
                return row["id"]
            except BrokerError:
                raise
            except Exception:
                raise BrokerError("storage_error") from None

    async def expected_merge_tree(self, approval_id: UUID, lease_id: UUID) -> str:
        if not isinstance(approval_id, UUID) or not isinstance(lease_id, UUID):
            raise BrokerError("invalid_input")
        async with self._connection() as connection:
            try:
                row = await connection.fetchrow(
                    "SELECT expected_merge_tree FROM release_control.broker_merge_admission "
                    "WHERE approval_id=$1 AND lease_id=$2",
                    approval_id,
                    lease_id,
                )
                if row is None:
                    raise BrokerError("admission_denied")
                return row["expected_merge_tree"]
            except BrokerError:
                raise
            except Exception:
                raise BrokerError("storage_error") from None

    async def get_switch_operation(self, approval_id: UUID) -> dict[str, Any] | None:
        if not isinstance(approval_id, UUID):
            raise BrokerError("invalid_input")
        async with self._connection() as connection:
            try:
                row = await connection.fetchrow(
                    "SELECT * FROM release_control.broker_switch_operation WHERE approval_id=$1",
                    approval_id,
                )
                if row is None:
                    return None
                return {
                    "operation_id": str(row["operation_id"]),
                    "dispatch_fence": row["dispatch_fence"],
                    "baseline_identity": asdict(_identity(row["baseline_identity"])),
                    "payload_identity": asdict(_identity(row["payload_identity"])),
                    "payload_digest": bytes(row["payload_digest"]).hex(),
                    "state": row["state"],
                    "start_deadline": _time_value(row["start_deadline"]),
                    "recovery_deadline": _time_value(row["recovery_deadline"]),
                    "receipt": _json(row["receipt"]) if row["receipt"] is not None else None,
                }
            except BrokerError:
                raise
            except Exception:
                raise BrokerError("storage_error") from None

    async def record_merge_observation(
        self,
        approval_id: UUID,
        *,
        dispatch_fence: int,
        lease_id: UUID,
        run: RunAttemptBinding,
        observation: VerifiedMergeObservation,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist an independently fetched GitHub object, including anomalous parents."""
        current = _utc(now or datetime.now(UTC))
        if (
            not isinstance(approval_id, UUID)
            or type(dispatch_fence) is not int
            or dispatch_fence <= 0
            or not isinstance(lease_id, UUID)
            or not isinstance(run, RunAttemptBinding)
            or not isinstance(observation, VerifiedMergeObservation)
            or observation.observed_at > current
        ):
            raise BrokerError("invalid_input")
        observed = {
            "merge_commit": observation.commit,
            "parents": list(observation.parents),
            "tree": observation.tree,
            "expected_tree": observation.expected_tree,
            "observed_at": observation.observed_at.astimezone(UTC).isoformat(),
        }
        observation_digest = hashlib.sha256(_canonical(observed).encode()).digest()
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    preliminary = await connection.fetchrow(
                        "SELECT target FROM release_control.release_card WHERE id=$1", approval_id
                    )
                    if preliminary is None:
                        raise BrokerError("target_unavailable")
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    card = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                        approval_id,
                    )
                    state = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_target_state "
                        "WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    lease = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_merge_lease WHERE lease_id=$1",
                        lease_id,
                    )
                    admission = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_merge_admission WHERE approval_id=$1",
                        approval_id,
                    )
                    if (
                        lease is None
                        or admission is None
                        or lease["approval_id"] != approval_id
                        or lease["dispatch_fence"] != dispatch_fence
                        or lease["repository_id"] != run.repository_id
                        or lease["run_id"] != run.run_id
                        or lease["run_attempt"] != run.attempt
                        or lease["check_run_id"] != run.check_run_id
                        or admission["dispatch_fence"] != dispatch_fence
                        or admission["lease_id"] != lease_id
                        or card["fence"] != dispatch_fence
                    ):
                        raise BrokerError("stale_fence")
                    binding = _json(card["binding"])
                    expected = admission["expected_merge_tree"]
                    if observation.expected_tree != expected:
                        raise BrokerError("binding_mismatch")
                    valid = (
                        len(observation.parents) == 2
                        and observation.parents == (binding["base_sha"], binding["head_sha"])
                        and observation.commit not in observation.parents
                        and observation.tree == expected
                    )
                    reason = (
                        "verified_two_parent"
                        if valid
                        else "tree_mismatch"
                        if observation.tree != expected
                        else "parent_mismatch"
                    )
                    await connection.execute(
                        """INSERT INTO release_control.broker_merge_observation
                           (approval_id,target,dispatch_fence,lease_id,merge_commit,parents,
                            merge_tree,expected_tree,reason,observation_digest,observed_at)
                           VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11)
                           ON CONFLICT (approval_id,dispatch_fence,observation_digest)
                           DO NOTHING""",
                        approval_id,
                        card["target"],
                        dispatch_fence,
                        lease_id,
                        observation.commit,
                        _canonical(list(observation.parents)),
                        observation.tree,
                        expected,
                        reason,
                        observation_digest,
                        observation.observed_at,
                    )
                    current_fence = (
                        queue["active_approval_id"] == approval_id
                        and queue["active_fence"] == dispatch_fence
                    )
                    first_record = (
                        card["merge_recorded_at"] is None and card["merge_anomaly"] is None
                    )
                    same_verified = (
                        valid
                        and card["merge_recorded_at"] is not None
                        and card["expected_merge_tree"] == expected
                        and card["merge_commit"] == observation.commit
                        and card["merge_tree"] == observation.tree
                        and (card["merge_parent_base"], card["merge_parent_head"])
                        == observation.parents
                    )
                    if same_verified:
                        return {
                            "recorded": True,
                            "status": "merged_verified",
                            "observation_count": await connection.fetchval(
                                "SELECT count(*) FROM release_control.broker_merge_observation "
                                "WHERE approval_id=$1",
                                approval_id,
                            ),
                            "reconciliation_required": bool(state["reconciliation_required"]),
                        }
                    if (
                        valid
                        and first_record
                        and current_fence
                        and card["status"] == ReleaseStatus.MERGE_AUTHORIZED.value
                    ):
                        await connection.execute(
                            """UPDATE release_control.release_card SET
                               expected_merge_tree=$2, merge_commit=$3, merge_tree=$4,
                               merge_parent_base=$5, merge_parent_head=$6, merge_recorded_at=$7,
                               status='merged_verified'
                               WHERE id=$1""",
                            approval_id,
                            expected,
                            observation.commit,
                            observation.tree,
                            observation.parents[0],
                            observation.parents[1],
                            observation.observed_at,
                        )
                    else:
                        if first_record:
                            await connection.execute(
                                """UPDATE release_control.release_card
                                   SET merge_anomaly=$2::jsonb,status='merged_blocked'
                                   WHERE id=$1 AND status='merge_authorized'""",
                                approval_id,
                                _canonical(observed),
                            )
                        elif card["status"] in {
                            ReleaseStatus.MERGE_AUTHORIZED.value,
                            ReleaseStatus.MERGED_VERIFIED.value,
                        }:
                            await connection.execute(
                                "UPDATE release_control.release_card SET status='merged_blocked' "
                                "WHERE id=$1",
                                approval_id,
                            )
                        await connection.execute(
                            """UPDATE release_control.broker_target_state
                               SET reconciliation_required=TRUE,updated_at=$2
                               WHERE target=$1""",
                            preliminary["target"],
                            current,
                        )
                    count = await connection.fetchval(
                        "SELECT count(*) FROM release_control.broker_merge_observation "
                        "WHERE approval_id=$1",
                        approval_id,
                    )
                    return {
                        "recorded": True,
                        "status": "merged_verified"
                        if valid and current_fence and first_record
                        else "merged_blocked",
                        "observation_count": count,
                        "reconciliation_required": bool(state["reconciliation_required"])
                        or not (valid and current_fence and first_record),
                    }
            except BrokerError:
                raise
            except Exception:
                raise BrokerError("storage_error") from None

    async def prepare_switch(
        self,
        approval_id: UUID,
        *,
        dispatch_fence: int,
        operation: HostOperationBinding,
        actual_identity: DeploymentIdentity,
        now: datetime | None = None,
    ) -> HostOperationBinding:
        """Bind a host-prepared payload to exact current D and verified M; no switch grant."""
        current = _utc(now or datetime.now(UTC))
        if (
            not isinstance(approval_id, UUID)
            or type(dispatch_fence) is not int
            or dispatch_fence <= 0
            or not isinstance(operation, HostOperationBinding)
            or not isinstance(actual_identity, DeploymentIdentity)
        ):
            raise BrokerError("invalid_input")
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    preliminary = await connection.fetchrow(
                        "SELECT target FROM release_control.release_card WHERE id=$1", approval_id
                    )
                    if preliminary is None:
                        raise BrokerError("target_unavailable")
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    card = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                        approval_id,
                    )
                    state = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_target_state "
                        "WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    if state is None:
                        raise BrokerError("target_unavailable")
                    binding = _json(card["binding"])
                    operation_row = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_switch_operation "
                        "WHERE approval_id=$1 FOR UPDATE",
                        approval_id,
                    )
                    if operation_row is not None:
                        existing = _switch_binding(operation_row)
                        if existing != operation:
                            raise BrokerError("operation_conflict")
                        return existing
                    if (
                        queue["active_approval_id"] != approval_id
                        or queue["active_fence"] != dispatch_fence
                        or card["fence"] != dispatch_fence
                    ):
                        raise BrokerError("stale_fence")
                    baseline = _identity(binding["deployed_identity"])
                    payload = operation.payload
                    if (
                        card["status"] != ReleaseStatus.MERGED_VERIFIED.value
                        or state["reconciliation_required"]
                        or state["pending_operation_id"] is not None
                        or state["policy_digest"] != binding["policy_digest"]
                        or state["telegram_owner_user_id"] != card["owner_user_id"]
                        or _identity(state["deployed_identity"]) != baseline
                        or card["sequence"] <= baseline.target_sequence
                        or actual_identity != baseline
                        or operation.baseline != baseline
                        or card["merge_recorded_at"] is None
                        or payload.git_sha != card["merge_commit"]
                        or payload.policy_digest != binding["policy_digest"]
                        or payload.target_sequence != card["sequence"]
                        or payload.config_generation != baseline.config_generation
                        or payload.config_digest != baseline.config_digest
                    ):
                        raise BrokerError("admission_denied")
                    if card["host_operation_id"] is not None:
                        raise BrokerError("operation_conflict")
                    payload_data = asdict(payload)
                    await connection.execute(
                        """UPDATE release_control.release_card SET
                           host_operation_id=$2, host_baseline_identity=$3::jsonb,
                           host_payload_identity=$4::jsonb, host_payload_sha256=$5
                           WHERE id=$1""",
                        approval_id,
                        operation.operation_id,
                        _canonical(asdict(baseline)),
                        _canonical(payload_data),
                        bytes.fromhex(operation.payload_digest),
                    )
                    await connection.execute(
                        """INSERT INTO release_control.broker_switch_operation
                           (operation_id,approval_id,target,dispatch_fence,baseline_identity,
                            payload_identity,payload_digest,state,prepared_at)
                           VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,'prepared',$8)""",
                        operation.operation_id,
                        approval_id,
                        card["target"],
                        dispatch_fence,
                        _canonical(asdict(baseline)),
                        _canonical(payload_data),
                        bytes.fromhex(operation.payload_digest),
                        current,
                    )
                    return operation
            except BrokerError:
                raise
            except LedgerError:
                raise BrokerError("admission_denied") from None
            except Exception:
                raise BrokerError("storage_error") from None

    async def admit_switch(
        self,
        approval_id: UUID,
        *,
        dispatch_fence: int,
        operation_id: UUID,
        current_main_sha: str,
        actual_identity: DeploymentIdentity,
        now: datetime | None = None,
    ) -> SwitchAdmission:
        current = _utc(now or datetime.now(UTC))
        if (
            not isinstance(approval_id, UUID)
            or type(dispatch_fence) is not int
            or dispatch_fence <= 0
            or not isinstance(operation_id, UUID)
            or not isinstance(current_main_sha, str)
            or not _SHA.fullmatch(current_main_sha)
            or not isinstance(actual_identity, DeploymentIdentity)
        ):
            raise BrokerError("invalid_input")
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    preliminary = await connection.fetchrow(
                        "SELECT target FROM release_control.release_card WHERE id=$1", approval_id
                    )
                    if preliminary is None:
                        raise BrokerError("target_unavailable")
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    card = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                        approval_id,
                    )
                    state = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_target_state "
                        "WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    operation = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_switch_operation "
                        "WHERE operation_id=$1 FOR UPDATE",
                        operation_id,
                    )
                    if operation is None or operation["approval_id"] != approval_id:
                        raise BrokerError("operation_conflict")
                    if (
                        operation["dispatch_fence"] != dispatch_fence
                        or card["fence"] != dispatch_fence
                    ):
                        raise BrokerError("stale_fence")
                    if operation["state"] in {
                        "admitted",
                        "started",
                        "healthy",
                        "restored_healthy",
                        "recovery_failed",
                    }:
                        return _switch_admission(operation, current)
                    if await is_revoked(connection, approval_id):
                        raise BrokerError("admission_denied")
                    binding = _json(card["binding"])
                    baseline = _identity(operation["baseline_identity"])
                    payload = _identity(operation["payload_identity"])
                    if (
                        operation["state"] != "prepared"
                        or card["status"] != ReleaseStatus.MERGED_VERIFIED.value
                        or card["decision"] != "approve"
                        or card["expires_at"] <= current
                        or queue["active_approval_id"] != approval_id
                        or queue["active_fence"] != dispatch_fence
                        or state["reconciliation_required"]
                        or state["pending_operation_id"] is not None
                        or state["telegram_owner_user_id"] != card["owner_user_id"]
                        or state["policy_digest"] != binding["policy_digest"]
                        or _identity(state["deployed_identity"]) != baseline
                        or actual_identity != baseline
                        or baseline != _identity(binding["deployed_identity"])
                        or card["sequence"] <= baseline.target_sequence
                        or current_main_sha != card["merge_commit"]
                        or payload.git_sha != card["merge_commit"]
                        or payload.policy_digest != binding["policy_digest"]
                        or payload.target_sequence != card["sequence"]
                    ):
                        raise BrokerError("admission_denied")
                    start_deadline = current + timedelta(seconds=SWITCH_START_SECONDS)
                    recovery_deadline = current + timedelta(seconds=SWITCH_RECOVERY_SECONDS)
                    await connection.execute(
                        """UPDATE release_control.broker_switch_operation
                           SET state='admitted',admitted_at=$2,start_deadline=$3,
                               recovery_deadline=$4
                           WHERE operation_id=$1""",
                        operation_id,
                        current,
                        start_deadline,
                        recovery_deadline,
                    )
                    await connection.execute(
                        """UPDATE release_control.broker_target_state
                           SET pending_operation_id=$2,reconciliation_required=TRUE,updated_at=$3
                           WHERE target=$1""",
                        card["target"],
                        operation_id,
                        current,
                    )
                    await connection.execute(
                        "UPDATE release_control.release_card SET status='deploying' WHERE id=$1",
                        approval_id,
                    )
                    operation = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_switch_operation "
                        "WHERE operation_id=$1",
                        operation_id,
                    )
                    return _switch_admission(operation, current)
            except BrokerError:
                raise
            except Exception:
                raise BrokerError("storage_error") from None

    async def start_switch(
        self,
        approval_id: UUID,
        *,
        dispatch_fence: int,
        operation_id: UUID,
        recovery: bool = False,
        now: datetime | None = None,
    ) -> SwitchAdmission:
        current = _utc(now or datetime.now(UTC))
        if (
            not isinstance(approval_id, UUID)
            or type(dispatch_fence) is not int
            or dispatch_fence <= 0
            or not isinstance(operation_id, UUID)
            or type(recovery) is not bool
        ):
            raise BrokerError("invalid_input")
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    card = await connection.fetchrow(
                        "SELECT target FROM release_control.release_card WHERE id=$1", approval_id
                    )
                    if card is None:
                        raise BrokerError("target_unavailable")
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        card["target"],
                    )
                    release = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                        approval_id,
                    )
                    operation = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_switch_operation "
                        "WHERE operation_id=$1 FOR UPDATE",
                        operation_id,
                    )
                    if (
                        operation is None
                        or operation["approval_id"] != approval_id
                        or operation["dispatch_fence"] != dispatch_fence
                        or release["fence"] != dispatch_fence
                    ):
                        raise BrokerError("stale_fence")
                    if operation["state"] in {"healthy", "restored_healthy", "recovery_failed"}:
                        return _switch_admission(operation, current)
                    target_status = (
                        ReleaseStatus.RESTORING.value if recovery else ReleaseStatus.DEPLOYING.value
                    )
                    if operation["state"] == "started" and release["status"] == target_status:
                        return _switch_admission(operation, current)
                    deadline = (
                        operation["recovery_deadline"] if recovery else operation["start_deadline"]
                    )
                    if (
                        operation["state"] not in {"admitted", "started"}
                        or operation["start_deadline"] is None
                        or deadline <= current
                        or queue["active_approval_id"] != approval_id
                        or queue["active_fence"] != dispatch_fence
                        or not recovery
                        and operation["started_at"] is not None
                    ):
                        raise BrokerError("admission_denied")
                    await connection.execute(
                        "UPDATE release_control.broker_switch_operation "
                        "SET state='started',started_at=COALESCE(started_at,$2) "
                        "WHERE operation_id=$1",
                        operation_id,
                        current,
                    )
                    await connection.execute(
                        "UPDATE release_control.release_card SET status=$2 WHERE id=$1",
                        approval_id,
                        target_status,
                    )
                    operation = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_switch_operation "
                        "WHERE operation_id=$1",
                        operation_id,
                    )
                    return _switch_admission(operation, current)
            except BrokerError:
                raise
            except Exception:
                raise BrokerError("storage_error") from None

    async def record_host_receipt(
        self,
        approval_id: UUID,
        receipt: HostReceipt,
        *,
        actual_identity: DeploymentIdentity,
        now: datetime | None = None,
    ) -> HostReceipt:
        current = _utc(now or datetime.now(UTC))
        if (
            not isinstance(approval_id, UUID)
            or not isinstance(receipt, HostReceipt)
            or not isinstance(actual_identity, DeploymentIdentity)
            or receipt.approval_id != approval_id
            or receipt.observed_at > current
        ):
            raise BrokerError("invalid_input")
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    preliminary = await connection.fetchrow(
                        "SELECT target FROM release_control.release_card WHERE id=$1", approval_id
                    )
                    if preliminary is None:
                        raise BrokerError("target_unavailable")
                    await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    card = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                        approval_id,
                    )
                    operation = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_switch_operation "
                        "WHERE operation_id=$1 FOR UPDATE",
                        receipt.operation_id,
                    )
                    state = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_target_state "
                        "WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    if operation is None or operation["approval_id"] != approval_id:
                        raise BrokerError("operation_conflict")
                    if operation["receipt_id"] is not None:
                        if operation["receipt_digest"] != bytes.fromhex(receipt.digest):
                            raise BrokerError("operation_conflict")
                        return receipt
                    if (
                        receipt.target != card["target"]
                        or receipt.fence != operation["dispatch_fence"]
                        or receipt.fence != card["fence"]
                        or receipt.merge_commit != card["merge_commit"]
                        or card["host_operation_id"] != receipt.operation_id
                        or state["pending_operation_id"] != receipt.operation_id
                        or operation["state"] not in {"admitted", "started"}
                    ):
                        raise BrokerError("stale_fence")
                    baseline = _identity(operation["baseline_identity"])
                    payload = _identity(operation["payload_identity"])
                    if receipt.status is ReleaseStatus.HEALTHY:
                        if receipt.observed_identity != payload or actual_identity != payload:
                            raise BrokerError("binding_mismatch")
                    elif receipt.status is ReleaseStatus.RESTORED_HEALTHY:
                        if (
                            receipt.observed_identity != baseline
                            or actual_identity != baseline
                            or card["status"] != ReleaseStatus.RESTORING.value
                        ):
                            raise BrokerError("binding_mismatch")
                    elif receipt.status is ReleaseStatus.RECOVERY_FAILED:
                        if (
                            receipt.observed_identity is None
                            or receipt.observed_identity != actual_identity
                        ):
                            raise BrokerError("binding_mismatch")
                    else:
                        raise BrokerError("invalid_input")
                    allowed = {
                        ReleaseStatus.HEALTHY: {ReleaseStatus.DEPLOYING.value},
                        ReleaseStatus.RESTORED_HEALTHY: {ReleaseStatus.RESTORING.value},
                        ReleaseStatus.RECOVERY_FAILED: {
                            ReleaseStatus.DEPLOYING.value,
                            ReleaseStatus.RESTORING.value,
                        },
                    }
                    if card["status"] not in allowed[receipt.status]:
                        raise BrokerError("admission_denied")
                    encoded = receipt.to_json()
                    status = receipt.status.value
                    await connection.execute(
                        """UPDATE release_control.release_card SET host_receipt_id=$2,
                           host_receipt_sha256=$3,host_receipt=$4::jsonb,host_recorded_at=$5,status=$6
                           WHERE id=$1""",
                        approval_id,
                        receipt.receipt_id,
                        bytes.fromhex(receipt.digest),
                        encoded,
                        current,
                        status,
                    )
                    await connection.execute(
                        "UPDATE release_control.broker_switch_operation "
                        "SET state=$2,receipt_id=$3,receipt_digest=$4,receipt=$5::jsonb,"
                        "terminal_at=$6 WHERE operation_id=$1",
                        receipt.operation_id,
                        status,
                        receipt.receipt_id,
                        bytes.fromhex(receipt.digest),
                        encoded,
                        current,
                    )
                    if receipt.status in {ReleaseStatus.HEALTHY, ReleaseStatus.RESTORED_HEALTHY}:
                        new_identity = receipt.observed_identity
                        await connection.execute(
                            """UPDATE release_control.broker_target_state
                               SET deployed_identity=$2::jsonb,
                               generation=generation+1,pending_operation_id=NULL,
                               reconciliation_required=FALSE,updated_at=$3 WHERE target=$1""",
                            card["target"],
                            _canonical(asdict(new_identity)),
                            current,
                        )
                        await connection.execute(
                            """UPDATE release_control.target_queue SET active_approval_id=NULL,
                               active_fence=NULL,active_until=NULL WHERE target=$1
                               AND active_approval_id=$2 AND active_fence=$3""",
                            card["target"],
                            approval_id,
                            receipt.fence,
                        )
                    else:
                        await connection.execute(
                            """UPDATE release_control.broker_target_state
                               SET deployed_identity=$2::jsonb,
                               reconciliation_required=TRUE,updated_at=$3 WHERE target=$1""",
                            card["target"],
                            _canonical(asdict(actual_identity)),
                            current,
                        )
                    return receipt
            except BrokerError:
                raise
            except LedgerError:
                raise BrokerError("binding_mismatch") from None
            except Exception:
                raise BrokerError("storage_error") from None

    async def get_release_context(
        self, approval_id: UUID, *, now: datetime | None = None
    ) -> ReleaseContext:
        if not isinstance(approval_id, UUID):
            raise BrokerError("invalid_input")
        current = _utc(now or datetime.now(UTC))
        if not await self.schema_ready():
            raise BrokerError("schema_unavailable")
        async with self._connection() as connection:
            try:
                row = await connection.fetchrow(
                    """SELECT c.*, q.active_approval_id, q.active_fence,
                              s.telegram_owner_user_id, p.repository, p.workflow_sha,
                              p.policy_digest AS card_policy_digest, p.policy_manifest,
                              l.lease_id, l.run_id AS lease_run_id,
                              l.run_attempt AS lease_run_attempt,
                              l.check_run_id AS lease_check_run_id,
                              l.expires_at AS lease_expires_at, l.released_at AS lease_released_at,
                              a.admitted_at, a.start_deadline,
                              a.expected_merge_tree AS admitted_merge_tree
                       FROM release_control.release_card c
                       JOIN release_control.target_queue q ON q.target=c.target
                       JOIN release_control.broker_target_state s ON s.target=c.target
                       JOIN release_control.broker_policy_snapshot p
                         ON p.target=c.target AND p.policy_digest=c.binding->>'policy_digest'
                       LEFT JOIN release_control.broker_merge_lease l
                         ON l.approval_id=c.id AND l.dispatch_fence=c.fence
                        AND l.released_at IS NULL
                       LEFT JOIN release_control.broker_merge_admission a
                         ON a.approval_id=c.id AND a.lease_id=l.lease_id
                       WHERE c.id=$1""",
                    approval_id,
                )
                if row is None:
                    raise BrokerError("target_unavailable")
                binding = _json(row["binding"])
                policy = _policy(row["policy_manifest"])
                if policy.digest != row["card_policy_digest"] or policy.digest != binding.get(
                    "policy_digest"
                ):
                    raise BrokerError("reconciliation_required")
                merge_parents = None
                merge_commit = row["merge_commit"]
                merge_tree = row["merge_tree"]
                if merge_commit is not None:
                    merge_parents = (row["merge_parent_base"], row["merge_parent_head"])
                elif row["merge_anomaly"] is not None:
                    anomaly = _json(row["merge_anomaly"])
                    merge_commit = anomaly.get("merge_commit")
                    merge_tree = anomaly.get("tree")
                    merge_parents = tuple(anomaly.get("parents", []))
                receipt = _json(row["host_receipt"]) if row["host_receipt"] is not None else None
                lease = None
                if row["lease_id"] is not None:
                    lease = {
                        "lease_id": str(row["lease_id"]),
                        "fence": row["fence"],
                        "expires_at": (
                            row["start_deadline"] if row["admitted_at"] else row["lease_expires_at"]
                        )
                        .astimezone(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "admitted_at": row["admitted_at"]
                        .astimezone(UTC)
                        .isoformat()
                        .replace("+00:00", "Z")
                        if row["admitted_at"]
                        else None,
                        "run_id": row["lease_run_id"],
                        "run_attempt": row["lease_run_attempt"],
                        "check_run_id": row["lease_check_run_id"],
                        "may_start": row["admitted_at"] is not None
                        and row["start_deadline"] > current,
                    }
                return ReleaseContext(
                    approval_id=approval_id,
                    repository=row["repository"],
                    repository_id=policy.repository_id,
                    owner_id=policy.owner_id,
                    telegram_owner_user_id=row["telegram_owner_user_id"],
                    pr_number=binding["pr_number"],
                    target=row["target"],
                    sequence=row["sequence"],
                    dispatch_fence=row["fence"],
                    deployed_identity=_identity(binding["deployed_identity"]),
                    base_sha=binding["base_sha"],
                    head_sha=binding["head_sha"],
                    head_tree=binding["head_tree"],
                    expected_merge_tree=(row["expected_merge_tree"] or row["admitted_merge_tree"]),
                    policy_digest=binding["policy_digest"],
                    policy_manifest=policy,
                    review_bundle=binding["review_bundle"],
                    review_bundle_digest=binding["review_bundle_digest"],
                    workflow_sha=row["workflow_sha"],
                    status=ReleaseStatus(row["status"]),
                    merge_commit=merge_commit,
                    merge_tree=merge_tree,
                    merge_parents=merge_parents,
                    host_receipt=receipt,
                    merge_lease=lease,
                )
            except BrokerError:
                raise
            except Exception:
                raise BrokerError("storage_error") from None

    async def get_active_dispatch_id(self, approval_id: UUID, dispatch_fence: int) -> UUID:
        """Resolve the immutable worker nonce for the currently active fence."""
        if (
            not isinstance(approval_id, UUID)
            or type(dispatch_fence) is not int
            or dispatch_fence <= 0
        ):
            raise BrokerError("invalid_input")
        try:
            value = await self._pool.fetchval(
                """SELECT i.dispatch_id
                   FROM release_control.broker_dispatch_intent i
                   JOIN release_control.release_card c ON c.id=i.approval_id
                   JOIN release_control.target_queue q ON q.target=c.target
                   WHERE i.approval_id=$1 AND i.dispatch_fence=$2
                     AND i.state IN ('sending','accepted','uncertain')
                     AND c.fence=$2 AND q.active_approval_id=$1 AND q.active_fence=$2""",
                approval_id,
                dispatch_fence,
            )
        except Exception:
            raise BrokerError("storage_error") from None
        if not isinstance(value, UUID):
            raise BrokerError("admission_denied")
        return value

    async def get_dispatch_id_for_job(self, approval_id: UUID, dispatch_fence: int) -> UUID:
        """Resolve the nonce for an active fence or its persisted read-only terminal run."""
        if (
            not isinstance(approval_id, UUID)
            or type(dispatch_fence) is not int
            or dispatch_fence <= 0
        ):
            raise BrokerError("invalid_input")
        try:
            value = await self._pool.fetchval(
                """SELECT i.dispatch_id
                   FROM release_control.broker_dispatch_intent i
                   JOIN release_control.release_card c ON c.id=i.approval_id
                   LEFT JOIN release_control.target_queue q ON q.target=c.target
                   WHERE i.approval_id=$1 AND i.dispatch_fence=$2 AND c.fence=$2
                     AND (
                       (i.state IN ('sending','accepted','uncertain')
                        AND q.active_approval_id=$1 AND q.active_fence=$2)
                       OR
                       (i.state IN ('accepted','uncertain','retired')
                        AND c.status IN ('merged_verified','deploying','restoring',
                                         'healthy','restored_healthy','recovery_failed')
                        AND c.run_id IS NOT NULL AND c.run_attempt IS NOT NULL
                        AND EXISTS (
                          SELECT 1 FROM release_control.broker_merge_admission a
                          WHERE a.approval_id=c.id AND a.dispatch_fence=$2
                            AND a.run_id=c.run_id AND a.run_attempt=c.run_attempt
                            AND a.check_run_id=c.check_run_id
                        ))
                     )""",
                approval_id,
                dispatch_fence,
            )
        except Exception:
            raise BrokerError("storage_error") from None
        if not isinstance(value, UUID):
            raise BrokerError("admission_denied")
        return value

    async def acquire_merge_lease(
        self,
        approval_id: UUID,
        *,
        dispatch_fence: int,
        run: RunAttemptBinding,
        now: datetime | None = None,
    ) -> MergeLease:
        """Bind a signed run/attempt and issue a phase lease without freeing P05's fence."""
        current = _utc(now or datetime.now(UTC))
        if (
            not isinstance(approval_id, UUID)
            or type(dispatch_fence) is not int
            or dispatch_fence <= 0
            or not isinstance(run, RunAttemptBinding)
        ):
            raise BrokerError("invalid_input")
        if not await self.schema_ready():
            raise BrokerError("schema_unavailable")
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    preliminary = await connection.fetchrow(
                        "SELECT target FROM release_control.release_card WHERE id=$1", approval_id
                    )
                    if preliminary is None:
                        raise BrokerError("target_unavailable")
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    card = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                        approval_id,
                    )
                    state = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_target_state "
                        "WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    if state is None:
                        raise BrokerError("target_unavailable")
                    binding = _json(card["binding"])
                    if (
                        queue["active_approval_id"] != approval_id
                        or queue["active_fence"] != dispatch_fence
                        or card["fence"] != dispatch_fence
                    ):
                        raise BrokerError("stale_fence")
                    if (
                        state["reconciliation_required"]
                        or state["policy_digest"] != binding["policy_digest"]
                        or _identity(state["deployed_identity"])
                        != _identity(binding["deployed_identity"])
                        or card["sequence"]
                        <= _identity(binding["deployed_identity"]).target_sequence
                        or state["telegram_owner_user_id"] != card["owner_user_id"]
                    ):
                        raise BrokerError("reconciliation_required")
                    if (
                        card["status"]
                        not in {ReleaseStatus.QUEUED.value, ReleaseStatus.MERGE_AUTHORIZED.value}
                        or queue["active_until"] is not None
                        or run.repository_id != binding["repository_id"]
                        or run.workflow_sha != state["workflow_sha"]
                        or not run.workflow_ref.startswith(state["repository"] + "/")
                        or not run.workflow_ref.endswith("@refs/heads/main")
                    ):
                        raise BrokerError("admission_denied")
                    outbox = await connection.fetchrow(
                        "SELECT state FROM release_control.decision_outbox WHERE approval_id=$1",
                        approval_id,
                    )
                    if outbox is None or outbox["state"] != "dispatched":
                        raise BrokerError("admission_denied")
                    retired = await connection.fetchval(
                        """SELECT EXISTS(
                             SELECT 1 FROM release_control.release_run_history
                             WHERE repository_id=$1 AND run_id=$2
                         )""",
                        run.repository_id,
                        run.run_id,
                    )
                    if retired:
                        raise BrokerError("binding_mismatch")
                    existing_run = card["run_bound_at"] is not None
                    if existing_run and (
                        card["run_repository_id"] != run.repository_id
                        or card["run_id"] != run.run_id
                        or card["run_attempt"] != run.attempt
                        or card["workflow_ref"] != run.workflow_ref
                        or card["workflow_sha"] != run.workflow_sha
                        or card["check_run_id"] != run.check_run_id
                    ):
                        raise BrokerError("binding_mismatch")
                    active = await connection.fetchrow(
                        """SELECT * FROM release_control.broker_merge_lease
                           WHERE approval_id=$1 AND released_at IS NULL FOR UPDATE""",
                        approval_id,
                    )
                    admission = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_merge_admission WHERE approval_id=$1",
                        approval_id,
                    )
                    if active is not None:
                        same_run = (
                            active["dispatch_fence"] == dispatch_fence
                            and active["repository_id"] == run.repository_id
                            and active["run_id"] == run.run_id
                            and active["run_attempt"] == run.attempt
                            and active["check_run_id"] == run.check_run_id
                        )
                        if not same_run:
                            raise BrokerError("binding_mismatch")
                        if admission is not None or active["expires_at"] > current:
                            return _lease_from_row(active, run, admission)
                        await connection.execute(
                            "UPDATE release_control.broker_merge_lease SET released_at=$2 "
                            "WHERE lease_id=$1",
                            active["lease_id"],
                            current,
                        )
                    elif admission is not None:
                        raise BrokerError("operation_conflict")
                    if await is_revoked(connection, approval_id):
                        raise BrokerError("admission_denied")
                    if not existing_run:
                        await connection.execute(
                            """UPDATE release_control.release_card SET
                               run_repository_id=$2, run_id=$3, run_attempt=$4,
                               workflow_ref=$5, workflow_sha=$6, check_run_id=$7, run_bound_at=$8
                               WHERE id=$1""",
                            approval_id,
                            run.repository_id,
                            run.run_id,
                            run.attempt,
                            run.workflow_ref,
                            run.workflow_sha,
                            run.check_run_id,
                            current,
                        )
                    lease_id = uuid4()
                    expiry = current + timedelta(seconds=MERGE_LEASE_SECONDS)
                    await connection.execute(
                        """INSERT INTO release_control.broker_merge_lease
                           (lease_id,approval_id,target,dispatch_fence,repository_id,run_id,
                            run_attempt,check_run_id,created_at,expires_at)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                        lease_id,
                        approval_id,
                        card["target"],
                        dispatch_fence,
                        run.repository_id,
                        run.run_id,
                        run.attempt,
                        run.check_run_id,
                        current,
                        expiry,
                    )
                    return MergeLease(
                        lease_id, approval_id, card["target"], dispatch_fence, run, current, expiry
                    )
            except BrokerError:
                raise
            except LedgerError as exc:
                raise BrokerError("admission_denied") from exc
            except Exception:
                raise BrokerError("storage_error") from None

    async def release_merge_lease(
        self,
        approval_id: UUID,
        *,
        dispatch_fence: int,
        lease_id: UUID,
        run: RunAttemptBinding,
        now: datetime | None = None,
    ) -> bool:
        current = _utc(now or datetime.now(UTC))
        if (
            not isinstance(approval_id, UUID)
            or type(dispatch_fence) is not int
            or dispatch_fence <= 0
            or not isinstance(lease_id, UUID)
            or not isinstance(run, RunAttemptBinding)
        ):
            raise BrokerError("invalid_input")
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    row = await connection.fetchrow(
                        """SELECT l.*, a.admitted_at FROM release_control.broker_merge_lease l
                           LEFT JOIN release_control.broker_merge_admission a
                             ON a.lease_id=l.lease_id
                           WHERE l.lease_id=$1 AND l.approval_id=$2 FOR UPDATE OF l""",
                        lease_id,
                        approval_id,
                    )
                    if (
                        row is None
                        or row["dispatch_fence"] != dispatch_fence
                        or row["repository_id"] != run.repository_id
                        or row["run_id"] != run.run_id
                        or row["run_attempt"] != run.attempt
                        or row["check_run_id"] != run.check_run_id
                    ):
                        raise BrokerError("stale_fence")
                    # Preserve an admitted lease as reconciliation context after a lost response.
                    if row["admitted_at"] is None and row["released_at"] is None:
                        await connection.execute(
                            "UPDATE release_control.broker_merge_lease SET released_at=$2 "
                            "WHERE lease_id=$1",
                            lease_id,
                            current,
                        )
                    return True
            except BrokerError:
                raise
            except Exception:
                raise BrokerError("storage_error") from None

    async def admit_merge(
        self,
        approval_id: UUID,
        *,
        dispatch_fence: int,
        lease_id: UUID,
        run: RunAttemptBinding,
        evidence: VerifiedAdmissionEvidence,
        now: datetime | None = None,
    ) -> MergeAdmission:
        current = _utc(now or datetime.now(UTC))
        if (
            not isinstance(approval_id, UUID)
            or type(dispatch_fence) is not int
            or dispatch_fence <= 0
            or not isinstance(lease_id, UUID)
            or not isinstance(run, RunAttemptBinding)
            or not isinstance(evidence, VerifiedAdmissionEvidence)
        ):
            raise BrokerError("invalid_input")
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    preliminary = await connection.fetchrow(
                        "SELECT target FROM release_control.release_card WHERE id=$1", approval_id
                    )
                    if preliminary is None:
                        raise BrokerError("target_unavailable")
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    card = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                        approval_id,
                    )
                    state = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_target_state "
                        "WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    lease = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_merge_lease "
                        "WHERE lease_id=$1 FOR UPDATE",
                        lease_id,
                    )
                    if (
                        queue["active_approval_id"] != approval_id
                        or queue["active_fence"] != dispatch_fence
                        or card["fence"] != dispatch_fence
                        or state is None
                    ):
                        raise BrokerError("stale_fence")
                    binding = _json(card["binding"])
                    admission = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_merge_admission "
                        "WHERE approval_id=$1 FOR UPDATE",
                        approval_id,
                    )
                    if admission is not None:
                        if (
                            admission["lease_id"] != lease_id
                            or admission["dispatch_fence"] != dispatch_fence
                            or admission["run_id"] != run.run_id
                            or admission["run_attempt"] != run.attempt
                            or admission["check_run_id"] != run.check_run_id
                            or admission["expected_merge_tree"] != evidence.expected_merge_tree
                            or admission["evidence_digest"] != evidence.evidence_digest
                        ):
                            raise BrokerError("binding_mismatch")
                        return _admission_from_rows(card, lease, admission, run, current)
                    if await is_revoked(connection, approval_id):
                        raise BrokerError("admission_denied")
                    outbox = await connection.fetchrow(
                        "SELECT state FROM release_control.decision_outbox WHERE approval_id=$1",
                        approval_id,
                    )
                    run_matches = (
                        card["run_bound_at"] is not None
                        and card["run_repository_id"] == run.repository_id
                        and card["run_id"] == run.run_id
                        and card["run_attempt"] == run.attempt
                        and card["workflow_sha"] == run.workflow_sha
                        and card["check_run_id"] == run.check_run_id
                    )
                    if (
                        card["status"]
                        not in {ReleaseStatus.QUEUED.value, ReleaseStatus.MERGE_AUTHORIZED.value}
                        or card["decision"] != "approve"
                        or card["expires_at"] <= current
                        or card["owner_user_id"] != state["telegram_owner_user_id"]
                        or state["reconciliation_required"]
                        or state["pending_operation_id"] is not None
                        or state["policy_digest"] != binding["policy_digest"]
                        or _identity(state["deployed_identity"])
                        != _identity(binding["deployed_identity"])
                        or card["sequence"]
                        <= _identity(binding["deployed_identity"]).target_sequence
                        or outbox is None
                        or outbox["state"] != "dispatched"
                        or not run_matches
                        or lease is None
                        or lease["approval_id"] != approval_id
                        or lease["dispatch_fence"] != dispatch_fence
                        or lease["released_at"] is not None
                        or lease["expires_at"] <= current
                        or lease["run_id"] != run.run_id
                        or lease["run_attempt"] != run.attempt
                        or lease["check_run_id"] != run.check_run_id
                        or evidence.deployed_identity != _identity(binding["deployed_identity"])
                        or evidence.current_main_sha != binding["base_sha"]
                        or evidence.base_sha != binding["base_sha"]
                        or evidence.head_sha != binding["head_sha"]
                        or evidence.head_tree != binding["head_tree"]
                        or evidence.complete_delta_reviewed is not True
                        or evidence.evidence_digest
                        != binding["review_bundle"].get("check_evidence_digest")
                        or state["policy_digest"] != evidence.deployed_identity.policy_digest
                    ):
                        raise BrokerError("admission_denied")
                    admitted_at = current
                    deadline = min(
                        lease["expires_at"], current + timedelta(seconds=MERGE_START_SECONDS)
                    )
                    await connection.execute(
                        """INSERT INTO release_control.broker_merge_admission
                           (approval_id,target,dispatch_fence,lease_id,run_id,run_attempt,
                            check_run_id,
                            expected_merge_tree,evidence_digest,admitted_at,start_deadline)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
                        approval_id,
                        card["target"],
                        dispatch_fence,
                        lease_id,
                        run.run_id,
                        run.attempt,
                        run.check_run_id,
                        evidence.expected_merge_tree,
                        evidence.evidence_digest,
                        admitted_at,
                        deadline,
                    )
                    await connection.execute(
                        "UPDATE release_control.release_card SET status='merge_authorized' "
                        "WHERE id=$1",
                        approval_id,
                    )
                    admission = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_merge_admission WHERE approval_id=$1",
                        approval_id,
                    )
                    return _admission_from_rows(card, lease, admission, run, current)
            except BrokerError:
                raise
            except Exception:
                raise BrokerError("storage_error") from None


def _lease_from_row(
    row: asyncpg.Record, run: RunAttemptBinding, admission: asyncpg.Record | None
) -> MergeLease:
    return MergeLease(
        lease_id=row["lease_id"],
        approval_id=row["approval_id"],
        target=row["target"],
        dispatch_fence=row["dispatch_fence"],
        run=run,
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        admitted_at=admission["admitted_at"] if admission is not None else None,
        start_deadline=admission["start_deadline"] if admission is not None else None,
    )


def _admission_from_rows(
    card: asyncpg.Record,
    lease: asyncpg.Record,
    admission: asyncpg.Record,
    run: RunAttemptBinding,
    now: datetime,
) -> MergeAdmission:
    binding = _json(card["binding"])
    return MergeAdmission(
        approval_id=card["id"],
        lease_id=lease["lease_id"],
        dispatch_fence=admission["dispatch_fence"],
        expires_at=min(lease["expires_at"], admission["start_deadline"]),
        admitted=True,
        may_start=(
            admission["started_at"] is None
            and admission["start_deadline"] > now
            and lease["expires_at"] > now
        ),
        deployed_identity=_identity(binding["deployed_identity"]),
        base_sha=binding["base_sha"],
        head_sha=binding["head_sha"],
        head_tree=binding["head_tree"],
        policy_digest=binding["policy_digest"],
        expected_merge_tree=admission["expected_merge_tree"],
        run=run,
    )


def _switch_binding(row: asyncpg.Record) -> HostOperationBinding:
    try:
        return HostOperationBinding(
            operation_id=row["operation_id"],
            baseline=_identity(row["baseline_identity"]),
            payload=_identity(row["payload_identity"]),
        )
    except Exception:
        raise BrokerError("storage_error") from None


def _switch_admission(row: asyncpg.Record, now: datetime) -> SwitchAdmission:
    if row["start_deadline"] is None or row["recovery_deadline"] is None:
        raise BrokerError("storage_error")
    return SwitchAdmission(
        approval_id=row["approval_id"],
        target=row["target"],
        operation_id=row["operation_id"],
        dispatch_fence=row["dispatch_fence"],
        admitted=row["state"] != "prepared",
        may_start=row["state"] == "admitted" and row["start_deadline"] > now,
        start_deadline=row["start_deadline"],
        recovery_deadline=row["recovery_deadline"],
        baseline_identity=_identity(row["baseline_identity"]),
        payload_identity=_identity(row["payload_identity"]),
    )
