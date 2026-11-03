from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import chatcore_release_control.release_coordinator as release_coordinator_module
import httpx
import pytest
from chatcore_release_control.git_evidence import collect_release_bundle
from chatcore_release_control.github_evidence import GitHubEvidenceClient
from chatcore_release_control.merge_control import MergeIntent, MergeOutcome
from chatcore_release_control.oidc import CallerIdentity, OIDCPolicy
from chatcore_release_control.policy import (
    Change,
    DeploymentIdentity,
    PolicyManifest,
    ReleaseBundle,
    TrustedCheck,
)
from chatcore_release_control.release_coordinator import (
    ActionsOIDCTokenProvider,
    BrokerHttpClient,
    BrokerMergeCallbacks,
    CoordinatorError,
    GitHubJobCorrelationVerifier,
    OIDCCallerVerifier,
    ProtectedRuntimeConfig,
    execute_merge,
    main,
    parse_command,
)


def _identity() -> CallerIdentity:
    return CallerIdentity(7, 8, "a" * 40, 11, 2, 13, "github-hosted")


def _context(approval_id) -> dict[str, object]:
    workflow = ".github/workflows/release.yml"
    digest = "1" * 64
    policy = PolicyManifest(
        repository_id=7,
        owner_id=8,
        target="main",
        workflow_digests=((workflow, digest),),
        allowed_paths=("src/file.py",),
        protected_paths=(workflow,),
        checks=(TrustedCheck("trusted checks", 5, workflow, digest),),
    )
    deployment = DeploymentIdentity(
        git_sha="f" * 40,
        image_digest="sha256:" + "2" * 64,
        config_generation=1,
        config_digest="3" * 64,
        policy_digest=policy.digest,
        target_sequence=5,
    )
    review_bundle = {
        "complete_delta_reviewed": True,
        "review_id": "fixed-review",
        "check_evidence_digest": "6" * 64,
    }
    review_digest = hashlib.sha256(
        json.dumps(review_bundle, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "approval_id": str(approval_id),
        "repository": "acme/plugin",
        "repository_id": 7,
        "owner_id": 8,
        "pr_number": 6,
        "target": "main",
        "sequence": 5,
        "dispatch_fence": 9,
        "deployed_identity": {
            "git_sha": deployment.git_sha,
            "image_digest": deployment.image_digest,
            "config_generation": deployment.config_generation,
            "config_digest": deployment.config_digest,
            "policy_digest": deployment.policy_digest,
            "target_sequence": deployment.target_sequence,
        },
        "base_sha": "b" * 40,
        "head_sha": "c" * 40,
        "head_tree": "d" * 40,
        "policy_digest": policy.digest,
        "policy_manifest": {
            "version": 1,
            "repository_id": 7,
            "owner_id": 8,
            "target": "main",
            "workflow_digests": {workflow: digest},
            "allowed_paths": ["src/file.py"],
            "protected_paths": [workflow],
            "checks": [
                {
                    "name": "trusted checks",
                    "creator_app_id": 5,
                    "workflow_path": workflow,
                    "workflow_sha": digest,
                }
            ],
        },
        "review_bundle": review_bundle,
        "review_bundle_digest": review_digest,
        "workflow_sha": "a" * 40,
        "status": "queued",
        "merge_commit": None,
        "merge_tree": None,
        "merge_parents": None,
        "host_receipt": None,
        "merge_lease": None,
    }


def _runtime_environment(object_store: str) -> dict[str, str]:
    return {
        "RELEASE_CONTROL_BROKER_ORIGIN": "https://broker.example",
        "RELEASE_CONTROL_OIDC_AUDIENCE": "https://broker.example/release",
        "GITHUB_REPOSITORY": "acme/plugin",
        "GITHUB_REPOSITORY_ID": "7",
        "GITHUB_OWNER_ID": "8",
        "GITHUB_RUN_ID": "11",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_WORKFLOW_SHA": "a" * 40,
        "RELEASE_CONTROL_WORKFLOW_PATH": ".github/workflows/release.yml",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RELEASE_CONTROL_OBJECT_REPOSITORY": object_store,
        "RELEASE_CONTROL_WORKFLOW_BLOBS_JSON": json.dumps(
            {".github/workflows/release.yml": "1" * 64}
        ),
        "RELEASE_CONTROL_GITHUB_API_TOKEN": "ghs_synthetic_token",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://pipelines.actions.githubusercontent.com/00000000-0000-4000-8000-000000000001/_apis/pipelines/workflows/22/runs/11/attempts/2/idtoken?api-version=2.0",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-request-token",
        "GITHUB_JOB": "trusted-merge-controller",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
    }


def test_broker_client_uses_fixed_https_origin_and_bearer_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = BrokerHttpClient(
        "https://broker.example",
        token_provider=lambda: "secret-token",
        transport=httpx.MockTransport(handler),
    )
    assert client.get("/v1/health") == {"ok": True}
    assert seen[0].url == "https://broker.example/v1/health"
    assert seen[0].headers["authorization"] == "Bearer secret-token"
    assert client._http._trust_env is False
    assert client._http.follow_redirects is False
    client.close()


@pytest.mark.parametrize(
    "origin", ["http://broker.example", "https://broker.example/path", "https://u:p@broker.example"]
)
def test_broker_origin_rejects_non_origin_values(origin: str) -> None:
    with pytest.raises(CoordinatorError):
        BrokerHttpClient(
            origin,
            token_provider=lambda: "token",
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        )


def test_merge_callbacks_send_strict_anomaly_for_non_two_parent_merge() -> None:
    approval_id = uuid4()
    requests: list[tuple[str, dict[str, object]]] = []
    context = _context(approval_id)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        requests.append((request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json=context)
        if request.method == "POST" and request.url.path.endswith("/merge-lease"):
            return httpx.Response(
                200,
                json={
                    "lease_id": "00000000-0000-4000-8000-000000000001",
                    "dispatch_fence": 9,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                },
            )
        if request.method == "DELETE":
            return httpx.Response(200, json={"released": True})
        return httpx.Response(200, json={"recorded": True})

    client = BrokerHttpClient(
        "https://broker.example",
        token_provider=lambda: "oidc-jwt",
        transport=httpx.MockTransport(handler),
    )
    callbacks = BrokerMergeCallbacks(client, approval_id=approval_id)
    callbacks.get_context()
    callbacks.bind_caller(_identity())
    with callbacks.target_lock("main"):
        callbacks._active_lease["deadline"] = 0
        callbacks.persist_merge(
            MergeOutcome(
                "merged_blocked",
                "b" * 40,
                ("a" * 40, "c" * 40, "d" * 40),
                "e" * 40,
                "f" * 40,
                None,
                True,
            )
        )
    assert requests == [
        (f"/v1/releases/{approval_id}/merge-context", {}),
        (
            f"/v1/releases/{approval_id}/merge-lease",
            {"dispatch_fence": 9, "run_id": 11, "run_attempt": 2, "check_run_id": 13},
        ),
        (
            f"/v1/releases/{approval_id}/merge-anomalies",
            {
                "merge_commit": "b" * 40,
                "parents": ["a" * 40, "c" * 40, "d" * 40],
                "tree": "e" * 40,
                "expected_tree": "f" * 40,
                "reason": "unexpected_merge_observation",
                "dispatch_fence": 9,
                "lease_id": "00000000-0000-4000-8000-000000000001",
            },
        ),
        (
            f"/v1/releases/{approval_id}/merge-lease",
            {"lease_id": "00000000-0000-4000-8000-000000000001", "dispatch_fence": 9},
        ),
    ]
    client.close()


def test_broker_context_and_admission_bind_dispatch_fence_and_run_ids() -> None:
    approval_id = uuid4()
    paths: list[tuple[str, str, dict[str, object]]] = []
    context = _context(approval_id)
    lease_expiry = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        if request.method == "GET":
            return httpx.Response(200, json=context)
        if request.method == "DELETE":
            return httpx.Response(200, json={"released": True})
        if request.url.path.endswith("/merge-lease"):
            return httpx.Response(
                200,
                json={
                    "lease_id": "00000000-0000-4000-8000-000000000001",
                    "dispatch_fence": 9,
                    "expires_at": lease_expiry,
                },
            )
        if request.url.path.endswith("/merge-admission"):
            return httpx.Response(
                200,
                json={
                    "approval_id": str(approval_id),
                    "lease_id": "00000000-0000-4000-8000-000000000001",
                    "dispatch_fence": 9,
                    "expires_at": lease_expiry,
                    "admitted": True,
                    "may_start": True,
                    "deployed_identity": context["deployed_identity"],
                    "base_sha": context["base_sha"],
                    "head_sha": context["head_sha"],
                    "head_tree": context["head_tree"],
                    "policy_digest": context["policy_digest"],
                    "expected_merge_tree": "d" * 40,
                    "run_id": 11,
                    "run_attempt": 2,
                    "check_run_id": 13,
                },
            )
        return httpx.Response(200, json={"admitted": True})

    client = BrokerHttpClient(
        "https://broker.example",
        token_provider=lambda: "signed-oidc",
        transport=httpx.MockTransport(handler),
    )
    callbacks = BrokerMergeCallbacks(client, approval_id=approval_id)
    assert callbacks.get_context() == context
    caller = _identity()
    callbacks.bind_caller(caller)
    intent = MergeIntent(
        repository_id=7,
        owner_id=8,
        target="main",
        pr_number=6,
        deployed_commit="f" * 40,
        base_commit="b" * 40,
        head_commit="c" * 40,
        policy_digest=context["policy_digest"],
        expected_merge_tree="d" * 40,
        caller=caller,
        evidence=object(),
    )
    with callbacks.target_lock("main"):
        assert callbacks.admit_merge(intent) is True
        assert callbacks.admission_live() is True
    assert paths[0] == ("GET", f"/v1/releases/{approval_id}/merge-context", {})
    assert paths[1][0:2] == ("POST", f"/v1/releases/{approval_id}/merge-lease")
    assert paths[2] == (
        "POST",
        f"/v1/releases/{approval_id}/merge-admission",
        {
            "head_sha": "c" * 40,
            "dispatch_fence": 9,
            "run_id": 11,
            "run_attempt": 2,
            "check_run_id": 13,
            "lease_id": "00000000-0000-4000-8000-000000000001",
        },
    )
    assert paths[-1][0:2] == ("DELETE", f"/v1/releases/{approval_id}/merge-lease")
    client.close()


def test_current_state_compares_full_deployed_identity_not_only_git_sha() -> None:
    approval_id = uuid4()
    context = _context(approval_id)
    deployment = dict(context["deployed_identity"])
    response_state = {
        "deployed_identity": deployment,
        "deployed_commit": deployment["git_sha"],
        "policy_digest": deployment["policy_digest"],
    }
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=context if requests == 1 else response_state)

    client = BrokerHttpClient(
        "https://broker.example",
        token_provider=lambda: "signed-oidc",
        transport=httpx.MockTransport(handler),
    )
    callbacks = BrokerMergeCallbacks(client, approval_id=approval_id)
    callbacks.get_context()
    assert callbacks.current_deployed_commit("main") == "f" * 40
    client.close()

    response_state["deployed_identity"] = {**deployment, "config_generation": 2}
    requests = 0
    client = BrokerHttpClient(
        "https://broker.example",
        token_provider=lambda: "signed-oidc",
        transport=httpx.MockTransport(handler),
    )
    callbacks = BrokerMergeCallbacks(client, approval_id=approval_id)
    callbacks.get_context()
    with pytest.raises(CoordinatorError, match="caller_denied"):
        callbacks.current_deployed_commit("main")
    client.close()


def test_broker_rejects_duplicate_json_and_oversized_response() -> None:
    def duplicate(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"ok":true,"ok":false}')

    client = BrokerHttpClient(
        "https://broker.example",
        token_provider=lambda: "token",
        transport=httpx.MockTransport(duplicate),
    )
    with pytest.raises(CoordinatorError, match="broker_invalid_response"):
        client.get("/v1/health")
    client.close()

    client = BrokerHttpClient(
        "https://broker.example",
        token_provider=lambda: "token",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"{" + b" " * 1_048_576 + b"}")
        ),
    )
    with pytest.raises(CoordinatorError, match="broker_invalid_response"):
        client.get("/v1/health")
    client.close()


