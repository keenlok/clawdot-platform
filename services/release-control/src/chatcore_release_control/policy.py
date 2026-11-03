from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

MAX_MANIFEST_BYTES = 64 * 1024
MAX_POLICY_PATHS = 2048
MAX_CHECKS = 64
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CHECK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./-]{0,99}$")


class PolicyError(ValueError):
    """A bounded, non-sensitive release-policy validation failure."""

    def __init__(self, code: str = "invalid_policy") -> None:
        if code not in {
            "invalid_policy",
            "invalid_path",
            "private_path",
            "protected_change",
            "unapproved_path",
            "invalid_object_type",
        }:
            code = "invalid_policy"
        super().__init__(code)
        self.code = code


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and (_SHA1.fullmatch(value) or _SHA256.fullmatch(value))


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _safe_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PolicyError("invalid_path")
    if value.startswith("/") or any(ord(char) < 32 for char in value):
        raise PolicyError("invalid_path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PolicyError("invalid_path")
    if str(PurePosixPath(value)) != value:
        raise PolicyError("invalid_path")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PolicyError("invalid_path") from exc
    return value


def _is_private_path(path: str) -> bool:
    parts = path.split("/")
    lowered = [part.casefold() for part in parts]
    for original, name in zip(parts, lowered, strict=True):
        if name.endswith(".ini"):
            return True
        if name.startswith(".env") and not (original == ".env.example"):
            return True
        if name.endswith(("~", ".bak", ".backup", ".old", ".orig", ".swp", ".swo")):
            return True
        if (
            name in {"credentials", "secrets", "id_rsa", "id_ed25519"}
            or name.startswith("credentials.")
            or name.startswith("secrets.")
        ):
            return True
        if name.endswith((".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3", ".dump")):
            return True
        if "backup" in name:
            return True
    return False


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError()
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise PolicyError()


@dataclass(frozen=True)
class TrustedCheck:
    name: str
    creator_app_id: int
    workflow_path: str
    workflow_digest: str

    def __post_init__(self) -> None:
        try:
            workflow_path = _safe_path(self.workflow_path)
        except PolicyError as exc:
            raise PolicyError() from exc
        if (
            not isinstance(self.name, str)
            or not _CHECK_NAME.fullmatch(self.name)
            or type(self.creator_app_id) is not int
            or self.creator_app_id <= 0
            or not workflow_path.startswith(".github/workflows/")
            or not _valid_digest(self.workflow_digest)
        ):
            raise PolicyError()


@dataclass(frozen=True)
class PolicyManifest:
    repository_id: int
    owner_id: int
    target: str
    workflow_digests: tuple[tuple[str, str], ...]
    allowed_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    checks: tuple[TrustedCheck, ...]
    version: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.repository_id) is not int
            or self.repository_id <= 0
            or type(self.owner_id) is not int
            or self.owner_id <= 0
            or not isinstance(self.target, str)
            or not _TARGET.fullmatch(self.target)
            or not isinstance(self.workflow_digests, tuple)
            or not isinstance(self.allowed_paths, tuple)
            or not isinstance(self.protected_paths, tuple)
            or not isinstance(self.checks, tuple)
            or len(self.workflow_digests) > MAX_POLICY_PATHS
            or len(self.allowed_paths) > MAX_POLICY_PATHS
            or len(self.protected_paths) > MAX_POLICY_PATHS
            or not 1 <= len(self.checks) <= MAX_CHECKS
        ):
            raise PolicyError()

        workflow_paths: set[str] = set()
        for path, digest in self.workflow_digests:
            safe = _safe_path(path)
            if (
                not safe.startswith(".github/workflows/")
                or not safe.endswith((".yml", ".yaml"))
                or not _valid_digest(digest)
                or safe in workflow_paths
            ):
                raise PolicyError()
            workflow_paths.add(safe)

        allowed = self._validated_path_set(self.allowed_paths)
        protected = self._validated_path_set(self.protected_paths)
        if allowed & protected:
            raise PolicyError()
        if not workflow_paths:
            raise PolicyError()
        check_keys: set[str] = set()
        workflow_digest_map = dict(self.workflow_digests)
        for check in self.checks:
            if not isinstance(check, TrustedCheck):
                raise PolicyError()
            if (
                check.name in check_keys
                or workflow_digest_map.get(check.workflow_path) != check.workflow_digest
            ):
                raise PolicyError()
            check_keys.add(check.name)

    @staticmethod
    def _validated_path_set(paths: tuple[str, ...]) -> set[str]:
        result: set[str] = set()
        for raw_path in paths:
            path = _safe_path(raw_path)
            if any(token in path for token in ("*", "?", "[", "]")):
                raise PolicyError()
            if _is_private_path(path) or path in result:
                raise PolicyError()
            result.add(path)
        return result

    @classmethod
    def from_json(cls, raw: str | bytes) -> PolicyManifest:
        try:
            data = raw.encode("utf-8") if isinstance(raw, str) else raw
            if not isinstance(data, bytes) or len(data) > MAX_MANIFEST_BYTES:
                raise PolicyError()
            value = json.loads(
                data.decode("utf-8", errors="strict"),
                object_pairs_hook=_pairs_no_duplicates,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError) as exc:
            raise PolicyError() from exc
        if not isinstance(value, dict) or set(value) != {
            "version",
            "repository_id",
            "owner_id",
            "target",
            "workflow_digests",
            "allowed_paths",
            "protected_paths",
            "checks",
        }:
            raise PolicyError()
        workflows = value["workflow_digests"]
        checks = value["checks"]
        if (
            not isinstance(workflows, dict)
            or not isinstance(value["allowed_paths"], list)
            or not isinstance(value["protected_paths"], list)
            or not isinstance(checks, list)
        ):
            raise PolicyError()
        parsed_checks: list[TrustedCheck] = []
        for item in checks:
            if not isinstance(item, dict) or set(item) != {
                "name",
                "creator_app_id",
                "workflow_path",
                "workflow_sha",
            }:
                raise PolicyError()
            parsed_checks.append(
                TrustedCheck(
                    name=item["name"],
                    creator_app_id=item["creator_app_id"],
                    workflow_path=item["workflow_path"],
                    workflow_digest=item["workflow_sha"],
                )
            )
        return cls(
            repository_id=value["repository_id"],
            owner_id=value["owner_id"],
            target=value["target"],
            workflow_digests=tuple(sorted(workflows.items())),
            allowed_paths=tuple(value["allowed_paths"]),
            protected_paths=tuple(value["protected_paths"]),
            checks=tuple(parsed_checks),
            version=value["version"],
        )

    @property
    def digest(self) -> str:
        canonical = {
            "version": self.version,
            "repository_id": self.repository_id,
            "owner_id": self.owner_id,
            "target": self.target,
            "workflow_digests": dict(self.workflow_digests),
            "allowed_paths": sorted(self.allowed_paths),
            "protected_paths": sorted(self.protected_paths),
            "checks": [
                {
                    "name": check.name,
                    "creator_app_id": check.creator_app_id,
                    "workflow_path": check.workflow_path,
                    "workflow_sha": check.workflow_digest,
                }
                for check in sorted(self.checks, key=lambda item: item.name)
            ],
        }
        encoded = json.dumps(
            canonical, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def validate_changes(self, changes: tuple[Change, ...]) -> None:
        if not isinstance(changes, tuple) or len(changes) > MAX_POLICY_PATHS:
            raise PolicyError()
        allowed = set(self.allowed_paths)
        protected = set(self.protected_paths)
        seen: set[str] = set()
        for change in changes:
            if not isinstance(change, Change):
                raise PolicyError()
            if _is_private_path(change.path) or (
                change.old_path is not None and _is_private_path(change.old_path)
            ):
                raise PolicyError("private_path")
            if change.path in protected or change.old_path in protected:
                raise PolicyError("protected_change")
            if change.object_type != "blob" or change.change_type in {"renamed", "copied"}:
                raise PolicyError("invalid_object_type")
            paths = (change.path,) if change.old_path is None else (change.path, change.old_path)
            for path in paths:
                if path not in allowed:
                    raise PolicyError("unapproved_path")
            if change.path in seen:
                raise PolicyError()
            seen.add(change.path)


@dataclass(frozen=True)
class Change:
    path: str
    change_type: str = "modified"
    object_type: str = "blob"
    old_path: str | None = None

    def __post_init__(self) -> None:
        _safe_path(self.path)
        if self.old_path is not None:
            _safe_path(self.old_path)
        if self.change_type not in {"added", "modified", "deleted", "renamed", "copied"}:
            raise PolicyError()
        if (self.change_type in {"renamed", "copied"}) != (self.old_path is not None):
            raise PolicyError()
        if self.object_type not in {"blob", "symlink", "commit", "tree"}:
            raise PolicyError("invalid_object_type")


@dataclass(frozen=True)
class ReleaseBundle:
    deployed_commit: str
    base_commit: str
    head_commit: str
    deployed_tree: str
    base_tree: str
    head_tree: str
    policy_digest: str
    base_is_ancestor_of_head: bool
    base_is_descendant_of_deployed: bool
    same_tree_continuation_recorded: bool
    complete_delta_reviewed: bool
    deployed_to_base: tuple[Change, ...]
    base_to_head: tuple[Change, ...]
    deployed_to_head: tuple[Change, ...]

    def __post_init__(self) -> None:
        if any(
            not _valid_sha(value)
            for value in (
                self.deployed_commit,
                self.base_commit,
                self.head_commit,
                self.deployed_tree,
                self.base_tree,
                self.head_tree,
            )
        ) or not _valid_digest(self.policy_digest):
            raise PolicyError()
        if any(
            type(value) is not bool
            for value in (
                self.base_is_ancestor_of_head,
                self.base_is_descendant_of_deployed,
                self.same_tree_continuation_recorded,
                self.complete_delta_reviewed,
            )
        ):
            raise PolicyError()
        if any(
            not isinstance(changes, tuple) or any(not isinstance(item, Change) for item in changes)
            for changes in (self.deployed_to_base, self.base_to_head, self.deployed_to_head)
        ):
            raise PolicyError()


@dataclass(frozen=True)
class ReleaseDecision:
    eligible: bool
    reason: str
    deployed_to_base: tuple[Change, ...]
    base_to_head: tuple[Change, ...]
    deployed_to_head: tuple[Change, ...]


def evaluate_ordinary_release(policy: PolicyManifest, bundle: ReleaseBundle) -> ReleaseDecision:
    def deny(reason: str) -> ReleaseDecision:
        return ReleaseDecision(
            False,
            reason,
            bundle.deployed_to_base,
            bundle.base_to_head,
            bundle.deployed_to_head,
        )

    if bundle.policy_digest != policy.digest:
        return deny("policy_mismatch")
    if not bundle.complete_delta_reviewed:
        return deny("incomplete_review")
    if not bundle.base_is_ancestor_of_head:
        return deny("unknown_base")
    if bundle.base_commit == bundle.deployed_commit:
        if bundle.base_tree != bundle.deployed_tree:
            return deny("unknown_base")
    elif not (
        bundle.base_is_descendant_of_deployed
        and bundle.same_tree_continuation_recorded
        and bundle.base_tree == bundle.deployed_tree
    ):
        return deny("unknown_base")

    try:
        policy.validate_changes(bundle.deployed_to_base)
        policy.validate_changes(bundle.base_to_head)
        policy.validate_changes(bundle.deployed_to_head)
    except PolicyError as exc:
        return deny(exc.code)
    return ReleaseDecision(
        True,
        "eligible",
        bundle.deployed_to_base,
        bundle.base_to_head,
        bundle.deployed_to_head,
    )


def evaluate_reconciliation_release(
    policy: PolicyManifest, bundle: ReleaseBundle
) -> ReleaseDecision:
    """Evaluate an explicitly owner-reviewed D-to-H reconciliation release.

    This path relaxes only the ordinary B-to-D provenance rule. The PR base must
    still be an ancestor of H, the complete delta must be reviewed, and policy
    validation still covers D-to-B, B-to-H, and D-to-H.
    """

    def deny(reason: str) -> ReleaseDecision:
        return ReleaseDecision(
            False,
            reason,
            bundle.deployed_to_base,
            bundle.base_to_head,
            bundle.deployed_to_head,
        )

    if bundle.policy_digest != policy.digest:
        return deny("policy_mismatch")
    if not bundle.complete_delta_reviewed:
        return deny("incomplete_review")
    if not bundle.base_is_ancestor_of_head:
        return deny("unknown_base")
    try:
        policy.validate_changes(bundle.deployed_to_base)
        policy.validate_changes(bundle.base_to_head)
        policy.validate_changes(bundle.deployed_to_head)
    except PolicyError as exc:
        return deny(exc.code)
    return ReleaseDecision(
        True,
        "eligible",
        bundle.deployed_to_base,
        bundle.base_to_head,
        bundle.deployed_to_head,
    )


def release_review_matches_bundle(bundle: ReleaseBundle, review: Mapping[str, Any]) -> bool:
    """Check the immutable owner-visible D/B/H facts against extracted Git facts."""
    if (
        not isinstance(bundle, ReleaseBundle)
        or not isinstance(review, Mapping)
        or review.get("complete_delta_reviewed") is not True
        or review.get("base_is_ancestor_of_head") is not bundle.base_is_ancestor_of_head
        or review.get("base_is_descendant_of_deployed") is not bundle.base_is_descendant_of_deployed
        or type(review.get("same_tree_continuation_recorded", False)) is not bool
        or review.get("same_tree_continuation_recorded", False)
        != bundle.same_tree_continuation_recorded
    ):
        return False
    scalar_fields = {
        "deployed_commit": bundle.deployed_commit,
        "base_commit": bundle.base_commit,
        "head_commit": bundle.head_commit,
        "deployed_tree": bundle.deployed_tree,
        "base_tree": bundle.base_tree,
        "head_tree": bundle.head_tree,
        "policy_digest": bundle.policy_digest,
    }
    if any(review.get(key) != expected for key, expected in scalar_fields.items()):
        return False
    for key, changes in (
        ("deployed_to_base", bundle.deployed_to_base),
        ("base_to_head", bundle.base_to_head),
        ("deployed_to_head", bundle.deployed_to_head),
    ):
        claimed = review.get(key)
        if not isinstance(claimed, list):
            return False
        normalized: list[dict[str, str]] = []
        for item in claimed:
            if not isinstance(item, Mapping) or set(item) - {
                "path",
                "change_type",
                "object_type",
                "old_path",
            }:
                return False
            if not isinstance(item.get("path"), str):
                return False
            change_type = item.get("change_type", "modified")
            object_type = item.get("object_type", "blob")
            old_path = item.get("old_path")
            if (
                not isinstance(change_type, str)
                or not isinstance(object_type, str)
                or (old_path is not None and not isinstance(old_path, str))
            ):
                return False
            value = {
                "path": item["path"],
                "change_type": change_type,
                "object_type": object_type,
            }
            if old_path is not None:
                value["old_path"] = old_path
            normalized.append(value)
        actual = [
            {
                "path": change.path,
                "change_type": change.change_type,
                "object_type": change.object_type,
                **({"old_path": change.old_path} if change.old_path is not None else {}),
            }
            for change in changes
        ]
        if normalized != actual:
            return False
    return True


def evaluate_reviewed_release(
    policy: PolicyManifest,
    bundle: ReleaseBundle,
    review: Mapping[str, Any],
) -> ReleaseDecision:
    """Apply the eligibility mode bound to a protected review/card bundle.

    Missing `release_kind` is the legacy ordinary path. Reconciliation is never
    inferred from Git state; it must be an explicit, immutable review field.
    """
    kind = review.get("release_kind", "ordinary") if isinstance(review, Mapping) else None
    if type(kind) is not str or kind not in {"ordinary", "reconciliation"}:
        reason = "invalid_review"
    elif not release_review_matches_bundle(bundle, review):
        reason = "review_mismatch"
    else:
        attested = replace(bundle, complete_delta_reviewed=True)
        if kind == "reconciliation":
            return evaluate_reconciliation_release(policy, attested)
        return evaluate_ordinary_release(policy, attested)
    return ReleaseDecision(
        False,
        reason,
        bundle.deployed_to_base,
        bundle.base_to_head,
        bundle.deployed_to_head,
    )


@dataclass(frozen=True)
class CheckEvidence:
    name: str
    repository_id: int
    event: str
    workflow_path: str
    workflow_sha: str
    workflow_digest: str
    run_id: int
    attempt: int
    creator_app_id: int
    head_commit: str
    head_tree: str
    conclusion: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not _CHECK_NAME.fullmatch(self.name)
            or type(self.repository_id) is not int
            or self.repository_id <= 0
            or type(self.event) is not str
            or type(self.workflow_path) is not str
            or type(self.workflow_sha) is not str
            or not _valid_digest(self.workflow_digest)
            or type(self.run_id) is not int
            or self.run_id <= 0
            or type(self.attempt) is not int
            or self.attempt <= 0
            or type(self.creator_app_id) is not int
            or self.creator_app_id <= 0
            or not _valid_sha(self.head_commit)
            or not _valid_sha(self.head_tree)
            or type(self.conclusion) is not str
        ):
            raise PolicyError()
        _safe_path(self.workflow_path)


