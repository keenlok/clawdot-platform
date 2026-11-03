from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
from pathlib import Path

import chatcore_release_control.hosted_bootstrap as hosted_bootstrap
import pytest
from chatcore_release_control.hosted_bootstrap import (
    HostedBootstrapError,
    HostedBootstrapper,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _pack(files: dict[str, tuple[bytes, int]]) -> bytes:
    archive_bytes_io = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes_io, mode="w") as archive:
        for path, (data, mode) in sorted(files.items()):
            info = tarfile.TarInfo(path)
            info.size, info.mode = len(data), mode
            archive.addfile(info, io.BytesIO(data))
    return archive_bytes_io.getvalue()


def _fixture(tmp_path: Path, *, bad_path: str | None = None):
    root = tmp_path / "install"
    root.mkdir(mode=0o700)
    files: dict[str, tuple[bytes, int]] = {}
    policy = {
        "version": 1,
        "repository_id": 10,
        "digest": "3" * 64,
        "allowed_paths": ["apps/sb_bot/src/sb_bot/app.py"],
    }
    policy_bytes = _canonical(policy)
    scanner = b"trusted scanner binary\n"
    scanner_config = b"scanner-policy-v1\n"
    runtime = b"trusted-runsc-binary\n"
    wheel = b"wheel-bytes"
    image = b"docker-image-tar"
    baseline_source = b"repository source bytes"
    baseline_tar = io.BytesIO()
    with tarfile.open(fileobj=baseline_tar, mode="w") as archive:
        info = tarfile.TarInfo("src/example.py")
        info.size = len(baseline_source)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(baseline_source))
    baseline_bytes = baseline_tar.getvalue()
    snapshot = {
        "tree": "1" * 40,
        "lock_digest": "2" * 64,
        "files": [
            {
                "path": "src/example.py",
                "size": len(baseline_source),
                "sha256": hashlib.sha256(baseline_source).hexdigest(),
                "mode": 0o644,
            }
        ],
        "source_roots": ["src"],
        "test_paths": ["tests"],
        "archive_digest": hashlib.sha256(baseline_bytes).hexdigest(),
        "reviewed_source_exceptions": [],
    }
    worker_manifest = _canonical(
        {
            "version": 2,
            "policy_digest": policy["digest"],
            "baseline_commit": "4" * 40,
            "worker_source_digest": "5" * 64,
            "test_harness_digest": "6" * 64,
            "allowed_paths": ["apps/sb_bot/src/sb_bot/app.py"],
            "baseline_snapshot": snapshot,
        }
    )
    image_reference = "ghcr.io/example/worker@sha256:" + "7" * 64
    config = {
        "version": 1,
        "gateway_origin": "https://release-gateway.example",
        "repository": "example/project",
        "repository_id": 10,
        "owner_id": 20,
        "object_repository": str(root / "source.git"),
        "policy_path": str(root / "runtime/policy.json"),
        "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
        "worker_manifest_path": str(root / "runtime/worker-manifest.json"),
        "worker_manifest_sha256": hashlib.sha256(worker_manifest).hexdigest(),
        "baseline_root": str(root / "runtime/baseline"),
        "docker_path": "/usr/bin/docker",
        "runtime_name": "runsc",
        "runtime_path": str(root / "runtime/runsc"),
        "runtime_sha256": hashlib.sha256(runtime).hexdigest(),
        "image_reference": image_reference,
        "resources": {
            "cpu_millis": 1000,
            "memory_bytes": 1024 * 1024 * 128,
            "pids_limit": 64,
            "timeout_seconds": 90,
            "max_output_bytes": 65536,
            "workspace_bytes": 1024 * 1024,
        },
        "scanner_executable_path": str(root / "runtime/scanner"),
        "scanner_executable_sha256": hashlib.sha256(scanner).hexdigest(),
        "scanner_configuration_path": str(root / "runtime/scanner-config"),
        "scanner_configuration_sha256": hashlib.sha256(scanner_config).hexdigest(),
        "publication_scanner_identity_sha256": hashlib.sha256(
            bytes.fromhex(hashlib.sha256(scanner).hexdigest())
            + bytes.fromhex(hashlib.sha256(scanner_config).hexdigest())
        ).hexdigest(),
        "state_directory": str(root / "state"),
        "artifact_directory": str(root / "artifacts"),
    }
    config_bytes = _canonical(config)
    runtime_policy = {
        "version": 1,
        "owner_policy_id": "p02-runsc-v1",
        "runtime_name": "runsc",
        "runtime_path": config["runtime_path"],
        "runtime_sha256": config["runtime_sha256"],
        "docker_path": config["docker_path"],
        "image_reference": image_reference,
        "worker_manifest_sha256": config["worker_manifest_sha256"],
        "runner_python_binary": "/usr/bin/python3",
        "resources": config["resources"],
        "network": "none",
        "read_only_root": True,
        "capabilities": [],
    }
    runtime_policy_bytes = _canonical(runtime_policy)
    files.update(
        {
            "runtime/ci-runtime.json": (config_bytes, 0o644),
            "runtime/policy.json": (policy_bytes, 0o644),
            "runtime/worker-manifest.json": (worker_manifest, 0o644),
            "runtime/baseline.tar": (baseline_bytes, 0o644),
            "runtime/worker-image.tar": (image, 0o644),
            "runtime/runsc": (runtime, 0o755),
            "runtime/runtime-policy.json": (runtime_policy_bytes, 0o644),
            "runtime/scanner": (scanner, 0o755),
            "runtime/scanner-config": (scanner_config, 0o644),
            "runtime/wheels/coordinator-1.0-py3-none-any.whl": (wheel, 0o644),
        }
    )
    if bad_path is not None:
        files[bad_path] = (b"unexpected", 0o600)
    archive_bytes = _pack(files)
    manifest = {
        "version": 1,
        "bootstrap_source_sha256": hashlib.sha256(
            Path(hosted_bootstrap.__file__).read_bytes()
        ).hexdigest(),
        "repository": "example/project",
        "repository_id": 10,
        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "archive_size": len(archive_bytes),
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "mode": mode,
            }
            for path, (data, mode) in sorted(files.items())
        ],
        "ci_config_path": "runtime/ci-runtime.json",
        "policy_path": "runtime/policy.json",
        "worker_manifest_path": "runtime/worker-manifest.json",
        "baseline_archive_path": "runtime/baseline.tar",
        "worker_image_path": "runtime/worker-image.tar",
        "runsc_path": "runtime/runsc",
        "runtime_policy_path": "runtime/runtime-policy.json",
        "scanner_path": "runtime/scanner",
        "scanner_config_path": "runtime/scanner-config",
        "coordinator_wheel": "runtime/wheels/coordinator-1.0-py3-none-any.whl",
        "image_reference": image_reference,
    }
    manifest_bytes = _canonical(manifest)
    return root, archive_bytes, manifest_bytes, manifest, files


