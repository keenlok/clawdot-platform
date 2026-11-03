from __future__ import annotations

import base64
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15

sys.path.insert(0, str(Path(__file__).parent))
pytest_plugins = ("test_broker",)

import chatcore_release_control.broker_http as broker_http  # noqa: E402
from chatcore_release_control.broker import BrokerStore  # noqa: E402
from chatcore_release_control.broker_http import (  # noqa: E402
    BrokerApplication,
    BrokerHttpError,
    BrokerRuntimeConfig,
    OIDCJobAuthenticator,
    ProtectedHostClient,
    ReleaseEvidenceVerifier,
)  # noqa: E402
from chatcore_release_control.oidc import CallerIdentity  # noqa: E402
from test_broker import _merged_release, _queued_card  # noqa: E402

DISPATCH_ID = UUID("33333333-3333-4333-8333-333333333333")


async def _async_value(value):
    return value


def _jwks(key):
    value = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {"keys": [{**value, "kid": "broker-test", "use": "sig", "alg": "RS256"}]}


def _token(key, *, now=None, runner_environment="github-hosted", check_run_id="6001"):
    current = int(now or time.time())
    return jwt.encode(
        {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "https://broker.invalid/release",
            "sub": "repo:acme/plugin:ref:refs/heads/main",
            "iat": current - 1,
            "nbf": current - 1,
            "exp": current + 120,
            "repository_id": "101",
            "repository_owner_id": "202",
            "repository": "acme/plugin",
            "repository_visibility": "private",
            "event_name": "workflow_dispatch",
            "ref": "refs/heads/main",
            "workflow_ref": "acme/plugin/.github/workflows/release.yml@refs/heads/main",
            "workflow_sha": "a" * 40,
            "run_id": "5001",
            "run_attempt": "1",
            "check_run_id": check_run_id,
            "runner_environment": runner_environment,
        },
        key,
        algorithm="RS256",
        headers={"kid": "broker-test"},
    )


def _transport(check_run_id="6001", display_title=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/plugin/actions/runs/5001/attempts/1":
            return httpx.Response(
                200,
                json={
                    "id": 5001,
                    "run_attempt": 1,
                    "repository": {"id": 101},
                    "event": "workflow_dispatch",
                    "head_branch": "main",
                    "head_sha": "a" * 40,
                    "path": ".github/workflows/release.yml@refs/heads/main",
                    "display_title": display_title or f"release:{DISPATCH_ID}",
                },
            )
        if request.url.path == "/repos/acme/plugin/actions/runs/5001/attempts/1/jobs":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "jobs": [
                        {
                            "id": 7001,
                            "run_id": 5001,
                            "run_attempt": 1,
                            "status": "completed",
                            "conclusion": "success",
                            "check_run_url": (
                                "https://api.github.com/repos/acme/plugin/check-runs/"
                                f"{check_run_id}"
                            ),
                        }
                    ],
                },
            )
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


async def _request(app, method, path, *, headers=(), body=b""):
    events = [{"type": "http.request", "body": body, "more_body": False}]
    sent = []

    async def receive():
        return events.pop(0) if events else {"type": "http.disconnect"}

    async def send(event):
        sent.append(event)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": list(headers),
        },
        receive,
        send,
    )
    start = sent[0]
    payload = json.loads(sent[1]["body"])
    return start["status"], payload


