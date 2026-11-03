import json
from pathlib import Path

import pytest
from chatcore_google_browser_editor.settings import WorkerSettings
from pydantic import ValidationError


def make_settings(tmp_path: Path, **changes) -> WorkerSettings:
    seed = tmp_path / "seed-state.json"
    seed.write_text(json.dumps({"cookies": [], "origins": []}))
    seed.chmod(0o600)
    secret = tmp_path / "worker-token"
    secret.write_text("x" * 48)
    secret.chmod(0o600)
    values = {
        "editor_email": "editor@example.com",
        "owner_email": "owner@example.com",
        "state_path": seed,
        "secret_file": secret,
        "db_path": tmp_path / "jobs.sqlite3",
    }
    values.update(changes)
    return WorkerSettings(_env_file=None, **values)


def test_worker_requires_dedicated_editor_and_secure_seed_and_secret(tmp_path) -> None:
    settings = make_settings(tmp_path)
    assert settings.editor_email == "editor@example.com"
    assert settings.read_secret() == "x" * 48
    settings.read_storage_state()
    assert settings.bind_address() == ("0.0.0.0", 8080)


def test_worker_refuses_owner_account_and_noncanonical_email(tmp_path) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, owner_email="editor@example.com")
    with pytest.raises(ValidationError):
        make_settings(tmp_path, editor_email="EDITOR@example.com")


def test_seed_and_secret_must_be_private_regular_files(tmp_path) -> None:
    settings = make_settings(tmp_path)
    settings.secret_file.chmod(0o644)
    with pytest.raises(RuntimeError):
        settings.read_secret()
    settings = make_settings(tmp_path)
    settings.state_path.chmod(0o644)
    with pytest.raises(RuntimeError):
        settings.read_storage_state()


def test_seed_rejects_non_google_origins_and_cookies(tmp_path) -> None:
    settings = make_settings(tmp_path)
    settings.state_path.write_text(
        json.dumps(
            {"cookies": [{"name": "x", "value": "x", "domain": "evil.example"}], "origins": []}
        )
    )
    settings.state_path.chmod(0o600)
    with pytest.raises(RuntimeError):
        settings.read_storage_state()


def test_worker_paths_must_be_outside_repository(tmp_path):
    repo_path = Path(__file__).resolve().parents[1] / "unsafe-seed.json"
    with pytest.raises(ValidationError):
        make_settings(tmp_path, state_path=repo_path)


def test_bind_requires_valid_ip_and_port(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, bind="docs.google.com:8080")
