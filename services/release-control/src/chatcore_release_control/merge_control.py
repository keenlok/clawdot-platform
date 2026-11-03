"""Trusted merge gate; candidate code is never imported or executed here."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from .github_evidence import (
    GitHubEvidenceClient,
    GitHubEvidenceError,
    GitHubEvidenceSnapshot,
    _nested_int,
)
from .oidc import CallerIdentity
from .policy import MergeEvidence, ReleaseBundle, reconcile_merge

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class MergeControlError(RuntimeError):
    """Fixed safe merge denial or uncertain-outcome code."""

    _CODES = frozenset(
        {
            "invalid_input",
            "caller_denied",
            "admission_denied",
            "deployment_moved",
            "policy_moved",
            "stale_pull_request",
            "pull_request_closed",
            "merge_rejected",
            "merge_uncertain",
            "invalid_merge_object",
            "persistence_failed",
        }
    )

    def __init__(self, code: str = "merge_uncertain") -> None:
        self.code = code if code in self._CODES else "merge_uncertain"
        super().__init__(self.code)


@dataclass(frozen=True)
class MergeIntent:
    repository_id: int
    owner_id: int
    target: str
    pr_number: int
    deployed_commit: str
    base_commit: str
    head_commit: str
    policy_digest: str
    expected_merge_tree: str
    caller: CallerIdentity
    evidence: GitHubEvidenceSnapshot


@dataclass(frozen=True)
class MergeOutcome:
    outcome: str
    merge_commit: str
    parents: tuple[str, ...]
    tree: str
    expected_tree: str
    evidence: MergeEvidence | None
    api_response_commit_matches: bool


class GitHubMergeTransport:
    """One fixed PR merge operation over the evidence client's bounded HTTP session."""

    def __init__(self, client: GitHubEvidenceClient) -> None:
        if not isinstance(client, GitHubEvidenceClient):
            raise MergeControlError("invalid_input")
        self._client = client

    def merge_pull_request(self, pr_number: int, head_sha: str) -> dict[str, Any]:
        if (
            type(pr_number) is not int
            or pr_number <= 0
            or not isinstance(head_sha, str)
            or not _SHA.fullmatch(head_sha)
        ):
            raise MergeControlError("invalid_input")
        try:
            return self._client._request_json(
                "PUT",
                f"pulls/{pr_number}/merge",
                json_body={"sha": head_sha, "merge_method": "merge"},
            )
        except GitHubEvidenceError:
            raise MergeControlError("merge_uncertain") from None


