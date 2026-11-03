from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import chatcore_release_control.broker_http as broker_http_module
import httpx
import pytest
from chatcore_release_control.broker import (
    BrokerError,
    BrokerStore,
    VerifiedAdmissionEvidence,
    VerifiedMergeObservation,
)
from chatcore_release_control.broker_http import (
    BrokerApplication,
    BrokerRuntimeConfig,
    HostObservation,
    OIDCJobAuthenticator,
    ProtectedHostClient,
    ReleaseEvidenceVerifier,
)
from chatcore_release_control.development import DevelopmentController
from chatcore_release_control.generation_service import execute_generation
from chatcore_release_control.host_journal import HostJournal
from chatcore_release_control.host_service import HostService, P01HostEvidence
from chatcore_release_control.host_service_http import BrokerHostClient, HostServiceASGI
from chatcore_release_control.ledger import (
    Decision,
    ReleaseBinding,
    ReleaseLedger,
    ReleaseStatus,
    RunAttemptBinding,
)
from chatcore_release_control.policy import DeploymentIdentity, JournalState
from chatcore_release_control.publication import PublicationService, ScanVerdict
from chatcore_release_control.publication_service import (
    PublicationGatewayService,
    PublicationGatewayStore,
)

sys.path.insert(0, str(Path(__file__).parent))
pytest_plugins = ("test_broker",)
from test_broker import _policy, _policy_value, _seed_target  # noqa: E402
from test_broker_http import DISPATCH_ID, _jwks, _token, _transport  # noqa: E402
from test_ledger import create_card, make_command  # noqa: E402
from test_publication_service import _GitHub  # noqa: E402

_BROKER_TOKEN = "synthetic-host-broker-credential-32bytes"
_HOST_TOKEN = "synthetic-broker-host-credential-32bytes"


