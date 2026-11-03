from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from chatcore_release_control.candidate_bundle import (
    CandidateBundleError,
    build_candidate_bundle,
)
from chatcore_release_control.development import DevelopmentResult
from chatcore_release_control.policy import PolicyManifest


def _git(repo: Path, *args: str) -> str:
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(repo.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    return (
        subprocess.check_output(
            ["git", "-C", str(repo), *args], env=environment, stderr=subprocess.PIPE
        )
        .decode("ascii")
        .strip()
    )


def _fixture(tmp_path: Path) -> tuple[Path, str, str, PolicyManifest]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Synthetic")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    _git(repo, "config", "core.hooksPath", ".git/hooks")
    source = repo / "src" / "plugin.py"
    source.parent.mkdir()
    source.write_text("value = 'baseline'\n")
    _git(repo, "add", "src/plugin.py")
    _git(repo, "commit", "-m", "deployed")
    deployed = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    policy = PolicyManifest.from_json(
        json.dumps(
            {
                "version": 1,
                "repository_id": 101,
                "owner_id": 202,
                "target": "production",
                "workflow_digests": {".github/workflows/release.yml": "a" * 64},
                "allowed_paths": ["src/plugin.py"],
                "protected_paths": [".github/workflows/release.yml"],
                "checks": [
                    {
                        "name": "candidate-tests",
                        "creator_app_id": 303,
                        "workflow_path": ".github/workflows/release.yml",
                        "workflow_sha": "a" * 64,
                    }
                ],
            }
        )
    )
    return repo, deployed, base, policy


def _result(base: str, policy: PolicyManifest, files: tuple[tuple[str, str], ...]):
    return DevelopmentResult(
        report=b"synthetic report",
        report_digest=hashlib.sha256(b"synthetic report").hexdigest(),
        base_commit=base,
        policy_digest=policy.digest,
        candidate_files=files,
    )


def _build(repo, deployed, base, policy, *, content="raise RuntimeError('must not execute')\n"):
    return build_candidate_bundle(
        repository=repo,
        result=_result(base, policy, (("src/plugin.py", content),)),
        request_id="request-123",
        deployed_commit=deployed,
        base_commit=base,
        policy=policy,
        commit_timestamp=1_798_765_432,
        specification=b"Implement a safe feature.",
        pull_request_title=b"Add the safe feature",
        pull_request_body=b"Synthetic summary.",
    )


def test_builds_deterministic_frozen_commit_and_full_bundle_without_touching_source(tmp_path):
    repo, deployed, base, policy = _fixture(tmp_path)
    status_before = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    config_before = (repo / ".git" / "config").read_bytes()

    first = _build(repo, deployed, base, policy)
    second = _build(repo, deployed, base, policy)

    assert first.head_commit == second.head_commit
    assert first.bundle == second.bundle
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == status_before
    assert (repo / ".git" / "config").read_bytes() == config_before
    assert first.base_commit == base
    assert first.deployed_commit == deployed
    assert first.request_id == "request-123"
    assert first.head_commit not in {deployed, base}

    bundle_path = tmp_path / "candidate.bundle"
    bundle_path.write_bytes(first.bundle)
    receiver = tmp_path / "bundle.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(bundle_path), str(receiver)], check=True)
    assert _git(receiver, "rev-parse", f"{first.head_commit}^") == base
    assert _git(receiver, "show", "-s", "--format=%s", first.head_commit) == (
        "Candidate release request-123"
    )
    heads = subprocess.check_output(
        ["git", "bundle", "list-heads", str(bundle_path)],
    ).decode()
    assert deployed in heads and base in heads and first.head_commit in heads


def test_rejects_candidate_paths_and_metadata_outside_fixed_policy(tmp_path):
    repo, deployed, base, policy = _fixture(tmp_path)
    bad_result = _result(base, policy, (("src/unapproved.py", "safe\n"),))
    with pytest.raises(CandidateBundleError):
        build_candidate_bundle(
            repository=repo,
            result=bad_result,
            request_id="request-123",
            deployed_commit=deployed,
            base_commit=base,
            policy=policy,
            commit_timestamp=1_798_765_432,
            specification=b"Implement a safe feature.",
            pull_request_title=b"Add the safe feature",
            pull_request_body=b"Synthetic summary.",
        )


def test_rejects_result_bound_to_different_baseline_or_policy(tmp_path):
    repo, deployed, base, policy = _fixture(tmp_path)
    result = _result("0" * 40, policy, (("src/plugin.py", "safe\n"),))
    with pytest.raises(CandidateBundleError):
        build_candidate_bundle(
            repository=repo,
            result=result,
            request_id="request-123",
            deployed_commit=deployed,
            base_commit=base,
            policy=policy,
            commit_timestamp=1_798_765_432,
            specification=b"Implement a safe feature.",
            pull_request_title=b"Add the safe feature",
            pull_request_body=b"Synthetic summary.",
        )


def test_rejects_inconsistent_deployed_base_ancestry_and_bad_timestamp(tmp_path):
    repo, deployed, base, policy = _fixture(tmp_path)
    with pytest.raises(CandidateBundleError):
        build_candidate_bundle(
            repository=repo,
            result=_result(base, policy, (("src/plugin.py", "safe\n"),)),
            request_id="request-123",
            deployed_commit=base,
            base_commit=deployed,
            policy=policy,
            commit_timestamp=-1,
            specification=b"Implement a safe feature.",
            pull_request_title=b"Add the safe feature",
            pull_request_body=b"Synthetic summary.",
        )
