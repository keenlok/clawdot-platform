"""Materialize exact PR commit objects into an isolated local object store.

The fetcher never checks out or runs repository content. Authentication is
provided to Git's HTTPS askpass prompt from a temporary 0600 file and is never
placed in arguments, environment values, Git config, or output.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .git_evidence import (
    GitEvidenceError,
    _object_directory,
    collect_release_bundle,
)
from .policy import PolicyManifest

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_./+=:-]{16,4096}\Z")
_PACK = re.compile(r"pack-[0-9a-f]{40,64}\.(?:pack|idx)\Z")
MAX_FETCH_PACK_BYTES = 512 * 1024 * 1024
MAX_FETCH_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_FETCH_SECONDS = 120.0
MAX_COMMAND_SECONDS = 45.0
MAX_OUTPUT_BYTES = 64 * 1024


class GitFetchError(ValueError):
    """A fixed safe code for fetch and promotion failures."""

    def __init__(self, code: str = "git_fetch_failed") -> None:
        if code not in {
            "invalid_git_fetch_config",
            "git_fetch_failed",
            "git_fetch_timeout",
            "git_fetch_size_limit",
            "git_fetch_incomplete",
            "git_fetch_policy_denied",
        }:
            code = "git_fetch_failed"
        super().__init__(code)
        self.code = code


class GitProcessExecutor(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        output_limit: int = MAX_OUTPUT_BYTES,
    ) -> bytes: ...


class SubprocessGitExecutor:
    """Bounded Git subprocess runner with fixed resource and output caps."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        output_limit: int = MAX_OUTPUT_BYTES,
    ) -> bytes:
        if (
            not isinstance(args, (tuple, list))
            or not args
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in args)
            or not isinstance(env, Mapping)
            or type(timeout) not in {int, float}
            or not 0 < timeout <= MAX_COMMAND_SECONDS
            or type(output_limit) is not int
            or not 1 <= output_limit <= MAX_OUTPUT_BYTES
        ):
            raise GitFetchError("invalid_git_fetch_config")
        clean = {
            "PATH": env["PATH"],
            "HOME": env["HOME"],
            "TMPDIR": env["TMPDIR"],
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_DIR": env["GIT_DIR"],
            "GIT_OBJECT_DIRECTORY": env["GIT_OBJECT_DIRECTORY"],
            "GIT_ASKPASS": env["GIT_ASKPASS"],
            "GIT_ASKPASS_REQUIRE": "force",
            "GIT_RELEASE_CONTROL_TOKEN_FILE": env["GIT_RELEASE_CONTROL_TOKEN_FILE"],
        }
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.Popen(  # noqa: S603 - fixed Git plumbing; repository data is never executed.
                    list(args),
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    env=clean,
                    close_fds=True,
                    preexec_fn=_limit_file_size,
                )
                try:
                    status = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise GitFetchError("git_fetch_timeout") from None
                stdout.seek(0, os.SEEK_END)
                stdout_size = stdout.tell()
                stderr.seek(0, os.SEEK_END)
                stderr_size = stderr.tell()
                if stdout_size > output_limit or stderr_size > output_limit:
                    raise GitFetchError("git_fetch_size_limit")
                stdout.seek(0)
                data = stdout.read(output_limit + 1)
                if status != 0 or len(data) > output_limit:
                    raise GitFetchError("git_fetch_failed")
                return data
        except GitFetchError:
            raise
        except (OSError, subprocess.SubprocessError):
            raise GitFetchError("git_fetch_failed") from None


