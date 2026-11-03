from __future__ import annotations

import json
from dataclasses import replace

import pytest
from chatcore_release_control.policy import (
    Change,
    CheckEvidence,
    DeploymentIdentity,
    JournalState,
    MergeEvidence,
    PolicyError,
    PolicyManifest,
    ReleaseBundle,
    SwitchContext,
    admit_switch,
    evaluate_ordinary_release,
    evaluate_reconciliation_release,
    evaluate_reviewed_release,
    reconcile_merge,
    validate_all_required_checks,
    validate_check_evidence,
    validate_journal_transition,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def policy_data() -> dict[str, object]:
    return {
        "version": 1,
        "repository_id": 101,
        "owner_id": 202,
        "target": "production",
        "workflow_digests": {".github/workflows/ci.yml": DIGEST_A},
        "allowed_paths": ["packages/plugin-web/src/chatcore_web/plugin.py"],
        "protected_paths": [".github/workflows/ci.yml", "uv.lock"],
        "checks": [
            {
                "name": "candidate-tests",
                "creator_app_id": 303,
                "workflow_path": ".github/workflows/ci.yml",
                "workflow_sha": DIGEST_A,
            }
        ],
    }


def manifest() -> PolicyManifest:
    return PolicyManifest.from_json(json.dumps(policy_data()))


def bundle(**changes: object) -> ReleaseBundle:
    defaults: dict[str, object] = {
        "deployed_commit": SHA_A,
        "base_commit": SHA_A,
        "head_commit": SHA_B,
        "deployed_tree": SHA_C,
        "base_tree": SHA_C,
        "head_tree": SHA_D,
        "policy_digest": manifest().digest,
        "base_is_ancestor_of_head": True,
        "base_is_descendant_of_deployed": True,
        "same_tree_continuation_recorded": False,
        "complete_delta_reviewed": True,
        "deployed_to_base": (),
        "base_to_head": (Change("packages/plugin-web/src/chatcore_web/plugin.py"),),
        "deployed_to_head": (Change("packages/plugin-web/src/chatcore_web/plugin.py"),),
    }
    defaults.update(changes)
    return ReleaseBundle(**defaults)


def test_manifest_digest_is_canonical_and_unknown_or_wildcard_policy_is_rejected() -> None:
    raw = json.dumps(policy_data(), indent=2)
    assert PolicyManifest.from_json(raw).digest == manifest().digest

    wildcard = policy_data()
    wildcard["allowed_paths"] = ["packages/plugin-web/**"]
    with pytest.raises(PolicyError):
        PolicyManifest.from_json(json.dumps(wildcard))

    duplicate = '{"version":1,"version":1}'
    with pytest.raises(PolicyError):
        PolicyManifest.from_json(duplicate)

    with pytest.raises(PolicyError):
        PolicyManifest.from_json("\ud800")


@pytest.mark.parametrize(
    "path",
    [
        "config/private.ini",
        "config/.env.production",
        "config/.ENV",
        "config/token.json.bak",
        "config/db.dump",
        "../outside.py",
    ],
)
def test_private_and_noncanonical_paths_are_rejected(path: str) -> None:
    with pytest.raises(PolicyError):
        manifest().validate_changes((Change(path),))


def test_env_example_is_not_treated_as_private_configuration() -> None:
    policy = policy_data()
    policy["allowed_paths"] = ["apps/sb_bot/.env.example"]
    assert (
        PolicyManifest.from_json(json.dumps(policy)).validate_changes(
            (Change("apps/sb_bot/.env.example"),)
        )
        is None
    )


@pytest.mark.parametrize(
    "path", ["config/secrets.json", "data/production.db", "packages/plugin-web/backup.py"]
)
def test_secret_and_database_artifact_paths_cannot_be_allowlisted(path: str) -> None:
    policy = policy_data()
    policy["allowed_paths"] = [path]
    with pytest.raises(PolicyError):
        PolicyManifest.from_json(json.dumps(policy))


@pytest.mark.parametrize(
    "change",
    [
        Change("uv.lock"),
        Change("packages/plugin-web/src/chatcore_web/plugin.py", object_type="symlink"),
        Change("packages/plugin-web/src/chatcore_web/plugin.py", object_type="commit"),
        Change(
            "packages/plugin-web/src/chatcore_web/plugin.py",
            change_type="renamed",
            old_path="packages/plugin-web/src/chatcore_web/old.py",
        ),
    ],
)
def test_protected_and_non_regular_candidate_changes_require_maintenance(change: Change) -> None:
    with pytest.raises(PolicyError):
        manifest().validate_changes((change,))


def test_ordinary_release_requires_complete_review_and_recorded_base_provenance() -> None:
    decision = evaluate_ordinary_release(manifest(), bundle())
    assert decision.eligible
    assert decision.deployed_to_base == ()
    assert decision.base_to_head[0].path.endswith("plugin.py")
    assert decision.deployed_to_head[0].path.endswith("plugin.py")

    unknown_base = bundle(
        deployed_commit=SHA_A,
        base_commit=SHA_B,
        base_tree=SHA_D,
        same_tree_continuation_recorded=False,
    )
    assert evaluate_ordinary_release(manifest(), unknown_base).reason == "unknown_base"

    incomplete = bundle(complete_delta_reviewed=False)
    assert evaluate_ordinary_release(manifest(), incomplete).reason == "incomplete_review"

    inherited_control = bundle(
        base_commit=SHA_B,
        base_tree=SHA_C,
        same_tree_continuation_recorded=True,
        deployed_to_base=(Change("uv.lock"),),
        deployed_to_head=(
            Change("uv.lock"),
            Change("packages/plugin-web/src/chatcore_web/plugin.py"),
        ),
    )
    assert evaluate_ordinary_release(manifest(), inherited_control).reason == "protected_change"


def test_same_tree_continuation_requires_ancestry_and_recorded_provenance() -> None:
    accepted = bundle(
        base_commit=SHA_B,
        base_tree=SHA_C,
        same_tree_continuation_recorded=True,
        base_is_descendant_of_deployed=True,
    )
    assert evaluate_ordinary_release(manifest(), accepted).eligible

    rewritten = bundle(
        base_commit=SHA_B,
        base_tree=SHA_C,
        same_tree_continuation_recorded=True,
        base_is_descendant_of_deployed=False,
    )
    assert evaluate_ordinary_release(manifest(), rewritten).reason == "unknown_base"


def test_reconciliation_release_requires_explicit_kind_and_full_allowed_delta() -> None:
    unmerged_base = bundle(
        deployed_commit=SHA_A,
        base_commit=SHA_B,
        head_commit=SHA_D,
        deployed_tree=SHA_C,
        base_tree=SHA_D,
        head_tree=SHA_C,
        base_is_ancestor_of_head=True,
        base_is_descendant_of_deployed=False,
        deployed_to_base=(Change("packages/plugin-web/src/chatcore_web/plugin.py"),),
        base_to_head=(Change("packages/plugin-web/src/chatcore_web/plugin.py"),),
        deployed_to_head=(),
    )
    assert evaluate_ordinary_release(manifest(), unmerged_base).reason == "unknown_base"
    assert evaluate_reconciliation_release(manifest(), unmerged_base).eligible
    assert not evaluate_reconciliation_release(
        manifest(), replace(unmerged_base, complete_delta_reviewed=False)
    ).eligible

    protected_delta = replace(
        unmerged_base,
        deployed_to_base=(Change("uv.lock"),),
    )
    assert evaluate_reconciliation_release(manifest(), protected_delta).reason == "protected_change"


def test_review_kind_defaults_to_ordinary_and_unknown_or_unbound_kind_denies() -> None:
    ordinary = bundle()
    review = {
        "complete_delta_reviewed": True,
        "deployed_commit": ordinary.deployed_commit,
        "base_commit": ordinary.base_commit,
        "head_commit": ordinary.head_commit,
        "deployed_tree": ordinary.deployed_tree,
        "base_tree": ordinary.base_tree,
        "head_tree": ordinary.head_tree,
        "policy_digest": ordinary.policy_digest,
        "base_is_ancestor_of_head": ordinary.base_is_ancestor_of_head,
        "base_is_descendant_of_deployed": ordinary.base_is_descendant_of_deployed,
        "same_tree_continuation_recorded": ordinary.same_tree_continuation_recorded,
        "deployed_to_base": [],
        "base_to_head": [
            {"path": item.path, "change_type": item.change_type, "object_type": item.object_type}
            for item in ordinary.base_to_head
        ],
        "deployed_to_head": [
            {"path": item.path, "change_type": item.change_type, "object_type": item.object_type}
            for item in ordinary.deployed_to_head
        ],
    }
    assert evaluate_reviewed_release(manifest(), ordinary, review).eligible

    unknown = {**review, "release_kind": "maintenance_bypass"}
    assert evaluate_reviewed_release(manifest(), ordinary, unknown).reason == "invalid_review"

    mismatched = {**review, "head_commit": SHA_D}
    assert evaluate_reviewed_release(manifest(), ordinary, mismatched).reason == "review_mismatch"


def test_check_evidence_binds_creator_workflow_run_attempt_and_exact_tree() -> None:
    item = CheckEvidence(
        name="candidate-tests",
        repository_id=101,
        event="pull_request",
        workflow_path=".github/workflows/ci.yml",
        workflow_sha=SHA_A,
        workflow_digest=DIGEST_A,
        run_id=7,
        attempt=2,
        creator_app_id=303,
        head_commit=SHA_B,
        head_tree=SHA_D,
        conclusion="success",
    )
    assert validate_check_evidence(
        manifest(),
        item,
        bundle(),
        expected_run_id=7,
        expected_attempt=2,
        expected_workflow_sha=SHA_A,
    )
    assert validate_all_required_checks(
        manifest(),
        (item,),
        bundle(),
        expected_run_id=7,
        expected_attempt=2,
        expected_workflow_sha=SHA_A,
    )
    assert not validate_all_required_checks(
        manifest(),
        (),
        bundle(),
        expected_run_id=7,
        expected_attempt=2,
        expected_workflow_sha=SHA_A,
    )
    assert not validate_all_required_checks(
        manifest(),
        (item, item),
        bundle(),
        expected_run_id=7,
        expected_attempt=2,
        expected_workflow_sha=SHA_A,
    )

    for invalid in (
        CheckEvidence(**{**item.__dict__, "creator_app_id": 999}),
        CheckEvidence(**{**item.__dict__, "workflow_sha": DIGEST_B}),
        CheckEvidence(**{**item.__dict__, "attempt": 1}),
        CheckEvidence(**{**item.__dict__, "head_tree": SHA_C}),
        CheckEvidence(**{**item.__dict__, "conclusion": "skipped"}),
    ):
        assert not validate_check_evidence(
            manifest(),
            invalid,
            bundle(),
            expected_run_id=7,
            expected_attempt=2,
            expected_workflow_sha=SHA_A,
        )


def test_merge_reconciliation_accepts_only_expected_immutable_merge() -> None:
    expected = MergeEvidence(merge_commit=SHA_D, parents=(SHA_A, SHA_B), tree=SHA_C)
    assert reconcile_merge(bundle(), expected, expected_merge_tree=SHA_C).deployable

    unexpected = MergeEvidence(merge_commit=SHA_D, parents=(SHA_A, SHA_C), tree=SHA_C)
    result = reconcile_merge(bundle(), unexpected, expected_merge_tree=SHA_C)
    assert not result.deployable
    assert result.outcome == "merged_blocked"

    non_merge = MergeEvidence(merge_commit=SHA_A, parents=(SHA_A, SHA_B), tree=SHA_C)
    assert (
        reconcile_merge(bundle(), non_merge, expected_merge_tree=SHA_C).outcome == "merged_blocked"
    )


def identity(**changes: object) -> DeploymentIdentity:
    values: dict[str, object] = {
        "git_sha": SHA_A,
        "image_digest": "sha256:" + DIGEST_A,
        "config_generation": 5,
        "config_digest": DIGEST_B,
        "policy_digest": DIGEST_C,
        "target_sequence": 8,
    }
    values.update(changes)
    return DeploymentIdentity(**values)


def switch_context(**changes: object) -> SwitchContext:
    values: dict[str, object] = {
        "affirmative_approval": True,
        "approval_is_active": True,
        "approval_valid_until": 500,
        "now": 100,
        "owner_is_current": True,
        "policy_is_current": True,
        "current_main": SHA_D,
        "expected_merge_commit": SHA_D,
        "current_sequence": 8,
        "expected_sequence": 8,
        "current_fence": 12,
        "expected_fence": 12,
        "current_deployment": identity(),
        "expected_deployment": identity(),
        "start_deadline": 160,
        "max_start_window_seconds": 120,
    }
    values.update(changes)
    return SwitchContext(**values)


def test_switch_admission_checks_approval_sequence_fence_baseline_and_deadline() -> None:
    assert admit_switch(switch_context()).admitted

    for invalid in (
        switch_context(affirmative_approval=False),
        switch_context(approval_is_active=False),
        switch_context(approval_valid_until=100),
        switch_context(owner_is_current=False),
        switch_context(policy_is_current=False),
        switch_context(current_main=SHA_C),
        switch_context(current_sequence=9),
        switch_context(current_fence=11),
        switch_context(current_deployment=identity(config_generation=6)),
        switch_context(start_deadline=99),
        switch_context(start_deadline=300),
    ):
        assert not admit_switch(invalid).admitted


def test_journal_transitions_are_monotonic_and_recovery_is_operation_bound() -> None:
    operation = "release-7"
    assert validate_journal_transition(
        JournalState.PREPARED,
        JournalState.SWITCHING,
        current_operation_id=operation,
        requested_operation_id=operation,
    )
    assert validate_journal_transition(
        JournalState.SWITCHING,
        JournalState.CHECKING,
        current_operation_id=operation,
        requested_operation_id=operation,
    )
    assert validate_journal_transition(
        JournalState.CHECKING,
        JournalState.HEALTHY,
        current_operation_id=operation,
        requested_operation_id=operation,
    )
    assert validate_journal_transition(
        JournalState.CHECKING,
        JournalState.RESTORING,
        current_operation_id=operation,
        requested_operation_id=operation,
    )
    assert validate_journal_transition(
        JournalState.RESTORING,
        JournalState.RESTORED_HEALTHY,
        current_operation_id=operation,
        requested_operation_id=operation,
    )
    assert validate_journal_transition(
        JournalState.RESTORING,
        JournalState.RECOVERY_FAILED,
        current_operation_id=operation,
        requested_operation_id=operation,
    )
    assert not validate_journal_transition(
        JournalState.PREPARED,
        JournalState.SWITCHING,
        current_operation_id=operation,
        requested_operation_id="release-8",
    )
    assert not validate_journal_transition(
        JournalState.HEALTHY,
        JournalState.SWITCHING,
        current_operation_id=operation,
        requested_operation_id=operation,
    )
    assert not validate_journal_transition(
        JournalState.PREPARED,
        JournalState.HEALTHY,
        current_operation_id=operation,
        requested_operation_id=operation,
    )
    assert not validate_journal_transition(
        JournalState.RECOVERY_FAILED,
        JournalState.SWITCHING,
        current_operation_id=operation,
        requested_operation_id=operation,
    )
