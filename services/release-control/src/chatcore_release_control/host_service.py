"""Protected host deployment orchestration.

The controller accepts release identifiers only. Repository, build recipe, runtime
definition, credentials and resource policy are installed operator configuration.
All production effects are behind explicit adapters so local tests can use fakes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .baseline import build_baseline_bundle
from .git_evidence import GitEvidenceError, collect_release_bundle
from .host_journal import (
    HostJournal,
    HostJournalError,
    JournalRecord,
    ReconciliationAction,
    ReconciliationRequired,
)
from .monorepo_harness import prepare_dependency_cache
from .policy import DeploymentIdentity, JournalState, PolicyManifest

_APPROVAL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_OPERATION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_POLICY_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REVIEWED_SOURCE_EXCEPTIONS = frozenset({"apps/sb_bot/deploy/backup.sh"})
_DEFAULT_DOCKER_ENDPOINT = "unix:///var/run/docker.sock"
_HOST_OPERATION_STATES = frozenset(
    {"prepared", "admitted", "started", "healthy", "restored_healthy", "recovery_failed"}
)


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


class HostServiceError(RuntimeError):
    """Fixed host-service error code safe to return over the local socket."""

    def __init__(self, code: str = "host_service_denied") -> None:
        allowed = {
            "host_service_denied",
            "invalid_request",
            "invalid_configuration",
            "broker_denied",
            "baseline_mismatch",
            "build_denied",
            "deployment_mismatch",
            "health_failed",
            "recovery_failed",
            "reconciliation_required",
        }
        self.code = code if code in allowed else "host_service_denied"
        super().__init__(self.code)


@dataclass(frozen=True)
class HostDeploymentContext:
    approval_id: str
    target: str
    sequence: int
    dispatch_fence: int
    status: str
    merge_commit: str
    policy_digest: str
    deployed_identity: DeploymentIdentity
    payload: DeploymentIdentity | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    head_tree: str | None = None
    expected_merge_tree: str | None = None
    merge_tree: str | None = None
    merge_parents: tuple[str, str] | None = None
    host_operation_id: str | None = None
    host_operation_payload_digest: str | None = None
    host_operation_state: str | None = None
    host_operation_start_deadline: int | None = None
    host_operation_recovery_deadline: int | None = None
    host_operation_receipt_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.approval_id, str)
            or not _APPROVAL_ID.fullmatch(self.approval_id)
            or not isinstance(self.target, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", self.target)
            or type(self.sequence) is not int
            or self.sequence <= 0
            or type(self.dispatch_fence) is not int
            or self.dispatch_fence <= 0
            or self.status
            not in {
                "merged_verified",
                "deploying",
                "restoring",
                "healthy",
                "restored_healthy",
                "recovery_failed",
            }
            or not isinstance(self.merge_commit, str)
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.merge_commit)
            or not isinstance(self.policy_digest, str)
            or not _POLICY_DIGEST.fullmatch(self.policy_digest)
            or not isinstance(self.deployed_identity, DeploymentIdentity)
            or not self._valid_merge_binding()
            or not self._valid_host_operation_binding()
            or (
                self.payload is not None
                and (
                    not isinstance(self.payload, DeploymentIdentity)
                    or self.payload.git_sha != self.merge_commit
                    or self.payload.policy_digest != self.policy_digest
                    or self.payload.target_sequence != self.sequence
                    or self.payload.config_generation != self.deployed_identity.config_generation
                    or self.payload.config_digest != self.deployed_identity.config_digest
                )
            )
        ):
            raise HostServiceError("invalid_request")

    def _valid_merge_binding(self) -> bool:
        values = (
            self.base_sha,
            self.head_sha,
            self.head_tree,
            self.expected_merge_tree,
            self.merge_tree,
        )
        if all(value is None for value in values) and self.merge_parents is None:
            return True
        return (
            all(
                isinstance(value, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value)
                for value in values
            )
            and len({len(value) for value in values if isinstance(value, str)}) == 1
            and isinstance(self.merge_parents, tuple)
            and len(self.merge_parents) == 2
            and self.merge_parents == (self.base_sha, self.head_sha)
            and self.expected_merge_tree == self.merge_tree
        )

    def _valid_host_operation_binding(self) -> bool:
        fields = (
            self.host_operation_id,
            self.host_operation_payload_digest,
            self.host_operation_state,
            self.host_operation_start_deadline,
            self.host_operation_recovery_deadline,
            self.host_operation_receipt_id,
        )
        if all(value is None for value in fields):
            return True
        if (
            not isinstance(self.host_operation_id, str)
            or not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                self.host_operation_id,
            )
            or self.payload is None
            or not isinstance(self.host_operation_payload_digest, str)
            or not _POLICY_DIGEST.fullmatch(self.host_operation_payload_digest)
            or self.host_operation_state not in _HOST_OPERATION_STATES
        ):
            return False
        if (self.host_operation_start_deadline is None) != (
            self.host_operation_recovery_deadline is None
        ):
            return False
        if self.host_operation_state == "prepared":
            if self.host_operation_start_deadline is not None:
                return False
        elif self.host_operation_start_deadline is None:
            return False
        if self.host_operation_start_deadline is not None and (
            type(self.host_operation_start_deadline) is not int
            or self.host_operation_start_deadline < 0
            or type(self.host_operation_recovery_deadline) is not int
            or self.host_operation_recovery_deadline <= self.host_operation_start_deadline
        ):
            return False
        terminal = self.host_operation_state in {
            "healthy",
            "restored_healthy",
            "recovery_failed",
        }
        return (self.host_operation_receipt_id is not None) == terminal and (
            self.host_operation_receipt_id is None
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.host_operation_receipt_id) is not None
        )


class BrokerAdapter(Protocol):
    def get_context(self, approval_id: str) -> HostDeploymentContext: ...

    def prepare(
        self,
        approval_id: str,
        fence: int,
        operation_id: str,
        baseline: DeploymentIdentity,
        payload: DeploymentIdentity,
    ) -> object: ...

    def admit(self, approval_id: str, fence: int, operation_id: str) -> object: ...

    def started(self, approval_id: str, fence: int, operation_id: str) -> object: ...

    def restore_started(self, approval_id: str, fence: int, operation_id: str) -> object: ...

    def receipt(
        self, approval_id: str, fence: int, operation_id: str, record: JournalRecord
    ) -> object: ...


class EvidenceAdapter(Protocol):
    def verify(self, repository: Path, context: HostDeploymentContext) -> bool: ...


class P01HostEvidence:
    """Independently verify exact D→M Git evidence under the installed policy."""

    _MIGRATION_PARTS = frozenset({"migrations", "migration", "versions"})

    def __init__(
        self, policy: PolicyManifest, migration_path_prefixes: tuple[str, ...] = ()
    ) -> None:
        if (
            not isinstance(policy, PolicyManifest)
            or not isinstance(migration_path_prefixes, tuple)
            or len(set(migration_path_prefixes)) != len(migration_path_prefixes)
            or any(
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or ".." in path.split("/")
                or "\\" in path
                for path in migration_path_prefixes
            )
        ):
            raise HostServiceError("invalid_configuration")
        self.policy = policy
        self.migration_path_prefixes = migration_path_prefixes

    def verify(self, repository: Path, context: HostDeploymentContext) -> bool:
        if (
            not isinstance(repository, Path)
            or not isinstance(context, HostDeploymentContext)
            or context.policy_digest != self.policy.digest
            or context.target != self.policy.target
            or context.base_sha is None
            or context.head_sha is None
            or context.head_tree is None
            or context.expected_merge_tree is None
            or context.merge_tree is None
            or context.merge_parents is None
        ):
            return False
        try:
            merge_evidence = collect_release_bundle(
                repository,
                context.deployed_identity.git_sha,
                context.deployed_identity.git_sha,
                context.merge_commit,
                self.policy,
            )
            merge_tree, merge_parents = _inspect_raw_merge_commit(repository, context.merge_commit)
            head_tree, _ = _inspect_raw_commit(repository, context.head_sha)
            deployed_to_base = _is_commit_ancestor(
                repository, context.deployed_identity.git_sha, context.base_sha
            )
            deployed_to_head = _is_commit_ancestor(
                repository, context.deployed_identity.git_sha, context.head_sha
            )
        except (GitEvidenceError, OSError, ValueError):
            return False
        if (
            not merge_evidence.base_is_ancestor_of_head
            or not merge_evidence.base_is_descendant_of_deployed
            or merge_evidence.deployed_commit != context.deployed_identity.git_sha
            or merge_evidence.head_commit != context.merge_commit
            or merge_evidence.policy_digest != context.policy_digest
            or not deployed_to_base
            or not deployed_to_head
            or head_tree != context.head_tree
            or merge_tree != context.expected_merge_tree
            or merge_tree != context.merge_tree
            or merge_parents != (context.base_sha, context.head_sha)
            or merge_parents != context.merge_parents
        ):
            return False
        # The initial service refuses all migration-set changes. This is stricter than
        # allowing a migration and avoids installing an unauthenticated manifest.
        for change in merge_evidence.deployed_to_head:
            components = {part.casefold() for part in change.path.split("/")}
            if (
                components & self._MIGRATION_PARTS
                or any("migration" in part for part in components)
                or any(
                    change.path == prefix or change.path.startswith(prefix.rstrip("/") + "/")
                    for prefix in self.migration_path_prefixes
                )
            ):
                return False
        return True


def _inspect_raw_merge_commit(repository: Path, merge_commit: str) -> tuple[str, tuple[str, ...]]:
    """Read the exact raw M tree and parent list without invoking repository helpers."""
    tree, parents = _inspect_raw_commit(repository, merge_commit)
    if len(parents) != 2:
        raise GitEvidenceError("invalid_git_output")
    return tree, parents


def _inspect_raw_commit(repository: Path, commit: str) -> tuple[str, tuple[str, ...]]:
    git = shutil.which("git")
    if git is None:
        raise GitEvidenceError("invalid_repository")
    try:
        result = subprocess.run(  # noqa: S603 - fixed Git executable; validated commit IDs only
            (
                git,
                "-C",
                str(repository),
                "--no-pager",
                "--no-replace-objects",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "show",
                "-s",
                "--format=%T%n%P",
                commit,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={
                "PATH": str(Path(git).parent),
                "HOME": "/var/empty",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "LC_ALL": "C",
            },
            timeout=8,
            check=False,
            close_fds=True,
        )
        if result.returncode != 0 or len(result.stdout) > 4096:
            raise GitEvidenceError("missing_object")
        fields = result.stdout.decode("ascii", errors="strict").strip().splitlines()
        if len(fields) != 2:
            raise GitEvidenceError("invalid_git_output")
        tree = fields[0]
        parents = tuple(fields[1].split())
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", tree) or any(
            not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", item) for item in parents
        ):
            raise GitEvidenceError("invalid_git_output")
        return tree, parents
    except GitEvidenceError:
        raise
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        raise GitEvidenceError("invalid_git_output") from None


def _is_commit_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    git = shutil.which("git")
    if git is None:
        raise GitEvidenceError("invalid_repository")
    try:
        result = subprocess.run(  # noqa: S603 - fixed Git executable; validated commit IDs only
            (
                git,
                "-C",
                str(repository),
                "--no-replace-objects",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": str(Path(git).parent), "GIT_CONFIG_NOSYSTEM": "1"},
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GitEvidenceError("invalid_repository") from None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise GitEvidenceError("invalid_repository")


class BuildAdapter(Protocol):
    def build(self, repository: Path, context: HostDeploymentContext) -> str: ...


class RuntimeAdapter(Protocol):
    def observe(self) -> DeploymentIdentity | None: ...

    def switch(self, identity: DeploymentIdentity) -> None: ...

    def restore(self, identity: DeploymentIdentity) -> None: ...

    def healthy(self, identity: DeploymentIdentity) -> bool: ...

    def smoke_test(self, identity: DeploymentIdentity) -> bool: ...


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes = b""


class CommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> CommandResult: ...


class SourceAdapter(Protocol):
    def fetch(self, commit: str, baseline_commit: str): ...


class SubprocessCommandRunner:
    """Fixed-argv runner used only for installed host commands."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout_seconds: int,
        max_output_bytes: int,
        workspace_directory: Path | None = None,
        max_workspace_bytes: int | None = None,
    ) -> CommandResult:
        if (
            not isinstance(argv, tuple)
            or not argv
            or any(not isinstance(item, str) or not item or "\0" in item for item in argv)
            or type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= 600
            or type(max_output_bytes) is not int
            or not 1 <= max_output_bytes <= 1024 * 1024
            or not isinstance(env, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()
            )
            or (workspace_directory is None) != (max_workspace_bytes is None)
            or (
                workspace_directory is not None
                and (
                    not isinstance(workspace_directory, Path)
                    or type(max_workspace_bytes) is not int
                    or max_workspace_bytes < 1
                )
            )
        ):
            raise HostServiceError("invalid_configuration")
        process = None
        try:
            process = subprocess.Popen(  # noqa: S603 - caller supplies only fixed installed argv adapters
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                close_fds=True,
                start_new_session=True,
                bufsize=0,
            )
            assert process.stdout is not None
            output = bytearray()
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout_seconds
            while selector.get_map():
                if (
                    workspace_directory is not None
                    and _directory_size(workspace_directory, max_workspace_bytes)
                    > max_workspace_bytes
                ):
                    raise HostServiceError("host_service_denied")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HostServiceError("host_service_denied")
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fd, min(8192, max_output_bytes - len(output) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > max_output_bytes:
                        raise HostServiceError("host_service_denied")
            returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            if (
                workspace_directory is not None
                and _directory_size(workspace_directory, max_workspace_bytes) > max_workspace_bytes
            ):
                raise HostServiceError("host_service_denied")
            return CommandResult(returncode, bytes(output))
        except HostServiceError:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
        except (OSError, subprocess.TimeoutExpired):
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise HostServiceError("host_service_denied") from None


def _directory_size(path: Path, maximum: int) -> int:
    total = 0
    try:
        pending = [path]
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
                        if total > maximum:
                            return total
        return total
    except OSError:
        raise HostServiceError("host_service_denied") from None


@dataclass(frozen=True)
class GitSourceConfig:
    repository_url: str
    git_path: str
    credential_file: Path | None
    scratch_directory: Path
    timeout_seconds: int = 120
    max_output_bytes: int = 64 * 1024
    max_repository_bytes: int = 1024 * 1024 * 1024
    token_provider: Callable[[], str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.repository_url, str)
            or not re.fullmatch(
                r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", self.repository_url
            )
            or not isinstance(self.git_path, str)
            or not Path(self.git_path).is_absolute()
            or (self.credential_file is None) == (self.token_provider is None)
            or (
                self.credential_file is not None
                and (
                    not isinstance(self.credential_file, Path)
                    or not self.credential_file.is_absolute()
                )
            )
            or (self.token_provider is not None and not callable(self.token_provider))
            or not isinstance(self.scratch_directory, Path)
            or not self.scratch_directory.is_absolute()
            or type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 600
            or type(self.max_output_bytes) is not int
            or not 1024 <= self.max_output_bytes <= 1024 * 1024
            or type(self.max_repository_bytes) is not int
            or not 64 * 1024 * 1024 <= self.max_repository_bytes <= 8 * 1024 * 1024 * 1024
        ):
            raise HostServiceError("invalid_configuration")


