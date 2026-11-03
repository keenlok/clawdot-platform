"""Bounded, read-only evidence extraction from local Git objects.

This module supplies mechanical Git facts to policy code. It does not scan blob
contents for secrets, establish human review, or authenticate remote evidence.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from .policy import Change, PolicyError, PolicyManifest, ReleaseBundle, _is_private_path

MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_HISTORY_COMMITS = 2048
MAX_TREE_ENTRIES = 100_000
MAX_CLOSURE_BLOB_BYTES = 1024 * 1024
MAX_TOTAL_SECONDS = 90.0
COMMAND_TIMEOUT_SECONDS = 8.0
_OBJECT_ID = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MODES = {b"100644": "blob", b"100755": "blob", b"120000": "symlink", b"160000": "commit"}


class GitEvidenceError(ValueError):
    """A bounded, non-sensitive failure while extracting local Git evidence."""

    def __init__(self, code: str = "incomplete_git_evidence") -> None:
        if code not in {
            "invalid_input",
            "invalid_repository",
            "missing_object",
            "invalid_git_output",
            "private_path",
            "protected_path",
            "unapproved_path",
            "unsupported_mode",
            "closure_mismatch",
            "history_limit",
            "output_limit",
            "timeout",
            "incomplete_git_evidence",
        }:
            code = "incomplete_git_evidence"
        super().__init__(code)
        self.code = code


def collect_release_bundle(
    repository: str | os.PathLike[str],
    deployed_commit: str,
    base_commit: str,
    head_commit: str,
    policy: PolicyManifest,
    *,
    same_tree_continuation_recorded: bool = False,
) -> ReleaseBundle:
    """Build complete D/B/H evidence without checking out or executing source."""
    if not isinstance(policy, PolicyManifest) or type(same_tree_continuation_recorded) is not bool:
        raise GitEvidenceError("invalid_input")
    ids = (deployed_commit, base_commit, head_commit)
    if (
        any(not isinstance(item, str) or not _valid_oid(item) for item in ids)
        or len({len(item) for item in ids if isinstance(item, str)}) != 1
    ):
        raise GitEvidenceError("invalid_input")
    try:
        repo = Path(repository).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GitEvidenceError("invalid_repository") from exc
    if not repo.is_dir():
        raise GitEvidenceError("invalid_repository")

    git = shutil.which("git")
    if git is None:
        raise GitEvidenceError("invalid_repository")
    with tempfile.TemporaryDirectory(prefix="release-git-evidence-") as home:
        runner = _GitRunner(git, repo, Path(home), len(deployed_commit))
        try:
            commits = tuple(runner.commit(item) for item in ids)
            d, b, h = commits
            trees = tuple(runner.tree(item) for item in commits)
            d_tree, b_tree, h_tree = trees

            b_to_h = tuple(runner.diff(b, h))
            d_to_b = tuple(runner.diff(d, b))
            d_to_h = tuple(runner.diff(d, h))
            runner.inspect_introduced_history(d, h, policy)

            return ReleaseBundle(
                deployed_commit=d,
                base_commit=b,
                head_commit=h,
                deployed_tree=d_tree,
                base_tree=b_tree,
                head_tree=h_tree,
                policy_digest=policy.digest,
                base_is_ancestor_of_head=runner.is_ancestor(b, h),
                base_is_descendant_of_deployed=runner.is_ancestor(d, b),
                same_tree_continuation_recorded=same_tree_continuation_recorded,
                # Complete object evidence is not a trusted review attestation.
                complete_delta_reviewed=False,
                deployed_to_base=d_to_b,
                base_to_head=b_to_h,
                deployed_to_head=d_to_h,
            )
        except GitEvidenceError:
            raise
        except (OSError, PolicyError, UnicodeError, ValueError) as exc:
            raise GitEvidenceError("invalid_git_output") from exc


def collect_delta_stats(
    repository: str | os.PathLike[str], older: str, newer: str
) -> tuple[tuple[str, int | None, int | None], ...]:
    if (
        not isinstance(older, str)
        or not isinstance(newer, str)
        or not _valid_oid(older)
        or not _valid_oid(newer)
        or len(older) != len(newer)
    ):
        raise GitEvidenceError("invalid_input")
    repo = _evidence_repo(repository)
    with tempfile.TemporaryDirectory(prefix="release-git-evidence-") as home:
        runner = _GitRunner(shutil.which("git") or "git", repo, Path(home), len(older))
        runner.commit(older)
        runner.commit(newer)
        raw = runner.run(
            "diff-tree", "--numstat", "-z", "--no-renames", "-r", "--no-commit-id", older, newer
        )
    output = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        fields = entry.split(b"\t")
        if len(fields) != 3:
            raise GitEvidenceError("invalid_git_output")
        try:
            path = _decode_path(fields[2])
            added, deleted = (
                (None, None) if fields[:2] == [b"-", b"-"] else (int(fields[0]), int(fields[1]))
            )
        except (ValueError, UnicodeError) as exc:
            raise GitEvidenceError("invalid_git_output") from exc
        output.append((path, added, deleted))
    return tuple(output)


def collect_delta_patch(
    repository: str | os.PathLike[str], older: str, newer: str, *, max_bytes: int
) -> bytes | None:
    if (
        not isinstance(older, str)
        or not isinstance(newer, str)
        or not _valid_oid(older)
        or not _valid_oid(newer)
        or len(older) != len(newer)
        or type(max_bytes) is not int
        or max_bytes < 0
    ):
        raise GitEvidenceError("invalid_input")
    repo = _evidence_repo(repository)
    with tempfile.TemporaryDirectory(prefix="release-git-evidence-") as home:
        runner = _GitRunner(shutil.which("git") or "git", repo, Path(home), len(older))
        runner.commit(older)
        runner.commit(newer)
        try:
            return runner.run(
                "diff-tree",
                "-p",
                "-r",
                "--no-renames",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--full-index",
                "--no-commit-id",
                older,
                newer,
                output_limit=max_bytes,
            )
        except GitEvidenceError as exc:
            if exc.code == "output_limit":
                return None
            raise


def _evidence_repo(repository: str | os.PathLike[str]) -> Path:
    try:
        repo = Path(repository).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise GitEvidenceError("invalid_repository") from exc
    if not repo.is_dir() or shutil.which("git") is None:
        raise GitEvidenceError("invalid_repository")
    return repo


def verify_installed_blob_digests(
    repository: str | os.PathLike[str],
    workflow_commit: str,
    expected_blobs: Mapping[str, str],
) -> None:
    """Verify an operator-pinned execution-closure path→SHA-256 map.

    This proves only that each listed path is a regular blob with the expected
    content at the pinned commit. It does not infer whether the operator listed
    the complete transitive execution closure.
    """
    if (
        not isinstance(workflow_commit, str)
        or not _valid_oid(workflow_commit)
        or not isinstance(expected_blobs, Mapping)
        or not 1 <= len(expected_blobs) <= 2048
    ):
        raise GitEvidenceError("invalid_input")
    try:
        expected = tuple(expected_blobs.items())
    except (TypeError, ValueError) as exc:
        raise GitEvidenceError("invalid_input") from exc
    for path, digest in expected:
        if (
            not isinstance(path, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or _is_private_path(path)
        ):
            raise GitEvidenceError("invalid_input")
        try:
            Change(path)
        except PolicyError as exc:
            raise GitEvidenceError("invalid_input") from exc

    try:
        repo = Path(repository).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GitEvidenceError("invalid_repository") from exc
    git = shutil.which("git")
    if git is None or not repo.is_dir():
        raise GitEvidenceError("invalid_repository")
    oid_length = len(workflow_commit)
    with tempfile.TemporaryDirectory(prefix="release-closure-evidence-") as home:
        runner = _GitRunner(git, repo, Path(home), oid_length)
        commit = runner.commit(workflow_commit)
        listing = runner.run("ls-tree", "-r", "-z", "--full-tree", commit)
        if listing and not listing.endswith(b"\0"):
            raise GitEvidenceError("invalid_git_output")
        entries = listing.split(b"\0")[:-1]
        if len(entries) > MAX_TREE_ENTRIES:
            raise GitEvidenceError("output_limit")
        found: dict[str, tuple[bytes, bytes]] = {}
        wanted = {path for path, _ in expected}
        for entry in entries:
            header, separator, raw_path = entry.partition(b"\t")
            parts = header.split(b" ")
            if not separator or len(parts) != 3:
                raise GitEvidenceError("invalid_git_output")
            mode, object_type, object_id = parts
            if object_type not in {b"blob", b"commit"} or not _OBJECT_ID.fullmatch(object_id):
                raise GitEvidenceError("invalid_git_output")
            path = _decode_path(raw_path)
            if path in wanted:
                if mode not in {b"100644", b"100755"} or object_type != b"blob":
                    raise GitEvidenceError("closure_mismatch")
                found[path] = (mode, object_id)

        for path, expected_digest in expected:
            item = found.get(path)
            if item is None:
                raise GitEvidenceError("closure_mismatch")
            _, object_id = item
            content = runner.run(
                "cat-file", "blob", object_id.decode("ascii"), output_limit=MAX_CLOSURE_BLOB_BYTES
            )
            if hashlib.sha256(content).hexdigest() != expected_digest:
                raise GitEvidenceError("closure_mismatch")


def _valid_oid(value: str) -> bool:
    try:
        encoded = value.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        return False
    return _OBJECT_ID.fullmatch(encoded) is not None


class _GitRunner:
    def __init__(self, executable: str, repo: Path, home: Path, oid_length: int = 40) -> None:
        self.executable = executable
        self.repo = repo
        self.started = time.monotonic()
        self.home = home
        object_directory = _object_directory(repo)
        self.git_dir = home / "gitdir"
        self._initialize_git_dir(oid_length)
        self.env = {
            "PATH": os.path.dirname(executable),
            "HOME": str(home),
            "TMPDIR": str(home),
            "GIT_DIR": str(self.git_dir),
            "GIT_OBJECT_DIRECTORY": str(object_directory),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "LC_ALL": "C",
        }

    def _initialize_git_dir(self, oid_length: int) -> None:
        env = {
            "PATH": os.path.dirname(self.executable),
            "HOME": str(self.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "LC_ALL": "C",
        }
        command = [
            self.executable,
            "init",
            "--bare",
            "--quiet",
            f"--object-format={'sha256' if oid_length == 64 else 'sha1'}",
            str(self.git_dir),
        ]
        try:
            subprocess.run(  # noqa: S603 - fixed git init for an isolated temporary gitdir.
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                close_fds=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitEvidenceError("invalid_repository") from exc

    def run(self, *args: str, output_limit: int = MAX_OUTPUT_BYTES) -> bytes:
        remaining = MAX_TOTAL_SECONDS - (time.monotonic() - self.started)
        timeout = min(COMMAND_TIMEOUT_SECONDS, remaining)
        if timeout <= 0:
            raise GitEvidenceError("timeout")
        command = [
            self.executable,
            "--git-dir",
            str(self.git_dir),
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
            "diff.renames=false",
            "-c",
            "protocol.allow=never",
            *args,
        ]
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                proc = subprocess.Popen(  # noqa: S603 - fixed command, resolved executable.
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    env=self.env,
                    close_fds=True,
                    preexec_fn=_limit_output_file,
                )  # noqa: S603 - fixed plumbing command and resolved executable.
                try:
                    return_code = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    proc.kill()
                    proc.wait()
                    raise GitEvidenceError("timeout") from exc
                stdout.seek(0, os.SEEK_END)
                out_size = stdout.tell()
                stderr.seek(0, os.SEEK_END)
                err_size = stderr.tell()
                if out_size >= MAX_OUTPUT_BYTES or err_size >= MAX_OUTPUT_BYTES:
                    raise GitEvidenceError("output_limit")
                if out_size > output_limit or err_size > 64 * 1024:
                    raise GitEvidenceError("output_limit")
                stdout.seek(0)
                output = stdout.read(output_limit + 1)
        except GitEvidenceError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitEvidenceError("incomplete_git_evidence") from exc
        if return_code != 0:
            raise GitEvidenceError("missing_object")
        return output

    def commit(self, object_id: str) -> str:
        raw = self.run("rev-parse", "--verify", "--end-of-options", f"{object_id}^{{commit}}")
        value = raw.rstrip(b"\n")
        if not _OBJECT_ID.fullmatch(value):
            raise GitEvidenceError("invalid_git_output")
        # Require the supplied name to resolve to the exact immutable commit ID.
        if value.decode("ascii") != object_id:
            raise GitEvidenceError("invalid_input")
        return object_id

    def tree(self, commit: str) -> str:
        raw = self.run("show", "-s", "--format=%T", commit)
        value = raw.rstrip(b"\n")
        if not _OBJECT_ID.fullmatch(value):
            raise GitEvidenceError("invalid_git_output")
        return value.decode("ascii")

    def is_ancestor(self, older: str, newer: str) -> bool:
        raw = self.run_status("merge-base", "--is-ancestor", older, newer)
        if raw == 0:
            return True
        if raw == 1:
            return False
        raise GitEvidenceError("invalid_git_output")

    def run_status(self, *args: str) -> int:
        # A separate bounded-output path is needed because --is-ancestor uses 1
        # for a valid negative answer.
        remaining = MAX_TOTAL_SECONDS - (time.monotonic() - self.started)
        timeout = min(COMMAND_TIMEOUT_SECONDS, remaining)
        if timeout <= 0:
            raise GitEvidenceError("timeout")
        command = [
            self.executable,
            "--git-dir",
            str(self.git_dir),
            "--no-pager",
            "--no-replace-objects",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "protocol.allow=never",
            *args,
        ]
        try:
            proc = subprocess.run(  # noqa: S603 - fixed command, resolved executable.
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self.env,
                close_fds=True,
                timeout=timeout,
                check=False,
            )  # noqa: S603 - fixed plumbing command and resolved executable.
        except subprocess.TimeoutExpired as exc:
            raise GitEvidenceError("timeout") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitEvidenceError("incomplete_git_evidence") from exc
        return proc.returncode

    def diff(self, older: str, newer: str) -> list[Change]:
        raw = self.run(
            "diff-tree",
            "--raw",
            "-z",
            "--no-abbrev",
            "--no-renames",
            "--no-commit-id",
            "-r",
            older,
            newer,
        )
        if not raw:
            return []
        fields = raw.split(b"\0")
        if fields[-1] != b"":
            raise GitEvidenceError("invalid_git_output")
        fields.pop()
        result: list[Change] = []
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
                raise GitEvidenceError("invalid_git_output")
            old_mode, new_mode, _, _, status = match.groups()
            if any(mode not in {b"000000", b"100644", b"100755"} for mode in (old_mode, new_mode)):
                raise GitEvidenceError("unsupported_mode")
            path = _decode_path(fields[index])
            index += 1
            if status in {b"A", b"D"}:
                change_type = "added" if status == b"A" else "deleted"
            elif status in {b"M", b"T"}:
                change_type = "modified"
            else:
                raise GitEvidenceError("invalid_git_output")
            mode = old_mode if change_type == "deleted" else new_mode
            object_type = _MODES.get(mode)
            if object_type is None:
                raise GitEvidenceError("unsupported_mode")
            result.append(Change(path=path, change_type=change_type, object_type=object_type))
            if len(result) > 2048:
                raise GitEvidenceError("output_limit")
        return result

    def inspect_introduced_history(self, deployed: str, head: str, policy: PolicyManifest) -> None:
        raw = self.run(
            "rev-list",
            "--topo-order",
            "--reverse",
            "--parents",
            "--max-count",
            str(MAX_HISTORY_COMMITS + 1),
            f"{deployed}..{head}",
            output_limit=256 * 1024,
        )
        if raw and not raw.endswith(b"\n"):
            raise GitEvidenceError("invalid_git_output")
        records = raw.splitlines()
        if len(records) > MAX_HISTORY_COMMITS:
            raise GitEvidenceError("history_limit")
        for record in records:
            fields = record.split()
            if not fields or any(not _OBJECT_ID.fullmatch(field) for field in fields):
                raise GitEvidenceError("invalid_git_output")
            commit, *parents = fields
            if not parents:
                listing = self.run("ls-tree", "-r", "-z", "--full-tree", commit.decode("ascii"))
                if listing and not listing.endswith(b"\0"):
                    raise GitEvidenceError("invalid_git_output")
                entries = listing.split(b"\0")[:-1]
                if len(entries) > MAX_TREE_ENTRIES:
                    raise GitEvidenceError("output_limit")
                for entry in entries:
                    header, separator, raw_path = entry.partition(b"\t")
                    parts = header.split(b" ")
                    if not separator or len(parts) != 3:
                        raise GitEvidenceError("invalid_git_output")
                    mode, object_type, object_id = parts
                    if object_type not in {b"blob", b"commit"} or not _OBJECT_ID.fullmatch(
                        object_id
                    ):
                        raise GitEvidenceError("invalid_git_output")
                    change = Change(
                        _decode_path(raw_path),
                        "added",
                        _MODES.get(mode, "tree"),
                    )
                    self._validate_history_change(change, policy)
            else:
                for parent in parents:
                    for change in self.diff(parent.decode("ascii"), commit.decode("ascii")):
                        self._validate_history_change(change, policy)

    @staticmethod
    def _validate_history_change(change: Change, policy: PolicyManifest) -> None:
        if _is_private_path(change.path):
            raise GitEvidenceError("private_path")
        if change.path in policy.protected_paths:
            raise GitEvidenceError("protected_path")
        if change.object_type != "blob":
            raise GitEvidenceError("unsupported_mode")
        if change.path not in policy.allowed_paths:
            raise GitEvidenceError("unapproved_path")


def _decode_path(raw: bytes) -> str:
    try:
        path = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GitEvidenceError("invalid_git_output") from exc
    if not path or "\x00" in path or "\\" in path or path.startswith("/"):
        raise GitEvidenceError("invalid_git_output")
    return path


def _object_directory(repository: Path) -> Path:
    """Resolve Git's pointer metadata without loading repository config files."""
    dot_git = repository / ".git"
    if dot_git.is_dir():
        git_dir = dot_git
    elif dot_git.is_file():
        raw = _read_small_file(dot_git, 4096)
        try:
            line = raw.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise GitEvidenceError("invalid_repository") from exc
        if not line.startswith("gitdir: ") or "\n" in line:
            raise GitEvidenceError("invalid_repository")
        target = Path(line[8:])
        git_dir = (repository / target).resolve() if not target.is_absolute() else target.resolve()
    elif (repository / "objects").is_dir():
        git_dir = repository
    else:
        raise GitEvidenceError("invalid_repository")

    common_dir = git_dir
    commondir_file = git_dir / "commondir"
    if commondir_file.exists():
        raw = _read_small_file(commondir_file, 4096)
        try:
            relative = raw.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise GitEvidenceError("invalid_repository") from exc
        if not relative or "\n" in relative:
            raise GitEvidenceError("invalid_repository")
        common_dir = (git_dir / relative).resolve()

    object_directory = common_dir / "objects"
    if not object_directory.is_dir():
        raise GitEvidenceError("invalid_repository")
    for shallow_file in (git_dir / "shallow", common_dir / "shallow"):
        if shallow_file.exists() and _read_small_file(shallow_file, 1024 * 1024).strip():
            raise GitEvidenceError("incomplete_git_evidence")
    alternates = object_directory / "info" / "alternates"
    if alternates.exists() and _read_small_file(alternates, 64 * 1024).strip():
        raise GitEvidenceError("incomplete_git_evidence")
    return object_directory


def _read_small_file(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as exc:
        raise GitEvidenceError("invalid_repository") from exc
    if len(raw) > limit:
        raise GitEvidenceError("output_limit")
    return raw


def _limit_output_file() -> None:
    # Applied to the child only: Git output is redirected to temporary regular
    # files, so the kernel caps it before Python reads it into memory.
    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