def test_oidc_caller_verifier_requires_workflow_and_job_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatcore_release_control import release_coordinator

    caller = _identity()
    witnessed: list[str] = []

    def fake_verify(token, keys, policy, *, verify_workflow, verify_job):
        assert token == "jwt"
        assert keys == {"keys": []}
        assert policy is not None
        assert verify_workflow(caller) is True
        assert verify_job(caller) is True
        witnessed.append("verified")
        return caller

    monkeypatch.setattr(release_coordinator, "verify_oidc", fake_verify)
    verifier = OIDCCallerVerifier(
        approval_id=uuid4(),
        token_provider=lambda: "jwt",
        jwks_provider=lambda: {"keys": []},
        policy_provider=lambda _: OIDCPolicy(
            audience="https://broker.example/release",
            repository_id=7,
            owner_id=8,
            repository="acme/plugin",
            workflow_path=".github/workflows/release.yml",
            workflow_sha="a" * 40,
            run_id=11,
            run_attempt=2,
            check_run_id=13,
            runner_environment="github-hosted",
            subject="repo:acme/plugin:ref:refs/heads/main",
        ),
        workflow_verifier=lambda _: True,
        job_verifier=lambda _: True,
    )
    assert verifier(caller) is True
    assert witnessed == ["verified"]


