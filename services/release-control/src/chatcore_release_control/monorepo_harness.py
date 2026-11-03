"""Fixed image-installed baseline staging and pytest entrypoint.

All candidate execution is confined to the image/container. Helpers in this file
can validate/copy synthetic data in unit tests; host tests must never call main().
Candidate stdout and process success are observations, not trusted test counts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_FILES = 10000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class HarnessError(ValueError):
    def __init__(self) -> None:
        super().__init__("baseline_harness_denied")


def _relative(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 1024
        and not any(char in value for char in ("\\", "\0", ":"))
        and all(ord(char) >= 32 for char in value)
        and all(
            part not in {"", ".", ".."} and not part.startswith("-") and part.casefold() != ".git"
            for part in value.split("/")
        )
    )


def _private_path(path: str, reviewed_scripts: tuple[str, ...] = ()) -> bool:
    for part in path.split("/"):
        name = part.casefold()
        if (
            name.endswith(
                (".ini", ".pem", ".key", ".p12", ".pfx", ".db", ".dump", ".sqlite", ".sqlite3")
            )
            or name.startswith(".env")
            and part != ".env.example"
            or name in {"secrets", "credentials", "id_rsa", "id_ed25519"}
            or name.startswith(("secrets.", "credentials."))
            or name.endswith(("~", ".bak", ".backup", ".old", ".orig", ".swp", ".swo"))
        ):
            return True
        if "backup" in name and not (path in reviewed_scripts and path.endswith((".py", ".sh"))):
            return True
    return False


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    size: int
    sha256: str
    mode: int


@dataclass(frozen=True)
class SnapshotManifest:
    tree: str
    lock_digest: str
    files: tuple[SnapshotFile, ...]
    source_roots: tuple[str, ...]
    test_paths: tuple[str, ...]
    archive_digest: str
    reviewed_source_exceptions: tuple[str, ...]

    @classmethod
    def from_value(cls, value: object) -> SnapshotManifest:
        if not isinstance(value, dict) or set(value) != {
            "tree",
            "lock_digest",
            "files",
            "source_roots",
            "test_paths",
            "archive_digest",
            "reviewed_source_exceptions",
        }:
            raise HarnessError()
        if (
            not isinstance(value["tree"], str)
            or not _OID.fullmatch(value["tree"])
            or not isinstance(value["lock_digest"], str)
            or not _DIGEST.fullmatch(value["lock_digest"])
            or not isinstance(value["archive_digest"], str)
            or not _DIGEST.fullmatch(value["archive_digest"])
            or not isinstance(value["files"], list)
            or not 1 <= len(value["files"]) <= MAX_FILES
        ):
            raise HarnessError()
        reviewed = value["reviewed_source_exceptions"]
        if (
            not isinstance(reviewed, list)
            or len(reviewed) > 64
            or any(not _relative(path) or not path.endswith((".py", ".sh")) for path in reviewed)
            or reviewed != sorted(set(reviewed))
        ):
            raise HarnessError()
        entries = []
        total = 0
        for item in value["files"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "size", "sha256", "mode"}
                or not _relative(item["path"])
                or _private_path(item["path"], tuple(reviewed))
                or type(item["size"]) is not int
                or not 0 <= item["size"] <= MAX_FILE_BYTES
                or not isinstance(item["sha256"], str)
                or not _DIGEST.fullmatch(item["sha256"])
                or type(item["mode"]) is not int
                or item["mode"] not in {0o644, 0o755}
            ):
                raise HarnessError()
            total += item["size"]
            if total > MAX_TOTAL_BYTES:
                raise HarnessError()
            entries.append(SnapshotFile(**item))
        paths = [item.path for item in entries]
        if paths != sorted(set(paths)):
            raise HarnessError()
        if not set(reviewed) <= set(paths):
            raise HarnessError()
        path_set = set(paths)
        for path in paths:
            parts = path.split("/")
            if any("/".join(parts[:index]) in path_set for index in range(1, len(parts))):
                raise HarnessError()
        lock = next((item for item in entries if item.path == "uv.lock"), None)
        if lock is None or lock.sha256 != value["lock_digest"]:
            raise HarnessError()
        groups = []
        for name in ("source_roots", "test_paths"):
            items = value[name]
            if (
                not isinstance(items, list)
                or not 1 <= len(items) <= 128
                or any(not _relative(path) for path in items)
                or items != sorted(set(items))
                or any(
                    not any(file == path or file.startswith(path + "/") for file in paths)
                    for path in items
                )
            ):
                raise HarnessError()
            groups.append(tuple(items))
        return cls(
            value["tree"],
            value["lock_digest"],
            tuple(entries),
            *groups,
            value["archive_digest"],
            tuple(reviewed),
        )


def _read_regular(root: Path, entry: SnapshotFile) -> bytes:
    path = root
    if root.is_symlink() or not root.is_dir():
        raise HarnessError()
    try:
        for part in entry.path.split("/")[:-1]:
            path /= part
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise HarnessError()
        path /= entry.path.split("/")[-1]
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size != entry.size
                or stat.S_IMODE(info.st_mode) != entry.mode
            ):
                raise HarnessError()
            data = bytearray()
            while chunk := os.read(descriptor, min(65536, entry.size - len(data) + 1)):
                data.extend(chunk)
                if len(data) > entry.size:
                    raise HarnessError()
        finally:
            os.close(descriptor)
    except OSError:
        raise HarnessError() from None
    if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
        raise HarnessError()
    return bytes(data)


def _write(root: Path, relative: str, content: bytes, mode: int, *, replace: bool) -> None:
    if not _relative(relative) or root.is_symlink() or not root.is_dir():
        raise HarnessError()
    parent = root
    try:
        for part in relative.split("/")[:-1]:
            parent /= part
            parent.mkdir(mode=0o700, exist_ok=True)
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise HarnessError()
        path = parent / relative.split("/")[-1]
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
        flags |= os.O_TRUNC if replace else os.O_EXCL
        fd = os.open(path, flags, mode)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise HarnessError()
            view = memoryview(content)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise HarnessError()
                view = view[count:]
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
    except OSError:
        raise HarnessError() from None


def prepare_baseline(
    snapshot: SnapshotManifest, installed_root: Path, candidate_root: Path
) -> None:
    if not isinstance(snapshot, SnapshotManifest):
        raise HarnessError()
    try:
        actual = set()
        expected_directories = {
            "/".join(entry.path.split("/")[:index])
            for entry in snapshot.files
            for index in range(1, len(entry.path.split("/")))
        }
        if installed_root.is_symlink() or not installed_root.is_dir():
            raise HarnessError()
        for directory, directories, files in os.walk(installed_root, followlinks=False):
            for name in directories:
                child = Path(directory) / name
                if (
                    not stat.S_ISDIR(child.lstat().st_mode)
                    or child.relative_to(installed_root).as_posix() not in expected_directories
                ):
                    raise HarnessError()
            for name in files:
                path = Path(directory) / name
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise HarnessError()
                actual.add(path.relative_to(installed_root).as_posix())
                if len(actual) > MAX_FILES:
                    raise HarnessError()
        if actual != {entry.path for entry in snapshot.files}:
            raise HarnessError()
        candidate_root.mkdir(mode=0o700, parents=False, exist_ok=False)
        for entry in snapshot.files:
            data = _read_regular(installed_root, entry)
            _write(candidate_root, entry.path, data, entry.mode, replace=False)
    except OSError:
        raise HarnessError() from None


def overlay_files(root: Path, files: dict[str, str], allowed_paths: tuple[str, ...]) -> None:
    if (
        not isinstance(files, dict)
        or not 1 <= len(files) <= 64
        or set(files) - set(allowed_paths)
        or any(not _relative(path) for path in files)
    ):
        raise HarnessError()
    total = 0
    for path, content in sorted(files.items()):
        if not isinstance(content, str) or "\0" in content:
            raise HarnessError()
        data = content.encode("utf-8", errors="strict")
        total += len(data)
        if not 1 <= len(data) <= 32768 or total > 128 * 1024:
            raise HarnessError()
        existing = root / path
        mode = stat.S_IMODE(existing.lstat().st_mode) if existing.exists() else 0o644
        if mode not in {0o644, 0o755}:
            raise HarnessError()
        _write(root, path, data, mode, replace=True)


def pytest_arguments(snapshot: SnapshotManifest) -> tuple[str, ...]:
    return (
        "--import-mode=importlib",
        "-q",
        "-p",
        "pytest_asyncio.plugin",
        "-m",
        "not integration and not browser and not sandbox_integration",
        *snapshot.test_paths,
    )


def install_baseline_archive(archive_path: Path, snapshot: SnapshotManifest, target: Path) -> None:
    """Freeze and verify every source entry before writing an installed baseline."""
    if not isinstance(snapshot, SnapshotManifest):
        raise HarnessError()
    maximum = MAX_TOTAL_BYTES + MAX_FILES * 2048
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
    try:
        fd = os.open(archive_path, flags)
        with os.fdopen(fd, "rb") as source, tempfile.TemporaryFile() as frozen:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise HarnessError()
            digest = hashlib.sha256()
            size = 0
            while chunk := source.read(65536):
                size += len(chunk)
                if size > maximum:
                    raise HarnessError()
                digest.update(chunk)
                frozen.write(chunk)
            if digest.hexdigest() != snapshot.archive_digest:
                raise HarnessError()
            frozen.seek(0)
            expected = {entry.path: entry for entry in snapshot.files}
            names = set()
            with tarfile.open(fileobj=frozen, mode="r:") as archive:
                for member in archive:
                    entry = expected.get(member.name)
                    if (
                        entry is None
                        or member.name in names
                        or not member.isfile()
                        or member.sparse is not None
                        or member.size != entry.size
                        or member.mode != entry.mode
                    ):
                        raise HarnessError()
                    names.add(member.name)
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise HarnessError()
                    data = stream.read(entry.size + 1)
                    if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                        raise HarnessError()
                if names != set(expected):
                    raise HarnessError()
                target.mkdir(mode=0o700, parents=False, exist_ok=False)
                for member in archive.getmembers():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise HarnessError()
                    data = stream.read(member.size + 1)
                    entry = expected[member.name]
                    _write(target, member.name, data, entry.mode, replace=False)
            # The baseline is root-owned at image build time, but readable by
            # the unprivileged worker. Runtime staging verifies every file again.
            for directory, _, _ in os.walk(target, topdown=False):
                Path(directory).chmod(0o555)
    except (OSError, ValueError, tarfile.TarError):
        raise HarnessError() from None


def prepare_dependency_cache(archive_path: Path, expected_digest: str, target: Path) -> None:
    """Validate a copied, immutable, uncompressed cache archive before extraction.

    This is a separate offline image-preparation operation. uv.lock still checks
    dependency artifacts during installation; no candidate process calls this.
    """
    if not isinstance(expected_digest, str) or not _DIGEST.fullmatch(expected_digest):
        raise HarnessError()
    maximum = 1024 * 1024 * 1024
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
    try:
        fd = os.open(archive_path, flags)
        with os.fdopen(fd, "rb") as source, tempfile.TemporaryFile() as frozen:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise HarnessError()
            digest = hashlib.sha256()
            size = 0
            while chunk := source.read(65536):
                size += len(chunk)
                if size > maximum:
                    raise HarnessError()
                digest.update(chunk)
                frozen.write(chunk)
            if digest.hexdigest() != expected_digest:
                raise HarnessError()
            frozen.seek(0)
            with tarfile.open(fileobj=frozen, mode="r:") as archive:
                entries = []
                names = set()
                total = 0
                for member in archive:
                    if (
                        not _relative(member.name)
                        or member.name in names
                        or not (member.isfile() or member.isdir())
                        or member.sparse is not None
                    ):
                        raise HarnessError()
                    names.add(member.name)
                    entries.append(member)
                    total += member.size
                    if len(entries) > 100000 or not 0 <= member.size <= 128 * 1024 * 1024:
                        raise HarnessError()
                    if total > maximum:
                        raise HarnessError()
                target.mkdir(mode=0o700, parents=False, exist_ok=False)
                for member in entries:
                    if member.isdir():
                        continue
                    source_file = archive.extractfile(member)
                    if source_file is None:
                        raise HarnessError()
                    data = source_file.read(member.size + 1)
                    if len(data) != member.size:
                        raise HarnessError()
                    mode = 0o755 if member.mode & 0o111 else 0o644
                    _write(target, member.name, data, mode, replace=False)
    except (OSError, ValueError, tarfile.TarError):
        raise HarnessError() from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HarnessError()
        result[key] = value
    return result


def main() -> int:
    # Fixed arguments and interpreter are installed control inputs, never request data.
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--prepare-cache":
            prepare_dependency_cache(
                Path("/opt/chatcore-release/dependency-cache.tar"),
                sys.argv[2],
                Path("/opt/chatcore-release/cache"),
            )
            return 0
        if len(sys.argv) == 3 and sys.argv[1] == "--prepare-baseline":
            raw = Path("/opt/chatcore-release/manifest.json").read_bytes()
            if len(raw) > MAX_MANIFEST_BYTES or hashlib.sha256(raw).hexdigest() != sys.argv[2]:
                raise HarnessError()
            manifest = json.loads(raw, object_pairs_hook=_unique)
            if (
                manifest.get("version") != 2
                or hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                != manifest["test_harness_digest"]
            ):
                raise HarnessError()
            install_baseline_archive(
                Path("/opt/chatcore-release/baseline.tar"),
                SnapshotManifest.from_value(manifest["baseline_snapshot"]),
                Path("/opt/chatcore-release/baseline"),
            )
            return 0
        if (
            sys.prefix != "/opt/chatcore-release/venv"
            or len(sys.argv) != 3
            or sys.argv[1] != "--manifest-digest"
            or not _DIGEST.fullmatch(sys.argv[2])
        ):
            raise HarnessError()
        raw = Path("/opt/chatcore-release/manifest.json").read_bytes()
        if len(raw) > MAX_MANIFEST_BYTES or hashlib.sha256(raw).hexdigest() != sys.argv[2]:
            raise HarnessError()
        manifest = json.loads(raw, object_pairs_hook=_unique)
        if (
            manifest.get("version") != 2
            or hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            != manifest["test_harness_digest"]
        ):
            raise HarnessError()
        request_raw = Path("/workspace/request.json").read_bytes()
        if len(request_raw) > 4 * 1024 * 1024:
            raise HarnessError()
        request = json.loads(request_raw, object_pairs_hook=_unique)
        if (
            request["base_commit"] != manifest["baseline_commit"]
            or request["policy_digest"] != manifest["policy_digest"]
            or request["worker_manifest_digest"] != sys.argv[2]
        ):
            raise HarnessError()
        snapshot = SnapshotManifest.from_value(manifest["baseline_snapshot"])
        candidate = Path("/workspace/candidate")
        prepare_baseline(snapshot, Path("/opt/chatcore-release/baseline"), candidate)
        overlay_files(candidate, request["files"], tuple(manifest["allowed_paths"]))
        # Import the pinned installed pytest before adding candidate source paths.
        import pytest

        for relative in reversed(snapshot.source_roots):
            sys.path.insert(0, str(candidate / relative))
        os.chdir(candidate)
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        return int(pytest.main(list(pytest_arguments(snapshot))))
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