@pytest.mark.asyncio
async def test_signed_oidc_context_request_uses_closure_and_independent_job_transport(
    broker_pool, tmp_path, monkeypatch
):
    _, card, _, _ = await _queued_card(broker_pool)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(broker_http, "verify_installed_blob_digests", lambda *args: None)
    oidc_transport = httpx.MockTransport(
        lambda request: (
            httpx.Response(200, json=_jwks(key))
            if request.url.host == "token.actions.githubusercontent.com"
            else httpx.Response(404)
        )
    )
    auth = OIDCJobAuthenticator(
        audience="https://broker.invalid/release",
        workflow_path=".github/workflows/release.yml",
        merge_runner_environment="github-hosted",
        relay_runner_environment="self-hosted",
        object_repository=tmp_path,
        workflow_blobs={".github/workflows/release.yml": "b" * 64},
        dispatch_id_provider=lambda _approval_id, _fence: _async_value(DISPATCH_ID),
        github_token_provider=lambda: "synthetic-github-token",
        github_transport=_transport(),
        jwks_transport=oidc_transport,
    )
    config = BrokerRuntimeConfig(
        config_path=tmp_path / "config.json",
        database_dsn_file=tmp_path / "dsn",
        github_app_roles=(
            ("publication", 10),
            ("broker", 12),
            ("runner_registration", 14),
        ),
        github_installation_id=34,
        github_repository_id=101,
        github_private_key_file=tmp_path / "app.pem",
        host_inbound_credential_file=tmp_path / "host-in",
        host_client_credential_file=tmp_path / "host-out",
        host_socket=tmp_path / "host.sock",
        object_repository=tmp_path,
        workflow_path=".github/workflows/release.yml",
        merge_runner_environment="github-hosted",
        relay_runner_environment="self-hosted",
        audience="https://broker.invalid/release",
        targets=("production",),
        workflow_blobs=((".github/workflows/release.yml", "b" * 64),),
        uds_path=tmp_path / "broker.sock",
    )
    app = BrokerApplication(
        store=BrokerStore(broker_pool),
        config=config,
        job_authenticator=auth,
        host_client=ProtectedHostClient(
            socket_path=config.host_socket,
            credential="host-outbound-synthetic",
            transport=httpx.MockTransport(lambda _: httpx.Response(503)),
        ),
        evidence_verifier=ReleaseEvidenceVerifier(
            object_repository=tmp_path,
            workflow_blobs=dict(config.workflow_blobs),
            github_token_provider=lambda: "synthetic-github-token",
        ),
        host_inbound_credential="host-inbound-synthetic",
        github_token_provider=lambda: "synthetic-github-token",
    )

    status, body = await _request(
        app,
        "GET",
        f"/v1/releases/{card.approval_id}/merge-context",
        headers=[(b"authorization", f"Bearer {_token(key)}".encode())],
    )

    assert status == 200
    assert body["approval_id"] == str(card.approval_id)
    assert body["deployed_identity"]["git_sha"] == "d" * 40
    assert body["owner_id"] == 202


@pytest.mark.asyncio
async def test_oidc_phase_uses_self_hosted_runner_and_distinct_job_check_id(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(broker_http, "verify_installed_blob_digests", lambda *_args: None)
    auth = OIDCJobAuthenticator(
        audience="https://broker.invalid/release",
        workflow_path=".github/workflows/release.yml",
        merge_runner_environment="github-hosted",
        relay_runner_environment="self-hosted",
        object_repository=tmp_path,
        workflow_blobs={".github/workflows/release.yml": "b" * 64},
        dispatch_id_provider=lambda _approval_id, _fence: _async_value(DISPATCH_ID),
        github_token_provider=lambda: "synthetic-github-token",
        github_transport=_transport(check_run_id="9001"),
        jwks_transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_jwks(key))),
    )
    context = SimpleNamespace(
        approval_id=UUID("11111111-1111-4111-8111-111111111111"),
        dispatch_fence=3,
        repository="acme/plugin",
        repository_id=101,
        owner_id=202,
        workflow_sha="a" * 40,
    )
    token = _token(key, runner_environment="self-hosted", check_run_id="9001")

    identity = await auth.verify(token, context, phase="relay")

    assert identity.runner_environment == "self-hosted"
    assert identity.check_run_id == 9001