def test_actions_oidc_provider_uses_only_validated_issuer_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"value": "eyJhbGciOiJSUzI1NiJ9.payload.signature"})

    provider = ActionsOIDCTokenProvider(
        "https://pipelines.actions.githubusercontent.com/00000000-0000-4000-8000-000000000001/_apis/pipelines/workflows/22/runs/11/attempts/2/idtoken?api-version=2.0",
        "request-capability",
        audience="https://broker.example/release",
        transport=httpx.MockTransport(handler),
    )
    assert provider() == "eyJhbGciOiJSUzI1NiJ9.payload.signature"
    assert seen[0].url.host == "pipelines.actions.githubusercontent.com"
    assert seen[0].url.params["audience"] == "https://broker.example/release"
    assert seen[0].headers["authorization"] == "Bearer request-capability"
    assert "request-capability" not in repr(provider)
    provider.close()
    with pytest.raises(CoordinatorError):
        ActionsOIDCTokenProvider(
            "https://attacker.example/idtoken",
            "request-capability",
            audience="https://broker.example/release",
        )


def test_protected_runtime_config_uses_named_synthetic_environment() -> None:
    with tempfile.TemporaryDirectory() as object_store:
        environment = _runtime_environment(object_store)
        config = ProtectedRuntimeConfig.from_environment(environment)
        assert config.repository == "plugin"
        assert config.workflow_blobs == ((".github/workflows/release.yml", "1" * 64),)
        assert "synthetic" not in repr(config)
        assert main(["checkconfig"], environment=environment) == 0
    environment["GITHUB_REF"] = "refs/pull/1/merge"
    with pytest.raises(CoordinatorError, match="invalid_runtime_configuration"):
        ProtectedRuntimeConfig.from_environment(environment)


