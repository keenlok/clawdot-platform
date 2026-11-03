"""Immutable local publication gate.

The caller supplies trusted scanner, provenance-store, and publish-transport
adapters. This module never checks out or executes candidate source and never
performs a network request itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .git_evidence import (
    COMMAND_TIMEOUT_SECONDS,
    MAX_HISTORY_COMMITS,
    MAX_OUTPUT_BYTES,
    GitEvidenceError,
    collect_release_bundle,
)
from .policy import Change, PolicyError, PolicyManifest, _is_private_path

MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_SCAN_ITEM_BYTES = 4 * 1024 * 1024
MAX_TOTAL_SCAN_BYTES = 32 * 1024 * 1024
MAX_ARTIFACTS = 128
MAX_TOTAL_SECONDS = 180.0
MAX_SCANNER_BINARY_BYTES = 64 * 1024 * 1024
MAX_SCANNER_CONFIG_BYTES = 1024 * 1024
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REQUEST_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MODES = {b"100644": "blob", b"100755": "blob", b"120000": "symlink", b"160000": "commit"}
_ZERO_ID = {b"0" * 40, b"0" * 64}


class PublicationError(ValueError):
    """A bounded publication denial code; never includes source or credentials."""

    def __init__(self, code: str = "publication_denied") -> None:
        if code not in {
            "invalid_input",
            "invalid_bundle",
            "missing_object",
            "invalid_git_output",
            "private_path",
            "protected_path",
            "unapproved_path",
            "unsupported_mode",
            "copied_forbidden_blob",
            "scanner_unavailable",
            "scanner_failed",
            "scanner_finding",
            "binary_content",
            "size_limit",
            "timeout",
            "provenance_failed",
            "publication_failed",
            "publication_mismatch",
        }:
            code = "publication_denied"
        super().__init__(code)
        self.code = code


class ScanVerdict(StrEnum):
    CLEAN = "clean"
    FINDING = "finding"
    ERROR = "error"


@dataclass(frozen=True)
class ScanSurface:
    name: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class PublicationRequest:
    request_id: str
    deployed_commit: str
    base_commit: str
    head_commit: str
    bundle: bytes = field(repr=False)
    specification: bytes = field(repr=False)
    pull_request_title: bytes = field(repr=False)
    pull_request_body: bytes = field(repr=False)
    artifacts: tuple[ScanSurface, ...] = ()


@dataclass(frozen=True)
class ScanInput:
    surface_name: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class ProvenanceRecord:
    request_id: str
    deployed_commit: str
    base_commit: str
    head_commit: str
    policy_digest: str
    scanner_identity_sha256: str
    baseline_history_scan_complete: bool
    bundle_sha256: str
    surface_manifest_sha256: str
    scanned_surface_count: int
    scanned_bytes: int


@dataclass(frozen=True)
class PublishCommand:
    request_id: str
    repository_id: int
    frozen_commit: str
    refspec: str
    quarantine_git_dir: Path


@dataclass(frozen=True)
class PublishResult:
    commit: str
    ref: str


@dataclass(frozen=True)
class PublicationReceipt:
    request_id: str
    deployed_commit: str
    base_commit: str
    head_commit: str
    policy_digest: str
    scanner_identity_sha256: str
    published_ref: str
    bundle_sha256: str
    surface_manifest_sha256: str


class Scanner(Protocol):
    """Trusted adapter that returns within its own bounded scan deadline."""

    def __call__(self, item: ScanInput, /) -> ScanVerdict: ...


class ProvenanceStore(Protocol):
    """Trusted durable, idempotent store for the same immutable record."""

    def __call__(self, record: ProvenanceRecord, /) -> None: ...


class PublishTransport(Protocol):
    """Trusted fixed-remote CAS transport with a bounded request timeout."""

    def __call__(self, command: PublishCommand, /) -> PublishResult: ...


class SqliteProvenanceStore:
    """Durable gateway-owned insert-once provenance keyed by request ID."""

    def __init__(self, database: str | os.PathLike[str]) -> None:
        try:
            self.path = Path(database).resolve(strict=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path, timeout=5.0) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS publication_provenance ("
                    "request_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, "
                    "record_sha256 TEXT NOT NULL, created_at REAL NOT NULL) WITHOUT ROWID"
                )
                connection.execute(
                    "CREATE TRIGGER IF NOT EXISTS publication_provenance_no_update "
                    "BEFORE UPDATE ON publication_provenance BEGIN "
                    "SELECT RAISE(ABORT, 'immutable provenance'); END"
                )
                connection.execute(
                    "CREATE TRIGGER IF NOT EXISTS publication_provenance_no_delete "
                    "BEFORE DELETE ON publication_provenance BEGIN "
                    "SELECT RAISE(ABORT, 'immutable provenance'); END"
                )
            os.chmod(self.path, 0o600)
        except (OSError, sqlite3.Error, ValueError):
            raise PublicationError("provenance_failed") from None

    def __call__(self, record: ProvenanceRecord, /) -> None:
        if not isinstance(record, ProvenanceRecord):
            raise PublicationError("provenance_failed")
        canonical = json.dumps(
            asdict(record),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
        try:
            with sqlite3.connect(self.path, timeout=5.0, isolation_level=None) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT record_json FROM publication_provenance WHERE request_id = ?",
                    (record.request_id,),
                ).fetchone()
                if row is not None:
                    if row[0] != canonical:
                        raise PublicationError("provenance_failed")
                    connection.commit()
                    return
                connection.execute(
                    "INSERT INTO publication_provenance "
                    "(request_id, record_json, record_sha256, created_at) VALUES (?, ?, ?, ?)",
                    (record.request_id, canonical, digest, time.time()),
                )
                connection.commit()
        except PublicationError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise PublicationError("provenance_failed") from None


class FixedBareGitTransport:
    """Publish exact frozen commits to one configured local bare repository.

    This implementation is deliberately local-filesystem-only. The fixed remote
    path and numeric repository identity are supplied by gateway configuration;
    worker requests cannot select either. It uses object fetch plus atomic
    create-only ref update, never force, tags, mirrors, hooks or network URLs.
    """

    def __init__(
        self,
        remote_git_dir: str | os.PathLike[str],
        *,
        repository_id: int,
        git_executable: str | None = None,
    ) -> None:
        try:
            remote = Path(remote_git_dir).resolve(strict=True)
        except (OSError, RuntimeError):
            raise PublicationError("invalid_input") from None
        executable = git_executable or shutil.which("git")
        if (
            type(repository_id) is not int
            or repository_id <= 0
            or not executable
            or not Path(executable).is_absolute()
            or not remote.is_dir()
            or not (remote / "objects").is_dir()
            or not (remote / "refs").is_dir()
            or not (remote / "HEAD").is_file()
        ):
            raise PublicationError("invalid_input")
        self._remote = remote
        self._repository_id = repository_id
        self._git = executable

    def __call__(self, command: PublishCommand, /) -> PublishResult:
        if not isinstance(command, PublishCommand):
            raise PublicationError("publication_failed")
        expected_ref = f"refs/heads/clawdot/{command.request_id}"
        if (
            type(command.repository_id) is not int
            or command.repository_id != self._repository_id
            or not _OBJECT_ID.fullmatch(command.frozen_commit)
            or not _REQUEST_ID.fullmatch(command.request_id)
            or command.refspec != f"{command.frozen_commit}:{expected_ref}"
        ):
            raise PublicationError("publication_mismatch")
        source = Path(command.quarantine_git_dir)
        if (
            not source.is_dir()
            or not (source / "objects").is_dir()
            or not (source / "HEAD").is_file()
        ):
            raise PublicationError("publication_failed")

        existing = self._read_ref(expected_ref)
        if existing is not None:
            if existing == command.frozen_commit:
                return PublishResult(existing, expected_ref)
            raise PublicationError("publication_mismatch")

        self._run(
            self._remote,
            "-c",
            "protocol.file.allow=always",
            "-c",
            "core.hooksPath=/dev/null",
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            str(source),
            command.frozen_commit,
        )
        self._run(
            self._remote,
            "cat-file",
            "-e",
            f"{command.frozen_commit}^{{commit}}",
        )
        update = f"create {expected_ref} {command.frozen_commit}\n".encode("ascii")
        try:
            self._run(self._remote, "update-ref", "--stdin", stdin=update)
        except PublicationError:
            current = self._read_ref(expected_ref)
            if current == command.frozen_commit:
                return PublishResult(current, expected_ref)
            raise PublicationError("publication_mismatch") from None
        current = self._read_ref(expected_ref)
        if current != command.frozen_commit:
            raise PublicationError("publication_mismatch")
        return PublishResult(current, expected_ref)

    def _read_ref(self, ref: str) -> str | None:
        output = self._run(
            self._remote,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "--",
            ref,
        )
        if not output:
            return None
        try:
            lines = output.decode("ascii", errors="strict").splitlines()
        except UnicodeDecodeError:
            raise PublicationError("publication_failed") from None
        if len(lines) != 1:
            raise PublicationError("publication_failed")
        ref_name, separator, candidate = lines[0].partition(" ")
        if not separator or ref_name != ref:
            raise PublicationError("publication_failed")
        if not _OBJECT_ID.fullmatch(candidate):
            raise PublicationError("publication_failed")
        return candidate

    def _run(
        self,
        git_dir: Path,
        *args: str,
        stdin: bytes | None = None,
        allow_missing: bool = False,
    ) -> bytes:
        command = [
            self._git,
            "--git-dir",
            str(git_dir),
            "--no-pager",
            "--no-replace-objects",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ]
        env = _clean_git_env(self._git, self._remote.parent)
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.Popen(  # noqa: S603 - fixed local bare-remote plumbing.
                    command,
                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    env=env,
                    close_fds=True,
                    preexec_fn=_limit_git_output_for(MAX_BUNDLE_BYTES),
                )
                try:
                    process.communicate(stdin, timeout=COMMAND_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise PublicationError("timeout") from None
                stdout.seek(0, os.SEEK_END)
                out_size = stdout.tell()
                stderr.seek(0, os.SEEK_END)
                err_size = stderr.tell()
                if out_size > 64 * 1024 or err_size > 64 * 1024:
                    raise PublicationError("size_limit")
                stdout.seek(0)
                output = stdout.read(64 * 1024 + 1)
        except PublicationError:
            raise
        except (OSError, subprocess.SubprocessError):
            raise PublicationError("publication_failed") from None
        if process.returncode and not (allow_missing and process.returncode == 1):
            raise PublicationError("publication_failed")
        return output


class ScannerBinaryAdapter:
    """Run an installed scanner binary using a bounded JSON-result/stdin-byte protocol.

    The trusted executable receives the fixed arguments —config PATH
    —surface-name NAME, reads the unmodified surface bytes from stdin, and writes
    exactly {"verdict":"clean|finding|error"} to stdout.
    """

    def __init__(
        self,
        executable: str | os.PathLike[str],
        configuration: str | os.PathLike[str],
        *,
        expected_executable_sha256: str,
        expected_configuration_sha256: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        try:
            config_name = Path(configuration).name
        except (TypeError, ValueError):
            raise PublicationError("scanner_unavailable") from None
        if _is_private_path(config_name):
            raise PublicationError("scanner_unavailable")
        self.executable = _verified_file(
            executable, expected_executable_sha256, MAX_SCANNER_BINARY_BYTES
        )
        self.configuration = _verified_file(
            configuration, expected_configuration_sha256, MAX_SCANNER_CONFIG_BYTES
        )
        if not 0.1 <= timeout_seconds <= 60.0:
            raise PublicationError("scanner_unavailable")
        self.timeout_seconds = timeout_seconds
        self._binary_sha256 = expected_executable_sha256
        self._configuration_sha256 = expected_configuration_sha256
        self.identity_sha256 = hashlib.sha256(
            bytes.fromhex(expected_executable_sha256) + bytes.fromhex(expected_configuration_sha256)
        ).hexdigest()

    def __call__(self, item: ScanInput, /) -> ScanVerdict:
        # Recheck both protected installed files before execution. Deployment
        # must keep them root-owned/immutable to close the verify/exec race.
        _verified_file(self.executable, self._binary_sha256, MAX_SCANNER_BINARY_BYTES)
        _verified_file(self.configuration, self._configuration_sha256, MAX_SCANNER_CONFIG_BYTES)
        command = [
            str(self.executable),
            "--config",
            str(self.configuration),
            "--surface-name",
            item.surface_name,
        ]
        with tempfile.TemporaryDirectory(prefix="clawdot-scanner-") as temp:
            home = Path(temp)
            env = {
                "PATH": str(self.executable.parent),
                "HOME": str(home),
                "TMPDIR": str(home),
                "LC_ALL": "C",
            }
            try:
                with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                    process = subprocess.Popen(  # noqa: S603 - pinned trusted scanner executable.
                        command,
                        stdin=subprocess.PIPE,
                        stdout=stdout,
                        stderr=stderr,
                        env=env,
                        close_fds=True,
                        preexec_fn=_limit_scanner_output,
                    )
                    try:
                        process.communicate(item.content, timeout=self.timeout_seconds)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                        raise PublicationError("scanner_failed") from None
                    if process.returncode != 0:
                        raise PublicationError("scanner_failed")
                    stdout.seek(0, os.SEEK_END)
                    out_size = stdout.tell()
                    stderr.seek(0, os.SEEK_END)
                    err_size = stderr.tell()
                    if out_size > 256 or err_size != 0:
                        raise PublicationError("scanner_failed")
                    stdout.seek(0)
                    result = stdout.read(257)
            except PublicationError:
                raise
            except Exception:
                raise PublicationError("scanner_failed") from None
        if result.endswith(b"\n"):
            result = result[:-1]
        return {
            b'{"verdict":"clean"}': ScanVerdict.CLEAN,
            b'{"verdict":"finding"}': ScanVerdict.FINDING,
            b'{"verdict":"error"}': ScanVerdict.ERROR,
        }.get(result, ScanVerdict.ERROR)


class PublicationService:
    """Validated gateway core with mandatory trusted external adapters."""

    def __init__(
        self,
        *,
        policy: PolicyManifest,
        scanner: Scanner,
        scanner_identity_sha256: str,
        baseline_history_scan_complete: bool,
        record_provenance: ProvenanceStore,
        publish_transport: PublishTransport,
        git_executable: str | None = None,
    ) -> None:
        configured_scanner_identity = getattr(scanner, "identity_sha256", None)
        if (
            not isinstance(policy, PolicyManifest)
            or not callable(scanner)
            or not callable(record_provenance)
            or not callable(publish_transport)
            or not isinstance(scanner_identity_sha256, str)
            or not _SHA256.fullmatch(scanner_identity_sha256)
            or (
                isinstance(configured_scanner_identity, str)
                and configured_scanner_identity != scanner_identity_sha256
            )
            or type(baseline_history_scan_complete) is not bool
            or not baseline_history_scan_complete
        ):
            raise PublicationError("scanner_unavailable")
        executable = git_executable or shutil.which("git")
        if not executable or not Path(executable).is_absolute():
            raise PublicationError("invalid_input")
        self._policy = policy
        self._scanner = scanner
        self._scanner_identity = scanner_identity_sha256
        self._record_provenance = record_provenance
        self._publish_transport = publish_transport
        self._git = executable

    def publish(self, request: PublicationRequest) -> PublicationReceipt:
        self._validate_request(request)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="clawdot-quarantine-") as temp:
            root = Path(temp)
            bundle_path = root / "incoming.bundle"
            bundle_path.write_bytes(request.bundle)
            git_dir = root / "repo.git"
            self._init_quarantine(git_dir, len(request.head_commit))
            self._run_git(git_dir, "bundle", "verify", str(bundle_path), started=started)
            self._run_git(
                git_dir,
                "bundle",
                "unbundle",
                str(bundle_path),
                started=started,
                file_size_limit=MAX_BUNDLE_BYTES,
            )

            try:
                bundle = collect_release_bundle(
                    git_dir,
                    request.deployed_commit,
                    request.base_commit,
                    request.head_commit,
                    self._policy,
                )
            except GitEvidenceError as exc:
                raise PublicationError(_map_evidence_error(exc.code)) from None
            if not bundle.base_is_ancestor_of_head:
                raise PublicationError("invalid_bundle")

            scanner_state = _ScanState(
                scanner=self._scanner,
                item_limit=MAX_SCAN_ITEM_BYTES,
                total_limit=MAX_TOTAL_SCAN_BYTES,
                started=started,
            )
            try:
                scanner_state.scan_required_surfaces(
                    request.specification,
                    request.pull_request_title,
                    request.pull_request_body,
                    request.artifacts,
                )
                self._scan_history(
                    git_dir,
                    request.deployed_commit,
                    request.head_commit,
                    scanner_state,
                    started,
                )
            except PublicationError:
                raise
            except Exception:
                raise PublicationError("scanner_failed") from None

            _check_deadline(started)

            ref = f"refs/heads/clawdot/{request.request_id}"
            record = ProvenanceRecord(
                request_id=request.request_id,
                deployed_commit=bundle.deployed_commit,
                base_commit=bundle.base_commit,
                head_commit=bundle.head_commit,
                policy_digest=bundle.policy_digest,
                scanner_identity_sha256=self._scanner_identity,
                baseline_history_scan_complete=True,
                bundle_sha256=hashlib.sha256(request.bundle).hexdigest(),
                surface_manifest_sha256=scanner_state.surface_manifest_sha256,
                scanned_surface_count=scanner_state.surface_count,
                scanned_bytes=scanner_state.total_bytes,
            )
            try:
                self._record_provenance(record)
            except Exception:
                raise PublicationError("provenance_failed") from None

            _check_deadline(started)

            command = PublishCommand(
                request_id=request.request_id,
                repository_id=self._policy.repository_id,
                frozen_commit=bundle.head_commit,
                refspec=f"{bundle.head_commit}:{ref}",
                quarantine_git_dir=git_dir,
            )
            try:
                result = self._publish_transport(command)
            except Exception:
                raise PublicationError("publication_failed") from None
            if (
                not isinstance(result, PublishResult)
                or result.commit != bundle.head_commit
                or result.ref != ref
            ):
                raise PublicationError("publication_mismatch")
            return PublicationReceipt(
                request_id=request.request_id,
                deployed_commit=bundle.deployed_commit,
                base_commit=bundle.base_commit,
                head_commit=bundle.head_commit,
                policy_digest=bundle.policy_digest,
                scanner_identity_sha256=self._scanner_identity,
                published_ref=ref,
                bundle_sha256=record.bundle_sha256,
                surface_manifest_sha256=record.surface_manifest_sha256,
            )

    def _validate_request(self, request: PublicationRequest) -> None:
        if (
            not isinstance(request, PublicationRequest)
            or not isinstance(request.request_id, str)
            or not _REQUEST_ID.fullmatch(request.request_id)
            or any(
                not isinstance(commit, str) or not _OBJECT_ID.fullmatch(commit)
                for commit in (
                    request.deployed_commit,
                    request.base_commit,
                    request.head_commit,
                )
            )
            or len(
                {len(request.deployed_commit), len(request.base_commit), len(request.head_commit)}
            )
            != 1
            or not isinstance(request.bundle, bytes)
            or not 1 <= len(request.bundle) <= MAX_BUNDLE_BYTES
            or not all(
                isinstance(surface, bytes) and (surface or name == "pull_request_body")
                for name, surface in (
                    ("specification", request.specification),
                    ("pull_request_title", request.pull_request_title),
                    ("pull_request_body", request.pull_request_body),
                )
            )
            or not isinstance(request.artifacts, tuple)
            or len(request.artifacts) > MAX_ARTIFACTS
            or any(
                not isinstance(item, ScanSurface)
                or not isinstance(item.name, str)
                or not isinstance(item.content, bytes)
                for item in request.artifacts
            )
        ):
            raise PublicationError("invalid_input")
        if request.head_commit in {request.deployed_commit, request.base_commit}:
            raise PublicationError("invalid_input")

    def _init_quarantine(self, git_dir: Path, oid_length: int) -> None:
        env = _clean_git_env(self._git, git_dir.parent)
        command = [
            self._git,
            "init",
            "--bare",
            "--quiet",
            f"--object-format={'sha256' if oid_length == 64 else 'sha1'}",
            str(git_dir),
        ]
        try:
            subprocess.run(  # noqa: S603 - fixed, bounded git init in private quarantine.
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                close_fds=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            raise PublicationError("invalid_bundle") from None

    def _run_git(
        self,
        git_dir: Path,
        *args: str,
        started: float,
        output_limit: int = MAX_OUTPUT_BYTES,
        file_size_limit: int = MAX_OUTPUT_BYTES,
    ) -> bytes:
        command = [
            self._git,
            "--git-dir",
            str(git_dir),
            "--no-pager",
            "--no-replace-objects",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.pager=cat",
            "-c",
            "diff.external=",
            "-c",
            "protocol.allow=never",
            *args,
        ]
        remaining = MAX_TOTAL_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise PublicationError("timeout")
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                proc = subprocess.Popen(  # noqa: S603 - fixed git plumbing; no shell or helpers.
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    env=_clean_git_env(self._git, git_dir.parent),
                    close_fds=True,
                    preexec_fn=_limit_git_output_for(file_size_limit),
                )
                try:
                    status = proc.wait(timeout=min(COMMAND_TIMEOUT_SECONDS, remaining))
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    raise PublicationError("timeout") from None
                stdout.seek(0, os.SEEK_END)
                out_size = stdout.tell()
                stderr.seek(0, os.SEEK_END)
                err_size = stderr.tell()
                if out_size >= MAX_OUTPUT_BYTES or err_size > 64 * 1024:
                    raise PublicationError("size_limit")
                if out_size > output_limit:
                    raise PublicationError("size_limit")
                stdout.seek(0)
                output = stdout.read(output_limit + 1)
        except PublicationError:
            raise
        except (OSError, subprocess.SubprocessError):
            raise PublicationError("invalid_git_output") from None
        if status:
            raise PublicationError("invalid_bundle")
        return output

    def _scan_history(
        self,
        git_dir: Path,
        deployed: str,
        head: str,
        state: _ScanState,
        started: float,
    ) -> None:
        forbidden_oids = self._forbidden_baseline_blobs(git_dir, deployed, started)
        records = self._run_git(
            git_dir,
            "rev-list",
            "--topo-order",
            "--reverse",
            "--parents",
            "--max-count",
            str(MAX_HISTORY_COMMITS + 1),
            f"{deployed}..{head}",
            started=started,
            output_limit=256 * 1024,
        ).splitlines()
        if len(records) > MAX_HISTORY_COMMITS:
            raise PublicationError("size_limit")
        scanned_blobs: set[bytes] = set()
        for record in records:
            parts = record.split()
            if not parts or any(
                not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", part) for part in parts
            ):
                raise PublicationError("invalid_git_output")
            commit, *parents = parts
            commit_text = commit.decode("ascii")
            metadata = self._run_git(
                git_dir,
                "cat-file",
                "commit",
                commit_text,
                started=started,
                output_limit=MAX_SCAN_ITEM_BYTES,
            )
            state.scan_text(f"commit:{commit_text}", metadata)
            if not parents:
                entries = self._tree_entries(git_dir, commit_text, started)
                changes = tuple(
                    _RawChange(path, b"000000", mode, _zero_id(len(commit)), oid, b"A")
                    for path, mode, oid in entries
                )
                self._validate_edge(changes)
                for change in changes:
                    state.scan_text("git_path", change.path.encode("utf-8"))
                    self._scan_blob(
                        git_dir,
                        change.new_mode,
                        change.new_oid,
                        forbidden_oids,
                        scanned_blobs,
                        state,
                        started,
                    )
            else:
                for parent in parents:
                    raw = self._run_git(
                        git_dir,
                        "diff-tree",
                        "--raw",
                        "-z",
                        "--no-abbrev",
                        "--no-renames",
                        "--no-commit-id",
                        "-r",
                        parent.decode("ascii"),
                        commit_text,
                        started=started,
                    )
                    changes = _parse_raw_diff(raw)
                    self._validate_edge(changes)
                    for change in changes:
                        state.scan_text("git_path", change.path.encode("utf-8"))
                        for mode, oid in (
                            (change.old_mode, change.old_oid),
                            (change.new_mode, change.new_oid),
                        ):
                            self._scan_blob(
                                git_dir,
                                mode,
                                oid,
                                forbidden_oids,
                                scanned_blobs,
                                state,
                                started,
                            )

    def _validate_edge(self, changes: Sequence[_RawChange]) -> None:
        policy_changes: list[Change] = []
        for item in changes:
            if _is_private_path(item.path):
                raise PublicationError("private_path")
            if _is_protected(item.path, self._policy.protected_paths):
                raise PublicationError("protected_path")
            old_type = _MODES.get(item.old_mode)
            new_type = _MODES.get(item.new_mode)
            if item.old_mode != b"000000" and old_type != "blob":
                raise PublicationError("unsupported_mode")
            if item.new_mode != b"000000" and new_type != "blob":
                raise PublicationError("unsupported_mode")
            change_type = {
                b"A": "added",
                b"D": "deleted",
                b"M": "modified",
                b"T": "modified",
            }.get(item.status)
            if change_type is None:
                raise PublicationError("invalid_git_output")
            mode_type = old_type if item.status == b"D" else new_type
            policy_changes.append(Change(item.path, change_type, mode_type or "tree"))
        try:
            self._policy.validate_changes(tuple(policy_changes))
        except PolicyError as exc:
            raise PublicationError(_map_policy_error(exc.code)) from None

    def _scan_blob(
        self,
        git_dir: Path,
        mode: bytes,
        object_id: bytes,
        forbidden_oids: set[bytes],
        scanned: set[bytes],
        state: _ScanState,
        started: float,
    ) -> None:
        if mode == b"000000" or object_id in _ZERO_ID:
            return
        if mode not in {b"100644", b"100755"}:
            raise PublicationError("unsupported_mode")
        if object_id in forbidden_oids:
            raise PublicationError("copied_forbidden_blob")
        if object_id in scanned:
            return
        content = self._run_git(
            git_dir,
            "cat-file",
            "blob",
            object_id.decode("ascii"),
            started=started,
            output_limit=MAX_SCAN_ITEM_BYTES,
        )
        state.scan_text(f"git_blob:{object_id.decode('ascii')}", content)
        scanned.add(object_id)

    def _forbidden_baseline_blobs(self, git_dir: Path, deployed: str, started: float) -> set[bytes]:
        commits = self._run_git(
            git_dir,
            "rev-list",
            "--topo-order",
            "--max-count",
            str(MAX_HISTORY_COMMITS + 1),
            deployed,
            started=started,
            output_limit=256 * 1024,
        ).splitlines()
        if len(commits) > MAX_HISTORY_COMMITS or any(
            not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", commit) for commit in commits
        ):
            raise PublicationError("size_limit")
        forbidden: set[bytes] = set()
        for commit in commits:
            entries = self._tree_entries(git_dir, commit.decode("ascii"), started)
            for path, mode, object_id in entries:
                if mode in {b"100644", b"100755"} and (
                    _is_private_path(path) or _is_protected(path, self._policy.protected_paths)
                ):
                    forbidden.add(object_id)
        return forbidden

    def _tree_entries(
        self, git_dir: Path, commit: str, started: float
    ) -> list[tuple[str, bytes, bytes]]:
        raw = self._run_git(git_dir, "ls-tree", "-r", "-z", "--full-tree", commit, started=started)
        if raw and not raw.endswith(b"\0"):
            raise PublicationError("invalid_git_output")
        fields = raw.split(b"\0")[:-1]
        if len(fields) > 100_000:
            raise PublicationError("size_limit")
        entries: list[tuple[str, bytes, bytes]] = []
        for entry in fields:
            header, separator, raw_path = entry.partition(b"\t")
            parts = header.split(b" ")
            if not separator or len(parts) != 3:
                raise PublicationError("invalid_git_output")
            mode, object_type, oid = parts
            if object_type not in {b"blob", b"commit"} or not re.fullmatch(
                rb"[0-9a-f]{40}|[0-9a-f]{64}", oid
            ):
                raise PublicationError("invalid_git_output")
            path = _decode_git_path(raw_path)
            entries.append((path, mode, oid))
        return entries


@dataclass(frozen=True)
class _RawChange:
    path: str
    old_mode: bytes
    new_mode: bytes
    old_oid: bytes
    new_oid: bytes
    status: bytes


class _ScanState:
    def __init__(self, scanner: Scanner, item_limit: int, total_limit: int, started: float) -> None:
        self.scanner = scanner
        self.item_limit = item_limit
        self.total_limit = total_limit
        self.started = started
        self.total_bytes = 0
        self.surface_count = 0
        self._required_names: set[str] = set()
        self._manifest_entries: list[tuple[str, str]] = []

    @property
    def surface_manifest_sha256(self) -> str:
        canonical = json.dumps(
            sorted(self._manifest_entries),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()

    def scan_required_surfaces(
        self,
        specification: bytes,
        title: bytes,
        body: bytes,
        artifacts: tuple[ScanSurface, ...],
    ) -> None:
        surfaces = [
            ("specification", specification),
            ("pull_request_title", title),
            ("pull_request_body", body),
        ]
        names = {"specification", "pull_request_title", "pull_request_body"}
        for artifact in artifacts:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", artifact.name) or artifact.name in names:
                raise PublicationError("invalid_input")
            names.add(artifact.name)
            surfaces.append(
                (
                    f"artifact:{artifact.name}",
                    artifact.name.encode("utf-8") + b"\n" + artifact.content,
                )
            )
        for name, content in surfaces:
            self.scan_text(name, content)

    def scan_text(self, name: str, content: bytes) -> None:
        _check_deadline(self.started)
        if (
            not isinstance(name, str)
            or not name
            or len(name.encode("utf-8", errors="strict")) > 256
            or not isinstance(content, bytes)
            or len(content) > self.item_limit
        ):
            raise PublicationError("size_limit")
        self.total_bytes += len(content)
        if self.total_bytes > self.total_limit:
            raise PublicationError("size_limit")
        try:
            content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise PublicationError("binary_content") from None
        if b"\0" in content:
            raise PublicationError("binary_content")
        try:
            verdict = self.scanner(ScanInput(name, content))
        except Exception:
            raise PublicationError("scanner_failed") from None
        _check_deadline(self.started)
        if verdict is not ScanVerdict.CLEAN:
            raise PublicationError(
                "scanner_finding" if verdict is ScanVerdict.FINDING else "scanner_failed"
            )
        self.surface_count += 1
        self._manifest_entries.append((name, hashlib.sha256(content).hexdigest()))


def _parse_raw_diff(raw: bytes) -> list[_RawChange]:
    if not raw:
        return []
    fields = raw.split(b"\0")
    if fields[-1] != b"":
        raise PublicationError("invalid_git_output")
    fields.pop()
    result: list[_RawChange] = []
    index = 0
    while index < len(fields):
        header = fields[index]
        index += 1
        match = re.fullmatch(
            rb":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}|[0-9a-f]{64}) "
            rb"([0-9a-f]{40}|[0-9a-f]{64}) ([A-Z][0-9]*)",
            header,
        )
        if match is None or index >= len(fields):
            raise PublicationError("invalid_git_output")
        old_mode, new_mode, old_oid, new_oid, status = match.groups()
        path = _decode_git_path(fields[index])
        index += 1
        result.append(_RawChange(path, old_mode, new_mode, old_oid, new_oid, status))
        if len(result) > 2048:
            raise PublicationError("size_limit")
    return result


def _decode_git_path(raw: bytes) -> str:
    try:
        path = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise PublicationError("binary_content") from None
    if not path or "\0" in path or "\\" in path or path.startswith("/"):
        raise PublicationError("invalid_git_output")
    return path


def _is_protected(path: str, protected_paths: tuple[str, ...]) -> bool:
    return any(
        path == protected or path.startswith(protected + "/") for protected in protected_paths
    )


def _map_policy_error(code: str) -> str:
    return {
        "private_path": "private_path",
        "protected_change": "protected_path",
        "unapproved_path": "unapproved_path",
        "invalid_object_type": "unsupported_mode",
    }.get(code, "invalid_git_output")


def _map_evidence_error(code: str) -> str:
    return {
        "private_path": "private_path",
        "protected_path": "protected_path",
        "unsupported_mode": "unsupported_mode",
        "unapproved_path": "unapproved_path",
        "missing_object": "missing_object",
        "timeout": "timeout",
    }.get(code, "invalid_bundle")


def _clean_git_env(executable: str, home: Path) -> dict[str, str]:
    return {
        "PATH": os.path.dirname(executable),
        "HOME": str(home),
        "TMPDIR": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "LC_ALL": "C",
    }


def _limit_git_output_for(limit: int):
    def apply_limit() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    return apply_limit


def _verified_file(path: str | os.PathLike[str], expected_sha256: str, limit: int) -> Path:
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        raise PublicationError("scanner_unavailable")
    try:
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_file() or _file_sha256(resolved, limit) != expected_sha256:
            raise PublicationError("scanner_unavailable")
        return resolved
    except PublicationError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise PublicationError("scanner_unavailable") from None


def _file_sha256(path: Path, limit: int) -> str:
    try:
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                total += len(chunk)
                if total > limit:
                    raise PublicationError("scanner_unavailable")
                digest.update(chunk)
    except OSError:
        raise PublicationError("scanner_unavailable") from None
    return digest.hexdigest()


def _limit_scanner_output() -> None:
    import resource

    limit = 256 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))


def _check_deadline(started: float) -> None:
    if time.monotonic() - started > MAX_TOTAL_SECONDS:
        raise PublicationError("timeout")


def _zero_id(oid_length: int) -> bytes:
    return b"0" * oid_length
