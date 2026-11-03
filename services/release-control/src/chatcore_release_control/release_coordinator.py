"""Bounded broker and GitHub adapters for the inactive release workflows.

The command line accepts an approval UUID only. Deployment configuration and token
providers are trusted process inputs; event fields never choose hosts, URLs, commands
or credentials. This module does not execute candidate code.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx

from .development import (
    MAX_CANDIDATE_FILES,
    MAX_FILE_BYTES,
    MAX_PATCH_BYTES,
    DevelopmentError,
    DockerSandboxRunner,
    ProcessExecutor,
    ResourcePolicy,
    SandboxConfig,
)
from .development_worker import WorkerManifest
from .git_evidence import GitEvidenceError, _GitRunner, collect_release_bundle
from .git_fetch import (
    GitFetchError,
    GitObjectFetcher,
    GitProcessExecutor,
    SubprocessGitExecutor,
)
from .github_evidence import (
    GitHubEvidenceClient,
    GitHubEvidenceError,
    GitHubEvidenceResolver,
    _check_run_id_from_job_url,
    evidence_digest,
)
from .merge_control import (
    GitHubMergeTransport,
    MergeControlError,
    MergeController,
    MergeIntent,
    MergeOutcome,
)
from .monorepo_harness import SnapshotManifest
from .oidc import (
    CallerIdentity,
    OIDCError,
    OIDCPolicy,
    fetch_github_jwks,
    github_main_subject,
    installed_workflow_verifier,
    verify_oidc,
)
from .policy import (
    Change,
    DeploymentIdentity,
    PolicyError,
    PolicyManifest,
    _is_private_path,
    evaluate_reviewed_release,
)
from .publication import ScanInput, ScannerBinaryAdapter, ScanVerdict

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_TARGET = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,16384}\Z")
_MAX_RESPONSE = 1024 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024
_MAX_SECONDS = 20.0
_MAX_REQUESTS = 32
_ACTIONS_OIDC_ORIGIN = "https://pipelines.actions.githubusercontent.com"
_MAX_OIDC_RESPONSE = 16 * 1024
_MAX_OBSERVED_PARENTS = 20


class CoordinatorError(RuntimeError):
    """Fixed error codes that never contain upstream bodies or credentials."""

    _CODES = frozenset(
        {
            "invalid_input",
            "broker_unavailable",
            "broker_invalid_response",
            "caller_denied",
            "job_unverified",
            "merge_persistence_failed",
            "runtime_not_configured",
            "invalid_runtime_configuration",
            "reconciliation_required",
            "ci_context_mismatch",
            "ci_patch_unrepresentable",
            "ci_state_invalid",
            "ci_runner_failed",
            "ci_report_rejected",
            "ci_report_invalid",
            "ci_upload_failed",
        }
    )

    def __init__(self, code: str = "broker_unavailable") -> None:
        self.code = code if code in self._CODES else "broker_unavailable"
        super().__init__(self.code)


class ActionsOIDCTokenProvider:
    """Exchange only the protected Actions OIDC request capability for a token."""

    def __init__(
        self,
        request_url: str,
        request_token: str,
        *,
        audience: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        try:
            parts = urlsplit(request_url)
            if (
                parts.scheme != "https"
                or f"{parts.scheme}://{parts.hostname}" != _ACTIONS_OIDC_ORIGIN
                or parts.port is not None
                or parts.username is not None
                or parts.password is not None
                or parts.fragment
                or re.fullmatch(
                    r"/[0-9a-fA-F-]{36}/_apis/pipelines/[A-Za-z0-9/_-]{1,512}/idtoken",
                    parts.path,
                )
                is None
                or (values := parse_qs(parts.query).get("api-version")) is None
                or len(values) != 1
                or re.fullmatch(r"[0-9.]{1,16}", values[0]) is None
                or not isinstance(request_token, str)
                or not _TOKEN.fullmatch(request_token)
                or not isinstance(audience, str)
                or not 1 <= len(audience) <= 512
            ):
                raise CoordinatorError("invalid_runtime_configuration")
            self._request_url = request_url
            self._request_token = request_token
            self._audience = audience
            self._http = httpx.Client(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(5.0, connect=2.0),
            )
        except CoordinatorError:
            raise
        except Exception:
            raise CoordinatorError("invalid_runtime_configuration") from None

    def __repr__(self) -> str:
        return "ActionsOIDCTokenProvider([protected])"

    def close(self) -> None:
        self._http.close()

    def __call__(self) -> str:
        started = time.monotonic()
        try:
            request = self._http.build_request(
                "GET",
                self._request_url,
                params={"audience": self._audience},
                headers={"Authorization": f"Bearer {self._request_token}"},
            )
            response = self._http.send(request, stream=True)
            if response.status_code != 200:
                response.close()
                raise CoordinatorError("caller_denied")
            content = bytearray()
            try:
                for chunk in response.iter_bytes(4096):
                    if (
                        len(content) + len(chunk) > _MAX_OIDC_RESPONSE
                        or time.monotonic() - started > 5.0
                    ):
                        raise CoordinatorError("caller_denied")
                    content.extend(chunk)
            finally:
                response.close()
            payload = json.loads(bytes(content), object_pairs_hook=_unique_pairs)
            token = payload.get("value") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                raise CoordinatorError("caller_denied")
            return token
        except CoordinatorError:
            raise
        except Exception:
            raise CoordinatorError("caller_denied") from None


@dataclass(frozen=True)
class ProtectedRuntimeConfig:
    """Deployment-only settings read from named protected workflow variables."""

    broker_origin: str
    audience: str
    owner: str
    repository: str
    repository_id: int
    owner_id: int
    workflow_path: str
    runner_environment: str
    object_repository: Path
    workflow_blobs: tuple[tuple[str, str], ...]
    run_id: int
    run_attempt: int
    workflow_sha: str
    job_name: str
    github_token: str = field(repr=False)
    oidc_request_url: str = field(repr=False)
    oidc_request_token: str = field(repr=False)
    subject_format: str = "legacy"
    dispatch_id: UUID | None = None
    expected_pr_number: int | None = None
    expected_head_sha: str | None = None

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> ProtectedRuntimeConfig:
        source = os.environ if environment is None else environment
        try:
            repository = source["GITHUB_REPOSITORY"]
            owner, repo_name = repository.split("/", 1)
            repository_id = _env_int(source, "GITHUB_REPOSITORY_ID")
            owner_id = _env_int(source, "GITHUB_OWNER_ID")
            run_id = _env_int(source, "GITHUB_RUN_ID")
            run_attempt = _env_int(source, "GITHUB_RUN_ATTEMPT")
            workflow_sha = source["GITHUB_WORKFLOW_SHA"]
            workflow_path = source["RELEASE_CONTROL_WORKFLOW_PATH"]
            runner_environment = source["RUNNER_ENVIRONMENT"]
            object_repository = Path(source["RELEASE_CONTROL_OBJECT_REPOSITORY"]).resolve(
                strict=True
            )
            workflow_blob_value = json.loads(
                source["RELEASE_CONTROL_WORKFLOW_BLOBS_JSON"], object_pairs_hook=_unique_pairs
            )
            broker_origin = source["RELEASE_CONTROL_BROKER_ORIGIN"]
            audience = source["RELEASE_CONTROL_OIDC_AUDIENCE"]
            github_token = source["RELEASE_CONTROL_GITHUB_API_TOKEN"]
            oidc_request_url = source["ACTIONS_ID_TOKEN_REQUEST_URL"]
            oidc_request_token = source["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
            job_name = source["GITHUB_JOB"]
            subject_format = source.get("RELEASE_CONTROL_OIDC_SUBJECT_FORMAT", "legacy")
            dispatch_id = expected_pr_number = expected_head_sha = None
            dispatch_keys = {
                "RELEASE_CONTROL_DISPATCH_ID",
                "RELEASE_CONTROL_PR_NUMBER",
                "RELEASE_CONTROL_HEAD_SHA",
            }
            if dispatch_keys & set(source):
                dispatch_id = UUID(source["RELEASE_CONTROL_DISPATCH_ID"])
                if str(dispatch_id) != source["RELEASE_CONTROL_DISPATCH_ID"]:
                    raise ValueError
                expected_pr_number = _env_int(source, "RELEASE_CONTROL_PR_NUMBER")
                expected_head_sha = source["RELEASE_CONTROL_HEAD_SHA"]
                if not _SHA.fullmatch(expected_head_sha):
                    raise ValueError
            if (
                source["GITHUB_EVENT_NAME"] != "workflow_dispatch"
                or source["GITHUB_REF"] != "refs/heads/main"
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo_name)
                or not _SHA.fullmatch(workflow_sha)
                or re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", workflow_path) is None
                or runner_environment not in {"github-hosted", "self-hosted"}
                or not object_repository.is_dir()
                or not isinstance(workflow_blob_value, dict)
                or not workflow_blob_value
                or len(workflow_blob_value) > 2048
                or len(source["RELEASE_CONTROL_WORKFLOW_BLOBS_JSON"]) > 1024 * 1024
                or any(
                    not isinstance(path, str)
                    or _invalid_closure_path(path)
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    for path, digest in workflow_blob_value.items()
                )
                or workflow_path not in workflow_blob_value
                or not _TOKEN.fullmatch(github_token)
                or not _TOKEN.fullmatch(oidc_request_token)
                or not re.fullmatch(r"[A-Za-z0-9_. -]{1,128}", job_name)
                or subject_format not in {"legacy", "immutable"}
            ):
                raise ValueError
            origin_parts = urlsplit(broker_origin)
            if (
                origin_parts.scheme != "https"
                or not origin_parts.hostname
                or origin_parts.username is not None
                or origin_parts.password is not None
                or origin_parts.path not in {"", "/"}
                or origin_parts.query
                or origin_parts.fragment
                or not 1 <= len(audience) <= 512
            ):
                raise ValueError
            return cls(
                broker_origin=broker_origin,
                audience=audience,
                owner=owner,
                repository=repo_name,
                repository_id=repository_id,
                owner_id=owner_id,
                workflow_path=workflow_path,
                runner_environment=runner_environment,
                object_repository=object_repository,
                workflow_blobs=tuple(sorted(workflow_blob_value.items())),
                run_id=run_id,
                run_attempt=run_attempt,
                workflow_sha=workflow_sha,
                job_name=job_name,
                github_token=github_token,
                oidc_request_url=oidc_request_url,
                oidc_request_token=oidc_request_token,
                subject_format=subject_format,
                dispatch_id=dispatch_id,
                expected_pr_number=expected_pr_number,
                expected_head_sha=expected_head_sha,
            )
        except CoordinatorError:
            raise
        except Exception:
            raise CoordinatorError("invalid_runtime_configuration") from None


@dataclass(frozen=True)
class RecordedReleaseOutcome:
    status: str
    approval_id: UUID
    merge_commit: str | None
    merge_tree: str | None
    merge_parents: tuple[str, ...] | None
    host_receipt: tuple[str, str, str, int] | None

    @property
    def outcome(self) -> str:
        return self.status


def execute_merge(
    approval_id: UUID,
    *,
    environment: Mapping[str, str] | None = None,
    broker_transport: httpx.BaseTransport | None = None,
    github_transport: httpx.BaseTransport | None = None,
    oidc_transport: httpx.BaseTransport | None = None,
    jwks_transport: httpx.AsyncBaseTransport | None = None,
    git_executor: Any = None,
) -> Any:
    """Run one configured merge using only fixed HTTP APIs and installed Git objects."""
    if not isinstance(approval_id, UUID):
        raise CoordinatorError("invalid_input")
    config = ProtectedRuntimeConfig.from_environment(environment)
    actions_token = ActionsOIDCTokenProvider(
        config.oidc_request_url,
        config.oidc_request_token,
        audience=config.audience,
        transport=oidc_transport,
    )
    github: GitHubEvidenceClient | None = None
    broker: BrokerHttpClient | None = None
    try:
        github = GitHubEvidenceClient(
            owner=config.owner,
            repository=config.repository,
            repository_id=config.repository_id,
            token_provider=lambda: config.github_token,
            transport=github_transport,
        )
        broker = BrokerHttpClient(
            config.broker_origin,
            token_provider=actions_token,
            transport=broker_transport,
        )
        broker_callbacks = BrokerMergeCallbacks(broker, approval_id=approval_id)
        context = broker_callbacks.get_context()
        if config.expected_pr_number is not None and (
            context["pr_number"] != config.expected_pr_number
            or context["head_sha"] != config.expected_head_sha
        ):
            raise CoordinatorError("caller_denied")
        if context["status"] not in {"queued", "merge_authorized"}:
            return recorded_release_outcome(approval_id, context)
        if context["status"] == "merge_authorized" and context["merge_lease"] is None:
            raise CoordinatorError("reconciliation_required")
        pinned_policy = PolicyManifest.from_json(_canonical(context["policy_manifest"]))
        if (
            pinned_policy.digest != context["policy_digest"]
            or pinned_policy.repository_id != config.repository_id
            or pinned_policy.owner_id != config.owner_id
        ):
            raise CoordinatorError("caller_denied")
        fetch_args = {"executor": git_executor} if git_executor is not None else {}
        GitObjectFetcher(
            repository=config.object_repository,
            owner=config.owner,
            name=config.repository,
            token=config.github_token,
            **fetch_args,
        ).ensure_history(
            pull_request_number=context["pr_number"],
            deployed_commit=context["deployed_identity"]["git_sha"],
            base_commit=context["base_sha"],
            head_commit=context["head_sha"],
            policy=pinned_policy,
        )
        job_verifier = GitHubJobCorrelationVerifier(
            github,
            workflow_path=config.workflow_path,
            runner_environment=config.runner_environment,
            expected_run_title=f"release:{config.dispatch_id}" if config.dispatch_id else None,
        )
        check_run_id = job_verifier.check_run_id_for_job(
            run_id=config.run_id,
            run_attempt=config.run_attempt,
            workflow_sha=config.workflow_sha,
            job_name=config.job_name,
        )
        caller = CallerIdentity(
            config.repository_id,
            config.owner_id,
            config.workflow_sha,
            config.run_id,
            config.run_attempt,
            check_run_id,
            config.runner_environment,
        )
        if context["status"] == "merge_authorized":
            prior_lease = context["merge_lease"]
            if any(
                prior_lease[name] != value
                for name, value in (
                    ("run_id", caller.run_id),
                    ("run_attempt", caller.run_attempt),
                    ("check_run_id", caller.check_run_id),
                )
            ):
                raise CoordinatorError("reconciliation_required")
        oidc_policy = OIDCPolicy(
            audience=config.audience,
            repository_id=config.repository_id,
            owner_id=config.owner_id,
            repository=f"{config.owner}/{config.repository}",
            workflow_path=config.workflow_path,
            workflow_sha=config.workflow_sha,
            run_id=config.run_id,
            run_attempt=config.run_attempt,
            check_run_id=check_run_id,
            runner_environment=config.runner_environment,
            subject=github_main_subject(
                f"{config.owner}/{config.repository}",
                config.owner_id,
                config.repository_id,
                config.subject_format,
            ),
        )
        closure = installed_workflow_verifier(config.object_repository, dict(config.workflow_blobs))
        caller_verifier = OIDCCallerVerifier(
            approval_id=approval_id,
            token_provider=actions_token,
            jwks_provider=lambda: asyncio.run(fetch_github_jwks(transport=jwks_transport)),
            policy_provider=lambda _: oidc_policy,
            workflow_verifier=closure,
            job_verifier=job_verifier,
        )
        coordinator = build_merge_coordinator(
            github,
            broker,
            approval_id=approval_id,
            repository_id=config.repository_id,
            owner_id=config.owner_id,
            caller_verifier=caller_verifier,
            broker_callbacks=broker_callbacks,
        )
        context = coordinator.context
        policy = PolicyManifest.from_json(_canonical(context["policy_manifest"]))
        deployment = DeploymentIdentity(**context["deployed_identity"])
        if (
            context["repository_id"] != config.repository_id
            or context["owner_id"] != config.owner_id
            or context["repository"] != f"{config.owner}/{config.repository}"
            or context["policy_digest"] != policy.digest
            or policy.repository_id != config.repository_id
            or policy.owner_id != config.owner_id
            or context["workflow_sha"] != config.workflow_sha
            or policy.target != context["target"]
            or any(
                dict(config.workflow_blobs).get(path) != digest
                for path, digest in policy.workflow_digests
            )
        ):
            raise CoordinatorError("caller_denied")
        bundle = collect_release_bundle(
            config.object_repository,
            deployment.git_sha,
            context["base_sha"],
            context["head_sha"],
            policy,
            same_tree_continuation_recorded=(
                context["review_bundle"].get("same_tree_continuation_recorded") is True
            ),
        )
        if bundle.head_tree != context["head_tree"]:
            raise CoordinatorError("caller_denied")
        # The immutable card review determines whether ordinary B/D continuity is
        # sufficient or an explicitly labeled full-delta reconciliation was approved.
        eligibility = evaluate_reviewed_release(policy, bundle, context["review_bundle"])
        if not eligibility.eligible:
            raise CoordinatorError("caller_denied")
        snapshot = GitHubEvidenceResolver(
            github,
            policy,
            verify_workflow_closure=closure,
            expected_base_branch="main",
        ).resolve(context["pr_number"], bundle)
        if context["review_bundle"].get("check_evidence_digest") != evidence_digest(snapshot):
            raise CoordinatorError("caller_denied")
        return coordinator.merge(snapshot, bundle, caller=caller)
    finally:
        if github is not None:
            github.close()
        if broker is not None:
            broker.close()
        actions_token.close()


class BrokerHttpClient:
    """Fixed configured HTTPS origin with bounded responses and protected auth."""

    def __init__(
        self,
        origin: str,
        *,
        token_provider: Callable[[], str],
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        try:
            parts = urlsplit(origin)
            if (
                not isinstance(origin, str)
                or parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.path not in {"", "/"}
                or parts.query
                or parts.fragment
                or not callable(token_provider)
            ):
                raise CoordinatorError("invalid_input")
            self._origin = f"https://{parts.hostname.lower()}"
            if parts.port is not None:
                self._origin += f":{parts.port}"
            self._token_provider = token_provider
            self._request_count = 0
            self._response_bytes = 0
            self._http = httpx.Client(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(5.0, connect=2.0),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        except CoordinatorError:
            raise
        except Exception:
            raise CoordinatorError("invalid_input") from None

    def __repr__(self) -> str:
        return "BrokerHttpClient(origin=[configured])"

    def close(self) -> None:
        self._http.close()

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def post(self, path: str, body: Mapping[str, object]) -> dict[str, Any]:
        return self._request("POST", path, body=body)

    def delete(self, path: str, body: Mapping[str, object]) -> dict[str, Any]:
        return self._request("DELETE", path, body=body)

    def _request(
        self, method: str, path: str, *, body: Mapping[str, object] | None = None
    ) -> dict[str, Any]:
        if (
            method not in {"GET", "POST", "DELETE"}
            or not isinstance(path, str)
            or re.fullmatch(r"/v1/[A-Za-z0-9._/-]{1,500}", path) is None
            or "?" in path
            or "#" in path
            or ".." in path
            or "//" in path
            or len(path) > 512
            or (body is not None and len(_canonical(body)) > _MAX_RESPONSE)
        ):
            raise CoordinatorError("invalid_input")
        started = time.monotonic()
        try:
            token = self._token_provider()
            if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                raise CoordinatorError("caller_denied")
            self._request_count += 1
            if self._request_count > _MAX_REQUESTS:
                raise CoordinatorError("broker_unavailable")
            request = self._http.build_request(
                method,
                self._origin + path,
                headers={"Authorization": f"Bearer {token}"},
                content=_canonical(body) if body is not None else None,
            )
            response = self._http.send(request, stream=True)
            self._http.cookies.clear()
            if response.status_code not in {200, 201, 204}:
                response.close()
                raise CoordinatorError("broker_unavailable")
            data = bytearray()
            try:
                for chunk in response.iter_bytes(8192):
                    self._response_bytes += len(chunk)
                    if (
                        len(data) + len(chunk) > _MAX_RESPONSE
                        or self._response_bytes > _MAX_TOTAL_BYTES
                    ):
                        raise CoordinatorError("broker_invalid_response")
                    if time.monotonic() - started > _MAX_SECONDS:
                        raise CoordinatorError("broker_unavailable")
                    data.extend(chunk)
            finally:
                response.close()
            if response.status_code == 204 and not data:
                return {}
            value = json.loads(bytes(data), object_pairs_hook=_unique_pairs)
            if not isinstance(value, dict):
                raise CoordinatorError("broker_invalid_response")
            return value
        except CoordinatorError:
            raise
        except Exception:
            raise CoordinatorError("broker_unavailable") from None


class BrokerMergeCallbacks:
    """HTTP callbacks for broker context, P05 admission and merge persistence.

    Endpoint request/response schemas are specified in the P04 plan and intentionally
    contain only signed caller run IDs plus immutable Git object observations.
    """

    def __init__(
        self,
        client: BrokerHttpClient,
        *,
        approval_id: UUID,
        dispatch_fence: int | None = None,
    ) -> None:
        if not isinstance(client, BrokerHttpClient) or not isinstance(approval_id, UUID):
            raise CoordinatorError("invalid_input")
        if dispatch_fence is not None and (type(dispatch_fence) is not int or dispatch_fence <= 0):
            raise CoordinatorError("invalid_input")
        self._client = client
        self.approval_id = approval_id
        self.dispatch_fence = dispatch_fence
        self.caller: CallerIdentity | None = None
        self._active_lease: dict[str, Any] | None = None
        self._admission_deadline: float | None = None
        self._lease_reused = False
        self._target: str | None = None
        self.context: dict[str, Any] | None = None
        self._deployed_identity: DeploymentIdentity | None = None

    def bind_caller(self, identity: CallerIdentity) -> None:
        if not isinstance(identity, CallerIdentity):
            raise CoordinatorError("invalid_input")
        self.caller = identity

    def get_context(self) -> dict[str, Any]:
        result = self._client.get(f"/v1/releases/{self.approval_id}/merge-context")
        required = {
            "approval_id",
            "repository",
            "repository_id",
            "owner_id",
            "pr_number",
            "target",
            "sequence",
            "dispatch_fence",
            "deployed_identity",
            "base_sha",
            "head_sha",
            "head_tree",
            "policy_digest",
            "policy_manifest",
            "review_bundle",
            "review_bundle_digest",
            "workflow_sha",
            "status",
            "merge_commit",
            "merge_tree",
            "merge_parents",
            "host_receipt",
            "merge_lease",
        }
        if (
            set(result) != required
            or result.get("approval_id") != str(self.approval_id)
            or not isinstance(result.get("repository"), str)
            or not re.fullmatch(
                r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", result["repository"]
            )
            or any(
                type(result.get(name)) is not int or result[name] <= 0
                for name in ("repository_id", "owner_id", "pr_number")
            )
            or not isinstance(result.get("target"), str)
            or not _TARGET.fullmatch(result["target"])
            or any(
                type(result.get(name)) is not int or result[name] <= 0
                for name in ("sequence", "dispatch_fence")
            )
            or not isinstance(result.get("deployed_identity"), dict)
            or any(
                not isinstance(result.get(name), str) or not _SHA.fullmatch(result[name])
                for name in ("base_sha", "head_sha", "head_tree", "workflow_sha")
            )
            or not isinstance(result.get("policy_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", result["policy_digest"])
            or not isinstance(result.get("policy_manifest"), dict)
            or not isinstance(result.get("review_bundle"), dict)
            or not isinstance(result["review_bundle"].get("check_evidence_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", result["review_bundle"]["check_evidence_digest"])
            or not isinstance(result.get("review_bundle_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", result["review_bundle_digest"])
            or result.get("status")
            not in {
                "awaiting_approval",
                "rejected",
                "expired",
                "invalidated",
                "queued",
                "merge_authorized",
                "merged_verified",
                "merged_blocked",
                "deploying",
                "healthy",
                "restoring",
                "restored_healthy",
                "recovery_failed",
            }
            or not _valid_merge_observation(
                result.get("merge_commit"),
                result.get("merge_tree"),
                result.get("merge_parents"),
            )
            or not _valid_merge_lease(result.get("merge_lease"), result.get("dispatch_fence"))
        ):
            raise CoordinatorError("broker_invalid_response")
        try:
            deployment = DeploymentIdentity(**result["deployed_identity"])
            policy = PolicyManifest.from_json(_canonical(result["policy_manifest"]))
            review_bytes = _canonical(result["review_bundle"])
            review_digest = hashlib.sha256(review_bytes).hexdigest()
            if (
                policy.digest != result["policy_digest"]
                or policy.repository_id != result["repository_id"]
                or policy.owner_id != result["owner_id"]
                or policy.target != result["target"]
                or deployment.policy_digest != policy.digest
                or result["review_bundle"].get("complete_delta_reviewed") is not True
                or review_digest != result["review_bundle_digest"]
                or not _valid_host_receipt(result.get("host_receipt"), result["dispatch_fence"])
            ):
                raise ValueError
        except Exception:
            raise CoordinatorError("broker_invalid_response") from None
        if self.dispatch_fence is not None and result["dispatch_fence"] != self.dispatch_fence:
            raise CoordinatorError("caller_denied")
        self.dispatch_fence = result["dispatch_fence"]
        self._target = result["target"]
        self._deployed_identity = deployment
        self.context = dict(result)
        return result

    @contextmanager
    def target_lock(self, target: str) -> Iterator[dict[str, Any]]:
        if (
            not isinstance(target, str)
            or not _TARGET.fullmatch(target)
            or target != self._target
            or self.dispatch_fence is None
            or self.caller is None
            or self.context is None
            or self.context["status"] not in {"queued", "merge_authorized"}
        ):
            raise CoordinatorError("invalid_input")
        caller = self.caller
        self._lease_reused = self.context["status"] == "merge_authorized"
        if self._lease_reused:
            lease_context = self.context.get("merge_lease")
            if (
                not isinstance(lease_context, dict)
                or lease_context["fence"] != self.dispatch_fence
                or lease_context["run_id"] != caller.run_id
                or lease_context["run_attempt"] != caller.run_attempt
                or lease_context["check_run_id"] != caller.check_run_id
            ):
                self._lease_reused = False
                raise CoordinatorError("reconciliation_required")
            lease = {
                "lease_id": lease_context["lease_id"],
                "dispatch_fence": lease_context["fence"],
                "expires_at": lease_context["expires_at"],
            }
        else:
            lease = self._client.post(
                f"/v1/releases/{self.approval_id}/merge-lease",
                {
                    "dispatch_fence": self.dispatch_fence,
                    "run_id": caller.run_id,
                    "run_attempt": caller.run_attempt,
                    "check_run_id": caller.check_run_id,
                },
            )
            if (
                set(lease) != {"lease_id", "dispatch_fence", "expires_at"}
                or not isinstance(lease.get("lease_id"), str)
                or not _is_uuid(lease["lease_id"])
                or type(lease.get("dispatch_fence")) is not int
                or lease["dispatch_fence"] != self.dispatch_fence
                or not isinstance(lease.get("expires_at"), str)
            ):
                raise CoordinatorError("broker_invalid_response")
        try:
            expires = dt.datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
            remaining = (expires - dt.datetime.now(dt.UTC)).total_seconds()
            if expires.tzinfo is None or (not self._lease_reused and not 0 < remaining <= 300):
                raise ValueError
        except (ValueError, OverflowError):
            raise CoordinatorError("broker_invalid_response") from None
        self._active_lease = {**lease, "deadline": time.monotonic() + remaining}
        try:
            yield self._active_lease
        finally:
            try:
                if not self._lease_reused:
                    released = self._client.delete(
                        f"/v1/releases/{self.approval_id}/merge-lease",
                        {
                            "lease_id": lease["lease_id"],
                            "dispatch_fence": lease["dispatch_fence"],
                        },
                    )
                    if released != {"released": True}:
                        raise CoordinatorError("broker_unavailable")
            finally:
                self._active_lease = None
                self._admission_deadline = None
                self._lease_reused = False

    def current_deployed_commit(self, target: str) -> str:
        if target != self._target:
            raise CoordinatorError("caller_denied")
        current = self._current_state(target)
        return current["deployed_commit"]

    def current_policy_digest(self) -> str:
        if self._target is None:
            self.get_context()
        if self._target is None:
            raise CoordinatorError("broker_invalid_response")
        return self._current_state(self._target)["policy_digest"]

    def _current_state(self, target: str) -> dict[str, str]:
        if not isinstance(target, str) or not _TARGET.fullmatch(target) or target != self._target:
            raise CoordinatorError("invalid_input")
        result = self._client.get(f"/v1/targets/{target}/current-state")
        if set(result) != {"deployed_identity", "deployed_commit", "policy_digest"}:
            raise CoordinatorError("broker_invalid_response")
        if not isinstance(result.get("deployed_identity"), dict):
            raise CoordinatorError("broker_invalid_response")
        try:
            identity = DeploymentIdentity(**result["deployed_identity"])
        except Exception:
            raise CoordinatorError("broker_invalid_response") from None
        if (
            self._deployed_identity is None
            or identity != self._deployed_identity
            or result.get("deployed_commit") != identity.git_sha
            or result.get("policy_digest") != identity.policy_digest
        ):
            raise CoordinatorError("caller_denied")
        return {
            "deployed_commit": identity.git_sha,
            "policy_digest": identity.policy_digest,
        }

    def verify_caller(
        self, identity: CallerIdentity, *, local_verifier: Callable[[CallerIdentity], bool]
    ) -> bool:
        if not isinstance(identity, CallerIdentity) or not callable(local_verifier):
            return False
        try:
            return bool(local_verifier(identity))
        except Exception:
            return False

    def admit_merge(self, intent: MergeIntent) -> bool:
        lease = self._require_lease()
        context = self.context
        if (
            self.dispatch_fence is None
            or not isinstance(intent, MergeIntent)
            or context is None
            or self._deployed_identity is None
            or intent.repository_id != context["repository_id"]
            or intent.owner_id != context["owner_id"]
            or intent.pr_number != context["pr_number"]
            or intent.target != context["target"]
            or intent.deployed_commit != self._deployed_identity.git_sha
            or intent.base_commit != context["base_sha"]
            or intent.head_commit != context["head_sha"]
            or intent.policy_digest != context["policy_digest"]
        ):
            raise CoordinatorError("caller_denied")
        caller = intent.caller
        body = {
            "head_sha": intent.head_commit,
            "dispatch_fence": self.dispatch_fence,
            "run_id": caller.run_id,
            "run_attempt": caller.run_attempt,
            "check_run_id": caller.check_run_id,
            "lease_id": lease["lease_id"],
        }
        result = self._client.post(f"/v1/releases/{self.approval_id}/merge-admission", body)
        admission_fields = {
            "approval_id",
            "lease_id",
            "dispatch_fence",
            "expires_at",
            "admitted",
            "may_start",
            "deployed_identity",
            "base_sha",
            "head_sha",
            "head_tree",
            "policy_digest",
            "expected_merge_tree",
            "run_id",
            "run_attempt",
            "check_run_id",
        }
        if (
            set(result) != admission_fields
            or result.get("approval_id") != str(self.approval_id)
            or result.get("lease_id") != lease["lease_id"]
            or type(result.get("dispatch_fence")) is not int
            or result.get("dispatch_fence") != self.dispatch_fence
            or type(result.get("admitted")) is not bool
            or type(result.get("may_start")) is not bool
            or not isinstance(result.get("expires_at"), str)
            or (result["may_start"] and not result["admitted"])
            or result.get("base_sha") != context["base_sha"]
            or result.get("head_sha") != intent.head_commit
            or result.get("head_tree") != context["head_tree"]
            or result.get("policy_digest") != context["policy_digest"]
            or result.get("expected_merge_tree") != intent.expected_merge_tree
            or result.get("run_id") != caller.run_id
            or result.get("run_attempt") != caller.run_attempt
            or result.get("check_run_id") != caller.check_run_id
        ):
            raise CoordinatorError("broker_invalid_response")
        try:
            admitted_identity = DeploymentIdentity(**result["deployed_identity"])
            expires = dt.datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
            remaining = (expires - dt.datetime.now(dt.UTC)).total_seconds()
            if expires.tzinfo is None or admitted_identity != self._deployed_identity:
                raise ValueError
        except (ValueError, OverflowError):
            raise CoordinatorError("broker_invalid_response") from None
        if result["admitted"] is not True:
            self._admission_deadline = None
            return False
        if result["may_start"] is not True:
            self._admission_deadline = None
            return True
        lease = self._require_lease()
        admission_deadline = time.monotonic() + remaining
        # Both deadlines originate from the same broker expiry but cross the
        # wall-clock/monotonic boundary separately. Clamp tiny conversion drift;
        # a material extension beyond the lease remains a denial.
        if admission_deadline > lease["deadline"]:
            drift = admission_deadline - lease["deadline"]
            if drift <= 0.25:
                admission_deadline = lease["deadline"]
        if remaining <= 0 or admission_deadline > lease["deadline"]:
            self._admission_deadline = None
            return False
        self._admission_deadline = admission_deadline
        if not self.admission_live():
            self._admission_deadline = None
            return False
        return True

    def admission_live(self) -> bool:
        lease = self._active_lease
        deadline = self._admission_deadline
        return bool(
            lease is not None
            and deadline is not None
            and time.monotonic() < min(lease["deadline"], deadline)
        )

    def persist_merge(self, outcome: MergeOutcome) -> None:
        if not isinstance(outcome, MergeOutcome) or self.dispatch_fence is None:
            raise CoordinatorError("merge_persistence_failed")
        if (
            outcome.outcome not in {"merged_verified", "merged_blocked"}
            or not isinstance(outcome.merge_commit, str)
            or not _SHA.fullmatch(outcome.merge_commit)
            or not isinstance(outcome.tree, str)
            or not _SHA.fullmatch(outcome.tree)
            or not isinstance(outcome.expected_tree, str)
            or not _SHA.fullmatch(outcome.expected_tree)
            or not isinstance(outcome.parents, tuple)
            or len(outcome.parents) > _MAX_OBSERVED_PARENTS
            or any(
                not isinstance(parent, str) or not _SHA.fullmatch(parent)
                for parent in outcome.parents
            )
        ):
            raise CoordinatorError("merge_persistence_failed")
        # Reconciliation is allowed after lease expiry; the broker binds it to the
        # immutable lease/fence and never treats it as renewed admission authority.
        lease = self._require_lease(require_live=False)
        if len(outcome.parents) == 2 and outcome.evidence is not None:
            if (
                outcome.evidence.merge_commit != outcome.merge_commit
                or outcome.evidence.parents != outcome.parents
                or outcome.evidence.tree != outcome.tree
            ):
                raise CoordinatorError("merge_persistence_failed")
            route = f"/v1/releases/{self.approval_id}/merge-result"
            body = {
                "merge_commit": outcome.merge_commit,
                "parents": list(outcome.parents),
                "tree": outcome.tree,
                "expected_tree": outcome.expected_tree,
                "dispatch_fence": self.dispatch_fence,
                "lease_id": lease["lease_id"],
            }
        else:
            route = f"/v1/releases/{self.approval_id}/merge-anomalies"
            body = {
                "merge_commit": outcome.merge_commit,
                "parents": list(outcome.parents),
                "tree": outcome.tree,
                "expected_tree": outcome.expected_tree,
                "reason": "unexpected_merge_observation",
                "dispatch_fence": self.dispatch_fence,
                "lease_id": lease["lease_id"],
            }
        try:
            result = None
            for attempt in range(2):
                try:
                    result = self._client.post(route, body)
                    break
                except CoordinatorError as exc:
                    if exc.code != "broker_unavailable" or attempt == 1:
                        raise
            if result is None or result.get("recorded") is not True:
                raise CoordinatorError("merge_persistence_failed")
        except CoordinatorError:
            raise
        except Exception:
            raise CoordinatorError("merge_persistence_failed") from None

    def _require_lease(self, *, require_live: bool = True) -> dict[str, Any]:
        lease = self._active_lease
        if (
            lease is None
            or (require_live and time.monotonic() >= lease["deadline"])
            or lease.get("dispatch_fence") != self.dispatch_fence
        ):
            raise CoordinatorError("caller_denied")
        return lease


class ReleaseCoordinator:
    """Bind the strict CLI approval UUID to the P04 trusted merge controller."""

    def __init__(
        self,
        controller: MergeController,
        broker: BrokerMergeCallbacks,
        context: Mapping[str, Any],
    ) -> None:
        if (
            not isinstance(controller, MergeController)
            or not isinstance(broker, BrokerMergeCallbacks)
            or not isinstance(context, Mapping)
        ):
            raise CoordinatorError("invalid_input")
        self._controller = controller
        self._broker = broker
        self.context = dict(context)

    def merge(self, snapshot: Any, bundle: Any, *, caller: CallerIdentity) -> Any:
        self._broker.bind_caller(caller)
        return self._controller.merge(
            snapshot,
            bundle,
            caller=caller,
            prior_admission=self.context["status"] == "merge_authorized",
        )


def build_merge_coordinator(
    github: GitHubEvidenceClient,
    broker_client: BrokerHttpClient,
    *,
    approval_id: UUID,
    repository_id: int,
    owner_id: int,
    caller_verifier: Callable[[CallerIdentity], bool],
    dispatch_fence: int | None = None,
    broker_callbacks: BrokerMergeCallbacks | None = None,
) -> ReleaseCoordinator:
    """Wire P04 merge control to P05/P07 broker callbacks without ambient defaults."""
    if (
        not isinstance(github, GitHubEvidenceClient)
        or not isinstance(broker_client, BrokerHttpClient)
        or not isinstance(approval_id, UUID)
        or type(repository_id) is not int
        or repository_id <= 0
        or type(owner_id) is not int
        or owner_id <= 0
        or not callable(caller_verifier)
    ):
        raise CoordinatorError("invalid_input")
    if broker_callbacks is None:
        broker = BrokerMergeCallbacks(
            broker_client, approval_id=approval_id, dispatch_fence=dispatch_fence
        )
        context = broker.get_context()
    else:
        if (
            not isinstance(broker_callbacks, BrokerMergeCallbacks)
            or broker_callbacks._client is not broker_client
            or broker_callbacks.approval_id != approval_id
            or broker_callbacks.context is None
            or broker_callbacks.context["status"] not in {"queued", "merge_authorized"}
            or (
                broker_callbacks.context["status"] == "merge_authorized"
                and broker_callbacks.context["merge_lease"] is None
            )
        ):
            raise CoordinatorError("invalid_input")
        broker = broker_callbacks
        context = broker.context
    controller = MergeController(
        github,
        repository_id=repository_id,
        owner_id=owner_id,
        target=context["target"],
        base_branch="main",
        merge_transport=GitHubMergeTransport(github),
        target_lock=broker.target_lock,
        current_deployed_commit=broker.current_deployed_commit,
        current_policy_digest=broker.current_policy_digest,
        verify_caller=lambda identity: broker.verify_caller(
            identity, local_verifier=caller_verifier
        ),
        admit_merge=broker.admit_merge,
        admission_live=broker.admission_live,
        persist_merge=broker.persist_merge,
    )
    return ReleaseCoordinator(controller, broker, context)


class GitHubJobCorrelationVerifier:
    """Require the signed OIDC job ID to match the exact API run attempt and job."""

    def __init__(
        self,
        client: GitHubEvidenceClient,
        *,
        workflow_path: str,
        runner_environment: str,
        expected_run_title: str | None = None,
    ) -> None:
        if (
            not isinstance(client, GitHubEvidenceClient)
            or not isinstance(workflow_path, str)
            or not workflow_path.startswith(".github/workflows/")
            or not isinstance(runner_environment, str)
            or runner_environment not in {"github-hosted", "self-hosted"}
            or expected_run_title is not None
            and (
                not isinstance(expected_run_title, str)
                or not re.fullmatch(r"release:[0-9a-f-]{36}", expected_run_title)
            )
        ):
            raise CoordinatorError("invalid_input")
        self._client = client
        self._workflow_path = workflow_path
        self._runner_environment = runner_environment
        self._expected_run_title = expected_run_title

    def __call__(self, identity: CallerIdentity) -> bool:
        if (
            not isinstance(identity, CallerIdentity)
            or identity.repository_id != self._client.repository_id
            or identity.runner_environment != self._runner_environment
        ):
            return False
        try:
            jobs = self._jobs(identity.run_id, identity.run_attempt, identity.workflow_sha)
            matches = [
                job
                for job in jobs
                if job.get("run_id") == identity.run_id
                and job.get("run_attempt") == identity.run_attempt
                and _check_run_id_from_job_url(
                    job.get("check_run_url"), self._client.owner, self._client.repository
                )
                == identity.check_run_id
            ]
            return len(matches) == 1 and _job_is_live_or_successful(matches[0])
        except GitHubEvidenceError:
            return False

    def check_run_id_for_job(
        self,
        *,
        run_id: int,
        run_attempt: int,
        workflow_sha: str,
        job_name: str,
    ) -> int:
        """Resolve a unique configured job to its immutable check-run ID."""
        if (
            type(run_id) is not int
            or run_id <= 0
            or type(run_attempt) is not int
            or run_attempt <= 0
            or not isinstance(workflow_sha, str)
            or not _SHA.fullmatch(workflow_sha)
            or not isinstance(job_name, str)
            or not re.fullmatch(r"[A-Za-z0-9_. -]{1,128}", job_name)
        ):
            raise CoordinatorError("invalid_input")
        try:
            jobs = self._jobs(run_id, run_attempt, workflow_sha)
            matches = [
                job
                for job in jobs
                if job.get("name") == job_name
                and job.get("run_id") == run_id
                and job.get("run_attempt") == run_attempt
                and _job_is_live_or_successful(job)
            ]
            if len(matches) != 1:
                raise CoordinatorError("job_unverified")
            check_id = _check_run_id_from_job_url(
                matches[0].get("check_run_url"), self._client.owner, self._client.repository
            )
            if check_id is None:
                raise CoordinatorError("job_unverified")
            return check_id
        except GitHubEvidenceError:
            raise CoordinatorError("job_unverified") from None

    def _jobs(self, run_id: int, run_attempt: int, workflow_sha: str) -> tuple[dict[str, Any], ...]:
        base = f"actions/runs/{run_id}/attempts/{run_attempt}"
        run = self._client.get_json(base)
        if (
            run.get("id") != run_id
            or run.get("run_attempt") != run_attempt
            or _nested_id(run, "repository", "id") != self._client.repository_id
            or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != "main"
            or run.get("head_sha") != workflow_sha
            or _workflow_path(run.get("path")) != self._workflow_path
            or self._expected_run_title is not None
            and run.get("display_title") != self._expected_run_title
        ):
            raise GitHubEvidenceError("workflow_untrusted")
        return self._client.list_paginated(f"{base}/jobs", "jobs")


class CiGatewayClient:
    """Fixed-origin authenticated gateway contract for trusted pull-request CI."""

    def __init__(
        self,
        origin: str,
        *,
        token_provider: Callable[[], str],
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = BrokerHttpClient(origin, token_provider=token_provider, transport=transport)

    def close(self) -> None:
        self._client.close()

    def get_ci_context(self, pull_request_number: int) -> dict[str, Any]:
        _validate_pr_number(pull_request_number)
        return self._client.get(f"/v1/ci/pull-requests/{pull_request_number}/context")

    def upload_ci_report(
        self, pull_request_number: int, body: Mapping[str, object]
    ) -> dict[str, Any]:
        _validate_pr_number(pull_request_number)
        return self._client.post(f"/v1/ci/pull-requests/{pull_request_number}/report", body)


class CiPhaseCoordinator:
    """Run the three trusted CI phases from pinned gateway and installed evidence.

    CI context is supplied by an authenticated fixed gateway and must include an
    insert-once P03 provenance record. Candidate source is read only from exact H
    blobs through P01's isolated object reader, then passed as data to P02's fixed
    Docker/runsc worker. This class never checks out or executes source itself.
    """

    def __init__(
        self,
        *,
        repository: str | os.PathLike[str],
        policy: PolicyManifest,
        github: GitHubEvidenceClient,
        gateway: CiGatewayClient | Any,
        sandbox: SandboxConfig,
        baseline_root: str | os.PathLike[str],
        baseline_manifest: WorkerManifest,
        baseline_manifest_bytes: bytes,
        baseline_worker_source: bytes,
        runner: Any,
        report_scanner: Callable[[ScanInput], ScanVerdict],
        state_directory: str | os.PathLike[str],
        artifact_directory: str | os.PathLike[str],
        expected_repository: str,
        repository_id: int,
        owner_id: int,
        scanner_identity_sha256: str,
        git_fetcher: GitObjectFetcher | None = None,
    ) -> None:
        if (
            not isinstance(policy, PolicyManifest)
            or not callable(getattr(github, "get_json", None))
            or not callable(getattr(github, "close", None))
            or not callable(getattr(gateway, "get_ci_context", None))
            or not callable(getattr(gateway, "upload_ci_report", None))
            or not isinstance(sandbox, SandboxConfig)
            or sandbox.resources.max_output_bytes > 512 * 1024
            or not isinstance(baseline_manifest, WorkerManifest)
            or baseline_manifest.version != 2
            or baseline_manifest.policy_digest != policy.digest
            or set(baseline_manifest.allowed_paths) != set(policy.allowed_paths)
            or not isinstance(baseline_manifest_bytes, bytes)
            or not isinstance(baseline_worker_source, bytes)
            or hashlib.sha256(baseline_manifest_bytes).hexdigest() != baseline_manifest.digest
            or hashlib.sha256(baseline_worker_source).hexdigest()
            != baseline_manifest.worker_source_digest
            or not callable(runner.run)
            or getattr(runner, "worker_manifest_digest", None) != baseline_manifest.digest
            or getattr(runner, "policy_digest", None) != policy.digest
            or getattr(runner, "baseline_commit", None) != baseline_manifest.baseline_commit
            or getattr(runner, "max_input_bytes", None) != sandbox.resources.workspace_bytes
            or getattr(runner, "max_output_bytes", None) != sandbox.resources.max_output_bytes
            or not callable(report_scanner)
            or not isinstance(expected_repository, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", expected_repository)
            or type(repository_id) is not int
            or repository_id <= 0
            or repository_id != policy.repository_id
            or type(owner_id) is not int
            or owner_id <= 0
            or owner_id != policy.owner_id
            or getattr(github, "repository_id", None) != repository_id
            or f"{getattr(github, 'owner', '')}/{getattr(github, 'repository', '')}"
            != expected_repository
            or not isinstance(scanner_identity_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", scanner_identity_sha256) is None
            or (
                git_fetcher is not None
                and not callable(getattr(git_fetcher, "ensure_history", None))
            )
        ):
            raise CoordinatorError("invalid_runtime_configuration")
        try:
            repo = Path(repository).resolve(strict=True)
            baseline = Path(baseline_root).resolve(strict=True)
            state = Path(state_directory).resolve(strict=False)
            artifacts = Path(artifact_directory).resolve(strict=False)
            if not repo.is_dir() or not baseline.is_dir():
                raise ValueError
            _prepare_private_directory(state)
            _prepare_private_directory(artifacts)
        except (OSError, RuntimeError, ValueError):
            raise CoordinatorError("invalid_runtime_configuration") from None
        if (
            sandbox.policy_digest != policy.digest
            or sandbox.baseline_commit != baseline_manifest.baseline_commit
            or sandbox.worker_manifest_digest != baseline_manifest.digest
            or github.repository_id != repository_id
        ):
            raise CoordinatorError("invalid_runtime_configuration")
        self.repository = repo
        self.policy = policy
        self.github = github
        self.gateway = gateway
        self.sandbox = sandbox
        self.baseline_root = baseline
        self.baseline_manifest = baseline_manifest
        self.baseline_manifest_bytes = baseline_manifest_bytes
        self.baseline_worker_source = baseline_worker_source
        self.runner = runner
        self.report_scanner = report_scanner
        self.state_directory = state
        self.artifact_directory = artifacts
        self.expected_repository = expected_repository
        self.repository_id = repository_id
        self.owner_id = owner_id
        self.scanner_identity_sha256 = scanner_identity_sha256
        self.git_fetcher = git_fetcher

    def state_path(self, pull_request_number: int) -> Path:
        _validate_pr_number(pull_request_number)
        return self.state_directory / f"pr-{pull_request_number}.json"

    def prepare(self, pull_request_number: int) -> dict[str, object]:
        _validate_pr_number(pull_request_number)
        try:
            context = self.gateway.get_ci_context(pull_request_number)
            self._validate_context(context, pull_request_number)
            self._validate_pull_request(pull_request_number, context)
            if self.git_fetcher is not None:
                try:
                    self.git_fetcher.ensure_history(
                        pull_request_number=pull_request_number,
                        deployed_commit=context["deployed_commit"],
                        base_commit=context["base_commit"],
                        head_commit=context["head_commit"],
                        policy=self.policy,
                    )
                except GitFetchError:
                    raise CoordinatorError("ci_context_mismatch") from None
            bundle = collect_release_bundle(
                self.repository,
                context["deployed_commit"],
                context["base_commit"],
                context["head_commit"],
                self.policy,
            )
            if (
                bundle.head_tree != context["head_tree"]
                or bundle.base_commit != self.baseline_manifest.baseline_commit
                or not bundle.base_is_ancestor_of_head
                or not bundle.base_is_descendant_of_deployed
            ):
                raise CoordinatorError("ci_context_mismatch")
            files = _read_ci_overlay(
                self.repository,
                context["head_commit"],
                bundle.base_to_head,
                self.policy,
            )
            request = {
                "version": 1,
                "base_commit": bundle.base_commit,
                "policy_digest": self.policy.digest,
                "worker_manifest_digest": self.baseline_manifest.digest,
                "specification": context["specification"],
                "synthetic_examples": context["synthetic_examples"],
                "files": files,
            }
            request_bytes = _canonical(request)
            if len(request_bytes) > self.sandbox.resources.workspace_bytes:
                raise CoordinatorError("ci_patch_unrepresentable")
            prepared = {
                "version": 1,
                "pull_request_number": pull_request_number,
                "request_id": context["request_id"],
                "repository": self.expected_repository,
                "repository_id": self.repository_id,
                "owner_id": self.owner_id,
                "deployed_commit": bundle.deployed_commit,
                "base_commit": bundle.base_commit,
                "head_commit": bundle.head_commit,
                "head_tree": bundle.head_tree,
                "policy_digest": bundle.policy_digest,
                "context_sha256": context["context_sha256"],
                "patch_sha256": hashlib.sha256(request_bytes).hexdigest(),
                "request": request,
            }
            prepared["state_sha256"] = hashlib.sha256(_canonical(prepared)).hexdigest()
            _write_private_immutable(self.state_path(pull_request_number), _canonical(prepared))
            return {
                "phase": "prepared",
                "pull_request_number": pull_request_number,
                "request_id": context["request_id"],
                "head_sha": bundle.head_commit,
                "patch_sha256": prepared["patch_sha256"],
            }
        except CoordinatorError:
            raise
        except (GitEvidenceError, PolicyError, OSError, ValueError, TypeError, KeyError):
            raise CoordinatorError("ci_context_mismatch") from None

    def run_candidate(self, pull_request_number: int) -> dict[str, object]:
        prepared = self._read_prepared(pull_request_number)
        self._assert_prepared_context(prepared)
        request_bytes = _canonical(prepared["request"])
        if hashlib.sha256(request_bytes).hexdigest() != prepared["patch_sha256"]:
            raise CoordinatorError("ci_state_invalid")
        try:
            report = self.runner.run(request_bytes)
        except DevelopmentError as exc:
            if exc.code in {
                "sandbox_identity_invalid",
                "sandbox_unavailable",
                "sandbox_command_failed",
                "sandbox_cleanup_failed",
            }:
                raise CoordinatorError("invalid_runtime_configuration") from None
            raise CoordinatorError("ci_runner_failed") from None
        except Exception:
            raise CoordinatorError("ci_runner_failed") from None
        if (
            not isinstance(report, bytes)
            or not 1 <= len(report) <= self.sandbox.resources.max_output_bytes
        ):
            raise CoordinatorError("ci_runner_failed")
        stored = {
            "version": 1,
            "prepared_state_sha256": prepared["state_sha256"],
            "report_sha256": hashlib.sha256(report).hexdigest(),
            "report_base64": base64.b64encode(report).decode("ascii"),
        }
        path = self.state_directory / f"pr-{pull_request_number}.report.json"
        _write_private_immutable(path, _canonical(stored))
        return {
            "phase": "tested",
            "pull_request_number": pull_request_number,
            "request_id": prepared["request_id"],
            "head_sha": prepared["head_commit"],
            "report_sha256": stored["report_sha256"],
        }

    def scan_and_publish_report(self, pull_request_number: int) -> dict[str, object]:
        prepared = self._read_prepared(pull_request_number)
        self._assert_prepared_context(prepared)
        path = self.state_directory / f"pr-{pull_request_number}.report.json"
        stored = _read_private_json(path, max_bytes=2 * _MAX_RESPONSE)
        if (
            set(stored) != {"version", "prepared_state_sha256", "report_sha256", "report_base64"}
            or stored.get("version") != 1
            or stored.get("prepared_state_sha256") != prepared["state_sha256"]
            or not isinstance(stored.get("report_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", stored["report_sha256"]) is None
            or not isinstance(stored.get("report_base64"), str)
        ):
            raise CoordinatorError("ci_state_invalid")
        try:
            report = base64.b64decode(stored["report_base64"], validate=True)
        except (ValueError, TypeError):
            raise CoordinatorError("ci_state_invalid") from None
        if (
            not report
            or len(report) > self.sandbox.resources.max_output_bytes
            or hashlib.sha256(report).hexdigest() != stored["report_sha256"]
        ):
            raise CoordinatorError("ci_state_invalid")
        try:
            verdict = self.report_scanner(ScanInput("ci-execution-report", report))
        except Exception:
            raise CoordinatorError("ci_report_rejected") from None
        if verdict is not ScanVerdict.CLEAN:
            raise CoordinatorError("ci_report_rejected")
        self._validate_worker_report(report)
        artifact_path = (
            self.artifact_directory
            / f"pr-{pull_request_number}-{prepared['request_id']}-{stored['report_sha256']}.json"
        )
        _write_private_immutable(artifact_path, report)
        upload = {
            "request_id": prepared["request_id"],
            "head_sha": prepared["head_commit"],
            "policy_digest": prepared["policy_digest"],
            "context_sha256": prepared["context_sha256"],
            "report_sha256": stored["report_sha256"],
            "report_base64": stored["report_base64"],
        }
        try:
            receipt = self.gateway.upload_ci_report(pull_request_number, upload)
        except Exception:
            raise CoordinatorError("ci_upload_failed") from None
        if (
            not isinstance(receipt, dict)
            or set(receipt)
            != {
                "request_id",
                "pull_request_number",
                "head_sha",
                "report_sha256",
                "artifact_id",
                "stored",
            }
            or receipt.get("request_id") != prepared["request_id"]
            or receipt.get("pull_request_number") != pull_request_number
            or receipt.get("head_sha") != prepared["head_commit"]
            or receipt.get("report_sha256") != stored["report_sha256"]
            or not isinstance(receipt.get("artifact_id"), str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", receipt["artifact_id"]) is None
            or receipt.get("stored") is not True
        ):
            raise CoordinatorError("ci_upload_failed")
        return {
            "phase": "reported",
            "pull_request_number": pull_request_number,
            "request_id": prepared["request_id"],
            "head_sha": prepared["head_commit"],
            "report_sha256": stored["report_sha256"],
            "artifact_id": receipt["artifact_id"],
        }

    def close(self) -> None:
        for adapter in (self.github, self.gateway):
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    def _assert_prepared_context(self, prepared: Mapping[str, Any]) -> None:
        try:
            number = prepared["pull_request_number"]
            context = self.gateway.get_ci_context(number)
            self._validate_context(context, number)
            self._validate_pull_request(number, context)
            if context["context_sha256"] != prepared["context_sha256"]:
                raise CoordinatorError("ci_context_mismatch")
        except CoordinatorError:
            raise
        except Exception:
            raise CoordinatorError("ci_context_mismatch") from None

    def _validate_context(self, context: object, pull_request_number: int) -> None:
        required = {
            "version",
            "request_id",
            "repository",
            "repository_id",
            "owner_id",
            "pull_request_number",
            "base_branch",
            "deployed_commit",
            "base_commit",
            "head_commit",
            "head_tree",
            "policy_digest",
            "policy_manifest",
            "provenance",
            "published_ref",
            "specification",
            "synthetic_examples",
            "pull_request_title",
            "pull_request_body",
            "artifacts",
            "context_sha256",
        }
        if (
            not isinstance(context, dict)
            or set(context) != required
            or type(context.get("version")) is not int
            or context["version"] != 1
            or context.get("repository") != self.expected_repository
            or type(context.get("repository_id")) is not int
            or context["repository_id"] != self.repository_id
            or type(context.get("owner_id")) is not int
            or context["owner_id"] != self.owner_id
            or type(context.get("pull_request_number")) is not int
            or context["pull_request_number"] != pull_request_number
            or context.get("base_branch") != "main"
            or not isinstance(context.get("request_id"), str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", context["request_id"]) is None
            or context.get("published_ref") != f"refs/heads/clawdot/{context.get('request_id')}"
            or any(
                not isinstance(context.get(name), str) or not _SHA.fullmatch(context[name])
                for name in ("deployed_commit", "base_commit", "head_commit", "head_tree")
            )
            or context.get("policy_digest") != self.policy.digest
            or not isinstance(context.get("specification"), str)
            or not context["specification"].strip()
            or len(context["specification"].encode("utf-8")) > 8192
            or not isinstance(context.get("synthetic_examples"), list)
            or not 1 <= len(context["synthetic_examples"]) <= 8
            or len(_canonical(context["synthetic_examples"])) > 16 * 1024
            or not isinstance(context.get("pull_request_title"), str)
            or len(context["pull_request_title"].encode("utf-8")) > 16 * 1024
            or not isinstance(context.get("pull_request_body"), str)
            or len(context["pull_request_body"].encode("utf-8")) > 64 * 1024
            or not isinstance(context.get("artifacts"), list)
            or len(context["artifacts"]) > 64
            or not isinstance(context.get("context_sha256"), str)
            or context["context_sha256"] != ci_context_digest(context)
        ):
            raise CoordinatorError("ci_context_mismatch")
        artifact_total = 0
        try:
            for artifact in context["artifacts"]:
                if (
                    not isinstance(artifact, dict)
                    or set(artifact) != {"name", "sha256", "content_base64"}
                    or not isinstance(artifact["name"], str)
                    or re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", artifact["name"]) is None
                    or not isinstance(artifact["sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) is None
                    or not isinstance(artifact["content_base64"], str)
                    or len(artifact["content_base64"]) > 350_000
                ):
                    raise ValueError
                content = base64.b64decode(artifact["content_base64"], validate=True)
                artifact_total += len(content)
                if (
                    not content
                    or artifact_total > 512 * 1024
                    or hashlib.sha256(content).hexdigest() != artifact["sha256"]
                ):
                    raise ValueError
        except Exception:
            raise CoordinatorError("ci_context_mismatch") from None
        try:
            if PolicyManifest.from_json(_canonical(context["policy_manifest"])) != self.policy:
                raise ValueError
        except Exception:
            raise CoordinatorError("ci_context_mismatch") from None
        provenance = context.get("provenance")
        expected_provenance_fields = {
            "request_id",
            "deployed_commit",
            "base_commit",
            "head_commit",
            "policy_digest",
            "scanner_identity_sha256",
            "baseline_history_scan_complete",
            "bundle_sha256",
            "surface_manifest_sha256",
            "scanned_surface_count",
            "scanned_bytes",
        }
        if (
            not isinstance(provenance, dict)
            or set(provenance) != expected_provenance_fields
            or provenance.get("request_id") != context["request_id"]
            or provenance.get("deployed_commit") != context["deployed_commit"]
            or provenance.get("base_commit") != context["base_commit"]
            or provenance.get("head_commit") != context["head_commit"]
            or provenance.get("policy_digest") != context["policy_digest"]
            or provenance.get("scanner_identity_sha256") != self.scanner_identity_sha256
            or provenance.get("baseline_history_scan_complete") is not True
            or any(
                not isinstance(provenance.get(name), str)
                or re.fullmatch(r"[0-9a-f]{64}", provenance[name]) is None
                for name in ("bundle_sha256", "surface_manifest_sha256")
            )
            or type(provenance.get("scanned_surface_count")) is not int
            or not 1 <= provenance["scanned_surface_count"] <= 100_000
            or type(provenance.get("scanned_bytes")) is not int
            or not 0 <= provenance["scanned_bytes"] <= 32 * 1024 * 1024
        ):
            raise CoordinatorError("ci_context_mismatch")

    def _validate_pull_request(self, number: int, context: Mapping[str, Any]) -> None:
        try:
            pull = self.github.get_json(f"pulls/{number}")
        except Exception:
            raise CoordinatorError("ci_context_mismatch") from None
        base, head = pull.get("base"), pull.get("head")
        if (
            pull.get("number") != number
            or pull.get("state") != "open"
            or pull.get("merged") is not False
            or not isinstance(base, dict)
            or not isinstance(head, dict)
            or base.get("ref") != context["base_branch"]
            or base.get("sha") != context["base_commit"]
            or head.get("sha") != context["head_commit"]
            or head.get("ref") != context["published_ref"].removeprefix("refs/heads/")
            or pull.get("title") != context["pull_request_title"]
            or pull.get("body") != context["pull_request_body"]
            or _nested_id(base, "repo", "id") != self.repository_id
            or _nested_id(head, "repo", "id") != self.repository_id
            or _nested_id(base, "repo", "owner", "id") != self.owner_id
        ):
            raise CoordinatorError("ci_context_mismatch")

    def _read_prepared(self, pull_request_number: int) -> dict[str, Any]:
        _validate_pr_number(pull_request_number)
        value = _read_private_json(self.state_path(pull_request_number), max_bytes=4 * 1024 * 1024)
        state_digest = value.get("state_sha256")
        unsigned = dict(value)
        unsigned.pop("state_sha256", None)
        if (
            value.get("version") != 1
            or value.get("pull_request_number") != pull_request_number
            or not isinstance(state_digest, str)
            or hashlib.sha256(_canonical(unsigned)).hexdigest() != state_digest
            or value.get("policy_digest") != self.policy.digest
            or value.get("base_commit") != self.baseline_manifest.baseline_commit
            or not isinstance(value.get("request"), dict)
        ):
            raise CoordinatorError("ci_state_invalid")
        return value

    def _validate_worker_report(self, report: bytes) -> None:
        try:
            value = json.loads(
                report.decode("utf-8", errors="strict"), object_pairs_hook=_unique_pairs
            )
            if (
                not isinstance(value, dict)
                or set(value)
                != {
                    "version",
                    "base_commit",
                    "policy_digest",
                    "worker_manifest_digest",
                    "execution_kind",
                    "exit_status",
                    "output",
                }
                or type(value["version"]) is not int
                or value["version"] != 2
                or value["base_commit"] != self.baseline_manifest.baseline_commit
                or value["policy_digest"] != self.policy.digest
                or value["worker_manifest_digest"] != self.baseline_manifest.digest
                or value["execution_kind"] != "monorepo-pytest-v1"
                or type(value["exit_status"]) is not int
                or value["exit_status"] != 0
                or not isinstance(value["output"], str)
                or len(value["output"].encode("utf-8", errors="strict"))
                > self.sandbox.resources.max_output_bytes
            ):
                raise ValueError
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise CoordinatorError("ci_report_invalid") from None


@dataclass(frozen=True)
class ProtectedCiRuntimeConfig:
    """Digest-pinned installed CI settings and process-only credentials."""

    values: Mapping[str, Any] = field(repr=False)
    digest: str
    github_api_token: str = field(repr=False)
    gateway_token: str = field(repr=False)

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> ProtectedCiRuntimeConfig:
        source = os.environ if environment is None else environment
        try:
            config_path = Path(source["RELEASE_CONTROL_CI_CONFIG_PATH"])
            expected_digest = source["RELEASE_CONTROL_CI_CONFIG_SHA256"]
            raw = _read_protected_file(config_path, max_bytes=256 * 1024)
            if (
                not config_path.is_absolute()
                or not isinstance(expected_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
                or hashlib.sha256(raw).hexdigest() != expected_digest
            ):
                raise ValueError
            values = json.loads(raw, object_pairs_hook=_unique_pairs)
            if not isinstance(values, dict) or _canonical(values) != raw:
                raise ValueError
            github_token = source["RELEASE_CONTROL_GITHUB_API_TOKEN"]
            gateway_token = source["RELEASE_CONTROL_CI_GATEWAY_TOKEN"]
            if not _TOKEN.fullmatch(github_token) or not _TOKEN.fullmatch(gateway_token):
                raise ValueError
            _validate_ci_config_values(values)
            repository = values["repository"]
            if source.get("GITHUB_REPOSITORY") != repository:
                raise ValueError
            if source.get("GITHUB_EVENT_NAME") != "pull_request":
                raise ValueError
            return cls(values, expected_digest, github_token, gateway_token)
        except CoordinatorError:
            raise
        except Exception:
            raise CoordinatorError("invalid_runtime_configuration") from None


def build_ci_coordinator(
    environment: Mapping[str, str] | None = None,
    *,
    gateway_transport: httpx.BaseTransport | None = None,
    github_transport: httpx.BaseTransport | None = None,
    process_executor: ProcessExecutor | None = None,
    git_fetch_executor: GitProcessExecutor | None = None,
) -> CiPhaseCoordinator:
    """Construct the actual CI runtime from protected config and fixed adapters."""
    runtime = ProtectedCiRuntimeConfig.from_environment(environment)
    config = runtime.values
    try:
        policy_raw = _read_protected_file(Path(config["policy_path"]), max_bytes=64 * 1024)
        if hashlib.sha256(policy_raw).hexdigest() != config["policy_sha256"]:
            raise ValueError
        policy = PolicyManifest.from_json(policy_raw)
        worker_raw = _read_protected_file(
            Path(config["worker_manifest_path"]), max_bytes=4 * 1024 * 1024
        )
        if hashlib.sha256(worker_raw).hexdigest() != config["worker_manifest_sha256"]:
            raise ValueError
        worker_dir = Path(__file__).resolve().parent
        worker_source = _read_protected_file(
            worker_dir / "development_worker.py", max_bytes=1024 * 1024
        )
        harness_source = _read_protected_file(
            worker_dir / "monorepo_harness.py", max_bytes=1024 * 1024
        )
        manifest = WorkerManifest.from_bytes(
            worker_raw,
            expected_manifest_digest=config["worker_manifest_sha256"],
            worker_source=worker_source,
        )
        if (
            manifest.version != 2
            or hashlib.sha256(harness_source).hexdigest() != manifest.test_harness_digest
        ):
            raise ValueError
        _verify_installed_baseline(Path(config["baseline_root"]), worker_raw)
        resources = ResourcePolicy(**config["resources"])
        if resources.max_output_bytes > 512 * 1024:
            raise ValueError
        sandbox = SandboxConfig(
            config["docker_path"],
            config["runtime_name"],
            config["runtime_path"],
            config["runtime_sha256"],
            config["image_reference"],
            manifest.digest,
            policy.digest,
            manifest.baseline_commit,
            resources,
        )
        scanner = ScannerBinaryAdapter(
            config["scanner_executable_path"],
            config["scanner_configuration_path"],
            expected_executable_sha256=config["scanner_executable_sha256"],
            expected_configuration_sha256=config["scanner_configuration_sha256"],
        )
        if scanner.identity_sha256 != config["publication_scanner_identity_sha256"]:
            raise ValueError
        owner, repository = config["repository"].split("/", 1)
        github = GitHubEvidenceClient(
            owner=owner,
            repository=repository,
            repository_id=config["repository_id"],
            token_provider=lambda: runtime.github_api_token,
            transport=github_transport,
        )
        gateway = CiGatewayClient(
            config["gateway_origin"],
            token_provider=lambda: runtime.gateway_token,
            transport=gateway_transport,
        )
        runner = DockerSandboxRunner(sandbox, executor=process_executor)
        git_executable = shutil.which("git")
        if git_executable is None:
            raise ValueError
        git_fetcher = GitObjectFetcher(
            repository=Path(config["object_repository"]),
            owner=owner,
            name=repository,
            token=runtime.github_api_token,
            git_executable=git_executable,
            executor=git_fetch_executor or SubprocessGitExecutor(),
        )
        return CiPhaseCoordinator(
            repository=config["object_repository"],
            policy=policy,
            github=github,
            gateway=gateway,
            sandbox=sandbox,
            baseline_root=config["baseline_root"],
            baseline_manifest=manifest,
            baseline_manifest_bytes=worker_raw,
            baseline_worker_source=worker_source,
            runner=runner,
            report_scanner=scanner,
            state_directory=config["state_directory"],
            artifact_directory=config["artifact_directory"],
            expected_repository=config["repository"],
            repository_id=config["repository_id"],
            owner_id=config["owner_id"],
            scanner_identity_sha256=scanner.identity_sha256,
            git_fetcher=git_fetcher,
        )
    except CoordinatorError:
        raise
    except Exception:
        raise CoordinatorError("invalid_runtime_configuration") from None


def ci_context_digest(context: Mapping[str, Any]) -> str:
    if not isinstance(context, Mapping):
        raise CoordinatorError("ci_context_mismatch")
    value = dict(context)
    value.pop("context_sha256", None)
    try:
        return hashlib.sha256(_canonical(value)).hexdigest()
    except CoordinatorError:
        raise CoordinatorError("ci_context_mismatch") from None


def _read_ci_overlay(
    repository: Path,
    head_commit: str,
    changes: tuple[Change, ...],
    policy: PolicyManifest,
) -> dict[str, str]:
    if not changes or len(changes) > MAX_CANDIDATE_FILES:
        raise CoordinatorError("ci_patch_unrepresentable")
    try:
        policy.validate_changes(changes)
        if any(change.change_type == "deleted" for change in changes):
            raise CoordinatorError("ci_patch_unrepresentable")
        if any(change.change_type not in {"added", "modified"} for change in changes):
            raise CoordinatorError("ci_patch_unrepresentable")
        git = shutil.which("git")
        if git is None:
            raise GitEvidenceError("invalid_repository")
        with tempfile.TemporaryDirectory(prefix="release-ci-overlay-") as home:
            reader = _GitRunner(git, repository, Path(home), len(head_commit))
            listing = reader.run("ls-tree", "-r", "-z", "--full-tree", head_commit)
            if listing and not listing.endswith(b"\0"):
                raise GitEvidenceError("invalid_git_output")
            wanted = {change.path for change in changes}
            found: dict[str, tuple[bytes, bytes]] = {}
            for record in listing.split(b"\0")[:-1]:
                header, separator, raw_path = record.partition(b"\t")
                fields = header.split(b" ")
                if not separator or len(fields) != 3:
                    raise GitEvidenceError("invalid_git_output")
                mode, kind, oid = fields
                if raw_path.decode("utf-8", errors="strict") not in wanted:
                    continue
                if (
                    mode not in {b"100644", b"100755"}
                    or kind != b"blob"
                    or re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) is None
                ):
                    raise CoordinatorError("ci_patch_unrepresentable")
                path = raw_path.decode("utf-8", errors="strict")
                if path in found:
                    raise GitEvidenceError("invalid_git_output")
                found[path] = (mode, oid)
            if set(found) != wanted:
                raise CoordinatorError("ci_patch_unrepresentable")
            result: dict[str, str] = {}
            total = 0
            for path in sorted(wanted):
                mode, oid = found[path]
                data = reader.run(
                    "cat-file", "blob", oid.decode("ascii"), output_limit=MAX_FILE_BYTES
                )
                text_value = data.decode("utf-8", errors="strict")
                if not data or b"\0" in data or len(data) > MAX_FILE_BYTES or "\x00" in text_value:
                    raise CoordinatorError("ci_patch_unrepresentable")
                total += len(data)
                if total > MAX_PATCH_BYTES:
                    raise CoordinatorError("ci_patch_unrepresentable")
                result[path] = text_value
            return result
    except CoordinatorError:
        raise
    except (GitEvidenceError, PolicyError, OSError, UnicodeError, ValueError):
        raise CoordinatorError("ci_patch_unrepresentable") from None


def _validate_ci_config_values(value: object) -> None:
    required = {
        "version",
        "gateway_origin",
        "repository",
        "repository_id",
        "owner_id",
        "object_repository",
        "policy_path",
        "policy_sha256",
        "worker_manifest_path",
        "worker_manifest_sha256",
        "baseline_root",
        "docker_path",
        "runtime_name",
        "runtime_path",
        "runtime_sha256",
        "image_reference",
        "resources",
        "scanner_executable_path",
        "scanner_executable_sha256",
        "scanner_configuration_path",
        "scanner_configuration_sha256",
        "publication_scanner_identity_sha256",
        "state_directory",
        "artifact_directory",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("version") != 1:
        raise ValueError
    if (
        not isinstance(value.get("gateway_origin"), str)
        or not isinstance(value.get("repository"), str)
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", value["repository"]) is None
        or any(
            type(value.get(name)) is not int or value[name] <= 0
            for name in ("repository_id", "owner_id")
        )
        or value.get("runtime_name") != "runsc"
        or not isinstance(value.get("resources"), dict)
        or set(value["resources"])
        != {
            "cpu_millis",
            "memory_bytes",
            "pids_limit",
            "timeout_seconds",
            "max_output_bytes",
            "workspace_bytes",
        }
    ):
        raise ValueError
    origin = urlsplit(value["gateway_origin"])
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username is not None
        or origin.password is not None
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
    ):
        raise ValueError
    if any(
        not isinstance(value.get(name), str) or not Path(value[name]).is_absolute()
        for name in (
            "object_repository",
            "policy_path",
            "worker_manifest_path",
            "baseline_root",
            "docker_path",
            "runtime_path",
            "scanner_executable_path",
            "scanner_configuration_path",
            "state_directory",
            "artifact_directory",
        )
    ):
        raise ValueError
    for name in (
        "policy_sha256",
        "worker_manifest_sha256",
        "runtime_sha256",
        "scanner_executable_sha256",
        "scanner_configuration_sha256",
        "publication_scanner_identity_sha256",
    ):
        if (
            not isinstance(value.get(name), str)
            or re.fullmatch(r"[0-9a-f]{64}", value[name]) is None
        ):
            raise ValueError
    # Instantiate validated value types before any external process is created.
    ResourcePolicy(**value["resources"])
    SandboxConfig(
        value["docker_path"],
        value["runtime_name"],
        value["runtime_path"],
        value["runtime_sha256"],
        value["image_reference"],
        value["worker_manifest_sha256"],
        "0" * 64,
        "0" * 40,
        ResourcePolicy(**value["resources"]),
    )


def _read_protected_file(path: Path, *, max_bytes: int) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute() or type(max_bytes) is not int:
        raise ValueError
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_size > max_bytes
        ):
            raise ValueError
        content = bytearray()
        while chunk := os.read(descriptor, min(65536, max_bytes + 1 - len(content))):
            content.extend(chunk)
            if len(content) > max_bytes:
                raise ValueError
        return bytes(content)
    finally:
        os.close(descriptor)


def _prepare_private_directory(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ValueError


def _write_private_immutable(path: Path, content: bytes) -> None:
    if not isinstance(content, bytes) or len(content) > 4 * 1024 * 1024:
        raise CoordinatorError("ci_state_invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = _read_protected_file(path, max_bytes=4 * 1024 * 1024)
        if existing != content:
            raise CoordinatorError("ci_state_invalid") from None
        return
    except OSError:
        raise CoordinatorError("ci_state_invalid") from None
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise CoordinatorError("ci_state_invalid")
            view = view[written:]
        os.fsync(descriptor)
    except OSError:
        raise CoordinatorError("ci_state_invalid") from None
    finally:
        os.close(descriptor)


def _read_private_json(path: Path, *, max_bytes: int) -> dict[str, Any]:
    try:
        raw = _read_protected_file(path, max_bytes=max_bytes)
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
        if not isinstance(value, dict) or _canonical(value) != raw:
            raise ValueError
        return value
    except CoordinatorError:
        raise
    except Exception:
        raise CoordinatorError("ci_state_invalid") from None


def _verify_installed_baseline(root: Path, manifest_bytes: bytes) -> None:
    try:
        value = json.loads(manifest_bytes, object_pairs_hook=_unique_pairs)
        snapshot = SnapshotManifest.from_value(value["baseline_snapshot"])
        expected = {item.path: item for item in snapshot.files}
        observed: set[str] = set()
        for relative, entry in expected.items():
            current = root
            parts = relative.split("/")
            for part in parts[:-1]:
                current = current / part
                if not stat.S_ISDIR(current.lstat().st_mode):
                    raise ValueError
            current = current / parts[-1]
            info = current.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != entry.mode:
                raise ValueError
            data = _read_protected_file(current, max_bytes=entry.size)
            if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise ValueError
            observed.add(relative)
        walked: set[str] = set()
        for directory, dirs, files in os.walk(root, followlinks=False):
            base = Path(directory)
            for name in dirs:
                if stat.S_ISLNK((base / name).lstat().st_mode):
                    raise ValueError
            for name in files:
                path = base / name
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError
                walked.add(path.relative_to(root).as_posix())
        if walked != observed:
            raise ValueError
    except Exception:
        raise CoordinatorError("invalid_runtime_configuration") from None


def parse_command(argv: list[str] | None = None) -> tuple[str, UUID | int]:
    parser = _StrictArgumentParser(prog="chatcore-release-coordinator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("checkconfig")
    merge = subparsers.add_parser("merge-approved-release")
    merge.add_argument("--approval-id", required=True)
    for name in ("inspect-pull-request", "run-isolated-candidate", "scan-and-publish-report"):
        ci = subparsers.add_parser(name)
        ci.add_argument("--pull-request-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "checkconfig":
        return args.command, 0
    if args.command == "merge-approved-release":
        try:
            approval_id = UUID(args.approval_id)
            if str(approval_id) != args.approval_id.lower():
                raise ValueError
        except (ValueError, AttributeError):
            raise CoordinatorError("invalid_input") from None
        return args.command, approval_id
    try:
        pull_request_id = int(args.pull_request_id)
        if not 1 <= pull_request_id <= 2**31 - 1:
            raise ValueError
    except (ValueError, AttributeError):
        raise CoordinatorError("invalid_input") from None
    return args.command, pull_request_id


def main(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Run the configured merge or fixed hosted CI phase coordinator."""
    try:
        command, value = parse_command(argv)
        if command == "checkconfig":
            ProtectedRuntimeConfig.from_environment(environment)
            return 0
        if command == "merge-approved-release" and isinstance(value, UUID):
            outcome = execute_merge(value, environment=environment)
            print(
                json.dumps(
                    {"outcome": outcome.outcome, "merge_commit": outcome.merge_commit},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0 if outcome.outcome in {"merged_verified", "healthy", "restored_healthy"} else 1
        if isinstance(value, int) and command in {
            "inspect-pull-request",
            "run-isolated-candidate",
            "scan-and-publish-report",
        }:
            coordinator = build_ci_coordinator(environment)
            try:
                operation = {
                    "inspect-pull-request": coordinator.prepare,
                    "run-isolated-candidate": coordinator.run_candidate,
                    "scan-and-publish-report": coordinator.scan_and_publish_report,
                }[command]
                result = operation(value)
                print(_canonical(result).decode("utf-8"))
                return 0
            finally:
                coordinator.close()
    except CoordinatorError as exc:
        raise SystemExit(exc.code) from None
    except (
        GitHubEvidenceError,
        GitEvidenceError,
        MergeControlError,
        OIDCError,
        PolicyError,
    ) as exc:
        raise SystemExit(str(exc)) from None
    raise SystemExit(CoordinatorError("runtime_not_configured").code)


def _nested_id(value: object, *path: str) -> int | None:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if type(current) is int and current > 0 else None


def _validate_pr_number(value: object) -> None:
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        raise CoordinatorError("invalid_input")


def _env_int(environment: Mapping[str, str], name: str) -> int:
    value = environment[name]
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,18}", value) is None:
        raise CoordinatorError("invalid_runtime_configuration")
    return int(value)


def _invalid_closure_path(value: object) -> bool:
    if not isinstance(value, str) or _is_private_path(value):
        return True
    try:
        Change(value)
    except PolicyError:
        return True
    return False


def _workflow_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.split("@", 1)[0]
    return path if path.startswith(".github/workflows/") else None


def _job_is_live_or_successful(job: Mapping[str, Any]) -> bool:
    return (job.get("status") == "in_progress" and job.get("conclusion") is None) or (
        job.get("status") == "completed" and job.get("conclusion") == "success"
    )


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def _valid_merge_observation(commit: object, tree: object, parents: object) -> bool:
    if commit is None:
        return tree is None and parents is None
    return (
        isinstance(commit, str)
        and _SHA.fullmatch(commit) is not None
        and isinstance(tree, str)
        and _SHA.fullmatch(tree) is not None
        and isinstance(parents, list)
        and len(parents) <= _MAX_OBSERVED_PARENTS
        and all(isinstance(parent, str) and _SHA.fullmatch(parent) for parent in parents)
    )


def _valid_host_receipt(value: object, dispatch_fence: int) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {
        "operation_id",
        "receipt_id",
        "status",
        "fence",
    }:
        return False
    return (
        _is_uuid(value.get("operation_id"))
        and isinstance(value.get("receipt_id"), str)
        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value["receipt_id"]) is not None
        and value.get("status") in {"healthy", "restored_healthy", "recovery_failed"}
        and type(value.get("fence")) is int
        and value["fence"] == dispatch_fence
    )