class _Executor:
    def __init__(self, daemon_path: Path) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.daemon_path = daemon_path
        self.runtime_path = ""

    def run(self, args, *, stdin=None, timeout=30, output_limit=1024 * 1024):
        del stdin, timeout, output_limit
        call = tuple(str(part) for part in args)
        self.calls.append(call)
        if call[:2] == ("sudo", "install"):
            if "-d" in call:
                target = Path(call[-1])
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, int(call[call.index("-m") + 1], 8))
            elif "-D" in call:
                source, target = Path(call[-2]), Path(call[-1])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                os.chmod(target, int(call[call.index("-m") + 1], 8))
                if target.name == "runsc":
                    self.runtime_path = str(target)
            else:
                source, target = Path(call[-2]), Path(call[-1])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        if "venv" in call and "-m" in call:
            target = Path(call[-1])
            python = target / "bin/python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("trusted fake venv python\n")
        if call[:2] == ("sudo", "mv"):
            shutil.move(call[-2], call[-1])
        if "info" in call:
            return _canonical({"runsc": {"path": self.runtime_path}})
        if "inspect" in call:
            return _canonical([{"RepoDigests": [call[-1]]}])
        return b""


def _make_bootstrap(tmp_path: Path, fixture=None):
    root, archive, manifest_bytes, manifest, files = fixture or _fixture(tmp_path)
    calls: list[tuple[str, str, int]] = []

    def download(url: str, token: str, max_bytes: int) -> bytes:
        calls.append((url, token, max_bytes))
        if url.endswith("/v1/ci/bootstrap/manifest"):
            return manifest_bytes
        if url.endswith(f"/v1/ci/bootstrap/artifacts/{manifest['archive_sha256']}"):
            return archive
        raise AssertionError(url)

    executor = _Executor(root / "daemon.json")
    env_file = tmp_path / "github-env"
    env_file.touch()
    bootstrapper = HostedBootstrapper(
        origin="https://release-gateway.example",
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        repository="example/project",
        repository_id=10,
        bearer_token="test-token-0123456789",
        install_root=root,
        download=download,
        executor=executor,
        github_environment_file=env_file,
        docker_daemon_config_path=root / "daemon.json",
    )
    return bootstrapper, calls, executor, env_file, files, manifest_bytes