class GitExactCommitSource:
    """Fetch one exact commit from an installed HTTPS repository using host credentials."""

    def __init__(
        self, config: GitSourceConfig, runner: CommandRunner, *, close_resources=()
    ) -> None:
        if not isinstance(config, GitSourceConfig) or not callable(getattr(runner, "run", None)):
            raise HostServiceError("invalid_configuration")
        self.config = config
        self.runner = runner
        self._close_resources = close_resources

    def close(self) -> None:
        for resource in self._close_resources:
            resource.close()

    def __repr__(self) -> str:
        return "GitExactCommitSource(credentials=[REDACTED])"

    @contextmanager
    def fetch(self, commit: str, baseline_commit: str):
        if (
            not isinstance(commit, str)
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit)
            or not isinstance(baseline_commit, str)
            or len(baseline_commit) != len(commit)
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", baseline_commit)
        ):
            raise HostServiceError("invalid_request")
        try:
            token = (
                self.config.token_provider()
                if self.config.token_provider
                else BrokerCredential.read(self.config.credential_file)
            )
            if (
                not isinstance(token, str)
                or re.fullmatch(r"[A-Za-z0-9_.,-]{1,4096}", token) is None
            ):
                raise ValueError
        except Exception:
            raise HostServiceError("build_denied") from None
        try:
            self.config.scratch_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.config.scratch_directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise HostServiceError("invalid_configuration")
            with tempfile.TemporaryDirectory(
                prefix="host-source-", dir=self.config.scratch_directory
            ) as temporary:
                root = Path(temporary)
                repository = root / "source.git"
                askpass = root / "askpass"
                token_file = root / "git-token"
                descriptor = os.open(
                    token_file,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as credential_output:
                    credential_output.write(token.encode("ascii"))
                    credential_output.flush()
                    os.fsync(credential_output.fileno())
                askpass.write_text(
                    '#!/bin/sh\ncase "$1" in\n'
                    "*Username*) printf '%s' x-access-token;;\n"
                    '*Password*) exec /bin/cat "$CHATCORE_GIT_TOKEN_FILE";;\n'
                    "*) exit 1;;\nesac\n",
                    encoding="ascii",
                )
                os.chmod(askpass, 0o700)
                env = {
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(root),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_ASKPASS": str(askpass),
                    "CHATCORE_GIT_TOKEN_FILE": str(token_file),
                }
                self._git(("init", "--bare", str(repository)), env)
                self._git(
                    (
                        "--git-dir",
                        str(repository),
                        "remote",
                        "add",
                        "origin",
                        self.config.repository_url,
                    ),
                    env,
                )
                self._git(
                    (
                        "--git-dir",
                        str(repository),
                        "fetch",
                        "--no-tags",
                        "--no-recurse-submodules",
                        "origin",
                        commit,
                        baseline_commit,
                    ),
                    env,
                    workspace=repository,
                )
                self._git(
                    ("--git-dir", str(repository), "cat-file", "-e", f"{commit}^{{commit}}"),
                    env,
                    workspace=repository,
                )
                yield repository
        except HostServiceError:
            raise
        except (OSError, RuntimeError):
            raise HostServiceError("build_denied") from None
        finally:
            token = ""

    def _git(
        self,
        args: tuple[str, ...],
        env: dict[str, str],
        *,
        workspace: Path | None = None,
    ) -> None:
        argv = (self.config.git_path, "-c", "http.followRedirects=false", *args)
        if isinstance(self.runner, SubprocessCommandRunner) and workspace is not None:
            result = self.runner.run(
                argv,
                env=env,
                timeout_seconds=self.config.timeout_seconds,
                max_output_bytes=self.config.max_output_bytes,
                workspace_directory=workspace,
                max_workspace_bytes=self.config.max_repository_bytes,
            )
        else:
            result = self.runner.run(
                argv,
                env=env,
                timeout_seconds=self.config.timeout_seconds,
                max_output_bytes=self.config.max_output_bytes,
            )
        if result.returncode != 0:
            raise HostServiceError("build_denied")
        if (
            isinstance(self.runner, SubprocessCommandRunner)
            and workspace is not None
            and _directory_size(workspace, self.config.max_repository_bytes)
            > self.config.max_repository_bytes
        ):
            raise HostServiceError("build_denied")


class BrokerCredential:
    """Shared mode/owner-checked host credential reader with no repr leakage."""

    @staticmethod
    def read(path: Path) -> str:
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > 4096
            ):
                raise HostServiceError("invalid_configuration")
            token = os.read(descriptor, 4097).decode("ascii", errors="strict").strip()
            if not re.fullmatch(r"[A-Za-z0-9._~-]{32,2048}", token):
                raise HostServiceError("invalid_configuration")
            return token
        except HostServiceError:
            raise
        except (OSError, UnicodeError):
            raise HostServiceError("invalid_configuration") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)


