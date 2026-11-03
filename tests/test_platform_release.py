"""Release decisions use reviewed main ancestry and never overwrite a platform tag."""

import runpy
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
API = runpy.run_path(str(ROOT / ".github/scripts/platform_release.py"))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_version(repo: Path, version: str) -> str:
    (repo / "pyproject.toml").write_text(f'[project]\nname = "example"\nversion = "{version}"\n')
    git(repo, "add", "pyproject.toml")
    git(repo, "commit", "--allow-empty", "-m", f"version {version}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Release test")
    git(tmp_path, "config", "user.email", "release@example.invalid")
    commit_version(tmp_path, "0.0.0")
    return tmp_path


def test_unchanged_root_version_creates_no_release(repository):
    target = commit_version(repository, "0.0.0")
    assert API["release_tag"](repository, target) is None
    assert git(repository, "tag", "--list") == ""


def test_merge_release_compares_main_first_parent_instead_of_pr_tip(repository):
    git(repository, "checkout", "-b", "feature")
    commit_version(repository, "0.1.1")
    git(repository, "checkout", "main")
    commit_version(repository, "0.0.0")
    git(repository, "merge", "--no-ff", "feature", "-m", "Merge reviewed release")
    target = git(repository, "rev-parse", "HEAD")
    assert API["release_tag"](repository, target) == "v0.1.1"
    assert git(repository, "tag", "--list") == ""  # deciding never mutates refs


@pytest.mark.parametrize("annotated", [False, True])
def test_existing_tag_on_another_commit_is_refused(repository, annotated):
    args = ("-a", "-m", "Original release") if annotated else ()
    git(repository, "tag", *args, "v0.1.1")
    original = git(repository, "rev-parse", "v0.1.1^{commit}")
    target = commit_version(repository, "0.1.1")
    with pytest.raises(API["ReleaseError"], match="different commit"):
        API["release_tag"](repository, target)
    assert git(repository, "rev-parse", "v0.1.1^{commit}") == original


def test_retry_of_same_tag_and_commit_is_idempotent(repository):
    target = commit_version(repository, "0.1.1")
    git(repository, "tag", "v0.1.1", target)
    assert API["release_tag"](repository, target) is None


@pytest.mark.parametrize("version", ["0.0.0", "invalid", "1.2.3;echo unsafe"])
def test_invalid_or_decreasing_version_cannot_release(repository, version):
    commit_version(repository, "0.1.1")
    target = commit_version(repository, version)
    with pytest.raises(API["ReleaseError"]):
        API["release_tag"](repository, target)


def test_release_job_requires_both_checks_and_trusted_main_push():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["release"]
    assert set(job["needs"]) == {"ci", "scaffold"}
    assert "github.event_name == 'push'" in job["if"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "github.repository == 'keenlok/clawdot-platform'" in job["if"]
    assert job["permissions"] == {"contents": "write"}
    assert workflow["permissions"] == {"contents": "read"}
    assert all(
        "contents" not in value.get("permissions", {})
        for name, value in workflow["jobs"].items()
        if name != "release"
    )


def test_first_post_split_release_must_exceed_the_existing_baseline_tag(repository):
    git(repository, "tag", "v0.1.0")
    target = commit_version(repository, "0.0.1")
    with pytest.raises(API["ReleaseError"], match="published"):
        API["release_tag"](repository, target)