def validate_check_evidence(
    policy: PolicyManifest,
    evidence: CheckEvidence,
    bundle: ReleaseBundle,
    *,
    expected_run_id: int,
    expected_attempt: int,
    expected_workflow_sha: str,
) -> bool:
    if (
        type(expected_run_id) is not int
        or expected_run_id <= 0
        or type(expected_attempt) is not int
        or expected_attempt <= 0
        or not _valid_sha(expected_workflow_sha)
    ):
        return False
    trusted = next((check for check in policy.checks if check.name == evidence.name), None)
    expected_workflow_digests = dict(policy.workflow_digests)
    return bool(
        trusted is not None
        and evidence.repository_id == policy.repository_id
        and evidence.event == "pull_request"
        and evidence.workflow_path == trusted.workflow_path
        and evidence.workflow_sha == expected_workflow_sha
        and _valid_sha(expected_workflow_sha)
        and evidence.workflow_digest == trusted.workflow_digest
        and expected_workflow_digests.get(evidence.workflow_path) == evidence.workflow_digest
        and evidence.creator_app_id == trusted.creator_app_id
        and evidence.run_id == expected_run_id
        and evidence.attempt == expected_attempt
        and evidence.head_commit == bundle.head_commit
        and evidence.head_tree == bundle.head_tree
        and evidence.conclusion == "success"
    )


