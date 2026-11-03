from __future__ import annotations

import hashlib
import hmac
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from chatcore_extensions.models import canonical_json

RUNSC_PACKAGE_FILES = frozenset(
    {
        "runsc",
        "containerd-shim-runsc-v1",
        "gvisor-bin/checkpointgofer",
        "gvisor-bin/gvisor-proc-umounter",
        "gvisor-bin/gvisor-sentry-prewarmer",
        "gvisor-bin/gvisor_sentry",
        "gvisor-bin/runsc-fd-parking",
        "gvisor-bin/runsc-metric-server",
    }
)


@dataclass(frozen=True)
class RuntimeIdentity:
    runtime: str
    version: str
    manifest_digest: str
    files: dict[str, str]


def runc_identity() -> RuntimeIdentity:
    return RuntimeIdentity("runc", "operator-default", "builtin:runc-v1", {})


def verify_runsc_manifest(manifest_path: str | Path) -> RuntimeIdentity:
    """Verify a root-owned immutable runsc package before the worker accepts jobs."""
    return _verify_runsc_manifest(manifest_path, owner_uid=0, validate_ancestors=True)


def _verify_runsc_manifest(
    manifest_path: str | Path, *, owner_uid: int, validate_ancestors: bool = False
) -> RuntimeIdentity:
    try:
        path = Path(manifest_path)
        if path.is_symlink() or not path.is_file():
            raise ValueError
        root = path.parent
        if root.is_symlink():
            raise ValueError
        if validate_ancestors:
            for ancestor in (root, *root.parents):
                _require_private_owner(ancestor, owner_uid)
        else:
            _require_private_owner(root, owner_uid)
        _require_private_owner(path, owner_uid)
        raw = path.read_bytes()
        if len(raw) > 16 * 1024:
            raise ValueError
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        if not isinstance(data, dict) or set(data) != {
            "schema_version",
            "release",
            "archive_sha512",
            "files",
        }:
            raise ValueError
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError
        release = data["release"]
        archive_digest = data["archive_sha512"]
        files = data["files"]
        if (
            not isinstance(release, str)
            or not re.fullmatch(r"[0-9]{8}\.[0-9]+", release)
            or not isinstance(archive_digest, str)
            or not re.fullmatch(r"[0-9a-f]{128}", archive_digest)
            or not isinstance(files, dict)
            or set(files) != RUNSC_PACKAGE_FILES
        ):
            raise ValueError
        if canonical_json(data) != raw:
            raise ValueError
        root_resolved = root.resolve(strict=True)
        for relative, expected in files.items():
            if (
                not isinstance(relative, str)
                or not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
            ):
                raise ValueError
            pure = PurePosixPath(relative)
            if (
                pure.is_absolute()
                or not pure.parts
                or any(part in {"", ".", ".."} for part in pure.parts)
            ):
                raise ValueError
            target = root.joinpath(*pure.parts)
            current = root
            for part in pure.parts:
                current = current / part
                metadata = current.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISDIR(metadata.st_mode)
                    and part != pure.parts[-1]
                ):
                    raise ValueError
                _require_private_owner(current, owner_uid)
            if not target.is_file():
                raise ValueError
            if not target.stat().st_mode & stat.S_IXUSR:
                raise ValueError
            if (
                target.resolve(strict=True).parent != root_resolved
                and root_resolved not in target.resolve(strict=True).parents
            ):
                raise ValueError
            digest = _sha256_file(target)
            if not hmac.compare_digest(digest, expected):
                raise ValueError
        return RuntimeIdentity("runsc", release, hashlib.sha256(raw).hexdigest(), dict(files))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        raise ValueError("invalid runtime manifest") from None


def _require_private_owner(path: Path, owner_uid: int) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ValueError


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError
        result[key] = value
    return result
