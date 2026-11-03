"""Build deterministic candidate Git objects from isolated worker output.

The candidate worker returns text file data. This module writes those bytes to a
private temporary object database using plumbing commands; it never checks out,
imports, or executes candidate content and never writes into the source repo.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .development import DevelopmentResult
from .git_evidence import (
    COMMAND_TIMEOUT_SECONDS,
    MAX_OUTPUT_BYTES,
    GitEvidenceError,
    _GitRunner,
    _object_directory,
    collect_release_bundle,
)
from .policy import Change, PolicyError, PolicyManifest, _is_private_path
from .publication import MAX_BUNDLE_BYTES, PublicationRequest, ScanSurface

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REQUEST_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_MAX_FILES = 64
_MAX_FILE_BYTES = 32 * 1024
_MAX_PATCH_BYTES = 128 * 1024
_MAX_SURFACE_BYTES = 64 * 1024
_AUTHOR_NAME = "Chatcore Release Gateway"
_AUTHOR_EMAIL = "release-gateway@invalid"


class CandidateBundleError(ValueError):
    """A candidate object construction failure without source details."""

    def __init__(self, code: str = "candidate_bundle_invalid") -> None:
        if code not in {
            "candidate_bundle_invalid",
            "candidate_policy_denied",
            "candidate_git_unavailable",
            "candidate_git_failed",
            "candidate_size_limit",
        }:
            code = "candidate_bundle_invalid"
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _Input:
    request_id: str
    deployed_commit: str
    base_commit: str
    policy: PolicyManifest
    result: DevelopmentResult
    timestamp: int
    specification: bytes
    title: bytes
    body: bytes
    artifacts: tuple[ScanSurface, ...]


def build_candidate_bundle(
    *,
    repository: str | os.PathLike[str],
    result: DevelopmentResult,
    request_id: str,
    deployed_commit: str,
    base_commit: str,
    policy: PolicyManifest,
    commit_timestamp: int,
    specification: bytes,
    pull_request_title: bytes,
    pull_request_body: bytes,
    artifacts: tuple[ScanSurface, ...] = (),
) -> PublicationRequest:
    """Return a full bundle whose new commit is the exact isolated worker output."""
    request = _validate_input(
        result=result,
        request_id=request_id,
        deployed_commit=deployed_commit,
        base_commit=base_commit,
        policy=policy,
        commit_timestamp=commit_timestamp,
        specification=specification,
        title=pull_request_title,
        body=pull_request_body,
        artifacts=artifacts,
    )
    git = shutil.which("git")
    try:
        source = Path(repository).resolve(strict=True)
        source_objects = _object_directory(source)
    except (OSError, RuntimeError, GitEvidenceError):
        raise CandidateBundleError("candidate_git_unavailable") from None
    if not git or not Path(git).is_absolute() or not source.is_dir():
        raise CandidateBundleError("candidate_git_unavailable")

    try:
        with tempfile.TemporaryDirectory(prefix="candidate-bundle-") as temp:
            root = Path(temp)
            oid_length = len(request.base_commit)
            validation = _GitRunner(git, source, root / "source-reader", oid_length)
            validation.commit(request.deployed_commit)
            validation.commit(request.base_commit)
            if not validation.is_ancestor(request.deployed_commit, request.base_commit):
                raise CandidateBundleError("candidate_policy_denied")

            git_dir = root / "candidate.git"
            env = _isolated_env(git, root, git_dir, source_objects)
            _run(
                git,
                env,
                root,
                "init",
                "--bare",
                "--quiet",
                _object_format(oid_length),
                str(git_dir),
            )
            env["GIT_DIR"] = str(git_dir)
            env["GIT_OBJECT_DIRECTORY"] = str(git_dir / "objects")
            env["GIT_INDEX_FILE"] = str(root / "index")
            _run(git, env, root, "cat-file", "-e", f"{request.deployed_commit}^{{commit}}")
            _run(git, env, root, "cat-file", "-e", f"{request.base_commit}^{{commit}}")
            _run(git, env, root, "read-tree", request.base_commit)

            total = 0
            records = bytearray()
            for path, text in request.result.candidate_files:
                data = text.encode("utf-8", errors="strict")
                total += len(data)
                if not data or len(data) > _MAX_FILE_BYTES or total > _MAX_PATCH_BYTES:
                    raise CandidateBundleError("candidate_size_limit")
                blob_oid = _run(
                    git, env, root, "hash-object", "--no-filters", "-w", "--stdin", stdin=data
                )
                blob = blob_oid.rstrip(b"\n")
                if not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", blob):
                    raise CandidateBundleError("candidate_git_failed")
                records.extend(b"100644 " + blob + b"\t" + path.encode("utf-8") + b"\0")
            _run(
                git, env, root, "update-index", "--add", "-z", "--index-info", stdin=bytes(records)
            )
            tree = _run(git, env, root, "write-tree").rstrip(b"\n").decode("ascii")
            date = f"@{request.timestamp} +0000"
            commit_env = dict(env)
            commit_env.update(
                {
                    "GIT_AUTHOR_NAME": _AUTHOR_NAME,
                    "GIT_AUTHOR_EMAIL": _AUTHOR_EMAIL,
                    "GIT_AUTHOR_DATE": date,
                    "GIT_COMMITTER_NAME": _AUTHOR_NAME,
                    "GIT_COMMITTER_EMAIL": _AUTHOR_EMAIL,
                    "GIT_COMMITTER_DATE": date,
                }
            )
            message = f"Candidate release {request.request_id}\n".encode("ascii")
            head = (
                _run(
                    git,
                    commit_env,
                    root,
                    "-c",
                    "commit.gpgSign=false",
                    "commit-tree",
                    tree,
                    "-p",
                    request.base_commit,
                    stdin=message,
                )
                .rstrip(b"\n")
                .decode("ascii")
            )
            if not _OID.fullmatch(head):
                raise CandidateBundleError("candidate_git_failed")
            for ref, oid in (
                ("refs/heads/release-deployed", request.deployed_commit),
                ("refs/heads/release-base", request.base_commit),
                (f"refs/heads/clawdot/{request.request_id}", head),
            ):
                _run(git, env, root, "update-ref", ref, oid)

            # Copy only reachable source objects from the validated raw-object
            # alternate into this owned store, then detach before P01 inspection.
            _run(git, env, root, "repack", "-a", "-d")
            env.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
            collected = collect_release_bundle(
                git_dir,
                request.deployed_commit,
                request.base_commit,
                head,
                request.policy,
            )
            if (
                collected.head_commit != head
                or collected.base_commit != request.base_commit
                or not collected.base_is_ancestor_of_head
                or not collected.base_is_descendant_of_deployed
            ):
                raise CandidateBundleError("candidate_policy_denied")
            bundle_path = root / "candidate.bundle"
            _run(
                git,
                env,
                root,
                "bundle",
                "create",
                str(bundle_path),
                "refs/heads/release-deployed",
                "refs/heads/release-base",
                f"refs/heads/clawdot/{request.request_id}",
            )
            bundle = bundle_path.read_bytes()
            if not 1 <= len(bundle) <= MAX_BUNDLE_BYTES:
                raise CandidateBundleError("candidate_size_limit")
            return PublicationRequest(
                request_id=request.request_id,
                deployed_commit=request.deployed_commit,
                base_commit=request.base_commit,
                head_commit=head,
                bundle=bundle,
                specification=request.specification,
                pull_request_title=request.title,
                pull_request_body=request.body,
                artifacts=request.artifacts,
            )
    except CandidateBundleError:
        raise
    except (GitEvidenceError, PolicyError):
        raise CandidateBundleError("candidate_policy_denied") from None
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, TypeError):
        raise CandidateBundleError("candidate_git_failed") from None


def _validate_input(
    *,
    result: DevelopmentResult,
    request_id: str,
    deployed_commit: str,
    base_commit: str,
    policy: PolicyManifest,
    commit_timestamp: int,
    specification: bytes,
    title: bytes,
    body: bytes,
    artifacts: tuple[ScanSurface, ...],
) -> _Input:
    try:
        if (
            not isinstance(result, DevelopmentResult)
            or not isinstance(policy, PolicyManifest)
            or not isinstance(request_id, str)
            or not _REQUEST_ID.fullmatch(request_id)
            or any(
                not isinstance(oid, str) or not _OID.fullmatch(oid)
                for oid in (deployed_commit, base_commit)
            )
            or len(deployed_commit) != len(base_commit)
            or result.base_commit != base_commit
            or result.policy_digest != policy.digest
            or not isinstance(result.report, bytes)
            or result.report_digest != hashlib.sha256(result.report).hexdigest()
            or type(commit_timestamp) is not int
            or not 0 <= commit_timestamp <= 4_102_444_800
            or any(not isinstance(value, bytes) for value in (specification, title, body))
            or not specification.strip()
            or not title.strip()
            or max(len(specification), len(title), len(body)) > _MAX_SURFACE_BYTES
            or any(b"\0" in value for value in (specification, title, body))
            or not isinstance(artifacts, tuple)
            or len(artifacts) > 128
            or any(
                not isinstance(item, ScanSurface)
                or not isinstance(item.name, str)
                or not isinstance(item.content, bytes)
                for item in artifacts
            )
        ):
            raise CandidateBundleError()
        files = result.candidate_files
        if not isinstance(files, tuple) or not 1 <= len(files) <= _MAX_FILES:
            raise CandidateBundleError()
        paths: list[str] = []
        changes: list[Change] = []
        for entry in files:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise CandidateBundleError()
            path, text = entry
            if not isinstance(path, str) or not isinstance(text, str) or "\0" in text:
                raise CandidateBundleError()
            data = text.encode("utf-8", errors="strict")
            if not data or len(data) > _MAX_FILE_BYTES:
                raise CandidateBundleError("candidate_size_limit")
            if _is_private_path(path):
                raise CandidateBundleError("candidate_policy_denied")
            paths.append(path)
            changes.append(Change(path, "modified", "blob"))
        if paths != sorted(set(paths)):
            raise CandidateBundleError()
        policy.validate_changes(tuple(changes))
        return _Input(
            request_id,
            deployed_commit,
            base_commit,
            policy,
            result,
            commit_timestamp,
            specification,
            title,
            body,
            artifacts,
        )
    except CandidateBundleError:
        raise
    except (PolicyError, UnicodeError, ValueError, TypeError):
        raise CandidateBundleError("candidate_policy_denied") from None


def _object_format(oid_length: int) -> str:
    return f"--object-format={'sha256' if oid_length == 64 else 'sha1'}"


def _isolated_env(git: str, root: Path, git_dir: Path, source_objects: Path) -> dict[str, str]:
    return {
        "PATH": str(Path(git).parent),
        "HOME": str(root),
        "TMPDIR": str(root),
        "GIT_DIR": str(git_dir),
        "GIT_OBJECT_DIRECTORY": str(git_dir / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(source_objects),
        "GIT_INDEX_FILE": str(root / "index"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "LC_ALL": "C",
    }


def _run(
    git: str,
    env: dict[str, str],
    cwd: Path,
    *args: str,
    stdin: bytes | None = None,
) -> bytes:
    command = [
        git,
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
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(  # noqa: S603 - fixed Git plumbing without shell.
                command,
                cwd=cwd,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                env=env,
                close_fds=True,
                preexec_fn=_limit_output,
            )
            try:
                process.communicate(stdin, timeout=COMMAND_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise CandidateBundleError("candidate_git_failed") from None
            stdout.seek(0, os.SEEK_END)
            stderr.seek(0, os.SEEK_END)
            if stdout.tell() > MAX_OUTPUT_BYTES or stderr.tell() > 64 * 1024:
                raise CandidateBundleError("candidate_size_limit")
            stdout.seek(0)
            result = stdout.read(MAX_OUTPUT_BYTES + 1)
    except CandidateBundleError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise CandidateBundleError("candidate_git_failed") from None
    if process.returncode != 0:
        raise CandidateBundleError("candidate_git_failed")
    return result


def _limit_output() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