def validate_all_required_checks(
    policy: PolicyManifest,
    evidence: tuple[CheckEvidence, ...],
    bundle: ReleaseBundle,
    *,
    expected_run_id: int,
    expected_attempt: int,
    expected_workflow_sha: str,
) -> bool:
    if not isinstance(evidence, tuple) or len(evidence) != len(policy.checks):
        return False
    names = [item.name for item in evidence if isinstance(item, CheckEvidence)]
    if len(names) != len(evidence) or len(set(names)) != len(names):
        return False
    if set(names) != {check.name for check in policy.checks}:
        return False
    return all(
        validate_check_evidence(
            policy,
            item,
            bundle,
            expected_run_id=expected_run_id,
            expected_attempt=expected_attempt,
            expected_workflow_sha=expected_workflow_sha,
        )
        for item in evidence
    )


@dataclass(frozen=True)
class MergeEvidence:
    merge_commit: str
    parents: tuple[str, ...]
    tree: str

    def __post_init__(self) -> None:
        if (
            not _valid_sha(self.merge_commit)
            or not isinstance(self.parents, tuple)
            or len(self.parents) != 2
            or any(not _valid_sha(parent) for parent in self.parents)
            or not _valid_sha(self.tree)
        ):
            raise PolicyError()


@dataclass(frozen=True)
class MergeDecision:
    deployable: bool
    outcome: str
    merge_commit: str