@pytest.mark.asyncio
async def test_oidc_denies_run_with_title_not_bound_to_active_dispatch_id(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(broker_http, "verify_installed_blob_digests", lambda *_args: None)
    auth = OIDCJobAuthenticator(
        audience="https://broker.invalid/release",
        workflow_path=".github/workflows/release.yml",
        merge_runner_environment="github-hosted",
        relay_runner_environment="self-hosted",
        object_repository=tmp_path,
        workflow_blobs={".github/workflows/release.yml": "b" * 64},
        dispatch_id_provider=lambda _approval_id, _fence: _async_value(DISPATCH_ID),
        github_token_provider=lambda: "synthetic-github-token",
        github_transport=_transport(display_title="release:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        jwks_transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_jwks(key))),
    )
    context = SimpleNamespace(
        approval_id=UUID("11111111-1111-4111-8111-111111111111"),
        dispatch_fence=3,
        repository="acme/plugin",
        repository_id=101,
        owner_id=202,
        workflow_sha="a" * 40,
    )

    with pytest.raises(BrokerHttpError) as denied:
        await auth.verify(_token(key), context)

    assert denied.value.code == "caller_denied"


@pytest.mark.asyncio
async def test_host_routes_reject_job_oidc_and_job_routes_reject_host_credential(
    broker_pool, tmp_path, monkeypatch
):
    _, card, _, _ = await _queued_card(broker_pool)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(broker_http, "verify_installed_blob_digests", lambda *args: None)
    oidc_transport = httpx.MockTransport(lambda _: httpx.Response(200, json=_jwks(key)))
    config = BrokerRuntimeConfig(
        config_path=tmp_path / "config.json",
        database_dsn_file=tmp_path / "dsn",
        github_app_roles=(
            ("publication", 10),
            ("broker", 12),
            ("runner_registration", 14),
        ),
        github_installation_id=34,
        github_repository_id=101,
        github_private_key_file=tmp_path / "app.pem",
        host_inbound_credential_file=tmp_path / "host-in",
        host_client_credential_file=tmp_path / "host-out",
        host_socket=tmp_path / "host.sock",
        object_repository=tmp_path,
        workflow_path=".github/workflows/release.yml",
        merge_runner_environment="github-hosted",
        relay_runner_environment="self-hosted",
        audience="https://broker.invalid/release",
        targets=("production",),
        workflow_blobs=((".github/workflows/release.yml", "b" * 64),),
        uds_path=tmp_path / "broker.sock",
    )
    app = BrokerApplication(
        store=BrokerStore(broker_pool),
        config=config,
        job_authenticator=OIDCJobAuthenticator(
            audience=config.audience,
            workflow_path=config.workflow_path,
            merge_runner_environment=config.merge_runner_environment,
            relay_runner_environment=config.relay_runner_environment,
            object_repository=tmp_path,
            workflow_blobs=dict(config.workflow_blobs),
            dispatch_id_provider=lambda _approval_id, _fence: _async_value(DISPATCH_ID),
            github_token_provider=lambda: "synthetic-github-token",
            github_transport=_transport(),
            jwks_transport=oidc_transport,
        ),
        host_client=ProtectedHostClient(
            socket_path=config.host_socket,
            credential="host-outbound-synthetic",
            transport=httpx.MockTransport(lambda _: httpx.Response(503)),
        ),
        evidence_verifier=ReleaseEvidenceVerifier(
            object_repository=tmp_path,
            workflow_blobs=dict(config.workflow_blobs),
            github_token_provider=lambda: "synthetic-github-token",
        ),
        host_inbound_credential="host-inbound-synthetic",
        github_token_provider=lambda: "synthetic-github-token",
    )

    status, _ = await _request(
        app,
        "GET",
        f"/v1/host/releases/{card.approval_id}/deployment-context",
        headers=[(b"authorization", f"Bearer {_token(key)}".encode())],
    )
    assert status == 403
    status, _ = await _request(
        app,
        "GET",
        f"/v1/releases/{card.approval_id}/merge-context",
        headers=[(b"authorization", b"Bearer host-inbound-synthetic")],
    )
    assert status == 401
    status, context = await _request(
        app,
        "GET",
        f"/v1/host/releases/{card.approval_id}/deployment-context",
        headers=[(b"authorization", b"Bearer host-inbound-synthetic")],
    )
    assert status == 200
    assert context["base_sha"] == "a" * 40
    assert context["head_sha"] == "b" * 40
    assert context["head_tree"] == "c" * 40
    assert context["expected_merge_tree"] is None
    assert context["merge_tree"] is None
    assert context["merge_parents"] is None


@pytest.mark.asyncio
async def test_runner_relay_binds_merge_run_but_allows_its_own_check_id(
    broker_pool, tmp_path, monkeypatch
):
    store, card, fence, _, run, now, operation = await _merged_release(broker_pool)
    await store.prepare_switch(
        card.approval_id,
        dispatch_fence=fence,
        operation=operation,
        actual_identity=operation.baseline,
        now=now,
    )
    config = BrokerRuntimeConfig(
        config_path=tmp_path / "config.json",
        database_dsn_file=tmp_path / "dsn",
        github_app_roles=(
            ("publication", 10),
            ("broker", 12),
            ("runner_registration", 14),
        ),
        github_installation_id=34,
        github_repository_id=101,
        github_private_key_file=tmp_path / "app.pem",
        host_inbound_credential_file=tmp_path / "host-in",
        host_client_credential_file=tmp_path / "host-out",
        host_socket=tmp_path / "host.sock",
        object_repository=tmp_path,
        workflow_path=".github/workflows/release.yml",
        merge_runner_environment="github-hosted",
        relay_runner_environment="self-hosted",
        audience="https://broker.invalid/release",
        targets=("production",),
        workflow_blobs=((".github/workflows/release.yml", "b" * 64),),
        uds_path=tmp_path / "broker.sock",
    )
    assert config.github_app_id == 12
    with pytest.raises(BrokerHttpError) as duplicate_apps:
        replace(
            config,
            github_app_roles=(
                ("publication", 10),
                ("broker", 10),
                ("runner_registration", 14),
            ),
        )
    assert duplicate_apps.value.code == "runtime_not_configured"
    authenticator = OIDCJobAuthenticator(
        audience=config.audience,
        workflow_path=config.workflow_path,
        merge_runner_environment=config.merge_runner_environment,
        relay_runner_environment=config.relay_runner_environment,
        object_repository=tmp_path,
        workflow_blobs=dict(config.workflow_blobs),
        dispatch_id_provider=lambda _approval_id, _fence: _async_value(DISPATCH_ID),
        github_token_provider=lambda: "synthetic-github-token",
    )
    host = ProtectedHostClient(
        socket_path=config.host_socket,
        credential="synthetic-host-credential",
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )
    app = BrokerApplication(
        store=store,
        config=config,
        job_authenticator=authenticator,
        host_client=host,
        evidence_verifier=ReleaseEvidenceVerifier(
            object_repository=tmp_path,
            workflow_blobs=dict(config.workflow_blobs),
            github_token_provider=lambda: "synthetic-github-token",
        ),
        host_inbound_credential="synthetic-host-inbound",
        github_token_provider=lambda: "synthetic-github-token",
    )
    identity = CallerIdentity(101, 202, "a" * 40, run.run_id, run.attempt, 9001, "self-hosted")
    verified_phases = []

    async def verify(_token, _context, *, phase="merge"):
        verified_phases.append(phase)
        return identity

    monkeypatch.setattr(authenticator, "verify", verify)
    called = []

    async def operate(action, approval_id, operation_id):
        called.append((action, approval_id, operation_id))
        return {
            "operation_id": str(operation_id),
            "phase": "prepared",
            "receipt": None,
            "payload_digest": operation.payload_digest,
        }

    monkeypatch.setattr(host, "operate", operate)
    path = f"/v1/releases/{card.approval_id}/host-deploy"
    headers = [
        (b"authorization", b"Bearer synthetic-job-token"),
        (b"content-type", b"application/json"),
    ]
    body = json.dumps({"operation_id": str(operation.operation_id)}).encode()

    status, result = await _request(app, "POST", path, headers=headers, body=body)

    assert status == 200, result
    assert result["operation_id"] == str(operation.operation_id)
    assert verified_phases == ["relay"]
    assert called == [("deploy", card.approval_id, operation.operation_id)]

    identity = CallerIdentity(101, 202, "a" * 40, run.run_id + 1, run.attempt, 9002, "self-hosted")
    status, _ = await _request(app, "POST", path, headers=headers, body=body)
    assert status == 401
    assert len(called) == 1


def test_protected_config_and_secret_files_reject_group_readable_or_symlink(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("synthetic\n")
    secret.chmod(0o644)

    with pytest.raises(BrokerHttpError) as error:
        broker_http._secret(secret)

    assert error.value.code == "runtime_not_configured"
    secure = tmp_path / "secure"
    secure.write_text("synthetic\n")
    secure.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(secure)
    with pytest.raises(BrokerHttpError):
        broker_http._secret(link)


@pytest.mark.parametrize(
    ("release_kind", "expected_eligible"),
    [
        (None, False),
        ("ordinary-continuation", True),
        ("reconciliation", True),
        ("maintenance_bypass", False),
    ],
)
def test_release_evidence_verifier_requires_explicit_reconciliation_kind(
    tmp_path, monkeypatch, release_kind, expected_eligible
):
    from chatcore_release_control.policy import Change, ReleaseBundle
    from test_broker import _initial_identity, _policy

    policy = _policy()
    inherited = Change("packages/plugin/src/plugin.py")
    continuation = release_kind == "ordinary-continuation"
    same_tree = "c" * 40 if continuation else "e" * 40
    bundle = ReleaseBundle(
        deployed_commit="d" * 40,
        base_commit="a" * 40,
        head_commit="b" * 40,
        deployed_tree=same_tree,
        base_tree=same_tree,
        head_tree="f" * 40,
        policy_digest=policy.digest,
        base_is_ancestor_of_head=True,
        base_is_descendant_of_deployed=continuation,
        same_tree_continuation_recorded=continuation,
        complete_delta_reviewed=False,
        deployed_to_base=() if continuation else (inherited,),
        base_to_head=(inherited,) if continuation else (),
        deployed_to_head=(inherited,),
    )
    review = {
        "complete_delta_reviewed": True,
        "deployed_commit": bundle.deployed_commit,
        "base_commit": bundle.base_commit,
        "head_commit": bundle.head_commit,
        "deployed_tree": bundle.deployed_tree,
        "base_tree": bundle.base_tree,
        "head_tree": bundle.head_tree,
        "policy_digest": bundle.policy_digest,
        "base_is_ancestor_of_head": bundle.base_is_ancestor_of_head,
        "base_is_descendant_of_deployed": bundle.base_is_descendant_of_deployed,
        "same_tree_continuation_recorded": bundle.same_tree_continuation_recorded,
        "deployed_to_base": (
            []
            if continuation
            else [
                {
                    "path": inherited.path,
                    "change_type": inherited.change_type,
                    "object_type": inherited.object_type,
                }
            ]
        ),
        "base_to_head": (
            [
                {
                    "path": inherited.path,
                    "change_type": inherited.change_type,
                    "object_type": inherited.object_type,
                }
            ]
            if continuation
            else []
        ),
        "deployed_to_head": [
            {
                "path": inherited.path,
                "change_type": inherited.change_type,
                "object_type": inherited.object_type,
            }
        ],
        "check_evidence_digest": "6" * 64,
    }
    if release_kind is not None:
        review["release_kind"] = "ordinary" if continuation else release_kind

    class FakeGitHubClient:
        def __init__(self, **_kwargs):
            self.closed = False

        def get_json(self, path):
            assert path == "git/ref/heads/main"
            return {"object": {"sha": bundle.base_commit}}

        def close(self):
            self.closed = True

    class FakeResolver:
        def __init__(self, *_args, **_kwargs):
            pass

        def resolve(self, pr_number, candidate):
            assert pr_number == 6 and candidate == bundle
            return SimpleNamespace(
                base_commit=bundle.base_commit,
                head_commit=bundle.head_commit,
                head_tree=bundle.head_tree,
                synthetic_merge_tree="7" * 40,
            )

    monkeypatch.setattr(broker_http, "collect_release_bundle", lambda *_args, **_kwargs: bundle)
    monkeypatch.setattr(broker_http, "GitHubEvidenceClient", FakeGitHubClient)
    monkeypatch.setattr(broker_http, "GitHubEvidenceResolver", FakeResolver)
    monkeypatch.setattr(broker_http, "evidence_digest", lambda _snapshot: "6" * 64)
    context = SimpleNamespace(
        policy_manifest=policy,
        policy_digest=policy.digest,
        repository="acme/plugin",
        repository_id=policy.repository_id,
        deployed_identity=_initial_identity(),
        base_sha=bundle.base_commit,
        head_sha=bundle.head_commit,
        head_tree=bundle.head_tree,
        review_bundle=review,
        pr_number=6,
    )
    verifier = ReleaseEvidenceVerifier(
        object_repository=tmp_path,
        workflow_blobs={},
        github_token_provider=lambda: "synthetic-github-token",
    )
    if not expected_eligible:
        with pytest.raises(BrokerHttpError, match="caller_denied"):
            verifier.verify(context)
    else:
        verified = verifier.verify(context)
        assert verified.base_sha == bundle.base_commit
        assert verified.complete_delta_reviewed


@pytest.mark.asyncio
async def test_host_state_response_is_bounded_and_requires_exact_healthy_identity(tmp_path):
    identity = {
        "git_sha": "d" * 40,
        "image_digest": "sha256:" + "e" * 64,
        "config_generation": 4,
        "config_digest": "f" * 64,
        "policy_digest": "1" * 64,
        "target_sequence": 8,
    }
    client = ProtectedHostClient(
        socket_path=tmp_path / "host.sock",
        credential="host-synthetic",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "deployed_identity": identity,
                    "healthy": True,
                    "policy_digest": identity["policy_digest"],
                    "target_sequence": identity["target_sequence"],
                },
            )
        ),
    )

    observed = await client.current_state("production")

    assert observed.healthy is True
    assert observed.identity.git_sha == identity["git_sha"]