@dataclass(frozen=True)
class FixedComposeConfig:
    docker_path: str
    compose_file: Path
    runtime_env_file: Path
    app_env_file: Path
    config_manifest: Path
    config_files: tuple[Path, ...]
    docker_config: Path
    image_repository: str
    project: str = "sb-bot"
    service: str = "sb-bot"
    container: str = "sb-bot"
    command_timeout_seconds: int = 120
    max_output_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if (
            not isinstance(self.docker_path, str)
            or not Path(self.docker_path).is_absolute()
            or not isinstance(self.compose_file, Path)
            or not self.compose_file.is_absolute()
            or not isinstance(self.runtime_env_file, Path)
            or not self.runtime_env_file.is_absolute()
            or not isinstance(self.app_env_file, Path)
            or not self.app_env_file.is_absolute()
            or not isinstance(self.config_manifest, Path)
            or not self.config_manifest.is_absolute()
            or not isinstance(self.config_files, tuple)
            or not self.config_files
            or any(
                not isinstance(path, Path) or not path.is_absolute() for path in self.config_files
            )
            or len(set(self.config_files)) != len(self.config_files)
            or tuple(sorted(self.config_files)) != self.config_files
            or not isinstance(self.docker_config, Path)
            or not self.docker_config.is_absolute()
            or not isinstance(self.image_repository, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9._/-]{0,127}", self.image_repository)
            or self.project != "sb-bot"
            or self.service != "sb-bot"
            or self.container != "sb-bot"
            or self.compose_file not in self.config_files
            or self.runtime_env_file not in self.config_files
            or self.app_env_file not in self.config_files
            or type(self.command_timeout_seconds) is not int
            or not 1 <= self.command_timeout_seconds <= 600
            or type(self.max_output_bytes) is not int
            or not 1024 <= self.max_output_bytes <= 1024 * 1024
        ):
            raise HostServiceError("invalid_configuration")