def _git(repo: Path, *args: str, input: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(repo.parent),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        },
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError("temporary Git fixture command failed")
    return result.stdout.decode("ascii").strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str, str, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Synthetic")
    _git(repository, "config", "user.email", "synthetic@example.invalid")
    workflow = repository / ".github/workflows/release.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: synthetic\n", encoding="utf-8")
    plugin = repository / "packages/plugin/src/plugin.py"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("value = 'deployed'\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "deployed")
    deployed = _git(repository, "rev-parse", "HEAD")
    _git(repository, "commit", "--allow-empty", "-m", "main base")
    base = _git(repository, "rev-parse", "HEAD")
    plugin.write_text("value = 'candidate'\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "candidate")
    head = _git(repository, "rev-parse", "HEAD")
    head_tree = _git(repository, "rev-parse", f"{head}^{{tree}}")
    merge = _git(
        repository,
        "commit-tree",
        head_tree,
        "-p",
        base,
        "-p",
        head,
        input=b"synthetic merge\n",
    )
    _git(repository, "update-ref", "refs/heads/synthetic-merge", merge)
    return repository, deployed, base, head, head_tree, merge


class _Source:
    def __init__(self, repository: Path):
        self.repository = repository

    @contextmanager
    def fetch(self, _commit: str, _baseline: str):
        yield self.repository


class _Builder:
    def build(self, _repository: Path, _context) -> str:
        return "sha256:" + "8" * 64


class _Runtime:
    def __init__(self, identity: DeploymentIdentity):
        self.actual = identity
        self.switch_count = 0

    def observe(self):
        return self.actual

    def switch(self, identity: DeploymentIdentity) -> None:
        self.switch_count += 1
        self.actual = identity

    def restore(self, identity: DeploymentIdentity) -> None:
        self.actual = identity

    def healthy(self, identity: DeploymentIdentity) -> bool:
        return self.observe() == identity

    def smoke_test(self, identity: DeploymentIdentity) -> bool:
        return self.observe() == identity


class _GenerationProvider:
    async def complete(self, *_args, **_kwargs):
        return SimpleNamespace(
            stop_reason="end_turn",
            message=SimpleNamespace(
                tool_calls=(),
                text=json.dumps(
                    {"files": {"packages/plugin/src/plugin.py": "value = 'generated'\n"}}
                ),
            ),
        )


class _GenerationRunner:
    max_output_bytes = 1024 * 1024
    max_input_bytes = 256 * 1024
    worker_manifest_digest = "b" * 64

    def __init__(self, base: str, policy_digest: str):
        self.baseline_commit = base
        self.policy_digest = policy_digest

    def run(self, _payload: bytes) -> bytes:
        return json.dumps(
            {
                "version": 2,
                "base_commit": self.baseline_commit,
                "policy_digest": self.policy_digest,
                "worker_manifest_digest": self.worker_manifest_digest,
                "execution_kind": "monorepo-pytest-v1",
                "exit_status": 0,
                "output": "synthetic tests passed",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()


class _CurrentHost:
    def __init__(self, identity: DeploymentIdentity):
        self.identity = identity

    async def current_state(self, target: str) -> HostObservation:
        return HostObservation(self.identity, True, self.identity.policy_digest, 1)


class _LocalGitHub(_GitHub):
    def __init__(self, store, verification_repo: Path):
        super().__init__(store)
        self.verification_repo = verification_repo

    def publish(self, command):
        result = subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.verification_repo),
                "-c",
                "protocol.file.allow=always",
                "fetch",
                "--no-tags",
                str(command.quarantine_git_dir),
                command.frozen_commit,
            ],
            env={"PATH": os.environ["PATH"], "HOME": str(self.verification_repo.parent)},
            capture_output=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            raise AssertionError("local Git publication fixture failed")
        return super().publish(command)


@pytest.mark.asyncio
async def test_generated_candidate_becomes_immutable_card_and_atomic_approval(
    broker_pool, tmp_path
):
    repository, deployed, base, _head, _tree, _merge = _repo(tmp_path)
    policy = _policy()
    baseline = DeploymentIdentity(
        deployed,
        "sha256:" + "e" * 64,
        4,
        "f" * 64,
        policy.digest,
        1,
    )
    publication_store = PublicationGatewayStore(tmp_path / "publication.sqlite3")
    verification_repo = tmp_path / "published-objects.git"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", "--bare", str(repository), str(verification_repo)],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        capture_output=True,
        check=True,
        timeout=10,
    )
    github = _LocalGitHub(publication_store, verification_repo)
    publisher = PublicationService(
        policy=policy,
        scanner=lambda _item: ScanVerdict.CLEAN,
        scanner_identity_sha256="b" * 64,
        baseline_history_scan_complete=True,
        record_provenance=publication_store,
        publish_transport=github.publish,
    )
    policy_raw = json.dumps(_policy_value(), sort_keys=True, separators=(",", ":")).encode()
    gateway = PublicationGatewayService(
        publisher=publisher,
        github=github,
        store=publication_store,
        repository_path=repository,
        policy=policy,
        policy_manifest_json=policy_raw,
        repository="acme/plugin",
        repository_id=policy.repository_id,
        owner_id=policy.owner_id,
    )
    github._api_json = lambda _method, _path: {
        "ref": "refs/heads/main",
        "object": {"type": "commit", "sha": base},
    }
    runner = _GenerationRunner(base, policy.digest)
    development = DevelopmentController(
        provider=_GenerationProvider(),
        installed_policy=policy,
        runner=runner,
        report_scanner=lambda _report: True,
        base_commit_verifier=lambda candidate_base, digest: (
            candidate_base == base and digest == policy.digest
        ),
    )
    response = await execute_generation(
        {
            "request_id": "p10-generated",
            "specification": "Update the synthetic plugin value.",
            "synthetic_examples": [{"input": "safe", "output": "safe"}],
            "title": "Generated synthetic update",
            "body": "Generated locally for integration proof.",
        },
        gateway=gateway,
        development=development,
        host=_CurrentHost(baseline),
    )
    context = publication_store.get_context(response["pull_request_number"])
    assert response["head_sha"] == context["head_commit"]
    assert publication_store.get_provenance("p10-generated") is not None

    from chatcore_release_control.git_evidence import collect_release_bundle

    bundle = collect_release_bundle(
        verification_repo,
        deployed,
        base,
        response["head_sha"],
        policy,
        same_tree_continuation_recorded=True,
    )
    review = {
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
        "deployed_to_base": [asdict(change) for change in bundle.deployed_to_base],
        "base_to_head": [asdict(change) for change in bundle.base_to_head],
        "deployed_to_head": [asdict(change) for change in bundle.deployed_to_head],
        "complete_delta_reviewed": True,
        "check_evidence_digest": "6" * 64,
    }
    binding = ReleaseBinding(
        repository_id=policy.repository_id,
        owner_id=policy.owner_id,
        pr_number=response["pull_request_number"],
        target=policy.target,
        base_sha=base,
        head_sha=response["head_sha"],
        head_tree=bundle.head_tree,
        deployed_identity=baseline,
        policy_digest=policy.digest,
        review_bundle=review,
    )
    ledger = ReleaseLedger(broker_pool)
    now = datetime.now(UTC)
    card = await create_card(
        ledger,
        binding=binding,
        owner_user_id=7,
        chat_id=7,
        message_id=7002,
        now=now,
    )
    await _seed_target(broker_pool, identity=baseline)
    await ledger.record_decision(
        make_command(
            card,
            decision=Decision.APPROVE,
            owner_user_id=7,
            chat_id=7,
            message_id=7002,
        ),
        now=now + timedelta(seconds=1),
    )
    persisted_card = await ledger.get_card(card.approval_id)
    assert persisted_card.binding["head_sha"] == response["head_sha"]
    assert persisted_card.status is ReleaseStatus.QUEUED
    assert (
        await broker_pool.fetchval(
            "SELECT state FROM release_control.decision_outbox WHERE approval_id=$1",
            card.approval_id,
        )
        == "pending"
    )


@pytest.mark.asyncio
async def test_real_git_merge_runs_through_postgres_and_host_journal_to_healthy_receipt(
    broker_pool, tmp_path, monkeypatch
):
    repository, deployed, base, head, head_tree, merge = _repo(tmp_path)
    policy = _policy()
    baseline = DeploymentIdentity(
        deployed,
        "sha256:" + "e" * 64,
        4,
        "f" * 64,
        policy.digest,
        1,
    )
    from chatcore_release_control.git_evidence import collect_release_bundle

    bundle = collect_release_bundle(
        repository,
        deployed,
        base,
        head,
        policy,
        same_tree_continuation_recorded=True,
    )
    review = {
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
        "deployed_to_base": [asdict(change) for change in bundle.deployed_to_base],
        "base_to_head": [asdict(change) for change in bundle.base_to_head],
        "deployed_to_head": [asdict(change) for change in bundle.deployed_to_head],
        "complete_delta_reviewed": True,
        "check_evidence_digest": "6" * 64,
    }
    binding = ReleaseBinding(
        repository_id=policy.repository_id,
        owner_id=policy.owner_id,
        pr_number=42,
        target=policy.target,
        base_sha=base,
        head_sha=head,
        head_tree=head_tree,
        deployed_identity=baseline,
        policy_digest=policy.digest,
        review_bundle=review,
    )
    ledger = ReleaseLedger(broker_pool)
    issued = datetime.now(UTC)
    card = await create_card(
        ledger,
        binding=binding,
        owner_user_id=7,
        chat_id=7,
        message_id=7001,
        now=issued,
    )
    await _seed_target(broker_pool, identity=baseline)
    await ledger.record_decision(
        make_command(
            card,
            decision=Decision.APPROVE,
            owner_user_id=7,
            chat_id=7,
        ),
        now=issued + timedelta(seconds=1),
    )
    claim = await ledger.claim_next_dispatch(
        card.target, "p10-worker", now=issued + timedelta(seconds=2)
    )
    assert claim is not None
    await ledger.acknowledge_dispatch(
        card.approval_id, fence=claim.fence, now=issued + timedelta(seconds=3)
    )

    store = BrokerStore(broker_pool)
    dispatch_id = DISPATCH_ID
    await broker_pool.execute(
        """INSERT INTO release_control.broker_dispatch_intent
           (approval_id,dispatch_fence,dispatch_id,request_digest,state,created_at,updated_at)
           VALUES ($1,$2,$3,$4,'accepted',$5,$5)""",
        card.approval_id,
        claim.fence,
        dispatch_id,
        b"d" * 32,
        issued + timedelta(seconds=3),
    )
    run = RunAttemptBinding(
        repository_id=policy.repository_id,
        run_id=5001,
        attempt=1,
        workflow_ref="acme/plugin/.github/workflows/release.yml@refs/heads/main",
        workflow_sha="a" * 40,
        check_run_id=6001,
    )
    lease = await store.acquire_merge_lease(
        card.approval_id, dispatch_fence=claim.fence, run=run, now=issued + timedelta(seconds=4)
    )
    admission = VerifiedAdmissionEvidence(
        deployed_identity=baseline,
        base_sha=base,
        head_sha=head,
        head_tree=head_tree,
        current_main_sha=base,
        expected_merge_tree=head_tree,
        evidence_digest="6" * 64,
        complete_delta_reviewed=True,
    )
    await store.admit_merge(
        card.approval_id,
        dispatch_fence=claim.fence,
        lease_id=lease.lease_id,
        run=run,
        evidence=admission,
        now=issued + timedelta(seconds=5),
    )
    merge_tree = _git(repository, "rev-parse", f"{merge}^{{tree}}")
    actual_parents = tuple(_git(repository, "show", "-s", "--format=%P", merge).split())
    observation = VerifiedMergeObservation(
        commit=merge,
        parents=actual_parents,
        tree=merge_tree,
        expected_tree=head_tree,
        observed_at=issued + timedelta(seconds=6),
    )
    await store.record_merge_observation(
        card.approval_id,
        dispatch_fence=claim.fence,
        lease_id=lease.lease_id,
        run=run,
        observation=observation,
        now=issued + timedelta(seconds=7),
    )
    assert actual_parents == (base, head)

    broker_ref = {}
    broker_calls = []
    loop = asyncio.get_running_loop()

    def github_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/plugin/git/ref/heads/main":
            return httpx.Response(
                200,
                json={"ref": "refs/heads/main", "object": {"type": "commit", "sha": merge}},
            )
        return httpx.Response(404, json={})

    github_transport = httpx.MockTransport(github_handler)
    credential_file = tmp_path / "broker-host.token"
    credential_file.write_text(_BROKER_TOKEN, encoding="ascii")
    credential_file.chmod(0o600)
    broker_http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: _call_broker_from_host_thread(request, broker_ref, loop, broker_calls)
        ),
        base_url="http://release-broker",
        trust_env=False,
        follow_redirects=False,
    )
    host_broker = BrokerHostClient(
        socket_path=tmp_path / "broker.sock",
        credential_file=credential_file,
        client=broker_http_client,
    )
    runtime = _Runtime(baseline)
    host_service = HostService(
        source=_Source(repository),
        target=policy.target,
        policy_digest=policy.digest,
        journal=HostJournal(tmp_path / "host-journal.json"),
        broker=host_broker,
        evidence=P01HostEvidence(policy),
        builder=_Builder(),
        runtime=runtime,
        host_lock_path=tmp_path / "host-effect.lock",
        now=lambda: int(datetime.now(UTC).timestamp()),
    )
    host_app = HostServiceASGI(host_service)
    job_auth = OIDCJobAuthenticator(
        audience="https://synthetic.invalid/broker",
        workflow_path=".github/workflows/release.yml",
        merge_runner_environment="github-hosted",
        relay_runner_environment="self-hosted",
        object_repository=repository,
        workflow_blobs={".github/workflows/release.yml": "a" * 64},
        dispatch_id_provider=store.get_active_dispatch_id,
        github_token_provider=lambda: "synthetic-github-token",
        github_transport=github_transport,
    )
    config = BrokerRuntimeConfig(
        config_path=tmp_path / "broker-config.json",
        database_dsn_file=tmp_path / "dsn",
        github_app_roles=(("publication", 10), ("broker", 12), ("runner_registration", 14)),
        github_installation_id=34,
        github_repository_id=policy.repository_id,
        github_private_key_file=tmp_path / "app.pem",
        host_inbound_credential_file=tmp_path / "host-inbound",
        host_client_credential_file=credential_file,
        host_socket=tmp_path / "host.sock",
        object_repository=repository,
        workflow_path=".github/workflows/release.yml",
        merge_runner_environment="github-hosted",
        relay_runner_environment="self-hosted",
        audience="https://synthetic.invalid/broker",
        targets=(policy.target,),
        workflow_blobs=((".github/workflows/release.yml", "a" * 64),),
        uds_path=tmp_path / "broker.sock",
    )
    broker_app = BrokerApplication(
        store=store,
        config=config,
        job_authenticator=job_auth,
        host_client=ProtectedHostClient(
            socket_path=tmp_path / "host.sock",
            credential=_HOST_TOKEN,
            transport=httpx.ASGITransport(app=host_app),
        ),
        evidence_verifier=ReleaseEvidenceVerifier(
            object_repository=repository,
            workflow_blobs=dict(config.workflow_blobs),
            github_transport=github_transport,
            github_token_provider=lambda: "synthetic-github-token",
        ),
        host_inbound_credential=_BROKER_TOKEN,
        github_token_provider=lambda: "synthetic-github-token",
        github_transport=github_transport,
    )
    broker_ref["app"] = broker_app
    operation_id = uuid4()
    body = json.dumps(
        {"approval_id": str(card.approval_id), "operation_id": str(operation_id)}
    ).encode("ascii")
    path = "/v1/deploy"

    lost_status, lost_response = await _host_request_losing_response(host_app, path, body)
    try:
        lost_record = HostJournal(tmp_path / "host-journal.json").get_record(str(operation_id))
    except Exception:
        lost_record = None
    assert lost_status == 200, (lost_response, lost_record)

    status, response = await _asgi_request(host_app, "POST", path, body)
    assert status == 200, (
        response,
        [(method, path, code, value.get("error")) for method, path, code, value in broker_calls],
    )
    assert response["phase"] == JournalState.HEALTHY.value
    saved = await store.get_release_context(card.approval_id)
    assert saved.status is ReleaseStatus.HEALTHY
    assert runtime.actual.git_sha == merge
    assert runtime.switch_count == 1
    assert (
        HostJournal(tmp_path / "host-journal.json").get_record(str(operation_id)).phase
        is JournalState.HEALTHY
    )

    # Exact replay returns the terminal receipt without repeating the runtime effect.
    status, replay = await _asgi_request(host_app, "POST", path, body)
    assert status == 200 and replay == response
    assert runtime.switch_count == 1

    # A later queued card advances the target's sequence and clears the active slot.
    second_card = await create_card(
        ledger,
        binding=binding,
        owner_user_id=7,
        chat_id=7,
        message_id=7003,
        now=issued + timedelta(seconds=20),
    )
    assert second_card.sequence > card.sequence
    with pytest.raises(BrokerError):
        await store.get_active_dispatch_id(card.approval_id, claim.fence)
    assert await store.get_dispatch_id_for_job(card.approval_id, claim.fence) == dispatch_id
    with pytest.raises(BrokerError):
        await store.get_dispatch_id_for_job(second_card.approval_id, second_card.sequence)

    # The original signed run can still read its terminal context after target advancement.
    # It cannot regain a merge or switch admission through this read-only endpoint.
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(broker_http_module, "verify_installed_blob_digests", lambda *_args: None)
    auth = OIDCJobAuthenticator(
        audience="https://broker.invalid/release",
        workflow_path=".github/workflows/release.yml",
        merge_runner_environment="github-hosted",
        relay_runner_environment="self-hosted",
        object_repository=repository,
        workflow_blobs={".github/workflows/release.yml": "a" * 64},
        dispatch_id_provider=store.get_dispatch_id_for_job,
        github_token_provider=lambda: "synthetic-github-token",
        github_transport=_transport(display_title=f"release:{dispatch_id}"),
        jwks_transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=_jwks(key))),
    )
    broker_app._job_authenticator = auth
    signed_token = _token(key, now=int(datetime.now(UTC).timestamp()))
    status, terminal_context = await _asgi_request(
        broker_app,
        "GET",
        f"/v1/releases/{card.approval_id}/merge-context",
        headers=[(b"authorization", f"Bearer {signed_token}".encode("ascii"))],
    )
    assert status == 200 and terminal_context["dispatch_fence"] == claim.fence

    # A different server-observed run identity carrying the same approval cannot borrow
    # the historical dispatch id to get context for this release.
    altered_claims = jwt.decode(signed_token, options={"verify_signature": False})
    altered_claims["run_id"] = "5002"
    unrelated_run = jwt.encode(
        altered_claims, key, algorithm="RS256", headers={"kid": "broker-test"}
    )
    status, _ = await _asgi_request(
        broker_app,
        "GET",
        f"/v1/releases/{card.approval_id}/merge-context",
        headers=[(b"authorization", f"Bearer {unrelated_run}".encode("ascii"))],
    )
    assert status != 200

    host_broker.close()
    await broker_app.close()