@pytest.mark.asyncio
async def test_protected_host_operation_uses_fixed_path_and_exact_typed_receipt(tmp_path):
    approval_id = "11111111-1111-4111-8111-111111111111"
    operation_id = "22222222-2222-4222-8222-222222222222"
    observed = {}

    def handler(request):
        observed["method"] = request.method
        observed["path"] = request.url.path
        observed["authorization"] = request.headers["authorization"]
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "operation_id": operation_id,
                "phase": "healthy",
                "receipt": "receipt-7",
                "payload_digest": "a" * 64,
            },
        )

    client = ProtectedHostClient(
        socket_path=tmp_path / "host.sock",
        credential="synthetic-host-credential",
        transport=httpx.MockTransport(handler),
    )

    result = await client.operate("deploy", UUID(approval_id), UUID(operation_id))

    assert result["phase"] == "healthy"
    assert observed == {
        "method": "POST",
        "path": "/v1/deploy",
        "authorization": "Bearer synthetic-host-credential",
        "body": {"approval_id": approval_id, "operation_id": operation_id},
    }


def test_unverified_oidc_claim_parser_rejects_duplicate_json_keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    header = encode(b'{"alg":"RS256","kid":"synthetic"}')
    claims = encode(b'{"run_id":"1","run_id":"2"}')
    signing_input = f"{header}.{claims}".encode("ascii")
    signature = key.sign(signing_input, PKCS1v15(), hashes.SHA256())
    token = f"{header}.{claims}.{encode(signature)}"

    with pytest.raises(BrokerHttpError) as error:
        broker_http._unverified_claims(token)

    assert error.value.code == "caller_denied"