def reconcile_merge(
    bundle: ReleaseBundle, merge: MergeEvidence, *, expected_merge_tree: str
) -> MergeDecision:
    expected = (
        merge.merge_commit not in merge.parents
        and merge.parents == (bundle.base_commit, bundle.head_commit)
        and _valid_sha(expected_merge_tree)
        and merge.tree == expected_merge_tree
    )
    return MergeDecision(
        deployable=expected,
        outcome="merged_verified" if expected else "merged_blocked",
        merge_commit=merge.merge_commit,
    )


@dataclass(frozen=True)
class DeploymentIdentity:
    git_sha: str
    image_digest: str
    config_generation: int
    config_digest: str
    policy_digest: str
    target_sequence: int

    def __post_init__(self) -> None:
        if (
            not _valid_sha(self.git_sha)
            or not isinstance(self.image_digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_digest)
            or type(self.config_generation) is not int
            or self.config_generation < 0
            or not _valid_digest(self.config_digest)
            or not _valid_digest(self.policy_digest)
            or type(self.target_sequence) is not int
            or self.target_sequence < 0
        ):
            raise PolicyError()


@dataclass(frozen=True)
class SwitchContext:
    affirmative_approval: bool
    approval_is_active: bool
    approval_valid_until: int
    now: int
    owner_is_current: bool
    policy_is_current: bool
    current_main: str
    expected_merge_commit: str
    current_sequence: int
    expected_sequence: int
    current_fence: int
    expected_fence: int
    current_deployment: DeploymentIdentity
    expected_deployment: DeploymentIdentity
    start_deadline: int
    max_start_window_seconds: int


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str