def test_dispatch_inputs_are_complete_and_bound_to_installed_runtime_request():
    with tempfile.TemporaryDirectory() as object_store:
        environment = _runtime_environment(object_store)
        environment.update(
            {
                "RELEASE_CONTROL_DISPATCH_ID": "33333333-3333-4333-8333-333333333333",
                "RELEASE_CONTROL_PR_NUMBER": "42",
                "RELEASE_CONTROL_HEAD_SHA": "c" * 40,
            }
        )
        config = ProtectedRuntimeConfig.from_environment(environment)
        assert config.dispatch_id == UUID(environment["RELEASE_CONTROL_DISPATCH_ID"])
        assert config.expected_pr_number == 42 and config.expected_head_sha == "c" * 40
        del environment["RELEASE_CONTROL_HEAD_SHA"]
        with pytest.raises(CoordinatorError):
            ProtectedRuntimeConfig.from_environment(environment)


def test_terminal_retry_returns_recorded_merge_and_host_receipt_without_github_calls() -> None:
    approval_id = uuid4()
    context = _context(approval_id)
    context.update(
        {
            "status": "healthy",
            "merge_commit": "9" * 40,
            "merge_tree": "8" * 40,
            "merge_parents": ["f" * 40, "c" * 40],
            "host_receipt": {
                "operation_id": str(uuid4()),
                "receipt_id": "receipt-123",
                "status": "healthy",
                "fence": 9,
            },
        }
    )
    broker_requests: list[str] = []
    github_requests: list[str] = []

    def broker_handler(request: httpx.Request) -> httpx.Response:
        broker_requests.append(request.url.path)
        assert request.headers["authorization"] == "Bearer synthetic.jwt.token"
        return httpx.Response(200, json=context)

    def oidc_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "pipelines.actions.githubusercontent.com"
        return httpx.Response(200, json={"value": "synthetic.jwt.token"})

    def github_handler(request: httpx.Request) -> httpx.Response:
        github_requests.append(request.url.path)
        raise AssertionError("terminal retry must not call GitHub")

    with tempfile.TemporaryDirectory() as object_store:
        outcome = execute_merge(
            approval_id,
            environment=_runtime_environment(object_store),
            broker_transport=httpx.MockTransport(broker_handler),
            github_transport=httpx.MockTransport(github_handler),
            oidc_transport=httpx.MockTransport(oidc_handler),
        )
    assert outcome.status == "healthy"
    assert outcome.merge_commit == "9" * 40
    assert outcome.host_receipt == (
        context["host_receipt"]["operation_id"],
        "receipt-123",
        "healthy",
        9,
    )
    assert broker_requests == [f"/v1/releases/{approval_id}/merge-context"]
    assert github_requests == []


