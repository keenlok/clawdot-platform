from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from chatcore_release_control.host_journal import HostJournal
from chatcore_release_control.host_service import (
    HostDeploymentContext,
    HostService,
    HostServiceError,
)
from chatcore_release_control.host_service_http import (
    MAX_BODY_BYTES,
    BrokerHostClient,
    HostHTTPError,
    HostServiceASGI,
    main,
)
from chatcore_release_control.policy import DeploymentIdentity, JournalState


def identity(sha: str, image: str, sequence: int = 1) -> DeploymentIdentity:
    return DeploymentIdentity(sha, f"sha256:{image * 64}", 1, "4" * 64, "3" * 64, sequence)


class Dependencies:
    def __init__(self, context: HostDeploymentContext, repository: Path) -> None:
        self.context = context
        self.repository = repository
        self.actual = context.deployed_identity
        self.events: list[str] = []

    @contextmanager
    def fetch(self, _commit: str, _baseline_commit: str):
        yield self.repository

    def get_context(self, _approval: str) -> HostDeploymentContext:
        return self.context

    def prepare(self, *_args) -> None:
        self.events.append("prepare")

    def admit(self, approval: str, fence: int, operation: str) -> dict[str, object]:
        return {
            "approval_id": approval,
            "target": self.context.target,
            "operation_id": operation,
            "dispatch_fence": fence,
            "admitted": True,
            "may_start": True,
            "start_deadline": 120,
            "recovery_deadline": 500,
            "baseline_identity": self.context.deployed_identity,
            "payload_identity": self.context.payload,
        }

    def started(self, *_args) -> None:
        self.events.append("started")

    def restore_started(self, *_args) -> None:
        self.events.append("restore_started")

    def receipt(self, *_args) -> None:
        self.events.append("receipt")

    def verify(self, *_args) -> bool:
        return True

    def build(self, *_args) -> str:
        return self.context.payload.image_digest

    def observe(self) -> DeploymentIdentity:
        return self.actual

    def switch(self, identity_value: DeploymentIdentity) -> None:
        self.actual = identity_value

    def restore(self, identity_value: DeploymentIdentity) -> None:
        self.actual = identity_value

    def healthy(self, _identity: DeploymentIdentity) -> bool:
        return True

    def smoke_test(self, _identity: DeploymentIdentity) -> bool:
        return True


def make_app(tmp_path: Path) -> tuple[HostServiceASGI, Dependencies]:
    repo = tmp_path / "repo"
    repo.mkdir()
    context = HostDeploymentContext(
        "approval",
        "sb-bot",
        2,
        3,
        "merged_verified",
        "2" * 40,
        "3" * 64,
        identity("1" * 40, "a"),
        identity("2" * 40, "b", sequence=2),
    )
    deps = Dependencies(context, repo)
    service = HostService(
        source=deps,
        target="sb-bot",
        policy_digest="3" * 64,
        journal=HostJournal(tmp_path / "journal"),
        broker=deps,
        evidence=deps,
        builder=deps,
        runtime=deps,
        now=lambda: 100,
    )
    return HostServiceASGI(service), deps


async def request(app, *, method: str = "POST", path: str = "/v1/deploy", body: bytes = b"{}"):
    sent = []
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        },
        receive,
        send,
    )
    return sent[0]["status"], json.loads(sent[1]["body"])


@pytest.mark.asyncio
async def test_http_accepts_only_ids_and_returns_bounded_receipt(tmp_path: Path) -> None:
    app, deps = make_app(tmp_path)
    status, body = await request(
        app,
        body=b'{"approval_id":"approval","operation_id":"operation"}',
    )

    assert status == 200
    assert body["phase"] == JournalState.HEALTHY.value
    assert set(body) == {"operation_id", "phase", "receipt", "payload_digest"}
    assert "receipt" in deps.events


@pytest.mark.asyncio
async def test_http_rejects_host_selection_and_duplicate_json_keys(tmp_path: Path) -> None:
    app, deps = make_app(tmp_path)
    status, _ = await request(
        app,
        body=b'{"approval_id":"approval","operation_id":"operation","image":"attacker"}',
    )
    assert status == 400 and "started" not in deps.events

    status, _ = await request(
        app,
        body=b'{"approval_id":"approval","operation_id":"operation","operation_id":"other"}',
    )
    assert status == 400 and "started" not in deps.events


@pytest.mark.asyncio
async def test_http_caps_body_and_requires_exact_route(tmp_path: Path) -> None:
    app, deps = make_app(tmp_path)
    status, _ = await request(app, body=b" " * (MAX_BODY_BYTES + 1))
    assert status == 413 and "started" not in deps.events
    status, _ = await request(app, path="/v1/arbitrary")
    assert status == 404 and "started" not in deps.events