def admit_switch(context: SwitchContext) -> AdmissionDecision:
    def deny(reason: str) -> AdmissionDecision:
        return AdmissionDecision(False, reason)

    if type(context.affirmative_approval) is not bool or not context.affirmative_approval:
        return deny("approval_required")
    if type(context.approval_is_active) is not bool or not context.approval_is_active:
        return deny("approval_revoked")
    if (
        type(context.now) is not int
        or type(context.approval_valid_until) is not int
        or context.now >= context.approval_valid_until
    ):
        return deny("approval_expired")
    if context.owner_is_current is not True:
        return deny("owner_changed")
    if context.policy_is_current is not True:
        return deny("policy_changed")
    if (
        not _valid_sha(context.current_main)
        or context.current_main != context.expected_merge_commit
        or not _valid_sha(context.expected_merge_commit)
    ):
        return deny("main_moved")
    if (
        type(context.current_sequence) is not int
        or type(context.expected_sequence) is not int
        or context.current_sequence != context.expected_sequence
    ):
        return deny("sequence_changed")
    if (
        type(context.current_fence) is not int
        or type(context.expected_fence) is not int
        or context.current_fence != context.expected_fence
    ):
        return deny("fence_changed")
    if (
        not isinstance(context.current_deployment, DeploymentIdentity)
        or not isinstance(context.expected_deployment, DeploymentIdentity)
        or context.current_deployment != context.expected_deployment
        or context.current_deployment.target_sequence != context.current_sequence
    ):
        return deny("baseline_changed")
    if (
        type(context.max_start_window_seconds) is not int
        or context.max_start_window_seconds <= 0
        or type(context.start_deadline) is not int
        or context.start_deadline <= context.now
        or context.start_deadline - context.now > context.max_start_window_seconds
    ):
        return deny("start_window_invalid")
    return AdmissionDecision(True, "admitted")