def test_bootstrap_verifies_exact_inventory_and_installs_offline_before_ci(tmp_path):
    bootstrapper, calls, executor, env_file, files, _ = _make_bootstrap(tmp_path)

    result = bootstrapper.run()

    assert result.config_path.endswith("/runtime/ci-runtime.json")
    assert Path(result.config_path).read_bytes() == files["runtime/ci-runtime.json"][0]
    assert (
        Path(result.config_path).parent / "baseline/src/example.py"
    ).read_bytes() == b"repository source bytes"
    assert calls == [
        (
            "https://release-gateway.example/v1/ci/bootstrap/manifest",
            "test-token-0123456789",
            64 * 1024,
        ),
        (
            f"https://release-gateway.example/v1/ci/bootstrap/artifacts/{result.archive_sha256}",
            "test-token-0123456789",
            result.archive_size,
        ),
    ]
    assert "RELEASE_CONTROL_CI_CONFIG_PATH=" in env_file.read_text()
    assert "RELEASE_CONTROL_CI_CONFIG_SHA256=" in env_file.read_text()
    pip = next(call for call in executor.calls if "pip" in call)
    assert "--no-index" in pip and "--no-deps" in pip
    assert all("test-token-0123456789" not in arg for call in executor.calls for arg in call)
    assert any("load" in call for call in executor.calls)
    assert any(any("runsc" in argument for argument in call) for call in executor.calls)
    assert len(list((Path(result.config_path).parent / "wheels").glob("*.whl"))) == 1
    for relative in ("source.git", "state", "artifacts"):
        directory = Path(result.config_path).parents[1] / relative
        command = next(
            call for call in executor.calls if "-d" in call and call[-1] == str(directory)
        )
        assert command[command.index("-o") + 1] == str(os.getuid())
        assert command[command.index("-g") + 1] == str(os.getgid())
        assert command[command.index("-m") + 1] == "0700"
        info = directory.stat()
        assert (info.st_uid, info.st_gid, info.st_mode & 0o777) == (
            os.getuid(),
            os.getgid(),
            0o700,
        )
    assert (Path(result.config_path).parents[1] / "source.git/objects/pack").is_dir()


@pytest.mark.parametrize(
    "change", ["manifest", "bootstrap-source", "archive", "file", "inventory", "policy"]
)
def test_bootstrap_fails_before_host_mutation_on_bad_pins(tmp_path, change):
    root, archive, manifest_bytes, manifest, files = _fixture(tmp_path)
    if change == "manifest":
        expected = "0" * 64
    elif change == "bootstrap-source":
        manifest["bootstrap_source_sha256"] = "0" * 64
        manifest_bytes = _canonical(manifest)
        expected = hashlib.sha256(manifest_bytes).hexdigest()
    elif change == "archive":
        archive = archive[:-1] + bytes([archive[-1] ^ 1])
        expected = hashlib.sha256(manifest_bytes).hexdigest()
    elif change == "file":
        archive = archive.replace(b"trusted scanner", b"altered scanner")
        expected = hashlib.sha256(manifest_bytes).hexdigest()
    elif change == "inventory":
        manifest["files"].append(manifest["files"][0])
        manifest_bytes = _canonical(manifest)
        expected = hashlib.sha256(manifest_bytes).hexdigest()
    else:
        config = json.loads(files["runtime/ci-runtime.json"][0])
        config["image_reference"] = "ghcr.io/other/worker@sha256:" + "8" * 64
        files["runtime/ci-runtime.json"] = (_canonical(config), 0o644)
        archive = _pack(files)
        manifest["archive_sha256"] = hashlib.sha256(archive).hexdigest()
        manifest["archive_size"] = len(archive)
        manifest["files"] = [
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "mode": mode,
            }
            for path, (data, mode) in sorted(files.items())
        ]
        manifest_bytes = _canonical(manifest)
        expected = hashlib.sha256(manifest_bytes).hexdigest()

    def download(url: str, token: str, max_bytes: int) -> bytes:
        del token, max_bytes
        if url.endswith("/manifest"):
            return manifest_bytes
        return archive

    executor = _Executor(root / "daemon.json")
    env_file = tmp_path / "github-env"
    env_file.touch()
    bootstrapper = HostedBootstrapper(
        origin="https://release-gateway.example",
        manifest_sha256=expected,
        repository="example/project",
        repository_id=10,
        bearer_token="test-token-0123456789",
        install_root=root,
        download=download,
        executor=executor,
        github_environment_file=env_file,
        docker_daemon_config_path=root / "daemon.json",
    )

    with pytest.raises(HostedBootstrapError):
        bootstrapper.run()

    assert executor.calls == []
    assert env_file.read_bytes() == b""