def test_runtime_config_rejects_private_workflow_closure_paths(tmp_path):
    from chatcore_release_control.policy import _is_private_path

    assert _is_private_path(".env") is True
    config_file = tmp_path / "broker.json"
    secure_files = {}
    for name in ("dsn", "github-key", "host-in", "host-out"):
        path = tmp_path / name
        path.write_text("synthetic")
        path.chmod(0o600)
        secure_files[name] = str(path)
    config = {
        "version": 1,
        "database_dsn_file": secure_files["dsn"],
        "github_app_roles": {
            "publication": 10,
            "broker": 12,
            "runner_registration": 14,
        },
        "github_installation_id": 34,
        "github_repository_id": 101,
        "github_private_key_file": secure_files["github-key"],
        "host_inbound_credential_file": secure_files["host-in"],
        "host_client_credential_file": secure_files["host-out"],
        "host_socket": str(tmp_path / "host.sock"),
        "object_repository": str(tmp_path),
        "workflow_path": ".github/workflows/release.yml",
        "merge_runner_environment": "github-hosted",
        "relay_runner_environment": "self-hosted",
        "audience": "https://broker.invalid/release",
        "targets": ["production"],
        "workflow_blobs": {
            ".github/workflows/release.yml": "a" * 64,
            ".env": "b" * 64,
        },
        "uds_path": str(tmp_path / "broker.sock"),
    }
    config_file.write_text(json.dumps(config))
    config_file.chmod(0o600)

    with pytest.raises(BrokerHttpError) as error:
        BrokerRuntimeConfig.from_file(config_file)

    assert error.value.code == "runtime_not_configured"