class FixedComposeRuntime:
    """Docker/Compose adapter bound to one installed service definition."""

    _IDENTITY_LABEL = "org.chatcore.release.identity"
    _APP_ENV_KEYS = frozenset(
        {
            "BOT_NAME",
            "ALLOWED_CHATS",
            "ADMIN_USERS",
            "OWNER_USERS",
            "PG_DSN",
            "VALKEY_URL",
            "CACHE_PREFIX",
            "RECENT_CONTEXT_MESSAGES",
            "LOG_LEVEL",
            "VALKEY_PASSWORD",
            "ES_CA_FINGERPRINT",
            "EMBED_CACHE_DIR",
            "HEARTBEAT_FILE",
            "BOT_DISPLAY_NAME",
            "CONVERSATION_CONTINUE_MINUTES",
            "LLM_PROVIDER",
            "LLM_BASE_URL",
            "LLM_MODEL",
            "LLM_API_KEY",
            "LLM_MAX_TOKENS",
            "LLM_TIMEOUT_S",
            "LLM_VISION",
            "LLM_EFFORT",
            "LLM_ANTHROPIC_FALLBACKS",
            "VISION_PROVIDER",
            "VISION_BASE_URL",
            "VISION_MODEL",
            "VISION_API_KEY",
            "VISION_MAX_TOKENS",
            "VISION_TIMEOUT_S",
            "ES_URL",
            "ES_API_KEY",
            "SEARCH_INDEX_PREFIX",
            "TAVILY_API_KEY",
            "WEB_MAX_RESULTS",
            "WEB_FETCH_MAX_BYTES",
            "WEB_FETCH_TIMEOUT_S",
            "WEB_FETCH_MAX_CHARS",
            "WEB_MAX_FETCHES_PER_TURN",
            "STOCKS_API_BASE",
            "NET_GUARD_EXTRA_BLOCKED",
            "RATE_USER_PER_HOUR",
            "RATE_CHAT_PER_DAY",
            "DAILY_TOKEN_BUDGET",
            "BUDGET_TZ",
            "BROWSER_RENDER_URL",
            "BROWSE_DM_ENABLED",
            "BROWSE_REQUEST_TIMEOUT_S",
            "BROWSE_MAX_CHARS",
            "BROWSE_MAX_RENDERS_PER_TURN",
            "BROWSE_MAX_FETCHES_PER_TURN",
            "BROWSE_CACHE_TTL_S",
            "BROWSE_MAX_RESPONSE_BYTES",
            "MEMORY_RECALL_K",
            "MEMORY_PINNED_MAX",
            "MEMORY_AUTO_EXTRACT",
            "MEMORY_DEDUPE_THRESHOLD",
            "MEMORY_MAX_CHARS",
            "RESEARCH_PER_USER_PER_DAY",
            "RESEARCH_PER_CHAT_PER_DAY",
            "RESEARCH_INLINE_MAX_SEARCHES",
            "RESEARCH_INLINE_MAX_FETCHES",
            "RESEARCH_INLINE_TIMEOUT_S",
            "RESEARCH_TOOL_TIMEOUT_S",
            "RESEARCH_INLINE_TOKEN_CEILING",
            "RESEARCH_LONG_MAX_SEARCHES",
            "RESEARCH_LONG_MAX_FETCHES",
            "RESEARCH_LONG_TIMEOUT_S",
            "RESEARCH_LONG_TOKEN_CEILING",
            "RESEARCH_MAX_ATTEMPTS",
            "RESEARCH_PAGE_CHARS",
            "RESEARCH_MAX_TOKENS",
            "RESEARCH_POLL_SECONDS",
            "RESEARCH_WORKER_CONCURRENCY",
            "RESEARCH_RETENTION_DAYS",
            "RESEARCH_TOPIC_MAX_CHARS",
            "POLLS_MAX_OPTIONS",
            "POLLS_MAX_OPEN_PER_CHAT",
            "POLLS_MAX_OPEN_PER_USER",
            "POLLS_CLOSE_HORIZON_DAYS",
            "POLLS_MIN_CLOSE_LEAD_SECONDS",
            "POLLS_SCAN_SECONDS",
            "POLLS_LEASE_SECONDS",
            "POLLS_MAX_CLOSE_FAILURES",
            "POLLS_RETENTION_DAYS",
            "RECEIPT_READS_PER_USER_PER_DAY",
            "EXTENSIONS_ENABLED",
            "EXTENSIONS_WORKER_SOCKET",
            "EXTENSIONS_WORKER_TOKEN_FILE",
            "EXTENSIONS_RUNTIME",
            "EXTENSIONS_RUNTIME_MANIFEST_DIGEST",
            "JOBS_DM_ENABLED",
            "JOBS_MAX_JOBS_PER_USER",
            "JOBS_MAX_JOBS_PER_CHAT",
            "JOBS_MAX_JOBS_GLOBAL",
            "JOBS_MINIMUM_WATCH_INTERVAL_SECONDS",
            "JOBS_MAXIMUM_WATCH_INTERVAL_SECONDS",
            "JOBS_MINIMUM_RENDERED_WATCH_INTERVAL_SECONDS",
            "JOBS_MAX_RENDERED_WATCHES_GLOBAL",
            "JOBS_RENDERED_CONCURRENCY",
            "JOBS_RENDERED_FETCH_TIMEOUT_SECONDS",
            "JOBS_REMINDER_HORIZON_DAYS",
            "JOBS_REMINDER_MAX_CHARS",
            "JOBS_URL_MAX_CHARS",
            "JOBS_FETCH_MAX_BYTES",
            "JOBS_FETCH_TIMEOUT_SECONDS",
            "JOBS_EXTRACTED_MAX_CHARS",
            "JOBS_WORKER_CONCURRENCY",
            "JOBS_POLL_SECONDS",
            "JOBS_EXECUTION_DEADLINE_SECONDS",
            "JOBS_SEND_DEADLINE_SECONDS",
            "JOBS_LEASE_SECONDS",
            "JOBS_RETENTION_DAYS",
            "SEARCH_WINDOW_SIZE",
            "SEARCH_ATTACHMENT_MAX_BYTES",
            "SEARCH_PDF_MAX_PAGES",
            "SEARCH_PARSE_TIMEOUT_S",
            "TELEGRAM_TRANSPORT",
            "TELEGRAM_BRIDGE_ORIGIN",
            "TELEGRAM_BRIDGE_CREDENTIAL",
        }
    )
    _RUNTIME_ENV_KEYS = frozenset({"SB_BOT_MEMORY_LIMIT", "SB_BOT_CPU_LIMIT", "SB_BOT_PIDS_LIMIT"})

    def __init__(self, config: FixedComposeConfig, runner: CommandRunner) -> None:
        if not isinstance(config, FixedComposeConfig) or not callable(getattr(runner, "run", None)):
            raise HostServiceError("invalid_configuration")
        self.config = config
        self.runner = runner

    @staticmethod
    def _read_config_file(path: Path, maximum: int = 1024 * 1024, *, private: bool = True) -> bytes:
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022)
                or info.st_size > maximum
            ):
                raise HostServiceError("invalid_configuration")
            data = bytearray()
            while chunk := os.read(descriptor, min(65536, maximum - len(data) + 1)):
                data.extend(chunk)
                if len(data) > maximum:
                    raise HostServiceError("invalid_configuration")
            return bytes(data)
        except HostServiceError:
            raise
        except OSError:
            raise HostServiceError("invalid_configuration") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _config_snapshot(self) -> tuple[int, str]:
        raw = self._read_config_file(self.config.config_manifest, 32 * 1024)
        try:
            value = json.loads(raw, object_pairs_hook=_unique_json_pairs)
        except (ValueError, UnicodeError):
            raise HostServiceError("invalid_configuration") from None
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "generation", "files", "digest"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or type(value["generation"]) is not int
            or value["generation"] < 0
            or not isinstance(value["files"], dict)
            or not isinstance(value["digest"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["digest"])
        ):
            raise HostServiceError("invalid_configuration")
        expected_paths = [str(path) for path in self.config.config_files]
        if list(value["files"]) != expected_paths:
            raise HostServiceError("invalid_configuration")
        actual_files: dict[str, str] = {}
        for path in self.config.config_files:
            actual_files[str(path)] = hashlib.sha256(
                self._read_config_file(path, private=path != self.config.compose_file)
            ).hexdigest()
        if actual_files != value["files"]:
            raise HostServiceError("invalid_configuration")
        canonical = json.dumps(
            {"version": 1, "generation": value["generation"], "files": actual_files},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        if digest != value["digest"]:
            raise HostServiceError("invalid_configuration")
        return value["generation"], digest

    def _validate_installed_files(self) -> None:
        raw_compose = self._read_config_file(self.config.compose_file, 64 * 1024, private=False)
        try:
            compose = json.loads(raw_compose, object_pairs_hook=_unique_json_pairs)
        except (ValueError, UnicodeError):
            raise HostServiceError("invalid_configuration") from None
        if compose != self._expected_compose_definition():
            raise HostServiceError("invalid_configuration")

        runtime_values = self._parse_env_file(self.config.runtime_env_file, self._RUNTIME_ENV_KEYS)
        if set(runtime_values) != self._RUNTIME_ENV_KEYS:
            raise HostServiceError("invalid_configuration")
        if (
            not re.fullmatch(r"(?:[1-9][0-9]{0,4})(?:m|g)", runtime_values["SB_BOT_MEMORY_LIMIT"])
            or not re.fullmatch(r"(?:[1-4](?:\.[0-9]{1,2})?)", runtime_values["SB_BOT_CPU_LIMIT"])
            or not re.fullmatch(r"[1-9][0-9]{0,3}", runtime_values["SB_BOT_PIDS_LIMIT"])
        ):
            raise HostServiceError("invalid_configuration")
        memory_match = re.fullmatch(
            r"([1-9][0-9]{0,4})(m|g)", runtime_values["SB_BOT_MEMORY_LIMIT"]
        )
        memory_bytes = int(memory_match.group(1)) * (
            1024**3 if memory_match.group(2) == "g" else 1024**2
        )
        if (
            memory_bytes > 16 * 1024**3
            or float(runtime_values["SB_BOT_CPU_LIMIT"]) > 4
            or int(runtime_values["SB_BOT_PIDS_LIMIT"]) > 2048
        ):
            raise HostServiceError("invalid_configuration")

        app_values = self._parse_env_file(
            self.config.app_env_file, self._APP_ENV_KEYS, maximum=32 * 1024
        )
        if (
            app_values.get("TELEGRAM_TRANSPORT") != "bridge"
            or not re.fullmatch(
                r"[A-Za-z0-9_.-]{32,4096}",
                app_values.get("TELEGRAM_BRIDGE_CREDENTIAL", ""),
            )
            or not self._valid_bridge_origin(app_values.get("TELEGRAM_BRIDGE_ORIGIN", ""))
        ):
            raise HostServiceError("invalid_configuration")

    @staticmethod
    def _parse_env_file(path: Path, allowed: frozenset[str], maximum: int = 8192) -> dict[str, str]:
        try:
            lines = (
                FixedComposeRuntime._read_config_file(path, maximum)
                .decode("ascii", errors="strict")
                .splitlines()
            )
        except (HostServiceError, UnicodeError):
            raise HostServiceError("invalid_configuration") from None
        values: dict[str, str] = {}
        for line in lines:
            key, separator, value = line.partition("=")
            if (
                not separator
                or key not in allowed
                or key in values
                or not value
                or value != value.strip()
                or re.fullmatch(r"[A-Za-z0-9_./:@%+=,?!&~-]+", value) is None
            ):
                raise HostServiceError("invalid_configuration")
            values[key] = value
        return values

    @staticmethod
    def _valid_bridge_origin(value: str) -> bool:
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                return False
            _ = parsed.port
            return True
        except ValueError:
            return False

    def _expected_compose_definition(self) -> dict[str, object]:
        identity = "${SB_BOT_RELEASE_IDENTITY:?host service must provide identity}"
        return {
            "name": "sb-bot",
            "services": {
                "sb-bot": {
                    "image": "${SB_BOT_IMAGE:?host service must select a verified local image}",
                    "container_name": "sb-bot",
                    "restart": "unless-stopped",
                    "env_file": [str(self.config.app_env_file)],
                    "environment": {
                        "HF_HUB_OFFLINE": "1",
                        "EMBED_CACHE_DIR": "/models/embeddinggemma-300m",
                        "SB_BOT_RELEASE_IDENTITY": identity,
                        "TELEGRAM_TRANSPORT": "bridge",
                        "TELEGRAM_BRIDGE_CREDENTIAL_FILE": "/tmp/telegram-bridge-credential",  # noqa: S108
                    },
                    "labels": {self._IDENTITY_LABEL: identity},
                    "user": "10001:10001",
                    "read_only": True,
                    "tmpfs": ["/tmp:size=64m"],  # noqa: S108
                    "cap_drop": ["ALL"],
                    "security_opt": ["no-new-privileges:true"],
                    "mem_limit": "${SB_BOT_MEMORY_LIMIT:?operator measured runtime limit required}",
                    "cpus": "${SB_BOT_CPU_LIMIT:?operator measured runtime limit required}",
                    "pids_limit": "${SB_BOT_PIDS_LIMIT:?operator measured runtime limit required}",
                    "networks": ["app-backend", "app-internal"],
                    "volumes": ["app-models:/models:ro"],
                    "healthcheck": {
                        "test": ["CMD", "python", "-I", "/usr/local/bin/chatcore-smoke"],
                        "interval": "30s",
                        "timeout": "3s",
                        "start_period": "60s",
                        "retries": 3,
                    },
                    "logging": {
                        "driver": "json-file",
                        "options": {"max-size": "10m", "max-file": "3"},
                    },
                }
            },
            "networks": {
                "app-backend": {"external": True},
                "app-internal": {"external": True},
            },
            "volumes": {"app-models": {"external": True}},
        }

    def _env(self, identity: DeploymentIdentity | None = None) -> dict[str, str]:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/var/empty",
            "DOCKER_CONFIG": str(self.config.docker_config),
        }
        if identity is not None:
            tag = f"release-{identity.git_sha}"
            env["SB_BOT_IMAGE"] = f"{self.config.image_repository}:{tag}"
            env["SB_BOT_RELEASE_IDENTITY"] = json.dumps(
                asdict(identity), sort_keys=True, separators=(",", ":")
            )
        return env

    def _docker(self, *args: str, identity: DeploymentIdentity | None = None) -> CommandResult:
        return self.runner.run(
            (self.config.docker_path, "--context", "default", *args),
            env=self._env(identity),
            timeout_seconds=self.config.command_timeout_seconds,
            max_output_bytes=self.config.max_output_bytes,
        )

    def observe(self) -> DeploymentIdentity | None:
        self._validate_installed_files()
        generation, config_digest = self._config_snapshot()
        image_result = self._docker("inspect", "--format={{.Image}}", self.config.container)
        image_digest = image_result.stdout.decode("ascii", errors="ignore").strip()
        if image_result.returncode != 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
            raise HostServiceError("reconciliation_required")
        result = self._docker("inspect", "--format={{json .Config.Labels}}", self.config.container)
        if result.returncode != 0:
            # A fixed inspect miss is an actual absence; any ambiguous output is not.
            if not result.stdout and not result.stderr:
                return None
            raise HostServiceError("reconciliation_required")
        try:
            labels = json.loads(result.stdout.decode("utf-8", errors="strict"))
            raw = labels[self._IDENTITY_LABEL]
            value = json.loads(raw)
            fields = {
                "git_sha",
                "image_digest",
                "config_generation",
                "config_digest",
                "policy_digest",
                "target_sequence",
            }
            if not isinstance(value, dict) or set(value) != fields:
                raise ValueError
            container_identity = DeploymentIdentity(**value)
            if (
                container_identity.image_digest != image_digest
                or container_identity.config_generation != generation
                or container_identity.config_digest != config_digest
            ):
                raise ValueError
            return container_identity
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise HostServiceError("reconciliation_required") from None

    def _ensure_image(self, identity: DeploymentIdentity) -> None:
        tag = f"{self.config.image_repository}:release-{identity.git_sha}"
        result = self._docker("image", "inspect", "--format={{.Id}}", tag)
        if (
            result.returncode != 0
            or result.stdout.decode("ascii", "ignore").strip() != identity.image_digest
        ):
            raise HostServiceError("deployment_mismatch")
        labels = self._docker("image", "inspect", "--format={{json .Config.Labels}}", tag)
        try:
            data = json.loads(labels.stdout.decode("utf-8", errors="strict"))
        except (ValueError, UnicodeError):
            raise HostServiceError("deployment_mismatch") from None
        if (
            labels.returncode != 0
            or not isinstance(data, dict)
            or data.get("org.chatcore.release-control.commit") != identity.git_sha
            or data.get("org.chatcore.release-control.policy-digest") != identity.policy_digest
        ):
            raise HostServiceError("deployment_mismatch")

    def _apply(self, identity: DeploymentIdentity) -> None:
        self._validate_installed_files()
        if self._config_snapshot() != (identity.config_generation, identity.config_digest):
            raise HostServiceError("deployment_mismatch")
        self._ensure_image(identity)
        if self._config_snapshot() != (identity.config_generation, identity.config_digest):
            raise HostServiceError("deployment_mismatch")
        result = self.runner.run(
            (
                self.config.docker_path,
                "--context",
                "default",
                "compose",
                "--project-name",
                self.config.project,
                "--file",
                str(self.config.compose_file),
                "--env-file",
                str(self.config.runtime_env_file),
                "up",
                "--detach",
                "--no-deps",
                self.config.service,
            ),
            env=self._env(identity),
            timeout_seconds=self.config.command_timeout_seconds,
            max_output_bytes=self.config.max_output_bytes,
        )
        if result.returncode != 0:
            raise HostServiceError("host_service_denied")
        if self.observe() != identity:
            raise HostServiceError("deployment_mismatch")

    def switch(self, identity: DeploymentIdentity) -> None:
        self._apply(identity)

    def restore(self, identity: DeploymentIdentity) -> None:
        self._apply(identity)

    def healthy(self, identity: DeploymentIdentity) -> bool:
        if self.observe() != identity:
            return False
        result = self._docker("inspect", "--format={{.State.Health.Status}}", self.config.container)
        return result.returncode == 0 and result.stdout.strip() == b"healthy"

    def smoke_test(self, identity: DeploymentIdentity) -> bool:
        if self.observe() != identity:
            return False
        result = self._docker("exec", self.config.container, "/usr/local/bin/chatcore-smoke")
        return (
            result.returncode == 0
            and len(result.stdout) + len(result.stderr) <= self.config.max_output_bytes
        )


