"""Read-only, bounded GitHub evidence bound to immutable PR and workflow objects."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

import httpx

from .policy import (
    CheckEvidence,
    PolicyManifest,
    ReleaseBundle,
    validate_check_evidence,
)

_API_ROOT = "https://api.github.com"
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,4096}\Z")
_OWNER = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_SECONDS = 60.0
MAX_ITEMS = 1000
MAX_PAGES = 10
MAX_REQUESTS = 256


class _ApiBudget:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + MAX_TOTAL_SECONDS
        self.request_count = 0
        self.response_bytes = 0

    def begin_request(self) -> None:
        self.request_count += 1
        if self.request_count > MAX_REQUESTS or time.monotonic() >= self.deadline:
            raise GitHubEvidenceError("api_unavailable")

    def add_response_bytes(self, size: int) -> None:
        self.response_bytes += size
        if self.response_bytes > MAX_TOTAL_RESPONSE_BYTES:
            raise GitHubEvidenceError("invalid_response")


class GitHubEvidenceError(ValueError):
    """Fixed safe failure codes; upstream responses and credentials are excluded."""

    _CODES = frozenset(
        {
            "invalid_input",
            "api_unavailable",
            "invalid_response",
            "pagination_incomplete",
            "stale_pull_request",
            "synthetic_merge_unverified",
            "workflow_untrusted",
            "checks_failed",
            "attempt_stale",
        }
    )

    def __init__(self, code: str = "api_unavailable") -> None:
        self.code = code if code in self._CODES else "api_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True)
class VerifiedWorkflowRun:
    check_suite_id: int
    run_id: int
    attempt: int
    workflow_path: str
    workflow_sha: str
    check_run_ids: tuple[int, ...]
    job_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not int or value <= 0
                for value in (self.check_suite_id, self.run_id, self.attempt)
            )
            or not isinstance(self.workflow_path, str)
            or _workflow_path(self.workflow_path) != self.workflow_path
            or not isinstance(self.workflow_sha, str)
            or not _SHA.fullmatch(self.workflow_sha)
            or not isinstance(self.check_run_ids, tuple)
            or not self.check_run_ids
            or any(type(value) is not int or value <= 0 for value in self.check_run_ids)
            or len(set(self.check_run_ids)) != len(self.check_run_ids)
            or not isinstance(self.job_ids, tuple)
            or not self.job_ids
            or any(type(value) is not int or value <= 0 for value in self.job_ids)
            or len(set(self.job_ids)) != len(self.job_ids)
        ):
            raise GitHubEvidenceError("invalid_response")


@dataclass(frozen=True)
class GitHubEvidenceSnapshot:
    repository_id: int
    pr_number: int
    target_branch: str
    event_name: str
    base_commit: str
    head_commit: str
    head_tree: str
    synthetic_merge_commit: str
    synthetic_merge_tree: str
    synthetic_merge_parents: tuple[str, str]
    checks: tuple[CheckEvidence, ...]
    check_run_ids: tuple[tuple[str, int], ...]
    workflow_runs: tuple[VerifiedWorkflowRun, ...]

    def __post_init__(self) -> None:
        if (
            type(self.repository_id) is not int
            or self.repository_id <= 0
            or type(self.pr_number) is not int
            or self.pr_number <= 0
            or not isinstance(self.target_branch, str)
            or not re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", self.target_branch)
            or self.event_name != "pull_request"
            or not isinstance(self.synthetic_merge_parents, tuple)
            or len(self.synthetic_merge_parents) != 2
            or any(
                not isinstance(value, str) or not _SHA.fullmatch(value)
                for value in (
                    self.base_commit,
                    self.head_commit,
                    self.head_tree,
                    self.synthetic_merge_commit,
                    self.synthetic_merge_tree,
                    *self.synthetic_merge_parents,
                )
            )
            or self.synthetic_merge_parents != (self.base_commit, self.head_commit)
            or not isinstance(self.checks, tuple)
            or any(not isinstance(item, CheckEvidence) for item in self.checks)
            or not isinstance(self.check_run_ids, tuple)
            or not isinstance(self.workflow_runs, tuple)
            or any(not isinstance(item, VerifiedWorkflowRun) for item in self.workflow_runs)
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or type(item[1]) is not int
                or item[1] <= 0
                for item in self.check_run_ids
            )
            or len({name for name, _ in self.check_run_ids}) != len(self.check_run_ids)
            or len({run_id for _, run_id in self.check_run_ids}) != len(self.check_run_ids)
            or len(self.checks) != len(self.check_run_ids)
            or len(self.workflow_runs) != len(self.check_run_ids)
            or {item.name for item in self.checks} != {name for name, _ in self.check_run_ids}
        ):
            raise GitHubEvidenceError("invalid_response")
        run_ids_by_name = dict(self.check_run_ids)
        workflows_by_check = {
            check_id: workflow
            for workflow in self.workflow_runs
            for check_id in workflow.check_run_ids
        }
        if len(workflows_by_check) != len(self.check_run_ids):
            raise GitHubEvidenceError("invalid_response")
        if any(
            check.name not in run_ids_by_name
            or (workflow := workflows_by_check.get(run_ids_by_name.get(check.name, -1))) is None
            or workflow.run_id != check.run_id
            or workflow.attempt != check.attempt
            or workflow.workflow_path != check.workflow_path
            or workflow.workflow_sha != check.workflow_sha
            for check in self.checks
        ):
            raise GitHubEvidenceError("invalid_response")


def evidence_digest(snapshot: GitHubEvidenceSnapshot) -> str:
    """Hash the complete canonical GitHub evidence, including job/run correlation."""
    if not isinstance(snapshot, GitHubEvidenceSnapshot):
        raise GitHubEvidenceError("invalid_input")
    value = {
        "version": 1,
        "repository_id": snapshot.repository_id,
        "pr_number": snapshot.pr_number,
        "target_branch": snapshot.target_branch,
        "event_name": snapshot.event_name,
        "base_commit": snapshot.base_commit,
        "head_commit": snapshot.head_commit,
        "head_tree": snapshot.head_tree,
        "synthetic_merge_commit": snapshot.synthetic_merge_commit,
        "synthetic_merge_tree": snapshot.synthetic_merge_tree,
        "synthetic_merge_parents": list(snapshot.synthetic_merge_parents),
        "checks": [
            {
                "name": item.name,
                "repository_id": item.repository_id,
                "event": item.event,
                "workflow_path": item.workflow_path,
                "workflow_sha": item.workflow_sha,
                "workflow_digest": item.workflow_digest,
                "run_id": item.run_id,
                "attempt": item.attempt,
                "creator_app_id": item.creator_app_id,
                "head_commit": item.head_commit,
                "head_tree": item.head_tree,
                "conclusion": item.conclusion,
            }
            for item in sorted(snapshot.checks, key=lambda check: check.name)
        ],
        "check_run_ids": [list(item) for item in sorted(snapshot.check_run_ids)],
        "workflow_runs": [
            {
                "check_suite_id": run.check_suite_id,
                "run_id": run.run_id,
                "attempt": run.attempt,
                "workflow_path": run.workflow_path,
                "workflow_sha": run.workflow_sha,
                "check_run_ids": sorted(run.check_run_ids),
                "job_ids": sorted(run.job_ids),
            }
            for run in sorted(
                snapshot.workflow_runs,
                key=lambda item: (item.run_id, item.attempt, item.check_suite_id),
            )
        ],
    }
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class GitHubEvidenceClient:
    """Fixed-origin GitHub REST client with no caller-controlled URL or redirects."""

    def __init__(
        self,
        *,
        owner: str,
        repository: str,
        repository_id: int,
        token_provider: Callable[[], str],
        transport: httpx.BaseTransport | None = None,
        page_size: int = 100,
    ) -> None:
        if (
            not isinstance(owner, str)
            or not _OWNER.fullmatch(owner)
            or owner in {".", ".."}
            or not isinstance(repository, str)
            or not _REPOSITORY.fullmatch(repository)
            or repository in {".", ".."}
            or type(repository_id) is not int
            or repository_id <= 0
            or not callable(token_provider)
            or type(page_size) is not int
            or not 1 <= page_size <= 100
        ):
            raise GitHubEvidenceError("invalid_input")
        self.owner = owner
        self.repository = repository
        self.repository_id = repository_id
        self._token_provider = token_provider
        self._page_size = page_size
        try:
            self._http = httpx.Client(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(8.0, connect=4.0),
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except Exception:
            raise GitHubEvidenceError("api_unavailable") from None

    def __repr__(self) -> str:
        return (
            f"GitHubEvidenceClient(owner={self.owner!r}, repository={self.repository!r}, "
            f"repository_id={self.repository_id})"
        )

    def close(self) -> None:
        self._http.close()

    def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        budget: _ApiBudget | None = None,
    ) -> dict[str, Any]:
        return self._request_json("GET", path, params=params, budget=budget)

    def list_paginated(
        self,
        path: str,
        collection: str,
        *,
        params: Mapping[str, str | int] | None = None,
        budget: _ApiBudget | None = None,
    ) -> tuple[dict[str, Any], ...]:
        if not isinstance(collection, str) or not collection:
            raise GitHubEvidenceError("invalid_input")
        items: list[dict[str, Any]] = []
        total: int | None = None
        for page in range(1, MAX_PAGES + 1):
            query = {**(params or {}), "per_page": self._page_size, "page": page}
            response = self.get_json(path, params=query, budget=budget)
            page_items = response.get(collection)
            page_total = response.get("total_count")
            if (
                not isinstance(page_items, list)
                or any(not isinstance(item, dict) for item in page_items)
                or type(page_total) is not int
                or page_total < 0
                or page_total > MAX_ITEMS
                or (total is not None and page_total != total)
                or len(page_items) > self._page_size
            ):
                raise GitHubEvidenceError("invalid_response")
            total = page_total
            items.extend(page_items)
            if len(items) > total:
                raise GitHubEvidenceError("pagination_incomplete")
            if len(items) == total:
                return tuple(items)
            if not page_items:
                raise GitHubEvidenceError("pagination_incomplete")
        raise GitHubEvidenceError("pagination_incomplete")

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, object] | None = None,
        budget: _ApiBudget | None = None,
    ) -> dict[str, Any]:
        if (
            method not in {"GET", "PUT"}
            or not isinstance(path, str)
            or path.startswith("/")
            or "?" in path
            or "#" in path
            or ".." in path.split("/")
            or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", segment) for segment in path.split("/"))
        ):
            raise GitHubEvidenceError("invalid_input")
        request_budget = budget or _ApiBudget()
        request_budget.begin_request()
        try:
            token = self._token_provider()
        except Exception:
            raise GitHubEvidenceError("api_unavailable") from None
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise GitHubEvidenceError("api_unavailable")
        effective_deadline = request_budget.deadline
        now = time.monotonic()
        remaining = effective_deadline - now
        if remaining <= 0:
            raise GitHubEvidenceError("api_unavailable")
        url = f"{_API_ROOT}/repos/{self.owner}/{self.repository}/{path}"
        try:
            with self._http.stream(
                method,
                url,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(min(8.0, remaining), connect=min(4.0, remaining)),
            ) as response:
                if response.status_code != 200:
                    raise GitHubEvidenceError("api_unavailable")
                raw = bytearray()
                for chunk in response.iter_bytes(8192):
                    if time.monotonic() >= effective_deadline:
                        raise GitHubEvidenceError("api_unavailable")
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise GitHubEvidenceError("invalid_response")
                    raw.extend(chunk)
                    request_budget.add_response_bytes(len(chunk))
        except GitHubEvidenceError:
            raise
        except Exception:
            raise GitHubEvidenceError("api_unavailable") from None
        try:
            value = json.loads(bytes(raw), object_pairs_hook=_unique_keys)
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise GitHubEvidenceError("invalid_response") from None
        if not isinstance(value, dict):
            raise GitHubEvidenceError("invalid_response")
        return value


class GitHubEvidenceResolver:
    """Correlate required PR checks to exact run attempts and workflow closure."""

    def __init__(
        self,
        client: GitHubEvidenceClient,
        policy: PolicyManifest,
        *,
        verify_workflow_closure: Callable[[str, Mapping[str, str]], bool],
        expected_base_branch: str | None = None,
    ) -> None:
        if (
            not isinstance(client, GitHubEvidenceClient)
            or not isinstance(policy, PolicyManifest)
            or client.repository_id != policy.repository_id
            or not callable(verify_workflow_closure)
            or (
                expected_base_branch is not None
                and (
                    not isinstance(expected_base_branch, str)
                    or not re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", expected_base_branch)
                )
            )
        ):
            raise GitHubEvidenceError("invalid_input")
        closure_paths = set(dict(policy.workflow_digests))
        protected_paths = set(policy.protected_paths)
        if not closure_paths or not closure_paths <= protected_paths:
            raise GitHubEvidenceError("workflow_untrusted")
        self.client = client
        self.policy = policy
        self._verify_workflow_closure = verify_workflow_closure
        # GitHub PR base branches and deployment targets are independent names.
        # The production controller's PR branch defaults to main; deployments may
        # target a different logical lane such as production.
        self._base_branch = "main" if expected_base_branch is None else expected_base_branch

    def resolve(self, pr_number: int, bundle: ReleaseBundle) -> GitHubEvidenceSnapshot:
        if (
            type(pr_number) is not int
            or pr_number <= 0
            or not isinstance(bundle, ReleaseBundle)
            or bundle.policy_digest != self.policy.digest
            or not bundle.base_is_ancestor_of_head
        ):
            raise GitHubEvidenceError("invalid_input")
        budget = _ApiBudget()
        pull = self.client.get_json(f"pulls/{pr_number}", budget=budget)
        _verify_pull_binding(
            pull,
            pr_number,
            self.client.repository_id,
            self.policy.owner_id,
            self._base_branch,
            bundle,
        )
        synthetic = pull.get("merge_commit_sha")
        if not isinstance(synthetic, str) or not _SHA.fullmatch(synthetic):
            raise GitHubEvidenceError("synthetic_merge_unverified")
        synthetic_commit = self.client.get_json(f"git/commits/{synthetic}", budget=budget)
        merge_tree, merge_parents = _commit_parts(synthetic_commit, synthetic)
        if merge_parents != (bundle.base_commit, bundle.head_commit):
            raise GitHubEvidenceError("synthetic_merge_unverified")
        try:
            trusted_closure = self._verify_workflow_closure(
                bundle.base_commit, dict(self.policy.workflow_digests)
            )
        except Exception:
            raise GitHubEvidenceError("workflow_untrusted") from None
        if trusted_closure is not True:
            raise GitHubEvidenceError("workflow_untrusted")

        check_runs = self.client.list_paginated(
            f"commits/{synthetic}/check-runs",
            "check_runs",
            params={"filter": "all"},
            budget=budget,
        )
        workflow_runs = self.client.list_paginated(
            "actions/runs",
            "workflow_runs",
            params={"head_sha": synthetic, "event": "pull_request"},
            budget=budget,
        )
        verified_checks: list[CheckEvidence] = []
        check_run_ids: list[tuple[str, int]] = []
        workflow_evidence: list[VerifiedWorkflowRun] = []
        for trusted in self.policy.checks:
            matches = [
                item
                for item in check_runs
                if item.get("name") == trusted.name
                and _nested_int(item, "app", "id") == trusted.creator_app_id
            ]
            if len(matches) != 1:
                raise GitHubEvidenceError("checks_failed")
            check_run = matches[0]
            check_run_id = _positive_int(check_run.get("id"))
            suite_id = _positive_int(_nested_int(check_run, "check_suite", "id"))
            if (
                check_run_id is None
                or suite_id is None
                or check_run.get("status") != "completed"
                or check_run.get("conclusion") != "success"
                or check_run.get("head_sha") != synthetic
                or not _contains_pull_request(check_run.get("pull_requests"), pr_number, bundle)
            ):
                raise GitHubEvidenceError("checks_failed")
            run_candidates = [
                item
                for item in workflow_runs
                if _positive_int(item.get("check_suite_id")) == suite_id
                and _nested_int(item, "repository", "id") == self.client.repository_id
                and item.get("event") == "pull_request"
                and item.get("head_sha") == synthetic
                and _contains_pull_request(item.get("pull_requests"), pr_number, bundle)
            ]
            if len(run_candidates) != 1:
                raise GitHubEvidenceError("checks_failed")
            run = run_candidates[0]
            run_id = _positive_int(run.get("id"))
            attempt = _positive_int(run.get("run_attempt"))
            workflow_path = _workflow_path(run.get("path"))
            if (
                run_id is None
                or attempt is None
                or workflow_path != trusted.workflow_path
                or run.get("status") != "completed"
                or run.get("conclusion") != "success"
            ):
                raise GitHubEvidenceError("checks_failed")
            latest = self.client.get_json(f"actions/runs/{run_id}", budget=budget)
            if (
                _positive_int(latest.get("run_attempt")) != attempt
                or type(latest.get("id")) is not int
                or latest.get("id") != run_id
                or _positive_int(latest.get("check_suite_id")) != suite_id
            ):
                raise GitHubEvidenceError("attempt_stale")
            _verify_attempt(latest, run, self.client.repository_id, pr_number, bundle)
            exact_attempt = self.client.get_json(
                f"actions/runs/{run_id}/attempts/{attempt}", budget=budget
            )
            _verify_attempt(exact_attempt, run, self.client.repository_id, pr_number, bundle)
            jobs = self.client.list_paginated(
                f"actions/runs/{run_id}/attempts/{attempt}/jobs",
                "jobs",
                budget=budget,
            )
            job_ids: list[int] = []
            job_check_run_ids: list[int] = []
            for job in jobs:
                job_id = _positive_int(job.get("id"))
                linked_check_run_id = _check_run_id_from_job_url(
                    job.get("check_run_url"), self.client.owner, self.client.repository
                )
                if (
                    job_id is None
                    or linked_check_run_id is None
                    or _positive_int(job.get("run_id")) != run_id
                    or _positive_int(job.get("run_attempt")) != attempt
                    or job.get("status") != "completed"
                    or job.get("conclusion") != "success"
                ):
                    raise GitHubEvidenceError("checks_failed")
                job_ids.append(job_id)
                job_check_run_ids.append(linked_check_run_id)
            if not job_ids or len(set(job_ids)) != len(job_ids):
                raise GitHubEvidenceError("checks_failed")
            if job_check_run_ids.count(check_run_id) != 1:
                raise GitHubEvidenceError("checks_failed")
            evidence = CheckEvidence(
                name=trusted.name,
                repository_id=self.client.repository_id,
                event="pull_request",
                workflow_path=workflow_path,
                workflow_sha=bundle.base_commit,
                workflow_digest=trusted.workflow_digest,
                run_id=run_id,
                attempt=attempt,
                creator_app_id=trusted.creator_app_id,
                head_commit=synthetic,
                head_tree=merge_tree,
                conclusion="success",
            )
            tested_bundle = replace(bundle, head_commit=synthetic, head_tree=merge_tree)
            if not validate_check_evidence(
                self.policy,
                evidence,
                tested_bundle,
                expected_run_id=run_id,
                expected_attempt=attempt,
                expected_workflow_sha=bundle.base_commit,
            ):
                raise GitHubEvidenceError("checks_failed")
            verified_checks.append(evidence)
            check_run_ids.append((trusted.name, check_run_id))
            workflow_evidence.append(
                VerifiedWorkflowRun(
                    suite_id,
                    run_id,
                    attempt,
                    workflow_path,
                    bundle.base_commit,
                    (check_run_id,),
                    tuple(job_ids),
                )
            )
        return GitHubEvidenceSnapshot(
            self.client.repository_id,
            pr_number,
            self._base_branch,
            "pull_request",
            bundle.base_commit,
            bundle.head_commit,
            bundle.head_tree,
            synthetic,
            merge_tree,
            merge_parents,
            tuple(verified_checks),
            tuple(check_run_ids),
            tuple(workflow_evidence),
        )


def _verify_pull_binding(
    pull: Mapping[str, Any],
    pr_number: int,
    repository_id: int,
    owner_id: int,
    base_branch: str,
    bundle: ReleaseBundle,
) -> None:
    base = pull.get("base")
    head = pull.get("head")
    if (
        pull.get("number") != pr_number
        or type(pull.get("number")) is not int
        or pull.get("state") != "open"
        or pull.get("merged") is not False
        or not isinstance(base, dict)
        or not isinstance(head, dict)
        or base.get("ref") != base_branch
        or base.get("sha") != bundle.base_commit
        or head.get("sha") != bundle.head_commit
        or _nested_int(pull, "base", "repo", "id") != repository_id
        or _nested_int(pull, "head", "repo", "id") != repository_id
        or _nested_int(pull, "base", "repo", "owner", "id") != owner_id
        or _nested_int(pull, "head", "repo", "owner", "id") != owner_id
    ):
        raise GitHubEvidenceError("stale_pull_request")


def _commit_parts(commit: Mapping[str, Any], expected_sha: str) -> tuple[str, tuple[str, str]]:
    tree = _nested_str(commit, "tree", "sha")
    parents = commit.get("parents")
    if (
        commit.get("sha") != expected_sha
        or not isinstance(tree, str)
        or not _SHA.fullmatch(tree)
        or not isinstance(parents, list)
        or len(parents) != 2
    ):
        raise GitHubEvidenceError("synthetic_merge_unverified")
    parent_ids = tuple(_nested_str(parent, "sha") for parent in parents)
    if any(not isinstance(parent, str) or not _SHA.fullmatch(parent) for parent in parent_ids):
        raise GitHubEvidenceError("synthetic_merge_unverified")
    return tree, parent_ids  # type: ignore[return-value]


def _contains_pull_request(items: object, pr_number: int, bundle: ReleaseBundle) -> bool:
    if not isinstance(items, list):
        return False
    matches = [
        item
        for item in items
        if isinstance(item, dict)
        and type(item.get("number")) is int
        and item.get("number") == pr_number
    ]
    return len(matches) == 1 and (
        _nested_str(matches[0], "base", "sha") == bundle.base_commit
        and _nested_str(matches[0], "head", "sha") == bundle.head_commit
    )


def _verify_attempt(
    attempt: Mapping[str, Any],
    run: Mapping[str, Any],
    repository_id: int,
    pr_number: int,
    bundle: ReleaseBundle,
) -> None:
    if (
        _positive_int(attempt.get("id")) != _positive_int(run.get("id"))
        or _positive_int(attempt.get("run_attempt")) != _positive_int(run.get("run_attempt"))
        or _positive_int(attempt.get("check_suite_id")) != _positive_int(run.get("check_suite_id"))
        or attempt.get("event") != "pull_request"
        or attempt.get("head_sha") != run.get("head_sha")
        or attempt.get("path") != run.get("path")
        or attempt.get("status") != "completed"
        or attempt.get("conclusion") != "success"
        or _nested_int(attempt, "repository", "id") != repository_id
        or not _contains_pull_request(attempt.get("pull_requests"), pr_number, bundle)
    ):
        raise GitHubEvidenceError("attempt_stale")


def _workflow_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.split("@", 1)[0]
    return path if path.startswith(".github/workflows/") else None


def _check_run_id_from_job_url(value: object, owner: str, repository: str) -> int | None:
    if not isinstance(value, str):
        return None
    prefix = f"https://api.github.com/repos/{owner}/{repository}/check-runs/"
    if not value.startswith(prefix):
        return None
    candidate = value[len(prefix) :]
    if not re.fullmatch(r"[1-9][0-9]{0,19}", candidate):
        return None
    parsed = int(candidate)
    return parsed if parsed <= 2**63 - 1 else None


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _nested_int(value: object, *path: str) -> int | None:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _positive_int(current)


def _nested_str(value: object, *path: str) -> str | None:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, str) else None


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result