def test_execute_merge_blocks_ordinary_unmerged_base_before_github_merge(tmp_path, monkeypatch):
    approval_id = uuid4()
    context = _context(approval_id)
    policy = PolicyManifest.from_json(json.dumps(context["policy_manifest"]))
    changed = Change("src/file.py")
    bundle = ReleaseBundle(
        deployed_commit=context["deployed_identity"]["git_sha"],
        base_commit=context["base_sha"],
        head_commit=context["head_sha"],
        deployed_tree="1" * 40,
        base_tree="2" * 40,
        head_tree=context["head_tree"],
        policy_digest=policy.digest,
        base_is_ancestor_of_head=True,
        base_is_descendant_of_deployed=False,
        same_tree_continuation_recorded=False,
        complete_delta_reviewed=False,
        deployed_to_base=(changed,),
        base_to_head=(),
        deployed_to_head=(changed,),
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
        "base_is_ancestor_of_head": True,
        "base_is_descendant_of_deployed": False,
        "same_tree_continuation_recorded": False,
        "deployed_to_base": [
            {
                "path": changed.path,
                "change_type": changed.change_type,
                "object_type": changed.object_type,
            }
        ],
        "base_to_head": [],
        "deployed_to_head": [
            {
                "path": changed.path,
                "change_type": changed.change_type,
                "object_type": changed.object_type,
            }
        ],
        "check_evidence_digest": "6" * 64,
    }
    context["review_bundle"] = review
    context["review_bundle_digest"] = hashlib.sha256(
        json.dumps(review, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    class FakeFetcher:
        def __init__(self, **_kwargs):
            pass

        def ensure_history(self, **_kwargs):
            return None

    class FakeJobVerifier:
        def __init__(self, *_args, **_kwargs):
            pass

        def __call__(self, _identity):
            return True

        def check_run_id_for_job(self, **_kwargs):
            return 13

    monkeypatch.setattr(release_coordinator_module, "GitObjectFetcher", FakeFetcher)
    monkeypatch.setattr(release_coordinator_module, "GitHubJobCorrelationVerifier", FakeJobVerifier)
    monkeypatch.setattr(
        release_coordinator_module, "collect_release_bundle", lambda *_args, **_kwargs: bundle
    )
    broker_requests = []

    def broker_handler(request: httpx.Request) -> httpx.Response:
        broker_requests.append(request.url.path)
        return httpx.Response(200, json=context)

    def github_handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("unknown ordinary base must fail before GitHub evidence or merge")

    with pytest.raises(CoordinatorError, match="caller_denied"):
        execute_merge(
            approval_id,
            environment=_runtime_environment(str(tmp_path)),
            broker_transport=httpx.MockTransport(broker_handler),
            github_transport=httpx.MockTransport(github_handler),
            oidc_transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"value": "synthetic.jwt.token"})
            ),
        )
    assert broker_requests == [f"/v1/releases/{approval_id}/merge-context"]


