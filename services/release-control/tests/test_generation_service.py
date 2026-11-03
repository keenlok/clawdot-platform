from __future__ import annotations

import json
import os

import pytest
from chatcore_release_control.generation_service import GenerationServiceConfig, main, parse_request


def test_explicit_request_cannot_select_commits_policy_provider_or_commands():
    value = {
        "request_id": "synthetic-request",
        "specification": "Add a safe plugin behavior.",
        "synthetic_examples": [{"input": 1, "output": 2}],
        "title": "Safe plugin update",
        "body": "Synthetic description.",
    }
    assert parse_request(json.dumps(value).encode()) == value
    for field in (
        "base_commit",
        "deployed_commit",
        "policy_digest",
        "provider",
        "image",
        "command",
    ):
        with pytest.raises(ValueError):
            parse_request(json.dumps({**value, field: "synthetic"}).encode())
    with pytest.raises(ValueError):
        parse_request(b'{"request_id":"first","request_id":"second"}')


def test_generation_cli_help_never_loads_config_or_contacts_provider(capsys):
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0 and "--request" in capsys.readouterr().out


def test_generation_config_pins_all_runtime_files_and_refuses_unknown_fields(tmp_path):
    import hashlib

    for name in ("publication", "development", "llm"):
        file = tmp_path / name
        file.write_text("{}")
        file.chmod(0o600)
    for name in ("llm-key", "host-credential"):
        file = tmp_path / name
        file.write_text("synthetic-secret")
        file.chmod(0o600)
    value = {
        "version": 1,
        "target": "production",
        "llm_key_file": str(tmp_path / "llm-key"),
        "host_credential_file": str(tmp_path / "host-credential"),
        "host_socket": str(tmp_path / "host.sock"),
    }
    for name in ("publication", "development", "llm"):
        value[name + "_config_file"] = str(tmp_path / name)
        value[name + "_config_sha256"] = hashlib.sha256(b"{}").hexdigest()
    path = tmp_path / "generation.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    config = GenerationServiceConfig.from_file(path, expected_uid=os.getuid())
    assert config.target == "production" and "synthetic-secret" not in repr(config)
    value["unsafe_extra"] = True
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        GenerationServiceConfig.from_file(path, expected_uid=os.getuid())


@pytest.mark.asyncio
async def test_durable_generation_retry_resumes_before_observing_changed_baseline():
    from types import SimpleNamespace

    from chatcore_release_control.generation_service import execute_generation

    request = {
        "request_id": "durable-request",
        "specification": "Safe behavior",
        "synthetic_examples": [{"input": 1, "output": 2}],
        "title": "Safe update",
        "body": "Description",
    }
    resumed = []

    class Gateway:
        store = SimpleNamespace(get_submission=lambda request_id: object())

        def resume_request(self, request_id, **logical):
            resumed.append((request_id, logical))
            return SimpleNamespace(
                publication=SimpleNamespace(head_commit="a" * 40),
                pull_request=SimpleNamespace(pull_request_number=7),
            )

    result = await execute_generation(request, gateway=Gateway(), development=None, host=None)
    assert result == {
        "request_id": "durable-request",
        "pull_request_number": 7,
        "head_sha": "a" * 40,
    }
    assert resumed == [
        (
            "durable-request",
            {
                "specification": request["specification"],
                "pull_request_title": request["title"],
                "pull_request_body": request["body"],
                "synthetic_examples": request["synthetic_examples"],
            },
        )
    ]
