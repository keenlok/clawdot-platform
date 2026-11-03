"""Install digest-pinned trusted CI runtime artifacts on an ephemeral runner.

This module is intentionally standard-library-only so a tiny, independently
digest-pinned copy can run before the packaged release coordinator. Its only
network paths and host commands are fixed; downloaded artifacts are fully
validated before installation or execution.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_./+=:-]{16,4096}\Z")
_IMAGE = re.compile(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}\Z")
_MAX_MANIFEST = 64 * 1024
_MAX_ARCHIVE = 1024 * 1024 * 1024
_MAX_FILE = 512 * 1024 * 1024
_MAX_FILES = 4096
_MAX_TOTAL = 1024 * 1024 * 1024
_MAX_TIMEOUT = 90
_INSTALL_ROOT = Path("/opt/release-control")
_MANIFEST_PATH = "/v1/ci/bootstrap/manifest"
_ARCHIVE_PATH = "/v1/ci/bootstrap/artifacts/"


class HostedBootstrapError(ValueError):
    """Safe bootstrap failure with no downloaded data or credential details."""

    def __init__(self, code: str = "bootstrap_invalid") -> None:
        if code not in {
            "bootstrap_invalid",
            "bootstrap_download_failed",
            "bootstrap_install_failed",
            "bootstrap_runtime_unavailable",
        }:
            code = "bootstrap_invalid"
        super().__init__(code)
        self.code = code


class CommandExecutor(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        stdin: bytes | None = None,
        timeout: int = 30,
        output_limit: int = 1024 * 1024,
    ) -> bytes: ...


Download = Callable[[str, str, int], bytes]


@dataclass(frozen=True)
class BootstrapResult:
    config_path: str
    config_sha256: str
    archive_sha256: str
    archive_size: int
    installed_files: tuple[str, ...] = field(repr=False)


class SubprocessExecutor:
    """Run only explicit host administration commands with a clean environment."""

    def run(
        self,
        args: Sequence[str],
        *,
        stdin: bytes | None = None,
        timeout: int = 30,
        output_limit: int = 1024 * 1024,
    ) -> bytes:
        if (
            not isinstance(args, (tuple, list))
            or not args
            or any(not isinstance(item, str) or "\0" in item for item in args)
            or type(timeout) is not int
            or not 1 <= timeout <= _MAX_TIMEOUT
            or type(output_limit) is not int
            or not 1 <= output_limit <= 4 * 1024 * 1024
            or (stdin is not None and not isinstance(stdin, bytes))
        ):
            raise HostedBootstrapError()
        clean_env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/root",
            "LC_ALL": "C",
            "LANG": "C",
        }
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                completed = subprocess.run(  # noqa: S603 - explicit fixed host administration commands.
                    list(args),
                    input=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                    timeout=timeout,
                    env=clean_env,
                    close_fds=True,
                )
                if stdout.tell() > output_limit or stderr.tell() > output_limit:
                    raise HostedBootstrapError("bootstrap_install_failed")
                stdout.seek(0)
                result = stdout.read(output_limit + 1)
                if completed.returncode != 0 or len(result) > output_limit:
                    raise HostedBootstrapError("bootstrap_install_failed")
                return result
        except HostedBootstrapError:
            raise
        except (OSError, subprocess.SubprocessError):
            raise HostedBootstrapError("bootstrap_install_failed") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        return None


def _https_get(url: str, bearer_token: str, max_bytes: int) -> bytes:
    """Fetch one bounded response without environment proxy or redirect use."""
    if (
        not isinstance(url, str)
        or not _TOKEN.fullmatch(bearer_token)
        or type(max_bytes) is not int
        or not 1 <= max_bytes <= _MAX_ARCHIVE
    ):
        raise HostedBootstrapError("bootstrap_download_failed")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or not (
            parsed.path == _MANIFEST_PATH
            or re.fullmatch(_ARCHIVE_PATH + r"[0-9a-f]{64}", parsed.path) is not None
            or parsed.path == "/v1/ci/bootstrap/hosted-bootstrap.py"
        )
    ):
        raise HostedBootstrapError("bootstrap_download_failed")
    request = urllib.request.Request(  # noqa: S310 - the URL is restricted to fixed HTTPS paths.
        url,
        headers={
            "Accept": "application/json, application/octet-stream",
            "Authorization": f"Bearer {bearer_token}",
            "User-Agent": "release-control-hosted-bootstrap/1",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    started = time.monotonic()
    try:
        with opener.open(request, timeout=10) as response:  # noqa: S310 - HTTPS host/path validated above.
            if response.status != 200 or response.geturl() != url:
                raise HostedBootstrapError("bootstrap_download_failed")
            content_length = response.headers.get("Content-Length")
            if content_length is not None and (
                not content_length.isdecimal() or int(content_length) > max_bytes
            ):
                raise HostedBootstrapError("bootstrap_download_failed")
            parts = bytearray()
            while True:
                if time.monotonic() - started > _MAX_TIMEOUT:
                    raise HostedBootstrapError("bootstrap_download_failed")
                chunk = response.read(min(64 * 1024, max_bytes + 1 - len(parts)))
                if not chunk:
                    break
                parts.extend(chunk)
                if len(parts) > max_bytes:
                    raise HostedBootstrapError("bootstrap_download_failed")
            return bytes(parts)
    except HostedBootstrapError:
        raise
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        raise HostedBootstrapError("bootstrap_download_failed") from None


class HostedBootstrapper:
    """Download, verify, and install the owner-provisioned runner closure."""

    def __init__(
        self,
        *,
        origin: str,
        manifest_sha256: str,
        repository: str,
        repository_id: int,
        bearer_token: str,
        install_root: str | os.PathLike[str] = _INSTALL_ROOT,
        download: Download = _https_get,
        executor: CommandExecutor | None = None,
        github_environment_file: str | os.PathLike[str] | None = None,
        docker_daemon_config_path: str | os.PathLike[str] = "/etc/docker/daemon.json",
    ) -> None:
        self.origin = origin
        self.manifest_sha256 = manifest_sha256
        self.repository = repository
        self.repository_id = repository_id
        self._token = bearer_token
        self.install_root = Path(install_root)
        self.download = download
        self.executor = executor or SubprocessExecutor()
        self.github_environment_file = (
            None if github_environment_file is None else Path(github_environment_file)
        )
        self._docker_daemon_config_path = Path(docker_daemon_config_path)

    def run(self) -> BootstrapResult:
        self._validate_inputs()
        manifest_url = self.origin + _MANIFEST_PATH
        manifest_raw = self._fetch(manifest_url, _MAX_MANIFEST)
        if hashlib.sha256(manifest_raw).hexdigest() != self.manifest_sha256:
            raise HostedBootstrapError("bootstrap_invalid")
        manifest = _parse_canonical_json(manifest_raw, max_bytes=_MAX_MANIFEST)
        try:
            bootstrap_source_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        except OSError:
            raise HostedBootstrapError("bootstrap_invalid") from None
        if manifest.get("bootstrap_source_sha256") != bootstrap_source_digest:
            raise HostedBootstrapError("bootstrap_invalid")
        _validate_manifest(
            manifest,
            repository=self.repository,
            repository_id=self.repository_id,
        )
        archive_url = self.origin + _ARCHIVE_PATH + manifest["archive_sha256"]
        archive = self._fetch(archive_url, manifest["archive_size"])
        if (
            len(archive) != manifest["archive_size"]
            or hashlib.sha256(archive).hexdigest() != manifest["archive_sha256"]
        ):
            raise HostedBootstrapError("bootstrap_invalid")

        try:
            with tempfile.TemporaryDirectory(prefix="release-control-stage-") as temp:
                stage = Path(temp)
                extracted = _extract_exact_archive(archive, manifest["files"], stage)
                checks = _validate_runtime_artifacts(
                    manifest,
                    stage,
                    self.install_root,
                    repository=self.repository,
                    repository_id=self.repository_id,
                    expected_origin=self.origin,
                )
                baseline_snapshot = checks["worker_manifest"]["baseline_snapshot"]
                baseline_archive = (stage / manifest["baseline_archive_path"]).read_bytes()
                _extract_exact_archive(
                    baseline_archive,
                    baseline_snapshot["files"],
                    stage / "runtime/baseline",
                    strip_prefix="",
                )
                daemon_config = self._prepare_daemon_config(checks["runtime_policy"])
                self._install(stage, manifest, checks, daemon_config)
                config_path = str(self.install_root / "runtime/ci-runtime.json")
                config_digest = hashlib.sha256(checks["config_raw"]).hexdigest()
                self._write_github_environment(config_path, config_digest)
                return BootstrapResult(
                    config_path,
                    config_digest,
                    manifest["archive_sha256"],
                    len(archive),
                    tuple(sorted(str(self.install_root / path) for path in extracted)),
                )
        except HostedBootstrapError:
            raise
        except (OSError, ValueError, TypeError, KeyError, tarfile.TarError):
            raise HostedBootstrapError("bootstrap_install_failed") from None

    def _fetch(self, url: str, maximum: int) -> bytes:
        try:
            result = self.download(url, self._token, maximum)
            if not isinstance(result, bytes) or len(result) > maximum:
                raise ValueError
            return result
        except Exception:
            raise HostedBootstrapError("bootstrap_download_failed") from None

    def _validate_inputs(self) -> None:
        parsed = urlsplit(self.origin) if isinstance(self.origin, str) else None
        if (
            parsed is None
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 443}
            or self.origin != f"https://{parsed.hostname.lower()}"
            or not isinstance(self.manifest_sha256, str)
            or not _DIGEST.fullmatch(self.manifest_sha256)
            or not isinstance(self.repository, str)
            or not _REPOSITORY.fullmatch(self.repository)
            or type(self.repository_id) is not int
            or self.repository_id <= 0
            or not isinstance(self._token, str)
            or not _TOKEN.fullmatch(self._token)
            or not self.install_root.is_absolute()
            or self.install_root == Path("/")
            or not callable(self.download)
            or not callable(getattr(self.executor, "run", None))
            or os.getuid() <= 0
            or os.getgid() <= 0
        ):
            raise HostedBootstrapError("bootstrap_invalid")
        try:
            info = self.install_root.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o022
            or any(self.install_root.iterdir())
        ):
            raise HostedBootstrapError("bootstrap_invalid")

    def _install(
        self,
        stage: Path,
        manifest: Mapping[str, object],
        checks: Mapping[str, object],
        daemon_config: bytes,
    ) -> None:
        # All downloaded content and cross-file relationships have passed before
        # the first privileged command or package install is reached.
        runner_uid, runner_gid = os.getuid(), os.getgid()
        if runner_uid <= 0 or runner_gid <= 0:
            raise HostedBootstrapError("bootstrap_install_failed")
        self.executor.run(
            (
                "sudo",
                "install",
                "-d",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0755",
                str(self.install_root),
            )
        )
        for relative in ("source.git", "state", "artifacts"):
            self.executor.run(
                (
                    "sudo",
                    "install",
                    "-d",
                    "-o",
                    str(runner_uid),
                    "-g",
                    str(runner_gid),
                    "-m",
                    "0700",
                    str(self.install_root / relative),
                )
            )
        for record in manifest["files"]:
            relative = record["path"]
            source = stage / relative
            destination = self.install_root / relative
            mode = f"{record['mode']:04o}"
            self.executor.run(
                (
                    "sudo",
                    "install",
                    "-D",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    mode,
                    str(source),
                    str(destination),
                )
            )
        baseline_root = self.install_root / "runtime/baseline"
        for record in checks["worker_manifest"]["baseline_snapshot"]["files"]:
            source = stage / "runtime/baseline" / record["path"]
            destination = baseline_root / record["path"]
            self.executor.run(
                (
                    "sudo",
                    "install",
                    "-D",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    f"{record['mode']:04o}",
                    str(source),
                    str(destination),
                )
            )
        _verify_installed_inventory(
            self.install_root,
            manifest["files"],
            checks["worker_manifest"]["baseline_snapshot"]["files"],
        )
        object_repository = Path(checks["config"]["object_repository"])
        repository_info = object_repository.lstat()
        if (
            not stat.S_ISDIR(repository_info.st_mode)
            or repository_info.st_uid != runner_uid
            or repository_info.st_gid != runner_gid
            or stat.S_IMODE(repository_info.st_mode) != 0o700
            or any(object_repository.iterdir())
        ):
            raise HostedBootstrapError("bootstrap_install_failed")
        objects = object_repository / "objects"
        objects.mkdir(mode=0o700, parents=False, exist_ok=False)
        (objects / "info").mkdir(mode=0o700)
        (objects / "pack").mkdir(mode=0o700)
        self.executor.run(
            (
                "sudo",
                "install",
                "-d",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0755",
                str(self.install_root / "venv"),
            )
        )
        self.executor.run(
            ("sudo", "/usr/bin/python3", "-m", "venv", str(self.install_root / "venv")), timeout=60
        )
        wheel_paths = sorted(
            str(self.install_root / record["path"])
            for record in manifest["files"]
            if record["path"].endswith(".whl")
        )
        if not wheel_paths:
            raise HostedBootstrapError()
        self.executor.run(
            (
                "sudo",
                str(self.install_root / "venv/bin/python"),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--no-cache-dir",
                "--disable-pip-version-check",
                "--no-compile",
                *wheel_paths,
            ),
            timeout=60,
        )
        self.executor.run(
            (
                "sudo",
                str(self.install_root / "venv/bin/python"),
                "-m",
                "pip",
                "check",
            ),
            timeout=30,
        )
        self._register_runsc(checks["runtime_policy"], daemon_config)
        self.executor.run(
            (
                "sudo",
                checks["config"]["docker_path"],
                "load",
                "--input",
                str(self.install_root / manifest["worker_image_path"]),
            ),
            timeout=60,
        )
        inspected = self.executor.run(
            (
                "sudo",
                checks["config"]["docker_path"],
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                checks["config"]["image_reference"],
            )
        )
        if checks["config"]["image_reference"] not in _image_digests(inspected):
            raise HostedBootstrapError("bootstrap_runtime_unavailable")
        self._check_runtime(checks["config"], checks["worker_manifest"])

    def _prepare_daemon_config(self, policy: Mapping[str, object]) -> bytes:
        daemon_path = self._docker_daemon_config_path
        daemon = _read_json_if_present(daemon_path)
        runtimes = daemon.setdefault("runtimes", {})
        if not isinstance(runtimes, dict):
            raise HostedBootstrapError("bootstrap_invalid")
        expected = {"path": str(self.install_root / "runtime/runsc")}
        existing = runtimes.get("runsc")
        if existing is not None and existing != expected:
            raise HostedBootstrapError("bootstrap_invalid")
        runtimes["runsc"] = expected
        return _canonical_json(daemon)

    def _register_runsc(self, policy: Mapping[str, object], daemon_bytes: bytes) -> None:
        daemon_path = self._docker_daemon_config_path
        expected_path = str(self.install_root / "runtime/runsc")
        with tempfile.NamedTemporaryFile(prefix="release-control-daemon-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(daemon_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            self.executor.run(
                (
                    "sudo",
                    "install",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    "0644",
                    str(temporary),
                    str(daemon_path) + ".release-control",
                )
            )
            self.executor.run(
                ("sudo", "mv", "-f", "--", str(daemon_path) + ".release-control", str(daemon_path))
            )
            self.executor.run(("sudo", "systemctl", "restart", "docker"), timeout=60)
            info = self.executor.run(
                ("sudo", str(policy["docker_path"]), "info", "--format", "{{json .Runtimes}}")
            )
            runtimes_value = _parse_json(info, max_bytes=64 * 1024)
            runsc = runtimes_value.get("runsc") if isinstance(runtimes_value, dict) else None
            if not isinstance(runsc, dict) or runsc.get("path") != expected_path:
                raise HostedBootstrapError("bootstrap_runtime_unavailable")
        finally:
            temporary.unlink(missing_ok=True)

    def _check_runtime(self, config: Mapping[str, object], worker: Mapping[str, object]) -> None:
        policy = _parse_canonical_json(
            (self.install_root / "runtime/runtime-policy.json").read_bytes(), max_bytes=64 * 1024
        )
        if not isinstance(worker.get("baseline_snapshot"), dict):
            raise HostedBootstrapError()
        self.executor.run(
            (
                "sudo",
                config["docker_path"],
                "run",
                "--rm",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=8",
                "--memory=128m",
                "--runtime=runsc",
                "--entrypoint=" + policy["runner_python_binary"],
                config["image_reference"],
                "-I",
                "-c",
                "pass",
            ),
            timeout=30,
        )

    def _write_github_environment(self, config_path: str, config_digest: str) -> None:
        if self.github_environment_file is None:
            return
        path = self.github_environment_file
        if not path.is_absolute():
            raise HostedBootstrapError()
        payload = (
            f"RELEASE_CONTROL_CI_CONFIG_PATH={config_path}\n"
            f"RELEASE_CONTROL_CI_CONFIG_SHA256={config_digest}\n"
        ).encode("ascii")
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            raise HostedBootstrapError("bootstrap_install_failed") from None


def _validate_manifest(value: object, *, repository: str, repository_id: int) -> None:
    required = {
        "version",
        "bootstrap_source_sha256",
        "repository",
        "repository_id",
        "archive_sha256",
        "archive_size",
        "files",
        "ci_config_path",
        "policy_path",
        "worker_manifest_path",
        "baseline_archive_path",
        "worker_image_path",
        "runsc_path",
        "runtime_policy_path",
        "scanner_path",
        "scanner_config_path",
        "coordinator_wheel",
        "image_reference",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["version"] != 1
        or not isinstance(value["bootstrap_source_sha256"], str)
        or not _DIGEST.fullmatch(value["bootstrap_source_sha256"])
        or value["repository"] != repository
        or type(value["repository_id"]) is not int
        or value["repository_id"] != repository_id
        or not isinstance(value["archive_sha256"], str)
        or not _DIGEST.fullmatch(value["archive_sha256"])
        or type(value["archive_size"]) is not int
        or not 1 <= value["archive_size"] <= _MAX_ARCHIVE
        or not _IMAGE.fullmatch(value["image_reference"])
        or value["ci_config_path"] != "runtime/ci-runtime.json"
        or value["policy_path"] != "runtime/policy.json"
        or value["worker_manifest_path"] != "runtime/worker-manifest.json"
        or value["baseline_archive_path"] != "runtime/baseline.tar"
        or value["worker_image_path"] != "runtime/worker-image.tar"
        or value["runsc_path"] != "runtime/runsc"
        or value["runtime_policy_path"] != "runtime/runtime-policy.json"
        or value["scanner_path"] != "runtime/scanner"
        or value["scanner_config_path"] != "runtime/scanner-config"
        or not isinstance(value["coordinator_wheel"], str)
        or not value["coordinator_wheel"].startswith("runtime/wheels/")
        or not value["coordinator_wheel"].endswith(".whl")
        or not isinstance(value["files"], list)
        or not 9 <= len(value["files"]) <= _MAX_FILES
    ):
        raise HostedBootstrapError()
    seen: set[str] = set()
    previous = ""
    for record in value["files"]:
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "sha256", "size", "mode"}
            or not isinstance(record["path"], str)
            or not _safe_artifact_path(record["path"])
            or record["path"] in seen
            or record["path"] <= previous
            or not isinstance(record["sha256"], str)
            or not _DIGEST.fullmatch(record["sha256"])
            or type(record["size"]) is not int
            or not 0 <= record["size"] <= _MAX_FILE
            or type(record["mode"]) is not int
            or record["mode"] not in {0o600, 0o644, 0o700, 0o755}
            or record["mode"]
            != (0o755 if record["path"] in {"runtime/runsc", "runtime/scanner"} else 0o644)
        ):
            raise HostedBootstrapError()
        seen.add(record["path"])
        previous = record["path"]
    required_paths = {
        value[key]
        for key in (
            "ci_config_path",
            "policy_path",
            "worker_manifest_path",
            "baseline_archive_path",
            "worker_image_path",
            "runsc_path",
            "runtime_policy_path",
            "scanner_path",
            "scanner_config_path",
            "coordinator_wheel",
        )
    }
    if not required_paths.issubset(seen) or not any(
        path.startswith("runtime/wheels/") and path.endswith(".whl") for path in seen
    ):
        raise HostedBootstrapError()


def _safe_artifact_path(value: str) -> bool:
    path = PurePosixPath(value)
    if (
        not value.startswith("runtime/")
        or value.startswith("/")
        or "\\" in value
        or "\0" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or path.as_posix() != value
    ):
        return False
    return value in {
        "runtime/ci-runtime.json",
        "runtime/policy.json",
        "runtime/worker-manifest.json",
        "runtime/baseline.tar",
        "runtime/worker-image.tar",
        "runtime/runsc",
        "runtime/runtime-policy.json",
        "runtime/scanner",
        "runtime/scanner-config",
    } or (value.startswith("runtime/wheels/") and value.endswith(".whl"))


def _extract_exact_archive(
    archive_bytes: bytes,
    records: object,
    destination: Path,
    *,
    strip_prefix: str = "",
) -> tuple[str, ...]:
    if not isinstance(records, list) or len(archive_bytes) > _MAX_ARCHIVE:
        raise HostedBootstrapError()
    expected: dict[str, Mapping[str, object]] = {}
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "sha256", "size", "mode"}
            or not isinstance(record["path"], str)
            or not _safe_relative_file(record["path"])
            or record["path"] in expected
            or not isinstance(record["sha256"], str)
            or not _DIGEST.fullmatch(record["sha256"])
            or type(record["size"]) is not int
            or not 0 <= record["size"] <= _MAX_FILE
            or type(record["mode"]) is not int
            or record["mode"] not in {0o600, 0o644, 0o700, 0o755}
        ):
            raise HostedBootstrapError()
        expected[record["path"]] = record
    if not expected or len(expected) > _MAX_FILES:
        raise HostedBootstrapError()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    actual: dict[str, bytes] = {}
    total = 0
    allowed_dirs = {
        "/".join(parts[:index])
        for relative in expected
        for parts in [relative.split("/")]
        for index in range(1, len(parts))
    }
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
            members = archive.getmembers()
            if len(members) > _MAX_FILES * 2:
                raise HostedBootstrapError()
            dirs: set[str] = set()
            for member in members:
                name = member.name
                if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
                    raise HostedBootstrapError()
                normalized = name.rstrip("/") if member.isdir() else name
                if not _safe_relative_path(normalized):
                    raise HostedBootstrapError()
                if member.pax_headers or getattr(member, "sparse", None):
                    raise HostedBootstrapError()
                if member.isdir():
                    if (
                        normalized not in allowed_dirs
                        or normalized in dirs
                        or stat.S_IMODE(member.mode) & 0o022
                    ):
                        raise HostedBootstrapError()
                    dirs.add(normalized)
                    continue
                if not member.isfile() or member.islnk() or member.issym():
                    raise HostedBootstrapError()
                if (
                    normalized in actual
                    or normalized not in expected
                    or member.size != expected[normalized]["size"]
                ):
                    raise HostedBootstrapError()
                data_stream = archive.extractfile(member)
                if data_stream is None:
                    raise HostedBootstrapError()
                data = data_stream.read(_MAX_FILE + 1)
                if len(data) != member.size:
                    raise HostedBootstrapError()
                total += len(data)
                if total > _MAX_TOTAL:
                    raise HostedBootstrapError()
                record = expected[normalized]
                if hashlib.sha256(data).hexdigest() != record["sha256"]:
                    raise HostedBootstrapError()
                tar_mode = stat.S_IMODE(member.mode)
                if tar_mode != record["mode"]:
                    raise HostedBootstrapError()
                actual[normalized] = data
            if set(actual) != set(expected):
                raise HostedBootstrapError()
    except (tarfile.TarError, OSError, EOFError):
        raise HostedBootstrapError() from None

    for relative, data in actual.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags, expected[relative]["mode"])
            try:
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(target, expected[relative]["mode"])
        except OSError:
            raise HostedBootstrapError() from None
    return tuple(sorted(actual))


def _validate_runtime_artifacts(
    manifest: Mapping[str, object],
    stage: Path,
    install_root: Path,
    *,
    repository: str,
    repository_id: int,
    expected_origin: str,
) -> dict[str, object]:
    try:
        config_raw = (stage / manifest["ci_config_path"]).read_bytes()
        config = _parse_canonical_json(config_raw, max_bytes=256 * 1024)
        _validate_ci_config(config)
        if (
            config["repository"] != repository
            or config["repository_id"] != repository_id
            or config["gateway_origin"] != expected_origin
            or config["object_repository"] != str(install_root / "source.git")
            or config["policy_path"] != str(install_root / "runtime/policy.json")
            or config["worker_manifest_path"] != str(install_root / "runtime/worker-manifest.json")
            or config["baseline_root"] != str(install_root / "runtime/baseline")
            or config["runtime_path"] != str(install_root / "runtime/runsc")
            or config["scanner_executable_path"] != str(install_root / "runtime/scanner")
            or config["scanner_configuration_path"] != str(install_root / "runtime/scanner-config")
            or config["state_directory"] != str(install_root / "state")
            or config["artifact_directory"] != str(install_root / "artifacts")
            or config["docker_path"] != "/usr/bin/docker"
            or config["runtime_name"] != "runsc"
            or config["image_reference"] != manifest["image_reference"]
        ):
            raise ValueError
        paths = {record["path"]: record for record in manifest["files"]}
        for key, path in (
            ("policy_sha256", manifest["policy_path"]),
            ("worker_manifest_sha256", manifest["worker_manifest_path"]),
            ("runtime_sha256", manifest["runsc_path"]),
            ("scanner_executable_sha256", manifest["scanner_path"]),
            ("scanner_configuration_sha256", manifest["scanner_config_path"]),
        ):
            if config[key] != paths[path]["sha256"]:
                raise ValueError
        worker_raw = (stage / manifest["worker_manifest_path"]).read_bytes()
        worker = _parse_canonical_json(worker_raw, max_bytes=4 * 1024 * 1024)
        if (
            worker.get("version") != 2
            or not isinstance(worker.get("policy_digest"), str)
            or not isinstance(worker.get("baseline_snapshot"), dict)
            or not isinstance(worker["baseline_snapshot"].get("files"), list)
            or worker["baseline_snapshot"].get("archive_digest")
            != paths[manifest["baseline_archive_path"]]["sha256"]
        ):
            raise ValueError
        baseline_files = worker["baseline_snapshot"]["files"]
        _validate_baseline_inventory(baseline_files)
        policy_raw = (stage / manifest["policy_path"]).read_bytes()
        policy = _parse_canonical_json(policy_raw, max_bytes=64 * 1024)
        if (
            policy.get("repository_id") != repository_id
            or policy.get("digest") != worker["policy_digest"]
        ):
            raise ValueError
        scanner = (stage / manifest["scanner_path"]).read_bytes()
        scanner_config = (stage / manifest["scanner_config_path"]).read_bytes()
        if (
            hashlib.sha256(scanner).hexdigest() != config["scanner_executable_sha256"]
            or hashlib.sha256(scanner_config).hexdigest() != config["scanner_configuration_sha256"]
        ):
            raise ValueError
        scanner_identity = hashlib.sha256(
            bytes.fromhex(config["scanner_executable_sha256"])
            + bytes.fromhex(config["scanner_configuration_sha256"])
        ).hexdigest()
        if config["publication_scanner_identity_sha256"] != scanner_identity:
            raise ValueError
        runtime_policy_raw = (stage / manifest["runtime_policy_path"]).read_bytes()
        runtime_policy = _parse_canonical_json(runtime_policy_raw, max_bytes=64 * 1024)
        if (
            set(runtime_policy)
            != {
                "version",
                "owner_policy_id",
                "runtime_name",
                "runtime_path",
                "runtime_sha256",
                "docker_path",
                "image_reference",
                "worker_manifest_sha256",
                "runner_python_binary",
                "resources",
                "network",
                "read_only_root",
                "capabilities",
            }
            or runtime_policy["version"] != 1
            or not isinstance(runtime_policy["owner_policy_id"], str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", runtime_policy["owner_policy_id"]
            )
            or runtime_policy["runtime_name"] != "runsc"
            or runtime_policy["runtime_path"] != config["runtime_path"]
            or runtime_policy["runtime_sha256"] != config["runtime_sha256"]
            or runtime_policy["docker_path"] != config["docker_path"]
            or runtime_policy["image_reference"] != config["image_reference"]
            or runtime_policy["worker_manifest_sha256"] != config["worker_manifest_sha256"]
            or runtime_policy["resources"] != config["resources"]
            or not isinstance(runtime_policy["runner_python_binary"], str)
            or not _safe_absolute_command(runtime_policy["runner_python_binary"])
            or runtime_policy["network"] != "none"
            or runtime_policy["read_only_root"] is not True
            or runtime_policy["capabilities"] != []
        ):
            raise ValueError
        wheels = [
            path for path in paths if path.startswith("runtime/wheels/") and path.endswith(".whl")
        ]
        if manifest["coordinator_wheel"] not in wheels:
            raise ValueError
        return {
            "config": config,
            "config_raw": config_raw,
            "worker_manifest": worker,
            "runtime_policy": runtime_policy,
        }
    except HostedBootstrapError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise HostedBootstrapError("bootstrap_invalid") from None


def _validate_ci_config(value: object) -> None:
    required = {
        "version",
        "gateway_origin",
        "repository",
        "repository_id",
        "owner_id",
        "object_repository",
        "policy_path",
        "policy_sha256",
        "worker_manifest_path",
        "worker_manifest_sha256",
        "baseline_root",
        "docker_path",
        "runtime_name",
        "runtime_path",
        "runtime_sha256",
        "image_reference",
        "resources",
        "scanner_executable_path",
        "scanner_executable_sha256",
        "scanner_configuration_path",
        "scanner_configuration_sha256",
        "publication_scanner_identity_sha256",
        "state_directory",
        "artifact_directory",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["version"] != 1
        or not _REPOSITORY.fullmatch(value["repository"])
        or type(value["repository_id"]) is not int
        or value["repository_id"] <= 0
        or type(value["owner_id"]) is not int
        or value["owner_id"] <= 0
        or value["runtime_name"] != "runsc"
        or value["docker_path"] != "/usr/bin/docker"
        or not _IMAGE.fullmatch(value["image_reference"])
        or not isinstance(value["resources"], dict)
        or set(value["resources"])
        != {
            "cpu_millis",
            "memory_bytes",
            "pids_limit",
            "timeout_seconds",
            "max_output_bytes",
            "workspace_bytes",
        }
        or any(type(number) is not int or number <= 0 for number in value["resources"].values())
    ):
        raise HostedBootstrapError()
    origin = value["gateway_origin"]
    parsed = urlsplit(origin) if isinstance(origin, str) else None
    if (
        parsed is None
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise HostedBootstrapError()
    for key in required & {
        "object_repository",
        "policy_path",
        "worker_manifest_path",
        "baseline_root",
        "docker_path",
        "runtime_path",
        "scanner_executable_path",
        "scanner_configuration_path",
        "state_directory",
        "artifact_directory",
    }:
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise HostedBootstrapError()
    for key in (
        "policy_sha256",
        "worker_manifest_sha256",
        "runtime_sha256",
        "scanner_executable_sha256",
        "scanner_configuration_sha256",
        "publication_scanner_identity_sha256",
    ):
        if not isinstance(value[key], str) or not _DIGEST.fullmatch(value[key]):
            raise HostedBootstrapError()


def _validate_baseline_inventory(files: object) -> None:
    if not isinstance(files, list) or not 1 <= len(files) <= 2048:
        raise HostedBootstrapError()
    seen: set[str] = set()
    total = 0
    for record in files:
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "size", "sha256", "mode"}
            or not isinstance(record["path"], str)
            or not _safe_relative_file(record["path"])
            or record["path"] in seen
            or type(record["size"]) is not int
            or not 0 <= record["size"] <= 8 * 1024 * 1024
            or not isinstance(record["sha256"], str)
            or not _DIGEST.fullmatch(record["sha256"])
            or type(record["mode"]) is not int
            or record["mode"] not in {0o644, 0o755}
            or _is_private_baseline_path(record["path"])
        ):
            raise HostedBootstrapError()
        seen.add(record["path"])
        total += record["size"]
        if total > _MAX_TOTAL:
            raise HostedBootstrapError()


def _verify_installed_inventory(root: Path, artifacts: object, baseline_files: object) -> None:
    if not isinstance(artifacts, list) or not isinstance(baseline_files, list):
        raise HostedBootstrapError("bootstrap_install_failed")
    for prefix, records in (("", artifacts), ("runtime/baseline/", baseline_files)):
        for record in records:
            relative = record.get("path")
            if prefix == "" and not isinstance(relative, str):
                raise HostedBootstrapError("bootstrap_install_failed")
            if not isinstance(relative, str):
                raise HostedBootstrapError("bootstrap_install_failed")
            path = root / prefix / relative
            try:
                info = path.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid not in {0, os.geteuid()}
                    or stat.S_IMODE(info.st_mode) != record["mode"]
                    or info.st_size != record["size"]
                ):
                    raise ValueError
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                descriptor = os.open(path, flags)
                digest = hashlib.sha256()
                try:
                    while chunk := os.read(descriptor, 64 * 1024):
                        digest.update(chunk)
                finally:
                    os.close(descriptor)
                if digest.hexdigest() != record["sha256"]:
                    raise ValueError
            except (OSError, TypeError, KeyError, ValueError):
                raise HostedBootstrapError("bootstrap_install_failed") from None


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        isinstance(value, str)
        and bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and "\0" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and path.as_posix() == value
    )


def _safe_relative_file(value: str) -> bool:
    return _safe_relative_path(value) and not value.endswith("/")


def _safe_absolute_command(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        value.startswith("/")
        and "\\" not in value
        and "\0" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/")[1:])
        and path.as_posix() == value
    )


def _is_private_baseline_path(value: str) -> bool:
    components = [part.casefold() for part in PurePosixPath(value).parts]
    return (
        any(part in {".git", ".codex", "secrets", "private", "credentials"} for part in components)
        or any(part.startswith(".env") for part in components)
        or any(part.endswith((".ini", ".pem", ".key", ".p12", ".pfx")) for part in components)
    )


def _parse_canonical_json(raw: bytes, *, max_bytes: int) -> dict[str, object]:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= max_bytes:
        raise HostedBootstrapError()
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
        if not isinstance(value, dict) or _canonical_json(value) != raw:
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise HostedBootstrapError() from None


def _parse_json(raw: bytes, *, max_bytes: int) -> object:
    if not isinstance(raw, bytes) or len(raw) > max_bytes:
        raise HostedBootstrapError()
    try:
        return json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise HostedBootstrapError() from None


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("constant")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _image_digests(raw: bytes) -> set[str]:
    value = _parse_json(raw, max_bytes=64 * 1024)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise HostedBootstrapError("bootstrap_runtime_unavailable")
    digests = value[0].get("RepoDigests")
    if not isinstance(digests, list) or any(
        not isinstance(item, str) or not _IMAGE.fullmatch(item) for item in digests
    ):
        raise HostedBootstrapError("bootstrap_runtime_unavailable")
    return set(digests)


def _read_json_if_present(path: Path) -> dict[str, object]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
        raise HostedBootstrapError("bootstrap_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        raw = os.read(descriptor, 64 * 1024 + 1)
    finally:
        os.close(descriptor)
    value = _parse_json(raw, max_bytes=64 * 1024)
    if not isinstance(value, dict):
        raise HostedBootstrapError("bootstrap_invalid")
    return value


def main(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    download: Download = _https_get,
    executor: CommandExecutor | None = None,
) -> int:
    args = [] if argv is None else list(argv)
    if args != ["bootstrap"]:
        raise SystemExit("invalid_input")
    source = os.environ if environment is None else environment
    try:
        bootstrapper = HostedBootstrapper(
            origin=source["RELEASE_CONTROL_BOOTSTRAP_ORIGIN"],
            manifest_sha256=source["RELEASE_CONTROL_BOOTSTRAP_MANIFEST_SHA256"],
            repository=source["GITHUB_REPOSITORY"],
            repository_id=int(source["GITHUB_REPOSITORY_ID"]),
            bearer_token=source["RELEASE_CONTROL_BOOTSTRAP_TOKEN"],
            install_root=_INSTALL_ROOT,
            download=download,
            executor=executor,
            github_environment_file=source.get("GITHUB_ENV"),
        )
        result = bootstrapper.run()
        sys.stdout.write(
            "bootstrap=installed "
            f"archive_sha256={result.archive_sha256} "
            f"files={len(result.installed_files)}\n"
        )
        return 0
    except HostedBootstrapError as exc:
        raise SystemExit(exc.code) from None
    except Exception:
        raise SystemExit("bootstrap_invalid") from None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
