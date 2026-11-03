"""Bounded offline initial-history audit and protected baseline receipt preparation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .bridge_service import _secure_parent
from .git_evidence import MAX_HISTORY_COMMITS, _GitRunner
from .monorepo_harness import MAX_FILES, _private_path, _relative
from .policy import PolicyManifest
from .publication import MAX_SCAN_ITEM_BYTES, MAX_TOTAL_SCAN_BYTES, ScannerBinaryAdapter, _ScanState
from .publication_service import ProtectedPublicationConfig, _read_protected_file


class BaselineAuditError(ValueError):
    def __init__(self):
        super().__init__("baseline_audit_denied")


def audit_baseline_history(
    repository,
    commit: str,
    policy: PolicyManifest,
    *,
    scanner,
    scanner_identity_sha256: str,
    reviewed_source_exceptions: tuple[str, ...] = (),
) -> dict:
    try:
        if (
            not isinstance(commit, str)
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is None
            or not isinstance(policy, PolicyManifest)
            or not callable(scanner)
            or not isinstance(scanner_identity_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", scanner_identity_sha256) is None
            or getattr(scanner, "identity_sha256", scanner_identity_sha256)
            != scanner_identity_sha256
            or not isinstance(reviewed_source_exceptions, tuple)
            or len(reviewed_source_exceptions) > 64
            or any(
                not _relative(path) or not path.endswith((".py", ".sh"))
                for path in reviewed_source_exceptions
            )
        ):
            raise BaselineAuditError()
        executable = shutil.which("git")
        if not executable:
            raise BaselineAuditError()
        with tempfile.TemporaryDirectory(prefix="baseline-audit-") as temporary:
            reader = _GitRunner(
                executable, Path(repository).resolve(strict=True), Path(temporary), len(commit)
            )
            reader.commit(commit)
            commits = reader.run(
                "rev-list",
                "--topo-order",
                "--reverse",
                f"--max-count={MAX_HISTORY_COMMITS + 1}",
                commit,
            ).splitlines()
            if not 1 <= len(commits) <= MAX_HISTORY_COMMITS or any(
                re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None for value in commits
            ):
                raise BaselineAuditError()
            blobs = set()
            paths = set()
            # Validate the entire path inventory before reading ANY source blob.
            for item in commits:
                listing = reader.run("ls-tree", "-r", "-z", "--full-tree", item.decode("ascii"))
                if listing and not listing.endswith(b"\0"):
                    raise BaselineAuditError()
                entries = listing.split(b"\0")[:-1]
                if len(entries) > MAX_FILES:
                    raise BaselineAuditError()
                for entry in entries:
                    raw, separator, name = entry.partition(b"\t")
                    fields = raw.split()
                    path = name.decode("utf-8", errors="strict")
                    if (
                        not separator
                        or len(fields) != 3
                        or not _relative(path)
                        or _private_path(path, reviewed_source_exceptions)
                        or fields[0] not in {b"100644", b"100755"}
                        or fields[1] != b"blob"
                        or re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[2]) is None
                    ):
                        raise BaselineAuditError()
                    blobs.add(fields[2])
                    paths.add(path)
                    if len(blobs) > 100_000 or len(paths) > 100_000:
                        raise BaselineAuditError()
            if not set(reviewed_source_exceptions) <= paths:
                raise BaselineAuditError()
            state = _ScanState(scanner, MAX_SCAN_ITEM_BYTES, MAX_TOTAL_SCAN_BYTES, time.monotonic())
            for item in commits:
                oid = item.decode("ascii")
                state.scan_text(
                    f"baseline-commit:{oid}",
                    reader.run("cat-file", "commit", oid, output_limit=MAX_SCAN_ITEM_BYTES),
                )
            for path in sorted(paths):
                state.scan_text("baseline-path", path.encode())
            for item in sorted(blobs):
                oid = item.decode("ascii")
                state.scan_text(
                    f"baseline-blob:{oid}",
                    reader.run("cat-file", "blob", oid, output_limit=MAX_SCAN_ITEM_BYTES),
                )
            return {
                "version": 1,
                "repository_id": policy.repository_id,
                "baseline_commit": commit,
                "policy_digest": policy.digest,
                "scanner_identity_sha256": scanner_identity_sha256,
                "history_complete": True,
                "scanned_commit_count": len(commits),
                "scanned_bytes": state.total_bytes,
            }
    except Exception:
        raise BaselineAuditError() from None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="chatcore-baseline-audit")
    parser.add_argument("--publication-config", required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--reviewed-source-script", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise BaselineAuditError()
        config = ProtectedPublicationConfig.from_environment(
            {
                "RELEASE_CONTROL_PUBLICATION_CONFIG_PATH": args.publication_config,
                "RELEASE_CONTROL_PUBLICATION_CONFIG_SHA256": args.config_sha256,
                "RELEASE_CONTROL_PUBLICATION_MODE": "gateway",
            }
        )
        value = config.values
        raw = _read_protected_file(Path(value["policy_path"]), max_bytes=64 * 1024)
        if hashlib.sha256(raw).hexdigest() != value["policy_sha256"]:
            raise BaselineAuditError()
        policy = PolicyManifest.from_json(raw)
        scanner = ScannerBinaryAdapter(
            value["scanner_executable_path"],
            value["scanner_configuration_path"],
            expected_executable_sha256=value["scanner_executable_sha256"],
            expected_configuration_sha256=value["scanner_configuration_sha256"],
        )
        result = audit_baseline_history(
            value["source_repository"],
            value["baseline_commit"],
            policy,
            scanner=scanner,
            scanner_identity_sha256=value["publication_scanner_identity_sha256"],
            reviewed_source_exceptions=tuple(args.reviewed_source_script),
        )
        output = Path(args.output)
        _secure_parent(str(output), 0)
        raw = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            parent = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        print("baseline receipt written")
        return 0
    except Exception:
        print("baseline_audit_denied")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