def test_execute_merge_passes_recorded_same_tree_continuation_to_local_gate(tmp_path, monkeypatch):
    repository = tmp_path / "objects"
    repository.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            capture_output=True,
            check=True,
        )
        return result.stdout.decode().strip()

    git("init", "-q")
    git("config", "user.name", "Synthetic")
    git("config", "user.email", "synthetic@example.invalid")
    (repository / ".github/workflows").mkdir(parents=True)
    (repository / ".github/workflows/release.yml").write_text("name: release\n")
    (repository / "src").mkdir()
    (repository / "src/file.py").write_text("baseline\n")
    git("add", "-A")
    git("commit", "-qm", "deployed")
    deployed = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "same-tree continuation")
    base = git("rev-parse", "HEAD")
    (repository / "src/file.py").write_text("candidate\n")
    git("add", "-A")
    git("commit", "-qm", "candidate")
    head = git("rev-parse", "HEAD")

    approval_id = uuid4()
    context = _context(approval_id)
    policy = PolicyManifest.from_json(json.dumps(context["policy_manifest"]))
    bundle = collect_release_bundle(
        repository,
        deployed,
        base,
        head,
        policy,
        same_tree_continuation_recorded=True,
    )
    assert bundle.base_is_ancestor_of_head
    assert bundle.base_is_descendant_of_deployed
    assert bundle.base_tree == bundle.deployed_tree
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
        "same_tree_continuation_recorded": True,
        "deployed_to_base": [],
        "base_to_head": [
            {
                "path": change.path,
                "change_type": change.change_type,
                "object_type": change.object_type,
            }
            for change in bundle.base_to_head
        ],
        "deployed_to_head": [
            {
                "path": change.path,
                "change_type": change.change_type,
                "object_type": change.object_type,
            }
            for change in bundle.deployed_to_head
        ],
        "check_evidence_digest": "6" * 64,
    }
    context["deployed_identity"]["git_sha"] = deployed
    context["base_sha"] = base
    context["head_sha"] = head
    context["head_tree"] = bundle.head_tree
    context["review_bundle"] = review
    context["review_bundle_digest"] = hashlib.sha256(
        json.dumps(review, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    class FakeFetcher:
        def __init__(self, **_kwargs):
            pass

        def ensure_history(self, **_kwargs):
            return None

    class FakeJobVerifier:
        def __init__(self, *_args, **_kwargs):
            pass

        def __call__(self, _identity):
            return True

        def check_run_id_for_job(self, **_kwargs):
            return 13

    class FakeResolver:
        def __init__(self, *_args, **_kwargs):
            pass

        def resolve(self, _pr_number, observed_bundle):
            assert observed_bundle.base_is_descendant_of_deployed
            assert observed_bundle.same_tree_continuation_recorded
            return SimpleNamespace(
                base_commit=base,
                head_commit=head,
                head_tree=bundle.head_tree,
                synthetic_merge_tree="7" * 40,
            )

    class FakeCoordinator:
        def __init__(self):
            self.context = context

        def merge(self, snapshot, reviewed_bundle, *, caller):
            assert snapshot.head_commit == head
            assert reviewed_bundle.same_tree_continuation_recorded
            return "same-tree ordinary merge reached controller"

    monkeypatch.setattr(release_coordinator_module, "GitObjectFetcher", FakeFetcher)
    monkeypatch.setattr(release_coordinator_module, "GitHubJobCorrelationVerifier", FakeJobVerifier)
    monkeypatch.setattr(release_coordinator_module, "GitHubEvidenceResolver", FakeResolver)
    monkeypatch.setattr(
        release_coordinator_module,
        "evidence_digest",
        lambda _snapshot: "6" * 64,
    )
    monkeypatch.setattr(
        release_coordinator_module,
        "build_merge_coordinator",
        lambda *_args, **_kwargs: FakeCoordinator(),
    )

    def broker_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=context)

    def github_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/git/ref/heads/main")
        return httpx.Response(200, json={"object": {"sha": base}})

    result = execute_merge(
        approval_id,
        environment=_runtime_environment(str(repository)),
        broker_transport=httpx.MockTransport(broker_handler),
        github_transport=httpx.MockTransport(github_handler),
        oidc_transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"value": "synthetic.jwt.token"})
        ),
    )
    assert result == "same-tree ordinary merge reached controller"


def test_merge_authorized_retry_requires_reconciliation_and_never_remerges() -> None:
    approval_id = uuid4()
    context = _context(approval_id)
    context["status"] = "merge_authorized"

    def broker_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=context)

    def oidc_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": "synthetic.jwt.token"})

    def github_handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("uncertain admission must not start another API merge")

    with tempfile.TemporaryDirectory() as object_store:
        with pytest.raises(CoordinatorError, match="reconciliation_required"):
            execute_merge(
                approval_id,
                environment=_runtime_environment(object_store),
                broker_transport=httpx.MockTransport(broker_handler),
                github_transport=httpx.MockTransport(github_handler),
                oidc_transport=httpx.MockTransport(oidc_handler),
            )