@dataclass(frozen=True)
class FixedBuildConfig:
    docker_path: str
    recipe: Path
    entrypoint: Path
    smoke_probe: Path
    dependency_cache: Path
    dependency_cache_sha256: str
    resource_manifest: Path
    scratch_directory: Path
    image_repository: str
    allowed_paths: tuple[str, ...]
    source_roots: tuple[str, ...]
    test_paths: tuple[str, ...]
    worker_source: Path
    harness_source: Path
    reviewed_source_exceptions: tuple[str, ...]
    cpu_quota_micros: int
    memory_bytes: int
    pids_max: int
    scratch_bytes: int
    buildx_builder: str
    buildkit_container: str
    buildkit_image_ref: str
    buildkit_image_id: str
    timeout_seconds: int = 600
    max_output_bytes: int = 65536
    cgroup_parent: str = "/system.slice/chatcore-release-build.slice"

    def __post_init__(self) -> None:
        fixed_paths = (
            self.recipe,
            self.entrypoint,
            self.smoke_probe,
            self.dependency_cache,
            self.resource_manifest,
            self.scratch_directory,
            self.worker_source,
            self.harness_source,
        )
        if (
            not isinstance(self.docker_path, str)
            or not Path(self.docker_path).is_absolute()
            or any(not isinstance(path, Path) or not path.is_absolute() for path in fixed_paths)
            or not re.fullmatch(r"[0-9a-f]{64}", self.dependency_cache_sha256)
            or not isinstance(self.image_repository, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9._/-]{0,127}", self.image_repository)
            or not isinstance(self.allowed_paths, tuple)
            or not 1 <= len(self.allowed_paths) <= 64
            or not isinstance(self.source_roots, tuple)
            or not self.source_roots
            or not isinstance(self.test_paths, tuple)
            or not self.test_paths
            or not isinstance(self.reviewed_source_exceptions, tuple)
            or self.reviewed_source_exceptions
            != tuple(sorted(set(self.reviewed_source_exceptions)))
            or any(
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or ".." in path.split("/")
                or "\\" in path
                or not path.endswith((".py", ".sh"))
                for path in self.reviewed_source_exceptions
            )
            or not set(self.reviewed_source_exceptions).issubset(_REVIEWED_SOURCE_EXCEPTIONS)
            or any(
                type(value) is not int
                for value in (
                    self.cpu_quota_micros,
                    self.memory_bytes,
                    self.pids_max,
                    self.scratch_bytes,
                    self.timeout_seconds,
                    self.max_output_bytes,
                )
            )
            or not 10000 <= self.cpu_quota_micros <= 4_000_000
            or not 64 * 1024 * 1024 <= self.memory_bytes <= 16 * 1024 * 1024 * 1024
            or not 1 <= self.pids_max <= 2048
            or not 128 * 1024 * 1024 <= self.scratch_bytes <= 16 * 1024 * 1024 * 1024
            or not 1 <= self.timeout_seconds <= 1800
            or not 1024 <= self.max_output_bytes <= 1024 * 1024
            or self.cgroup_parent != "/system.slice/chatcore-release-build.slice"
            or not isinstance(self.buildx_builder, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", self.buildx_builder)
            or self.buildkit_container != f"buildx_buildkit_{self.buildx_builder}0"
            or not isinstance(self.buildkit_image_ref, str)
            or not re.fullmatch(r"[A-Za-z0-9._/:+-]+@sha256:[0-9a-f]{64}", self.buildkit_image_ref)
            or not isinstance(self.buildkit_image_id, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.buildkit_image_id)
        ):
            raise HostServiceError("invalid_configuration")


class FixedOfflineImageBuilder:
    """Build only verified Git source with an installed offline Dockerfile."""

    def __init__(self, config: FixedBuildConfig, runner: CommandRunner) -> None:
        if not isinstance(config, FixedBuildConfig) or not callable(getattr(runner, "run", None)):
            raise HostServiceError("invalid_configuration")
        self.config = config
        self.runner = runner

    @staticmethod
    def _read_installed(path: Path, maximum: int, *, private: bool = False) -> bytes:
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022)
                or info.st_size > maximum
            ):
                raise HostServiceError("invalid_configuration")
            data = bytearray()
            while chunk := os.read(descriptor, min(65536, maximum - len(data) + 1)):
                data.extend(chunk)
                if len(data) > maximum:
                    raise HostServiceError("invalid_configuration")
            return bytes(data)
        except HostServiceError:
            raise
        except OSError:
            raise HostServiceError("invalid_configuration") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _resource_manifest(self) -> str:
        raw = self._read_installed(self.config.resource_manifest, 16 * 1024)
        try:
            value = json.loads(raw, object_pairs_hook=_unique_json_pairs)
        except (ValueError, UnicodeError):
            raise HostServiceError("invalid_configuration") from None
        expected = {
            "version": 1,
            "builder": self.config.buildx_builder,
            "builder_container": self.config.buildkit_container,
            "driver": "docker-container",
            "docker_context": "default",
            "docker_endpoint": _DEFAULT_DOCKER_ENDPOINT,
            "buildkit_image_ref": self.config.buildkit_image_ref,
            "buildkit_image_id": self.config.buildkit_image_id,
            "builder_cgroup": self.config.cgroup_parent,
            "cpu_quota_micros": self.config.cpu_quota_micros,
            "memory_max_bytes": self.config.memory_bytes,
            "memory_swap_bytes": self.config.memory_bytes,
            "tasks_max": self.config.pids_max,
            "scratch_max_bytes": self.config.scratch_bytes,
            "scratch_mount": "/var/lib/buildkit",
            "network": "none",
            "reviewed_source_exceptions": list(self.config.reviewed_source_exceptions),
        }
        if not isinstance(value, dict) or value != expected:
            raise HostServiceError("invalid_configuration")
        return hashlib.sha256(raw).hexdigest()

    def _docker(self, *args: str) -> CommandResult:
        return self.runner.run(
            (self.config.docker_path, "--context", "default", *args),
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/var/empty",
                "DOCKER_CONFIG": "/var/lib/chatcore-release-control/docker",
            },
            timeout_seconds=30,
            max_output_bytes=8192,
        )

    def _verify_builder(self, manifest_digest: str) -> None:
        config = self.config
        context = self._docker(
            "context",
            "inspect",
            "default",
            "--format={{json .Endpoints.docker.Host}}",
        )
        if context.returncode != 0:
            raise HostServiceError("invalid_configuration")
        try:
            endpoint = json.loads(context.stdout.decode("utf-8", "strict"))
        except (ValueError, UnicodeError):
            raise HostServiceError("invalid_configuration") from None
        if endpoint != _DEFAULT_DOCKER_ENDPOINT:
            raise HostServiceError("invalid_configuration")
        details = self._docker("buildx", "inspect", config.buildx_builder)
        if details.returncode != 0:
            raise HostServiceError("invalid_configuration")
        lines = [line.strip() for line in details.stdout.decode("utf-8", "strict").splitlines()]
        names = [line.removeprefix("Name:").strip() for line in lines if line.startswith("Name:")]
        if (
            "Driver: docker-container" not in lines
            or names != [config.buildx_builder, f"{config.buildx_builder}0"]
            or "Endpoint: default" not in lines
            or "Status: running" not in lines
        ):
            raise HostServiceError("invalid_configuration")
        result = self._docker(
            "inspect",
            "--type",
            "container",
            "--format={{json .}}",
            config.buildkit_container,
        )
        if result.returncode != 0:
            raise HostServiceError("invalid_configuration")
        try:
            value = json.loads(result.stdout.decode("utf-8", "strict"))
            host = value["HostConfig"]
            container = value["Config"]
            labels = container["Labels"]
            mounts = value["Mounts"]
            tmpfs = host["Tmpfs"]
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise HostServiceError("invalid_configuration") from None
        if (
            not isinstance(host, dict)
            or not isinstance(container, dict)
            or not isinstance(labels, dict)
            or not isinstance(tmpfs, dict)
            or set(tmpfs) != {"/var/lib/buildkit"}
            or not isinstance(tmpfs["/var/lib/buildkit"], str)
            or not isinstance(mounts, list)
        ):
            raise HostServiceError("invalid_configuration")
        valid_mounts = not mounts or (
            len(mounts) == 1
            and isinstance(mounts[0], dict)
            and mounts[0].get("Type") == "tmpfs"
            and mounts[0].get("Destination") == "/var/lib/buildkit"
            and mounts[0].get("RW") is True
            and mounts[0].get("Source", "") in {"", "tmpfs"}
        )
        scratch_options = {
            item.partition("=")[0]: item.partition("=")[2]
            for item in tmpfs.get("/var/lib/buildkit", "").split(",")
        }
        required_labels = {
            "org.chatcore.release-control.builder": config.buildx_builder,
            "org.chatcore.release-control.resource-manifest": manifest_digest,
        }
        if (
            value.get("Image") != config.buildkit_image_id
            or container.get("Image") != config.buildkit_image_ref
            or any(labels.get(name) != expected for name, expected in required_labels.items())
            or not isinstance(value.get("State"), dict)
            or value["State"].get("Running") is not True
            or host.get("Memory") != config.memory_bytes
            or host.get("MemorySwap") != config.memory_bytes
            or host.get("CpuQuota") != config.cpu_quota_micros
            or host.get("PidsLimit") != config.pids_max
            or host.get("CgroupParent") != config.cgroup_parent
            or host.get("NetworkMode") != "none"
            or host.get("Privileged") is not False
            or host.get("ReadonlyRootfs") is not True
            or not valid_mounts
            or scratch_options.get("size") != str(config.scratch_bytes)
            or set(scratch_options) != {"rw", "nosuid", "nodev", "noexec", "size"}
            or any(scratch_options[key] for key in ("rw", "nosuid", "nodev", "noexec"))
            or scratch_options.get("rw") is None
            or host.get("Binds") not in (None, [])
        ):
            raise HostServiceError("invalid_configuration")

    def build(self, repository: Path, context: HostDeploymentContext) -> str:
        if not isinstance(context, HostDeploymentContext):
            raise HostServiceError("build_denied")
        config = self.config
        manifest_digest = self._resource_manifest()
        self._verify_builder(manifest_digest)
        if config.dependency_cache.stat().st_size > min(config.scratch_bytes, 1024 * 1024 * 1024):
            raise HostServiceError("build_denied")
        worker = self._read_installed(config.worker_source, 1024 * 1024)
        harness = self._read_installed(config.harness_source, 1024 * 1024)
        try:
            bundle = build_baseline_bundle(
                repository,
                context.merge_commit,
                policy_digest=context.policy_digest,
                allowed_paths=config.allowed_paths,
                source_roots=config.source_roots,
                test_paths=config.test_paths,
                worker_source=worker,
                harness_source=harness,
                reviewed_source_exceptions=config.reviewed_source_exceptions,
            )
            helper_entrypoint = self._read_installed(config.entrypoint, 16 * 1024)
            helper_smoke = self._read_installed(config.smoke_probe, 16 * 1024)
            cache_size = config.dependency_cache.stat().st_size
            if (
                len(bundle.source_tar)
                + len(bundle.manifest)
                + len(worker)
                + len(harness)
                + len(helper_entrypoint)
                + len(helper_smoke)
                + cache_size * 2
                > config.scratch_bytes
            ):
                raise HostServiceError("build_denied")
            config.scratch_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            scratch_info = config.scratch_directory.lstat()
            if (
                not stat.S_ISDIR(scratch_info.st_mode)
                or scratch_info.st_uid != os.geteuid()
                or stat.S_IMODE(scratch_info.st_mode) & 0o077
            ):
                raise HostServiceError("invalid_configuration")
            with tempfile.TemporaryDirectory(
                prefix="host-cache-check-", dir=config.scratch_directory
            ) as cache_check:
                prepare_dependency_cache(
                    config.dependency_cache,
                    config.dependency_cache_sha256,
                    Path(cache_check) / "cache",
                )
            with tempfile.TemporaryDirectory(
                prefix="host-build-", dir=config.scratch_directory
            ) as scratch:
                build_context = Path(scratch)
                self._write_private(build_context / "baseline.tar", bundle.source_tar)
                self._write_private(build_context / "manifest.json", bundle.manifest)
                self._copy_cache(
                    config.dependency_cache,
                    build_context / "dependency-cache.tar",
                    maximum=min(config.scratch_bytes, 1024 * 1024 * 1024),
                    expected=config.dependency_cache_sha256,
                )
                self._write_private(build_context / "p08-entrypoint.sh", helper_entrypoint)
                self._write_private(build_context / "p08-smoke.py", helper_smoke)
                recipe = self._read_installed(config.recipe, 64 * 1024)
                self._write_private(build_context / "Dockerfile", recipe)
                tag = f"{config.image_repository}:release-{context.merge_commit}"
                env = {
                    "PATH": "/usr/bin:/bin",
                    "HOME": "/var/empty",
                    "DOCKER_CONFIG": "/var/lib/chatcore-release-control/docker",
                }
                result = self.runner.run(
                    (
                        config.docker_path,
                        "--context",
                        "default",
                        "buildx",
                        "build",
                        f"--builder={config.buildx_builder}",
                        "--network=none",
                        "--no-cache",
                        "--pull=false",
                        "--load",
                        f"--cgroup-parent={config.cgroup_parent}",
                        f"--build-arg=DEPENDENCY_CACHE_SHA256={config.dependency_cache_sha256}",
                        f"--tag={tag}",
                        f"--label=org.chatcore.release-control.commit={context.merge_commit}",
                        f"--label=org.chatcore.release-control.policy-digest={context.policy_digest}",
                        f"--label=org.chatcore.release-control.manifest-digest={bundle.manifest_digest}",
                        "--file",
                        str(build_context / "Dockerfile"),
                        str(build_context),
                    ),
                    env=env,
                    timeout_seconds=config.timeout_seconds,
                    max_output_bytes=config.max_output_bytes,
                )
                if result.returncode != 0:
                    raise HostServiceError("build_denied")
                inspected = self.runner.run(
                    (config.docker_path, "image", "inspect", "--format={{.Id}}", tag),
                    env=env,
                    timeout_seconds=30,
                    max_output_bytes=4096,
                )
                image_digest = inspected.stdout.decode("ascii", errors="strict").strip()
                if inspected.returncode != 0 or not re.fullmatch(
                    r"sha256:[0-9a-f]{64}", image_digest
                ):
                    raise HostServiceError("build_denied")
                labels = self.runner.run(
                    (
                        config.docker_path,
                        "--context",
                        "default",
                        "image",
                        "inspect",
                        "--format={{json .Config.Labels}}",
                        tag,
                    ),
                    env=env,
                    timeout_seconds=30,
                    max_output_bytes=4096,
                )
                try:
                    actual_labels = json.loads(labels.stdout.decode("utf-8", errors="strict"))
                except (ValueError, UnicodeError):
                    raise HostServiceError("build_denied") from None
                if (
                    labels.returncode != 0
                    or not isinstance(actual_labels, dict)
                    or actual_labels.get("org.chatcore.release-control.commit")
                    != context.merge_commit
                    or actual_labels.get("org.chatcore.release-control.policy-digest")
                    != context.policy_digest
                    or actual_labels.get("org.chatcore.release-control.manifest-digest")
                    != bundle.manifest_digest
                ):
                    raise HostServiceError("build_denied")
                return image_digest
        except HostServiceError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError):
            raise HostServiceError("build_denied") from None

    @staticmethod
    def _write_private(path: Path, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise HostServiceError("build_denied") from None

    @staticmethod
    def _copy_cache(source: Path, destination: Path, *, maximum: int, expected: str) -> None:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        output_fd = -1
        try:
            input_fd = os.open(source, flags)
            info = os.fstat(input_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                os.close(input_fd)
                raise HostServiceError("build_denied")
            output_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            digest = hashlib.sha256()
            copied = 0
            with os.fdopen(input_fd, "rb") as reader, os.fdopen(output_fd, "wb") as writer:
                output_fd = -1
                while chunk := reader.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > maximum:
                        raise HostServiceError("build_denied")
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if copied != info.st_size or digest.hexdigest() != expected:
                raise HostServiceError("build_denied")
        except HostServiceError:
            raise
        except OSError:
            raise HostServiceError("build_denied") from None
        finally:
            if output_fd >= 0:
                os.close(output_fd)


class HostService:
    """Serialize verified host operations and journal before runtime effects."""

    def __init__(
        self,
        *,
        source: SourceAdapter,
        target: str,
        policy_digest: str,
        journal: HostJournal,
        broker: BrokerAdapter,
        evidence: EvidenceAdapter,
        builder: BuildAdapter,
        runtime: RuntimeAdapter,
        host_lock_path: str | os.PathLike[str] | None = None,
        now: Callable[[], int],
    ) -> None:
        if (
            not isinstance(target, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", target)
            or not isinstance(policy_digest, str)
            or not _POLICY_DIGEST.fullmatch(policy_digest)
            or not isinstance(journal, HostJournal)
            or not all(
                callable(getattr(value, method, None))
                for value, methods in (
                    (
                        broker,
                        (
                            "get_context",
                            "prepare",
                            "admit",
                            "started",
                            "restore_started",
                            "receipt",
                        ),
                    ),
                    (evidence, ("verify",)),
                    (builder, ("build",)),
                    (runtime, ("observe", "switch", "restore", "healthy", "smoke_test")),
                )
                for method in methods
            )
            or not callable(now)
        ):
            raise HostServiceError("invalid_configuration")
        lock_path = (
            Path(host_lock_path)
            if host_lock_path is not None
            else journal.path.with_name("host.lock")
        )
        if not callable(getattr(source, "fetch", None)):
            raise HostServiceError("invalid_configuration")
        self.source = source
        self.target = target
        self.policy_digest = policy_digest
        self.journal = journal
        self.broker = broker
        self.evidence = evidence
        self.builder = builder
        self.runtime = runtime
        self.host_lock_path = lock_path
        self._now = now

    def __repr__(self) -> str:
        return "HostService(credentials=[REDACTED])"

    @contextmanager
    def _locked(self):
        self.host_lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            parent = self.host_lock_path.parent.lstat()
        except OSError:
            raise HostServiceError("invalid_configuration") from None
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise HostServiceError("invalid_configuration")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
        fd = -1
        locked = False
        try:
            fd = os.open(self.host_lock_path, flags, 0o600)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise HostServiceError("invalid_configuration")
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
            yield
        except HostServiceError:
            raise
        except OSError:
            raise HostServiceError("invalid_configuration") from None
        finally:
            if fd >= 0:
                if locked:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def deploy(self, approval_id: str, *, operation_id: str) -> JournalRecord:
        if (
            not isinstance(approval_id, str)
            or not _APPROVAL_ID.fullmatch(approval_id)
            or not isinstance(operation_id, str)
            or not _OPERATION_ID.fullmatch(operation_id)
        ):
            raise HostServiceError("invalid_request")
        with self._locked():
            return self._deploy_locked(approval_id, operation_id)

    def _deploy_locked(self, approval_id: str, operation_id: str) -> JournalRecord:
        try:
            try:
                previous = self.journal.get_record(operation_id)
            except HostJournalError as error:
                if error.code != "journal_not_found":
                    raise
                previous = None
            context = self.broker.get_context(approval_id)
            if (
                not isinstance(context, HostDeploymentContext)
                or context.approval_id != approval_id
                or context.target != self.target
                or context.policy_digest != self.policy_digest
                or (previous is None and context.status != "merged_verified")
                or (
                    previous is not None
                    and context.status
                    not in {
                        "merged_verified",
                        "deploying",
                        "restoring",
                        "healthy",
                        "restored_healthy",
                        "recovery_failed",
                    }
                )
            ):
                raise HostServiceError("broker_denied")
            if previous is not None and previous.phase in {
                JournalState.HEALTHY,
                JournalState.RESTORED_HEALTHY,
                JournalState.RECOVERY_FAILED,
            }:
                self._validate_context_record(context, previous)
                self.broker.receipt(approval_id, context.dispatch_fence, operation_id, previous)
                self.journal.archive_terminal(operation_id, previous.receipt)
                return previous
            if previous is not None:
                context = self._validate_context_record(context, previous)
                if previous.phase is JournalState.PREPARED:
                    return self._resume_prepared_locked(
                        approval_id, operation_id, context, previous
                    )
                return self._reconcile_locked(approval_id, operation_id, context)
            baseline = context.deployed_identity
            if context.host_operation_id is not None and context.host_operation_id != operation_id:
                raise HostServiceError("broker_denied")
            if context.sequence <= baseline.target_sequence:
                raise HostServiceError("baseline_mismatch")
            if self.runtime.observe() != baseline:
                raise HostServiceError("baseline_mismatch")
            with self.source.fetch(context.merge_commit, baseline.git_sha) as source_repository:
                if self.evidence.verify(source_repository, context) is not True:
                    raise HostServiceError("baseline_mismatch")
                image_digest = self.builder.build(source_repository, context)
            if not isinstance(image_digest, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", image_digest
            ):
                raise HostServiceError("build_denied")
            payload = DeploymentIdentity(
                git_sha=context.merge_commit,
                image_digest=image_digest,
                config_generation=baseline.config_generation,
                config_digest=baseline.config_digest,
                policy_digest=context.policy_digest,
                target_sequence=context.sequence,
            )
            if context.payload is not None and context.payload != payload:
                raise HostServiceError("build_denied")
            context = replace(context, payload=payload)
            if self.runtime.observe() != baseline:
                raise HostServiceError("baseline_mismatch")
            self.broker.prepare(
                approval_id, context.dispatch_fence, operation_id, baseline, context.payload
            )
            admission = self.broker.admit(approval_id, context.dispatch_fence, operation_id)
            start_deadline, recovery_deadline = self._admission(
                admission, approval_id, context.dispatch_fence, operation_id, context
            )
            now = self._time()
            record = self.journal.prepare(
                operation_id=operation_id,
                baseline=baseline,
                payload=context.payload,
                start_deadline=start_deadline,
                recovery_deadline=recovery_deadline,
                now=now,
            )
            if record.phase in {
                JournalState.HEALTHY,
                JournalState.RESTORED_HEALTHY,
                JournalState.RECOVERY_FAILED,
            }:
                self.broker.receipt(approval_id, context.dispatch_fence, operation_id, record)
                self.journal.archive_terminal(operation_id, record.receipt)
                return record
            return self._start_and_finish(approval_id, operation_id, context, record)
        except HostServiceError:
            raise
        except (HostJournalError, ReconciliationRequired):
            raise HostServiceError("reconciliation_required") from None
        except Exception:
            # Do not expose process, Git, Docker, HTTP or credential diagnostics.
            raise HostServiceError("host_service_denied") from None

    @staticmethod
    def _validate_context_record(
        context: HostDeploymentContext, record: JournalRecord
    ) -> HostDeploymentContext:
        if (
            context.deployed_identity != record.baseline
            or context.merge_commit != record.payload.git_sha
            or context.policy_digest != record.payload.policy_digest
            or context.sequence != record.payload.target_sequence
            or (context.payload is not None and context.payload != record.payload)
            or (
                context.host_operation_id is not None
                and context.host_operation_id != record.operation_id
            )
            or (
                context.host_operation_payload_digest is not None
                and context.host_operation_payload_digest != record.payload_digest
            )
            or (
                context.host_operation_start_deadline is not None
                and context.host_operation_start_deadline != record.start_deadline
            )
            or (
                context.host_operation_recovery_deadline is not None
                and context.host_operation_recovery_deadline != record.recovery_deadline
            )
            or (
                context.host_operation_receipt_id is not None
                and context.host_operation_receipt_id != record.receipt
            )
            or (
                context.host_operation_state in {"prepared", "admitted"}
                and record.phase is not JournalState.PREPARED
            )
            or (
                context.host_operation_state == "healthy"
                and record.phase is not JournalState.HEALTHY
            )
            or (
                context.host_operation_state == "restored_healthy"
                and record.phase is not JournalState.RESTORED_HEALTHY
            )
            or (
                context.host_operation_state == "recovery_failed"
                and record.phase is not JournalState.RECOVERY_FAILED
            )
        ):
            raise HostServiceError("reconciliation_required")
        return replace(context, payload=record.payload)

    def _resume_prepared_locked(
        self,
        approval_id: str,
        operation_id: str,
        context: HostDeploymentContext,
        record: JournalRecord,
    ) -> JournalRecord:
        actual = self.runtime.observe()
        decision = self.journal.reconcile(operation_id, actual=actual, now=self._time())
        if decision.action is not ReconciliationAction.SWITCH_ALLOWED:
            raise HostServiceError("reconciliation_required")
        admission = self.broker.admit(approval_id, context.dispatch_fence, operation_id)
        start_deadline, recovery_deadline = self._admission(
            admission, approval_id, context.dispatch_fence, operation_id, context
        )
        if (start_deadline, recovery_deadline) != (
            record.start_deadline,
            record.recovery_deadline,
        ):
            raise HostServiceError("broker_denied")
        return self._start_and_finish(approval_id, operation_id, context, record)

    def _start_and_finish(
        self,
        approval_id: str,
        operation_id: str,
        context: HostDeploymentContext,
        record: JournalRecord,
    ) -> JournalRecord:
        if context.payload is None:
            raise HostServiceError("broker_denied")
        if self.runtime.observe() != record.baseline:
            raise HostServiceError("baseline_mismatch")
        self.journal.begin_switch(operation_id, now=self._time())
        self.broker.started(approval_id, context.dispatch_fence, operation_id)
        if self.runtime.observe() != record.baseline:
            actual = self.runtime.observe()
            self.journal.reconcile(operation_id, actual=actual, now=self._time())
            raise HostServiceError("reconciliation_required")
        self.runtime.switch(context.payload)
        actual = self.runtime.observe()
        self.journal.reconcile(operation_id, actual=actual, now=self._time())
        if actual != context.payload:
            raise HostServiceError("reconciliation_required")
        checks = self._health(context.payload)
        terminal = self.journal.reconcile(
            operation_id, actual=actual, healthy=checks, now=self._time()
        ).record
        if terminal.phase is JournalState.RESTORING:
            terminal = self._restore_locked(operation_id, context, terminal)
        self.broker.receipt(approval_id, context.dispatch_fence, operation_id, terminal)
        self.journal.archive_terminal(operation_id, terminal.receipt)
        if terminal.phase not in {JournalState.HEALTHY, JournalState.RESTORED_HEALTHY}:
            raise HostServiceError("recovery_failed")
        return terminal

    def _admission(
        self,
        value: object,
        approval_id: str,
        dispatch_fence: int,
        operation_id: str,
        context: HostDeploymentContext,
    ) -> tuple[int, int]:
        if not isinstance(value, dict) or set(value) != {
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
        }:
            raise HostServiceError("broker_denied")
        if (
            value["approval_id"] != approval_id
            or value["target"] != context.target
            or value["operation_id"] != operation_id
            or value["dispatch_fence"] != dispatch_fence
            or type(value["dispatch_fence"]) is not int
            or value["admitted"] is not True
            or value["may_start"] is not True
            or value["baseline_identity"] != context.deployed_identity
            or value["payload_identity"] != context.payload
        ):
            raise HostServiceError("broker_denied")
        start_deadline = self._deadline_epoch(value["start_deadline"])
        recovery_deadline = self._deadline_epoch(value["recovery_deadline"])
        if start_deadline is None or recovery_deadline is None:
            raise HostServiceError("broker_denied")
        return start_deadline, recovery_deadline

    @staticmethod
    def _deadline_epoch(value: object) -> int | None:
        if type(value) is int:
            return value if value >= 0 else None
        if not isinstance(value, str) or not 1 <= len(value) <= 40:
            return None
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
            if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
                return None
            return int(parsed.timestamp())
        except (OverflowError, OSError, ValueError):
            return None

    def _time(self) -> int:
        value = self._now()
        if type(value) is not int or value < 0:
            raise HostServiceError("invalid_configuration")
        return value

    def _health(self, identity: DeploymentIdentity) -> bool:
        try:
            return (
                self.runtime.healthy(identity) is True and self.runtime.smoke_test(identity) is True
            )
        except Exception:
            return False

    def _restore_locked(
        self, operation_id: str, context: HostDeploymentContext, record: JournalRecord
    ) -> JournalRecord:
        actual = self.runtime.observe()
        decision = self.journal.reconcile(operation_id, actual=actual, now=self._time())
        if decision.action is not ReconciliationAction.RESTORE_ALLOWED or actual != context.payload:
            raise HostServiceError("recovery_failed")
        # A second exact read fences out-of-band container/config changes immediately before
        # the only restore effect. Runtime adapter uses the installed service definition.
        if self.runtime.observe() != context.payload:
            raise HostServiceError("recovery_failed")
        self.broker.restore_started(context.approval_id, context.dispatch_fence, operation_id)
        self.runtime.restore(context.deployed_identity)
        restored = self.runtime.observe()
        restored_healthy = restored == context.deployed_identity and self._health(
            context.deployed_identity
        )
        return self.journal.reconcile(
            operation_id,
            actual=restored,
            healthy=restored_healthy,
            now=self._time(),
        ).record

    def reconcile(self, approval_id: str, operation_id: str) -> JournalRecord:
        if (
            not isinstance(approval_id, str)
            or not _APPROVAL_ID.fullmatch(approval_id)
            or not isinstance(operation_id, str)
            or not _OPERATION_ID.fullmatch(operation_id)
        ):
            raise HostServiceError("invalid_request")
        with self._locked():
            try:
                record = self.journal.get_record(operation_id)
                context = self.broker.get_context(approval_id)
                if not isinstance(context, HostDeploymentContext):
                    raise HostServiceError("reconciliation_required")
                context = self._validate_context_record(context, record)
                return self._reconcile_locked(approval_id, operation_id, context)
            except HostServiceError:
                raise
            except (HostJournalError, ReconciliationRequired):
                raise HostServiceError("reconciliation_required") from None

    def _reconcile_locked(
        self, approval_id: str, operation_id: str, context: HostDeploymentContext
    ) -> JournalRecord:
        record = self.journal.get_record(operation_id)
        actual = self.runtime.observe()
        if record.phase is JournalState.CHECKING and actual == record.payload:
            decision = self.journal.reconcile(
                operation_id,
                actual=actual,
                healthy=self._health(record.payload),
                now=self._time(),
            )
            if decision.action is ReconciliationAction.RECONCILIATION_REQUIRED:
                raise HostServiceError("reconciliation_required")
            record = decision.record
        elif record.phase is JournalState.RESTORING and actual == record.baseline:
            decision = self.journal.reconcile(
                operation_id,
                actual=actual,
                healthy=self._health(record.baseline),
                now=self._time(),
            )
            if decision.action is ReconciliationAction.RECONCILIATION_REQUIRED:
                raise HostServiceError("reconciliation_required")
            record = decision.record
        else:
            result = self.journal.reconcile(operation_id, actual=actual, now=self._time())
            if result.action is ReconciliationAction.RECONCILIATION_REQUIRED:
                raise HostServiceError("reconciliation_required")
            if result.action is ReconciliationAction.VERIFY_PAYLOAD:
                record = self.journal.reconcile(
                    operation_id,
                    actual=actual,
                    healthy=self._health(record.payload),
                    now=self._time(),
                ).record
            else:
                record = result.record
        if record.phase is JournalState.RESTORING:
            record = self._restore_locked(operation_id, context, record)
        if record.phase in {
            JournalState.HEALTHY,
            JournalState.RESTORED_HEALTHY,
            JournalState.RECOVERY_FAILED,
        }:
            self.broker.receipt(approval_id, context.dispatch_fence, operation_id, record)
            self.journal.archive_terminal(operation_id, record.receipt)
        return record

    def current_state(self) -> dict[str, object]:
        """Read a consistent actual state without re-entering a deploy-held host lock.

        Broker admission calls back to this endpoint while the deploy request retains the
        exclusive effect lock. This path is strictly read-only and accepts only a stable
        full identity across both health checks and two observations.
        """
        first = self.runtime.observe()
        if first is None:
            raise HostServiceError("reconciliation_required")
        healthy = self._health(first)
        second = self.runtime.observe()
        if second != first:
            raise HostServiceError("reconciliation_required")
        return {
            "deployed_identity": asdict(second),
            "healthy": healthy,
            "policy_digest": self.policy_digest,
            "target_sequence": second.target_sequence,
        }