@pytest.mark.asyncio
async def test_host_state_route_returns_full_fresh_identity(tmp_path: Path) -> None:
    app, deps = make_app(tmp_path)
    status, body = await request(
        app,
        method="GET",
        path="/v1/host/targets/sb-bot/current-state",
        body=b"",
    )

    assert status == 200
    assert set(body) == {"deployed_identity", "healthy", "policy_digest", "target_sequence"}
    assert body["deployed_identity"]["git_sha"] == deps.context.deployed_identity.git_sha
    assert body["healthy"] is True


@pytest.mark.asyncio
async def test_deploy_lock_allows_asgi_broker_callback_to_read_truthful_host_state(
    tmp_path: Path,
) -> None:
    app, deps = make_app(tmp_path)
    original_admit = deps.admit
    observations = []

    def callback(approval_id, fence, operation_id):
        status, body = asyncio.run(
            request(
                app,
                method="GET",
                path="/v1/host/targets/sb-bot/current-state",
                body=b"",
            )
        )
        assert status == 200
        observations.append(body)
        return original_admit(approval_id, fence, operation_id)

    deps.admit = callback
    status, receipt = await request(
        app,
        body=b'{"approval_id":"approval","operation_id":"operation"}',
    )

    assert status == 200
    assert receipt["phase"] == JournalState.HEALTHY.value
    assert observations == [
        {
            "deployed_identity": deps.context.deployed_identity.__dict__,
            "healthy": True,
            "policy_digest": deps.context.policy_digest,
            "target_sequence": deps.context.deployed_identity.target_sequence,
        }
    ]