@dataclass(frozen=True, repr=False)
class GitObjectFetcher:
    """Fetch and promote complete D/B/H objects for one installed repository."""

    repository: Path
    owner: str
    name: str
    token: str = field(repr=False)
    git_executable: str = "/usr/bin/git"
    executor: GitProcessExecutor = field(default_factory=SubprocessGitExecutor, repr=False)

    def ensure_history(
        self,
        *,
        pull_request_number: int,
        deployed_commit: str,
        base_commit: str,
        head_commit: str,
        policy: PolicyManifest,
    ) -> None:
        self._validate(pull_request_number, deployed_commit, base_commit, head_commit, policy)
        try:
            collect_release_bundle(
                self.repository,
                deployed_commit,
                base_commit,
                head_commit,
                policy,
            )
            return
        except GitEvidenceError as exc:
            if exc.code != "missing_object":
                raise GitFetchError("git_fetch_policy_denied") from None

        started = time.monotonic()
        target_objects = _object_directory(self.repository)
        if (target_objects / "info" / "alternates").exists():
            raise GitFetchError("invalid_git_fetch_config")
        with tempfile.TemporaryDirectory(prefix="release-ci-fetch-") as temporary:
            root = Path(temporary)
            git_dir = root / "quarantine.git"
            token_file = root / "token"
            askpass = root / "askpass"
            self._write_token(token_file)
            self._write_askpass(askpass)
            (git_dir / "objects").mkdir(parents=True, mode=0o700)
            oid_length = len(head_commit)
            self._run(
                root,
                git_dir,
                token_file,
                askpass,
                started,
                "init",
                "--bare",
                "--quiet",
                f"--object-format={'sha256' if oid_length == 64 else 'sha1'}",
                str(git_dir),
            )
            ref = f"refs/ci/pull/{pull_request_number}/head"
            remote = f"https://github.com/{self.owner}/{self.name}.git"
            self._run(
                root,
                git_dir,
                token_file,
                askpass,
                started,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "http.followRedirects=false",
                "-c",
                "credential.helper=",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--no-write-fetch-head",
                "--",
                remote,
                f"+refs/pull/{pull_request_number}/head:{ref}",
            )
            if (git_dir / "shallow").exists():
                raise GitFetchError("git_fetch_incomplete")
            observed = self._run(
                root,
                git_dir,
                token_file,
                askpass,
                started,
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{ref}^{{commit}}",
            ).strip()
            if observed.decode("ascii", errors="strict") != head_commit:
                raise GitFetchError("git_fetch_incomplete")
            for oid in (deployed_commit, base_commit, head_commit):
                self._run(
                    root,
                    git_dir,
                    token_file,
                    askpass,
                    started,
                    "cat-file",
                    "-e",
                    f"{oid}^{{commit}}",
                )
            try:
                collect_release_bundle(
                    git_dir,
                    deployed_commit,
                    base_commit,
                    head_commit,
                    policy,
                )
            except GitEvidenceError:
                raise GitFetchError("git_fetch_policy_denied") from None
            self._run(
                root,
                git_dir,
                token_file,
                askpass,
                started,
                "update-ref",
                "refs/ci/deployed",
                deployed_commit,
            )
            self._run(
                root,
                git_dir,
                token_file,
                askpass,
                started,
                "update-ref",
                "refs/ci/base",
                base_commit,
            )
            self._run(
                root,
                git_dir,
                token_file,
                askpass,
                started,
                "repack",
                "-a",
                "-d",
            )
            packs = _verified_packs(git_dir / "objects")
            if sum(size for _, size, _ in packs) > MAX_FETCH_TOTAL_BYTES:
                raise GitFetchError("git_fetch_size_limit")
            _promote_packs(packs, target_objects)
        try:
            collect_release_bundle(
                self.repository,
                deployed_commit,
                base_commit,
                head_commit,
                policy,
            )
        except GitEvidenceError:
            raise GitFetchError("git_fetch_incomplete") from None

    def _validate(
        self,
        number: int,
        deployed: str,
        base: str,
        head: str,
        policy: PolicyManifest,
    ) -> None:
        try:
            repo = self.repository.resolve(strict=True)
            object_dir = _object_directory(repo)
            git_stat = Path(self.git_executable).stat()
        except (OSError, GitEvidenceError, RuntimeError):
            raise GitFetchError("invalid_git_fetch_config") from None
        if (
            not repo.is_dir()
            or not object_dir.is_dir()
            or any(not _OID.fullmatch(item) for item in (deployed, base, head))
            or len({len(item) for item in (deployed, base, head)}) != 1
            or type(number) is not int
            or not 1 <= number <= 2**31 - 1
            or not isinstance(policy, PolicyManifest)
            or not _REPOSITORY.fullmatch(self.owner)
            or not _REPOSITORY.fullmatch(self.name)
            or not _TOKEN.fullmatch(self.token)
            or not stat.S_ISREG(git_stat.st_mode)
            or not os.access(self.git_executable, os.X_OK)
        ):
            raise GitFetchError("invalid_git_fetch_config")

    def _run(
        self,
        root: Path,
        git_dir: Path,
        token_file: Path,
        askpass: Path,
        started: float,
        *args: str,
    ) -> bytes:
        elapsed = time.monotonic() - started
        timeout = min(MAX_COMMAND_SECONDS, MAX_FETCH_SECONDS - elapsed)
        if timeout <= 0:
            raise GitFetchError("git_fetch_timeout")
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(root),
            "TMPDIR": str(root),
            "GIT_DIR": str(git_dir),
            "GIT_OBJECT_DIRECTORY": str(git_dir / "objects"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": str(askpass),
            "GIT_ASKPASS_REQUIRE": "force",
            "GIT_RELEASE_CONTROL_TOKEN_FILE": str(token_file),
        }
        try:
            result = self.executor.run(
                (self.git_executable, "--no-pager", *args),
                cwd=root,
                env=env,
                timeout=timeout,
                output_limit=MAX_OUTPUT_BYTES,
            )
        except GitFetchError:
            raise
        except Exception:
            raise GitFetchError("git_fetch_failed") from None
        if not isinstance(result, bytes) or len(result) > MAX_OUTPUT_BYTES:
            raise GitFetchError("git_fetch_size_limit")
        if time.monotonic() - started > MAX_FETCH_SECONDS:
            raise GitFetchError("git_fetch_timeout")
        return result

    def _write_token(self, path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                content = self.token.encode("ascii")
                view = memoryview(content)
                while view:
                    size = os.write(descriptor, view)
                    if size <= 0:
                        raise OSError
                    view = view[size:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            raise GitFetchError("git_fetch_failed") from None

    @staticmethod
    def _write_askpass(path: Path) -> None:
        content = (
            "#!/bin/sh\n"
            'case "${1:-}" in\n'
            "  *sername*) printf '%s\\n' 'x-access-token' ;;\n"
            '  *assword*) /bin/cat -- "$GIT_RELEASE_CONTROL_TOKEN_FILE" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n"
        ).encode("ascii")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
            try:
                os.write(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(path, 0o700)
        except OSError:
            raise GitFetchError("git_fetch_failed") from None


def _verified_packs(object_dir: Path) -> list[tuple[Path, int, str]]:
    pack_dir = object_dir / "pack"
    try:
        info = pack_dir.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError
        files = sorted(pack_dir.iterdir())
        packs: dict[str, Path] = {}
        indexes: dict[str, Path] = {}
        for path in files:
            file_info = path.lstat()
            if not stat.S_ISREG(file_info.st_mode) or not _PACK.fullmatch(path.name):
                continue
            stem, extension = path.name.rsplit(".", 1)
            (packs if extension == "pack" else indexes)[stem] = path
        if not packs or set(packs) != set(indexes):
            raise ValueError
        results = []
        for stem in sorted(packs):
            # Install the index first and the pack last so interruption can
            # leave an ignored orphan index, never an unindexed pack.
            for path in (indexes[stem], packs[stem]):
                size = path.stat().st_size
                if not 1 <= size <= MAX_FETCH_PACK_BYTES:
                    raise ValueError
                digest = _hash_file(path, MAX_FETCH_PACK_BYTES)
                results.append((path, size, digest))
        return results
    except (OSError, ValueError):
        raise GitFetchError("git_fetch_incomplete") from None


def _promote_packs(packs: list[tuple[Path, int, str]], destination_objects: Path) -> None:
    pack_directory = destination_objects / "pack"
    try:
        pack_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = pack_directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError
        for source, size, digest in packs:
            target = pack_directory / source.name
            if target.exists():
                if not stat.S_ISREG(target.lstat().st_mode) or _hash_file(target, size) != digest:
                    raise ValueError
                continue
            temporary = pack_directory / f".{source.name}.tmp-{os.getpid()}"
            with source.open("rb") as inp, temporary.open("xb") as out:
                remaining = size
                while remaining:
                    chunk = inp.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError
                    out.write(chunk)
                    remaining -= len(chunk)
                out.flush()
                os.fsync(out.fileno())
            if _hash_file(temporary, size) != digest:
                temporary.unlink(missing_ok=True)
                raise ValueError
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        directory_fd = os.open(pack_directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, ValueError):
        raise GitFetchError("git_fetch_failed") from None


def _hash_file(path: Path, maximum: int) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            total += len(chunk)
            if total > maximum:
                raise ValueError
            digest.update(chunk)
    return digest.hexdigest()


def _limit_file_size() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FETCH_PACK_BYTES, MAX_FETCH_PACK_BYTES))
        resource.setrlimit(
            resource.RLIMIT_CPU, (int(MAX_COMMAND_SECONDS) + 5, int(MAX_COMMAND_SECONDS) + 5)
        )
    except (ImportError, OSError, ValueError):
        os._exit(127)
