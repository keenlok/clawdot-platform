from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from chatcore_release_control.policy import PolicyManifest
from chatcore_release_control.publication import (
    FixedBareGitTransport,
    ProvenanceRecord,
    PublicationError,
    PublicationReceipt,
    PublicationRequest,
    PublicationService,
    PublishCommand,
    PublishResult,
    ScanInput,
    ScannerBinaryAdapter,
    ScanSurface,
    ScanVerdict,
    SqliteProvenanceStore,
)

DIGEST = "a" * 64
SENTINEL = "SYNTHETIC_SECRET_SENTINEL"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    git_env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        **(env or {}),
    }
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=git_env,
    )
    return result.stdout.decode("utf-8", errors="strict").strip()


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Synthetic")
    _git(path, "config", "user.email", "synthetic@example.invalid")
    return path


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _bundle(repo: Path, target: Path) -> bytes:
    _git(repo, "bundle", "create", str(target), "HEAD")
    return target.read_bytes()


def _policy() -> PolicyManifest:
    return PolicyManifest.from_json(
        json.dumps(
            {
                "version": 1,
                "repository_id": 101,
                "owner_id": 202,
                "target": "production",
                "workflow_digests": {".github/workflows/release.yml": DIGEST},
                "allowed_paths": ["src/plugin.py"],
                "protected_paths": [".github/workflows/release.yml"],
                "checks": [
                    {
                        "name": "candidate-tests",
                        "creator_app_id": 303,
                        "workflow_path": ".github/workflows/release.yml",
                        "workflow_sha": DIGEST,
                    }
                ],
            }
        )
    )


@dataclass
class Harness:
    request: PublicationRequest
    policy: PolicyManifest
    seen: list[ScanInput]
    records: list[object]
    transports: list[PublishCommand]


def _harness(
    tmp_path: Path,
    *,
    secret_history: bool = False,
    private_history: bool = False,
    copied_private_blob: bool = False,
    historical_private_blob: bool = False,
    unapproved_history: bool = False,
    binary_history: bool = False,
    introduced_root: bool = False,
    scanner_behavior=None,
) -> Harness:
    repo = _init_repo(tmp_path / "source")
    _write(repo, "src/plugin.py", "baseline\n")
    if copied_private_blob or historical_private_blob:
        _write(repo, "settings/private.ini", "synthetic private config bytes\n")
    if historical_private_blob:
        _commit(repo, "historical private path")
        (repo / "settings/private.ini").unlink()
        deployed = _commit(repo, "private path removed before deployed baseline")
    else:
        deployed = _commit(repo, "deployed")
    if introduced_root:
        main_branch = _git(repo, "branch", "--show-current")
        _git(repo, "checkout", "--orphan", "unrelated-root")
        _git(repo, "rm", "-rf", ".")
        _write(repo, "src/plugin.py", "unrelated root branch\n")
        root_commit = _commit(repo, "unrelated root commit")
        _git(repo, "checkout", main_branch)
        _git(
            repo,
            "merge",
            "--allow-unrelated-histories",
            "-s",
            "ours",
            root_commit,
            "-m",
            "merge root",
        )
    if secret_history:
        _write(repo, "src/plugin.py", f"{SENTINEL}\n")
        _commit(repo, "intermediate secret")
        _write(repo, "src/plugin.py", "safe final\n")
    elif private_history:
        _write(repo, "settings/private.ini", "synthetic private path\n")
        _commit(repo, "intermediate private path")
        (repo / "settings/private.ini").unlink()
    elif copied_private_blob:
        _write(repo, "src/plugin.py", "synthetic private config bytes\n")
    elif historical_private_blob:
        _write(repo, "src/plugin.py", "synthetic private config bytes\n")
    elif unapproved_history:
        _write(repo, "src/unapproved.py", "synthetic unapproved source\n")
        _commit(repo, "intermediate unapproved path")
        _write(repo, "src/plugin.py", "safe final\n")
    elif binary_history:
        (repo / "src/plugin.py").write_bytes(b"\x89PNG\r\n\x1a\n")
    else:
        _write(repo, "src/plugin.py", "safe candidate\n")
    head = _commit(repo, "candidate")

    bundle_bytes = _bundle(repo, tmp_path / "candidate.bundle")
    request = PublicationRequest(
        request_id="request-123",
        deployed_commit=deployed,
        base_commit=deployed,
        head_commit=head,
        bundle=bundle_bytes,
        specification=b"Implement a safe feature.",
        pull_request_title=b"Add the safe feature",
        pull_request_body=b"Summary: safe candidate.",
        artifacts=(ScanSurface("test-summary.md", b"All checks passed."),),
    )
    return Harness(request, _policy(), [], [], [])


