from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from chatcore_release_control.development import DevelopmentError, ResourcePolicy, SandboxConfig
from chatcore_release_control.development_worker import WorkerManifest
from chatcore_release_control.monorepo_harness import SnapshotFile
from chatcore_release_control.policy import PolicyManifest, TrustedCheck
from chatcore_release_control.publication import ScanVerdict
from chatcore_release_control.release_coordinator import (
    CiGatewayClient,
    CiPhaseCoordinator,
    CoordinatorError,
    ProtectedCiRuntimeConfig,
    build_ci_coordinator,
    ci_context_digest,
    main,
)


def _git(repo: Path, *args: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo), "LC_ALL": "C"},
    )
    return result.stdout.decode().strip()


def _repo(tmp_path: Path, *, delete: bool = False) -> tuple[Path, str, str, PolicyManifest]:
    repo = tmp_path / "objects.git"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "CI fixture")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n")
    (repo / "uv.lock").write_text("version = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    policy = PolicyManifest(
        repository_id=77,
        owner_id=88,
        target="production",
        workflow_digests=((".github/workflows/ci.yml", "1" * 64),),
        allowed_paths=("src/app.py",),
        protected_paths=(".github/workflows/ci.yml",),
        checks=(TrustedCheck("trusted", 99, ".github/workflows/ci.yml", "1" * 64),),
    )
    if delete:
        (repo / "src" / "app.py").unlink()
    else:
        (repo / "src" / "app.py").write_text("VALUE = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "candidate")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, base, head, policy


class _Gateway:
    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context
        self.uploads: list[dict[str, Any]] = []

    def get_ci_context(self, pull_request_number: int) -> dict[str, Any]:
        assert pull_request_number == self.context["pull_request_number"]
        return dict(self.context)

    def upload_ci_report(self, pull_request_number: int, body: dict[str, Any]) -> dict[str, Any]:
        self.uploads.append(body)
        return {
            "request_id": body["request_id"],
            "pull_request_number": pull_request_number,
            "head_sha": body["head_sha"],
            "report_sha256": body["report_sha256"],
            "artifact_id": "artifact-123",
            "stored": True,
        }


class _GitHub:
    owner = "acme"
    repository = "plugin"
    repository_id = 77

    def __init__(self, pull: dict[str, Any]) -> None:
        self.pull = pull
        self.calls: list[str] = []

    def get_json(self, path: str) -> dict[str, Any]:
        assert path == "pulls/6"
        self.calls.append("pull")
        return self.pull

    def close(self) -> None:
        pass


class _Runner:
    max_output_bytes = 512 * 1024
    max_input_bytes = 4 * 1024 * 1024

    def __init__(self, report: bytes | None = None) -> None:
        self.request_bytes: list[bytes] = []
        self.report = report or b"{}"
        self.failure: DevelopmentError | None = None

    def run(self, request_bytes: bytes) -> bytes:
        self.request_bytes.append(request_bytes)
        if self.failure is not None:
            raise self.failure
        return self.report


class _RecordingGitFetcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def ensure_history(self, **values: object) -> None:
        self.calls.append(values)


def _context(
    repo: Path, base: str, head: str, policy: PolicyManifest, *, deleted: bool = False
) -> dict[str, Any]:
    from chatcore_release_control.git_evidence import _GitRunner

    with tempfile.TemporaryDirectory() as home:
        runner = _GitRunner(shutil.which("git") or "/usr/bin/git", repo, Path(home), 40)
        tree = runner.tree(head)
    provenance = {
        "request_id": "request-6",
        "deployed_commit": base,
        "base_commit": base,
        "head_commit": head,
        "policy_digest": policy.digest,
        "scanner_identity_sha256": "4" * 64,
        "baseline_history_scan_complete": True,
        "bundle_sha256": hashlib.sha256(b"synthetic bundle").hexdigest(),
        "surface_manifest_sha256": "5" * 64,
        "scanned_surface_count": 2,
        "scanned_bytes": 100,
    }
    value = {
        "version": 1,
        "request_id": "request-6",
        "repository": "acme/plugin",
        "repository_id": 77,
        "owner_id": 88,
        "pull_request_number": 6,
        "base_branch": "main",
        "deployed_commit": base,
        "base_commit": base,
        "head_commit": head,
        "head_tree": tree,
        "policy_digest": policy.digest,
        "policy_manifest": {
            "version": policy.version,
            "repository_id": policy.repository_id,
            "owner_id": policy.owner_id,
            "target": policy.target,
            "workflow_digests": dict(policy.workflow_digests),
            "allowed_paths": list(policy.allowed_paths),
            "protected_paths": list(policy.protected_paths),
            "checks": [
                {
                    "name": check.name,
                    "creator_app_id": check.creator_app_id,
                    "workflow_path": check.workflow_path,
                    "workflow_sha": check.workflow_digest,
                }
                for check in policy.checks
            ],
        },
        "provenance": provenance,
        "published_ref": "refs/heads/clawdot/request-6",
        "specification": "Keep the candidate change.",
        "synthetic_examples": [{"input": "synthetic"}],
        "pull_request_title": "candidate",
        "pull_request_body": "synthetic body",
        "artifacts": [],
    }
    value["context_sha256"] = ci_context_digest(value)
    return value


def _baseline_manifest(
    repo: Path,
    commit: str,
    policy: PolicyManifest,
    worker_source: bytes = b"synthetic worker",
    harness_source: bytes = b"synthetic harness",
) -> bytes:
    from chatcore_release_control.git_evidence import _GitRunner

    with tempfile.TemporaryDirectory() as home:
        reader = _GitRunner(shutil.which("git") or "/usr/bin/git", repo, Path(home), 40)
        tree = reader.tree(commit)
        raw = reader.run("ls-tree", "-r", "-z", "--full-tree", commit)
        entries = []
        content = {}
        for record in raw.split(b"\0")[:-1]:
            header, _, raw_path = record.partition(b"\t")
            mode, kind, oid = header.split(b" ")
            path = raw_path.decode()
            data = reader.run("cat-file", "blob", oid.decode())
            file_mode = 0o755 if mode == b"100755" else 0o644
            entries.append(
                SnapshotFile(path, len(data), hashlib.sha256(data).hexdigest(), file_mode)
            )
            content[path] = data
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w", format=tarfile.PAX_FORMAT) as output:
            for entry in entries:
                info = tarfile.TarInfo(entry.path)
                info.size, info.mode, info.mtime = entry.size, entry.mode, 0
                info.uid = info.gid = 65532
                output.addfile(info, io.BytesIO(content[entry.path]))
        lock = next(entry for entry in entries if entry.path == "uv.lock")
        snapshot = {
            "tree": tree,
            "lock_digest": lock.sha256,
            "files": [entry.__dict__ for entry in sorted(entries, key=lambda item: item.path)],
            "source_roots": ["src"],
            "test_paths": ["tests/test_app.py"],
            "archive_digest": hashlib.sha256(archive.getvalue()).hexdigest(),
            "reviewed_source_exceptions": [],
        }
    return json.dumps(
        {
            "version": 2,
            "policy_digest": policy.digest,
            "baseline_commit": commit,
            "worker_source_digest": hashlib.sha256(worker_source).hexdigest(),
            "test_harness_digest": hashlib.sha256(harness_source).hexdigest(),
            "allowed_paths": sorted(policy.allowed_paths),
            "baseline_snapshot": snapshot,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _pull(base: str, head: str) -> dict[str, Any]:
    return {
        "number": 6,
        "state": "open",
        "merged": False,
        "title": "candidate",
        "body": "synthetic body",
        "base": {"ref": "main", "sha": base, "repo": {"id": 77, "owner": {"id": 88}}},
        "head": {"ref": "clawdot/request-6", "sha": head, "repo": {"id": 77}},
    }


def _coordinator(
    tmp_path: Path,
    *,
    deleted: bool = False,
    report: bytes = b"{}",
    scanner=None,
    git_fetcher=None,
) -> tuple[CiPhaseCoordinator, _Gateway, _Runner, dict[str, Any]]:
    repo, base, head, policy = _repo(tmp_path, delete=deleted)
    context = _context(repo, base, head, policy, deleted=deleted)
    manifest_bytes = _baseline_manifest(repo, base, policy)
    worker_source = b"synthetic worker"
    worker_manifest = WorkerManifest.from_bytes(
        manifest_bytes,
        expected_manifest_digest=hashlib.sha256(manifest_bytes).hexdigest(),
        worker_source=worker_source,
    )
    resources = ResourcePolicy(500, 256 * 1024 * 1024, 64, 60, 512 * 1024, 4 * 1024 * 1024)
    sandbox = SandboxConfig(
        "/usr/bin/docker",
        "runsc",
        "/usr/bin/runsc",
        "6" * 64,
        "registry.example/worker@sha256:" + "7" * 64,
        worker_manifest.digest,
        policy.digest,
        base,
        resources,
    )
    gateway = _Gateway(context)
    runner = _Runner(report)
    github = _GitHub(_pull(base, head))
    runner.worker_manifest_digest = worker_manifest.digest
    runner.policy_digest = policy.digest
    runner.baseline_commit = base
    (tmp_path / "installed-baseline").mkdir()
    coordinator = CiPhaseCoordinator(
        repository=repo,
        policy=policy,
        github=github,
        gateway=gateway,
        sandbox=sandbox,
        baseline_root=tmp_path / "installed-baseline",
        baseline_manifest=worker_manifest,
        baseline_manifest_bytes=manifest_bytes,
        baseline_worker_source=worker_source,
        runner=runner,
        report_scanner=scanner or (lambda _: ScanVerdict.CLEAN),
        state_directory=tmp_path / "ci-state",
        artifact_directory=tmp_path / "artifacts",
        expected_repository="acme/plugin",
        repository_id=77,
        owner_id=88,
        scanner_identity_sha256="4" * 64,
        git_fetcher=git_fetcher,
    )
    return coordinator, gateway, runner, context


def test_ci_prepare_extracts_only_exact_head_blobs_after_context_and_pr_binding(tmp_path):
    coordinator, _gateway, runner, context = _coordinator(tmp_path)

    result = coordinator.prepare(6)

    assert result["phase"] == "prepared"
    assert result["head_sha"] == context["head_commit"]
    assert result["request_id"] == context["request_id"]
    assert runner.request_bytes == []
    assert coordinator.state_path(6).is_file()
    value = json.loads(coordinator.state_path(6).read_bytes())
    assert value["request"]["files"] == {"src/app.py": "VALUE = 2\n"}


def test_ci_materializes_exact_d_b_h_after_authenticated_pr_refetch(tmp_path):
    fetcher = _RecordingGitFetcher()
    coordinator, _gateway, _runner, context = _coordinator(tmp_path, git_fetcher=fetcher)

    coordinator.prepare(6)

    assert len(fetcher.calls) == 1
    assert fetcher.calls[0] == {
        "pull_request_number": 6,
        "deployed_commit": context["deployed_commit"],
        "base_commit": context["base_commit"],
        "head_commit": context["head_commit"],
        "policy": coordinator.policy,
    }


def test_ci_prepare_denies_pr_head_or_base_that_moved(tmp_path):
    coordinator, _gateway, _runner, _context_value = _coordinator(tmp_path)
    coordinator.github.pull["head"]["sha"] = "f" * 40

    with pytest.raises(CoordinatorError, match="ci_context_mismatch"):
        coordinator.prepare(6)


def test_ci_test_refetches_context_and_denies_new_head_before_runner(tmp_path):
    coordinator, gateway, runner, _context_value = _coordinator(tmp_path)
    coordinator.prepare(6)
    gateway.context["head_commit"] = "f" * 40
    gateway.context["context_sha256"] = ci_context_digest(gateway.context)

    with pytest.raises(CoordinatorError, match="ci_context_mismatch"):
        coordinator.run_candidate(6)

    assert runner.request_bytes == []


def test_ci_maps_missing_runsc_runtime_to_configuration_error(tmp_path):
    coordinator, _gateway, runner, _context_value = _coordinator(tmp_path)
    coordinator.prepare(6)
    runner.failure = DevelopmentError("sandbox_identity_invalid")

    with pytest.raises(CoordinatorError, match="invalid_runtime_configuration"):
        coordinator.run_candidate(6)


def test_ci_prepare_denies_deletions_instead_of_testing_a_different_overlay(tmp_path):
    coordinator, _gateway, _runner, _context_value = _coordinator(tmp_path, deleted=True)

    with pytest.raises(CoordinatorError, match="ci_patch_unrepresentable"):
        coordinator.prepare(6)


def test_ci_test_and_report_scan_then_upload_only_bounded_report(tmp_path, capsys):
    report = json.dumps(
        {"output": "tests passed; synthetic output"}, sort_keys=True, separators=(",", ":")
    ).encode()
    scanned: list[bytes] = []
    coordinator, gateway, runner, _context_value = _coordinator(
        tmp_path,
        report=report,
        scanner=lambda data: (scanned.append(data), ScanVerdict.CLEAN)[1],
    )
    report = json.dumps(
        {
            "version": 2,
            "base_commit": runner.baseline_commit,
            "policy_digest": runner.policy_digest,
            "worker_manifest_digest": runner.worker_manifest_digest,
            "execution_kind": "monorepo-pytest-v1",
            "exit_status": 0,
            "output": "tests passed; synthetic output",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    runner.report = report

    coordinator.prepare(6)
    test_result = coordinator.run_candidate(6)
    report_result = coordinator.scan_and_publish_report(6)

    assert test_result["phase"] == "tested"
    assert report_result["phase"] == "reported"
    assert len(scanned) == 1 and scanned[0].content == report
    assert json.loads(runner.request_bytes[0])["files"] == {"src/app.py": "VALUE = 2\n"}
    assert gateway.uploads[0]["report_base64"]
    artifact = next((tmp_path / "artifacts").glob("*"))
    assert artifact.read_bytes() == report
    stdout = capsys.readouterr().out
    assert "tests passed" not in stdout
    assert "VALUE = 2" not in stdout


def test_ci_report_rejects_scanner_finding_before_artifact_or_upload(tmp_path):
    report = b"candidate output"
    coordinator, gateway, _runner, _context_value = _coordinator(
        tmp_path,
        report=report,
        scanner=lambda _: ScanVerdict.FINDING,
    )
    coordinator.prepare(6)
    coordinator.run_candidate(6)

    with pytest.raises(CoordinatorError, match="ci_report_rejected"):
        coordinator.scan_and_publish_report(6)

    assert gateway.uploads == []
    assert not list((tmp_path / "artifacts").glob("*"))


def test_ci_context_digest_and_provenance_are_mandatory(tmp_path):
    coordinator, _gateway, _runner, context = _coordinator(tmp_path)
    context["provenance"]["head_commit"] = "f" * 40
    context["context_sha256"] = ci_context_digest(context)

    with pytest.raises(CoordinatorError, match="ci_context_mismatch"):
        coordinator.prepare(6)


def test_ci_cli_prints_only_safe_result_fields(monkeypatch, capsys):
    from chatcore_release_control import release_coordinator

    class Coordinator:
        def prepare(self, number):
            assert number == 6
            return {"phase": "prepared", "pull_request_number": 6, "head_sha": "a" * 40}

        def run_candidate(self, number):
            raise AssertionError(number)

        def scan_and_publish_report(self, number):
            raise AssertionError(number)

        def close(self):
            pass

    monkeypatch.setattr(release_coordinator, "build_ci_coordinator", lambda _env: Coordinator())
    assert main(["inspect-pull-request", "--pull-request-id", "6"], environment={}) == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {"phase": "prepared", "pull_request_number": 6, "head_sha": "a" * 40},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def test_ci_gateway_uses_fixed_origin_and_exact_phase_routes():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"context": True})
        return httpx.Response(201, json={"stored": True})

    gateway = CiGatewayClient(
        "https://gateway.example",
        token_provider=lambda: "synthetic-gateway-token",
        transport=httpx.MockTransport(handler),
    )
    assert gateway.get_ci_context(6) == {"context": True}
    assert gateway.upload_ci_report(6, {"report_sha256": "a" * 64}) == {"stored": True}
    assert [request.url for request in seen] == [
        "https://gateway.example/v1/ci/pull-requests/6/context",
        "https://gateway.example/v1/ci/pull-requests/6/report",
    ]
    assert all(
        request.headers["authorization"] == "Bearer synthetic-gateway-token" for request in seen
    )
    gateway.close()


def test_ci_runtime_config_missing_is_configuration_error_not_runtime_stub():
    with pytest.raises(CoordinatorError, match="invalid_runtime_configuration"):
        ProtectedCiRuntimeConfig.from_environment({})
    with pytest.raises(SystemExit, match="invalid_runtime_configuration"):
        main(["run-isolated-candidate", "--pull-request-id", "6"], environment={})


def test_ci_runtime_builder_constructs_pinned_adapters_without_running_them(tmp_path):
    repo, base, _head, policy = _repo(tmp_path)
    from chatcore_release_control import development_worker, monorepo_harness

    worker_source = Path(development_worker.__file__).read_bytes()
    harness_module = monorepo_harness
    harness_source = Path(harness_module.__file__).read_bytes()
    manifest_bytes = _baseline_manifest(repo, base, policy, worker_source, harness_source)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    manifest_path.chmod(0o600)
    policy_path = tmp_path / "policy.json"
    policy_value = {
        "version": policy.version,
        "repository_id": policy.repository_id,
        "owner_id": policy.owner_id,
        "target": policy.target,
        "workflow_digests": dict(policy.workflow_digests),
        "allowed_paths": list(policy.allowed_paths),
        "protected_paths": list(policy.protected_paths),
        "checks": [
            {
                "name": check.name,
                "creator_app_id": check.creator_app_id,
                "workflow_path": check.workflow_path,
                "workflow_sha": check.workflow_digest,
            }
            for check in policy.checks
        ],
    }
    policy_raw = json.dumps(policy_value, sort_keys=True, separators=(",", ":")).encode()
    policy_path.write_bytes(policy_raw)
    policy_path.chmod(0o600)
    baseline_root = tmp_path / "baseline"
    baseline_root.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory() as home:
        from chatcore_release_control.git_evidence import _GitRunner

        reader = _GitRunner(shutil.which("git") or "/usr/bin/git", repo, Path(home), 40)
        for entry in json.loads(manifest_bytes)["baseline_snapshot"]["files"]:
            data = reader.run(
                "cat-file", "blob", _git(repo, "rev-parse", f"{base}:{entry['path']}")
            )
            installed = baseline_root / entry["path"]
            installed.parent.mkdir(parents=True, exist_ok=True)
            installed.write_bytes(data)
            installed.chmod(entry["mode"])
    scanner_binary = tmp_path / "scanner"
    scanner_binary.write_bytes(b"synthetic scanner executable")
    scanner_binary.chmod(0o700)
    scanner_config = tmp_path / "scanner.conf"
    scanner_config.write_bytes(b"synthetic config")
    scanner_config.chmod(0o600)
    scanner_binary_digest = hashlib.sha256(scanner_binary.read_bytes()).hexdigest()
    scanner_config_digest = hashlib.sha256(scanner_config.read_bytes()).hexdigest()
    scanner_identity = hashlib.sha256(
        bytes.fromhex(scanner_binary_digest) + bytes.fromhex(scanner_config_digest)
    ).hexdigest()
    config = {
        "version": 1,
        "gateway_origin": "https://gateway.example",
        "repository": "acme/plugin",
        "repository_id": 77,
        "owner_id": 88,
        "object_repository": str(repo),
        "policy_path": str(policy_path),
        "policy_sha256": hashlib.sha256(policy_raw).hexdigest(),
        "worker_manifest_path": str(manifest_path),
        "worker_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "baseline_root": str(baseline_root),
        "docker_path": "/usr/bin/docker",
        "runtime_name": "runsc",
        "runtime_path": "/usr/bin/runsc",
        "runtime_sha256": "6" * 64,
        "image_reference": "registry.example/worker@sha256:" + "7" * 64,
        "resources": {
            "cpu_millis": 500,
            "memory_bytes": 268435456,
            "pids_limit": 64,
            "timeout_seconds": 60,
            "max_output_bytes": 524288,
            "workspace_bytes": 4194304,
        },
        "scanner_executable_path": str(scanner_binary),
        "scanner_executable_sha256": scanner_binary_digest,
        "scanner_configuration_path": str(scanner_config),
        "scanner_configuration_sha256": scanner_config_digest,
        "publication_scanner_identity_sha256": scanner_identity,
        "state_directory": str(tmp_path / "state"),
        "artifact_directory": str(tmp_path / "artifacts"),
    }
    config_raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    config_path = tmp_path / "ci-config.json"
    config_path.write_bytes(config_raw)
    config_path.chmod(0o600)
    environment = {
        "RELEASE_CONTROL_CI_CONFIG_PATH": str(config_path),
        "RELEASE_CONTROL_CI_CONFIG_SHA256": hashlib.sha256(config_raw).hexdigest(),
        "RELEASE_CONTROL_GITHUB_API_TOKEN": "synthetic-github-token",
        "RELEASE_CONTROL_CI_GATEWAY_TOKEN": "synthetic-gateway-token",
        "GITHUB_REPOSITORY": "acme/plugin",
        "GITHUB_EVENT_NAME": "pull_request",
    }
    coordinator = build_ci_coordinator(
        environment,
        gateway_transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
        github_transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )
    assert isinstance(coordinator, CiPhaseCoordinator)
    assert coordinator.sandbox.image_reference == config["image_reference"]
    assert coordinator.baseline_manifest.digest == config["worker_manifest_sha256"]
    coordinator.close()