def test_broker_client_uses_host_auth_exact_routes_and_canonical_identities(tmp_path: Path) -> None:
    approval = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    operation = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    baseline = identity("1" * 40, "a")
    payload = identity("2" * 40, "b")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer " + "x" * 48
        if request.method == "GET":
            value = {
                "approval_id": approval,
                "target": "sb-bot",
                "sequence": 1,
                "dispatch_fence": 3,
                "status": "merged_verified",
                "merge_commit": payload.git_sha,
                "policy_digest": payload.policy_digest,
                "deployed_identity": baseline.__dict__,
                "base_sha": baseline.git_sha,
                "head_sha": payload.git_sha,
                "head_tree": "c" * 40,
                "expected_merge_tree": "d" * 40,
                "merge_tree": "d" * 40,
                "merge_parents": [baseline.git_sha, payload.git_sha],
                "host_operation": None,
            }
        elif request.url.path.endswith("/prepare"):
            digest = hashlib.sha256(
                json.dumps(
                    payload.__dict__, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest()
            value = {"operation_id": operation, "payload_digest": digest, "prepared": True}
        elif request.url.path.endswith("/switch-admission"):
            value = {
                "approval_id": approval,
                "target": "sb-bot",
                "operation_id": operation,
                "dispatch_fence": 3,
                "admitted": True,
                "may_start": True,
                "start_deadline": 120,
                "recovery_deadline": 600,
                "baseline_identity": baseline.__dict__,
                "payload_identity": payload.__dict__,
            }
        elif request.url.path.endswith("/switch-started") or request.url.path.endswith(
            "/restore-started"
        ):
            value = {"operation_id": operation, "started": True, "may_recover": True}
        else:
            value = {"recorded": True}
        return httpx.Response(200, json=value)

    credential = tmp_path / "host.token"
    credential.write_text("x" * 48, encoding="ascii")
    os.chmod(credential, 0o600)
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://broker")
    broker = BrokerHostClient(
        socket_path=tmp_path / "broker.sock", credential_file=credential, client=client
    )

    context = broker.get_context(approval)
    assert context.deployed_identity == baseline
    assert context.merge_parents == (baseline.git_sha, payload.git_sha)
    assert broker.prepare(approval, 3, operation, baseline, payload)["prepared"] is True
    admitted = broker.admit(approval, 3, operation)
    assert admitted["baseline_identity"] == baseline
    assert broker.started(approval, 3, operation)["operation_id"] == operation
    assert broker.restore_started(approval, 3, operation)["operation_id"] == operation
    assert [call.url.path.rsplit("/", 1)[-1] for call in calls] == [
        "deployment-context",
        "prepare",
        "switch-admission",
        "switch-started",
        "restore-started",
    ]
    assert UUID(operation)
    broker.close()


def test_broker_client_validates_expanded_terminal_host_operation(tmp_path: Path) -> None:
    approval = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    operation = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    baseline = identity("1" * 40, "a")
    payload = identity("2" * 40, "b")
    payload_digest = hashlib.sha256(
        json.dumps(
            payload.__dict__, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    terminal_receipt = {
        "approval_id": approval,
        "target": "sb-bot",
        "operation_id": operation,
        "receipt_id": "c" * 64,
        "fence": 3,
        "status": "healthy",
        "merge_commit": payload.git_sha,
        "observed_identity": payload.__dict__,
        "observed_at": "1970-01-01T00:05:00+00:00",
    }
    host_operation = {
        "operation_id": operation,
        "dispatch_fence": 3,
        "baseline_identity": baseline.__dict__,
        "payload_identity": payload.__dict__,
        "payload_digest": payload_digest,
        "state": "healthy",
        "start_deadline": "1970-01-01T00:02:00Z",
        "recovery_deadline": "1970-01-01T00:10:00Z",
        "receipt": terminal_receipt,
    }
    context_value = {
        "approval_id": approval,
        "target": "sb-bot",
        "sequence": 1,
        "dispatch_fence": 3,
        "status": "healthy",
        "merge_commit": payload.git_sha,
        "policy_digest": payload.policy_digest,
        "deployed_identity": baseline.__dict__,
        "base_sha": baseline.git_sha,
        "head_sha": payload.git_sha,
        "head_tree": "c" * 40,
        "expected_merge_tree": "d" * 40,
        "merge_tree": "d" * 40,
        "merge_parents": [baseline.git_sha, payload.git_sha],
        "host_operation": host_operation,
    }

    def client_for(value):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=value)),
            base_url="http://broker",
        )
        credential = tmp_path / "host.token"
        credential.write_text("x" * 48, encoding="ascii")
        os.chmod(credential, 0o600)
        return BrokerHostClient(
            socket_path=tmp_path / "broker.sock", credential_file=credential, client=client
        )

    broker = client_for(context_value)
    context = broker.get_context(approval)
    assert context.payload == payload
    assert context.host_operation_id == operation
    assert context.host_operation_payload_digest == payload_digest
    assert context.host_operation_receipt_id == "c" * 64
    broker.close()

    for mutate in (
        lambda item: item.update(payload_digest="0" * 64),
        lambda item: item.update(dispatch_fence=4),
        lambda item: item.update(recovery_deadline="1970-01-01T00:01:00Z"),
        lambda item: item.update(state="healthy", receipt=None),
        lambda item: item["receipt"].update(operation_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        lambda item: item["receipt"].update(observed_identity=baseline.__dict__),
    ):
        invalid = json.loads(json.dumps(context_value))
        mutate(invalid["host_operation"])
        client = client_for(invalid)
        with pytest.raises(HostServiceError):
            client.get_context(approval)
        client.close()


def test_broker_client_rejects_group_readable_host_credential(tmp_path: Path) -> None:
    credential = tmp_path / "host.token"
    credential.write_text("x" * 48, encoding="ascii")
    os.chmod(credential, 0o644)
    with pytest.raises(HostHTTPError):
        BrokerHostClient(socket_path=tmp_path / "broker.sock", credential_file=credential)


def test_broker_terminal_receipt_retry_reuses_persisted_observation_time(tmp_path: Path) -> None:
    approval = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    operation = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    payload = identity("2" * 40, "b")
    baseline = identity("1" * 40, "a")
    journal = HostJournal(tmp_path / "journal.json")
    journal.prepare(
        operation_id=operation,
        baseline=baseline,
        payload=payload,
        start_deadline=120,
        recovery_deadline=600,
        now=100,
    )
    journal.begin_switch(operation, now=101)
    journal.reconcile(operation, actual=payload, now=102)
    terminal = journal.reconcile(operation, actual=payload, healthy=True, now=103).record
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"recorded": True})

    credential = tmp_path / "host.token"
    credential.write_text("x" * 48, encoding="ascii")
    credential.chmod(0o600)
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://broker")
    broker = BrokerHostClient(
        socket_path=tmp_path / "broker.sock", credential_file=credential, client=client
    )

    broker.receipt(approval, 3, operation, terminal)
    broker.receipt(approval, 3, operation, journal.get_record(operation))

    assert requests[0] == requests[1]
    assert requests[0]["observed_at"] == "1970-01-01T00:01:43+00:00"
    broker.close()


def test_cli_refuses_uninstalled_config_path(capsys) -> None:
    assert main(["--config", "/tmp/operator-selected.json"]) == 2
    assert capsys.readouterr().err.strip() == "invalid_configuration"
