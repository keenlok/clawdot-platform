"""Fail-closed whole-runner supervision primitives for P09.

The default Docker executor is a real local process adapter. Tests inject fakes;
this module never starts a runner or Docker unless an operator explicitly calls
the supervisor in a provisioned environment.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit

import httpx
import jwt

from .runner_bootstrap import (
    RunnerImageManifest,
    compute_runner_closure_digest,
    load_runner_manifest,
    parse_runner_manifest,
)

_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64}\Z")
_OWNER_ID = re.compile(r"runner-[a-f0-9]{32}\Z")
_CONTAINER_ID = re.compile(r"[a-f0-9]{12,64}\Z")
_RELAY_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_GITHUB_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,4096}\Z")
_OIDC_JWT = re.compile(r"(?=.{1,8192}\Z)[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{16,}\Z")
_EGRESS_DESTINATION = re.compile(r"([a-z0-9.-]{1,253}):443\Z")
_MAX_POLICY_LIFETIME = 3600
_MAX_PROCESS_OUTPUT = 64 * 1024
_FIXED_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
_RELAY_PATH = "/v1/runner/receipt"
_GITHUB_API_ROOT = "https://api.github.com"
_RUNNER_IMAGE_MANIFEST_PATH = "/opt/p09/runner-image-manifest.json"
_RUNNER_BOOTSTRAP_PATH = "/opt/p09/runner_bootstrap.py"
_RUNNER_SOCKET_PATH = "/tmp/p09-registration.sock"  # noqa: S108
_MAX_HTTP_BODY = 256 * 1024
_CONFIG_FIELDS = {
    "schema_version",
    "docker_binary",
    "runtime_path",
    "runtime_manifest_path",
    "runtime_manifest_sha256",
    "image",
    "runner_manifest_path",
    "runner_closure_sha256",
    "runner_python_binary",
    "runner_user",
    "cpu_limit",
    "memory_limit_bytes",
    "pid_limit",
    "lifetime_seconds",
    "scratch_limit_bytes",
    "output_limit_bytes",
    "egress_network",
    "egress_destinations",
    "egress_manifest_path",
    "egress_manifest_sha256",
    "relay_origin",
    "oidc_audience",
    "repository_owner",
    "repository_name",
    "repository_id",
    "github_installation_id",
    "github_app_roles",
    "github_app_private_key_path",
    "ownership_database",
}


class SupervisorError(RuntimeError):
    """A sanitized denial/failure that does not contain credentials or subprocess output."""


class GitHubAppRole(StrEnum):
    PUBLICATION = "publication"
    BROKER = "broker"
    RUNNER_REGISTRATION = "runner_registration"


@dataclass(frozen=True, slots=True)
class SupervisorPolicy:
    docker_binary: str
    runtime_path: str
    runtime_name: str
    runtime_manifest_digest: str
    image: str
    runner_closure_digest: str
    repository_url: str
    runner_python_binary: str
    runner_user: str
    runner_entrypoint: str
    registration_stdin_command: tuple[str, ...]
    cpu_limit: float
    memory_limit_bytes: int
    pid_limit: int
    lifetime_seconds: int
    scratch_limit_bytes: int
    output_limit_bytes: int
    egress_network: str
    egress_destinations: tuple[str, ...]
    relay_origin: str
    oidc_audience: str

    def __post_init__(self) -> None:
        if (
            not _safe_host_path(self.docker_binary)
            or not _safe_host_path(self.runtime_path)
            or self.runtime_name != "runsc"
            or not isinstance(self.runtime_manifest_digest, str)
            or not _SHA256.fullmatch(self.runtime_manifest_digest)
            or not isinstance(self.image, str)
            or not _IMAGE.fullmatch(self.image)
            or not isinstance(self.runner_closure_digest, str)
            or not _SHA256.fullmatch(self.runner_closure_digest)
            or not _valid_repository_url(self.repository_url)
            or not _safe_container_path(self.runner_python_binary)
            or PurePosixPath(self.runner_python_binary).name not in {"python3", "python3.12"}
            or not isinstance(self.runner_user, str)
            or not re.fullmatch(r"[1-9][0-9]{0,8}:[1-9][0-9]{0,8}", self.runner_user)
            or self.runner_entrypoint != _RUNNER_BOOTSTRAP_PATH
            or not isinstance(self.registration_stdin_command, tuple)
            or self.registration_stdin_command
            != (
                self.runner_python_binary,
                _RUNNER_BOOTSTRAP_PATH,
                "receive-registration",
                "--manifest",
                _RUNNER_IMAGE_MANIFEST_PATH,
                "--expected-closure",
                self.runner_closure_digest,
                "--socket",
                _RUNNER_SOCKET_PATH,
            )
            or type(self.cpu_limit) not in (int, float)
            or not 0.1 <= self.cpu_limit <= 16
            or any(
                type(value) is not int or value <= 0
                for value in (
                    self.memory_limit_bytes,
                    self.pid_limit,
                    self.lifetime_seconds,
                    self.scratch_limit_bytes,
                    self.output_limit_bytes,
                )
            )
            or self.memory_limit_bytes > 32 * 1024**3
            or self.pid_limit > 4096
            or self.lifetime_seconds > _MAX_POLICY_LIFETIME
            or self.scratch_limit_bytes > 64 * 1024**3
            or self.output_limit_bytes > _MAX_PROCESS_OUTPUT
            or not isinstance(self.egress_network, str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", self.egress_network)
            or self.egress_network.lower() in {"bridge", "host", "none"}
            or not isinstance(self.egress_destinations, tuple)
            or not self.egress_destinations
            or any(not _valid_egress_destination(host) for host in self.egress_destinations)
            or len(set(self.egress_destinations)) != len(self.egress_destinations)
            or not _valid_origin(self.relay_origin)
            or not isinstance(self.oidc_audience, str)
            or not 1 <= len(self.oidc_audience) <= 512
            or any(ord(character) < 32 for character in self.oidc_audience)
        ):
            raise ValueError("invalid installed runner supervisor policy")
        # Freeze caller-provided tuple subclasses/iterables into immutable snapshots.
        object.__setattr__(
            self, "registration_stdin_command", tuple(self.registration_stdin_command)
        )
        object.__setattr__(self, "egress_destinations", tuple(self.egress_destinations))


def _safe_container_path(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return ".." not in path.parts and not any(ch.isspace() for ch in value)


def _safe_host_path(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        return False
    path = Path(value)
    return ".." not in path.parts and not any(ch.isspace() for ch in value)


def _valid_egress_destination(value: object) -> bool:
    if not isinstance(value, str):
        return False
    match = _EGRESS_DESTINATION.fullmatch(value)
    if match is None:
        return False
    labels = match.group(1).split(".")
    return bool(
        len(labels) >= 2
        and all(
            1 <= len(label) <= 63
            and label[0] != "-"
            and label[-1] != "-"
            and re.fullmatch(r"[a-z0-9-]+", label)
            for label in labels
        )
    )


def _valid_origin(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
        and not parsed.username
        and not parsed.password
        and parsed.port in (None, 443)
    )


def _valid_repository_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", parsed.path)
        and parsed.path.count("/") == 2
        and not parsed.query
        and not parsed.fragment
        and not parsed.username
        and not parsed.password
        and parsed.port in (None, 443)
    )


@dataclass(frozen=True, slots=True)
class InstalledSupervisorConfig:
    policy: SupervisorPolicy
    runtime_manifest_path: str
    runner_manifest_path: str
    egress_manifest_path: str
    egress_manifest_sha256: str
    repository_owner: str
    repository_name: str
    repository_id: int
    github_app_roles: Mapping[str, int]
    github_installation_id: int
    github_app_private_key_path: str
    ownership_database: str

    def __post_init__(self) -> None:
        roles = dict(self.github_app_roles)
        expected = {role.value for role in GitHubAppRole}
        if (
            set(roles) != expected
            or any(type(app_id) is not int or app_id <= 0 for app_id in roles.values())
            or len(set(roles.values())) != len(roles)
            or type(self.github_installation_id) is not int
            or self.github_installation_id <= 0
        ):
            raise ValueError("runner App roles are invalid")
        object.__setattr__(self, "github_app_roles", MappingProxyType(roles))

    @property
    def github_app_id(self) -> int:
        return self.github_app_roles[GitHubAppRole.RUNNER_REGISTRATION.value]

    @property
    def repository_url(self) -> str:
        return self.policy.repository_url


def _safe_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_protected_file(path: str | Path, limit: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            parent_info = Path(path).parent.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o077
                or info.st_size <= 0
                or info.st_size > limit
                or not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid != 0
                or parent_info.st_mode & 0o022
            ):
                raise SupervisorError("installed supervisor file is unsafe")
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(descriptor, min(8192, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if not data or len(data) > limit:
                raise SupervisorError("installed supervisor file is unsafe")
            return bytes(data)
        finally:
            os.close(descriptor)
    except SupervisorError:
        raise
    except OSError:
        raise SupervisorError("installed supervisor file is unavailable") from None


def load_installed_config(
    path: str | Path,
    *,
    protected_reader: Callable[[str | Path, int], bytes] = _read_protected_file,
) -> InstalledSupervisorConfig:
    """Load an exact root-owned 0600 JSON policy; there are no local defaults."""
    if (
        str(path) != "/etc/chatcore-release-control/p09-policy.json"
        or not _safe_host_path(str(path))
        or not callable(protected_reader)
    ):
        raise SupervisorError("installed supervisor policy path is invalid")
    try:
        raw = protected_reader(path, 64 * 1024)
        value = json.loads(raw, object_pairs_hook=_safe_json_object)
    except SupervisorError:
        raise
    except Exception:
        raise SupervisorError("installed supervisor policy is invalid") from None
    if not isinstance(value, dict) or set(value) != _CONFIG_FIELDS:
        raise SupervisorError("installed supervisor policy is invalid")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise SupervisorError("installed supervisor policy is invalid")
    strings = (
        "docker_binary",
        "runtime_path",
        "runtime_manifest_path",
        "runtime_manifest_sha256",
        "image",
        "runner_manifest_path",
        "runner_closure_sha256",
        "runner_python_binary",
        "runner_user",
        "egress_network",
        "egress_manifest_path",
        "egress_manifest_sha256",
        "relay_origin",
        "oidc_audience",
        "repository_owner",
        "repository_name",
        "github_app_private_key_path",
        "ownership_database",
    )
    if any(not isinstance(value.get(field), str) for field in strings):
        raise SupervisorError("installed supervisor policy is invalid")
    if (
        not isinstance(value.get("egress_destinations"), list)
        or any(not isinstance(item, str) for item in value["egress_destinations"])
        or type(value.get("repository_id")) is not int
        or type(value.get("github_installation_id")) is not int
        or not isinstance(value.get("github_app_roles"), Mapping)
    ):
        raise SupervisorError("installed supervisor policy is invalid")
    app_roles = value["github_app_roles"]
    expected_roles = {role.value for role in GitHubAppRole}
    if (
        set(app_roles) != expected_roles
        or any(type(app_id) is not int or app_id <= 0 for app_id in app_roles.values())
        or len(set(app_roles.values())) != len(app_roles)
        or type(value["github_installation_id"]) is not int
        or value["github_installation_id"] <= 0
    ):
        raise SupervisorError("installed supervisor App roles are invalid")
    owner = value["repository_owner"]
    repository = value["repository_name"]
    if (
        re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner) is None
        or owner in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository) is None
        or repository in {".", ".."}
        or not value["github_app_private_key_path"].startswith("/etc/chatcore-release-control/")
        or value["ownership_database"] != "/var/lib/chatcore-release-control/p09/owners.sqlite3"
        or not value["runtime_manifest_path"].startswith("/opt/gvisor/")
        or not value["runner_manifest_path"].startswith("/opt/chatcore-release-control/")
        or not value["egress_manifest_path"].startswith("/etc/chatcore-release-control/")
        or any(
            not _safe_host_path(value[field])
            for field in (
                "runtime_manifest_path",
                "runner_manifest_path",
                "egress_manifest_path",
                "github_app_private_key_path",
                "ownership_database",
            )
        )
    ):
        raise SupervisorError("installed supervisor policy is invalid")
    repository_url = f"https://github.com/{owner}/{repository}"
    bootstrap_command = (
        value["runner_python_binary"],
        _RUNNER_BOOTSTRAP_PATH,
        "receive-registration",
        "--manifest",
        _RUNNER_IMAGE_MANIFEST_PATH,
        "--expected-closure",
        value["runner_closure_sha256"],
        "--socket",
        _RUNNER_SOCKET_PATH,
    )
    try:
        policy = SupervisorPolicy(
            docker_binary=value["docker_binary"],
            runtime_path=value["runtime_path"],
            runtime_name="runsc",
            runtime_manifest_digest=value["runtime_manifest_sha256"],
            image=value["image"],
            runner_closure_digest=value["runner_closure_sha256"],
            repository_url=repository_url,
            runner_python_binary=value["runner_python_binary"],
            runner_user=value["runner_user"],
            runner_entrypoint=_RUNNER_BOOTSTRAP_PATH,
            registration_stdin_command=bootstrap_command,
            cpu_limit=value["cpu_limit"],
            memory_limit_bytes=value["memory_limit_bytes"],
            pid_limit=value["pid_limit"],
            lifetime_seconds=value["lifetime_seconds"],
            scratch_limit_bytes=value["scratch_limit_bytes"],
            output_limit_bytes=value["output_limit_bytes"],
            egress_network=value["egress_network"],
            egress_destinations=tuple(value["egress_destinations"]),
            relay_origin=value["relay_origin"],
            oidc_audience=value["oidc_audience"],
        )
        return InstalledSupervisorConfig(
            policy=policy,
            runtime_manifest_path=value["runtime_manifest_path"],
            runner_manifest_path=value["runner_manifest_path"],
            egress_manifest_path=value["egress_manifest_path"],
            egress_manifest_sha256=value["egress_manifest_sha256"],
            repository_owner=owner,
            repository_name=repository,
            repository_id=value["repository_id"],
            github_app_roles=app_roles,
            github_installation_id=value["github_installation_id"],
            github_app_private_key_path=value["github_app_private_key_path"],
            ownership_database=value["ownership_database"],
        )
    except (TypeError, ValueError):
        raise SupervisorError("installed supervisor policy is invalid") from None


def _read_root_owned_integrity_file(
    path: str | Path, limit: int, *, expected_uid: int = 0
) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != expected_uid
                or info.st_mode & 0o022
                or info.st_size <= 0
                or info.st_size > limit
            ):
                raise SupervisorError("installed capability file is unsafe")
            parent_info = Path(path).parent.lstat()
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid != expected_uid
                or parent_info.st_mode & 0o022
            ):
                raise SupervisorError("installed capability directory is unsafe")
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(descriptor, min(8192, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if not data or len(data) > limit:
                raise SupervisorError("installed capability file is unsafe")
            return bytes(data)
        finally:
            os.close(descriptor)
    except SupervisorError:
        raise
    except OSError:
        raise SupervisorError("installed capability file is unavailable") from None


_EGRESS_CAPABILITY_FIELDS = {
    "schema_version",
    "network",
    "destinations",
    "egress_enforced",
    "no_host_mounts",
    "no_host_socket",
    "no_host_namespaces",
    "isolated_user_namespace",
    "no_privilege",
    "resource_limits",
    "read_only_root",
    "disposable_scratch",
}


class InstalledCapabilityProbe:
    """Check pinned installed manifests plus fixed Docker runtime/image/network facts.

    The egress/isolation JSON is a protected host attestation. It records the
    intended installed network policy and cannot prove kernel or packet behavior.
    """

    def __init__(
        self,
        config: InstalledSupervisorConfig,
        *,
        execute: Executor | None = None,
        protected_reader: Callable[[str | Path, int], bytes] = _read_root_owned_integrity_file,
        host_reader: Callable[[str | Path], bytes] | None = None,
        expected_owner_uid: int = 0,
    ):
        self._config = config
        self._execute = execute or _bounded_process_executor
        self._protected_reader = protected_reader
        self._host_reader = host_reader or (lambda path: Path(path).read_bytes())
        self._expected_owner_uid = expected_owner_uid

    def __call__(self, policy: SupervisorPolicy) -> CapabilitySnapshot:
        if policy != self._config.policy:
            raise SupervisorError("installed runner capability identity mismatch")
        self._verify_runtime_manifest()
        runner_manifest = self._verify_runner_manifest()
        egress = self._verify_egress_manifest()
        info = self._docker_json([policy.docker_binary, "info", "--format", "{{json .}}"])
        runtimes = info.get("Runtimes")
        runsc = runtimes.get("runsc") if isinstance(runtimes, Mapping) else None
        runtime_path = runsc.get("path") if isinstance(runsc, Mapping) else None
        security_options = info.get("SecurityOptions")
        user_namespace_isolation = isinstance(security_options, list) and any(
            isinstance(value, str) and value in {"name=userns", "name=rootless"}
            for value in security_options
        )
        if (
            runtime_path != policy.runtime_path
            or str(info.get("CgroupVersion")) != "2"
            or not user_namespace_isolation
        ):
            raise SupervisorError("installed runner capabilities do not match policy")
        image = self._docker_json(
            [policy.docker_binary, "image", "inspect", "--format", "{{json .}}", policy.image]
        )
        self._verify_image(image, policy, runner_manifest)
        network = self._docker_json(
            [
                policy.docker_binary,
                "network",
                "inspect",
                "--format",
                "{{json .}}",
                policy.egress_network,
            ]
        )
        labels = network.get("Labels")
        if (
            network.get("Name") != policy.egress_network
            or network.get("Driver") != "bridge"
            or not isinstance(labels, Mapping)
            or labels.get("io.chatcore.p09-egress-sha256") != self._config.egress_manifest_sha256
        ):
            raise SupervisorError("installed egress network does not match policy")
        try:
            cgroup_controllers = self._host_reader("/sys/fs/cgroup/cgroup.controllers")
        except Exception:
            raise SupervisorError("cgroup v2 is unavailable") from None
        if not isinstance(cgroup_controllers, bytes) or not cgroup_controllers:
            raise SupervisorError("cgroup v2 is unavailable")
        return CapabilitySnapshot(
            runtime_path=policy.runtime_path,
            runtime_name="runsc",
            runtime_manifest_digest=policy.runtime_manifest_digest,
            image=policy.image,
            runner_closure_digest=runner_manifest.runner_closure_sha256,
            cgroup_v2=True,
            resource_limits=egress["resource_limits"],
            read_only_root=egress["read_only_root"],
            disposable_scratch=egress["disposable_scratch"],
            egress_enforced=egress["egress_enforced"],
            egress_network=policy.egress_network,
            egress_destinations=policy.egress_destinations,
            no_host_mounts=egress["no_host_mounts"],
            no_host_socket=egress["no_host_socket"],
            no_host_namespaces=egress["no_host_namespaces"],
            isolated_user_namespace=egress["isolated_user_namespace"],
            no_privilege=egress["no_privilege"],
        )

    def _read_json(self, path: str, limit: int) -> tuple[bytes, dict[str, object]]:
        try:
            raw = self._protected_reader(path, limit)
            value = json.loads(raw, object_pairs_hook=_safe_json_object)
            if not isinstance(value, dict):
                raise ValueError
            return raw, value
        except Exception:
            raise SupervisorError("installed capability manifest is invalid") from None

    def _verify_runtime_manifest(self) -> None:
        raw, manifest = self._read_json(self._config.runtime_manifest_path, 256 * 1024)
        if hashlib.sha256(raw).hexdigest() != self._config.policy.runtime_manifest_digest:
            raise SupervisorError("installed runtime manifest digest mismatch")
        if (
            set(manifest) != {"schema_version", "release", "archive_sha512", "files"}
            or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1
            or not isinstance(manifest.get("files"), Mapping)
            or not manifest["files"]
        ):
            raise SupervisorError("installed runtime manifest is invalid")
        root = Path(self._config.policy.runtime_path).parent
        for relative, expected in manifest["files"].items():
            if (
                not isinstance(relative, str)
                or not isinstance(expected, str)
                or not _SHA256.fullmatch(expected)
            ):
                raise SupervisorError("installed runtime manifest is invalid")
            if (
                not relative
                or PurePosixPath(relative).is_absolute()
                or ".." in PurePosixPath(relative).parts
            ):
                raise SupervisorError("installed runtime manifest is invalid")
            path = root / relative
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != self._expected_owner_uid:
                    raise SupervisorError("installed runtime package is unsafe")
                data = self._protected_reader(path, 256 * 1024 * 1024)
            except SupervisorError:
                raise
            except Exception:
                raise SupervisorError("installed runtime package is unavailable") from None
            if hashlib.sha256(data).hexdigest() != expected:
                raise SupervisorError("installed runtime package digest mismatch")
        runsc_relative = Path(self._config.policy.runtime_path).relative_to(root).as_posix()
        if runsc_relative not in manifest["files"]:
            raise SupervisorError("runsc is absent from installed runtime manifest")

    def _verify_runner_manifest(self) -> RunnerImageManifest:
        raw, _ = self._read_json(self._config.runner_manifest_path, 256 * 1024)
        manifest = parse_runner_manifest(raw)
        if (
            manifest.runner_closure_sha256 != self._config.policy.runner_closure_digest
            or manifest.runner_root != "/opt/p09/runner-files"
        ):
            raise SupervisorError("runner image closure digest mismatch")
        return manifest

    def _verify_egress_manifest(self) -> dict[str, object]:
        raw, manifest = self._read_json(self._config.egress_manifest_path, 64 * 1024)
        if hashlib.sha256(raw).hexdigest() != self._config.egress_manifest_sha256:
            raise SupervisorError("installed egress manifest digest mismatch")
        if (
            set(manifest) != _EGRESS_CAPABILITY_FIELDS
            or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1
            or manifest.get("network") != self._config.policy.egress_network
            or manifest.get("destinations") != list(self._config.policy.egress_destinations)
            or any(
                type(manifest.get(field)) is not bool or manifest.get(field) is not True
                for field in _EGRESS_CAPABILITY_FIELDS
                - {"schema_version", "network", "destinations"}
            )
        ):
            raise SupervisorError("installed egress manifest does not meet policy")
        return manifest

    def _verify_image(
        self,
        image: Mapping[str, object],
        policy: SupervisorPolicy,
        runner_manifest: RunnerImageManifest,
    ) -> None:
        config = image.get("Config")
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        if (
            policy.image not in image.get("RepoDigests", [])
            or not isinstance(config, Mapping)
            or config.get("User") != policy.runner_user
            or config.get("Entrypoint") != [policy.runner_python_binary, _RUNNER_BOOTSTRAP_PATH]
            or not isinstance(labels, Mapping)
            or labels.get("io.chatcore.runner-closure-sha256") != policy.runner_closure_digest
            or labels.get("io.chatcore.runner-version") != runner_manifest.runner_version
            or config.get("Volumes") not in (None, {})
            or not _safe_image_environment(config.get("Env"))
        ):
            raise SupervisorError("installed runner image does not match policy")

    def _docker_json(self, argv: list[str]) -> dict[str, object]:
        try:
            result = self._execute(
                argv,
                env=dict(_FIXED_ENV),
                timeout=8,
                input_data=None,
            )
            if (
                not isinstance(result, ExecResult)
                or type(result.returncode) is not int
                or result.returncode != 0
                or len(_output_bytes(result.stdout)) + len(_output_bytes(result.stderr)) > 64 * 1024
            ):
                raise ValueError
            value = json.loads(_output_bytes(result.stdout), object_pairs_hook=_safe_json_object)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except Exception:
            raise SupervisorError("fixed Docker capability probe failed") from None


def _output_bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    return b""


def _safe_image_environment(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, str) or "=" not in item:
            return False
        name = item.split("=", 1)[0].lower()
        if any(marker in name for marker in ("token", "secret", "password", "credential", "key")):
            return False
    return True


def _read_bounded_response(response: httpx.Response, limit: int) -> bytes:
    data = bytearray()
    try:
        for chunk in response.iter_bytes(chunk_size=8192):
            if len(data) + len(chunk) > limit:
                raise ValueError
            data.extend(chunk)
    except Exception:
        raise SupervisorError("fixed HTTP response exceeded policy") from None
    return bytes(data)


def _parse_http_json(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data, object_pairs_hook=_safe_json_object)
    except Exception:
        raise SupervisorError("fixed HTTP response was invalid") from None
    if not isinstance(value, dict):
        raise SupervisorError("fixed HTTP response was invalid")
    return value


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SupervisorError("supervisor clock is invalid")
    return value.astimezone(UTC)


def _parse_expiration(value: object, now: datetime, max_seconds: int) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise SupervisorError("GitHub credential response was invalid")
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
        expires = _aware_utc(expires)
    except Exception:
        raise SupervisorError("GitHub credential response was invalid") from None
    if not now < expires <= now + timedelta(seconds=max_seconds):
        raise SupervisorError("GitHub credential response was invalid")
    return expires


class GitHubRunnerAppInstallationTokenProvider:
    """Mint one-repository, short-lived runner-management installation tokens.

    The dedicated runner-registration App permission is fixed to administration
    write because the GitHub repository runner-management endpoints require that
    permission. Operators must review the actual grant for their repository.
    """

    def __init__(
        self,
        *,
        principal_role: GitHubAppRole,
        app_roles: Mapping[str, int],
        app_id: int,
        installation_id: int,
        repository_id: int,
        private_key_provider: Callable[[], str | bytes],
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        roles = dict(app_roles) if isinstance(app_roles, Mapping) else {}
        expected_roles = {role.value for role in GitHubAppRole}
        if (
            principal_role != GitHubAppRole.RUNNER_REGISTRATION
            or set(roles) != expected_roles
            or any(type(value) is not int or value <= 0 for value in roles.values())
            or len(set(roles.values())) != len(roles)
            or roles.get(GitHubAppRole.RUNNER_REGISTRATION.value) != app_id
            or type(app_id) is not int
            or app_id <= 0
            or type(installation_id) is not int
            or installation_id <= 0
            or type(repository_id) is not int
            or repository_id <= 0
            or not callable(private_key_provider)
            or not callable(now)
        ):
            raise SupervisorError("runner App token configuration is invalid")
        self._app_id = app_id
        self._principal_role = principal_role
        self._app_roles = MappingProxyType(roles)
        self._installation_id = installation_id
        self._repository_id = repository_id
        self._private_key_provider = private_key_provider
        self._now = now
        self._lock = threading.Lock()
        self._cached_token: str | None = None
        self._cached_expiration: datetime | None = None
        self._client = httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(8.0, connect=4.0),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def __repr__(self) -> str:
        return (
            f"GitHubRunnerAppInstallationTokenProvider(app_id={self._app_id}, "
            f"installation_id={self._installation_id}, repository_id={self._repository_id})"
        )

    def close(self) -> None:
        self._client.close()

    def __call__(self) -> str:
        with self._lock:
            now = _aware_utc(self._now())
            if (
                self._cached_token is not None
                and self._cached_expiration is not None
                and (self._cached_expiration - now).total_seconds() > 120
            ):
                return self._cached_token
            try:
                key = self._private_key_provider()
                app_jwt = jwt.encode(
                    {
                        "iss": str(self._app_id),
                        "iat": int(now.timestamp()) - 30,
                        "exp": int(now.timestamp()) + 540,
                    },
                    key,
                    algorithm="RS256",
                    headers={"typ": "JWT"},
                )
            except Exception:
                raise SupervisorError("runner App authentication failed") from None
            url = f"{_GITHUB_API_ROOT}/app/installations/{self._installation_id}/access_tokens"
            body = {
                "repository_ids": [self._repository_id],
                "permissions": {"administration": "write"},
            }
            headers = {"Authorization": f"Bearer {app_jwt}"}
            try:
                with self._client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code != 201:
                        raise SupervisorError("runner App token request failed")
                    parsed = _parse_http_json(_read_bounded_response(response, 64 * 1024))
            except SupervisorError:
                raise
            except Exception:
                raise SupervisorError("runner App token request failed") from None
            finally:
                self._client.cookies.clear()
            token = parsed.get("token")
            repositories = parsed.get("repositories")
            if (
                not isinstance(token, str)
                or not _GITHUB_TOKEN.fullmatch(token)
                or parsed.get("permissions") != {"administration": "write"}
                or not isinstance(repositories, list)
                or len(repositories) != 1
                or not isinstance(repositories[0], Mapping)
                or type(repositories[0].get("id")) is not int
                or repositories[0].get("id") != self._repository_id
            ):
                raise SupervisorError("runner App token response was invalid")
            expires = _parse_expiration(parsed.get("expires_at"), now, 3600)
            self._cached_token = token
            self._cached_expiration = expires
            return token


class GitHubRunnerRegistrationAPI:
    """Fixed, repository-selected GitHub runner registration/lifecycle API."""

    def __init__(
        self,
        *,
        owner: str,
        repository: str,
        repository_id: int,
        installation_token_provider: Callable[[], str],
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if (
            not isinstance(owner, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner) is None
            or owner in {".", ".."}
            or not isinstance(repository, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository) is None
            or repository in {".", ".."}
            or type(repository_id) is not int
            or repository_id <= 0
            or not callable(installation_token_provider)
            or not callable(now)
        ):
            raise SupervisorError("runner registration API configuration is invalid")
        self._owner = owner
        self._repository = repository
        self._repository_id = repository_id
        self._token_provider = installation_token_provider
        self._now = now
        self._base = f"{_GITHUB_API_ROOT}/repos/{owner}/{repository}/actions/runners"
        self._client = httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(8.0, connect=4.0),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def __repr__(self) -> str:
        return (
            f"GitHubRunnerRegistrationAPI(owner={self._owner!r}, repository={self._repository!r})"
        )

    def close(self) -> None:
        self._client.close()

    def create_ephemeral(self, owner_id: str) -> str:
        _validate_owner(owner_id)
        response = self._request("POST", self._base + "/registration-token", expected_status={201})
        token = response.get("token")
        now = _aware_utc(self._now())
        if not isinstance(token, str) or not _GITHUB_TOKEN.fullmatch(token):
            raise SupervisorError("GitHub runner registration response was invalid")
        _parse_expiration(response.get("expires_at"), now, 3600)
        return token

    def remove(self, owner_id: str) -> None:
        _validate_owner(owner_id)
        matches: list[int] = []
        total_count: int | None = None
        observed = 0
        for page in range(1, 11):
            response = self._request(
                "GET",
                self._base,
                expected_status={200},
                params={"per_page": 100, "page": page},
                body_limit=_MAX_HTTP_BODY,
            )
            runners = response.get("runners")
            count = response.get("total_count")
            if (
                not isinstance(runners, list)
                or len(runners) > 100
                or type(count) is not int
                or count < 0
                or count > 1000
                or (total_count is not None and count != total_count)
            ):
                raise SupervisorError("GitHub runner inventory is ambiguous")
            total_count = count
            observed += len(runners)
            if observed > total_count:
                raise SupervisorError("GitHub runner inventory is ambiguous")
            for runner in runners:
                if not isinstance(runner, Mapping):
                    raise SupervisorError("GitHub runner inventory is ambiguous")
                if runner.get("name") == owner_id:
                    runner_id = runner.get("id")
                    if type(runner_id) is not int or runner_id <= 0:
                        raise SupervisorError("GitHub runner inventory is ambiguous")
                    matches.append(runner_id)
            if observed >= total_count or len(runners) < 100:
                break
        else:
            raise SupervisorError("GitHub runner inventory is incomplete")
        if total_count is not None and observed < total_count:
            raise SupervisorError("GitHub runner inventory is incomplete")
        if not matches:
            return
        if len(matches) != 1:
            raise SupervisorError("GitHub runner identity is ambiguous")
        self._request(
            "DELETE",
            f"{self._base}/{matches[0]}",
            expected_status={204, 404},
            parse_json=False,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        expected_status: set[int],
        params: Mapping[str, str | int] | None = None,
        body_limit: int = 64 * 1024,
        parse_json: bool = True,
    ) -> dict[str, object]:
        try:
            token = self._token_provider()
            if not isinstance(token, str) or not _GITHUB_TOKEN.fullmatch(token):
                raise ValueError
            with self._client.stream(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            ) as response:
                if response.status_code not in expected_status:
                    raise SupervisorError("GitHub runner API request failed")
                if not parse_json:
                    return {}
                return _parse_http_json(_read_bounded_response(response, body_limit))
        except SupervisorError:
            raise
        except Exception:
            raise SupervisorError("GitHub runner API request failed") from None
        finally:
            self._client.cookies.clear()


class HTTPXOIDCRelayTransport:
    """Bounded fixed-origin OIDC relay; callers cannot choose any URL component."""

    def __init__(
        self,
        *,
        origin: str,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 5.0,
    ):
        if (
            not _valid_origin(origin)
            or type(timeout_seconds) not in (int, float)
            or not 0.1 <= timeout_seconds <= 15
        ):
            raise SupervisorError("fixed-origin relay configuration is invalid")
        self._origin = origin.rstrip("/")
        self._url = self._origin + _RELAY_PATH
        self._client = httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(float(timeout_seconds), connect=min(3.0, float(timeout_seconds))),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def post(self, url: str, payload: dict[str, str], bearer_token: str) -> Mapping[str, str]:
        if (
            url != self._url
            or not isinstance(payload, dict)
            or set(payload) != {"operation_id", "receipt_id"}
            or not isinstance(bearer_token, str)
            or not _OIDC_JWT.fullmatch(bearer_token)
        ):
            raise SupervisorError("fixed-origin relay request was rejected")
        try:
            request = RelayRequest(payload["operation_id"], payload["receipt_id"])
            with self._client.stream(
                "POST",
                self._url,
                headers={"Authorization": f"Bearer {bearer_token}"},
                json={"operation_id": request.operation_id, "receipt_id": request.receipt_id},
            ) as response:
                if response.status_code != 200:
                    raise SupervisorError("fixed-origin relay request failed")
                result = _parse_http_json(_read_bounded_response(response, 4096))
            if (
                set(result) != {"status", "receipt_id"}
                or result.get("status") not in {"accepted", "already_recorded"}
                or result.get("receipt_id") != request.receipt_id
            ):
                raise SupervisorError("fixed-origin relay response was invalid")
            return {"status": str(result["status"]), "receipt_id": request.receipt_id}
        except SupervisorError:
            raise
        except Exception:
            raise SupervisorError("fixed-origin relay request failed") from None
        finally:
            self._client.cookies.clear()


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    runtime_path: str
    runtime_name: str
    runtime_manifest_digest: str
    image: str
    runner_closure_digest: str
    cgroup_v2: bool
    resource_limits: bool
    read_only_root: bool
    disposable_scratch: bool
    egress_enforced: bool
    egress_network: str
    egress_destinations: tuple[str, ...]
    no_host_mounts: bool
    no_host_socket: bool
    no_host_namespaces: bool
    isolated_user_namespace: bool
    no_privilege: bool


@dataclass(frozen=True, slots=True)
class RelayRequest:
    operation_id: str
    receipt_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation_id, str)
            or not _RELAY_ID.fullmatch(self.operation_id)
            or not isinstance(self.receipt_id, str)
            or not _RELAY_ID.fullmatch(self.receipt_id)
        ):
            raise ValueError("invalid bounded relay identifiers")


@dataclass(frozen=True, slots=True)
class ExecResult:
    returncode: int
    stdout: str | bytes
    stderr: str | bytes


class ContainerAdapter(Protocol):
    def create(self, owner_id: str) -> str: ...

    def configure(self, container_id: str, registration_token: str) -> None: ...

    def start(self, container_id: str) -> None: ...

    def wait(self, container_id: str, timeout_seconds: int) -> int: ...

    def remove(self, owner_id: str) -> None: ...


class RegistrationAdapter(Protocol):
    def create_ephemeral(self, owner_id: str) -> str: ...

    def remove(self, owner_id: str) -> None: ...


class RelayTransport(Protocol):
    def post(self, url: str, payload: dict[str, str], bearer_token: str) -> Mapping[str, str]: ...


class OwnershipRegistry(Protocol):
    def locked(self) -> Iterator[None]: ...

    def list_owners(self) -> tuple[str, ...]: ...

    def reserve(self, owner_id: str) -> None: ...

    def release(self, owner_id: str) -> None: ...


class SQLiteRunnerOwnershipRegistry:
    """Durable one-runner ownership journal with an OS lock across the full lifecycle."""

    def __init__(self, database: str | Path):
        self._database = Path(database)
        if not self._database.is_absolute():
            raise ValueError("runner ownership database path must be absolute")
        self._lock_path = self._database.with_suffix(self._database.suffix + ".lock")
        self._prepare_paths()
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS owned_runner (owner_id TEXT PRIMARY KEY)"
            )

    @contextmanager
    def locked(self):
        descriptor = os.open(
            self._lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SupervisorError("another runner supervisor owns the lifecycle lock") from None
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def list_owners(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT owner_id FROM owned_runner ORDER BY owner_id"
            ).fetchall()
        owners = tuple(row[0] for row in rows)
        for owner_id in owners:
            _validate_owner(owner_id)
        if len(owners) > 1:
            raise SupervisorError("runner ownership journal contains multiple active owners")
        return owners

    def reserve(self, owner_id: str) -> None:
        _validate_owner(owner_id)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT owner_id FROM owned_runner LIMIT 1"
                ).fetchone()
                if existing is not None:
                    connection.rollback()
                    raise SupervisorError("a prior runner requires exact recovery")
                connection.execute("INSERT INTO owned_runner(owner_id) VALUES (?)", (owner_id,))
                connection.commit()
        except sqlite3.Error:
            raise SupervisorError("runner ownership could not be durably recorded") from None

    def release(self, owner_id: str) -> None:
        _validate_owner(owner_id)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM owned_runner WHERE owner_id = ?", (owner_id,))
                connection.commit()
        except sqlite3.Error:
            raise SupervisorError("runner ownership could not be cleared") from None

    def _prepare_paths(self) -> None:
        parent = self._database.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_info = parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode):
            raise ValueError("runner ownership directory must be a real directory")
        os.chmod(parent, 0o700)
        for path in (self._database, self._lock_path):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("runner ownership files must be regular files")
            os.chmod(path, 0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database, timeout=5, isolation_level=None)
        try:
            os.chmod(self._database, 0o600)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=DELETE")
            yield connection
        finally:
            connection.close()


Executor = Callable[..., ExecResult]


def _bounded_process_executor(
    argv: list[str], *, env: Mapping[str, str], timeout: int, input_data: bytes | None = None
) -> ExecResult:
    """Run a fixed argv and retain bounded output while draining both pipes."""
    try:
        process = subprocess.Popen(  # noqa: S603 - argv is assembled only by fixed adapters below.
            argv,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env=dict(env),
            shell=False,
            close_fds=True,
        )
    except OSError:
        raise SupervisorError("runner process could not be started") from None

    outputs: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}

    def drain(name: str, stream) -> None:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            remaining = _MAX_PROCESS_OUTPUT - len(outputs[name])
            if remaining > 0:
                outputs[name].extend(chunk[:remaining])

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        if input_data is not None:
            assert process.stdin is not None
            process.stdin.write(input_data)
            process.stdin.close()
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise SupervisorError("runner process exceeded its deadline") from None
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise SupervisorError("runner process operation failed") from None
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        for thread in threads:
            thread.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    return ExecResult(
        process.returncode,
        bytes(outputs["stdout"]),
        bytes(outputs["stderr"]),
    )


class DockerCLIContainerAdapter:
    """Fixed Docker CLI interface. It cannot accept caller-supplied commands or mounts."""

    def __init__(self, policy: SupervisorPolicy, *, execute: Executor | None = None):
        self._policy = policy
        self._execute = execute or _bounded_process_executor

    def create(self, owner_id: str) -> str:
        _validate_owner(owner_id)
        p = self._policy
        scratch = f"/tmp:rw,nosuid,nodev,exec,size={p.scratch_limit_bytes}"  # noqa: S108
        argv = [
            p.docker_binary,
            "create",
            "--name",
            owner_id,
            "--label",
            f"io.chatcore.p09-owner={owner_id}",
            "--runtime=runsc",
            f"--cpus={p.cpu_limit:g}",
            f"--memory={p.memory_limit_bytes}",
            f"--memory-swap={p.memory_limit_bytes}",
            f"--pids-limit={p.pid_limit}",
            "--read-only",
            f"--tmpfs={scratch}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            f"--user={p.runner_user}",
            f"--env=RELEASE_CONTROL_BROKER_ORIGIN={p.relay_origin}",
            f"--env=RELEASE_CONTROL_OIDC_AUDIENCE={p.oidc_audience}",
            f"--network={p.egress_network}",
            "--pid=private",
            "--ipc=private",
            "--uts=private",
            "--cgroupns=private",
            "--init",
            "--restart=no",
            "--pull=never",
            f"--entrypoint={p.runner_python_binary}",
            p.image,
            p.runner_entrypoint,
            "run-once",
            "--manifest",
            _RUNNER_IMAGE_MANIFEST_PATH,
            "--expected-closure",
            p.runner_closure_digest,
            "--runner-name",
            owner_id,
            "--repository-url",
            p.repository_url,
            "--timeout-seconds",
            str(p.lifetime_seconds),
        ]
        result = self._invoke(argv, timeout=p.lifetime_seconds)
        container_id = _stdout_text(result.stdout).strip()
        if result.returncode != 0 or not _CONTAINER_ID.fullmatch(container_id):
            raise SupervisorError("runner container creation failed")
        return container_id

    def configure(self, container_id: str, registration_token: str) -> None:
        _validate_container_id(container_id)
        if (
            not isinstance(registration_token, str)
            or not 1 <= len(registration_token) <= 4096
            or any(
                ord(character) < 0x21 or ord(character) > 0x7E for character in registration_token
            )
        ):
            raise SupervisorError("runner registration configuration was rejected")
        argv = [
            self._policy.docker_binary,
            "exec",
            "-i",
            f"--user={self._policy.runner_user}",
            container_id,
            *self._policy.registration_stdin_command,
        ]
        result = self._invoke(
            argv,
            timeout=min(30, self._policy.lifetime_seconds),
            input_data=registration_token.encode("ascii"),
        )
        if result.returncode != 0:
            raise SupervisorError("runner registration configuration failed")

    def start(self, container_id: str) -> None:
        _validate_container_id(container_id)
        result = self._invoke(
            [self._policy.docker_binary, "start", container_id],
            timeout=min(30, self._policy.lifetime_seconds),
        )
        if result.returncode != 0:
            raise SupervisorError("runner container start failed")

    def wait(self, container_id: str, timeout_seconds: int) -> int:
        _validate_container_id(container_id)
        if (
            type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= self._policy.lifetime_seconds
        ):
            raise SupervisorError("runner wait deadline was rejected")
        result = self._invoke(
            [self._policy.docker_binary, "wait", container_id], timeout=timeout_seconds
        )
        output = _stdout_text(result.stdout).strip()
        if result.returncode != 0 or not re.fullmatch(r"[0-9]{1,3}", output):
            raise SupervisorError("runner completion could not be determined")
        return int(output)

    def remove(self, owner_id: str) -> None:
        _validate_owner(owner_id)
        result = self._invoke(
            [self._policy.docker_binary, "rm", "--force", owner_id],
            timeout=min(30, self._policy.lifetime_seconds),
        )
        # Exact-owner removal is idempotent; Docker may report absent after a crash.
        if result.returncode != 0 and "No such container" not in _stdout_text(result.stderr):
            raise SupervisorError("owned runner container cleanup failed")

    def _invoke(
        self, argv: list[str], *, timeout: int, input_data: bytes | None = None
    ) -> ExecResult:
        try:
            result = self._execute(
                argv, env=dict(_FIXED_ENV), timeout=timeout, input_data=input_data
            )
        except SupervisorError:
            raise
        except BaseException:
            raise SupervisorError("runner process operation failed") from None
        if (
            not isinstance(result, ExecResult)
            or type(result.returncode) is not int
            or len(_stdout_bytes(result.stdout)) + len(_stdout_bytes(result.stderr))
            > self._policy.output_limit_bytes
        ):
            raise SupervisorError("runner process output exceeded policy")
        return result


def _stdout_bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    return b""


def _stdout_text(value: str | bytes) -> str:
    return _stdout_bytes(value).decode("utf-8", errors="replace")


def _validate_owner(value: str) -> None:
    if not isinstance(value, str) or not _OWNER_ID.fullmatch(value):
        raise SupervisorError("runner owner identity was rejected")


def _validate_container_id(value: str) -> None:
    if not isinstance(value, str) or not _CONTAINER_ID.fullmatch(value):
        raise SupervisorError("runner container identity was rejected")


class WholeRunnerSupervisor:
    """Admit exactly one isolated runner and always clean its exact owner identity."""

    def __init__(
        self,
        policy: SupervisorPolicy,
        *,
        containers: ContainerAdapter,
        registration: RegistrationAdapter,
        ownership: OwnershipRegistry,
        capability_probe: Callable[[SupervisorPolicy], CapabilitySnapshot],
        owner_id_factory: Callable[[], str] | None = None,
        relay_transport: RelayTransport | None = None,
        oidc_token_provider: Callable[[], str] | None = None,
    ):
        if not isinstance(policy, SupervisorPolicy):
            raise ValueError("installed supervisor policy is required")
        self._policy = policy
        self._containers = containers
        self._registration = registration
        self._ownership = ownership
        self._capability_probe = capability_probe
        self._owner_id_factory = owner_id_factory or (lambda: f"runner-{uuid.uuid4().hex}")
        self._relay_transport = relay_transport
        self._oidc_token_provider = oidc_token_provider

    def run_once(self) -> None:
        with self._ownership.locked():
            self._recover_locked()
            self._verify_capabilities()
            owner_id = self._owner_id_factory()
            _validate_owner(owner_id)
            self._ownership.reserve(owner_id)
            primary_failed = False
            try:
                container_id = self._containers.create(owner_id)
                _validate_container_id(container_id)
                registration_token = self._registration.create_ephemeral(owner_id)
                if (
                    not isinstance(registration_token, str)
                    or not 1 <= len(registration_token) <= 4096
                ):
                    raise SupervisorError("ephemeral runner registration was rejected")
                # Start only the fixed bootstrap; it must wait for configuration before jobs.
                self._containers.start(container_id)
                self._containers.configure(container_id, registration_token)
                # Drop the local reference before accepting any job; never return or log it.
                registration_token = ""
                exit_code = self._containers.wait(container_id, self._policy.lifetime_seconds)
                if type(exit_code) is not int or exit_code != 0:
                    raise SupervisorError("single-job runner completed unsuccessfully")
            except BaseException:
                primary_failed = True
            cleanup_errors = self._cleanup_owner(owner_id)
            if not cleanup_errors:
                try:
                    self._ownership.release(owner_id)
                except BaseException:
                    cleanup_errors.append("ownership journal")
            if primary_failed or cleanup_errors:
                detail = "; cleanup incomplete" if cleanup_errors else ""
                raise SupervisorError(f"single-job runner failed{detail}") from None

    def recover(self) -> None:
        """Retry cleanup for the one exact owner durably recorded before launch."""
        with self._ownership.locked():
            self._recover_locked()

    def _recover_locked(self) -> None:
        try:
            owners = self._ownership.list_owners()
        except BaseException:
            raise SupervisorError("runner ownership recovery is unavailable") from None
        for owner_id in owners:
            _validate_owner(owner_id)
            errors = self._cleanup_owner(owner_id)
            if errors:
                raise SupervisorError("prior runner cleanup requires recovery") from None
            try:
                self._ownership.release(owner_id)
            except BaseException:
                raise SupervisorError("prior runner ownership remains unresolved") from None

    def _cleanup_owner(self, owner_id: str) -> list[str]:
        errors: list[str] = []
        try:
            # Remove by generated owner name, including an ambiguous create outcome.
            self._containers.remove(owner_id)
        except BaseException:
            errors.append("container")
        try:
            # Exact-owner removal handles ambiguous registration creation and retries.
            self._registration.remove(owner_id)
        except BaseException:
            errors.append("registration")
        return errors

    def relay(self, request: RelayRequest) -> dict[str, str]:
        if (
            not isinstance(request, RelayRequest)
            or self._relay_transport is None
            or self._oidc_token_provider is None
        ):
            raise SupervisorError("fixed-origin relay is unavailable")
        try:
            token = self._oidc_token_provider()
            if not isinstance(token, str) or not _OIDC_JWT.fullmatch(token):
                raise ValueError
            response = self._relay_transport.post(
                f"{self._policy.relay_origin}{_RELAY_PATH}",
                {"operation_id": request.operation_id, "receipt_id": request.receipt_id},
                token,
            )
            if (
                not isinstance(response, Mapping)
                or set(response) != {"status", "receipt_id"}
                or response.get("status") not in {"accepted", "already_recorded"}
                or response.get("receipt_id") != request.receipt_id
            ):
                raise ValueError
            return {"status": response["status"], "receipt_id": request.receipt_id}
        except BaseException:
            raise SupervisorError("fixed-origin relay failed") from None

    def _verify_capabilities(self) -> None:
        try:
            snapshot = self._capability_probe(self._policy)
        except BaseException:
            raise SupervisorError("required runner isolation capability is unavailable") from None
        if not isinstance(snapshot, CapabilitySnapshot):
            raise SupervisorError("required runner isolation capability is unavailable")
        p = self._policy
        if (
            snapshot.runtime_path != p.runtime_path
            or snapshot.runtime_name != "runsc"
            or snapshot.runtime_name != p.runtime_name
            or snapshot.runtime_manifest_digest != p.runtime_manifest_digest
            or snapshot.image != p.image
            or snapshot.runner_closure_digest != p.runner_closure_digest
            or snapshot.egress_network != p.egress_network
            or snapshot.egress_destinations != p.egress_destinations
            or not all(
                (
                    snapshot.cgroup_v2,
                    snapshot.resource_limits,
                    snapshot.read_only_root,
                    snapshot.disposable_scratch,
                    snapshot.egress_enforced,
                    snapshot.no_host_mounts,
                    snapshot.no_host_socket,
                    snapshot.no_host_namespaces,
                    snapshot.isolated_user_namespace,
                    snapshot.no_privilege,
                )
            )
        ):
            raise SupervisorError("required runner isolation capability is unavailable")


def render_runner_image_recipe(manifest: RunnerImageManifest, *, python_binary: str) -> str:
    """Render a digest-only recipe; it does not fetch sources or build an image."""
    if (
        not isinstance(manifest, RunnerImageManifest)
        or not _safe_host_path(python_binary)
        or re.fullmatch(r"/[A-Za-z0-9._/-]+", python_binary) is None
        or Path(python_binary).name not in {"python3", "python3.12"}
        or manifest.runner_root != "/opt/p09/runner-files"
        or manifest.runner_binary != "config.sh"
        or manifest.runner_run_script != "run.sh"
        or manifest.registration_protocol != "actions-runner-config-prompt-v1"
        or compute_runner_closure_digest(
            {
                "base_image": manifest.base_image,
                "runner_archive_sha256": manifest.runner_archive_sha256,
                "bootstrap_sha256": manifest.bootstrap_sha256,
                "relay_helper_sha256": manifest.relay_helper_sha256,
                "runner_binary": manifest.runner_binary,
                "runner_run_script": manifest.runner_run_script,
                "runner_version": manifest.runner_version,
                "runner_files": dict(manifest.runner_files),
                "registration_protocol": manifest.registration_protocol,
            }
        )
        != manifest.runner_closure_sha256
    ):
        raise SupervisorError("runner image recipe inputs are incomplete or unpinned")
    if len(manifest.runner_files) > 4096:
        raise SupervisorError("runner image recipe contains too many files")
    return "\n".join(
        (
            f"FROM {manifest.base_image}",
            "USER 0:0",
            "COPY runner-files/ /opt/p09/runner-files/",
            "COPY runner-image-manifest.json /opt/p09/runner-image-manifest.json",
            "COPY runner_bootstrap.py /opt/p09/runner_bootstrap.py",
            "COPY runner_relay.py /opt/p09/runner_relay.py",
            "RUN chmod -R a-w /opt/p09/runner-files && \\",
            "    chmod a-w /opt/p09/runner_relay.py && \\",
            f"    {python_binary} /opt/p09/runner_bootstrap.py verify-image \\",
            "      --manifest /opt/p09/runner-image-manifest.json",
            "LABEL io.chatcore.runner-closure-sha256=" + manifest.runner_closure_sha256,
            "LABEL io.chatcore.runner-version=" + manifest.runner_version,
            "LABEL io.chatcore.runner-archive-sha256=" + manifest.runner_archive_sha256,
            "USER 10001:10001",
            f'ENTRYPOINT ["{python_binary}", "{_RUNNER_BOOTSTRAP_PATH}"]',
            "",
        )
    )


def run_installed_once(
    config: InstalledSupervisorConfig,
    *,
    process_executor: Executor | None = None,
    api_transport: httpx.BaseTransport | None = None,
    protected_reader: Callable[[str | Path, int], bytes] = _read_protected_file,
    expected_owner_uid: int = 0,
) -> None:
    """Construct production adapters from the checked installed policy and run one job."""
    token_provider = GitHubRunnerAppInstallationTokenProvider(
        principal_role=GitHubAppRole.RUNNER_REGISTRATION,
        app_roles=config.github_app_roles,
        app_id=config.github_app_id,
        installation_id=config.github_installation_id,
        repository_id=config.repository_id,
        private_key_provider=lambda: protected_reader(
            config.github_app_private_key_path, 64 * 1024
        ),
        transport=api_transport,
    )
    registration = GitHubRunnerRegistrationAPI(
        owner=config.repository_owner,
        repository=config.repository_name,
        repository_id=config.repository_id,
        installation_token_provider=token_provider,
        transport=api_transport,
    )
    supervisor = WholeRunnerSupervisor(
        config.policy,
        containers=DockerCLIContainerAdapter(config.policy, execute=process_executor),
        registration=registration,
        ownership=SQLiteRunnerOwnershipRegistry(config.ownership_database),
        capability_probe=InstalledCapabilityProbe(
            config,
            execute=process_executor,
            protected_reader=lambda path, limit: _read_root_owned_integrity_file(
                path, limit, expected_uid=expected_owner_uid
            ),
            expected_owner_uid=expected_owner_uid,
        ),
    )
    try:
        supervisor.run_once()
    finally:
        registration.close()
        token_provider.close()


def _parse_cli(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="chatcore-runner-supervisor")
    commands = parser.add_subparsers(dest="command", required=True)
    run_once = commands.add_parser("run-once")
    run_once.add_argument("--policy", required=True)
    image = commands.add_parser("render-image-recipe")
    image.add_argument("--manifest", required=True)
    image.add_argument("--python-binary", required=True)
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    config_loader: Callable[[str], InstalledSupervisorConfig] = load_installed_config,
    service_runner: Callable[[InstalledSupervisorConfig], None] = run_installed_once,
    output_writer: Callable[[str], None] = print,
) -> int:
    try:
        args = _parse_cli(argv)
        if args.command == "render-image-recipe":
            manifest = load_runner_manifest(args.manifest, expected_uid=os.getuid())
            output_writer(render_runner_image_recipe(manifest, python_binary=args.python_binary))
            return 0
        config = config_loader(args.policy)
        service_runner(config)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
