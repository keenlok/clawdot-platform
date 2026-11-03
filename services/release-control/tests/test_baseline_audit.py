from __future__ import annotations

import sys
from pathlib import Path

import pytest
from chatcore_release_control.baseline_audit import BaselineAuditError, audit_baseline_history
from chatcore_release_control.publication import ScanVerdict

sys.path.insert(0, str(Path(__file__).parent))
from test_baseline import fixture_repo  # noqa: E402
from test_publication import _policy  # noqa: E402


def test_baseline_audit_scans_all_ancestor_metadata_and_removed_blobs(tmp_path):
    repo, git = fixture_repo(tmp_path)
    (repo / "removed.py").write_text("historical synthetic value\n")
    git("add", ".")
    git("commit", "-qm", "historical commit message")
    (repo / "removed.py").unlink()
    git("add", ".")
    git("commit", "-qm", "remove historical source")
    seen = []
    result = audit_baseline_history(
        repo,
        git("rev-parse", "HEAD"),
        _policy(),
        scanner=lambda item: seen.append(item) or ScanVerdict.CLEAN,
        scanner_identity_sha256="a" * 64,
    )
    assert result["history_complete"] is True and result["scanned_commit_count"] == 3
    assert any(b"historical synthetic value" in item.content for item in seen)
    assert any(b"historical commit message" in item.content for item in seen)


def test_baseline_secret_in_deleted_history_prevents_receipt(tmp_path):
    repo, git = fixture_repo(tmp_path)
    path = repo / "historical.py"
    path.write_text("SYNTHETIC_SENTINEL\n")
    git("add", ".")
    git("commit", "-qm", "historical source")
    path.unlink()
    git("add", ".")
    git("commit", "-qm", "remove source")

    def scanner(item):
        return ScanVerdict.FINDING if b"SYNTHETIC_SENTINEL" in item.content else ScanVerdict.CLEAN

    with pytest.raises(BaselineAuditError):
        audit_baseline_history(
            repo,
            git("rev-parse", "HEAD"),
            _policy(),
            scanner=scanner,
            scanner_identity_sha256="a" * 64,
        )


def test_baseline_private_path_is_rejected_before_any_blob_scan_even_if_removed(tmp_path):
    repo, git = fixture_repo(tmp_path)
    path = repo / "private.ini"
    path.write_text("synthetic forbidden data\n")
    git("add", ".")
    git("commit", "-qm", "historical forbidden fixture")
    path.unlink()
    git("add", ".")
    git("commit", "-qm", "remove forbidden fixture")
    seen = []
    with pytest.raises(BaselineAuditError):
        audit_baseline_history(
            repo,
            git("rev-parse", "HEAD"),
            _policy(),
            scanner=lambda item: seen.append(item) or ScanVerdict.CLEAN,
            scanner_identity_sha256="a" * 64,
        )
    assert seen == []
