from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from chatcore_extensions.runtime import _verify_runsc_manifest, verify_runsc_manifest
from chatcore_extensions.settings import ExtensionsSettings
from pydantic import ValidationError


@pytest.mark.parametrize("value", [True, "true"])
def test_extensions_enabled_accepts_canonical_true(value):
    assert (
        ExtensionsSettings(
            enabled=value, worker_socket="/tmp/worker.sock", worker_token_file="/tmp/token"
        ).enabled
        is True
    )


@pytest.mark.parametrize("value", [False, "false"])
def test_extensions_enabled_accepts_canonical_false(value):
    assert ExtensionsSettings(enabled=value).enabled is False


@pytest.mark.parametrize("value", ["True", "TRUE", "1", "yes", " false", "false "])
def test_extensions_enabled_rejects_noncanonical_values(value):
    with pytest.raises(ValidationError):
        ExtensionsSettings(enabled=value)


def test_extensions_settings_ignore_lowercase_environment(monkeypatch):
    monkeypatch.setenv("extensions_enabled", "true")
    assert ExtensionsSettings(_env_file=None).enabled is False


def test_extensions_settings_reads_canonical_uppercase_environment(monkeypatch):
    monkeypatch.setenv("EXTENSIONS_ENABLED", "true")
    monkeypatch.setenv("EXTENSIONS_WORKER_SOCKET", "/run/sb-extensions/worker.sock")
    monkeypatch.setenv("EXTENSIONS_WORKER_TOKEN_FILE", "/run/sb-extensions/token")
    monkeypatch.setenv("EXTENSIONS_RUNTIME", "runsc")
    monkeypatch.setenv("EXTENSIONS_RUNTIME_MANIFEST_DIGEST", "a" * 64)
    settings = ExtensionsSettings(_env_file=None)
    assert settings.enabled is True
    assert settings.runtime == "runsc"
    assert settings.runtime_manifest_digest == "a" * 64


def _install_manifest(directory: Path) -> tuple[Path, str]:
    files = {
        "runsc": b"trusted runsc",
        "containerd-shim-runsc-v1": b"trusted shim",
        "gvisor-bin/checkpointgofer": b"checkpointgofer",
        "gvisor-bin/gvisor-proc-umounter": b"proc-umounter",
        "gvisor-bin/gvisor-sentry-prewarmer": b"prewarmer",
        "gvisor-bin/gvisor_sentry": b"sentry",
        "gvisor-bin/runsc-fd-parking": b"fd-parking",
        "gvisor-bin/runsc-metric-server": b"metric-server",
    }
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o500)
    manifest = {
        "schema_version": 1,
        "release": "20261026.0",
        "archive_sha512": "a" * 128,
        "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
    }
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_path = directory / "runtime-manifest.json"
    manifest_path.write_bytes(raw)
    manifest_path.chmod(0o400)
    return manifest_path, hashlib.sha256(raw).hexdigest()


def test_verifies_canonical_manifest_and_all_sidecar_digests(tmp_path: Path) -> None:
    manifest, expected_digest = _install_manifest(tmp_path)

    identity = _verify_runsc_manifest(manifest, owner_uid=os.getuid())

    assert identity.runtime == "runsc"
    assert identity.version == "20261026.0"
    assert identity.manifest_digest == expected_digest
    assert identity.files["runsc"] == hashlib.sha256(b"trusted runsc").hexdigest()


@pytest.mark.parametrize("unsafe", ["modified", "symlink", "writable", "traversal"])
def test_rejects_untrusted_runsc_manifest_inputs(tmp_path: Path, unsafe: str) -> None:
    manifest, _ = _install_manifest(tmp_path)
    if unsafe == "modified":
        (tmp_path / "runsc").chmod(0o600)
        (tmp_path / "runsc").write_bytes(b"tampered")
    elif unsafe == "symlink":
        target = tmp_path / "runsc"
        target.unlink()
        target.symlink_to(tmp_path / "real-runsc")
        (tmp_path / "real-runsc").write_bytes(b"trusted runsc")
    elif unsafe == "writable":
        (tmp_path / "runsc").chmod(0o620)
    else:
        data = json.loads(manifest.read_bytes())
        data["files"]["../outside"] = "b" * 64
        manifest.chmod(0o600)
        manifest.write_bytes(json.dumps(data, sort_keys=True, separators=(",", ":")).encode())

    with pytest.raises(ValueError, match="runtime manifest"):
        _verify_runsc_manifest(manifest, owner_uid=os.getuid())


def test_requires_root_owned_package_for_worker_startup(tmp_path: Path) -> None:
    manifest, _ = _install_manifest(tmp_path)
    if os.getuid() != 0:
        with pytest.raises(ValueError, match="runtime manifest"):
            verify_runsc_manifest(manifest)
    else:
        assert verify_runsc_manifest(manifest).runtime == "runsc"


def test_rejects_group_or_world_writable_manifest_parent(tmp_path: Path) -> None:
    manifest, _ = _install_manifest(tmp_path)
    tmp_path.chmod(stat.S_IMODE(tmp_path.stat().st_mode) | stat.S_IWGRP)

    with pytest.raises(ValueError, match="runtime manifest"):
        _verify_runsc_manifest(manifest, owner_uid=os.getuid())