def _valid_utc_timestamp(value: object, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    if not isinstance(value, str):
        return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == dt.timedelta(0)


def _valid_merge_lease(value: object, dispatch_fence: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {
        "lease_id",
        "fence",
        "expires_at",
        "admitted_at",
        "run_id",
        "run_attempt",
        "check_run_id",
        "may_start",
    }:
        return False
    return (
        _is_uuid(value.get("lease_id"))
        and type(value.get("fence")) is int
        and value.get("fence") == dispatch_fence
        and _valid_utc_timestamp(value.get("expires_at"))
        and _valid_utc_timestamp(value.get("admitted_at"), nullable=True)
        and all(
            type(value.get(name)) is int and value[name] > 0
            for name in ("run_id", "run_attempt", "check_run_id")
        )
        and type(value.get("may_start")) is bool
    )


def recorded_release_outcome(
    approval_id: UUID, context: Mapping[str, Any]
) -> RecordedReleaseOutcome:
    """Project a validated immutable context without consulting or changing GitHub."""
    if not isinstance(approval_id, UUID) or not isinstance(context, Mapping):
        raise CoordinatorError("invalid_input")
    parents = context["merge_parents"]
    host_receipt = context["host_receipt"]
    return RecordedReleaseOutcome(
        status=context["status"],
        approval_id=approval_id,
        merge_commit=context["merge_commit"],
        merge_tree=context["merge_tree"],
        merge_parents=tuple(parents) if parents is not None else None,
        host_receipt=(
            (
                host_receipt["operation_id"],
                host_receipt["receipt_id"],
                host_receipt["status"],
                host_receipt["fence"],
            )
            if host_receipt is not None
            else None
        ),
    )


class _StrictArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise CoordinatorError("invalid_input")


class OIDCCallerVerifier:
    """Join token signature, installed closure and authenticated GitHub job proof."""

    def __init__(
        self,
        *,
        approval_id: UUID,
        token_provider: Callable[[], str],
        jwks_provider: Callable[[], Mapping[str, object]],
        policy_provider: Callable[[UUID], OIDCPolicy],
        workflow_verifier: Callable[[CallerIdentity], bool],
        job_verifier: Callable[[CallerIdentity], bool],
    ) -> None:
        if not isinstance(approval_id, UUID) or any(
            not callable(callback)
            for callback in (
                token_provider,
                jwks_provider,
                policy_provider,
                workflow_verifier,
                job_verifier,
            )
        ):
            raise CoordinatorError("invalid_input")
        self._approval_id = approval_id
        self._token_provider = token_provider
        self._jwks_provider = jwks_provider
        self._policy_provider = policy_provider
        self._workflow_verifier = workflow_verifier
        self._job_verifier = job_verifier

    def __call__(self, identity: CallerIdentity) -> bool:
        if not isinstance(identity, CallerIdentity):
            return False
        try:
            token = self._token_provider()
            policy = self._policy_provider(self._approval_id)
            if not isinstance(policy, OIDCPolicy):
                return False
            verified = verify_oidc(
                token,
                self._jwks_provider(),
                policy,
                verify_workflow=self._workflow_verifier,
                verify_job=self._job_verifier,
            )
            return verified == identity
        except Exception:
            return False


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CoordinatorError("broker_invalid_response")
        result[key] = value
    return result


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise CoordinatorError("invalid_input") from None


if __name__ == "__main__":
    raise SystemExit(main())
