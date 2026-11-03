from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest
from chatcore_release_control.git_evidence import (
    GitEvidenceError,
    _GitRunner,
    collect_delta_patch,
    collect_delta_stats,
    collect_release_bundle,
    verify_installed_blob_digests,
)
from chatcore_release_control.policy import PolicyManifest, evaluate_ordinary_release

DIGEST = "a" * 64


def _policy(protected: tuple[str, ...] = ()) -> PolicyManifest:
    return PolicyManifest.from_json(
        json.dumps(
            {
                "version": 1,
                "repository_id": 11,
                "owner_id": 22,
                "target": "production",
                "workflow_digests": {".github/workflows/ci.yml": DIGEST},
                "allowed_paths": ["src/plugin.py"],
                "protected_paths": list(dict.fromkeys((".github/workflows/ci.yml", *protected))),
                "checks": [
                    {
                        "name": "candidate-tests",
                        "creator_app_id": 33,
                        "workflow_path": ".github/workflows/ci.yml",
                        "workflow_sha": DIGEST,
                    }
                ],
            }
        )
    )


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    process_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    if env:
        process_env.update(env)
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=process_env,
    )
    return result.stdout.decode().strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Synthetic Test")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    return repo


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _write(repo: Path, relative: str, data: str = "safe\n") -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def test_collects_immutable_d_b_h_trees_ancestry_and_all_three_deltas(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/plugin.py", "baseline\n")
    deployed = _commit(repo, "deployed")
    _write(repo, "src/plugin.py", "candidate\n")
    head = _commit(repo, "head")
    base = deployed

    bundle = collect_release_bundle(repo, deployed, base, head, _policy())

    assert bundle.deployed_commit == deployed
    assert bundle.base_commit == base
    assert bundle.head_commit == head
    assert bundle.deployed_tree == _git(repo, "show", "-s", "--format=%T", deployed)
    assert bundle.base_tree == bundle.deployed_tree
    assert bundle.head_tree == _git(repo, "show", "-s", "--format=%T", head)
    assert bundle.base_is_ancestor_of_head
    assert bundle.base_is_descendant_of_deployed
    assert not bundle.complete_delta_reviewed
    assert [(item.path, item.change_type) for item in bundle.deployed_to_base] == []
    assert [(item.path, item.change_type) for item in bundle.base_to_head] == [
        ("src/plugin.py", "modified")
    ]
    assert bundle.deployed_to_head == bundle.base_to_head
    decision = evaluate_ordinary_release(_policy(), bundle)
    assert not decision.eligible
    assert decision.reason == "incomplete_review"

    # Only a caller with the separate trusted review process may set the gate.
    attested = replace(bundle, complete_delta_reviewed=True)
    assert evaluate_ordinary_release(_policy(), attested).eligible


def test_collect_delta_stats_and_patch_support_text_binary_and_caps(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "text.txt", "base\n")
    (repo / "binary.bin").write_bytes(b"\x00base")
    older = _commit(repo, "base")
    _write(repo, "text.txt", "changed\nmore\n")
    (repo / "binary.bin").write_bytes(b"\x00changed")
    newer = _commit(repo, "changed")
    stats = dict(
        (path, (added, deleted)) for path, added, deleted in collect_delta_stats(repo, older, newer)
    )
    assert stats["text.txt"] == (2, 1)
    assert stats["binary.bin"] == (None, None)
    patch = collect_delta_patch(repo, older, newer, max_bytes=100_000)
    assert patch is not None and b"diff --git" in patch
    assert collect_delta_patch(repo, older, newer, max_bytes=1) is None


@pytest.mark.parametrize("secret_path", ["settings/private.ini", "nested/.env.prod"])
def test_rejects_private_file_even_when_added_then_removed(
    tmp_path: Path, secret_path: str
) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/plugin.py", "baseline\n")
    deployed = _commit(repo, "deployed")
    _write(repo, secret_path, "synthetic-not-a-secret\n")
    _commit(repo, "temporarily add private path")
    (repo / secret_path).unlink()
    _write(repo, "src/plugin.py", "candidate\n")
    head = _commit(repo, "remove private path and change candidate")

    with pytest.raises(GitEvidenceError) as error:
        collect_release_bundle(repo, deployed, deployed, head, _policy())
    assert error.value.code == "private_path"


def test_rejects_protected_path_in_introduced_commit_even_if_deleted_by_head(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/plugin.py")
    deployed = _commit(repo, "deployed")
    _write(repo, ".github/workflows/ci.yml", "name: temporary\n")
    _commit(repo, "add protected file")
    (repo / ".github/workflows/ci.yml").unlink()
    head = _commit(repo, "remove protected file")

    with pytest.raises(GitEvidenceError) as error:
        collect_release_bundle(
            repo, deployed, deployed, head, _policy((".github/workflows/ci.yml",))
        )
    assert error.value.code == "protected_path"


def test_does_not_reject_unchanged_protected_baseline_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/plugin.py", "baseline\n")
    _write(repo, ".github/workflows/ci.yml", "name: protected baseline\n")
    deployed = _commit(repo, "deployed baseline")
    _write(repo, "src/plugin.py", "candidate\n")
    head = _commit(repo, "candidate change")

    bundle = collect_release_bundle(
        repo, deployed, deployed, head, _policy((".github/workflows/ci.yml",))
    )

    assert [change.path for change in bundle.base_to_head] == ["src/plugin.py"]


def test_unallowlisted_intermediate_file_cannot_hide_behind_final_allowed_diff(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/plugin.py")
    deployed = _commit(repo, "baseline")
    _write(repo, "unreviewed.py", "arbitrary application source\n")
    _commit(repo, "intermediate unallowlisted source")
    (repo / "unreviewed.py").unlink()
    _write(repo, "src/plugin.py", "final allowed change\n")
    head = _commit(repo, "final")
    with pytest.raises(GitEvidenceError, match="unapproved_path"):
        collect_release_bundle(repo, deployed, deployed, head, _policy())


def test_reports_divergent_ancestry(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/plugin.py", "baseline\n")
    deployed = _commit(repo, "deployed")
    _git(repo, "checkout", "--orphan", "alternate")
    _write(repo, "src/plugin.py", "alternate\n")
    head = _commit(repo, "divergent head")
    _git(repo, "checkout", "-q", deployed)
    base = deployed

    bundle = collect_release_bundle(repo, deployed, base, head, _policy())

    assert not bundle.base_is_ancestor_of_head
    assert bundle.base_is_descendant_of_deployed


def test_rejects_symlink_mode(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/plugin.py")
    deployed = _commit(repo, "deployed")
    os.symlink("target", repo / "link")
    head = _commit(repo, "symlink")
    with pytest.raises(GitEvidenceError) as error:
        collect_release_bundle(repo, deployed, deployed, head, _policy())
    assert error.value.code == "unsupported_mode"


def test_replacing_a_baseline_symlink_still_requires_maintenance(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "src").mkdir()
    os.symlink("target", repo / "src/plugin.py")
    deployed = _commit(repo, "baseline symlink")
    (repo / "src/plugin.py").unlink()
    _write(repo, "src/plugin.py", "regular candidate\n")
    head = _commit(repo, "replace symlink")
    with pytest.raises(GitEvidenceError, match="unsupported_mode"):
        collect_release_bundle(repo, deployed, deployed, head, _policy())


def test_rejects_gitlink_submodule_mode(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/plugin.py")
    deployed = _commit(repo, "deployed")
    child_root = tmp_path / "child"
    child_root.mkdir()
    child = _repo(child_root)
    _write(child, "module.py")
    _commit(child, "submodule commit")
    _git(
        repo, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "vendor/submodule"
    )
    # Isolate mode rejection from the separate unallowlisted .gitmodules path.
    _git(repo, "rm", "--cached", ".gitmodules")
    (repo / ".gitmodules").unlink()
    head = _commit(repo, "gitlink")

    with pytest.raises(GitEvidenceError) as error:
        collect_release_bundle(repo, deployed, deployed, head, _policy())
    assert error.value.code == "unsupported_mode"


def test_rejects_malformed_and_truncated_raw_diff_output(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/plugin.py")
    deployed = _commit(repo, "deployed")
    _write(repo, "src/plugin.py", "candidate\n")
    head = _commit(repo, "head")
    original = _GitRunner.run

    def malformed(self: _GitRunner, *args: str, **kwargs: object) -> bytes:
        if args and args[0] == "diff-tree":
            return b":100644 100644 deadbeef"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(_GitRunner, "run", malformed)
    with pytest.raises(GitEvidenceError) as error:
        collect_release_bundle(repo, deployed, deployed, head, _policy())
    assert error.value.code == "invalid_git_output"


def test_git_evidence_ignores_ambient_git_environment_and_external_diff(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/plugin.py", "baseline\n")
    deployed = _commit(repo, "deployed")
    _write(repo, "src/plugin.py", "candidate\n")
    head = _commit(repo, "head")
    marker = tmp_path / "external-diff-ran"
    script = tmp_path / "external-diff"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n", encoding="utf-8")
    script.chmod(0o755)
    _git(repo, "config", "diff.external", str(script))
    with (repo / ".git/config").open("a", encoding="utf-8") as local_config:
        local_config.write("\nthis is intentionally malformed local Git config\n")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-repository"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong-worktree"))

    bundle = collect_release_bundle(
        repo,
        deployed,
        deployed,
        head,
        _policy(),
        # A poisoned ambient Git variable must not redirect object reads.
    )

    assert bundle.head_commit == head
    assert not marker.exists()


def test_git_runner_caps_child_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fake_git = tmp_path / "fake-git"
    fake_git.write_text("#!/bin/sh\nexec /usr/bin/head -c 9000000 /dev/zero\n", encoding="utf-8")
    fake_git.chmod(0o755)
    runner = _GitRunner(shutil.which("git"), repo, tmp_path)
    runner.executable = str(fake_git)
    with pytest.raises(GitEvidenceError) as error:
        runner.run("ignored")
    assert error.value.code == "output_limit"


def test_git_runner_enforces_command_timeout(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    fake_git = tmp_path / "fake-git"
    fake_git.write_text("#!/bin/sh\nexec /bin/sleep 2\n", encoding="utf-8")
    fake_git.chmod(0o755)
    runner = _GitRunner(shutil.which("git"), repo, tmp_path)
    runner.executable = str(fake_git)
    monkeypatch.setattr("chatcore_release_control.git_evidence.COMMAND_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    with pytest.raises(GitEvidenceError) as error:
        runner.run("ignored")
    assert error.value.code == "timeout"
    assert time.monotonic() - started < 1


def test_rejects_malformed_ids_and_missing_commits(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/plugin.py")
    deployed = _commit(repo, "deployed")
    with pytest.raises(GitEvidenceError, match="invalid_input"):
        collect_release_bundle(repo, "not-an-object", deployed, deployed, _policy())
    with pytest.raises(GitEvidenceError):
        collect_release_bundle(repo, "f" * 40, deployed, deployed, _policy())


def test_verifies_workflow_and_helper_blob_digests_at_pinned_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    workflow = b"name: release\n"
    helper = b"def verify():\n    return True\n"
    _write(repo, ".github/workflows/release.yml", workflow.decode())
    _write(repo, "packages/control/helper.py", helper.decode())
    commit = _commit(repo, "trusted workflow closure")
    expected = {
        ".github/workflows/release.yml": hashlib.sha256(workflow).hexdigest(),
        "packages/control/helper.py": hashlib.sha256(helper).hexdigest(),
    }

    assert verify_installed_blob_digests(repo, commit, expected) is None
    _write(repo, "packages/control/helper.py", "def verify():\n    return False\n")
    changed_commit = _commit(repo, "change trusted helper")
    with pytest.raises(GitEvidenceError) as error:
        verify_installed_blob_digests(repo, changed_commit, expected)
    assert error.value.code == "closure_mismatch"
