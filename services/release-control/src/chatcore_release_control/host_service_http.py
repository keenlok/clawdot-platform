"""Strict owner-only local HTTP/UDS boundary for protected host operations."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import stat
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import uvicorn

from .broker_app import BrokerAppHTTPPost, SourceReadAppTokenIssuer
from .host_journal import HostJournal
from .host_service import (
    BrokerCredential,
    FixedBuildConfig,
    FixedComposeConfig,
    FixedComposeRuntime,
    FixedOfflineImageBuilder,
    GitExactCommitSource,
    GitSourceConfig,
    HostDeploymentContext,
    HostService,
    HostServiceError,
    P01HostEvidence,
    SubprocessCommandRunner,
)
from .policy import DeploymentIdentity, JournalState, PolicyManifest

MAX_BODY_BYTES = 4096
HOST_CONFIG_PATH = Path("/etc/chatcore-release-control/host-service.json")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_RECEIPT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_OPERATION_STATES = frozenset(
    {"prepared", "admitted", "started", "healthy", "restored_healthy", "recovery_failed"}
)
_TERMINAL_OPERATION_STATES = frozenset({"healthy", "restored_healthy", "recovery_failed"})


class HostHTTPError(RuntimeError):
    def __init__(self, code: str = "invalid_request", status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HostHTTPError()
        result[key] = value
    return result


def _json(raw: bytes) -> object:
    try:
        return json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise HostHTTPError() from None


def _read_root_config(path: Path) -> dict[str, object]:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 64 * 1024
        ):
            raise HostHTTPError("invalid_configuration", 500)
        raw = os.read(descriptor, 64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise HostHTTPError("invalid_configuration", 500)
        value = _json(raw)
        if not isinstance(value, dict):
            raise HostHTTPError("invalid_configuration", 500)
        return value
    except HostHTTPError:
        raise
    except OSError:
        raise HostHTTPError("invalid_configuration", 500) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def build_installed_service(path: str | os.PathLike[str]) -> tuple[HostServiceASGI, Path]:
    """Load strict root-owned configuration and construct every concrete adapter."""
    value = _read_root_config(Path(path))
    fields = {
        "version",
        "target",
        "policy_manifest_file",
        "policy_digest",
        "broker_socket",
        "broker_credential_file",
        "host_socket",
        "journal_file",
        "host_lock_file",
        "repository_url",
        "git_path",
        "source_app_config_file",
        "source_scratch_directory",
        "docker_path",
        "docker_config",
        "compose_file",
        "runtime_env_file",
        "app_runtime_env_file",
        "runtime_config_manifest_file",
        "runtime_config_files",
        "build_recipe",
        "build_entrypoint",
        "build_smoke_probe",
        "dependency_cache",
        "dependency_cache_sha256",
        "build_resource_manifest",
        "build_scratch_directory",
        "image_repository",
        "reviewed_source_exceptions",
        "buildx_builder",
        "buildkit_container",
        "buildkit_image_ref",
        "buildkit_image_id",
        "allowed_paths",
        "migration_path_prefixes",
        "source_roots",
        "test_paths",
        "worker_source",
        "harness_source",
        "build_resource_limits",
    }
    if set(value) != fields or type(value.get("version")) is not int or value["version"] != 1:
        raise HostHTTPError("invalid_configuration", 500)
    path_names = fields - {
        "version",
        "target",
        "policy_digest",
        "repository_url",
        "dependency_cache_sha256",
        "image_repository",
        "reviewed_source_exceptions",
        "buildx_builder",
        "buildkit_container",
        "buildkit_image_ref",
        "buildkit_image_id",
        "allowed_paths",
        "migration_path_prefixes",
        "source_roots",
        "test_paths",
        "runtime_config_files",
        "build_resource_limits",
    }
    if any(
        not isinstance(value.get(name), str) or not Path(value[name]).is_absolute()
        for name in path_names
    ):
        raise HostHTTPError("invalid_configuration", 500)
    if (
        not isinstance(value["repository_url"], str)
        or not isinstance(value["target"], str)
        or not isinstance(value["policy_digest"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["policy_digest"])
        or not isinstance(value["dependency_cache_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["dependency_cache_sha256"])
        or not isinstance(value["image_repository"], str)
        or not isinstance(value["build_resource_limits"], dict)
        or any(
            not isinstance(value[name], list)
            for name in (
                "allowed_paths",
                "reviewed_source_exceptions",
                "migration_path_prefixes",
                "source_roots",
                "test_paths",
                "runtime_config_files",
            )
        )
        or any(
            not all(isinstance(item, str) for item in value[name])
            for name in (
                "allowed_paths",
                "reviewed_source_exceptions",
                "migration_path_prefixes",
                "source_roots",
                "test_paths",
                "runtime_config_files",
            )
        )
        or not value["migration_path_prefixes"]
        or value["migration_path_prefixes"] != sorted(set(value["migration_path_prefixes"]))
        or not value["runtime_config_files"]
        or value["runtime_config_files"] != sorted(set(value["runtime_config_files"]))
        or any(not Path(item).is_absolute() for item in value["runtime_config_files"])
        or value["reviewed_source_exceptions"] != sorted(set(value["reviewed_source_exceptions"]))
        or not set(value["reviewed_source_exceptions"]).issubset({"apps/sb_bot/deploy/backup.sh"})
    ):
        raise HostHTTPError("invalid_configuration", 500)
    limits = value["build_resource_limits"]
    if set(limits) != {
        "cpu_quota_micros",
        "memory_bytes",
        "pids_max",
        "scratch_bytes",
        "timeout_seconds",
        "max_output_bytes",
    } or any(type(item) is not int for item in limits.values()):
        raise HostHTTPError("invalid_configuration", 500)
    post = None
    broker = None
    try:
        policy_value = _read_root_config(Path(value["policy_manifest_file"]))
        policy_raw = json.dumps(
            policy_value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        policy = PolicyManifest.from_json(policy_raw)
        if policy.digest != value["policy_digest"] or policy.target != value["target"]:
            raise HostHTTPError("invalid_configuration", 500)
        if tuple(sorted(set(value["allowed_paths"]))) != tuple(value["allowed_paths"]):
            raise HostHTTPError("invalid_configuration", 500)
        if set(value["allowed_paths"]) != set(policy.allowed_paths):
            raise HostHTTPError("invalid_configuration", 500)
        source_app = _read_root_config(Path(value["source_app_config_file"]))
        if (
            set(source_app)
            != {"version", "github_app_roles", "installation_id", "private_key_file"}
            or type(source_app["version"]) is not int
            or source_app["version"] != 1
        ):
            raise HostHTTPError("invalid_configuration", 500)
        roles = source_app["github_app_roles"]
        if (
            not isinstance(roles, dict)
            or set(roles) != {"publication", "broker", "runner_registration"}
            or any(type(item) is not int or item <= 0 for item in roles.values())
            or len(set(roles.values())) != 3
            or type(source_app["installation_id"]) is not int
            or source_app["installation_id"] <= 0
            or not isinstance(source_app["private_key_file"], str)
            or not Path(source_app["private_key_file"]).is_absolute()
        ):
            raise HostHTTPError("invalid_configuration", 500)
        source_key_path = Path(source_app["private_key_file"])
        FixedOfflineImageBuilder._read_installed(source_key_path, 32 * 1024, private=True)
        post = BrokerAppHTTPPost()
        issuer = SourceReadAppTokenIssuer(
            app_id=roles["broker"],
            installation_id=source_app["installation_id"],
            repository_id=policy.repository_id,
            protected_key_provider=lambda: FixedOfflineImageBuilder._read_installed(
                source_key_path, 32 * 1024, private=True
            ),
            http_post=post,
            now=lambda: datetime.now(UTC),
        )
        runner = SubprocessCommandRunner()
        source = GitExactCommitSource(
            GitSourceConfig(
                repository_url=value["repository_url"],
                git_path=value["git_path"],
                credential_file=None,
                token_provider=lambda: issuer.get_token().access_token,
                scratch_directory=Path(value["source_scratch_directory"]),
                timeout_seconds=120,
                max_output_bytes=64 * 1024,
            ),
            runner,
            close_resources=(post,),
        )
        builder = FixedOfflineImageBuilder(
            FixedBuildConfig(
                docker_path=value["docker_path"],
                recipe=Path(value["build_recipe"]),
                entrypoint=Path(value["build_entrypoint"]),
                smoke_probe=Path(value["build_smoke_probe"]),
                dependency_cache=Path(value["dependency_cache"]),
                dependency_cache_sha256=value["dependency_cache_sha256"],
                resource_manifest=Path(value["build_resource_manifest"]),
                scratch_directory=Path(value["build_scratch_directory"]),
                image_repository=value["image_repository"],
                allowed_paths=tuple(value["allowed_paths"]),
                source_roots=tuple(value["source_roots"]),
                test_paths=tuple(value["test_paths"]),
                worker_source=Path(value["worker_source"]),
                harness_source=Path(value["harness_source"]),
                reviewed_source_exceptions=tuple(value["reviewed_source_exceptions"]),
                **limits,
                buildx_builder=value["buildx_builder"],
                buildkit_container=value["buildkit_container"],
                buildkit_image_ref=value["buildkit_image_ref"],
                buildkit_image_id=value["buildkit_image_id"],
            ),
            runner,
        )
        runtime = FixedComposeRuntime(
            FixedComposeConfig(
                docker_path=value["docker_path"],
                compose_file=Path(value["compose_file"]),
                runtime_env_file=Path(value["runtime_env_file"]),
                app_env_file=Path(value["app_runtime_env_file"]),
                config_manifest=Path(value["runtime_config_manifest_file"]),
                config_files=tuple(Path(item) for item in value["runtime_config_files"]),
                docker_config=Path(value["docker_config"]),
                image_repository=value["image_repository"],
            ),
            runner,
        )
        broker = BrokerHostClient(
            socket_path=value["broker_socket"],
            credential_file=value["broker_credential_file"],
        )
        service = HostService(
            source=source,
            target=value["target"],
            policy_digest=value["policy_digest"],
            journal=HostJournal(value["journal_file"]),
            broker=broker,
            evidence=P01HostEvidence(policy, tuple(value["migration_path_prefixes"])),
            builder=builder,
            runtime=runtime,
            host_lock_path=value["host_lock_file"],
            now=lambda: int(datetime.now(UTC).timestamp()),
        )
        socket_path = Path(value["host_socket"])
        validate_uds_directory(socket_path.parent)
        return HostServiceASGI(service), socket_path
    except HostHTTPError:
        if post is not None:
            post.close()
        if broker is not None:
            broker.close()
        raise
    except Exception:
        if post is not None:
            post.close()
        if broker is not None:
            broker.close()
        raise HostHTTPError("invalid_configuration", 500) from None


class HostServiceASGI:
    """Accept only IDs over the owner-only local socket; policy stays installed."""

    def __init__(self, service: HostService) -> None:
        if not isinstance(service, HostService):
            raise HostHTTPError("invalid_configuration", 500)
        self._service = service

    def __repr__(self) -> str:
        return "HostServiceASGI(credentials=[REDACTED])"

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            return
        status, body = 200, {"ok": True}
        try:
            route = scope.get("path")
            method = scope.get("method")
            current_route = f"/v1/host/targets/{self._service.target}/current-state"
            if method == "GET":
                if route != current_route or scope.get("query_string", b""):
                    raise HostHTTPError("not_found", 404)
                body = await asyncio.to_thread(self._service.current_state)
                encoded = json.dumps(body, separators=(",", ":")).encode("ascii")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(encoded)).encode("ascii")),
                            (b"cache-control", b"no-store"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": encoded})
                return
            if method != "POST":
                raise HostHTTPError("method_not_allowed", 405)
            expected = {
                "/v1/deploy": {"approval_id", "operation_id"},
                "/v1/reconcile": {"approval_id", "operation_id"},
            }
            fields = expected.get(route)
            if fields is None:
                raise HostHTTPError("not_found", 404)
            headers: dict[bytes, bytes] = {}
            for key, value in scope.get("headers", []):
                normalized = key.lower()
                if normalized in {b"content-type", b"content-length", b"transfer-encoding"}:
                    if normalized in headers:
                        raise HostHTTPError()
                    headers[normalized] = value
            if headers.get(b"content-type") != b"application/json":
                raise HostHTTPError()
            if b"transfer-encoding" in headers:
                raise HostHTTPError()
            length = headers.get(b"content-length")
            if length is not None:
                if not length.isdigit() or int(length) > MAX_BODY_BYTES:
                    raise HostHTTPError("body_too_large", 413)
            chunks = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    raise HostHTTPError("invalid_request", 400)
                if message["type"] != "http.request":
                    continue
                chunks.extend(message.get("body", b""))
                if len(chunks) > MAX_BODY_BYTES:
                    raise HostHTTPError("body_too_large", 413)
                if not message.get("more_body", False):
                    break
            value = _json(bytes(chunks))
            if not isinstance(value, dict) or set(value) != fields:
                raise HostHTTPError()
            approval_id, operation_id = value["approval_id"], value["operation_id"]
            if (
                not isinstance(approval_id, str)
                or not _ID.fullmatch(approval_id)
                or not isinstance(operation_id, str)
                or not _ID.fullmatch(operation_id)
            ):
                raise HostHTTPError()
            if route == "/v1/deploy":
                record = await asyncio.to_thread(
                    self._service.deploy, approval_id, operation_id=operation_id
                )
            else:
                record = await asyncio.to_thread(self._service.reconcile, approval_id, operation_id)
            body = {
                "operation_id": record.operation_id,
                "phase": record.phase.value,
                "receipt": record.receipt,
                "payload_digest": record.payload_digest,
            }
        except HostHTTPError as error:
            status, body = error.status, {"error": error.code}
        except HostServiceError as error:
            status = (
                409
                if error.code
                in {
                    "broker_denied",
                    "baseline_mismatch",
                    "deployment_mismatch",
                    "reconciliation_required",
                    "recovery_failed",
                }
                else 400
            )
            body = {"error": error.code}
        except Exception:
            status, body = 500, {"error": "host_service_denied"}
        encoded = json.dumps(body, separators=(",", ":")).encode("ascii")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(encoded)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": encoded})


class BrokerHostClient:
    """Concrete host-principal client for the broker's protected host routes."""

    def __init__(
        self,
        *,
        socket_path: str | os.PathLike[str],
        credential_file: str | os.PathLike[str],
        client: httpx.Client | None = None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self._token = self._read_credential(Path(credential_file))
        self._target: str | None = None
        self._client = client or httpx.Client(
            transport=httpx.HTTPTransport(uds=str(self.socket_path)),
            base_url="http://release-broker",
            timeout=httpx.Timeout(10, connect=2),
            follow_redirects=False,
            trust_env=False,
        )

    def __repr__(self) -> str:
        return "BrokerHostClient(credentials=[REDACTED])"

    @staticmethod
    def _read_credential(path: Path) -> str:
        try:
            return BrokerCredential.read(path)
        except HostServiceError:
            raise HostHTTPError("invalid_configuration", 500) from None

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _uuid(value: str) -> str:
        try:
            return str(UUID(value))
        except (ValueError, TypeError, AttributeError):
            raise HostServiceError("invalid_request") from None

    def _request(
        self, method: str, path: str, *, json_body: object | None = None
    ) -> dict[str, object]:
        try:
            body = bytearray()
            with self._client.stream(
                method,
                path,
                json=json_body,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                if response.status_code not in range(200, 300):
                    raise HostServiceError("broker_denied")
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        raise HostServiceError("broker_denied")
            value = _json(bytes(body))
            if not isinstance(value, dict):
                raise HostServiceError("broker_denied")
            return value
        except HostServiceError:
            raise
        except (httpx.HTTPError, HostHTTPError, ValueError):
            raise HostServiceError("broker_denied") from None

    @staticmethod
    def _identity(value: object) -> DeploymentIdentity:
        fields = {
            "git_sha",
            "image_digest",
            "config_generation",
            "config_digest",
            "policy_digest",
            "target_sequence",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise HostServiceError("broker_denied")
        try:
            return DeploymentIdentity(**value)
        except (TypeError, ValueError):
            raise HostServiceError("broker_denied") from None

    @staticmethod
    def _utc_epoch(value: object) -> int | None:
        if not isinstance(value, str) or not 1 <= len(value) <= 40:
            return None
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
            if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
                return None
            return int(parsed.timestamp())
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _validate_host_receipt(
        value: object,
        *,
        approval_id: str,
        target: object,
        operation_id: str,
        dispatch_fence: int,
        merge_commit: object,
        state: str,
        baseline: DeploymentIdentity,
        payload: DeploymentIdentity,
    ) -> str | None:
        if value is None:
            if state in _TERMINAL_OPERATION_STATES:
                raise HostServiceError("broker_denied")
            return None
        fields = {
            "approval_id",
            "target",
            "operation_id",
            "receipt_id",
            "fence",
            "status",
            "merge_commit",
            "observed_identity",
            "observed_at",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise HostServiceError("broker_denied")
        receipt_id = value["receipt_id"]
        status = value["status"]
        if (
            value["approval_id"] != approval_id
            or value["target"] != target
            or value["operation_id"] != operation_id
            or type(value["fence"]) is not int
            or value["fence"] != dispatch_fence
            or value["merge_commit"] != merge_commit
            or not isinstance(receipt_id, str)
            or not _RECEIPT_ID.fullmatch(receipt_id)
            or not isinstance(status, str)
            or status not in _TERMINAL_OPERATION_STATES
            or status != state
            or BrokerHostClient._utc_epoch(value["observed_at"]) is None
        ):
            raise HostServiceError("broker_denied")
        observed_value = value["observed_identity"]
        observed = (
            BrokerHostClient._identity(observed_value) if observed_value is not None else None
        )
        if status == "healthy" and observed != payload:
            raise HostServiceError("broker_denied")
        if status == "restored_healthy" and observed != baseline:
            raise HostServiceError("broker_denied")
        if (
            status == "recovery_failed"
            and observed is not None
            and not isinstance(observed, DeploymentIdentity)
        ):
            raise HostServiceError("broker_denied")
        return receipt_id

    def get_context(self, approval_id: str) -> HostDeploymentContext:
        approval = self._uuid(approval_id)
        value = self._request("GET", f"/v1/host/releases/{approval}/deployment-context")
        fields = {
            "approval_id",
            "target",
            "sequence",
            "dispatch_fence",
            "status",
            "merge_commit",
            "policy_digest",
            "deployed_identity",
            "base_sha",
            "head_sha",
            "head_tree",
            "expected_merge_tree",
            "merge_tree",
            "merge_parents",
            "host_operation",
        }
        if set(value) != fields or value["approval_id"] != approval:
            raise HostServiceError("broker_denied")
        if not isinstance(value["status"], str):
            raise HostServiceError("broker_denied")
        baseline = self._identity(value["deployed_identity"])
        payload = None
        raw_parents = value["merge_parents"]
        if not isinstance(raw_parents, list) or len(raw_parents) != 2:
            raise HostServiceError("broker_denied")
        parents = tuple(raw_parents)
        operation = value["host_operation"]
        host_operation_id = None
        host_operation_payload_digest = None
        host_operation_state = None
        host_operation_start_deadline = None
        host_operation_recovery_deadline = None
        host_operation_receipt_id = None
        if operation is not None:
            operation_fields = {
                "operation_id",
                "dispatch_fence",
                "baseline_identity",
                "payload_identity",
                "payload_digest",
                "state",
                "start_deadline",
                "recovery_deadline",
                "receipt",
            }
            if not isinstance(operation, dict) or set(operation) != operation_fields:
                raise HostServiceError("broker_denied")
            if self._identity(operation["baseline_identity"]) != baseline:
                raise HostServiceError("broker_denied")
            payload = self._identity(operation["payload_identity"])
            operation_id = operation["operation_id"]
            state = operation["state"]
            if (
                not isinstance(operation_id, str)
                or self._uuid(operation_id) != operation_id
                or type(operation["dispatch_fence"]) is not int
                or operation["dispatch_fence"] != value["dispatch_fence"]
                or not isinstance(state, str)
                or state not in _OPERATION_STATES
                or (state in {"prepared", "admitted"} and value["status"] != "merged_verified")
                or (state == "started" and value["status"] not in {"deploying", "restoring"})
                or (state in _TERMINAL_OPERATION_STATES and value["status"] != state)
                or payload.git_sha != value["merge_commit"]
                or payload.policy_digest != value["policy_digest"]
                or payload.target_sequence != value["sequence"]
                or payload.config_generation != baseline.config_generation
                or payload.config_digest != baseline.config_digest
            ):
                raise HostServiceError("broker_denied")
            digest = hashlib.sha256(
                json.dumps(
                    asdict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
            ).hexdigest()
            if operation["payload_digest"] != digest:
                raise HostServiceError("broker_denied")
            raw_start, raw_recovery = operation["start_deadline"], operation["recovery_deadline"]
            if state == "prepared":
                if raw_start is not None or raw_recovery is not None:
                    raise HostServiceError("broker_denied")
            else:
                host_operation_start_deadline = self._utc_epoch(raw_start)
                host_operation_recovery_deadline = self._utc_epoch(raw_recovery)
                if (
                    host_operation_start_deadline is None
                    or host_operation_recovery_deadline is None
                    or host_operation_recovery_deadline <= host_operation_start_deadline
                ):
                    raise HostServiceError("broker_denied")
            host_operation_receipt_id = self._validate_host_receipt(
                operation["receipt"],
                approval_id=approval,
                target=value["target"],
                operation_id=operation_id,
                dispatch_fence=value["dispatch_fence"],
                merge_commit=value["merge_commit"],
                state=state,
                baseline=baseline,
                payload=payload,
            )
            if (state in _TERMINAL_OPERATION_STATES) != (host_operation_receipt_id is not None):
                raise HostServiceError("broker_denied")
            host_operation_id = operation_id
            host_operation_payload_digest = digest
            host_operation_state = state
        try:
            context = HostDeploymentContext(
                approval_id=approval,
                target=value["target"],
                sequence=value["sequence"],
                dispatch_fence=value["dispatch_fence"],
                status=value["status"],
                merge_commit=value["merge_commit"],
                policy_digest=value["policy_digest"],
                deployed_identity=baseline,
                payload=payload,
                base_sha=value["base_sha"],
                head_sha=value["head_sha"],
                head_tree=value["head_tree"],
                expected_merge_tree=value["expected_merge_tree"],
                merge_tree=value["merge_tree"],
                merge_parents=parents,
                host_operation_id=host_operation_id,
                host_operation_payload_digest=host_operation_payload_digest,
                host_operation_state=host_operation_state,
                host_operation_start_deadline=host_operation_start_deadline,
                host_operation_recovery_deadline=host_operation_recovery_deadline,
                host_operation_receipt_id=host_operation_receipt_id,
            )
            if self._target is not None and self._target != context.target:
                raise HostServiceError("broker_denied")
            self._target = context.target
            return context
        except (TypeError, ValueError):
            raise HostServiceError("broker_denied") from None

    def prepare(
        self,
        approval_id: str,
        fence: int,
        operation_id: str,
        baseline: DeploymentIdentity,
        payload: DeploymentIdentity,
    ) -> dict[str, object]:
        approval, operation = self._uuid(approval_id), self._uuid(operation_id)
        value = self._request(
            "POST",
            f"/v1/host/releases/{approval}/prepare",
            json_body={
                "dispatch_fence": fence,
                "operation_id": operation,
                "baseline_identity": asdict(baseline),
                "payload_identity": asdict(payload),
            },
        )
        if set(value) != {"operation_id", "payload_digest", "prepared"} or (
            value["operation_id"] != operation
            or value["prepared"] is not True
            or not isinstance(value["payload_digest"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["payload_digest"])
        ):
            raise HostServiceError("broker_denied")
        expected_digest = hashlib.sha256(
            json.dumps(
                asdict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        if value["payload_digest"] != expected_digest:
            raise HostServiceError("broker_denied")
        return value

    def admit(self, approval_id: str, fence: int, operation_id: str) -> dict[str, object]:
        approval, operation = self._uuid(approval_id), self._uuid(operation_id)
        value = self._request(
            "POST",
            f"/v1/host/releases/{approval}/switch-admission",
            json_body={"dispatch_fence": fence, "operation_id": operation},
        )
        fields = {
            "approval_id",
            "target",
            "operation_id",
            "dispatch_fence",
            "admitted",
            "may_start",
            "start_deadline",
            "recovery_deadline",
            "baseline_identity",
            "payload_identity",
        }
        if (
            set(value) != fields
            or value["approval_id"] != approval
            or value["target"] != self._target
            or self._target is None
            or value["operation_id"] != operation
            or type(value["dispatch_fence"]) is not int
            or value["dispatch_fence"] != fence
        ):
            raise HostServiceError("broker_denied")
        value["baseline_identity"] = self._identity(value["baseline_identity"])
        value["payload_identity"] = self._identity(value["payload_identity"])
        return value

    def started(self, approval_id: str, fence: int, operation_id: str) -> dict[str, object]:
        approval, operation = self._uuid(approval_id), self._uuid(operation_id)
        value = self._request(
            "POST",
            f"/v1/host/releases/{approval}/switch-started",
            json_body={"dispatch_fence": fence, "operation_id": operation},
        )
        if value != {"operation_id": operation, "started": True, "may_recover": True}:
            raise HostServiceError("broker_denied")
        return value

    def restore_started(self, approval_id: str, fence: int, operation_id: str) -> dict[str, object]:
        approval, operation = self._uuid(approval_id), self._uuid(operation_id)
        value = self._request(
            "POST",
            f"/v1/host/releases/{approval}/restore-started",
            json_body={"dispatch_fence": fence, "operation_id": operation},
        )
        if value != {"operation_id": operation, "started": True, "may_recover": True}:
            raise HostServiceError("broker_denied")
        return value

    def receipt(self, approval_id: str, fence: int, operation_id: str, record) -> dict[str, object]:
        approval, operation = self._uuid(approval_id), self._uuid(operation_id)
        statuses = {
            JournalState.HEALTHY: ("healthy", record.payload),
            JournalState.RESTORED_HEALTHY: ("restored_healthy", record.baseline),
            JournalState.RECOVERY_FAILED: ("recovery_failed", None),
        }
        if record.phase not in statuses or not record.receipt:
            raise HostServiceError("broker_denied")
        if type(record.terminal_at) is not int:
            raise HostServiceError("broker_denied")
        status, observed = statuses[record.phase]
        return self._request(
            "POST",
            f"/v1/host/releases/{approval}/receipt",
            json_body={
                "dispatch_fence": fence,
                "operation_id": operation,
                "receipt_id": record.receipt,
                "status": status,
                "merge_commit": record.payload.git_sha,
                "observed_identity": asdict(observed) if observed is not None else None,
                "observed_at": datetime.fromtimestamp(record.terminal_at, UTC).isoformat(),
            },
        )


def validate_uds_directory(path: str | os.PathLike[str]) -> Path:
    directory = Path(path)
    try:
        info = directory.lstat()
    except OSError:
        raise HostHTTPError("invalid_configuration", 500) from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise HostHTTPError("invalid_configuration", 500)
    return directory


def _bind_private_uds(path: Path) -> socket.socket:
    validate_uds_directory(path.parent)
    if len(os.fsencode(path)) > 100:
        raise HostHTTPError("invalid_configuration", 500)
    if path.exists() or path.is_symlink():
        try:
            info = path.lstat()
        except OSError:
            raise HostHTTPError("invalid_configuration", 500) from None
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise HostHTTPError("invalid_configuration", 500)
        path.unlink()
    server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_socket.setblocking(False)
    previous_mask = os.umask(0o077)
    try:
        server_socket.bind(str(path))
    finally:
        os.umask(previous_mask)
    os.chmod(path, 0o600)
    server_socket.listen(128)
    return server_socket


def _remove_owned_socket(path: Path) -> None:
    try:
        info = path.lstat()
        if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid():
            path.unlink()
    except OSError:
        pass


async def serve_private_uds(
    app: Callable,
    socket_path: str | os.PathLike[str],
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """Serve on an owner-only UDS without unlinking an unknown filesystem object."""
    path = Path(socket_path)
    server_socket = await asyncio.to_thread(_bind_private_uds, path)
    config = uvicorn.Config(app=app, log_config=None, access_log=False, lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[server_socket]))
    try:
        if stop is None:
            await task
        else:
            await stop.wait()
            server.should_exit = True
            await task
    finally:
        if not task.done():
            server.should_exit = True
            await task
        await asyncio.to_thread(_remove_owned_socket, path)


async def _serve_configured(config_path: str) -> None:
    app, socket_path = build_installed_service(config_path)
    try:
        await serve_private_uds(app, socket_path)
    finally:
        app._service.broker.close()
        app._service.source.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chatcore-host-service")
    parser.add_argument("--config", required=True)
    arguments = parser.parse_args(argv)
    if Path(arguments.config) != HOST_CONFIG_PATH:
        sys.stderr.write("invalid_configuration\n")
        return 2
    try:
        asyncio.run(_serve_configured(arguments.config))
    except HostHTTPError as error:
        sys.stderr.write(error.code + "\n")
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
