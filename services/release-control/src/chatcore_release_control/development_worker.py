"""Fixed stdlib-only worker installed in the isolated development image.

This module accepts only a controller-created request file and an installed,
image-pinned manifest. Candidate source is written under the container's
bounded workspace and is executed only by the child test process here. The
version-2 harness stages the verified baseline before applying the generated
overlay. Dependencies are installed offline in the pinned image. Reports remain
untrusted candidate observations, never independent pass/count proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_SPECIFICATION_BYTES = 8192
MAX_EXAMPLE_BYTES = 16 * 1024
MAX_EXAMPLES = 8
MAX_FILES = 64
MAX_FILE_BYTES = 32 * 1024
MAX_PATCH_BYTES = 128 * 1024
MAX_TEST_TIMEOUT = 3600
MAX_OUTPUT_BYTES = 1024 * 1024
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_PATH = Path("/opt/chatcore-release/manifest.json")
_WORKER_PATH = Path(__file__).resolve()
_REQUEST_PATH = Path("/workspace/request.json")
_CANDIDATE_PATH = Path("/workspace/candidate")


class WorkerError(ValueError):
    """Fixed worker validation failure with no untrusted details."""

    def __init__(self) -> None:
        super().__init__("worker_request_invalid")


@dataclass(frozen=True)
class WorkerManifest:
    digest: str
    policy_digest: str
    baseline_commit: str
    worker_source_digest: str
    allowed_paths: tuple[str, ...]
    version: int = 1
    test_harness_digest: str = ""

    @classmethod
    def from_bytes(
        cls,
        raw: bytes,
        *,
        expected_manifest_digest: str,
        worker_source: bytes,
    ) -> WorkerManifest:
        if (
            not isinstance(raw, bytes)
            or len(raw) > MAX_MANIFEST_BYTES
            or not isinstance(expected_manifest_digest, str)
            or not _DIGEST.fullmatch(expected_manifest_digest)
            or hashlib.sha256(raw).hexdigest() != expected_manifest_digest
            or not isinstance(worker_source, bytes)
        ):
            raise WorkerError()
        value = _load_json(raw, max_bytes=MAX_MANIFEST_BYTES)
        required = {
            "version",
            "policy_digest",
            "baseline_commit",
            "worker_source_digest",
            "allowed_paths",
        }
        if not isinstance(value, dict) or type(value.get("version")) is not int:
            raise WorkerError()
        if value["version"] == 2:
            required |= {"baseline_snapshot", "test_harness_digest"}
            if (
                not isinstance(value.get("baseline_snapshot"), dict)
                or not isinstance(value.get("test_harness_digest"), str)
                or not _DIGEST.fullmatch(value["test_harness_digest"])
            ):
                raise WorkerError()
        if value["version"] not in {1, 2} or set(value) != required:
            raise WorkerError()
        policy_digest = value["policy_digest"]
        baseline_commit = value["baseline_commit"]
        source_digest = value["worker_source_digest"]
        paths = value["allowed_paths"]
        if (
            type(value["version"]) is not int
            or value["version"] not in {1, 2}
            or not isinstance(policy_digest, str)
            or not _DIGEST.fullmatch(policy_digest)
            or not isinstance(baseline_commit, str)
            or not _SHA.fullmatch(baseline_commit)
            or not isinstance(source_digest, str)
            or not _DIGEST.fullmatch(source_digest)
            or hashlib.sha256(worker_source).hexdigest() != source_digest
            or not isinstance(paths, list)
            or not 1 <= len(paths) <= MAX_FILES
            or any(not _safe_relative_path(item) for item in paths)
            or paths != sorted(set(paths))
        ):
            raise WorkerError()
        canonical = _canonical_json(value)
        if canonical != raw:
            raise WorkerError()
        return cls(
            expected_manifest_digest,
            policy_digest,
            baseline_commit,
            source_digest,
            tuple(paths),
            value["version"],
            value.get("test_harness_digest", ""),
        )


@dataclass(frozen=True)
class WorkerRequest:
    base_commit: str
    policy_digest: str
    worker_manifest_digest: str
    specification: str
    synthetic_examples: tuple[Any, ...]
    files: tuple[tuple[str, str], ...]


def parse_worker_request(raw: bytes, manifest: WorkerManifest) -> WorkerRequest:
    if not isinstance(manifest, WorkerManifest):
        raise WorkerError()
    value = _load_json(raw, max_bytes=MAX_REQUEST_BYTES)
    required = {
        "version",
        "base_commit",
        "policy_digest",
        "worker_manifest_digest",
        "specification",
        "synthetic_examples",
        "files",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise WorkerError()
    base = value["base_commit"]
    policy_digest = value["policy_digest"]
    manifest_digest = value["worker_manifest_digest"]
    specification = value["specification"]
    examples = value["synthetic_examples"]
    files = value["files"]
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(base, str)
        or base != manifest.baseline_commit
        or policy_digest != manifest.policy_digest
        or manifest_digest != manifest.digest
        or not isinstance(specification, str)
        or not specification.strip()
        or not _valid_text(specification)
        or len(specification.encode("utf-8")) > MAX_SPECIFICATION_BYTES
        or not isinstance(examples, list)
        or not 1 <= len(examples) <= MAX_EXAMPLES
        or not isinstance(files, dict)
        or not 1 <= len(files) <= MAX_FILES
        or set(files) - set(manifest.allowed_paths)
    ):
        raise WorkerError()
    try:
        examples_bytes = _canonical_json(examples)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise WorkerError() from None
    if len(examples_bytes) > MAX_EXAMPLE_BYTES:
        raise WorkerError()
    normalized: list[tuple[str, str]] = []
    total = 0
    for path, content in sorted(files.items()):
        if (
            not _safe_relative_path(path)
            or not isinstance(content, str)
            or not _valid_text(content)
        ):
            raise WorkerError()
        data = content.encode("utf-8", errors="strict")
        if not data or len(data) > MAX_FILE_BYTES:
            raise WorkerError()
        total += len(data)
        if total > MAX_PATCH_BYTES:
            raise WorkerError()
        normalized.append((path, content))
    return WorkerRequest(
        base,
        policy_digest,
        manifest_digest,
        specification,
        tuple(examples),
        tuple(normalized),
    )


def write_candidate_files(request: WorkerRequest, root: Path) -> None:
    if not isinstance(request, WorkerRequest) or not isinstance(root, Path):
        raise WorkerError()
    try:
        root.mkdir(mode=0o700, parents=False, exist_ok=False)
        for relative, content in request.files:
            parts = PurePosixPath(relative).parts
            parent = root
            for part in parts[:-1]:
                parent = parent / part
                parent.mkdir(mode=0o700, exist_ok=True)
                info = parent.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise WorkerError()
            path = parent / parts[-1]
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            try:
                data = content.encode("utf-8", errors="strict")
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
            finally:
                os.close(descriptor)
    except (OSError, UnicodeError, ValueError):
        raise WorkerError() from None


def build_test_argv(
    candidate_root: Path, manifest: WorkerManifest | None = None
) -> tuple[str, ...]:
    if (
        candidate_root != Path("/workspace/candidate")
        or not isinstance(manifest, WorkerManifest)
        or manifest.version != 2
    ):
        raise WorkerError()
    return (
        "/opt/chatcore-release/venv/bin/python",
        "-I",
        "/opt/chatcore-release/monorepo_harness.py",
        "--manifest-digest",
        manifest.digest,
    )


def require_test_discovery(output: bytes) -> None:
    """Reject the common empty-discovery result; this does not authenticate counts."""
    if not isinstance(output, bytes):
        raise WorkerError()
    summaries = re.findall(rb"(?m)^Ran ([0-9]+) tests? in [0-9.]+s$", output)
    if not summaries or not summaries[-1].strip(b"0"):
        raise WorkerError()


def _load_json(raw: bytes, *, max_bytes: int) -> Any:
    if not isinstance(raw, bytes) or not raw or len(raw) > max_bytes:
        raise WorkerError()
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError, TypeError):
        raise WorkerError() from None


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and str(path) == value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and all(ord(char) >= 32 for char in value)
    )


def _valid_text(value: str) -> bool:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return all(char in {"\n", "\r", "\t"} or unicodedata.category(char) != "Cc" for char in value)


def _run_tests(
    timeout_seconds: int, max_output_bytes: int, manifest: WorkerManifest
) -> tuple[int, bytes]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TEST_TIMEOUT:
        raise WorkerError()
    if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= MAX_OUTPUT_BYTES:
        raise WorkerError()
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv; candidate source is test data only.
            build_test_argv(_CANDIDATE_PATH, manifest),
            cwd=Path("/workspace"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            close_fds=True,
            start_new_session=True,
            shell=False,
            bufsize=0,
        )
    except OSError:
        raise WorkerError() from None
    output = bytearray()
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    os.set_blocking(process.stdout.fileno(), False)
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            events = selector.select(min(remaining, 0.1))
            if not events and process.poll() is not None:
                continue
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), min(8192, max_output_bytes - len(output) + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                output.extend(chunk)
                if len(output) > max_output_bytes:
                    raise OverflowError
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        result = bytes(output)
        return process.wait(timeout=remaining), result
    except (TimeoutError, OverflowError, subprocess.TimeoutExpired, OSError):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        raise WorkerError() from None
    finally:
        selector.close()
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()


def _emit_report(request: WorkerRequest, status: int, output: bytes) -> None:
    report = {
        "version": 2,
        "base_commit": request.base_commit,
        "policy_digest": request.policy_digest,
        "worker_manifest_digest": request.worker_manifest_digest,
        "execution_kind": "monorepo-pytest-v1",
        "exit_status": status,
        "output": output.decode("utf-8", errors="replace"),
    }
    raw = _canonical_json(report)
    if len(raw) > MAX_OUTPUT_BYTES:
        raise WorkerError()
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request", required=True)
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--timeout-seconds", type=int, required=True)
    parser.add_argument("--max-output-bytes", type=int, required=True)
    args = parser.parse_args(argv)
    if args.request != str(_REQUEST_PATH):
        raise WorkerError()
    try:
        manifest_raw = _MANIFEST_PATH.read_bytes()
        worker_source = _WORKER_PATH.read_bytes()
        manifest = WorkerManifest.from_bytes(
            manifest_raw,
            expected_manifest_digest=args.manifest_digest,
            worker_source=worker_source,
        )
        request = parse_worker_request(_REQUEST_PATH.read_bytes(), manifest)
        harness = Path("/opt/chatcore-release/monorepo_harness.py").read_bytes()
        if (
            manifest.version != 2
            or hashlib.sha256(harness).hexdigest() != manifest.test_harness_digest
        ):
            raise WorkerError()
        status, output = _run_tests(args.timeout_seconds, args.max_output_bytes, manifest)
        _emit_report(request, status, output)
        return 0
    except Exception:
        return 2


if __name__ == "__main__":  # pragma: no cover - entrypoint is exercised in image proof
    raise SystemExit(_main())
