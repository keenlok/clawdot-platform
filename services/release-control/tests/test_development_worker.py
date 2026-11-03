from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from chatcore_release_control.development_worker import (
    WorkerError,
    WorkerManifest,
    build_test_argv,
    parse_worker_request,
    require_test_discovery,
    write_candidate_files,
)

POLICY_DIGEST = "a" * 64
BASE_COMMIT = "b" * 40
WORKER_SOURCE = b"trusted fixed worker source"
ALLOWED_PATHS = ("src/module.py", "tests/test_module.py")


def manifest_bytes(
    *,
    policy_digest: str = POLICY_DIGEST,
    allowed_paths: tuple[str, ...] = ALLOWED_PATHS,
) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "policy_digest": policy_digest,
            "baseline_commit": BASE_COMMIT,
            "worker_source_digest": hashlib.sha256(WORKER_SOURCE).hexdigest(),
            "allowed_paths": list(allowed_paths),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def worker_manifest(raw: bytes | None = None) -> WorkerManifest:
    data = manifest_bytes() if raw is None else raw
    return WorkerManifest.from_bytes(
        data,
        expected_manifest_digest=hashlib.sha256(data).hexdigest(),
        worker_source=WORKER_SOURCE,
    )


def request_bytes(**changes) -> bytes:
    value = {
        "version": 1,
        "base_commit": BASE_COMMIT,
        "policy_digest": POLICY_DIGEST,
        "worker_manifest_digest": hashlib.sha256(manifest_bytes()).hexdigest(),
        "specification": "synthetic test request",
        "synthetic_examples": [{"value": 1, "expected": 2}],
        "files": {
            "src/module.py": "def run(value):\n    return value + 1\n",
            "tests/test_module.py": "def test_run():\n    assert True\n",
        },
    }
    value.update(changes)
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_worker_manifest_binds_its_source_policy_and_exact_paths() -> None:
    raw = manifest_bytes()
    manifest = worker_manifest(raw)

    assert manifest.digest == hashlib.sha256(raw).hexdigest()
    assert manifest.policy_digest == POLICY_DIGEST
    assert manifest.allowed_paths == ALLOWED_PATHS

    with pytest.raises(WorkerError):
        WorkerManifest.from_bytes(
            raw,
            expected_manifest_digest="0" * 64,
            worker_source=WORKER_SOURCE,
        )
    with pytest.raises(WorkerError):
        WorkerManifest.from_bytes(
            raw,
            expected_manifest_digest=hashlib.sha256(raw).hexdigest(),
            worker_source=b"different worker source",
        )


def test_worker_request_must_match_installed_manifest_and_base() -> None:
    manifest = worker_manifest()

    request = parse_worker_request(request_bytes(), manifest)

    assert request.base_commit == BASE_COMMIT
    assert request.policy_digest == POLICY_DIGEST
    assert {path for path, _content in request.files} == set(ALLOWED_PATHS)


@pytest.mark.parametrize(
    "changes",
    [
        {"base_commit": "not-a-commit"},
        {"base_commit": "f" * 40},
        {"policy_digest": "0" * 64},
        {"worker_manifest_digest": "0" * 64},
        {"files": {"../outside.py": "bad"}},
        {"files": {"src/module.py": "\x00"}},
        {"files": {"src/module.py": "x" * 40_000}},
        {"extra": "unknown field"},
    ],
)
def test_worker_rejects_unbound_or_unsafe_request(changes) -> None:
    with pytest.raises(WorkerError):
        parse_worker_request(request_bytes(**changes), worker_manifest())


def test_worker_installs_text_files_exclusively_under_candidate_root(tmp_path: Path) -> None:
    request = parse_worker_request(request_bytes(), worker_manifest())
    root = tmp_path / "candidate"

    write_candidate_files(request, root)

    assert (root / "src/module.py").read_text(encoding="utf-8").startswith("def run")
    assert (root / "tests/test_module.py").is_file()
    assert not (root / "outside.py").exists()


def test_worker_refuses_a_preexisting_candidate_symlink(tmp_path: Path) -> None:
    request = parse_worker_request(request_bytes(), worker_manifest())
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "candidate"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkerError):
        write_candidate_files(request, root)
    assert list(outside.iterdir()) == []


def test_worker_test_argv_is_fixed_and_contains_no_candidate_command() -> None:
    # Version-1 candidate-only images are never an executable fallback.
    with pytest.raises(WorkerError):
        build_test_argv(Path("/workspace/candidate"), worker_manifest())
    with pytest.raises(WorkerError):
        build_test_argv(Path("/workspace/candidate"))


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"Ran 0 tests in 0.000s\n\nOK\n",
        b"test output without unittest summary\n",
    ],
)
def test_worker_rejects_missing_or_empty_test_discovery(output: bytes) -> None:
    with pytest.raises(WorkerError):
        require_test_discovery(output)


def test_worker_accepts_nonempty_unittest_summary_as_untrusted_output() -> None:
    # The count is only a harness sanity check; the report remains untrusted output.
    require_test_discovery(b"Ran 2 tests in 0.004s\n\nOK\n")
