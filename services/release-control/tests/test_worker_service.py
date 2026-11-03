from __future__ import annotations

import json
import os
from uuid import uuid4

import httpx
import pytest
from chatcore_release_control.worker_service import (
    ProtectedBridgeReporter,
    WorkerServiceConfig,
    main,
)


def config_file(tmp_path):
    for name, data in {
        "dsn": "synthetic-dsn",
        "key": "synthetic-key",
        "credential": "b" * 32,
    }.items():
        path = tmp_path / name
        path.write_text(data)
        path.chmod(0o600)
    value = {
        "version": 1,
        "database_dsn_file": str(tmp_path / "dsn"),
        "github_app_id": 11,
        "publication_app_id": 12,
        "runner_registration_app_id": 13,
        "github_installation_id": 21,
        "github_repository_id": 101,
        "github_private_key_file": str(tmp_path / "key"),
        "targets": ["production"],
        "bridge_socket": str(tmp_path / "bridge.sock"),
        "bridge_credential_file": str(tmp_path / "credential"),
        "worker_id": "protected-worker",
        "poll_seconds": 10,
    }
    path = tmp_path / "worker.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path, value


def test_worker_cli_config_is_explicit_private_and_has_distinct_app_roles(tmp_path, capsys):
    path, value = config_file(tmp_path)
    config = WorkerServiceConfig.from_file(path, expected_uid=os.getuid())
    assert config.targets == ("production",) and "synthetic-dsn" not in repr(config)
    assert main(["--config", str(path), "--check-config"], expected_uid=os.getuid()) == 0
    assert "configuration valid" in capsys.readouterr().out
    value["publication_app_id"] = value["github_app_id"]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        WorkerServiceConfig.from_file(path, expected_uid=os.getuid())


async def test_report_client_sends_only_ids_and_rejects_unexpected_response_fields():
    request_id, approval_id = uuid4(), uuid4()
    calls = []

    def send(request):
        calls.append(request)
        assert json.loads(request.content) == {
            "request_id": str(request_id),
            "approval_id": str(approval_id),
        }
        return httpx.Response(
            200, json={"approval_id": str(approval_id), "status": "queued", "message_id": 900}
        )

    client = ProtectedBridgeReporter(
        socket_path="/synthetic/bridge.sock",
        credential="b" * 32,
        transport=httpx.MockTransport(send),
    )
    try:
        await client.report(request_id, approval_id)
        assert str(calls[0].url) == "http://bridge.local/v1/broker/reports"
        assert "b" * 32 not in repr(client)
    finally:
        await client.close()


def test_worker_help_requires_no_configuration_or_database(capsys):
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0 and "--once" in capsys.readouterr().out