def test_merge_authorized_reconciliation_reuses_expired_lease_for_result_only() -> None:
    approval_id = uuid4()
    context = _context(approval_id)
    context["status"] = "merge_authorized"
    context["merge_lease"] = {
        "lease_id": "00000000-0000-4000-8000-000000000001",
        "fence": 9,
        "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "admitted_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
        "run_id": 11,
        "run_attempt": 2,
        "check_run_id": 13,
        "may_start": False,
    }
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        requests.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json=context)
        return httpx.Response(200, json={"recorded": True})

    client = BrokerHttpClient(
        "https://broker.example",
        token_provider=lambda: "signed-oidc",
        transport=httpx.MockTransport(handler),
    )
    callbacks = BrokerMergeCallbacks(client, approval_id=approval_id)
    callbacks.get_context()
    callbacks.bind_caller(_identity())
    with callbacks.target_lock("main"):
        callbacks.persist_merge(
            MergeOutcome(
                "merged_blocked",
                "9" * 40,
                ("a" * 40, "b" * 40, "c" * 40),
                "8" * 40,
                "7" * 40,
                None,
                True,
            )
        )
    assert [path for _, path, _ in requests] == [
        f"/v1/releases/{approval_id}/merge-context",
        f"/v1/releases/{approval_id}/merge-anomalies",
    ]
    assert requests[1][2]["lease_id"] == "00000000-0000-4000-8000-000000000001"
    client.close()


def test_job_verifier_binds_exact_attempt_and_check_run() -> None:
    requests: list[str] = []
    run = {
        "id": 11,
        "run_attempt": 2,
        "repository": {"id": 7},
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": "a" * 40,
        "path": ".github/workflows/release.yml@refs/heads/main",
    }
    job = {
        "id": 19,
        "run_id": 11,
        "run_attempt": 2,
        "status": "completed",
        "conclusion": "success",
        "check_run_url": "https://api.github.com/repos/acme/plugin/check-runs/13",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/attempts/2"):
            return httpx.Response(200, json=run)
        if request.url.path.endswith("/attempts/2/jobs"):
            return httpx.Response(200, json={"total_count": 1, "jobs": [job]})
        return httpx.Response(404, json={})

    github = GitHubEvidenceClient(
        owner="acme",
        repository="plugin",
        repository_id=7,
        token_provider=lambda: "app-token",
        transport=httpx.MockTransport(handler),
    )
    verifier = GitHubJobCorrelationVerifier(
        github,
        workflow_path=".github/workflows/release.yml",
        runner_environment="github-hosted",
    )
    assert verifier(_identity()) is True
    assert requests == [
        "/repos/acme/plugin/actions/runs/11/attempts/2",
        "/repos/acme/plugin/actions/runs/11/attempts/2/jobs",
    ]
    job["run_attempt"] = 1
    assert verifier(_identity()) is False
    github.close()


def test_job_lookup_derives_live_check_run_for_p07_caller_policy() -> None:
    run = {
        "id": 11,
        "run_attempt": 2,
        "repository": {"id": 7},
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": "a" * 40,
        "path": ".github/workflows/release.yml@refs/heads/main",
    }
    job = {
        "id": 19,
        "name": "trusted-merge-controller",
        "run_id": 11,
        "run_attempt": 2,
        "status": "in_progress",
        "conclusion": None,
        "check_run_url": "https://api.github.com/repos/acme/plugin/check-runs/13",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer app-token"
        if request.url.path.endswith("/attempts/2/jobs"):
            return httpx.Response(200, json={"total_count": 1, "jobs": [job]})
        return httpx.Response(200, json=run)

    github = GitHubEvidenceClient(
        owner="acme",
        repository="plugin",
        repository_id=7,
        token_provider=lambda: "app-token",
        transport=httpx.MockTransport(handler),
    )
    verifier = GitHubJobCorrelationVerifier(
        github,
        workflow_path=".github/workflows/release.yml",
        runner_environment="github-hosted",
    )
    assert (
        verifier.check_run_id_for_job(
            run_id=11,
            run_attempt=2,
            workflow_sha="a" * 40,
            job_name="trusted-merge-controller",
        )
        == 13
    )
    github.close()


@pytest.mark.parametrize(
    "argv",
    [
        ["merge-approved-release", "--approval-id", "../../bad"],
        ["merge-approved-release", "--approval-id", str(uuid4()), "--host", "evil.example"],
        ["merge-approved-release", "--approval-id", str(uuid4()), "--command", "echo bad"],
    ],
)
def test_cli_accepts_only_approval_id(argv: list[str]) -> None:
    with pytest.raises(CoordinatorError):
        parse_command(argv)