@pytest.mark.parametrize(
    "member_type,path",
    [(tarfile.SYMTYPE, "runtime/wheels/evil.whl"), (tarfile.LNKTYPE, "runtime/wheels/evil.whl")],
)
def test_bootstrap_rejects_links_without_installing(tmp_path, member_type, path):
    root, _, manifest_bytes, manifest, _ = _fixture(tmp_path)
    archive_io = io.BytesIO()
    with tarfile.open(fileobj=archive_io, mode="w") as archive:
        link = tarfile.TarInfo(path)
        link.type, link.linkname = member_type, "../../etc/shadow"
        archive.addfile(link)
    archive_bytes = archive_io.getvalue()
    manifest["archive_sha256"] = hashlib.sha256(archive_bytes).hexdigest()
    manifest["archive_size"] = len(archive_bytes)
    manifest_bytes = _canonical(manifest)

    def download(url: str, token: str, max_bytes: int) -> bytes:
        del token, max_bytes
        return manifest_bytes if url.endswith("/manifest") else archive_bytes

    executor = _Executor(root / "daemon.json")
    bootstrapper = HostedBootstrapper(
        origin="https://release-gateway.example",
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        repository="example/project",
        repository_id=10,
        bearer_token="test-token-0123456789",
        install_root=root,
        download=download,
        executor=executor,
        docker_daemon_config_path=root / "daemon.json",
    )

    with pytest.raises(HostedBootstrapError):
        bootstrapper.run()

    assert executor.calls == []


@pytest.mark.parametrize("attack", ["traversal", "duplicate", "truncated"])
def test_bootstrap_rejects_malformed_archive_before_host_mutation(tmp_path, attack):
    root, _, manifest_bytes, manifest, files = _fixture(tmp_path)
    archive_io = io.BytesIO()
    with tarfile.open(fileobj=archive_io, mode="w") as archive:
        for path, (data, mode) in sorted(files.items()):
            info = tarfile.TarInfo(path)
            info.size, info.mode = len(data), mode
            archive.addfile(info, io.BytesIO(data))
        if attack == "traversal":
            info = tarfile.TarInfo("runtime/../escape")
            info.size, info.mode = 1, 0o644
            archive.addfile(info, io.BytesIO(b"x"))
        elif attack == "duplicate":
            data, mode = files["runtime/runsc"]
            info = tarfile.TarInfo("runtime/runsc")
            info.size, info.mode = len(data), mode
            archive.addfile(info, io.BytesIO(data))
    archive_bytes = archive_io.getvalue()
    if attack == "truncated":
        archive_bytes = archive_bytes[:512]
    manifest["archive_sha256"] = hashlib.sha256(archive_bytes).hexdigest()
    manifest["archive_size"] = len(archive_bytes)
    manifest_bytes = _canonical(manifest)

    def download(url: str, token: str, max_bytes: int) -> bytes:
        del token, max_bytes
        return manifest_bytes if url.endswith("/manifest") else archive_bytes

    executor = _Executor(root / "daemon.json")
    bootstrapper = HostedBootstrapper(
        origin="https://release-gateway.example",
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        repository="example/project",
        repository_id=10,
        bearer_token="test-token-0123456789",
        install_root=root,
        download=download,
        executor=executor,
        docker_daemon_config_path=root / "daemon.json",
    )

    with pytest.raises(HostedBootstrapError):
        bootstrapper.run()

    assert executor.calls == []


def test_bootstrap_rejects_conflicting_existing_runsc_registration_before_install(tmp_path):
    root, archive, manifest_bytes, manifest, _ = _fixture(tmp_path)
    daemon_path = tmp_path / "daemon.json"
    daemon_path.write_bytes(_canonical({"runtimes": {"runsc": {"path": "/wrong/runsc"}}}))

    def download(url: str, token: str, max_bytes: int) -> bytes:
        del token, max_bytes
        return manifest_bytes if url.endswith("/manifest") else archive

    executor = _Executor(daemon_path)
    bootstrapper = HostedBootstrapper(
        origin="https://release-gateway.example",
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        repository="example/project",
        repository_id=10,
        bearer_token="test-token-0123456789",
        install_root=root,
        download=download,
        executor=executor,
        docker_daemon_config_path=daemon_path,
    )

    with pytest.raises(HostedBootstrapError):
        bootstrapper.run()

    assert executor.calls == []
    assert daemon_path.read_bytes() == _canonical({"runtimes": {"runsc": {"path": "/wrong/runsc"}}})


def test_bootstrap_rejects_mutable_image_or_origin_and_runtime_conflict(tmp_path):
    bootstrapper, _, _, _, _, _ = _make_bootstrap(tmp_path)
    bootstrapper.origin = "http://release-gateway.example"
    with pytest.raises(HostedBootstrapError):
        bootstrapper.run()


def test_inactive_ci_template_bootstraps_before_trusted_coordinator_without_checkout():
    template = (
        Path("services/release-control/src/chatcore_release_control")
        / "templates"
        / "p04-ci.yml.inactive"
    ).read_text()
    assert template.index("Bootstrap pinned trusted runtime") < template.index(
        "inspect-pull-request"
    )
    assert "actions/checkout" not in template
    assert "hosted-bootstrap.py" in template
    assert "worker image" in template.lower()