def _service(
    harness: Harness,
    *,
    scanner_behavior=None,
    transport=None,
    provenance=None,
) -> PublicationService:
    def scan(item: ScanInput) -> ScanVerdict:
        harness.seen.append(item)
        if scanner_behavior is not None:
            return scanner_behavior(item)
        return ScanVerdict.CLEAN

    def store(record) -> None:
        harness.records.append(record)

    def publish(command: PublishCommand) -> PublishResult:
        harness.transports.append(command)
        if transport is not None:
            return transport(command)
        return PublishResult(command.frozen_commit, command.refspec.split(":", 1)[1])

    return PublicationService(
        policy=harness.policy,
        scanner=scan,
        scanner_identity_sha256="b" * 64,
        baseline_history_scan_complete=True,
        record_provenance=provenance or store,
        publish_transport=publish,
    )


def _fixed_bare_transport(remote: Path, order: list[str] | None = None):
    transport = FixedBareGitTransport(remote, repository_id=101)

    def publish(command: PublishCommand) -> PublishResult:
        if order is not None:
            order.append("publish")
        assert command.refspec == (
            f"{command.frozen_commit}:refs/heads/clawdot/{command.request_id}"
        )
        return transport(command)

    return publish


def test_scans_and_publishes_exact_frozen_head_to_local_bare_remote(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    order: list[str] = []

    def record(record) -> None:
        order.append("provenance")
        harness.records.append(record)

    service = _service(
        harness,
        transport=_fixed_bare_transport(remote, order),
        provenance=record,
    )
    receipt = service.publish(harness.request)

    assert isinstance(receipt, PublicationReceipt)
    assert receipt.head_commit == harness.request.head_commit
    assert receipt.published_ref == "refs/heads/clawdot/request-123"
    assert order == ["provenance", "publish"]
    assert harness.records[0].head_commit == receipt.head_commit
    assert harness.records[0].surface_manifest_sha256 == receipt.surface_manifest_sha256
    assert harness.records[0].baseline_history_scan_complete
    assert harness.transports[0].refspec.endswith(":refs/heads/clawdot/request-123")
    actual = _git(remote, "rev-parse", "refs/heads/clawdot/request-123")
    assert actual == harness.request.head_commit
    names = {item.surface_name for item in harness.seen}
    assert "specification" in names
    assert "pull_request_title" in names
    assert "pull_request_body" in names
    assert "artifact:test-summary.md" in names
    assert any(name.startswith("commit:") for name in names)
    assert any(name.startswith("git_blob:") for name in names)


def test_intermediate_secret_blob_fails_closed_before_provenance_or_publish(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, secret_history=True)

    def scanner(item: ScanInput) -> ScanVerdict:
        if SENTINEL.encode() in item.content:
            return ScanVerdict.FINDING
        return ScanVerdict.CLEAN

    service = _service(harness, scanner_behavior=scanner)
    with pytest.raises(PublicationError) as error:
        service.publish(harness.request)
    assert error.value.code == "scanner_finding"
    assert not harness.records
    assert not harness.transports
    assert any(SENTINEL.encode() in item.content for item in harness.seen)


def test_introduced_root_commit_and_blob_are_scanned(tmp_path: Path) -> None:
    harness = _harness(tmp_path, introduced_root=True)
    service = _service(harness)
    receipt = service.publish(harness.request)

    assert receipt.head_commit == harness.request.head_commit
    assert any(
        item.surface_name.startswith("commit:") and b"unrelated root commit" in item.content
        for item in harness.seen
    )
    assert any(
        item.surface_name.startswith("git_blob:") and item.content == b"unrelated root branch\n"
        for item in harness.seen
    )


def test_private_path_add_then_remove_is_rejected_without_scanning_file_bytes(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, private_history=True)
    service = _service(harness)

    with pytest.raises(PublicationError) as error:
        service.publish(harness.request)
    assert error.value.code == "private_path"
    assert not harness.records
    assert not harness.transports
    assert all(b"synthetic private path" not in item.content for item in harness.seen)


def test_copying_a_baseline_private_blob_into_allowed_path_is_rejected_pre_read(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, copied_private_blob=True)
    service = _service(harness)

    with pytest.raises(PublicationError) as error:
        service.publish(harness.request)
    assert error.value.code == "copied_forbidden_blob"
    assert not harness.records
    assert not harness.transports
    assert all(b"synthetic private config bytes" not in item.content for item in harness.seen)


def test_copying_a_pre_deployment_private_blob_is_rejected_pre_read(tmp_path: Path) -> None:
    harness = _harness(tmp_path, historical_private_blob=True)
    service = _service(harness)

    with pytest.raises(PublicationError) as error:
        service.publish(harness.request)
    assert error.value.code == "copied_forbidden_blob"
    assert not harness.records
    assert not harness.transports
    assert all(b"synthetic private config bytes" not in item.content for item in harness.seen)


def test_scanner_failure_is_sanitized_and_never_publishes(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    def broken(_: ScanInput) -> ScanVerdict:
        raise RuntimeError("access_token=must-not-appear")

    service = _service(harness, scanner_behavior=broken)
    with pytest.raises(PublicationError) as error:
        service.publish(harness.request)
    assert error.value.code == "scanner_failed"
    assert "must-not-appear" not in str(error.value)
    assert not harness.records
    assert not harness.transports


def test_unapproved_intermediate_path_is_rejected(tmp_path: Path) -> None:
    harness = _harness(tmp_path, unapproved_history=True)
    service = _service(harness)
    with pytest.raises(PublicationError) as error:
        service.publish(harness.request)
    assert error.value.code == "unapproved_path"
    assert not harness.records
    assert not harness.transports


def test_binary_blob_is_rejected_without_scanner_approval(tmp_path: Path) -> None:
    harness = _harness(tmp_path, binary_history=True)
    service = _service(harness)
    with pytest.raises(PublicationError) as error:
        service.publish(harness.request)
    assert error.value.code == "binary_content"
    assert not harness.records
    assert not harness.transports


def test_transport_receipt_must_match_frozen_commit_and_ref(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    service = _service(
        harness,
        transport=lambda command: PublishResult("f" * 40, "refs/heads/clawdot/other"),
    )
    with pytest.raises(PublicationError) as error:
        service.publish(harness.request)
    assert error.value.code == "publication_mismatch"
    assert len(harness.records) == 1


def test_baseline_scan_identity_and_complete_scanner_are_mandatory(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    with pytest.raises(PublicationError) as error:
        PublicationService(
            policy=harness.policy,
            scanner=None,
            scanner_identity_sha256="b" * 64,
            baseline_history_scan_complete=True,
            record_provenance=lambda _: None,
            publish_transport=lambda _: None,
        )
    assert error.value.code == "scanner_unavailable"

    with pytest.raises(PublicationError):
        PublicationService(
            policy=harness.policy,
            scanner=lambda _: ScanVerdict.CLEAN,
            scanner_identity_sha256="b" * 64,
            baseline_history_scan_complete=False,
            record_provenance=lambda _: None,
            publish_transport=lambda _: None,
        )


def test_sqlite_provenance_is_durable_idempotent_and_immutable(tmp_path: Path) -> None:
    database = tmp_path / "gateway-state" / "publication.sqlite"
    record = ProvenanceRecord(
        request_id="request-123",
        deployed_commit="a" * 40,
        base_commit="a" * 40,
        head_commit="b" * 40,
        policy_digest="c" * 64,
        scanner_identity_sha256="d" * 64,
        baseline_history_scan_complete=True,
        bundle_sha256="e" * 64,
        surface_manifest_sha256="f" * 64,
        scanned_surface_count=5,
        scanned_bytes=1234,
    )
    first = SqliteProvenanceStore(database)
    first(record)
    reopened = SqliteProvenanceStore(database)
    reopened(record)

    with pytest.raises(PublicationError) as error:
        reopened(replace(record, head_commit="f" * 40))
    assert error.value.code == "provenance_failed"
    reopened(record)


def test_sqlite_provenance_serializes_concurrent_conflicting_request_ids(
    tmp_path: Path,
) -> None:
    database = tmp_path / "publication.sqlite"
    first = SqliteProvenanceStore(database)
    second = SqliteProvenanceStore(database)
    record_a = ProvenanceRecord(
        "concurrent-1",
        "a" * 40,
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "d" * 64,
        True,
        "e" * 64,
        "f" * 64,
        1,
        1,
    )
    record_b = replace(record_a, head_commit="f" * 40)

    def attempt(store, record) -> str:
        try:
            store(record)
            return "stored"
        except PublicationError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(attempt, first, record_a)
        future_b = pool.submit(attempt, second, record_b)
        outcomes = {future_a.result(), future_b.result()}
    assert outcomes == {"stored", "rejected"}


def test_mutated_remote_ref_is_not_replaced(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    seed = _init_repo(tmp_path / "seed")
    _write(seed, "seed.py", "different object\\n")
    other_commit = _commit(seed, "other object")
    ref = "refs/heads/clawdot/request-123"
    subprocess.run(
        ["git", "-C", str(seed), "push", str(remote), f"{other_commit}:{ref}"],
        check=True,
        capture_output=True,
        text=True,
    )
    original_remote = _git(remote, "rev-parse", ref)
    service = _service(harness, transport=_fixed_bare_transport(remote))
    with pytest.raises(PublicationError) as error:
        service.publish(harness.request)
    assert error.value.code == "publication_failed"
    assert _git(remote, "rev-parse", ref) == original_remote


def test_repeat_publication_is_idempotent_at_same_frozen_commit(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    order: list[str] = []
    service = _service(harness, transport=_fixed_bare_transport(remote, order))
    first = service.publish(harness.request)
    second = service.publish(harness.request)
    assert first == second
    assert order == ["publish", "publish"]
    assert _git(remote, "rev-parse", first.published_ref) == first.head_commit


def test_same_request_id_cannot_rebind_changed_same_length_scan_surfaces(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    store = SqliteProvenanceStore(tmp_path / "publication.sqlite")
    published: list[PublishCommand] = []

    def transport(command: PublishCommand) -> PublishResult:
        published.append(command)
        return PublishResult(
            command.frozen_commit,
            command.refspec.split(":", 1)[1],
        )

    service = _service(harness, provenance=store, transport=transport)
    original = harness.request
    service.publish(original)
    changed_requests = (
        replace(original, pull_request_title=b"Add the good feature"),
        replace(original, pull_request_body=b"Summary: good candidate."),
        replace(
            original,
            artifacts=(ScanSurface("test-summary.md", b"All checks failed!"),),
        ),
    )
    for changed in changed_requests:
        assert len(changed.pull_request_title) == len(original.pull_request_title)
        assert len(changed.pull_request_body) == len(original.pull_request_body)
        assert len(changed.artifacts[0].content) == len(original.artifacts[0].content)
        with pytest.raises(PublicationError) as error:
            service.publish(changed)
        assert error.value.code == "provenance_failed"
    assert len(published) == 1


def test_ambient_git_environment_cannot_redirect_quarantine(tmp_path: Path, monkeypatch) -> None:
    harness = _harness(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "attacker-controlled.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "attacker-worktree"))
    service = _service(harness)
    receipt = service.publish(harness.request)
    assert receipt.head_commit == harness.request.head_commit


def test_installed_scanner_binary_hashes_config_and_receives_surface_bytes(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "scanner"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "data = sys.stdin.buffer.read()\n"
        "verdict = 'finding' if b'SYNTHETIC_SECRET' in data else 'clean'\n"
        "sys.stdout.write(json.dumps({'verdict': verdict}, separators=(',', ':')))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    configuration = tmp_path / "scanner.conf"
    configuration.write_bytes(b"mode=source-secret-scan\n")
    scanner = ScannerBinaryAdapter(
        executable,
        configuration,
        expected_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        expected_configuration_sha256=hashlib.sha256(configuration.read_bytes()).hexdigest(),
    )
    assert scanner(ScanInput("test-surface", b"safe bytes")) is ScanVerdict.CLEAN
    assert scanner(ScanInput("test-surface", b"SYNTHETIC_SECRET")) is ScanVerdict.FINDING


def test_scanner_binary_identity_mismatch_and_timeout_fail_closed(tmp_path: Path) -> None:
    executable = tmp_path / "scanner"
    executable.write_text(
        f'#!{sys.executable}\nimport time\ntime.sleep(2)\nprint(\'{{"verdict":"clean"}}\')\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    configuration = tmp_path / "scanner.conf"
    configuration.write_bytes(b"fixed-config")
    executable_digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    config_digest = hashlib.sha256(configuration.read_bytes()).hexdigest()
    with pytest.raises(PublicationError) as error:
        ScannerBinaryAdapter(
            executable,
            configuration,
            expected_executable_sha256="0" * 64,
            expected_configuration_sha256=config_digest,
        )
    assert error.value.code == "scanner_unavailable"

    scanner = ScannerBinaryAdapter(
        executable,
        configuration,
        expected_executable_sha256=executable_digest,
        expected_configuration_sha256=config_digest,
        timeout_seconds=0.1,
    )
    with pytest.raises(PublicationError) as error:
        scanner(ScanInput("slow", b"bytes"))
    assert error.value.code == "scanner_failed"