class MergeController:
    """Recheck policy and PR under a trusted target lock before one exact-H merge."""

    def __init__(
        self,
        client: GitHubEvidenceClient,
        *,
        repository_id: int,
        owner_id: int,
        target: str,
        base_branch: str = "main",
        merge_transport: GitHubMergeTransport,
        target_lock: Callable[[str], AbstractContextManager[object]],
        current_deployed_commit: Callable[[str], str],
        current_policy_digest: Callable[[], str],
        verify_caller: Callable[[CallerIdentity], bool],
        admit_merge: Callable[[MergeIntent], bool],
        admission_live: Callable[[], bool],
        persist_merge: Callable[[MergeOutcome], None],
    ) -> None:
        if (
            not isinstance(client, GitHubEvidenceClient)
            or type(repository_id) is not int
            or repository_id <= 0
            or type(owner_id) is not int
            or owner_id <= 0
            or client.repository_id != repository_id
            or not isinstance(merge_transport, GitHubMergeTransport)
            or merge_transport._client is not client
            or not isinstance(target, str)
            or not target
            or not isinstance(base_branch, str)
            or not re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", base_branch)
            or any(
                not callable(callback)
                for callback in (
                    target_lock,
                    current_deployed_commit,
                    current_policy_digest,
                    verify_caller,
                    admit_merge,
                    admission_live,
                    persist_merge,
                )
            )
        ):
            raise MergeControlError("invalid_input")
        self._client = client
        self._merge_transport = merge_transport
        self._repository_id = repository_id
        self._owner_id = owner_id
        self._target = target
        self._base_branch = base_branch
        self._target_lock = target_lock
        self._current_deployed_commit = current_deployed_commit
        self._current_policy_digest = current_policy_digest
        self._verify_caller = verify_caller
        self._admit_merge = admit_merge
        self._admission_live = admission_live
        self._persist_merge = persist_merge

    def merge(
        self,
        snapshot: GitHubEvidenceSnapshot,
        bundle: ReleaseBundle,
        *,
        caller: CallerIdentity,
        prior_admission: bool = False,
    ) -> MergeOutcome:
        if (
            not isinstance(snapshot, GitHubEvidenceSnapshot)
            or not isinstance(bundle, ReleaseBundle)
            or not isinstance(caller, CallerIdentity)
            or type(prior_admission) is not bool
            or snapshot.repository_id != self._repository_id
            or snapshot.target_branch != self._base_branch
            or snapshot.base_commit != bundle.base_commit
            or snapshot.head_commit != bundle.head_commit
            or snapshot.head_tree != bundle.head_tree
            or snapshot.synthetic_merge_parents != (bundle.base_commit, bundle.head_commit)
            or not snapshot.checks
            or len(snapshot.checks) != len(snapshot.check_run_ids)
            or not bundle.base_is_ancestor_of_head
            or not _SHA.fullmatch(snapshot.synthetic_merge_tree)
        ):
            raise MergeControlError("invalid_input")
        intent = MergeIntent(
            self._repository_id,
            self._owner_id,
            self._target,
            snapshot.pr_number,
            bundle.deployed_commit,
            bundle.base_commit,
            bundle.head_commit,
            bundle.policy_digest,
            snapshot.synthetic_merge_tree,
            caller,
            snapshot,
        )
        try:
            with self._target_lock(self._target):
                if caller.repository_id != self._repository_id or caller.owner_id != self._owner_id:
                    raise MergeControlError("caller_denied")
                try:
                    caller_verified = self._verify_caller(caller)
                except Exception:
                    raise MergeControlError("caller_denied") from None
                if caller_verified is not True:
                    raise MergeControlError("caller_denied")
                initial_pull = self._read_pull(snapshot.pr_number)
                if initial_pull.get("state") != "open":
                    if initial_pull.get("merged") is True:
                        return self._reconcile_closed(
                            initial_pull,
                            snapshot,
                            bundle,
                            response_sha=None,
                            force_blocked=not prior_admission,
                        )
                    raise MergeControlError("pull_request_closed")
                try:
                    deployed = self._current_deployed_commit(self._target)
                except Exception:
                    raise MergeControlError("deployment_moved") from None
                if deployed != bundle.deployed_commit:
                    raise MergeControlError("deployment_moved")
                try:
                    policy_digest = self._current_policy_digest()
                except Exception:
                    raise MergeControlError("policy_moved") from None
                if policy_digest != bundle.policy_digest:
                    raise MergeControlError("policy_moved")
                try:
                    admitted = self._admit_merge(intent)
                except Exception:
                    admitted = False
                if admitted is not True:
                    try:
                        denied_pull = self._read_pull(snapshot.pr_number)
                    except MergeControlError:
                        raise MergeControlError("admission_denied") from None
                    if denied_pull.get("state") != "open" and denied_pull.get("merged") is True:
                        return self._reconcile_closed(
                            denied_pull,
                            snapshot,
                            bundle,
                            response_sha=None,
                            force_blocked=not prior_admission,
                        )
                    raise MergeControlError("admission_denied")
                pull = self._read_pull(snapshot.pr_number)
                if pull.get("state") != "open":
                    if pull.get("merged") is True:
                        return self._reconcile_closed(
                            pull,
                            snapshot,
                            bundle,
                            response_sha=None,
                            force_blocked=not prior_admission,
                        )
                    raise MergeControlError("pull_request_closed")
                self._verify_open_pull(pull, snapshot, bundle)
                try:
                    admission_live = self._admission_live()
                except Exception:
                    admission_live = False
                if admission_live is not True:
                    raise MergeControlError("admission_denied")
                response_sha: str | None = None
                merge_response_rejected = False
                merge_response_uncertain = False
                try:
                    response = self._merge_transport.merge_pull_request(
                        snapshot.pr_number,
                        snapshot.head_commit,
                    )
                    if response.get("merged") is True:
                        candidate_sha = response.get("sha")
                        if isinstance(candidate_sha, str) and _SHA.fullmatch(candidate_sha):
                            response_sha = candidate_sha
                    else:
                        merge_response_rejected = True
                except (GitHubEvidenceError, MergeControlError):
                    merge_response_uncertain = True
                try:
                    after = self._read_pull(snapshot.pr_number)
                except MergeControlError:
                    raise MergeControlError("merge_uncertain") from None
                if after.get("state") == "open" and after.get("merged") is False:
                    raise MergeControlError(
                        "merge_uncertain" if merge_response_uncertain else "merge_rejected"
                    )
                if after.get("merged") is not True:
                    raise MergeControlError("merge_uncertain")
                return self._reconcile_closed(
                    after,
                    snapshot,
                    bundle,
                    response_sha=response_sha,
                    force_blocked=merge_response_rejected,
                )
        except MergeControlError:
            raise
        except Exception:
            raise MergeControlError("merge_uncertain") from None

    def _read_pull(self, pr_number: int) -> dict[str, Any]:
        try:
            pull = self._client.get_json(f"pulls/{pr_number}")
        except GitHubEvidenceError:
            raise MergeControlError("merge_uncertain") from None
        if type(pull.get("number")) is not int or pull.get("number") != pr_number:
            raise MergeControlError("stale_pull_request")
        return pull

    def _verify_open_pull(
        self, pull: Mapping[str, Any], snapshot: GitHubEvidenceSnapshot, bundle: ReleaseBundle
    ) -> None:
        base = pull.get("base")
        head = pull.get("head")
        if (
            pull.get("state") != "open"
            or pull.get("merged") is not False
            or not isinstance(base, dict)
            or not isinstance(head, dict)
            or base.get("ref") != self._base_branch
            or base.get("sha") != bundle.base_commit
            or head.get("sha") != bundle.head_commit
            or _nested_int(pull, "base", "repo", "id") != self._repository_id
            or _nested_int(pull, "head", "repo", "id") != self._repository_id
            or _nested_int(pull, "base", "repo", "owner", "id") != self._owner_id
            or _nested_int(pull, "head", "repo", "owner", "id") != self._owner_id
            or pull.get("merge_commit_sha") != snapshot.synthetic_merge_commit
        ):
            raise MergeControlError("stale_pull_request")

    def _reconcile_closed(
        self,
        pull: Mapping[str, Any],
        snapshot: GitHubEvidenceSnapshot,
        bundle: ReleaseBundle,
        *,
        response_sha: str | None,
        force_blocked: bool = False,
    ) -> MergeOutcome:
        merge_sha = pull.get("merge_commit_sha")
        if (
            pull.get("state") != "closed"
            or pull.get("merged") is not True
            or not isinstance(merge_sha, str)
            or not _SHA.fullmatch(merge_sha)
        ):
            raise MergeControlError("pull_request_closed")
        try:
            commit = self._client.get_json(f"git/commits/{merge_sha}")
        except GitHubEvidenceError:
            raise MergeControlError("merge_uncertain") from None
        tree_obj = commit.get("tree")
        tree = tree_obj.get("sha") if isinstance(tree_obj, dict) else None
        raw_parents = commit.get("parents")
        if (
            commit.get("sha") != merge_sha
            or not isinstance(tree, str)
            or not _SHA.fullmatch(tree)
            or not isinstance(raw_parents, list)
            or len(raw_parents) > 20
            or any(not isinstance(item, dict) for item in raw_parents)
        ):
            raise MergeControlError("invalid_merge_object")
        parents = tuple(item.get("sha") for item in raw_parents)
        if any(not isinstance(parent, str) or not _SHA.fullmatch(parent) for parent in parents):
            raise MergeControlError("invalid_merge_object")
        evidence: MergeEvidence | None = None
        if len(parents) == 2:
            try:
                evidence = MergeEvidence(merge_sha, parents, tree)
            except Exception:
                evidence = None
        decision = (
            reconcile_merge(bundle, evidence, expected_merge_tree=snapshot.synthetic_merge_tree)
            if evidence is not None
            else None
        )
        response_matches = response_sha is None or response_sha == merge_sha
        outcome = (
            "merged_verified"
            if (
                decision is not None
                and decision.deployable
                and response_matches
                and not force_blocked
            )
            else "merged_blocked"
        )
        result = MergeOutcome(
            outcome,
            merge_sha,
            parents,
            tree,
            snapshot.synthetic_merge_tree,
            evidence,
            response_matches,
        )
        try:
            self._persist_merge(result)
        except Exception:
            raise MergeControlError("persistence_failed") from None
        return result
