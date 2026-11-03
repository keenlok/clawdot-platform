"""Protected async Postgres approval ledger.

Schema installation is an explicit maintenance action from schema.sql. This module
never creates or migrates database objects and never makes the ledger available to the
ordinary bot database role.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from .policy import DeploymentIdentity, MergeEvidence

_CARD_TTL = timedelta(hours=24)
_MAX_BINDING_BYTES = 512 * 1024
_TARGET = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SHA1_OR_SHA256 = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WORKER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_WORKFLOW_REF = re.compile(r"^[A-Za-z0-9_./@:-]{1,256}$")
_RECEIPT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class LedgerError(RuntimeError):
    """Fixed, non-sensitive ledger error."""

    _CODES = frozenset(
        {
            "invalid_binding",
            "invalid_card",
            "forbidden",
            "binding_mismatch",
            "expired",
            "replay_mismatch",
            "decision_conflict",
            "invalid_transition",
            "stale_fence",
            "storage_error",
        }
    )

    def __init__(self, code: str = "storage_error") -> None:
        self.code = code if code in self._CODES else "storage_error"
        super().__init__(self.code)


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ReleaseStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    QUEUED = "queued"
    MERGE_AUTHORIZED = "merge_authorized"
    MERGED_VERIFIED = "merged_verified"
    MERGED_BLOCKED = "merged_blocked"
    DEPLOYING = "deploying"
    HEALTHY = "healthy"
    RESTORING = "restoring"
    RESTORED_HEALTHY = "restored_healthy"
    RECOVERY_FAILED = "recovery_failed"


_TERMINAL = {
    ReleaseStatus.REJECTED,
    ReleaseStatus.EXPIRED,
    ReleaseStatus.INVALIDATED,
    ReleaseStatus.HEALTHY,
    ReleaseStatus.RESTORED_HEALTHY,
}
_TRANSITIONS = {
    ReleaseStatus.AWAITING_APPROVAL: {
        ReleaseStatus.INVALIDATED,
    },
    ReleaseStatus.QUEUED: {
        ReleaseStatus.INVALIDATED,
        ReleaseStatus.MERGE_AUTHORIZED,
    },
    ReleaseStatus.MERGE_AUTHORIZED: {
        ReleaseStatus.MERGED_VERIFIED,
        ReleaseStatus.MERGED_BLOCKED,
    },
    ReleaseStatus.MERGED_VERIFIED: {
        ReleaseStatus.MERGED_BLOCKED,
        ReleaseStatus.DEPLOYING,
    },
    ReleaseStatus.MERGED_BLOCKED: set(),
    ReleaseStatus.DEPLOYING: {
        ReleaseStatus.HEALTHY,
        ReleaseStatus.RESTORING,
    },
    ReleaseStatus.RESTORING: set(),
    ReleaseStatus.REJECTED: set(),
    ReleaseStatus.EXPIRED: set(),
    ReleaseStatus.INVALIDATED: set(),
    ReleaseStatus.HEALTHY: set(),
    ReleaseStatus.RESTORED_HEALTHY: set(),
    ReleaseStatus.RECOVERY_FAILED: set(),
}


def _canonical(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise LedgerError("invalid_binding") from None
    return encoded


def _parse_json(value: object) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, RecursionError):
            raise LedgerError("storage_error") from None
    return value


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    if depth > 32:
        raise LedgerError("invalid_binding")
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise LedgerError("invalid_binding") from None
        return
    if type(value) is int:
        if abs(value) > 2**63 - 1:
            raise LedgerError("invalid_binding")
        return
    if type(value) is list:
        if len(value) > 10_000:
            raise LedgerError("invalid_binding")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > 10_000 or any(not isinstance(key, str) for key in value):
            raise LedgerError("invalid_binding")
        try:
            for key in value:
                key.encode("utf-8", errors="strict")
        except UnicodeError:
            raise LedgerError("invalid_binding") from None
        for item in value.values():
            _validate_json_value(item, depth=depth + 1)
        return
    raise LedgerError("invalid_binding")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LedgerError("invalid_card")
    return value.astimezone(UTC)


def _require_positive_int(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise LedgerError("invalid_binding")


def _validate_sha(value: object, *, sha256_only: bool = False) -> None:
    pattern = _SHA256 if sha256_only else _SHA1_OR_SHA256
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise LedgerError("invalid_binding")


@dataclass(frozen=True, init=False, repr=False)
class ReleaseBinding:
    """Immutable snapshot of candidate, deployed identity, policy and full review bundle."""

    repository_id: int
    owner_id: int
    pr_number: int
    target: str
    base_sha: str
    head_sha: str
    head_tree: str
    policy_digest: str
    _deployed_json: str = field(repr=False, compare=False)
    _review_json: str = field(repr=False, compare=False)
    _json: str = field(repr=False, compare=False)
    _digest: str = field(repr=False, compare=False)
    _review_digest: str = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        repository_id: int,
        owner_id: int,
        pr_number: int,
        target: str,
        base_sha: str,
        head_sha: str,
        head_tree: str,
        deployed_identity: DeploymentIdentity,
        policy_digest: str,
        review_bundle: dict[str, object],
        review_bundle_digest: str | None = None,
    ) -> None:
        for number in (repository_id, owner_id, pr_number):
            _require_positive_int(number)
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            raise LedgerError("invalid_binding")
        for sha in (base_sha, head_sha, head_tree):
            _validate_sha(sha)
        _validate_sha(policy_digest, sha256_only=True)
        if not isinstance(deployed_identity, DeploymentIdentity):
            raise LedgerError("invalid_binding")
        deployed = asdict(deployed_identity)
        if (
            type(review_bundle) is not dict
            or review_bundle.get("complete_delta_reviewed") is not True
        ):
            raise LedgerError("invalid_binding")
        _validate_json_value(deployed)
        _validate_json_value(review_bundle)
        deployed_json = _canonical(deployed)
        review_json = _canonical(review_bundle)
        if (
            len(deployed_json.encode("utf-8")) + len(review_json.encode("utf-8"))
            > _MAX_BINDING_BYTES
        ):
            raise LedgerError("invalid_binding")
        review_digest = hashlib.sha256(review_json.encode("utf-8")).hexdigest()
        if review_bundle_digest is not None and review_bundle_digest != review_digest:
            raise LedgerError("invalid_binding")
        document = {
            "version": 1,
            "repository_id": repository_id,
            "owner_id": owner_id,
            "pr_number": pr_number,
            "target": target,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "head_tree": head_tree,
            "deployed_identity": json.loads(deployed_json),
            "policy_digest": policy_digest,
            "review_bundle": json.loads(review_json),
            "review_bundle_digest": review_digest,
        }
        canonical = _canonical(document)
        if len(canonical.encode("utf-8")) > _MAX_BINDING_BYTES:
            raise LedgerError("invalid_binding")
        for name, value in (
            ("repository_id", repository_id),
            ("owner_id", owner_id),
            ("pr_number", pr_number),
            ("target", target),
            ("base_sha", base_sha),
            ("head_sha", head_sha),
            ("head_tree", head_tree),
            ("policy_digest", policy_digest),
            ("_deployed_json", deployed_json),
            ("_review_json", review_json),
            ("_json", canonical),
            ("_digest", hashlib.sha256(canonical.encode("utf-8")).hexdigest()),
            ("_review_digest", review_digest),
        ):
            object.__setattr__(self, name, value)

    @property
    def deployed_identity(self) -> DeploymentIdentity:
        return DeploymentIdentity(**json.loads(self._deployed_json))

    @property
    def review_bundle(self) -> dict[str, object]:
        return json.loads(self._review_json)

    @property
    def review_bundle_digest(self) -> str:
        return self._review_digest

    @property
    def digest(self) -> str:
        return self._digest

    @property
    def review_digest(self) -> str:
        return self._review_digest

    def to_json(self) -> str:
        """Return a stable canonical snapshot, independent of later caller mutations."""
        return self._json

    def __repr__(self) -> str:
        return (
            f"ReleaseBinding(target={self.target!r}, pr_number={self.pr_number}, "
            f"digest={self.digest[:12]}...)"
        )


@dataclass(frozen=True, repr=False, init=False)
class CardNonce:
    """Trusted random nonce generated for one broker-created card."""

    _value: str = field(repr=False)

    def __init__(self) -> None:
        value = secrets.token_urlsafe(32)
        if not _NONCE.fullmatch(value):
            raise LedgerError("invalid_card")
        object.__setattr__(self, "_value", value)

    @property
    def value(self) -> str:
        return self._value

    @property
    def digest(self) -> bytes:
        return hashlib.sha256(self._value.encode("ascii")).digest()

    def __repr__(self) -> str:
        return "CardNonce([REDACTED])"


@dataclass(frozen=True)
class CardReceipt:
    approval_id: UUID
    target: str
    sequence: int
    status: ReleaseStatus
    issued_at: datetime
    expires_at: datetime
    binding_digest: str


@dataclass(frozen=True)
class CardRecord(CardReceipt):
    owner_user_id: int
    chat_id: int
    message_id: int
    binding: dict[str, object]
    decision: Decision | None
    decision_at: datetime | None
    nonce_consumed_at: datetime | None
    fence: int | None
    run_attempt: RunAttemptBinding | None
    merge_evidence: MergeEvidence | None
    expected_merge_tree: str | None
    host_operation_id: UUID | None
    host_operation: HostOperationBinding | None
    host_receipt_id: str | None


@dataclass(frozen=True, repr=False)
class DecisionCommand:
    update_id: int
    approval_id: UUID
    owner_user_id: int
    chat_id: int
    message_id: int
    nonce: str = field(repr=False)
    decision: Decision
    binding_digest: str

    def __post_init__(self) -> None:
        if type(self.update_id) is not int or self.update_id <= 0:
            raise LedgerError("invalid_card")
        if not isinstance(self.approval_id, UUID):
            raise LedgerError("invalid_card")
        for value in (self.owner_user_id, self.chat_id, self.message_id):
            if type(value) is not int or value <= 0:
                raise LedgerError("invalid_card")
        if self.owner_user_id != self.chat_id:
            raise LedgerError("forbidden")
        if not isinstance(self.nonce, str) or not _NONCE.fullmatch(self.nonce):
            raise LedgerError("invalid_card")
        if not isinstance(self.decision, Decision):
            raise LedgerError("invalid_card")
        _validate_sha(self.binding_digest, sha256_only=True)

    @property
    def nonce_digest(self) -> bytes:
        return hashlib.sha256(self.nonce.encode("ascii")).digest()

    @property
    def request_digest(self) -> bytes:
        value = {
            "update_id": self.update_id,
            "approval_id": str(self.approval_id),
            "owner_user_id": self.owner_user_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "nonce_sha256": self.nonce_digest.hex(),
            "decision": self.decision.value,
            "binding_sha256": self.binding_digest,
        }
        return hashlib.sha256(_canonical(value).encode("utf-8")).digest()

    def __repr__(self) -> str:
        return (
            f"DecisionCommand(update_id={self.update_id}, approval_id={self.approval_id}, "
            f"decision={self.decision.value!r}, nonce=[REDACTED])"
        )


@dataclass(frozen=True)
class DecisionResult:
    approval_id: UUID
    decision: Decision
    status: ReleaseStatus
    sequence: int
    binding_digest: str


@dataclass(frozen=True)
class DispatchClaim:
    approval_id: UUID
    target: str
    sequence: int
    fence: int
    lease_until: datetime
    binding_digest: str


@dataclass(frozen=True)
class HostOperationBinding:
    """Immutable baseline and expected deployed identity for one host operation."""

    operation_id: UUID
    baseline: DeploymentIdentity
    payload: DeploymentIdentity

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation_id, UUID)
            or not isinstance(self.baseline, DeploymentIdentity)
            or not isinstance(self.payload, DeploymentIdentity)
        ):
            raise LedgerError("invalid_binding")

    @property
    def payload_digest(self) -> str:
        return hashlib.sha256(_canonical(asdict(self.payload)).encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        return _canonical(
            {
                "operation_id": str(self.operation_id),
                "baseline": asdict(self.baseline),
                "payload": asdict(self.payload),
            }
        )


@dataclass(frozen=True)
class RunAttemptBinding:
    """Immutable GitHub run identity observed by the protected broker."""

    repository_id: int
    run_id: int
    attempt: int
    workflow_ref: str
    workflow_sha: str
    check_run_id: int

    def __post_init__(self) -> None:
        for value in (self.repository_id, self.run_id, self.attempt, self.check_run_id):
            _require_positive_int(value)
        if not isinstance(self.workflow_ref, str) or not _WORKFLOW_REF.fullmatch(self.workflow_ref):
            raise LedgerError("invalid_binding")
        _validate_sha(self.workflow_sha)

    def to_json(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "workflow_ref": self.workflow_ref,
            "workflow_sha": self.workflow_sha,
            "check_run_id": self.check_run_id,
        }


@dataclass(frozen=True, repr=False)
class HostReceipt:
    """Persisted terminal host observation supplied by the trusted host service."""

    approval_id: UUID
    target: str
    operation_id: UUID
    receipt_id: str
    fence: int
    status: ReleaseStatus
    merge_commit: str
    observed_identity: DeploymentIdentity | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.approval_id, UUID) or not isinstance(self.operation_id, UUID):
            raise LedgerError("invalid_card")
        if not isinstance(self.target, str) or not _TARGET.fullmatch(self.target):
            raise LedgerError("invalid_card")
        if not isinstance(self.receipt_id, str) or not _RECEIPT_ID.fullmatch(self.receipt_id):
            raise LedgerError("invalid_card")
        if type(self.fence) is not int or self.fence <= 0:
            raise LedgerError("invalid_card")
        if self.status not in {
            ReleaseStatus.HEALTHY,
            ReleaseStatus.RESTORED_HEALTHY,
            ReleaseStatus.RECOVERY_FAILED,
        }:
            raise LedgerError("invalid_card")
        _validate_sha(self.merge_commit)
        if self.observed_identity is not None and not isinstance(
            self.observed_identity, DeploymentIdentity
        ):
            raise LedgerError("invalid_card")
        _utc(self.observed_at)

    def to_json(self) -> str:
        return _canonical(
            {
                "approval_id": str(self.approval_id),
                "target": self.target,
                "operation_id": str(self.operation_id),
                "receipt_id": self.receipt_id,
                "fence": self.fence,
                "status": self.status.value,
                "merge_commit": self.merge_commit,
                "observed_identity": (
                    asdict(self.observed_identity) if self.observed_identity is not None else None
                ),
                "observed_at": _utc(self.observed_at).isoformat(),
            }
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return (
            f"HostReceipt(approval_id={self.approval_id}, receipt_id={self.receipt_id!r}, "
            f"status={self.status.value!r}, digest={self.digest[:12]}...)"
        )


class ReleaseLedger:
    """Async transactional ledger. The supplied pool must use the protected DB role."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    def __repr__(self) -> str:
        return "ReleaseLedger(pool=[protected])"

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[asyncpg.Connection]:
        try:
            async with self._pool.acquire() as connection:
                yield connection
        except LedgerError:
            raise
        except Exception:
            raise LedgerError("storage_error") from None

    async def create_card(
        self,
        *,
        binding: ReleaseBinding,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
        nonce: CardNonce,
        now: datetime | None = None,
    ) -> CardReceipt:
        if (
            not isinstance(binding, ReleaseBinding)
            or type(owner_user_id) is not int
            or owner_user_id <= 0
            or type(chat_id) is not int
            or chat_id != owner_user_id
            or type(message_id) is not int
            or message_id <= 0
            or type(nonce) is not CardNonce
        ):
            raise LedgerError("invalid_card")
        issued_at = _utc(now or datetime.now(UTC))
        expires_at = issued_at + _CARD_TTL
        approval_id = uuid4()
        nonce_digest = nonce.digest
        binding_json = binding.to_json()
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO release_control.target_queue (target)
                        VALUES ($1) ON CONFLICT (target) DO NOTHING
                        """,
                        binding.target,
                    )
                    target = await connection.fetchrow(
                        "SELECT next_sequence FROM release_control.target_queue "
                        "WHERE target=$1 FOR UPDATE",
                        binding.target,
                    )
                    sequence = max(
                        target["next_sequence"], binding.deployed_identity.target_sequence + 1
                    )
                    await connection.execute(
                        "UPDATE release_control.target_queue SET next_sequence=$2 WHERE target=$1",
                        binding.target,
                        sequence + 1,
                    )
                    await connection.execute(
                        """
                        INSERT INTO release_control.release_card (
                            id, target, sequence, status, owner_user_id, chat_id, message_id,
                            nonce_sha256, issued_at, expires_at, binding, binding_sha256
                        ) VALUES (
                            $1, $2, $3, 'awaiting_approval', $4, $5, $6,
                            $7, $8, $9, $10::jsonb, $11
                        )
                        """,
                        approval_id,
                        binding.target,
                        sequence,
                        owner_user_id,
                        chat_id,
                        message_id,
                        nonce_digest,
                        issued_at,
                        expires_at,
                        binding_json,
                        bytes.fromhex(binding.digest),
                    )
            except LedgerError:
                raise
            except Exception:
                raise LedgerError("storage_error") from None
        return CardReceipt(
            approval_id,
            binding.target,
            sequence,
            ReleaseStatus.AWAITING_APPROVAL,
            issued_at,
            expires_at,
            binding.digest,
        )

    async def record_decision(
        self, command: DecisionCommand, *, now: datetime | None = None
    ) -> DecisionResult:
        if not isinstance(command, DecisionCommand):
            raise LedgerError("invalid_card")
        decided_at = _utc(now or datetime.now(UTC))
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    await connection.execute("SELECT pg_advisory_xact_lock($1)", command.update_id)
                    prior = await connection.fetchrow(
                        "SELECT request_sha256, result FROM release_control.telegram_update "
                        "WHERE update_id=$1",
                        command.update_id,
                    )
                    if prior is not None:
                        if bytes(prior["request_sha256"]) != command.request_digest:
                            raise LedgerError("replay_mismatch")
                        return self._result(_parse_json(prior["result"]))

                    row = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                        command.approval_id,
                    )
                    if row is None:
                        raise LedgerError("invalid_card")
                    self._verify_command(row, command)

                    if row["decision"] is not None:
                        if row["decision"] != command.decision.value:
                            raise LedgerError("decision_conflict")
                        result = self._row_result(row)
                    else:
                        if row["status"] == ReleaseStatus.EXPIRED.value:
                            raise LedgerError("expired")
                        if row["status"] != ReleaseStatus.AWAITING_APPROVAL.value:
                            raise LedgerError("invalid_card")
                        if decided_at >= row["expires_at"]:
                            raise LedgerError("expired")
                        next_status = (
                            ReleaseStatus.QUEUED
                            if command.decision is Decision.APPROVE
                            else ReleaseStatus.REJECTED
                        )
                        await connection.execute(
                            """
                            UPDATE release_control.release_card
                            SET decision=$2, decision_at=$3, nonce_consumed_at=$3, status=$4
                            WHERE id=$1
                            """,
                            command.approval_id,
                            command.decision.value,
                            decided_at,
                            next_status.value,
                        )
                        if command.decision is Decision.APPROVE:
                            payload = _canonical(
                                {
                                    "approval_id": str(command.approval_id),
                                    "target": row["target"],
                                    "sequence": row["sequence"],
                                    "binding_sha256": command.binding_digest,
                                }
                            )
                            await connection.execute(
                                """
                                INSERT INTO release_control.decision_outbox (
                                    approval_id, target, sequence, event_type, payload
                                ) VALUES ($1, $2, $3, 'dispatch_release', $4::jsonb)
                                """,
                                command.approval_id,
                                row["target"],
                                row["sequence"],
                                payload,
                            )
                        result = DecisionResult(
                            command.approval_id,
                            command.decision,
                            next_status,
                            row["sequence"],
                            command.binding_digest,
                        )

                    result_json = _canonical(self._result_json(result))
                    await connection.execute(
                        """
                        INSERT INTO release_control.telegram_update (
                            update_id, approval_id, request_sha256, result
                        ) VALUES ($1, $2, $3, $4::jsonb)
                        """,
                        command.update_id,
                        command.approval_id,
                        command.request_digest,
                        result_json,
                    )
                    return result
            except LedgerError:
                raise
            except Exception:
                raise LedgerError("storage_error") from None

    async def bind_run_attempt(
        self,
        approval_id: UUID,
        *,
        fence: int,
        run: RunAttemptBinding,
        now: datetime | None = None,
    ) -> RunAttemptBinding:
        """Persist one immutable run/attempt before merge authorization is recorded."""
        current = _utc(now or datetime.now(UTC))
        if (
            not isinstance(approval_id, UUID)
            or type(fence) is not int
            or fence <= 0
            or not isinstance(run, RunAttemptBinding)
        ):
            raise LedgerError("invalid_card")
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    preliminary = await connection.fetchrow(
                        "SELECT target FROM release_control.release_card WHERE id=$1",
                        approval_id,
                    )
                    if preliminary is None:
                        raise LedgerError("invalid_card")
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    row = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                        approval_id,
                    )
                    if (
                        queue["active_approval_id"] != approval_id
                        or queue["active_fence"] != fence
                        or row["fence"] != fence
                    ):
                        raise LedgerError("stale_fence")
                    if row["run_bound_at"] is not None:
                        existing = self._run_binding(row)
                        if existing != run:
                            raise LedgerError("binding_mismatch")
                        return existing
                    if (
                        row["status"] != ReleaseStatus.QUEUED.value
                        or queue["active_until"] is not None
                    ):
                        raise LedgerError("invalid_transition")
                    outbox = await connection.fetchrow(
                        "SELECT state FROM release_control.decision_outbox WHERE approval_id=$1",
                        approval_id,
                    )
                    if outbox is None or outbox["state"] != "dispatched":
                        raise LedgerError("invalid_transition")
                    binding = _parse_json(row["binding"])
                    if run.repository_id != binding.get("repository_id"):
                        raise LedgerError("binding_mismatch")
                    retired = await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM release_control.release_run_history "
                        "WHERE repository_id=$1 AND run_id=$2)",
                        run.repository_id,
                        run.run_id,
                    )
                    if retired:
                        raise LedgerError("binding_mismatch")
                    await connection.execute(
                        """
                        UPDATE release_control.release_card
                        SET run_repository_id=$2, run_id=$3, run_attempt=$4,
                            workflow_ref=$5, workflow_sha=$6, check_run_id=$7,
                            run_bound_at=$8
                        WHERE id=$1
                        """,
                        approval_id,
                        run.repository_id,
                        run.run_id,
                        run.attempt,
                        run.workflow_ref,
                        run.workflow_sha,
                        run.check_run_id,
                        current,
                    )
                    return run
            except LedgerError:
                raise
            except Exception:
                raise LedgerError("storage_error") from None

    async def record_merge_result(
        self,
        approval_id: UUID,
        *,
        fence: int,
        evidence: MergeEvidence,
        expected_merge_tree: str,
        now: datetime | None = None,
    ) -> CardRecord:
        """Record caller-supplied GitHub merge evidence; this method does not authenticate it."""
        recorded_at = _utc(now or datetime.now(UTC))
        if (
            not isinstance(approval_id, UUID)
            or type(fence) is not int
            or fence <= 0
            or not isinstance(evidence, MergeEvidence)
        ):
            raise LedgerError("invalid_card")
        _validate_sha(expected_merge_tree)
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    preliminary = await connection.fetchrow(
                        "SELECT target FROM release_control.release_card WHERE id=$1",
                        approval_id,
                    )
                    if preliminary is None:
                        raise LedgerError("invalid_card")
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    row = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                        approval_id,
                    )
                    if (
                        queue["active_approval_id"] != approval_id
                        or queue["active_fence"] != fence
                        or row["fence"] != fence
                    ):
                        raise LedgerError("stale_fence")
                    if row["merge_recorded_at"] is not None:
                        same = (
                            row["expected_merge_tree"] == expected_merge_tree
                            and row["merge_commit"] == evidence.merge_commit
                            and row["merge_tree"] == evidence.tree
                            and row["merge_parent_base"] == evidence.parents[0]
                            and row["merge_parent_head"] == evidence.parents[1]
                        )
                        if not same:
                            raise LedgerError("binding_mismatch")
                        return self._record(row)
                    if (
                        row["status"] != ReleaseStatus.MERGE_AUTHORIZED.value
                        or row["run_bound_at"] is None
                    ):
                        raise LedgerError("invalid_transition")
                    binding = _parse_json(row["binding"])
                    expected_parents = (binding["base_sha"], binding["head_sha"])
                    verified = (
                        evidence.merge_commit not in evidence.parents
                        and evidence.parents == expected_parents
                        and evidence.tree == expected_merge_tree
                    )
                    status = (
                        ReleaseStatus.MERGED_VERIFIED if verified else ReleaseStatus.MERGED_BLOCKED
                    )
                    await connection.execute(
                        """
                        UPDATE release_control.release_card
                        SET expected_merge_tree=$2, merge_commit=$3, merge_tree=$4,
                            merge_parent_base=$5, merge_parent_head=$6,
                            merge_recorded_at=$7, status=$8
                        WHERE id=$1
                        """,
                        approval_id,
                        expected_merge_tree,
                        evidence.merge_commit,
                        evidence.tree,
                        evidence.parents[0],
                        evidence.parents[1],
                        recorded_at,
                        status.value,
                    )
                    final = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1",
                        approval_id,
                    )
                    return self._record(final)
            except LedgerError:
                raise
            except Exception:
                raise LedgerError("storage_error") from None

    async def expire_pending(self, *, now: datetime | None = None) -> int:
        current = _utc(now or datetime.now(UTC))
        try:
            async with self._connection() as connection:
                rows = await connection.fetch(
                    """
                    UPDATE release_control.release_card
                    SET status='expired'
                    WHERE status='awaiting_approval' AND expires_at <= $1
                    RETURNING id
                    """,
                    current,
                )
                return len(rows)
        except Exception:
            raise LedgerError("storage_error") from None

    async def claim_next_dispatch(
        self,
        target: str,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease: timedelta = timedelta(seconds=60),
    ) -> DispatchClaim | None:
        """Lease dispatch work only; a claim is never merge or host-switch authority."""
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            raise LedgerError("invalid_card")
        if not isinstance(worker_id, str) or not _WORKER.fullmatch(worker_id):
            raise LedgerError("invalid_card")
        current = _utc(now or datetime.now(UTC))
        if lease <= timedelta(0) or lease > timedelta(minutes=5):
            raise LedgerError("invalid_card")
        lease_until = current + lease
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        target,
                    )
                    if queue is None:
                        return None
                    approval_id = queue["active_approval_id"]
                    if approval_id is not None and queue["active_until"] is None:
                        return None
                    if approval_id is not None and queue["active_until"] > current:
                        active = await connection.fetchrow(
                            "SELECT * FROM release_control.decision_outbox WHERE approval_id=$1",
                            approval_id,
                        )
                        if active is not None and active["state"] == "leased":
                            if active["lease_owner"] == worker_id:
                                return self._claim_from_rows(
                                    approval_id,
                                    queue["active_fence"],
                                    active["lease_until"],
                                    await connection.fetchrow(
                                        "SELECT * FROM release_control.release_card WHERE id=$1",
                                        approval_id,
                                    ),
                                )
                        return None

                    if approval_id is not None:
                        outbox = await connection.fetchrow(
                            """
                            SELECT o.*, c.target AS card_target, c.sequence AS card_sequence,
                                   c.binding_sha256 AS card_binding_sha256
                            FROM release_control.decision_outbox o
                            JOIN release_control.release_card c ON c.id=o.approval_id
                            WHERE o.approval_id=$1 AND o.state='leased' AND o.lease_until <= $2
                            FOR UPDATE OF o
                            """,
                            approval_id,
                            current,
                        )
                    else:
                        outbox = await connection.fetchrow(
                            """
                            SELECT o.*, c.target AS card_target, c.sequence AS card_sequence,
                                   c.binding_sha256 AS card_binding_sha256
                            FROM release_control.decision_outbox o
                            JOIN release_control.release_card c ON c.id=o.approval_id
                            WHERE o.target=$1 AND o.state='pending' AND c.status='queued'
                            ORDER BY o.sequence
                            LIMIT 1
                            FOR UPDATE OF o SKIP LOCKED
                            """,
                            target,
                        )
                    if outbox is None:
                        return None
                    fence = queue["next_fence"]
                    await connection.execute(
                        """
                        UPDATE release_control.target_queue
                        SET next_fence=$2, active_approval_id=$3, active_fence=$4,
                            active_until=$5
                        WHERE target=$1
                        """,
                        target,
                        fence + 1,
                        outbox["approval_id"],
                        fence,
                        lease_until,
                    )
                    await connection.execute(
                        "UPDATE release_control.release_card SET fence=$2 WHERE id=$1",
                        outbox["approval_id"],
                        fence,
                    )
                    await connection.execute(
                        """
                        UPDATE release_control.decision_outbox
                        SET state='leased', lease_owner=$2, lease_until=$3, attempts=attempts+1
                        WHERE approval_id=$1
                        """,
                        outbox["approval_id"],
                        worker_id,
                        lease_until,
                    )
                    card = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1",
                        outbox["approval_id"],
                    )
                    return self._claim_from_rows(outbox["approval_id"], fence, lease_until, card)
            except LedgerError:
                raise
            except Exception:
                raise LedgerError("storage_error") from None

    async def acknowledge_dispatch(
        self,
        approval_id: UUID,
        *,
        fence: int,
        now: datetime | None = None,
    ) -> None:
        current = _utc(now or datetime.now(UTC))
        if not isinstance(approval_id, UUID) or type(fence) is not int or fence <= 0:
            raise LedgerError("invalid_card")
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    card = await connection.fetchrow(
                        "SELECT target FROM release_control.release_card WHERE id=$1",
                        approval_id,
                    )
                    if card is None:
                        raise LedgerError("invalid_card")
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        card["target"],
                    )
                    if queue["active_approval_id"] != approval_id or queue["active_fence"] != fence:
                        raise LedgerError("stale_fence")
                    outbox = await connection.fetchrow(
                        "SELECT * FROM release_control.decision_outbox "
                        "WHERE approval_id=$1 FOR UPDATE",
                        approval_id,
                    )
                    if outbox is None:
                        raise LedgerError("stale_fence")
                    if outbox["state"] == "dispatched":
                        return
                    if (
                        outbox["state"] != "leased"
                        or queue["active_until"] is None
                        or queue["active_until"] <= current
                    ):
                        raise LedgerError("stale_fence")
                    await connection.execute(
                        """
                        UPDATE release_control.decision_outbox
                        SET state='dispatched', lease_owner=NULL, lease_until=NULL,
                            dispatched_at=$2
                        WHERE approval_id=$1
                        """,
                        approval_id,
                        current,
                    )
                    await connection.execute(
                        "UPDATE release_control.target_queue SET active_until=NULL WHERE target=$1",
                        card["target"],
                    )
            except LedgerError:
                raise
            except Exception:
                raise LedgerError("storage_error") from None

    async def advance_status(
        self,
        approval_id: UUID,
        status: ReleaseStatus,
        *,
        fence: int | None = None,
    ) -> CardRecord:
        """Record non-evidence transitions; external admission remains a broker duty."""
        if not isinstance(approval_id, UUID) or not isinstance(status, ReleaseStatus):
            raise LedgerError("invalid_card")
        if status in {
            ReleaseStatus.MERGED_VERIFIED,
            ReleaseStatus.MERGED_BLOCKED,
            ReleaseStatus.HEALTHY,
            ReleaseStatus.RESTORED_HEALTHY,
            ReleaseStatus.RECOVERY_FAILED,
        }:
            raise LedgerError("invalid_transition")
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    preliminary = await connection.fetchrow(
                        "SELECT target FROM release_control.release_card WHERE id=$1",
                        approval_id,
                    )
                    if preliminary is None:
                        raise LedgerError("invalid_card")
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    row = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                        approval_id,
                    )
                    current = ReleaseStatus(row["status"])
                    if status not in _TRANSITIONS[current]:
                        raise LedgerError("invalid_transition")
                    active = queue["active_approval_id"] == approval_id
                    if active:
                        if fence is None or queue["active_fence"] != fence or row["fence"] != fence:
                            raise LedgerError("stale_fence")
                    elif current in {
                        ReleaseStatus.MERGE_AUTHORIZED,
                        ReleaseStatus.MERGED_VERIFIED,
                        ReleaseStatus.MERGED_BLOCKED,
                        ReleaseStatus.DEPLOYING,
                        ReleaseStatus.RESTORING,
                    }:
                        raise LedgerError("stale_fence")
                    if current is ReleaseStatus.QUEUED and status is ReleaseStatus.MERGE_AUTHORIZED:
                        if (
                            row["run_bound_at"] is None
                            or row["run_repository_id"]
                            != _parse_json(row["binding"])["repository_id"]
                        ):
                            raise LedgerError("invalid_transition")
                        sent = await connection.fetchval(
                            "SELECT state='dispatched' FROM release_control.decision_outbox "
                            "WHERE approval_id=$1",
                            approval_id,
                        )
                        if sent is not True:
                            raise LedgerError("invalid_transition")
                    if status is ReleaseStatus.DEPLOYING and row["host_operation_id"] is None:
                        raise LedgerError("invalid_transition")
                    if current is ReleaseStatus.QUEUED and status is ReleaseStatus.INVALIDATED:
                        if queue["active_approval_id"] == approval_id:
                            raise LedgerError("invalid_transition")
                        await connection.execute(
                            "UPDATE release_control.decision_outbox SET state='canceled' "
                            "WHERE approval_id=$1 AND state='pending'",
                            approval_id,
                        )
                    await connection.execute(
                        "UPDATE release_control.release_card SET status=$2 WHERE id=$1",
                        approval_id,
                        status.value,
                    )
                    if status in _TERMINAL and active:
                        await connection.execute(
                            """
                            UPDATE release_control.target_queue
                            SET active_approval_id=NULL, active_fence=NULL, active_until=NULL
                            WHERE target=$1
                            """,
                            row["target"],
                        )
                    final = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1",
                        approval_id,
                    )
                    return self._record(final)
            except LedgerError:
                raise
            except Exception:
                raise LedgerError("storage_error") from None

    async def record_host_receipt(
        self,
        approval_id: UUID,
        receipt: HostReceipt,
        *,
        now: datetime | None = None,
    ) -> HostReceipt:
        """Persist a terminal host observation supplied by the protected host service."""
        recorded_at = _utc(now or datetime.now(UTC))
        if not isinstance(approval_id, UUID) or not isinstance(receipt, HostReceipt):
            raise LedgerError("invalid_card")
        if receipt.approval_id != approval_id or _utc(receipt.observed_at) > recorded_at:
            raise LedgerError("binding_mismatch")
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    preliminary = await connection.fetchrow(
                        "SELECT target FROM release_control.release_card WHERE id=$1",
                        approval_id,
                    )
                    if preliminary is None:
                        raise LedgerError("invalid_card")
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    row = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                        approval_id,
                    )
                    if row["host_receipt_id"] is not None:
                        existing = self._host_receipt(row)
                        if existing.digest != receipt.digest:
                            raise LedgerError("binding_mismatch")
                        return existing
                    if (
                        queue["active_approval_id"] != approval_id
                        or queue["active_fence"] != receipt.fence
                        or row["fence"] != receipt.fence
                    ):
                        raise LedgerError("stale_fence")
                    current_status = ReleaseStatus(row["status"])
                    allowed_status = {
                        ReleaseStatus.HEALTHY: {ReleaseStatus.DEPLOYING},
                        ReleaseStatus.RESTORED_HEALTHY: {ReleaseStatus.RESTORING},
                        ReleaseStatus.RECOVERY_FAILED: {
                            ReleaseStatus.DEPLOYING,
                            ReleaseStatus.RESTORING,
                        },
                    }
                    if current_status not in allowed_status[receipt.status]:
                        raise LedgerError("invalid_transition")
                    if row["host_operation_id"] is None:
                        raise LedgerError("invalid_transition")
                    operation = self._host_operation(row)
                    if (
                        receipt.operation_id != operation.operation_id
                        or receipt.target != row["target"]
                        or receipt.merge_commit != row["merge_commit"]
                        or row["merge_recorded_at"] is None
                    ):
                        raise LedgerError("binding_mismatch")
                    if receipt.status is ReleaseStatus.HEALTHY:
                        if receipt.observed_identity != operation.payload:
                            raise LedgerError("binding_mismatch")
                    elif receipt.status is ReleaseStatus.RESTORED_HEALTHY:
                        if receipt.observed_identity != operation.baseline:
                            raise LedgerError("binding_mismatch")
                    encoded = receipt.to_json()
                    await connection.execute(
                        """
                        UPDATE release_control.release_card
                        SET host_receipt_id=$2, host_receipt_sha256=$3,
                            host_receipt=$4::jsonb, host_recorded_at=$5, status=$6
                        WHERE id=$1
                        """,
                        approval_id,
                        receipt.receipt_id,
                        bytes.fromhex(receipt.digest),
                        encoded,
                        recorded_at,
                        receipt.status.value,
                    )
                    if receipt.status in _TERMINAL:
                        await connection.execute(
                            """
                            UPDATE release_control.target_queue
                            SET active_approval_id=NULL, active_fence=NULL, active_until=NULL
                            WHERE target=$1 AND active_approval_id=$2 AND active_fence=$3
                            """,
                            row["target"],
                            approval_id,
                            receipt.fence,
                        )
                    return receipt
            except LedgerError:
                raise
            except Exception:
                raise LedgerError("storage_error") from None

    async def bind_host_operation(
        self,
        approval_id: UUID,
        *,
        fence: int,
        operation: HostOperationBinding,
    ) -> HostOperationBinding:
        """Persist the exact host baseline/payload under the active target fence."""
        if (
            not isinstance(approval_id, UUID)
            or type(fence) is not int
            or fence <= 0
            or not isinstance(operation, HostOperationBinding)
        ):
            raise LedgerError("invalid_card")
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    preliminary = await connection.fetchrow(
                        "SELECT target FROM release_control.release_card WHERE id=$1",
                        approval_id,
                    )
                    if preliminary is None:
                        raise LedgerError("invalid_card")
                    queue = await connection.fetchrow(
                        "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                        preliminary["target"],
                    )
                    row = await connection.fetchrow(
                        "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE",
                        approval_id,
                    )
                    if (
                        queue["active_approval_id"] != approval_id
                        or queue["active_fence"] != fence
                        or row["fence"] != fence
                    ):
                        raise LedgerError("stale_fence")
                    if row["host_operation_id"] is not None:
                        existing = self._host_operation(row)
                        if existing != operation:
                            raise LedgerError("binding_mismatch")
                        return existing
                    if row["status"] != ReleaseStatus.MERGED_VERIFIED.value:
                        raise LedgerError("invalid_transition")
                    binding = _parse_json(row["binding"])
                    deployed = binding.get("deployed_identity")
                    baseline = asdict(operation.baseline)
                    payload = operation.payload
                    if (
                        baseline != deployed
                        or row["merge_recorded_at"] is None
                        or payload.git_sha != row["merge_commit"]
                        or payload.policy_digest != binding.get("policy_digest")
                        or payload.target_sequence != row["sequence"]
                        or payload.config_generation != operation.baseline.config_generation
                        or payload.config_digest != operation.baseline.config_digest
                    ):
                        raise LedgerError("binding_mismatch")
                    await connection.execute(
                        """
                        UPDATE release_control.release_card
                        SET host_operation_id=$2, host_baseline_identity=$3::jsonb,
                            host_payload_identity=$4::jsonb, host_payload_sha256=$5
                        WHERE id=$1
                        """,
                        approval_id,
                        operation.operation_id,
                        _canonical(baseline),
                        _canonical(asdict(payload)),
                        bytes.fromhex(operation.payload_digest),
                    )
                    return operation
            except LedgerError:
                raise
            except Exception:
                raise LedgerError("storage_error") from None

    async def get_card(self, approval_id: UUID) -> CardRecord:
        if not isinstance(approval_id, UUID):
            raise LedgerError("invalid_card")
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    "SELECT * FROM release_control.release_card WHERE id=$1",
                    approval_id,
                )
            if row is None:
                raise LedgerError("invalid_card")
            return self._record(row)
        except LedgerError:
            raise
        except Exception:
            raise LedgerError("storage_error") from None

    async def get_card_by_message(self, chat_id: int, message_id: int) -> CardRecord:
        """Trusted ingress lookup only; ordinary app RPC must never expose this method."""
        if (
            type(chat_id) is not int
            or chat_id <= 0
            or type(message_id) is not int
            or message_id <= 0
        ):
            raise LedgerError("invalid_card")
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    "SELECT * FROM release_control.release_card WHERE chat_id=$1 AND message_id=$2",
                    chat_id,
                    message_id,
                )
            if row is None:
                raise LedgerError("invalid_card")
            return self._record(row)
        except LedgerError:
            raise
        except Exception:
            raise LedgerError("storage_error") from None

    async def get_host_receipt(self, approval_id: UUID) -> HostReceipt | None:
        if not isinstance(approval_id, UUID):
            raise LedgerError("invalid_card")
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    "SELECT * FROM release_control.release_card WHERE id=$1",
                    approval_id,
                )
            if row is None:
                raise LedgerError("invalid_card")
            if row["host_receipt_id"] is None:
                return None
            receipt = self._host_receipt(row)
            if receipt.digest != bytes(row["host_receipt_sha256"]).hex():
                raise LedgerError("storage_error")
            return receipt
        except LedgerError:
            raise
        except Exception:
            raise LedgerError("storage_error") from None

    @staticmethod
    def _run_binding(row: asyncpg.Record) -> RunAttemptBinding:
        try:
            return RunAttemptBinding(
                repository_id=row["run_repository_id"],
                run_id=row["run_id"],
                attempt=row["run_attempt"],
                workflow_ref=row["workflow_ref"],
                workflow_sha=row["workflow_sha"],
                check_run_id=row["check_run_id"],
            )
        except Exception:
            raise LedgerError("storage_error") from None

    @staticmethod
    def _host_receipt(row: asyncpg.Record) -> HostReceipt:
        document = _parse_json(row["host_receipt"])
        if not isinstance(document, dict):
            raise LedgerError("storage_error")
        try:
            identity = document["observed_identity"]
            return HostReceipt(
                approval_id=UUID(document["approval_id"]),
                target=document["target"],
                operation_id=UUID(document["operation_id"]),
                receipt_id=document["receipt_id"],
                fence=document["fence"],
                status=ReleaseStatus(document["status"]),
                merge_commit=document["merge_commit"],
                observed_identity=(
                    DeploymentIdentity(**identity) if identity is not None else None
                ),
                observed_at=datetime.fromisoformat(document["observed_at"]),
            )
        except (KeyError, TypeError, ValueError):
            raise LedgerError("storage_error") from None

    @staticmethod
    def _host_operation(row: asyncpg.Record) -> HostOperationBinding:
        try:
            baseline = _parse_json(row["host_baseline_identity"])
            payload = _parse_json(row["host_payload_identity"])
            if not isinstance(baseline, dict) or not isinstance(payload, dict):
                raise ValueError
            operation = HostOperationBinding(
                operation_id=row["host_operation_id"],
                baseline=DeploymentIdentity(**baseline),
                payload=DeploymentIdentity(**payload),
            )
            if operation.payload_digest != bytes(row["host_payload_sha256"]).hex():
                raise ValueError
            return operation
        except Exception:
            raise LedgerError("storage_error") from None

    @staticmethod
    def _verify_command(row: asyncpg.Record, command: DecisionCommand) -> None:
        if (
            row["owner_user_id"] != command.owner_user_id
            or row["chat_id"] != command.chat_id
            or row["message_id"] != command.message_id
            or bytes(row["nonce_sha256"]) != command.nonce_digest
        ):
            raise LedgerError("forbidden")
        if bytes(row["binding_sha256"]).hex() != command.binding_digest:
            raise LedgerError("binding_mismatch")
        binding = _parse_json(row["binding"])
        if (
            not isinstance(binding, dict)
            or binding.get("target") != row["target"]
            or hashlib.sha256(_canonical(binding).encode("utf-8")).digest()
            != bytes(row["binding_sha256"])
        ):
            raise LedgerError("storage_error")

    @staticmethod
    def _result_json(result: DecisionResult) -> dict[str, object]:
        return {
            "approval_id": str(result.approval_id),
            "decision": result.decision.value,
            "status": result.status.value,
            "sequence": result.sequence,
            "binding_digest": result.binding_digest,
        }

    @classmethod
    def _result(cls, value: object) -> DecisionResult:
        if not isinstance(value, dict):
            raise LedgerError("storage_error")
        try:
            return DecisionResult(
                UUID(value["approval_id"]),
                Decision(value["decision"]),
                ReleaseStatus(value["status"]),
                int(value["sequence"]),
                value["binding_digest"],
            )
        except (KeyError, TypeError, ValueError):
            raise LedgerError("storage_error") from None

    @classmethod
    def _row_result(cls, row: asyncpg.Record) -> DecisionResult:
        return DecisionResult(
            row["id"],
            Decision(row["decision"]),
            ReleaseStatus(row["status"]),
            row["sequence"],
            bytes(row["binding_sha256"]).hex(),
        )

    @staticmethod
    def _record(row: asyncpg.Record) -> CardRecord:
        decision = Decision(row["decision"]) if row["decision"] is not None else None
        run_attempt = ReleaseLedger._run_binding(row) if row["run_bound_at"] is not None else None
        merge_evidence = (
            MergeEvidence(
                merge_commit=row["merge_commit"],
                parents=(row["merge_parent_base"], row["merge_parent_head"]),
                tree=row["merge_tree"],
            )
            if row["merge_recorded_at"] is not None
            else None
        )
        return CardRecord(
            approval_id=row["id"],
            target=row["target"],
            sequence=row["sequence"],
            status=ReleaseStatus(row["status"]),
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            binding_digest=bytes(row["binding_sha256"]).hex(),
            owner_user_id=row["owner_user_id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            binding=_parse_json(row["binding"]),
            decision=decision,
            decision_at=row["decision_at"],
            nonce_consumed_at=row["nonce_consumed_at"],
            fence=row["fence"],
            run_attempt=run_attempt,
            merge_evidence=merge_evidence,
            expected_merge_tree=row["expected_merge_tree"],
            host_operation_id=row["host_operation_id"],
            host_operation=(
                ReleaseLedger._host_operation(row) if row["host_operation_id"] is not None else None
            ),
            host_receipt_id=row["host_receipt_id"],
        )

    @classmethod
    def _claim_from_rows(
        cls,
        approval_id: UUID,
        fence: int,
        lease_until: datetime,
        card: asyncpg.Record | None,
    ) -> DispatchClaim:
        if card is None:
            raise LedgerError("storage_error")
        return DispatchClaim(
            approval_id=approval_id,
            target=card["target"],
            sequence=card["sequence"],
            fence=fence,
            lease_until=lease_until,
            binding_digest=bytes(card["binding_sha256"]).hex(),
        )


__all__ = [
    "CardNonce",
    "CardRecord",
    "CardReceipt",
    "Decision",
    "DecisionCommand",
    "DecisionResult",
    "DispatchClaim",
    "HostOperationBinding",
    "HostReceipt",
    "LedgerError",
    "ReleaseBinding",
    "ReleaseLedger",
    "ReleaseStatus",
    "RunAttemptBinding",
]
