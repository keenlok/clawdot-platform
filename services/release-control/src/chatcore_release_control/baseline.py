"""Build immutable baseline data artifacts; never execute source or build images."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .git_evidence import GitEvidenceError, _GitRunner
from .monorepo_harness import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    HarnessError,
    SnapshotFile,
    SnapshotManifest,
    _private_path,
    _relative,
)
from .policy import _is_private_path


class BaselineError(ValueError):
    def __init__(self) -> None:
        super().__init__("baseline_denied")


@dataclass(frozen=True)
class BaselineBundle:
    commit: str
    tree: str
    manifest: bytes = field(repr=False)
    source_tar: bytes = field(repr=False)

    @property
    def manifest_digest(self) -> str:
        return hashlib.sha256(self.manifest).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def build_baseline_bundle(
    repository: str | os.PathLike[str],
    commit: str,
    *,
    policy_digest: str,
    allowed_paths: tuple[str, ...],
    source_roots: tuple[str, ...],
    test_paths: tuple[str, ...],
    worker_source: bytes,
    harness_source: bytes,
    reviewed_source_exceptions: tuple[str, ...] = (),
) -> BaselineBundle:
    """Export raw Git blobs at B plus canonical worker/snapshot identity.

    Exception paths are maintenance-reviewed source scripts only; they cannot
    permit INI/environment data. An independent baseline content scan is still
    required before building/installing this artifact or granting credentials.
    """
    try:
        if (
            not isinstance(commit, str)
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit)
            or not isinstance(policy_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", policy_digest)
            or not isinstance(allowed_paths, tuple)
            or not 1 <= len(allowed_paths) <= 64
            or len(set(allowed_paths)) != len(allowed_paths)
            or any(not _relative(path) or _is_private_path(path) for path in allowed_paths)
            or not isinstance(worker_source, bytes)
            or not worker_source
            or not isinstance(harness_source, bytes)
            or not harness_source
            or not isinstance(reviewed_source_exceptions, tuple)
            or any(
                not _relative(path) or not path.endswith((".py", ".sh"))
                for path in reviewed_source_exceptions
            )
        ):
            raise BaselineError()
        executable = shutil.which("git")
        if not executable:
            raise BaselineError()
        repo = Path(repository).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="release-baseline-") as scratch:
            reader = _GitRunner(executable, repo, Path(scratch), len(commit))
            reader.commit(commit)
            tree = reader.tree(commit)
            raw = reader.run("ls-tree", "-r", "-z", "--full-tree", commit)
            if not raw.endswith(b"\0"):
                raise BaselineError()
            records = raw.split(b"\0")[:-1]
            if not 1 <= len(records) <= MAX_FILES:
                raise BaselineError()
            files = []
            content = {}
            total = 0
            for record in records:
                header, separator, name = record.partition(b"\t")
                fields = header.split()
                if not separator or len(fields) != 3:
                    raise BaselineError()
                mode, kind, oid = fields
                path = name.decode("utf-8", errors="strict")
                if (
                    not _relative(path)
                    or kind != b"blob"
                    or mode not in {b"100644", b"100755"}
                    or not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid)
                ):
                    raise BaselineError()
                if _private_path(path, reviewed_source_exceptions):
                    raise BaselineError()
                data = reader.run("cat-file", "blob", oid.decode(), output_limit=MAX_FILE_BYTES)
                # Initial releases accept inspectable source/dependency metadata only.
                data.decode("utf-8", errors="strict")
                if b"\0" in data:
                    raise BaselineError()
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise BaselineError()
                item = SnapshotFile(
                    path,
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                    0o755 if mode == b"100755" else 0o644,
                )
                files.append(item)
                content[path] = data
            files.sort(key=lambda item: item.path)
            lock = next((entry for entry in files if entry.path == "uv.lock"), None)
            if lock is None:
                raise BaselineError()
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w", format=tarfile.PAX_FORMAT) as output:
                for item in files:
                    info = tarfile.TarInfo(item.path)
                    info.size, info.mode, info.mtime = item.size, item.mode, 0
                    info.uid = info.gid = 65532
                    output.addfile(info, io.BytesIO(content[item.path]))
            result = archive.getvalue()
            if len(result) > MAX_TOTAL_BYTES + MAX_FILES * 2048:
                raise BaselineError()
            snapshot = {
                "tree": tree,
                "lock_digest": lock.sha256,
                "files": [asdict(item) for item in files],
                "source_roots": sorted(source_roots),
                "test_paths": sorted(test_paths),
                "archive_digest": hashlib.sha256(result).hexdigest(),
                "reviewed_source_exceptions": sorted(reviewed_source_exceptions),
            }
            SnapshotManifest.from_value(snapshot)
            manifest = _canonical(
                {
                    "version": 2,
                    "policy_digest": policy_digest,
                    "baseline_commit": commit,
                    "worker_source_digest": hashlib.sha256(worker_source).hexdigest(),
                    "test_harness_digest": hashlib.sha256(harness_source).hexdigest(),
                    "allowed_paths": sorted(allowed_paths),
                    "baseline_snapshot": snapshot,
                }
            )
            if len(manifest) > 4 * 1024 * 1024:
                raise BaselineError()
            return BaselineBundle(commit, tree, manifest, result)
    except (BaselineError, KeyboardInterrupt, SystemExit):
        raise
    except (GitEvidenceError, HarnessError, OSError, ValueError, TypeError, UnicodeError):
        raise BaselineError() from None


def worker_image_recipe(parent_image: str, manifest: bytes, *, dependency_cache_digest: str) -> str:
    """Generate a fixed offline recipe for separate operator-owned preparation.

    Required context: baseline.tar, manifest.json, fixed worker/harness source and
    dependency-cache.tar. Parent image must already contain audited Python and uv.
    The installed build service must independently verify every context digest.
    """
    try:
        if (
            not isinstance(parent_image, str)
            or not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}", parent_image)
            or not isinstance(manifest, bytes)
            or len(manifest) > 4 * 1024 * 1024
            or not isinstance(dependency_cache_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", dependency_cache_digest)
        ):
            raise BaselineError()
        value = json.loads(manifest)
        if value.get("version") != 2 or _canonical(value) != manifest:
            raise BaselineError()
        SnapshotManifest.from_value(value["baseline_snapshot"])
        policy, baseline = value["policy_digest"], value["baseline_commit"]
        if not re.fullmatch(r"[0-9a-f]{64}", policy) or not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", baseline
        ):
            raise BaselineError()
        digest = hashlib.sha256(manifest).hexdigest()
        prepare = (
            "RUN --network=none /usr/bin/python3 -I "
            "/opt/chatcore-release/monorepo_harness.py --prepare-cache " + dependency_cache_digest
        )
        prepare_baseline = (
            "RUN --network=none /usr/bin/python3 -I "
            "/opt/chatcore-release/monorepo_harness.py --prepare-baseline " + digest
        )
        install = (
            "RUN --network=none /usr/bin/env -i PATH=/usr/bin:/bin "
            "UV_PROJECT_ENVIRONMENT=/opt/chatcore-release/venv "
            "UV_CACHE_DIR=/opt/chatcore-release/cache /opt/uv/bin/uv sync "
            "--offline --frozen --all-packages --no-editable"
        )
        labels = (
            f'LABEL org.chatcore.release-control.worker-manifest-digest="{digest}" '
            f'org.chatcore.release-control.policy-digest="{policy}" '
            f'org.chatcore.release-control.baseline-commit="{baseline}"'
        )
        return f"""FROM {parent_image}
COPY --chmod=0600 baseline.tar /opt/chatcore-release/baseline.tar
COPY --chmod=0600 dependency-cache.tar /opt/chatcore-release/dependency-cache.tar
COPY manifest.json development_worker.py monorepo_harness.py /opt/chatcore-release/
{prepare_baseline}
{prepare}
WORKDIR /opt/chatcore-release/baseline
{install}
{labels}
USER 65532:65532
"""
    except BaselineError:
        raise
    except (ValueError, TypeError, KeyError, HarnessError):
        raise BaselineError() from None
