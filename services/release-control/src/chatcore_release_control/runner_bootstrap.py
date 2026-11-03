"""Pinned-image bootstrap for one disposable Actions runner.

This module deliberately uses only the Python standard library so the immutable
runner image can copy it as a single audited file. Registration uses the pinned
standard runner's exact interactive token prompt. A PTY with terminal echo
disabled carries the token; unknown prompts or transcripts fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import termios
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from urllib.parse import urlsplit

_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64}\Z")
_RUNNER_NAME = re.compile(r"runner-[a-f0-9]{32}\Z")
_RUNNER_VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}\Z")
_RUNNER_VERSION_BANNER = re.compile(rb"Runner version:\s*['\"]([0-9]+\.[0-9]+\.[0-9]+)['\"]")
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,4096}\Z")
_PROTOCOL = "actions-runner-config-prompt-v1"
_REGISTRATION_PROMPT = b"Enter the runner registration token:"
_SOCKET_PATH = "/tmp/p09-registration.sock"  # noqa: S108
_RUNNER_ROOT = "/opt/p09/runner-files"
_RELAY_HELPER_PATH = "/opt/p09/runner_relay.py"
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_RUNNER_FILES = 4096
_MAX_RUNNER_BYTES = 2 * 1024**3
_MAX_PROCESS_OUTPUT = 64 * 1024
_MAX_PROCESS_SECONDS = 3600


class BootstrapError(RuntimeError):
    """Safe fixed bootstrap failure without token or subprocess diagnostics."""


@dataclass(frozen=True, slots=True)
class RunnerImageManifest:
    schema_version: int
    base_image: str
    runner_archive_sha256: str
    bootstrap_sha256: str
    relay_helper_sha256: str
    runner_closure_sha256: str
    runner_root: str
    runner_binary: str
    runner_run_script: str
    runner_version: str
    runner_files: Mapping[str, str]
    registration_protocol: str

    def __post_init__(self) -> None:
        frozen_files = dict(self.runner_files)
        object.__setattr__(self, "runner_files", MappingProxyType(frozen_files))
        closure_input = {
            "base_image": self.base_image,
            "runner_archive_sha256": self.runner_archive_sha256,
            "bootstrap_sha256": self.bootstrap_sha256,
            "relay_helper_sha256": self.relay_helper_sha256,
            "runner_binary": self.runner_binary,
            "runner_run_script": self.runner_run_script,
            "runner_version": self.runner_version,
            "runner_files": frozen_files,
            "registration_protocol": self.registration_protocol,
        }
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or not isinstance(self.base_image, str)
            or not _IMAGE.fullmatch(self.base_image)
            or not _valid_digest(self.runner_archive_sha256)
            or not _valid_digest(self.bootstrap_sha256)
            or not _valid_digest(self.relay_helper_sha256)
            or not _valid_digest(self.runner_closure_sha256)
            or not _absolute_path(self.runner_root)
            or not _relative_path(self.runner_binary)
            or not _relative_path(self.runner_run_script)
            or not isinstance(self.runner_version, str)
            or not _RUNNER_VERSION.fullmatch(self.runner_version)
            or self.runner_binary != "config.sh"
            or self.runner_run_script != "run.sh"
            or not frozen_files
            or any(
                not _relative_path(path) or not _valid_digest(digest)
                for path, digest in frozen_files.items()
            )
            or self.runner_binary not in frozen_files
            or self.runner_run_script not in frozen_files
            or self.runner_binary == self.runner_run_script
            or self.registration_protocol != _PROTOCOL
            or compute_runner_closure_digest(closure_input) != self.runner_closure_sha256
        ):
            raise BootstrapError("runner image manifest is invalid")

    @classmethod
    def from_mapping(cls, value: object) -> RunnerImageManifest:
        fields = {
            "schema_version",
            "base_image",
            "runner_archive_sha256",
            "bootstrap_sha256",
            "relay_helper_sha256",
            "runner_closure_sha256",
            "runner_root",
            "runner_binary",
            "runner_run_script",
            "runner_version",
            "runner_files",
            "registration_protocol",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise BootstrapError("runner image manifest is invalid")
        files = value.get("runner_files")
        if (
            type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or not isinstance(value.get("base_image"), str)
            or not _IMAGE.fullmatch(value["base_image"])
            or not _valid_digest(value.get("runner_archive_sha256"))
            or not _valid_digest(value.get("bootstrap_sha256"))
            or not _valid_digest(value.get("relay_helper_sha256"))
            or not _valid_digest(value.get("runner_closure_sha256"))
            or not _absolute_path(value.get("runner_root"))
            or not _relative_path(value.get("runner_binary"))
            or not _relative_path(value.get("runner_run_script"))
            or not isinstance(value.get("runner_version"), str)
            or not _RUNNER_VERSION.fullmatch(value["runner_version"])
            or value.get("runner_binary") != "config.sh"
            or value.get("runner_run_script") != "run.sh"
            or not isinstance(files, Mapping)
            or not 1 <= len(files) <= _MAX_RUNNER_FILES
            or any(
                not _relative_path(path) or not _valid_digest(digest)
                for path, digest in files.items()
            )
            or value["runner_binary"] not in files
            or value["runner_run_script"] not in files
            or value["runner_binary"] == value["runner_run_script"]
            or value.get("registration_protocol") != _PROTOCOL
        ):
            raise BootstrapError("runner image manifest is invalid")
        copied_files = dict(files)
        manifest = cls(
            schema_version=1,
            base_image=value["base_image"],
            runner_archive_sha256=value["runner_archive_sha256"],
            bootstrap_sha256=value["bootstrap_sha256"],
            relay_helper_sha256=value["relay_helper_sha256"],
            runner_closure_sha256=value["runner_closure_sha256"],
            runner_root=value["runner_root"],
            runner_binary=value["runner_binary"],
            runner_run_script=value["runner_run_script"],
            runner_version=value["runner_version"],
            runner_files=copied_files,
            registration_protocol=_PROTOCOL,
        )
        if compute_runner_closure_digest(value) != manifest.runner_closure_sha256:
            raise BootstrapError("runner image closure digest does not match")
        return manifest


@dataclass(frozen=True, slots=True)
class RunnerProcessResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


RunnerExecutor = Callable[..., RunnerProcessResult]


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _absolute_path(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        return False
    return ".." not in PurePosixPath(value).parts and not any(ch.isspace() for ch in value)


def _relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value or value.startswith("/"):
        return False
    if re.fullmatch(r"[A-Za-z0-9._/-]+", value) is None:
        return False
    path = PurePosixPath(value)
    return (
        path.as_posix() == value
        and not any(part in {"", ".", ".."} for part in path.parts)
        and not any(ch.isspace() for ch in value)
    )


def _canonical_closure_fields(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "base_image": value.get("base_image"),
        "runner_archive_sha256": value.get("runner_archive_sha256"),
        "bootstrap_sha256": value.get("bootstrap_sha256"),
        "relay_helper_sha256": value.get("relay_helper_sha256"),
        "runner_binary": value.get("runner_binary"),
        "runner_run_script": value.get("runner_run_script"),
        "runner_version": value.get("runner_version"),
        "runner_files": dict(sorted(dict(value.get("runner_files", {})).items())),
        "registration_protocol": value.get("registration_protocol"),
    }


def compute_runner_closure_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        _canonical_closure_fields(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_runner_manifest(data: bytes) -> RunnerImageManifest:
    if not isinstance(data, bytes) or not 1 <= len(data) <= _MAX_MANIFEST_BYTES:
        raise BootstrapError("runner image manifest is invalid")
    try:
        value = json.loads(data, object_pairs_hook=_unique_keys)
    except Exception:
        raise BootstrapError("runner image manifest is invalid") from None
    return RunnerImageManifest.from_mapping(value)


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _read_root_file(path: str | Path, *, expected_uid: int = 0, limit: int) -> bytes:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != expected_uid
                or info.st_mode & 0o022
                or info.st_size > limit
            ):
                raise BootstrapError("installed runner file is unsafe")
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(descriptor, min(8192, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if not data or len(data) > limit:
                raise BootstrapError("installed runner file is unsafe")
            return bytes(data)
        finally:
            os.close(descriptor)
    except BootstrapError:
        raise
    except OSError:
        raise BootstrapError("installed runner file is unavailable") from None


def load_runner_manifest(path: str | Path, *, expected_uid: int = 0) -> RunnerImageManifest:
    return parse_runner_manifest(
        _read_root_file(path, expected_uid=expected_uid, limit=_MAX_MANIFEST_BYTES)
    )


def verify_runner_files(
    root: str | Path, manifest: RunnerImageManifest, *, expected_uid: int = 0
) -> bool:
    root_path = Path(root)
    try:
        root_info = root_path.lstat()
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or root_info.st_uid != expected_uid
            or root_info.st_mode & 0o022
        ):
            raise BootstrapError("runner file tree is unsafe")
        actual_paths: set[str] = set()
        total_bytes = 0
        directory_count = 0
        for directory, dirnames, filenames in os.walk(root_path, followlinks=False):
            current = Path(directory)
            for dirname in tuple(dirnames):
                child = current / dirname
                child_info = child.lstat()
                directory_count += 1
                if (
                    not stat.S_ISDIR(child_info.st_mode)
                    or child_info.st_uid != expected_uid
                    or child_info.st_mode & 0o022
                    or directory_count > _MAX_RUNNER_FILES
                ):
                    raise BootstrapError("runner file tree is unsafe")
            for filename in filenames:
                path = current / filename
                relative = path.relative_to(root_path).as_posix()
                info = path.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != expected_uid
                    or info.st_mode & 0o022
                ):
                    raise BootstrapError("runner file tree is unsafe")
                expected_digest = manifest.runner_files.get(relative)
                if expected_digest is None or info.st_size > _MAX_RUNNER_BYTES - total_bytes:
                    raise BootstrapError("runner file tree does not match its manifest")
                total_bytes += info.st_size
                actual_paths.add(relative)
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != expected_digest:
                    raise BootstrapError("runner file digest does not match")
        if actual_paths != set(manifest.runner_files):
            raise BootstrapError("runner file tree does not match its manifest")
        binary = root_path / manifest.runner_binary
        run_script = root_path / manifest.runner_run_script
        if not binary.stat().st_mode & stat.S_IXUSR or not run_script.stat().st_mode & stat.S_IXUSR:
            raise BootstrapError("runner scripts are not executable")
        return True
    except BootstrapError:
        raise
    except OSError:
        raise BootstrapError("runner file tree is unavailable") from None


def read_registration_token(stream, *, limit: int = 4096) -> str:
    if type(limit) is not int or not 1 <= limit <= 4096:
        raise BootstrapError("registration token input is invalid")
    try:
        data = stream.read(limit + 1)
    except Exception:
        raise BootstrapError("registration token input is invalid") from None
    if not isinstance(data, bytes) or len(data) > limit:
        raise BootstrapError("registration token input is invalid")
    try:
        token = data.decode("ascii")
    except UnicodeDecodeError:
        raise BootstrapError("registration token input is invalid") from None
    if not _TOKEN.fullmatch(token):
        raise BootstrapError("registration token input is invalid")
    return token


def send_registration_token(
    token: str,
    *,
    expected_closure: str,
    manifest: RunnerImageManifest,
    socket_path: str = _SOCKET_PATH,
    socket_factory: Callable[..., object] = socket.socket,
) -> None:
    if (
        not isinstance(manifest, RunnerImageManifest)
        or expected_closure != manifest.runner_closure_sha256
        or socket_path != _SOCKET_PATH
        or not isinstance(token, str)
        or not _TOKEN.fullmatch(token)
    ):
        raise BootstrapError("runner registration handoff was rejected")
    connection = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + 10
    while True:
        try:
            connection.settimeout(min(1, max(0.1, deadline - time.monotonic())))
            connection.connect(socket_path)
            break
        except OSError:
            connection.close()
            if time.monotonic() >= deadline:
                raise BootstrapError("runner registration channel is not ready") from None
            time.sleep(0.05)
            connection = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.sendall(token.encode("ascii"))
        connection.shutdown(socket.SHUT_WR)
    except Exception:
        raise BootstrapError("runner registration handoff failed") from None
    finally:
        connection.close()


def receive_registration_token(*, timeout_seconds: int = 60) -> str:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 120:
        raise BootstrapError("runner registration deadline is invalid")
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if os.path.lexists(_SOCKET_PATH):
                info = os.lstat(_SOCKET_PATH)
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                    raise BootstrapError("runner registration channel is unsafe")
                os.unlink(_SOCKET_PATH)
            listener.bind(_SOCKET_PATH)
            os.chmod(_SOCKET_PATH, 0o600)
            listener.listen(1)
            listener.settimeout(timeout_seconds)
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(10)
                data = bytearray()
                while len(data) <= 4096:
                    chunk = connection.recv(min(1024, 4097 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                if len(data) > 4096:
                    raise BootstrapError("runner registration token is invalid")
            return read_registration_token(_BytesReader(bytes(data)))
        finally:
            listener.close()
            try:
                os.unlink(_SOCKET_PATH)
            except FileNotFoundError:
                pass
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError("runner registration channel failed") from None


class _BytesReader:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, size: int) -> bytes:
        value, self._data = self._data[:size], self._data[size:]
        return value


def _copy_verified_runner(root: Path, manifest: RunnerImageManifest, destination: Path) -> Path:
    destination.mkdir(mode=0o700)
    for relative in sorted(manifest.runner_files):
        source = root / relative
        target = destination / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, target, follow_symlinks=False)
        source_mode = source.stat().st_mode
        mode = source_mode & 0o555
        if (
            relative in {manifest.runner_binary, manifest.runner_run_script}
            and not mode & stat.S_IXUSR
        ):
            raise BootstrapError("runner binary is not executable")
        os.chmod(
            target,
            mode
            | (
                0o500 if relative in {manifest.runner_binary, manifest.runner_run_script} else 0o400
            ),
        )
    return destination / manifest.runner_binary


class _SubprocessPTYSession:
    """Run only the pinned config script on a no-echo PTY."""

    def __init__(self, argv: list[str], *, cwd: str, env: Mapping[str, str]):
        master, slave = pty.openpty()
        try:
            os.set_blocking(master, False)
            attributes = termios.tcgetattr(slave)
            attributes[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(slave, termios.TCSANOW, attributes)
            self._process = subprocess.Popen(  # noqa: S603 - pinned manifest script and fixed flags.
                argv,
                cwd=cwd,
                env=dict(env),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        self._master = master

    def read(self, timeout: float) -> bytes:
        readable, _, _ = select.select([self._master], [], [], timeout)
        if not readable:
            return b""
        try:
            return os.read(self._master, 8192)
        except OSError:
            return b""

    def write(self, data: bytes | bytearray) -> None:
        remaining = memoryview(data)
        while remaining:
            written = os.write(self._master, remaining)
            if written <= 0:
                raise OSError("PTY registration write was incomplete")
            remaining = remaining[written:]

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self, timeout: float) -> int:
        return self._process.wait(timeout=timeout)

    def kill(self) -> None:
        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._process.wait()

    def close(self) -> None:
        os.close(self._master)


def _has_unexpected_prompt(transcript: bytes) -> bool:
    for raw_line in transcript.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _REGISTRATION_PROMPT.startswith(line):
            continue
        if (
            b"Enter " in line
            or b"Please enter" in line
            or b"Do you want" in line
            or line.endswith(b"?")
        ):
            return True
    return False


def _runner_version_is_pinned(transcript: bytes, expected_version: str) -> bool:
    version_lines = [
        line for line in transcript.splitlines(keepends=True) if b"Runner version" in line
    ]
    if not version_lines:
        return False
    if len(version_lines) != 1:
        raise BootstrapError("runner version transcript is ambiguous")
    line = version_lines[0]
    match = _RUNNER_VERSION_BANNER.search(line)
    expected_prefix = f"Runner version: '{expected_version}'".encode("ascii")
    if (
        match is None
        and not line.endswith((b"\r", b"\n"))
        and expected_prefix.startswith(line.strip())
    ):
        return False
    if match is None or match.group(1).decode("ascii") != expected_version:
        raise BootstrapError("runner version does not match the pinned manifest")
    return True


def _valid_fixed_runner_config_command(argv: list[str], cwd: str, env: Mapping[str, str]) -> bool:
    return bool(
        len(argv) == 12
        and all(isinstance(argument, str) and "\x00" not in argument for argument in argv)
        and Path(cwd).is_absolute()
        and argv[0] == str(Path(cwd) / "config.sh")
        and argv[1] == "--url"
        and _valid_repository_url(argv[2])
        and argv[3] == "--name"
        and _RUNNER_NAME.fullmatch(argv[4])
        and argv[5:]
        == [
            "--no-default-labels",
            "--labels",
            "chatcore-p09-ephemeral",
            "--work",
            str(Path(cwd) / "_work"),
            "--ephemeral",
            "--disableupdate",
        ]
        and set(env) == {"PATH", "LANG", "HOME", "RUNNER_TEMP"}
        and env.get("PATH") == "/usr/bin:/bin"
        and env.get("LANG") == "C.UTF-8"
        and isinstance(env.get("HOME"), str)
        and Path(env["HOME"]).is_absolute()
        and isinstance(env.get("RUNNER_TEMP"), str)
        and Path(env["RUNNER_TEMP"]).is_absolute()
    )


def configure_runner_with_prompt(
    argv: list[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_seconds: int,
    registration_token: str,
    expected_runner_version: str,
    session_factory: Callable[..., object] = _SubprocessPTYSession,
) -> RunnerProcessResult:
    """Answer only the standard runner registration prompt using a no-echo PTY.

    The caller must supply the standard pinned ``config.sh`` and fixed option
    argv. This wrapper withholds the secret until the exact known prompt arrives;
    no transcript or process diagnostic is returned to its caller.
    """
    if (
        not isinstance(argv, list)
        or not argv
        or not isinstance(cwd, str)
        or not isinstance(env, Mapping)
        or type(timeout_seconds) is not int
        or not 1 <= timeout_seconds <= 120
        or not isinstance(expected_runner_version, str)
        or not _RUNNER_VERSION.fullmatch(expected_runner_version)
        or not isinstance(registration_token, str)
        or not _TOKEN.fullmatch(registration_token)
        or not callable(session_factory)
        or not _valid_fixed_runner_config_command(argv, cwd, env)
        or any(registration_token in str(argument) for argument in argv)
        or any(registration_token in str(value) for value in env.values())
    ):
        raise BootstrapError("runner registration prompt policy is invalid")
    secret = bytearray(registration_token.encode("ascii"))
    echo_check = bytes(secret)
    transcript = bytearray()
    session = None
    delivered = False
    completed = False
    pending_chunk = b""
    start = time.monotonic()
    try:
        session = session_factory(argv, cwd=cwd, env=env)
        while True:
            remaining = start + timeout_seconds - time.monotonic()
            if remaining <= 0:
                raise BootstrapError("runner registration configuration timed out")
            chunk, pending_chunk = pending_chunk or session.read(min(0.25, remaining)), b""
            if not isinstance(chunk, bytes):
                raise BootstrapError("runner registration transcript is invalid")
            if chunk:
                if len(transcript) + len(chunk) > _MAX_PROCESS_OUTPUT:
                    raise BootstrapError("runner registration transcript exceeded policy")
                transcript.extend(chunk)
                prompt_count = transcript.count(_REGISTRATION_PROMPT)
                if prompt_count > 1 or _has_unexpected_prompt(bytes(transcript)):
                    raise BootstrapError("runner registration prompt was not recognized")
                version_pinned = _runner_version_is_pinned(
                    bytes(transcript), expected_runner_version
                )
                prompt_at = transcript.find(_REGISTRATION_PROMPT)
                if prompt_at >= 0:
                    if not version_pinned:
                        raise BootstrapError(
                            "runner version was not identified before registration"
                        )
                    after_prompt = bytes(transcript[prompt_at + len(_REGISTRATION_PROMPT) :])
                    if not delivered and after_prompt not in (b"", b"\n", b"\r", b"\r\n"):
                        raise BootstrapError("runner registration transcript was not recognized")
                    if not delivered:
                        payload = bytearray(secret)
                        payload.extend(b"\n")
                        try:
                            session.write(payload)
                        finally:
                            payload[:] = b"\x00" * len(payload)
                            secret[:] = b"\x00" * len(secret)
                        delivered = True
                if delivered and echo_check in bytes(transcript):
                    raise BootstrapError("runner registration token was echoed")
            returncode = session.poll()
            if returncode is not None:
                trailing = session.read(0)
                if not isinstance(trailing, bytes):
                    raise BootstrapError("runner registration transcript is invalid")
                if trailing:
                    pending_chunk = trailing
                    continue
                if not delivered:
                    raise BootstrapError("runner exited before registration prompt")
                if type(returncode) is not int or returncode != 0:
                    raise BootstrapError("runner configuration failed")
                completed = True
                return RunnerProcessResult(returncode)
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError("runner registration configuration failed") from None
    finally:
        secret[:] = b"\x00" * len(secret)
        transcript[:] = b"\x00" * len(transcript)
        echo_check = b""
        if session is not None:
            if not completed:
                try:
                    session.kill()
                except Exception:  # noqa: S110 - suppress cleanup output and preserve safe failure.
                    pass
            try:
                session.close()
            except Exception:  # noqa: S110 - suppress cleanup output and preserve safe failure.
                pass


def run_runner_job(
    manifest: RunnerImageManifest,
    *,
    expected_closure: str,
    source_root: str | Path,
    bootstrap_path: str | Path,
    scratch_parent: str | Path,
    runner_name: str,
    repository_url: str,
    registration_token: str,
    execute: RunnerExecutor,
    timeout_seconds: int,
    prompt_session_factory: Callable[..., object] = _SubprocessPTYSession,
    expected_owner_uid: int = 0,
) -> int:
    """Verify, stage on disposable tmpfs, configure through stdin, and run once."""
    if (
        not isinstance(manifest, RunnerImageManifest)
        or expected_closure != manifest.runner_closure_sha256
        or not isinstance(runner_name, str)
        or not _RUNNER_NAME.fullmatch(runner_name)
        or not _valid_repository_url(repository_url)
        or not isinstance(registration_token, str)
        or not _TOKEN.fullmatch(registration_token)
        or not callable(execute)
        or type(timeout_seconds) is not int
        or not 1 <= timeout_seconds <= _MAX_PROCESS_SECONDS
    ):
        raise BootstrapError("runner launch policy is invalid")
    verify_runner_files(source_root, manifest, expected_uid=expected_owner_uid)
    bootstrap_info = Path(bootstrap_path).lstat()
    if (
        not stat.S_ISREG(bootstrap_info.st_mode)
        or bootstrap_info.st_uid != expected_owner_uid
        or bootstrap_info.st_mode & 0o022
        or _hash_file(Path(bootstrap_path)) != manifest.bootstrap_sha256
    ):
        raise BootstrapError("runner bootstrap digest does not match")

    start = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="p09-job-", dir=scratch_parent) as work:
            root = Path(work)
            os.chmod(root, 0o700)
            staged_root = root / "runner"
            binary = _copy_verified_runner(Path(source_root), manifest, staged_root)
            runner_work = staged_root / "_work"
            runner_temp = root / "_temp"
            home = root / "home"
            for directory in (runner_work, runner_temp, home):
                directory.mkdir(mode=0o700)
            env = {
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "HOME": str(home),
                "RUNNER_TEMP": str(runner_temp),
            }
            configure_argv = [
                str(binary),
                "--url",
                repository_url,
                "--name",
                runner_name,
                "--no-default-labels",
                "--labels",
                "chatcore-p09-ephemeral",
                "--work",
                str(runner_work),
                "--ephemeral",
                "--disableupdate",
            ]
            configured = configure_runner_with_prompt(
                configure_argv,
                cwd=str(staged_root),
                env=env,
                timeout_seconds=min(60, timeout_seconds),
                registration_token=registration_token,
                expected_runner_version=manifest.runner_version,
                session_factory=prompt_session_factory,
            )
            if not _valid_process_result(configured) or configured.returncode != 0:
                raise BootstrapError("runner configuration failed")
            registration_token = ""
            remaining = int(start + timeout_seconds - time.monotonic())
            if remaining < 1:
                raise BootstrapError("runner exceeded its deadline")
            result = execute(
                [str(staged_root / manifest.runner_run_script), "--once"],
                cwd=str(staged_root),
                env=env,
                timeout=remaining,
                input_data=None,
            )
            if not _valid_process_result(result):
                raise BootstrapError("runner output exceeded policy")
            return result.returncode
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError("runner job failed") from None


def _valid_process_result(value: object) -> bool:
    return bool(
        isinstance(value, RunnerProcessResult)
        and type(value.returncode) is int
        and isinstance(value.stdout, bytes)
        and isinstance(value.stderr, bytes)
        and len(value.stdout) + len(value.stderr) <= _MAX_PROCESS_OUTPUT
    )


def _valid_repository_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.path.count("/") == 2
        and re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", parsed.path)
        and not parsed.query
        and not parsed.fragment
        and not parsed.username
        and parsed.port in (None, 443)
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bounded_process(
    argv: list[str], *, cwd: str, env: Mapping[str, str], timeout: int, input_data: bytes | None
) -> RunnerProcessResult:
    try:
        process = subprocess.Popen(  # noqa: S603 - argv is fixed by the verified manifest.
            argv,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
        )
    except OSError:
        raise BootstrapError("runner process could not start") from None
    outputs = {"out": bytearray(), "err": bytearray()}

    def drain(which: str, stream) -> None:
        while True:
            block = stream.read(4096)
            if not block:
                return
            available = _MAX_PROCESS_OUTPUT - len(outputs[which])
            if available > 0:
                outputs[which].extend(block[:available])

    threads = [
        threading.Thread(target=drain, args=("out", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("err", process.stderr), daemon=True),
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
        raise BootstrapError("runner process exceeded its deadline") from None
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise BootstrapError("runner process failed") from None
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        for thread in threads:
            thread.join(timeout=2)
        process.stdout.close()
        process.stderr.close()
    return RunnerProcessResult(process.returncode, bytes(outputs["out"]), bytes(outputs["err"]))


def _safe_cli(
    *,
    manifest_path: str,
    expected_closure: str,
    runner_name: str,
    repository_url: str,
    timeout_seconds: int,
) -> int:
    manifest = load_runner_manifest(manifest_path)
    if expected_closure != manifest.runner_closure_sha256:
        raise BootstrapError("runner image closure does not match supervisor policy")
    bootstrap_path = _verify_installed_image(manifest)
    token = receive_registration_token(timeout_seconds=60)
    return run_runner_job(
        manifest,
        expected_closure=expected_closure,
        source_root=manifest.runner_root,
        bootstrap_path=bootstrap_path,
        scratch_parent="/tmp",  # noqa: S108 - container policy mounts bounded tmpfs here.
        runner_name=runner_name,
        repository_url=repository_url,
        registration_token=token,
        execute=_bounded_process,
        timeout_seconds=timeout_seconds,
        expected_owner_uid=0,
    )


def _verify_installed_image(manifest: RunnerImageManifest) -> Path:
    if manifest.runner_root != _RUNNER_ROOT:
        raise BootstrapError("installed runner image root is invalid")
    bootstrap_path = Path(__file__)
    bootstrap_info = bootstrap_path.lstat()
    if (
        not stat.S_ISREG(bootstrap_info.st_mode)
        or bootstrap_info.st_uid != 0
        or bootstrap_info.st_mode & 0o022
        or _hash_file(bootstrap_path) != manifest.bootstrap_sha256
    ):
        raise BootstrapError("installed runner bootstrap is unsafe")
    relay_helper = Path(_RELAY_HELPER_PATH)
    try:
        helper_info = relay_helper.lstat()
    except OSError:
        raise BootstrapError("installed runner relay helper is unavailable") from None
    if (
        not stat.S_ISREG(helper_info.st_mode)
        or helper_info.st_uid != 0
        or helper_info.st_mode & 0o022
        or _hash_file(relay_helper) != manifest.relay_helper_sha256
    ):
        raise BootstrapError("installed runner relay helper is unsafe")
    verify_runner_files(manifest.runner_root, manifest, expected_uid=0)
    return bootstrap_path


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="p09-runner-bootstrap")
    commands = parser.add_subparsers(dest="command", required=True)
    run_once = commands.add_parser("run-once")
    run_once.add_argument("--manifest", required=True)
    run_once.add_argument("--expected-closure", required=True)
    run_once.add_argument("--runner-name", required=True)
    run_once.add_argument("--repository-url", required=True)
    run_once.add_argument("--timeout-seconds", required=True, type=int)
    receive = commands.add_parser("receive-registration")
    receive.add_argument("--manifest", required=True)
    receive.add_argument("--expected-closure", required=True)
    receive.add_argument("--socket", required=True)
    verify = commands.add_parser("verify-image")
    verify.add_argument("--manifest", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        manifest = load_runner_manifest(args.manifest)
        if args.command == "verify-image":
            _verify_installed_image(manifest)
            return 0
        if args.expected_closure != manifest.runner_closure_sha256:
            raise BootstrapError("runner image closure does not match supervisor policy")
        if args.command == "receive-registration":
            if args.socket != _SOCKET_PATH:
                raise BootstrapError("runner registration channel was rejected")
            _verify_installed_image(manifest)
            token = read_registration_token(sys.stdin.buffer)
            send_registration_token(
                token,
                expected_closure=args.expected_closure,
                manifest=manifest,
                socket_path=args.socket,
            )
            return 0
        return _safe_cli(
            manifest_path=args.manifest,
            expected_closure=args.expected_closure,
            runner_name=args.runner_name,
            repository_url=args.repository_url,
            timeout_seconds=args.timeout_seconds,
        )
    except BootstrapError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
