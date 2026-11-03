import json
import os
import stat

import pytest
from chatcore_google_browser_editor import bootstrap
from chatcore_google_browser_editor.bootstrap import validate_output_path, validate_seed_file


def test_bootstrap_output_must_be_outside_repository_and_new(tmp_path):
    output = validate_output_path(tmp_path / "seed.json")
    assert output.parent.stat().st_mode & 0o777 == 0o700
    output.touch()
    with pytest.raises(ValueError):
        validate_output_path(output)
    repository_path = __import__("pathlib").Path(__file__).resolve().parents[3] / "unsafe-seed.json"
    with pytest.raises(ValueError):
        validate_output_path(repository_path)


@pytest.mark.parametrize("git_marker", ["directory", "worktree_file"])
def test_bootstrap_rejects_any_git_checkout_ancestor_when_installed(
    tmp_path, monkeypatch, git_marker
):
    checkout = tmp_path / "repo"
    nested = checkout / "runtime" / "bootstrap-output"
    nested.mkdir(parents=True)
    marker = checkout / ".git"
    if git_marker == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: ../.git/worktrees/worker\n")
    installed = tmp_path / "venv" / "site-packages" / "chatcore_google_browser_editor"
    installed.mkdir(parents=True)
    monkeypatch.setattr(bootstrap, "__file__", str(installed / "bootstrap.py"))

    with pytest.raises(ValueError, match="outside the repository"):
        validate_output_path(nested / "seed.json")


def test_bootstrap_never_changes_existing_parent_permissions(tmp_path):
    public_parent = tmp_path / "public"
    public_parent.mkdir(mode=0o755)
    public_parent.chmod(0o755)
    before = stat.S_IMODE(public_parent.stat().st_mode)
    with pytest.raises(ValueError, match="private directory"):
        validate_output_path(public_parent / "seed.json")
    assert stat.S_IMODE(public_parent.stat().st_mode) == before
    assert not (public_parent / "seed.json").exists()


def test_bootstrap_creates_only_new_private_output_directory(tmp_path):
    parent = tmp_path / "new-private"
    output = validate_output_path(parent / "seed.json")
    assert output.parent == parent
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_bootstrap_refuses_output_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("keep")
    output = tmp_path / "seed.json"
    output.symlink_to(target)
    with pytest.raises(ValueError):
        validate_output_path(output)
    assert target.read_text() == "keep"


def test_seed_writer_creates_mode_600_before_first_write(tmp_path, monkeypatch):
    output = validate_output_path(tmp_path / "seed.json")
    original_write = os.write
    modes_at_write = []

    def inspect_first_write(fd, data):
        modes_at_write.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return original_write(fd, data)

    monkeypatch.setattr(bootstrap.os, "write", inspect_first_write)
    bootstrap.write_seed_state(
        output,
        {
            "cookies": [{"name": "SID", "value": "private", "domain": ".google.com"}],
            "origins": [{"origin": "https://docs.google.com", "localStorage": []}],
        },
        "editor@example.com",
    )
    assert modes_at_write and all(mode == 0o600 for mode in modes_at_write)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    validate_seed_file(output, "editor@example.com")


def test_seed_writer_does_not_remove_file_created_after_preflight(tmp_path):
    output = validate_output_path(tmp_path / "seed.json")
    output.write_text("external")
    with pytest.raises(FileExistsError):
        bootstrap.write_seed_state(output, {"cookies": [], "origins": []}, "editor@example.com")
    assert output.read_text() == "external"


def test_bootstrap_seed_is_mode_600_and_google_only(tmp_path):
    path = tmp_path / "seed.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [{"name": "SID", "value": "private", "domain": ".google.com"}],
                "origins": [{"origin": "https://docs.google.com", "localStorage": []}],
            }
        )
    )
    path.chmod(0o600)
    validate_seed_file(path, "editor@example.com")
    path.chmod(0o644)
    with pytest.raises(RuntimeError):
        validate_seed_file(path, "editor@example.com")
    path.chmod(0o600)
    path.write_text(json.dumps({"cookies": [{"domain": "evil.example"}], "origins": []}))
    with pytest.raises(RuntimeError):
        validate_seed_file(path, "editor@example.com")