def _call_broker_from_host_thread(request, broker_ref, loop, calls):
    app = broker_ref["app"]
    future = asyncio.run_coroutine_threadsafe(
        _asgi_request(
            app,
            request.method,
            request.url.path,
            request.content,
            headers=[(key.lower(), value) for key, value in request.headers.raw],
        ),
        loop,
    )
    try:
        status, value = future.result(timeout=15)
    except Exception:
        calls.append((request.method, request.url.path, 503, "broker_unavailable"))
        return httpx.Response(503, json={"error": "broker_unavailable"})
    calls.append((request.method, request.url.path, status, value))
    return httpx.Response(status, json=value)


async def _asgi_request(app, method, path, body=b"", headers=()):
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

    request_headers = list(headers)
    existing = {key.lower() for key, _ in request_headers}
    if body:
        if b"content-type" not in existing:
            request_headers.append((b"content-type", b"application/json"))
        if b"content-length" not in existing:
            request_headers.append((b"content-length", str(len(body)).encode("ascii")))
    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": request_headers,
            "query_string": b"",
        },
        receive,
        send,
    )
    return sent[0]["status"], json.loads(sent[1]["body"])


async def _host_request_losing_response(app, path, body):
    delivered = False
    status = None
    response_body = b""

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        nonlocal status, response_body
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            response_body = message.get("body", b"")
            raise ConnectionError("synthetic reply loss")

    try:
        await app(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
                "query_string": b"",
            },
            receive,
            send,
        )
    except ConnectionError:
        return status, json.loads(response_body)
    raise AssertionError("synthetic response was not lost")
