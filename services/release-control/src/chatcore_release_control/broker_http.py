"""Protected bounded ASGI/UDS runtime for the release broker.

The ASGI app never runs SQL migrations. GitHub job tokens and host-service credentials
are separate principals. All external evidence is fetched from fixed configured peers.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from .broker import (
    BrokerError,
    BrokerStore,
    VerifiedAdmissionEvidence,
    VerifiedMergeObservation,
)
from .broker_app import BrokerAppHTTPPost, BrokerAppTokenIssuer
from .database_runtime import connection_initializer, verify_database_pool
from .git_evidence import GitEvidenceError, collect_release_bundle, verify_installed_blob_digests
from .github_evidence import (
    GitHubEvidenceClient,
    GitHubEvidenceError,
    GitHubEvidenceResolver,
    evidence_digest,
)
from .ledger import HostOperationBinding, HostReceipt, LedgerError, ReleaseStatus, RunAttemptBinding
from .oidc import (
    CallerIdentity,
    OIDCError,
    OIDCPolicy,
    fetch_github_jwks,
    github_main_subject,
    verify_oidc,
)
from .policy import (
    DeploymentIdentity,
    PolicyError,
    PolicyManifest,
    _is_private_path,
    evaluate_reviewed_release,
)
from .release_coordinator import GitHubJobCorrelationVerifier

_MAX_CONFIG_BYTES = 128 * 1024
_MAX_SECRET_BYTES = 16 * 1024
_MAX_BODY_BYTES = 64 * 1024
_MAX_HEADER_BYTES = 16 * 1024
_REQUEST_TIMEOUT_SECONDS = 60.0
_HOST_OPERATION_TIMEOUT_SECONDS = 900.0
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TARGET = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


class BrokerHttpError(RuntimeError):
    _CODES = frozenset(
        {
            "invalid_request",
            "caller_denied",
            "host_denied",
            "not_found",
            "method_not_allowed",
            "body_too_large",
            "request_timeout",
            "broker_unavailable",
            "runtime_not_configured",
        }
    )

    def __init__(self, code: str = "broker_unavailable") -> None:
        self.code = code if code in self._CODES else "broker_unavailable"
        super().__init__(self.code)


def _no_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _parse_json(raw: bytes) -> object:
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"), object_pairs_hook=_no_duplicate_pairs
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise BrokerHttpError("invalid_request") from None


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError, RecursionError):
        raise BrokerHttpError("invalid_request") from None


def _is_uuid(value: object) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _decode_identity(value: object) -> DeploymentIdentity:
    fields = {
        "git_sha",
        "image_digest",
        "config_generation",
        "config_digest",
        "policy_digest",
        "target_sequence",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise BrokerHttpError("invalid_request")
    try:
        identity = DeploymentIdentity(**value)
    except Exception:
        raise BrokerHttpError("invalid_request") from None
    return identity


def _safe_file(path: Path, *, maximum: int, exact_mode: int = 0o600) -> bytes:
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != exact_mode
            or info.st_size > maximum
        ):
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_ino != info.st_ino
                or opened.st_dev != info.st_dev
            ):
                raise ValueError
            data = bytearray()
            while chunk := os.read(descriptor, min(8192, maximum + 1 - len(data))):
                data.extend(chunk)
                if len(data) > maximum:
                    raise ValueError
        finally:
            os.close(descriptor)
        return bytes(data)
    except Exception:
        raise BrokerHttpError("runtime_not_configured") from None


def _secret(path: Path) -> str:
    raw = _safe_file(path, maximum=_MAX_SECRET_BYTES)
    try:
        value = raw.decode("utf-8", errors="strict").rstrip("\n")
        if not value or "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError
        return value
    except (UnicodeError, ValueError):
        raise BrokerHttpError("runtime_not_configured") from None


@dataclass(frozen=True, repr=False)
class BrokerRuntimeConfig:
    config_path: Path
    database_dsn_file: Path
    github_app_roles: tuple[tuple[str, int], ...]
    github_installation_id: int
    github_repository_id: int
    github_private_key_file: Path
    host_inbound_credential_file: Path
    host_client_credential_file: Path
    host_socket: Path
    object_repository: Path
    workflow_path: str
    merge_runner_environment: str
    relay_runner_environment: str
    audience: str
    targets: tuple[str, ...]
    workflow_blobs: tuple[tuple[str, str], ...]
    uds_path: Path
    oidc_subject_format: str = "legacy"

    def __post_init__(self) -> None:
        expected = {"publication", "broker", "runner_registration"}
        try:
            roles = dict(self.github_app_roles)
        except Exception:
            raise BrokerHttpError("runtime_not_configured") from None
        if (
            not isinstance(self.github_app_roles, tuple)
            or len(roles) != len(self.github_app_roles)
            or set(roles) != expected
            or any(type(app_id) is not int or app_id <= 0 for app_id in roles.values())
            or len(set(roles.values())) != len(roles)
            or type(self.github_installation_id) is not int
            or self.github_installation_id <= 0
            or self.oidc_subject_format not in {"legacy", "immutable"}
        ):
            raise BrokerHttpError("runtime_not_configured")

    @property
    def github_app_id(self) -> int:
        return dict(self.github_app_roles)["broker"]

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> BrokerRuntimeConfig:
        try:
            config_path = Path(path)
            raw = _safe_file(config_path, maximum=_MAX_CONFIG_BYTES)
            value = json.loads(
                raw.decode("utf-8", errors="strict"), object_pairs_hook=_no_duplicate_pairs
            )
            expected = {
                "version",
                "database_dsn_file",
                "github_app_roles",
                "github_installation_id",
                "github_repository_id",
                "github_private_key_file",
                "host_inbound_credential_file",
                "host_client_credential_file",
                "host_socket",
                "object_repository",
                "workflow_path",
                "merge_runner_environment",
                "relay_runner_environment",
                "oidc_subject_format",
                "audience",
                "targets",
                "workflow_blobs",
                "uds_path",
            }
            if not isinstance(value, dict) or set(value) != expected or value["version"] != 1:
                raise ValueError
            paths = {
                name: Path(value[name])
                for name in (
                    "database_dsn_file",
                    "github_private_key_file",
                    "host_inbound_credential_file",
                    "host_client_credential_file",
                    "host_socket",
                    "object_repository",
                    "uds_path",
                )
            }
            if (
                any(not p.is_absolute() for p in paths.values())
                or type(value["github_installation_id"]) is not int
                or value["github_installation_id"] <= 0
                or type(value["github_repository_id"]) is not int
                or value["github_repository_id"] <= 0
                or not isinstance(value["github_app_roles"], dict)
                or set(value["github_app_roles"])
                != {"publication", "broker", "runner_registration"}
                or any(
                    type(app_id) is not int or app_id <= 0
                    for app_id in value["github_app_roles"].values()
                )
                or len(set(value["github_app_roles"].values())) != 3
                or not paths["object_repository"].is_dir()
                or not isinstance(value["workflow_path"], str)
                or re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", value["workflow_path"])
                is None
                or value["merge_runner_environment"] != "github-hosted"
                or value["relay_runner_environment"] != "self-hosted"
                or not isinstance(value["audience"], str)
                or not 1 <= len(value["audience"]) <= 512
                or not isinstance(value["targets"], list)
                or not 1 <= len(value["targets"]) <= 32
                or any(
                    not isinstance(target, str) or not _TARGET.fullmatch(target)
                    for target in value["targets"]
                )
                or value["targets"] != sorted(set(value["targets"]))
                or not isinstance(value["workflow_blobs"], dict)
                or not 1 <= len(value["workflow_blobs"]) <= 2048
                or value["workflow_path"] not in value["workflow_blobs"]
                or any(
                    not isinstance(name, str)
                    or name.startswith("/")
                    or ".." in name.split("/")
                    or _is_private_path(name)
                    or not isinstance(digest, str)
                    or not _SHA256.fullmatch(digest)
                    for name, digest in value["workflow_blobs"].items()
                )
            ):
                raise ValueError
            result = cls(
                config_path=config_path,
                database_dsn_file=paths["database_dsn_file"],
                github_app_roles=tuple(sorted(value["github_app_roles"].items())),
                github_installation_id=value["github_installation_id"],
                github_repository_id=value["github_repository_id"],
                github_private_key_file=paths["github_private_key_file"],
                host_inbound_credential_file=paths["host_inbound_credential_file"],
                host_client_credential_file=paths["host_client_credential_file"],
                host_socket=paths["host_socket"],
                object_repository=paths["object_repository"],
                workflow_path=value["workflow_path"],
                merge_runner_environment=value["merge_runner_environment"],
                relay_runner_environment=value["relay_runner_environment"],
                oidc_subject_format=value["oidc_subject_format"],
                audience=value["audience"],
                targets=tuple(value["targets"]),
                workflow_blobs=tuple(sorted(value["workflow_blobs"].items())),
                uds_path=paths["uds_path"],
            )
            for secret_path in (
                result.database_dsn_file,
                result.github_private_key_file,
                result.host_inbound_credential_file,
                result.host_client_credential_file,
            ):
                _safe_file(secret_path, maximum=_MAX_SECRET_BYTES)
            _validate_socket_parent(result.uds_path, allow_existing_socket=False)
            _validate_socket_parent(result.host_socket, allow_existing_socket=True)
            return result
        except BrokerHttpError:
            raise
        except Exception:
            raise BrokerHttpError("runtime_not_configured") from None

    def __repr__(self) -> str:
        return "BrokerRuntimeConfig([protected])"


def _validate_socket_parent(path: Path, *, allow_existing_socket: bool) -> None:
    try:
        if not path.is_absolute() or not path.parent.is_dir() or path.parent.is_symlink():
            raise ValueError
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o022:
            raise ValueError
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if (
                not allow_existing_socket
                or not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.geteuid()
            ):
                raise ValueError
    except Exception:
        raise BrokerHttpError("runtime_not_configured") from None


class OIDCJobAuthenticator:
    """Verify each protected GitHub job against fresh JWKS, closure blobs, and API job."""

    def __init__(
        self,
        *,
        audience: str,
        workflow_path: str,
        merge_runner_environment: str,
        relay_runner_environment: str,
        object_repository: Path,
        workflow_blobs: Mapping[str, str],
        dispatch_id_provider: Any,
        github_token_provider: Any,
        github_transport: httpx.BaseTransport | None = None,
        jwks_transport: httpx.AsyncBaseTransport | None = None,
        subject_format: str = "legacy",
    ) -> None:
        if (
            not isinstance(audience, str)
            or not 1 <= len(audience) <= 512
            or not isinstance(workflow_path, str)
            or re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", workflow_path) is None
            or merge_runner_environment != "github-hosted"
            or relay_runner_environment != "self-hosted"
            or not isinstance(object_repository, Path)
            or not object_repository.is_dir()
            or not isinstance(workflow_blobs, Mapping)
            or workflow_path not in workflow_blobs
            or not callable(dispatch_id_provider)
            or not callable(github_token_provider)
            or subject_format not in {"legacy", "immutable"}
        ):
            raise BrokerHttpError("runtime_not_configured")
        self._audience = audience
        self._workflow_path = workflow_path
        self._runner_environments = {
            "merge": merge_runner_environment,
            "relay": relay_runner_environment,
        }
        self._object_repository = object_repository
        self._workflow_blobs = dict(workflow_blobs)
        self._dispatch_id_provider = dispatch_id_provider
        self._github_token_provider = github_token_provider
        self._github_transport = github_transport
        self._jwks_transport = jwks_transport
        self._subject_format = subject_format

    async def verify(self, token: str, context: Any, *, phase: str = "merge") -> CallerIdentity:
        runner_environment = self._runner_environments.get(phase)
        if runner_environment is None:
            raise BrokerHttpError("caller_denied")
        claims = _unverified_claims(token)
        try:
            numbers = tuple(
                _claim_positive(claims, key) for key in ("run_id", "run_attempt", "check_run_id")
            )
            if (
                claims.get("workflow_sha") != context.workflow_sha
                or claims.get("repository") != context.repository
                or claims.get("repository_id") != str(context.repository_id)
                or claims.get("repository_owner_id") != str(context.owner_id)
            ):
                raise OIDCError()
            owner, repository = context.repository.split("/", 1)
            dispatch_id = await self._dispatch_id_provider(
                context.approval_id, context.dispatch_fence
            )
            if not isinstance(dispatch_id, UUID):
                raise OIDCError()
            policy = OIDCPolicy(
                audience=self._audience,
                repository_id=context.repository_id,
                owner_id=context.owner_id,
                repository=context.repository,
                workflow_path=self._workflow_path,
                workflow_sha=context.workflow_sha,
                run_id=numbers[0],
                run_attempt=numbers[1],
                check_run_id=numbers[2],
                runner_environment=runner_environment,
                subject=github_main_subject(
                    context.repository,
                    context.owner_id,
                    context.repository_id,
                    self._subject_format,
                ),
            )
            jwks = await fetch_github_jwks(transport=self._jwks_transport)
            github = GitHubEvidenceClient(
                owner=owner,
                repository=repository,
                repository_id=context.repository_id,
                token_provider=self._github_token_provider,
                transport=self._github_transport,
            )
            try:
                jobs = GitHubJobCorrelationVerifier(
                    github,
                    workflow_path=self._workflow_path,
                    runner_environment=runner_environment,
                    expected_run_title=f"release:{dispatch_id}",
                )
                return await asyncio.to_thread(
                    verify_oidc,
                    token,
                    jwks,
                    policy,
                    verify_workflow=lambda identity: (
                        verify_installed_blob_digests(
                            self._object_repository, identity.workflow_sha, self._workflow_blobs
                        )
                        or True
                    ),
                    verify_job=jobs,
                )
            finally:
                github.close()
        except (BrokerHttpError, OIDCError):
            raise BrokerHttpError("caller_denied") from None
        except Exception:
            raise BrokerHttpError("caller_denied") from None


def _unverified_claims(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or not 1 <= len(token) <= 16384:
        raise BrokerHttpError("caller_denied")
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError

        def parse_segment(segment: str) -> Any:
            if not segment or re.fullmatch(r"[A-Za-z0-9_-]+", segment) is None:
                raise ValueError
            raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
            return json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_no_duplicate_pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )

        header = parse_segment(parts[0])
        value = parse_segment(parts[1])
        if not isinstance(header, dict) or not isinstance(value, dict):
            raise ValueError
        return value
    except Exception:
        raise BrokerHttpError("caller_denied") from None


def _claim_positive(claims: Mapping[str, object], key: str) -> int:
    value = claims.get(key)
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,18}", value) is None:
        raise BrokerHttpError("caller_denied")
    return int(value)


@dataclass(frozen=True)
class HostObservation:
    identity: DeploymentIdentity
    healthy: bool
    policy_digest: str
    target_sequence: int


class ProtectedHostClient:
    """Fixed private-UDS host read client; no request-controlled endpoint or credential."""

    def __init__(
        self,
        *,
        socket_path: Path,
        credential: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not socket_path.is_absolute() or not credential:
            raise BrokerHttpError("runtime_not_configured")
        self._socket_path = socket_path
        self._credential = credential
        self._transport = transport

    async def current_state(self, target: str) -> HostObservation:
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            raise BrokerHttpError("invalid_request")
        transport = self._transport or httpx.AsyncHTTPTransport(uds=str(self._socket_path))
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://protected-host",
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(5.0, connect=2.0),
                headers={"Authorization": f"Bearer {self._credential}"},
            ) as client:
                async with asyncio.timeout(5.0):
                    async with client.stream(
                        "GET", f"/v1/host/targets/{target}/current-state"
                    ) as response:
                        if response.status_code != 200:
                            raise BrokerHttpError("broker_unavailable")
                        body = bytearray()
                        async for chunk in response.aiter_bytes(4096):
                            if len(body) + len(chunk) > 16 * 1024:
                                raise BrokerHttpError("broker_unavailable")
                            body.extend(chunk)
                    if len(body) > 16 * 1024:
                        raise BrokerHttpError("broker_unavailable")
                    value = _parse_json(bytes(body))
            if not isinstance(value, dict) or set(value) != {
                "deployed_identity",
                "healthy",
                "policy_digest",
                "target_sequence",
            }:
                raise BrokerHttpError("broker_unavailable")
            identity = _decode_identity(value["deployed_identity"])
            if (
                type(value["healthy"]) is not bool
                or value["healthy"] is not True
                or value["policy_digest"] != identity.policy_digest
                or type(value["target_sequence"]) is not int
                or value["target_sequence"] != identity.target_sequence
            ):
                raise BrokerHttpError("broker_unavailable")
            return HostObservation(identity, True, identity.policy_digest, identity.target_sequence)
        except BrokerHttpError:
            raise
        except Exception:
            raise BrokerHttpError("broker_unavailable") from None

    async def operate(
        self, action: str, approval_id: UUID, operation_id: UUID
    ) -> dict[str, object]:
        if (
            action not in {"deploy", "reconcile"}
            or not isinstance(approval_id, UUID)
            or not isinstance(operation_id, UUID)
        ):
            raise BrokerHttpError("invalid_request")
        transport = self._transport or httpx.AsyncHTTPTransport(uds=str(self._socket_path))
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://protected-host",
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(_HOST_OPERATION_TIMEOUT_SECONDS, connect=2.0),
                headers={"Authorization": f"Bearer {self._credential}"},
            ) as client:
                async with asyncio.timeout(_HOST_OPERATION_TIMEOUT_SECONDS):
                    async with client.stream(
                        "POST",
                        f"/v1/{action}",
                        json={"approval_id": str(approval_id), "operation_id": str(operation_id)},
                        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                    ) as response:
                        if response.status_code != 200:
                            raise BrokerHttpError("broker_unavailable")
                        body = bytearray()
                        async for chunk in response.aiter_bytes(4096):
                            if len(body) + len(chunk) > 16 * 1024:
                                raise BrokerHttpError("broker_unavailable")
                            body.extend(chunk)
                    value = _parse_json(bytes(body))
            if not isinstance(value, dict) or set(value) != {
                "operation_id",
                "phase",
                "receipt",
                "payload_digest",
            }:
                raise BrokerHttpError("broker_unavailable")
            phases = {
                "prepared",
                "switching",
                "checking",
                "healthy",
                "restoring",
                "restored_healthy",
                "recovery_failed",
            }
            if (
                value["operation_id"] != str(operation_id)
                or value["phase"] not in phases
                or (
                    value["receipt"] is not None
                    and (
                        not isinstance(value["receipt"], str)
                        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value["receipt"]) is None
                    )
                )
                or not isinstance(value["payload_digest"], str)
                or not _SHA256.fullmatch(value["payload_digest"])
            ):
                raise BrokerHttpError("broker_unavailable")
            return value
        except BrokerHttpError:
            raise
        except Exception:
            raise BrokerHttpError("broker_unavailable") from None


class ReleaseEvidenceVerifier:
    """Recompute P01 D/B/H facts and fresh P04 provenance from configured sources."""

    def __init__(
        self,
        *,
        object_repository: Path,
        workflow_blobs: Mapping[str, str],
        github_transport: httpx.BaseTransport | None = None,
        github_token_provider: Any,
    ) -> None:
        self._object_repository = object_repository
        self._workflow_blobs = dict(workflow_blobs)
        self._github_transport = github_transport
        if not callable(github_token_provider):
            raise BrokerHttpError("runtime_not_configured")
        self._github_token_provider = github_token_provider

    def verify(self, context: Any) -> VerifiedAdmissionEvidence:
        try:
            policy = context.policy_manifest
            if not isinstance(policy, PolicyManifest) or policy.digest != context.policy_digest:
                raise BrokerHttpError("caller_denied")
            owner, repository = context.repository.split("/", 1)
            client = GitHubEvidenceClient(
                owner=owner,
                repository=repository,
                repository_id=context.repository_id,
                token_provider=self._github_token_provider,
                transport=self._github_transport,
            )
            try:
                review = context.review_bundle
                if review.get("complete_delta_reviewed") is not True:
                    raise BrokerHttpError("caller_denied")
                bundle = collect_release_bundle(
                    self._object_repository,
                    context.deployed_identity.git_sha,
                    context.base_sha,
                    context.head_sha,
                    policy,
                    same_tree_continuation_recorded=(
                        review.get("same_tree_continuation_recorded") is True
                    ),
                )
                eligibility = evaluate_reviewed_release(policy, bundle, review)
                if not eligibility.eligible:
                    raise BrokerHttpError("caller_denied")
                snapshot = GitHubEvidenceResolver(
                    client,
                    policy,
                    verify_workflow_closure=lambda commit, expected: _verify_closure(
                        self._object_repository, commit, expected, self._workflow_blobs
                    ),
                    expected_base_branch="main",
                ).resolve(context.pr_number, bundle)
                current_main = client.get_json("git/ref/heads/main")
                current_sha = current_main.get("object", {}).get("sha")
                digest = evidence_digest(snapshot)
                if (
                    snapshot.base_commit != context.base_sha
                    or snapshot.head_commit != context.head_sha
                    or snapshot.head_tree != context.head_tree
                    or current_sha != context.base_sha
                    or digest != review.get("check_evidence_digest")
                ):
                    raise BrokerHttpError("caller_denied")
                return VerifiedAdmissionEvidence(
                    deployed_identity=context.deployed_identity,
                    base_sha=snapshot.base_commit,
                    head_sha=snapshot.head_commit,
                    head_tree=snapshot.head_tree,
                    current_main_sha=current_sha,
                    expected_merge_tree=snapshot.synthetic_merge_tree,
                    evidence_digest=digest,
                    complete_delta_reviewed=True,
                )
            finally:
                client.close()
        except BrokerHttpError:
            raise
        except (GitEvidenceError, GitHubEvidenceError, PolicyError, OIDCError):
            raise BrokerHttpError("caller_denied") from None
        except Exception:
            raise BrokerHttpError("caller_denied") from None


def _verify_closure(
    repository: Path,
    commit: str,
    expected_policy_blobs: Mapping[str, str],
    installed_blobs: Mapping[str, str],
) -> bool:
    if any(installed_blobs.get(path) != digest for path, digest in expected_policy_blobs.items()):
        return False
    verify_installed_blob_digests(repository, commit, installed_blobs)
    return True


class BrokerApplication:
    """Exact-path ASGI façade; all authority arrives from mandatory verifier clients."""

    def __init__(
        self,
        *,
        store: BrokerStore,
        config: BrokerRuntimeConfig,
        job_authenticator: OIDCJobAuthenticator,
        host_client: ProtectedHostClient,
        evidence_verifier: ReleaseEvidenceVerifier,
        host_inbound_credential: str,
        github_token_provider: Any,
        github_transport: httpx.BaseTransport | None = None,
        close_resources: tuple[Any, ...] = (),
    ) -> None:
        if (
            not isinstance(store, BrokerStore)
            or not isinstance(config, BrokerRuntimeConfig)
            or not isinstance(job_authenticator, OIDCJobAuthenticator)
            or not isinstance(host_client, ProtectedHostClient)
            or not isinstance(evidence_verifier, ReleaseEvidenceVerifier)
            or not host_inbound_credential
            or not callable(github_token_provider)
        ):
            raise BrokerHttpError("runtime_not_configured")
        self._store = store
        self._config = config
        self._job_authenticator = job_authenticator
        self._host_client = host_client
        self._evidence_verifier = evidence_verifier
        self._host_credential = host_inbound_credential
        self._github_token_provider = github_token_provider
        self._github_transport = github_transport
        self._close_resources = close_resources

    async def close(self) -> None:
        for resource in self._close_resources:
            close = getattr(resource, "close", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope.get("type") != "http":
            await _send_json(send, 404, {"error": "not_found"})
            return
        try:
            path = scope.get("path")
            is_host_operation = (
                isinstance(path, str)
                and re.fullmatch(r"/v1/releases/[0-9a-fA-F-]{36}/host-(?:deploy|reconcile)", path)
                is not None
            )
            timeout_seconds = (
                _HOST_OPERATION_TIMEOUT_SECONDS if is_host_operation else _REQUEST_TIMEOUT_SECONDS
            )
            async with asyncio.timeout(timeout_seconds):
                method = scope.get("method")
                headers = _headers(scope.get("headers", []))
                if scope.get("query_string", b""):
                    raise BrokerHttpError("invalid_request")
                body = await _read_body(receive)
                if body and headers.get("content-type") != "application/json":
                    raise BrokerHttpError("invalid_request")
                content_length = headers.get("content-length")
                if content_length is not None and (
                    not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})", content_length)
                    or int(content_length) != len(body)
                ):
                    raise BrokerHttpError("invalid_request")
                value = _parse_json(body) if body else None
                status, response = await self._route(method, path, headers, value)
                await _send_json(send, status, response)
        except TimeoutError:
            await _send_json(send, 504, {"error": "request_timeout"})
        except BrokerHttpError as exc:
            status = (
                401
                if exc.code == "caller_denied"
                else 403
                if exc.code == "host_denied"
                else 404
                if exc.code == "not_found"
                else 405
                if exc.code == "method_not_allowed"
                else 400
                if exc.code in {"invalid_request", "body_too_large"}
                else 503
            )
            await _send_json(send, status, {"error": exc.code})
        except BrokerError as exc:
            status = (
                409
                if exc.code
                in {
                    "stale_fence",
                    "admission_denied",
                    "binding_mismatch",
                    "lease_expired",
                    "operation_conflict",
                    "reconciliation_required",
                }
                else 503
                if exc.code
                in {
                    "storage_error",
                    "schema_unavailable",
                    "target_unavailable",
                    "host_state_unavailable",
                }
                else 400
            )
            await _send_json(send, status, {"error": exc.code})
        except (
            LedgerError,
            OIDCError,
            GitHubEvidenceError,
            GitEvidenceError,
            PolicyError,
            httpx.HTTPError,
            ValueError,
        ):
            await _send_json(send, 503, {"error": "broker_unavailable"})
        except Exception:
            await _send_json(send, 503, {"error": "broker_unavailable"})

    async def _lifespan(self, receive: Any, send: Any) -> None:
        while True:
            event = await receive()
            if event.get("type") == "lifespan.startup":
                if not await self._store.schema_ready():
                    await send({"type": "lifespan.startup.failed", "message": "schema_unavailable"})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif event.get("type") == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _route(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        value: object,
    ) -> tuple[int, dict[str, Any]]:
        if not isinstance(path, str):
            raise BrokerHttpError("not_found")
        context_match = re.fullmatch(r"/v1/releases/([0-9a-fA-F-]{36})/merge-context", path)
        state_match = re.fullmatch(r"/v1/targets/([A-Za-z0-9._-]{1,64})/current-state", path)
        release_match = re.fullmatch(
            r"/v1/releases/([0-9a-fA-F-]{36})/(merge-lease|merge-admission|merge-result|merge-anomalies)",
            path,
        )
        host_context = re.fullmatch(
            r"/v1/host/releases/([0-9a-fA-F-]{36})/deployment-context", path
        )
        host_action = re.fullmatch(
            r"/v1/host/releases/([0-9a-fA-F-]{36})/(prepare|switch-admission|switch-started|restore-started|receipt)",
            path,
        )
        relay_match = re.fullmatch(r"/v1/releases/([0-9a-fA-F-]{36})/host-(deploy|reconcile)", path)
        if context_match:
            if method != "GET" or value is not None:
                raise BrokerHttpError(
                    "method_not_allowed" if method != "GET" else "invalid_request"
                )
            approval_id = _path_uuid(context_match.group(1))
            context = await self._store.get_release_context(approval_id)
            if context.target not in self._config.targets:
                raise BrokerHttpError("not_found")
            await self._authenticate_job(headers, context)
            return 200, context.to_json_value()
        if state_match:
            if method != "GET" or value is not None:
                raise BrokerHttpError(
                    "method_not_allowed" if method != "GET" else "invalid_request"
                )
            target = state_match.group(1)
            if target not in self._config.targets:
                raise BrokerHttpError("not_found")
            hints = _unverified_claims(_bearer(headers))
            approval_id = await self._store.find_active_release_for_run(
                target,
                run_id=_claim_positive(hints, "run_id"),
                run_attempt=_claim_positive(hints, "run_attempt"),
                check_run_id=_claim_positive(hints, "check_run_id"),
            )
            context = await self._store.get_release_context(approval_id)
            if context.target not in self._config.targets:
                raise BrokerHttpError("not_found")
            identity = await self._authenticate_job(headers, context)
            if identity.run_id != _claim_positive(hints, "run_id"):
                raise BrokerHttpError("caller_denied")
            state = await self._store.get_target_state(target)
            observed = await self._host_client.current_state(target)
            if (
                state.reconciliation_required
                or observed.identity != state.identity
                or state.identity != context.deployed_identity
            ):
                raise BrokerError("reconciliation_required")
            return 200, {
                "deployed_identity": asdict(observed.identity),
                "deployed_commit": observed.identity.git_sha,
                "policy_digest": observed.policy_digest,
            }
        if relay_match:
            if method != "POST":
                raise BrokerHttpError("method_not_allowed")
            data = _object(value, {"operation_id"})
            approval_id = _path_uuid(relay_match.group(1))
            operation_id = _body_uuid(data["operation_id"])
            context = await self._store.get_release_context(approval_id)
            if context.target not in self._config.targets or context.merge_lease is None:
                raise BrokerHttpError("not_found")
            identity = await self._authenticate_job(headers, context, phase="relay")
            lease = context.merge_lease
            if (
                identity.run_id != lease["run_id"]
                or identity.run_attempt != lease["run_attempt"]
                or identity.workflow_sha != context.workflow_sha
                or identity.repository_id != context.repository_id
            ):
                raise BrokerHttpError("caller_denied")
            existing = await self._store.get_switch_operation(approval_id)
            if existing is not None and existing["operation_id"] != str(operation_id):
                raise BrokerError("operation_conflict")
            result = await self._host_client.operate(
                relay_match.group(2), approval_id, operation_id
            )
            recorded = await self._store.get_switch_operation(approval_id)
            if (
                recorded is None
                or recorded["operation_id"] != str(operation_id)
                or recorded["payload_digest"] != result["payload_digest"]
            ):
                raise BrokerError("host_state_unavailable")
            return 200, result
        if release_match:
            approval_id = _path_uuid(release_match.group(1))
            route = release_match.group(2)
            context = await self._store.get_release_context(approval_id)
            if context.target not in self._config.targets:
                raise BrokerHttpError("not_found")
            identity = await self._authenticate_job(headers, context)
            if route == "merge-lease":
                if method == "POST":
                    data = _object(
                        value, {"dispatch_fence", "run_id", "run_attempt", "check_run_id"}
                    )
                    _same_run(data, identity, context.dispatch_fence)
                    run = _run_binding(context, identity, self._config.workflow_path)
                    lease = await self._store.acquire_merge_lease(
                        approval_id, dispatch_fence=context.dispatch_fence, run=run
                    )
                    return 200, {
                        "lease_id": str(lease.lease_id),
                        "dispatch_fence": lease.dispatch_fence,
                        "expires_at": _iso(lease.expires_at),
                    }
                if method == "DELETE":
                    data = _object(value, {"lease_id", "dispatch_fence"})
                    lease_id = _body_uuid(data["lease_id"])
                    fence = _positive(data["dispatch_fence"])
                    if fence != context.dispatch_fence:
                        raise BrokerError("stale_fence")
                    released = await self._store.release_merge_lease(
                        approval_id,
                        dispatch_fence=fence,
                        lease_id=lease_id,
                        run=_run_binding(context, identity, self._config.workflow_path),
                    )
                    return 200, {"released": released}
                raise BrokerHttpError("method_not_allowed")
            if method != "POST":
                raise BrokerHttpError("method_not_allowed")
            if route == "merge-admission":
                data = _object(
                    value,
                    {
                        "head_sha",
                        "dispatch_fence",
                        "run_id",
                        "run_attempt",
                        "check_run_id",
                        "lease_id",
                    },
                )
                _same_run(data, identity, context.dispatch_fence)
                if data["head_sha"] != context.head_sha:
                    raise BrokerError("binding_mismatch")
                lease_id = _body_uuid(data["lease_id"])
                actual = await self._host_client.current_state(context.target)
                state = await self._store.get_target_state(context.target)
                if not actual.healthy or actual.identity != state.identity:
                    raise BrokerError("reconciliation_required")
                evidence = await asyncio.to_thread(self._evidence_verifier.verify, context)
                admission = await self._store.admit_merge(
                    approval_id,
                    dispatch_fence=context.dispatch_fence,
                    lease_id=lease_id,
                    run=_run_binding(context, identity, self._config.workflow_path),
                    evidence=evidence,
                )
                return 200, admission.to_json_value()
            if route in {"merge-result", "merge-anomalies"}:
                expected = (
                    {
                        "merge_commit",
                        "parents",
                        "tree",
                        "expected_tree",
                        "dispatch_fence",
                        "lease_id",
                    }
                    if route == "merge-result"
                    else {
                        "merge_commit",
                        "parents",
                        "tree",
                        "expected_tree",
                        "reason",
                        "dispatch_fence",
                        "lease_id",
                    }
                )
                data = _object(value, expected)
                if route == "merge-anomalies" and data["reason"] != "unexpected_merge_observation":
                    raise BrokerHttpError("invalid_request")
                fence = _positive(data["dispatch_fence"])
                if fence != context.dispatch_fence:
                    raise BrokerError("stale_fence")
                lease_id = _body_uuid(data["lease_id"])
                commit = _sha(data["merge_commit"])
                expected_tree = await self._store.expected_merge_tree(approval_id, lease_id)
                if data["expected_tree"] != expected_tree:
                    raise BrokerError("binding_mismatch")
                actual_observation = await self._observe_merge(context, commit, expected_tree)
                result = await self._store.record_merge_observation(
                    approval_id,
                    dispatch_fence=fence,
                    lease_id=lease_id,
                    run=_run_binding(context, identity, self._config.workflow_path),
                    observation=actual_observation,
                )
                return 200, {"recorded": result["recorded"]}
        if host_context:
            self._authenticate_host(headers)
            if method != "GET" or value is not None:
                raise BrokerHttpError(
                    "method_not_allowed" if method != "GET" else "invalid_request"
                )
            approval_id = _path_uuid(host_context.group(1))
            context = await self._store.get_release_context(approval_id)
            operation = await self._store.get_switch_operation(approval_id)
            return 200, {
                "approval_id": str(context.approval_id),
                "target": context.target,
                "sequence": context.sequence,
                "dispatch_fence": context.dispatch_fence,
                "status": context.status.value,
                "merge_commit": context.merge_commit,
                "base_sha": context.base_sha,
                "head_sha": context.head_sha,
                "head_tree": context.head_tree,
                "expected_merge_tree": context.expected_merge_tree,
                "merge_tree": context.merge_tree,
                "merge_parents": (
                    list(context.merge_parents) if context.merge_parents is not None else None
                ),
                "policy_digest": context.policy_digest,
                "deployed_identity": asdict(context.deployed_identity),
                "host_operation": operation,
            }
        if host_action:
            self._authenticate_host(headers)
            approval_id = _path_uuid(host_action.group(1))
            action = host_action.group(2)
            context = await self._store.get_release_context(approval_id)
            if method != "POST":
                raise BrokerHttpError("method_not_allowed")
            if action == "prepare":
                data = _object(
                    value,
                    {"dispatch_fence", "operation_id", "baseline_identity", "payload_identity"},
                )
                fence = _positive(data["dispatch_fence"])
                operation_id = _body_uuid(data["operation_id"])
                operation = HostOperationBinding(
                    operation_id=operation_id,
                    baseline=_decode_identity(data["baseline_identity"]),
                    payload=_decode_identity(data["payload_identity"]),
                )
                actual = await self._host_client.current_state(context.target)
                if (
                    actual.policy_digest != context.policy_digest
                    or actual.target_sequence != context.deployed_identity.target_sequence
                ):
                    raise BrokerError("host_state_unavailable")
                prepared = await self._store.prepare_switch(
                    approval_id,
                    dispatch_fence=fence,
                    operation=operation,
                    actual_identity=actual.identity,
                )
                return 200, {
                    "operation_id": str(prepared.operation_id),
                    "payload_digest": prepared.payload_digest,
                    "prepared": True,
                }
            if action == "switch-admission":
                data = _object(value, {"dispatch_fence", "operation_id"})
                fence = _positive(data["dispatch_fence"])
                operation_id = _body_uuid(data["operation_id"])
                actual = await self._host_client.current_state(context.target)
                main_sha = await asyncio.to_thread(self._current_main_sha, context)
                result = await self._store.admit_switch(
                    approval_id,
                    dispatch_fence=fence,
                    operation_id=operation_id,
                    current_main_sha=main_sha,
                    actual_identity=actual.identity,
                )
                return 200, result.to_json_value()
            if action in {"switch-started", "restore-started"}:
                data = _object(value, {"dispatch_fence", "operation_id"})
                result = await self._store.start_switch(
                    approval_id,
                    dispatch_fence=_positive(data["dispatch_fence"]),
                    operation_id=_body_uuid(data["operation_id"]),
                    recovery=action == "restore-started",
                )
                return 200, {
                    "operation_id": str(result.operation_id),
                    "started": True,
                    "may_recover": True,
                }
            data = _object(
                value,
                {
                    "dispatch_fence",
                    "operation_id",
                    "receipt_id",
                    "status",
                    "merge_commit",
                    "observed_identity",
                    "observed_at",
                },
            )
            operation_id = _body_uuid(data["operation_id"])
            try:
                status = ReleaseStatus(data["status"])
                observed_at = datetime.fromisoformat(data["observed_at"].replace("Z", "+00:00"))
            except Exception:
                raise BrokerHttpError("invalid_request") from None
            if not isinstance(data["receipt_id"], str) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,128}", data["receipt_id"]
            ):
                raise BrokerHttpError("invalid_request")
            receipt = HostReceipt(
                approval_id=approval_id,
                target=context.target,
                operation_id=operation_id,
                receipt_id=data["receipt_id"],
                fence=_positive(data["dispatch_fence"]),
                status=status,
                merge_commit=_sha(data["merge_commit"]),
                observed_identity=_decode_identity(data["observed_identity"])
                if data["observed_identity"] is not None
                else None,
                observed_at=observed_at,
            )
            actual = await self._host_client.current_state(context.target)
            saved = await self._store.record_host_receipt(
                approval_id, receipt, actual_identity=actual.identity
            )
            return 200, {
                "recorded": True,
                "receipt_id": saved.receipt_id,
                "status": saved.status.value,
            }
        raise BrokerHttpError("not_found")

    async def _authenticate_job(
        self, headers: Mapping[str, str], context: Any, *, phase: str = "merge"
    ) -> CallerIdentity:
        identity = await self._job_authenticator.verify(_bearer(headers), context, phase=phase)
        if (
            identity.repository_id != context.repository_id
            or identity.owner_id != context.owner_id
            or identity.workflow_sha != context.workflow_sha
        ):
            raise BrokerHttpError("caller_denied")
        return identity

    def _authenticate_host(self, headers: Mapping[str, str]) -> None:
        if not hmac.compare_digest(_bearer(headers), self._host_credential):
            raise BrokerHttpError("host_denied")

    def _github(self, context: Any) -> GitHubEvidenceClient:
        owner, repository = context.repository.split("/", 1)
        return GitHubEvidenceClient(
            owner=owner,
            repository=repository,
            repository_id=context.repository_id,
            token_provider=self._github_token_provider,
            transport=self._github_transport,
        )

    def _current_main_sha(self, context: Any) -> str:
        client = self._github(context)
        try:
            value = client.get_json("git/ref/heads/main")
            sha = value.get("object", {}).get("sha")
            return _sha(sha)
        except Exception:
            raise BrokerHttpError("broker_unavailable") from None
        finally:
            client.close()

    async def _observe_merge(
        self, context: Any, commit: str, expected_tree: str
    ) -> VerifiedMergeObservation:
        def read() -> VerifiedMergeObservation:
            client = self._github(context)
            try:
                pull = client.get_json(f"pulls/{context.pr_number}")
                data = client.get_json(f"git/commits/{commit}")
                tree = data.get("tree", {}).get("sha")
                parents_value = data.get("parents")
                returned_sha = data.get("sha")
                if (
                    returned_sha != commit
                    or not isinstance(tree, str)
                    or not _SHA.fullmatch(tree)
                    or not isinstance(parents_value, list)
                    or len(parents_value) > 20
                    or any(
                        not isinstance(parent, dict)
                        or not isinstance(parent.get("sha"), str)
                        or not _SHA.fullmatch(parent["sha"])
                        for parent in parents_value
                    )
                    or pull.get("merged") is not True
                    or pull.get("merge_commit_sha") != commit
                ):
                    raise BrokerHttpError("caller_denied")
                return VerifiedMergeObservation(
                    commit=commit,
                    parents=tuple(parent["sha"] for parent in parents_value),
                    tree=tree,
                    expected_tree=expected_tree,
                    observed_at=datetime.now(UTC),
                )
            finally:
                client.close()

        try:
            return await asyncio.to_thread(read)
        except BrokerHttpError:
            raise
        except Exception:
            raise BrokerHttpError("broker_unavailable") from None


async def _read_body(receive: Any) -> bytes:
    data = bytearray()
    while True:
        event = await receive()
        if event.get("type") == "http.disconnect":
            raise BrokerHttpError("invalid_request")
        if event.get("type") != "http.request":
            continue
        chunk = event.get("body", b"")
        if not isinstance(chunk, bytes) or len(data) + len(chunk) > _MAX_BODY_BYTES:
            raise BrokerHttpError("body_too_large")
        data.extend(chunk)
        if not event.get("more_body", False):
            break
    return bytes(data)


def _headers(values: object) -> dict[str, str]:
    if not isinstance(values, list):
        raise BrokerHttpError("invalid_request")
    total = 0
    result: dict[str, str] = {}
    for item in values:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, bytes) for part in item)
        ):
            raise BrokerHttpError("invalid_request")
        key, value = item
        total += len(key) + len(value)
        if total > _MAX_HEADER_BYTES:
            raise BrokerHttpError("body_too_large")
        name = key.decode("ascii", errors="strict").lower()
        content = value.decode("latin-1")
        if name in result or "\r" in content or "\n" in content:
            raise BrokerHttpError("invalid_request")
        result[name] = content
    return result


def _bearer(headers: Mapping[str, str]) -> str:
    value = headers.get("authorization", "")
    if not value.startswith("Bearer ") or value.count(" ") != 1:
        raise BrokerHttpError("caller_denied")
    token = value[7:]
    if not token or len(token) > 16384:
        raise BrokerHttpError("caller_denied")
    return token


def _object(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BrokerHttpError("invalid_request")
    return value


def _positive(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise BrokerHttpError("invalid_request")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise BrokerHttpError("invalid_request")
    return value


def _body_uuid(value: object) -> UUID:
    if not _is_uuid(value):
        raise BrokerHttpError("invalid_request")
    return UUID(value)


def _path_uuid(value: str) -> UUID:
    try:
        result = UUID(value)
        if str(result) != value.lower():
            raise ValueError
        return result
    except (ValueError, AttributeError):
        raise BrokerHttpError("not_found") from None


def _same_run(data: Mapping[str, object], identity: CallerIdentity, fence: int) -> None:
    if (
        _positive(data.get("dispatch_fence")) != fence
        or _positive(data.get("run_id")) != identity.run_id
        or _positive(data.get("run_attempt")) != identity.run_attempt
        or _positive(data.get("check_run_id")) != identity.check_run_id
    ):
        raise BrokerHttpError("caller_denied")


def _run_binding(context: Any, identity: CallerIdentity, workflow_path: str) -> RunAttemptBinding:
    return RunAttemptBinding(
        repository_id=identity.repository_id,
        run_id=identity.run_id,
        attempt=identity.run_attempt,
        workflow_ref=f"{context.repository}/{workflow_path}@refs/heads/main",
        workflow_sha=identity.workflow_sha,
        check_run_id=identity.check_run_id,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def _send_json(send: Any, status: int, value: dict[str, Any]) -> None:
    body = _canonical(value)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def build_runtime(
    config: BrokerRuntimeConfig,
    *,
    github_transport: httpx.BaseTransport | None = None,
    host_transport: httpx.AsyncBaseTransport | None = None,
    jwks_transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[BrokerApplication, asyncpg.Pool]:
    dsn = _secret(config.database_dsn_file)
    host_inbound = _secret(config.host_inbound_credential_file)
    host_client_token = _secret(config.host_client_credential_file)
    pool: asyncpg.Pool | None = None
    github_http: BrokerAppHTTPPost | None = None
    try:
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=10,
            timeout=5,
            init=connection_initializer("release_control_broker"),
        )
        await verify_database_pool(pool, "release_control_broker")
        store = BrokerStore(pool)
        if not await store.schema_ready():
            raise BrokerHttpError("runtime_not_configured")
        worker_schema_ready = await pool.fetchval(
            "SELECT to_regclass('release_control.broker_dispatch_intent') IS NOT NULL"
        )
        if worker_schema_ready is not True:
            raise BrokerHttpError("runtime_not_configured")
        for target in config.targets:
            state = await store.get_target_state(target)
            policy_workflows = dict(state.policy.workflow_digests)
            if (
                state.policy.repository_id != config.github_repository_id
                or config.workflow_path not in policy_workflows
                or dict(config.workflow_blobs).get(config.workflow_path)
                != policy_workflows[config.workflow_path]
            ):
                raise BrokerHttpError("runtime_not_configured")
            verify_installed_blob_digests(
                config.object_repository, state.workflow_sha, dict(config.workflow_blobs)
            )
        github_http = BrokerAppHTTPPost(transport=github_transport)
        issuer = BrokerAppTokenIssuer(
            app_id=config.github_app_id,
            installation_id=config.github_installation_id,
            repository_id=config.github_repository_id,
            protected_key_provider=lambda: _safe_file(
                config.github_private_key_file, maximum=64 * 1024
            ),
            http_post=github_http,
            now=lambda: datetime.now(UTC),
        )

        def github_token_provider() -> str:
            return issuer.get_token().access_token

        authenticator = OIDCJobAuthenticator(
            subject_format=config.oidc_subject_format,
            audience=config.audience,
            workflow_path=config.workflow_path,
            merge_runner_environment=config.merge_runner_environment,
            relay_runner_environment=config.relay_runner_environment,
            object_repository=config.object_repository,
            workflow_blobs=dict(config.workflow_blobs),
            dispatch_id_provider=store.get_dispatch_id_for_job,
            github_token_provider=github_token_provider,
            github_transport=github_transport,
            jwks_transport=jwks_transport,
        )
        host = ProtectedHostClient(
            socket_path=config.host_socket,
            credential=host_client_token,
            transport=host_transport,
        )
        evidence = ReleaseEvidenceVerifier(
            object_repository=config.object_repository,
            workflow_blobs=dict(config.workflow_blobs),
            github_transport=github_transport,
            github_token_provider=github_token_provider,
        )
        app = BrokerApplication(
            store=store,
            config=config,
            job_authenticator=authenticator,
            host_client=host,
            evidence_verifier=evidence,
            host_inbound_credential=host_inbound,
            github_token_provider=github_token_provider,
            github_transport=github_transport,
            close_resources=(github_http,),
        )
        return app, pool
    except BrokerHttpError:
        if github_http is not None:
            github_http.close()
        if pool is not None:
            await pool.close()
        raise
    except Exception:
        if github_http is not None:
            github_http.close()
        if pool is not None:
            await pool.close()
        raise BrokerHttpError("runtime_not_configured") from None


class _StrictParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise BrokerHttpError("invalid_request")


def parse_command(argv: list[str] | None = None) -> tuple[Path, bool]:
    parser = _StrictParser(prog="chatcore-release-broker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    return Path(args.config), args.check_config


def main(argv: list[str] | None = None) -> int:
    try:
        config_path, check_only = parse_command(argv)
        config = BrokerRuntimeConfig.from_file(config_path)

        async def run() -> int:
            app, pool = await build_runtime(config)
            try:
                if check_only:
                    return 0
                import uvicorn

                server_config = uvicorn.Config(
                    app, uds=str(config.uds_path), lifespan="on", log_config=None, access_log=False
                )
                server = uvicorn.Server(server_config)
                await server.serve()
                return 0
            finally:
                await app.close()
                await pool.close()

        return asyncio.run(run())
    except BrokerHttpError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except Exception:
        print("runtime_not_configured", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