class JournalState(StrEnum):
    PREPARED = "prepared"
    SWITCHING = "switching"
    CHECKING = "checking"
    HEALTHY = "healthy"
    RESTORING = "restoring"
    RESTORED_HEALTHY = "restored_healthy"
    RECOVERY_FAILED = "recovery_failed"


_JOURNAL_TRANSITIONS = {
    JournalState.PREPARED: {JournalState.SWITCHING},
    JournalState.SWITCHING: {JournalState.CHECKING, JournalState.RESTORING},
    JournalState.CHECKING: {
        JournalState.HEALTHY,
        JournalState.RESTORING,
        JournalState.RECOVERY_FAILED,
    },
    JournalState.RESTORING: {JournalState.RESTORED_HEALTHY, JournalState.RECOVERY_FAILED},
    JournalState.HEALTHY: set(),
    JournalState.RESTORED_HEALTHY: set(),
    JournalState.RECOVERY_FAILED: set(),
}


def validate_journal_transition(
    current: JournalState,
    requested: JournalState,
    *,
    current_operation_id: str,
    requested_operation_id: str,
) -> bool:
    return (
        isinstance(current, JournalState)
        and isinstance(requested, JournalState)
        and isinstance(current_operation_id, str)
        and isinstance(requested_operation_id, str)
        and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", current_operation_id))
        and current_operation_id == requested_operation_id
        and requested in _JOURNAL_TRANSITIONS[current]
    )
