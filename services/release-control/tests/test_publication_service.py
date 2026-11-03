from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import chatcore_release_control.hosted_bootstrap as hosted_bootstrap
import httpx
import pytest
from chatcore_release_control.development import (
    DevelopmentController,
    GenerationRequest,
)
from chatcore_release_control.policy import PolicyManifest
from chatcore_release_control.publication import (
    ProvenanceRecord,
    PublicationService,
    PublishCommand,
    PublishResult,
    ScanInput,
    ScanSurface,
    ScanVerdict,
)
from chatcore_release_control.publication_github import PullRequestReceipt
from chatcore_release_control.publication_service import (
    ProtectedPublicationConfig,
    PublicationGatewayASGI,
    PublicationGatewayError,
    PublicationGatewayService,
    PublicationGatewayStore,
    build_publication_gateway,
)
from chatcore_release_control.publication_service import (
    main as publication_main,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPOSITORY = "acme/plugin"
REPOSITORY_ID = 101
OWNER_ID = 202
DIGEST = "a" * 64


def test_publication_module_is_an_actual_cli_and_help_requires_no_protected_config():
    result = subprocess.run(
        [sys.executable, "-m", "chatcore_release_control.publication_service", "--help"],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0 and b"checkconfig" in result.stdout and b"serve" in result.stdout


def _policy():
    return PolicyManifest.from_json(
        json.dumps(
            {
                "version": 1,
                "repository_id": REPOSITORY_ID,
                "owner_id": OWNER_ID,
                "target": "production",
                "workflow_digests": {".github/workflows/release.yml": DIGEST},
                "allowed_paths": ["src/plugin.py"],
                "protected_paths": [".github/workflows/release.yml"],
                "checks": [
                    {
                        "name": "candidate-tests",
                        "creator_app_id": 303,
                        "workflow_path": ".github/workflows/release.yml",
                        "workflow_sha": DIGEST,
                    }
                ],
            }
        )
    )


def _policy_json(policy: PolicyManifest):
    return {
        "version": policy.version,
        "repository_id": policy.repository_id,
        "owner_id": policy.owner_id,
        "target": policy.target,
        "workflow_digests": dict(policy.workflow_digests),
        "allowed_paths": list(policy.allowed_paths),
        "protected_paths": list(policy.protected_paths),
        "checks": [
            {
                "name": item.name,
                "creator_app_id": item.creator_app_id,
                "workflow_path": item.workflow_path,
                "workflow_sha": item.workflow_digest,
            }
            for item in policy.checks
        ],
    }


def _context(record: ProvenanceRecord, pull_number=73):
    policy = _policy()
    value = {
        "version": 1,
        "request_id": record.request_id,
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "owner_id": OWNER_ID,
        "pull_request_number": pull_number,
        "base_branch": "main",
        "deployed_commit": record.deployed_commit,
        "base_commit": record.base_commit,
        "head_commit": record.head_commit,
        "head_tree": "3" * len(record.head_commit),
        "policy_digest": record.policy_digest,
        "policy_manifest": _policy_json(policy),
        "provenance": asdict(record),
        "published_ref": f"refs/heads/clawdot/{record.request_id}",
        "specification": "Synthetic specification.",
        "synthetic_examples": [{"input": "synthetic"}],
        "pull_request_title": "Synthetic title",
        "pull_request_body": "Synthetic body",
        "artifacts": [],
    }
    value["context_sha256"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return value


def _git(repo: Path, *args: str) -> str:
    return (
        subprocess.check_output(
            ["git", "-C", str(repo), *args],
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(repo.parent),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            },
            stderr=subprocess.PIPE,
        )
        .decode("ascii")
        .strip()
    )


def _repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Synthetic")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    path = repo / "src" / "plugin.py"
    path.parent.mkdir()
    path.write_text("value = 'deployed'\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "deployed")
    deployed = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "--allow-empty", "-m", "main base")
    base = _git(repo, "rev-parse", "HEAD")
    policy = _policy()
    return repo, deployed, base, policy


class _Provider:
    def __init__(self):
        self.calls = 0

    async def complete(self, *_args, **_kwargs):
        self.calls += 1
        return SimpleNamespace(
            stop_reason="end_turn",
            message=SimpleNamespace(
                tool_calls=(),
                text=json.dumps({"files": {"src/plugin.py": "value = 'candidate'\n"}}),
            ),
        )


class _Runner:
    max_output_bytes = 1024 * 1024
    max_input_bytes = 256 * 1024
    policy_digest = DIGEST
    baseline_commit = ""
    worker_manifest_digest = "b" * 64

    def __init__(self, base: str, policy: PolicyManifest):
        self.baseline_commit = base
        self.policy_digest = policy.digest
        self.received = None

    def run(self, payload: bytes) -> bytes:
        self.received = payload
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


class _GitHub:
    def __init__(self, store: PublicationGatewayStore):
        self.store = store
        self.owner = "acme"
        self.repository = "plugin"
        self.repository_id = REPOSITORY_ID
        self.owner_id = OWNER_ID
        self.events = []
        self.published_refs = {}
        self.pull_requests = {}
        self.fail_after_publish = False
        self.fail_after_pr = False
        self.pull_request_creations = 0

    def verify_repository(self):
        self.events.append("verify")

    def publish(self, command: PublishCommand):
        assert self.store.get_provenance(command.request_id) is not None
        assert self.store.get_submission(command.request_id) is not None
        existing = self.published_refs.get(command.refspec.split(":", 1)[1])
        if existing is not None and existing != command.frozen_commit:
            raise RuntimeError("synthetic conflicting ref")
        self.published_refs[command.refspec.split(":", 1)[1]] = command.frozen_commit
        self.events.append("push")
        if self.fail_after_publish:
            self.fail_after_publish = False
            raise RuntimeError("synthetic lost push response")
        return PublishResult(command.frozen_commit, command.refspec.split(":", 1)[1])

    def create_or_get_pull_request(self, **values):
        self.events.append("pr")
        key = (values["request_id"], values["head_sha"], values["base_sha"])
        receipt = self.pull_requests.get(key)
        if receipt is None:
            self.pull_request_creations += 1
            receipt = PullRequestReceipt(
                73,
                values["request_id"],
                values["head_sha"],
                values["base_sha"],
                f"clawdot/{values['request_id']}",
                values["base_branch"],
                "open",
            )
            self.pull_requests[key] = receipt
        if self.fail_after_pr:
            self.fail_after_pr = False
            raise RuntimeError("synthetic lost pull-request response")
        return receipt

    def close(self):
        pass


def _service(tmp_path: Path, repo: Path, deployed: str, base: str, policy: PolicyManifest):
    store = PublicationGatewayStore(tmp_path / "gateway.sqlite3")
    github = _GitHub(store)

    def scanner(_item: ScanInput):
        return ScanVerdict.CLEAN

    publisher = PublicationService(
        policy=policy,
        scanner=scanner,
        scanner_identity_sha256=DIGEST,
        baseline_history_scan_complete=True,
        record_provenance=store,
        publish_transport=github.publish,
    )
    policy_json = json.dumps(_policy_json(policy), sort_keys=True, separators=(",", ":")).encode()
    gateway = PublicationGatewayService(
        publisher=publisher,
        github=github,
        store=store,
        repository_path=repo,
        policy=policy,
        policy_manifest_json=policy_json,
        repository=REPOSITORY,
        repository_id=REPOSITORY_ID,
        owner_id=OWNER_ID,
    )
    return gateway, github, store


def test_actual_development_controller_candidate_is_bundled_scanned_pushed_then_opened(tmp_path):
    repo, deployed, base, policy = _repo(tmp_path)
    service, github, store = _service(tmp_path, repo, deployed, base, policy)
    runner = _Runner(base, policy)
    provider = _Provider()
    development = DevelopmentController(
        provider=provider,
        installed_policy=policy,
        runner=runner,
        report_scanner=lambda _report: True,
        base_commit_verifier=lambda _commit, _digest: True,
    )
    request = GenerationRequest(
        specification="Implement a synthetic feature.",
        synthetic_examples=[{"input": "synthetic"}],
        base_commit=base,
        policy_digest=policy.digest,
    )

    result = asyncio.run(
        service.generate_and_publish(
            development=development,
            generation_request=request,
            request_id="request-73",
            deployed_commit=deployed,
            pull_request_title="Add a synthetic feature",
            pull_request_body="Synthetic summary.",
            commit_timestamp=1_798_765_432,
            artifacts=(ScanSurface("operator-context", b"initial artifact"),),
        )
    )

    assert runner.received is not None
    assert result.pull_request.pull_request_number == 73
    assert github.events == ["verify", "push", "pr"]
    context = store.get_context(73)
    assert context["request_id"] == "request-73"
    assert context["deployed_commit"] == deployed
    assert context["base_commit"] == base
    assert context["head_commit"] == result.publication.head_commit
    assert context["head_tree"] != _git(repo, "rev-parse", f"{base}^{{tree}}")
    assert context["published_ref"] == "refs/heads/clawdot/request-73"
    artifact_by_name = {item["name"]: item for item in context["artifacts"]}
    assert "synthetic-examples" in artifact_by_name
    assert "development-report" in artifact_by_name
    assert "operator-context" in artifact_by_name
    development_report = base64.b64decode(
        artifact_by_name["development-report"]["content_base64"], validate=True
    )
    assert (
        hashlib.sha256(development_report).hexdigest()
        == artifact_by_name["development-report"]["sha256"]
    )
    assert (
        context["context_sha256"]
        == hashlib.sha256(
            json.dumps(
                {key: value for key, value in context.items() if key != "context_sha256"},
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    with pytest.raises(PublicationGatewayError, match="state_conflict"):
        service.resume_request(
            "request-73",
            specification=request.specification,
            pull_request_title="Add a synthetic feature",
            pull_request_body="Synthetic summary.",
            synthetic_examples=request.synthetic_examples,
            artifacts=(ScanSurface("operator-context", b"changed artifact"),),
        )
    assert provider.calls == 1


def test_synthetic_example_scan_finding_denies_before_provenance_push_or_pr(tmp_path):
    repo, deployed, base, policy = _repo(tmp_path)
    service, github, store = _service(tmp_path, repo, deployed, base, policy)
    scanned_names: list[str] = []

    def scanner(item: ScanInput):
        scanned_names.append(item.surface_name)
        if item.surface_name == "artifact:synthetic-examples":
            return ScanVerdict.FINDING
        return ScanVerdict.CLEAN

    service.publisher._scanner = scanner
    development = DevelopmentController(
        provider=_Provider(),
        installed_policy=policy,
        runner=_Runner(base, policy),
        report_scanner=lambda _report: True,
        base_commit_verifier=lambda _commit, _digest: True,
    )
    request = GenerationRequest(
        specification="Implement a synthetic feature.",
        synthetic_examples=[{"input": "synthetic"}],
        base_commit=base,
        policy_digest=policy.digest,
    )

    with pytest.raises(PublicationGatewayError):
        asyncio.run(
            service.generate_and_publish(
                development=development,
                generation_request=request,
                request_id="request-scan-denied",
                deployed_commit=deployed,
                pull_request_title="Add a synthetic feature",
                pull_request_body="Synthetic summary.",
                commit_timestamp=1_798_765_432,
            )
        )

    assert "artifact:synthetic-examples" in scanned_names
    assert github.events == ["verify"]
    assert store.get_provenance("request-scan-denied") is None
    with pytest.raises(PublicationGatewayError, match="context_not_found"):
        store.get_context(73)


def test_same_length_changed_examples_conflict_before_second_push_and_report_is_scanned(tmp_path):
    repo, deployed, base, policy = _repo(tmp_path)
    service, github, store = _service(tmp_path, repo, deployed, base, policy)
    scanned: list[ScanInput] = []
    service.publisher._scanner = lambda item: scanned.append(item) or ScanVerdict.CLEAN
    provider = _Provider()
    development = DevelopmentController(
        provider=provider,
        installed_policy=policy,
        runner=_Runner(base, policy),
        report_scanner=lambda _report: True,
        base_commit_verifier=lambda _commit, _digest: True,
    )

    def generation(examples: list[object]):
        return GenerationRequest(
            specification="Implement a synthetic feature.",
            synthetic_examples=examples,
            base_commit=base,
            policy_digest=policy.digest,
        )

    asyncio.run(
        service.generate_and_publish(
            development=development,
            generation_request=generation([{"input": "synthetic"}]),
            request_id="request-example-replay",
            deployed_commit=deployed,
            pull_request_title="Add a synthetic feature",
            pull_request_body="Synthetic summary.",
            commit_timestamp=1_798_765_432,
        )
    )
    with pytest.raises(PublicationGatewayError):
        asyncio.run(
            service.generate_and_publish(
                development=development,
                generation_request=generation([{"input": "synthetix"}]),
                request_id="request-example-replay",
                deployed_commit=deployed,
                pull_request_title="Add a synthetic feature",
                pull_request_body="Synthetic summary.",
                commit_timestamp=1_798_765_432,
            )
        )

    for logical_changes in (
        {
            "specification": "Implement a synthetic feature!",
            "pull_request_title": "Add a synthetic feature",
            "pull_request_body": "Synthetic summary.",
        },
        {
            "specification": "Implement a synthetic feature.",
            "pull_request_title": "Add a synthetic Feature",
            "pull_request_body": "Synthetic summary.",
        },
        {
            "specification": "Implement a synthetic feature.",
            "pull_request_title": "Add a synthetic feature",
            "pull_request_body": "Synthetic summarY.",
        },
    ):
        with pytest.raises(PublicationGatewayError, match="state_conflict"):
            service.resume_request(
                "request-example-replay",
                **logical_changes,
                synthetic_examples=[{"input": "synthetic"}],
            )

    assert sum(item.surface_name == "artifact:synthetic-examples" for item in scanned) == 1
    assert sum(item.surface_name == "artifact:development-report" for item in scanned) == 1
    assert provider.calls == 1
    assert github.events == ["verify", "push", "pr"]
    record = store.get_provenance("request-example-replay")
    assert record is not None
    assert record.scanned_surface_count > 2


@pytest.mark.parametrize("failure_point", ["publish", "pull_request"])
def test_durable_submission_resumes_lost_publication_or_pr_reply_without_regeneration(
    tmp_path, failure_point
):
    repo, deployed, base, policy = _repo(tmp_path)
    service, github, store = _service(tmp_path, repo, deployed, base, policy)
    provider = _Provider()
    runner = _Runner(base, policy)
    development = DevelopmentController(
        provider=provider,
        installed_policy=policy,
        runner=runner,
        report_scanner=lambda _report: True,
        base_commit_verifier=lambda _commit, _digest: True,
    )
    specification = "Implement a synthetic feature."
    title = "Add a synthetic feature"
    body = "Synthetic summary."
    examples = [{"input": "synthetic"}]
    request_id = f"request-lost-{failure_point.replace('_', '-')}"
    if failure_point == "publish":
        github.fail_after_publish = True
    else:
        github.fail_after_pr = True

    with pytest.raises(PublicationGatewayError):
        asyncio.run(
            service.generate_and_publish(
                development=development,
                generation_request=GenerationRequest(
                    specification=specification,
                    synthetic_examples=examples,
                    base_commit=base,
                    policy_digest=policy.digest,
                ),
                request_id=request_id,
                deployed_commit=deployed,
                pull_request_title=title,
                pull_request_body=body,
                commit_timestamp=1_798_765_432,
            )
        )

    submission = store.get_submission(request_id)
    assert submission is not None
    assert submission.request.request_id == request_id
    assert submission.request.specification == specification.encode()
    assert submission.synthetic_examples == examples
    frozen_head = submission.request.head_commit
    reopened = PublicationGatewayStore(tmp_path / "gateway.sqlite3")
    github.store = reopened

    def scanner(_item: ScanInput):
        return ScanVerdict.CLEAN

    publisher = PublicationService(
        policy=policy,
        scanner=scanner,
        scanner_identity_sha256=DIGEST,
        baseline_history_scan_complete=True,
        record_provenance=reopened,
        publish_transport=github.publish,
    )
    resumed = PublicationGatewayService(
        publisher=publisher,
        github=github,
        store=reopened,
        repository_path=repo,
        policy=policy,
        policy_manifest_json=json.dumps(
            _policy_json(policy), sort_keys=True, separators=(",", ":")
        ).encode(),
        repository=REPOSITORY,
        repository_id=REPOSITORY_ID,
        owner_id=OWNER_ID,
    )

    result = resumed.resume_request(
        request_id,
        specification=specification,
        pull_request_title=title,
        pull_request_body=body,
        synthetic_examples=examples,
    )
    assert result.publication.head_commit == frozen_head
    assert provider.calls == 1
    assert github.published_refs[f"refs/heads/clawdot/{request_id}"] == frozen_head
    assert github.pull_request_creations == 1
    assert reopened.get_context(73)["head_commit"] == frozen_head

    push_count = github.events.count("push")
    pr_count = github.events.count("pr")
    with pytest.raises(PublicationGatewayError, match="state_conflict"):
        resumed.resume_request(
            request_id,
            specification=specification,
            pull_request_title=title,
            pull_request_body=body,
            synthetic_examples=[{"input": "synthetix"}],
        )
    assert github.events.count("push") == push_count
    assert github.events.count("pr") == pr_count


def test_submission_storage_is_immutable_bounded_and_survives_reopen(tmp_path, monkeypatch):
    from chatcore_release_control import publication_service as publication_module

    store = PublicationGatewayStore(tmp_path / "submission.sqlite3")
    request = publication_module.PublicationRequest(
        request_id="request-store",
        deployed_commit="1" * 40,
        base_commit="2" * 40,
        head_commit="3" * 40,
        bundle=b"synthetic bundle",
        specification=b"synthetic specification",
        pull_request_title=b"synthetic title",
        pull_request_body=b"synthetic body",
        artifacts=(publication_module.ScanSurface("owned-report", b"synthetic artifact"),),
    )
    examples = [{"input": "synthetic"}]
    store.store_submission(request, examples, development_report=b"synthetic report")
    reopened = PublicationGatewayStore(tmp_path / "submission.sqlite3")
    saved = reopened.get_submission("request-store")
    assert saved.request == request
    assert saved.synthetic_examples == examples
    assert saved.development_report == b"synthetic report"
    assert saved.request.artifacts[0].content == b"synthetic artifact"
    with pytest.raises(PublicationGatewayError, match="state_conflict"):
        reopened.store_submission(
            request,
            [{"input": "synthetix"}],
            development_report=b"synthetic report",
        )

    monkeypatch.setattr(publication_module, "_MAX_SUBMISSION_RECORD_BYTES", 64)
    too_large = publication_module.PublicationRequest(**{**request.__dict__, "bundle": b"x" * 128})
    with pytest.raises(PublicationGatewayError):
        reopened.store_submission(too_large, examples)


def test_submission_storage_exhaustion_fails_closed_without_pruning(tmp_path, monkeypatch):
    from chatcore_release_control import publication_service as publication_module

    store = PublicationGatewayStore(tmp_path / "submission-cap.sqlite3")
    request = publication_module.PublicationRequest(
        request_id="request-cap-one",
        deployed_commit="1" * 40,
        base_commit="2" * 40,
        head_commit="3" * 40,
        bundle=b"synthetic bundle",
        specification=b"synthetic specification",
        pull_request_title=b"synthetic title",
        pull_request_body=b"synthetic body",
        artifacts=(),
    )
    examples = [{"input": "synthetic"}]
    store.store_submission(request, examples)
    saved = store.get_submission(request.request_id)
    assert saved is not None
    monkeypatch.setattr(publication_module, "_MAX_SUBMISSION_TOTAL_BYTES", 1)
    second = publication_module.PublicationRequest(
        **{**request.__dict__, "request_id": "request-cap-two"}
    )
    with pytest.raises(PublicationGatewayError, match="state_unavailable"):
        store.store_submission(second, examples)
    assert store.get_submission(request.request_id) == saved
    assert store.get_submission("request-cap-two") is None

    count_store = PublicationGatewayStore(tmp_path / "submission-count.sqlite3")
    monkeypatch.setattr(publication_module, "_MAX_SUBMISSION_TOTAL_BYTES", 512 * 1024 * 1024)
    monkeypatch.setattr(publication_module, "_MAX_SUBMISSION_COUNT", 1)
    count_store.store_submission(request, examples)
    with pytest.raises(PublicationGatewayError, match="state_unavailable"):
        count_store.store_submission(second, examples)
    assert count_store.get_submission(request.request_id) is not None
    assert count_store.get_submission("request-cap-two") is None


def test_sqlite_gateway_store_is_durable_insert_once_for_provenance_context_and_report(tmp_path):
    store = PublicationGatewayStore(tmp_path / "state.sqlite3")
    store(
        ProvenanceRecord(
            request_id="request-1",
            deployed_commit="1" * 40,
            base_commit="2" * 40,
            head_commit="3" * 40,
            policy_digest=_policy().digest,
            scanner_identity_sha256="b" * 64,
            baseline_history_scan_complete=True,
            bundle_sha256="c" * 64,
            surface_manifest_sha256="d" * 64,
            scanned_surface_count=3,
            scanned_bytes=100,
        )
    )
    context = _context(store.get_provenance("request-1"))
    store.store_context(context)
    report = b"synthetic report"
    stored = store.store_report(context, report, hashlib.sha256(report).hexdigest())
    reopened = PublicationGatewayStore(tmp_path / "state.sqlite3")
    assert reopened.get_context(73) == context
    assert reopened.get_provenance("request-1").head_commit == "3" * 40
    assert reopened.get_report("request-1") == (hashlib.sha256(report).hexdigest(), report)
    assert reopened.store_report(context, report, hashlib.sha256(report).hexdigest()) == stored
    with pytest.raises(PublicationGatewayError):
        reopened.store_context({**context, "title": "changed"})
    with pytest.raises(PublicationGatewayError):
        reopened.store_report(context, b"different", hashlib.sha256(b"different").hexdigest())


def test_ci_asgi_requires_bearer_and_scans_immutable_report_before_receipt(tmp_path):
    store = PublicationGatewayStore(tmp_path / "ci.sqlite3")
    store(
        ProvenanceRecord(
            request_id="request-1",
            deployed_commit="d" * 40,
            base_commit="e" * 40,
            head_commit="a" * 40,
            policy_digest=_policy().digest,
            scanner_identity_sha256="c" * 64,
            baseline_history_scan_complete=True,
            bundle_sha256="f" * 64,
            surface_manifest_sha256="1" * 64,
            scanned_surface_count=3,
            scanned_bytes=100,
        )
    )
    context = _context(store.get_provenance("request-1"))
    store.store_context(context)
    scanned = []

    def scanner(item):
        scanned.append(item)
        return ScanVerdict.CLEAN

    app = PublicationGatewayASGI(
        store=store, ci_bearer_token="synthetic-ci-token", report_scanner=scanner
    )
    report = b'{"bounded":true}'
    body = {
        "request_id": "request-1",
        "head_sha": context["head_commit"],
        "policy_digest": context["policy_digest"],
        "context_sha256": context["context_sha256"],
        "report_sha256": hashlib.sha256(report).hexdigest(),
        "report_base64": base64.b64encode(report).decode(),
    }

    async def call():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            denied = await client.get("/v1/ci/pull-requests/73/context")
            assert denied.status_code == 401
            context_response = await client.get(
                "/v1/ci/pull-requests/73/context",
                headers={"Authorization": "Bearer synthetic-ci-token"},
            )
            report_response = await client.post(
                "/v1/ci/pull-requests/73/report",
                headers={"Authorization": "Bearer synthetic-ci-token"},
                json=body,
            )
            return context_response, report_response

    context_response, report_response = asyncio.run(call())
    assert context_response.json() == context
    receipt = report_response.json()
    assert receipt["stored"] is True and receipt["request_id"] == "request-1"
    assert receipt["report_sha256"] == body["report_sha256"]
    assert scanned[0].surface_name == "ci-execution-report"
    assert scanned[0].content == report
    assert store.get_report("request-1")[1] == report


def test_ci_asgi_denies_scanner_finding_and_does_not_store_report(tmp_path):
    store = PublicationGatewayStore(tmp_path / "ci.sqlite3")
    store(
        ProvenanceRecord(
            request_id="request-1",
            deployed_commit="d" * 40,
            base_commit="e" * 40,
            head_commit="a" * 40,
            policy_digest=_policy().digest,
            scanner_identity_sha256="c" * 64,
            baseline_history_scan_complete=True,
            bundle_sha256="f" * 64,
            surface_manifest_sha256="1" * 64,
            scanned_surface_count=3,
            scanned_bytes=100,
        )
    )
    context = _context(store.get_provenance("request-1"))
    store.store_context(context)
    app = PublicationGatewayASGI(
        store=store,
        ci_bearer_token="synthetic-ci-token",
        report_scanner=lambda _item: ScanVerdict.FINDING,
    )
    report = b"synthetic report"
    body = {
        "request_id": "request-1",
        "head_sha": context["head_commit"],
        "policy_digest": context["policy_digest"],
        "context_sha256": context["context_sha256"],
        "report_sha256": hashlib.sha256(report).hexdigest(),
        "report_base64": base64.b64encode(report).decode(),
    }

    async def call():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            return await client.post(
                "/v1/ci/pull-requests/73/report",
                headers={"Authorization": "Bearer synthetic-ci-token"},
                json=body,
            )

    response = asyncio.run(call())
    assert response.status_code == 422
    assert store.get_report("request-1") is None


def _protected_runtime(tmp_path: Path):
    repo, _deployed, base, policy = _repo(tmp_path)
    policy_raw = json.dumps(_policy_json(policy), sort_keys=True, separators=(",", ":")).encode()
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(policy_raw)
    policy_path.chmod(0o600)
    scanner = tmp_path / "scanner"
    scanner.write_bytes(b"synthetic trusted scanner")
    scanner.chmod(0o700)
    scanner_config = tmp_path / "scanner.conf"
    scanner_config.write_bytes(b"synthetic scanner config")
    scanner_config.chmod(0o600)
    scanner_sha = hashlib.sha256(scanner.read_bytes()).hexdigest()
    scanner_config_sha = hashlib.sha256(scanner_config.read_bytes()).hexdigest()
    scanner_identity = hashlib.sha256(
        bytes.fromhex(scanner_sha) + bytes.fromhex(scanner_config_sha)
    ).hexdigest()
    baseline_receipt = {
        "version": 1,
        "repository_id": REPOSITORY_ID,
        "baseline_commit": base,
        "policy_digest": policy.digest,
        "scanner_identity_sha256": scanner_identity,
        "history_complete": True,
        "scanned_commit_count": 32,
        "scanned_bytes": 100_000,
    }
    baseline_raw = json.dumps(baseline_receipt, sort_keys=True, separators=(",", ":")).encode()
    baseline_path = tmp_path / "baseline-history.json"
    baseline_path.write_bytes(baseline_raw)
    baseline_path.chmod(0o600)
    key_path = tmp_path / "app.pem"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    token_path = tmp_path / "ci-token"
    token_path.write_text("synthetic-ci-token-protected")
    token_path.chmod(0o600)
    bootstrap_dir = tmp_path / "bootstrap-artifacts"
    bootstrap_dir.mkdir(mode=0o700)
    bootstrap_source = Path(hosted_bootstrap.__file__).read_bytes()
    bootstrap_files = {
        "runtime/ci-runtime.json": (b"{}", 0o644),
        "runtime/policy.json": (b"{}", 0o644),
        "runtime/worker-manifest.json": (b"{}", 0o644),
        "runtime/baseline.tar": (b"baseline archive", 0o644),
        "runtime/worker-image.tar": (b"image archive", 0o644),
        "runtime/runsc": (b"runsc binary", 0o755),
        "runtime/runtime-policy.json": (b"{}", 0o644),
        "runtime/scanner": (b"scanner binary", 0o755),
        "runtime/scanner-config": (b"scanner config", 0o644),
        "runtime/wheels/coordinator-1.0-py3-none-any.whl": (b"wheel", 0o644),
    }
    archive_path = bootstrap_dir / "runtime.tar"
    with tarfile.open(archive_path, "w") as archive:
        import io

        for name, (data, mode) in sorted(bootstrap_files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            archive.addfile(info, io.BytesIO(data))
    archive_path.chmod(0o600)
    archive_bytes = archive_path.read_bytes()
    source_path = bootstrap_dir / "hosted-bootstrap.py"
    source_path.write_bytes(bootstrap_source)
    source_path.chmod(0o600)
    file_records = [
        {
            "path": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mode": mode,
        }
        for name, (data, mode) in sorted(bootstrap_files.items())
    ]
    bootstrap_manifest = {
        "version": 1,
        "bootstrap_source_sha256": hashlib.sha256(bootstrap_source).hexdigest(),
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "archive_size": len(archive_bytes),
        "files": file_records,
        "ci_config_path": "runtime/ci-runtime.json",
        "policy_path": "runtime/policy.json",
        "worker_manifest_path": "runtime/worker-manifest.json",
        "baseline_archive_path": "runtime/baseline.tar",
        "worker_image_path": "runtime/worker-image.tar",
        "runsc_path": "runtime/runsc",
        "runtime_policy_path": "runtime/runtime-policy.json",
        "scanner_path": "runtime/scanner",
        "scanner_config_path": "runtime/scanner-config",
        "coordinator_wheel": "runtime/wheels/coordinator-1.0-py3-none-any.whl",
        "image_reference": "ghcr.io/acme/worker@sha256:" + "7" * 64,
    }
    manifest_raw = json.dumps(bootstrap_manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_path = bootstrap_dir / "manifest.json"
    manifest_path.write_bytes(manifest_raw)
    manifest_path.chmod(0o600)
    socket_dir = Path(tempfile.mkdtemp(prefix="pub-", dir="/tmp"))
    config = {
        "version": 1,
        "repository": REPOSITORY,
        "app_id": 303,
        "installation_id": 404,
        "source_repository": str(repo),
        "policy_path": str(policy_path),
        "policy_sha256": hashlib.sha256(policy_raw).hexdigest(),
        "baseline_commit": base,
        "baseline_history_receipt_path": str(baseline_path),
        "baseline_history_receipt_sha256": hashlib.sha256(baseline_raw).hexdigest(),
        "app_private_key_path": str(key_path),
        "scanner_executable_path": str(scanner),
        "scanner_executable_sha256": scanner_sha,
        "scanner_configuration_path": str(scanner_config),
        "scanner_configuration_sha256": scanner_config_sha,
        "publication_scanner_identity_sha256": scanner_identity,
        "git_executable_path": shutil.which("git"),
        "database_path": str(tmp_path / "gateway.sqlite3"),
        "ci_token_path": str(token_path),
        "socket_path": str(socket_dir / "publication.sock"),
        "temporary_directory": str(tmp_path),
        "bootstrap_artifact_directory": str(bootstrap_dir),
        "bootstrap_source_path": str(source_path),
        "bootstrap_source_sha256": hashlib.sha256(bootstrap_source).hexdigest(),
        "bootstrap_manifest_path": str(manifest_path),
        "bootstrap_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "bootstrap_archive_path": str(archive_path),
        "bootstrap_archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "bootstrap_archive_size": len(archive_bytes),
    }
    config_raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    config_path = tmp_path / "publication-config.json"
    config_path.write_bytes(config_raw)
    config_path.chmod(0o600)
    environment = {
        "RELEASE_CONTROL_PUBLICATION_CONFIG_PATH": str(config_path),
        "RELEASE_CONTROL_PUBLICATION_CONFIG_SHA256": hashlib.sha256(config_raw).hexdigest(),
        "RELEASE_CONTROL_PUBLICATION_MODE": "gateway",
    }
    return environment, config, policy


def _github_handler(request):
    assert request.headers["Authorization"] == "Bearer ghs_synthetic_runtime_token"
    if request.url.path == "/repos/acme/plugin":
        return httpx.Response(
            200,
            json={
                "id": REPOSITORY_ID,
                "full_name": REPOSITORY,
                "private": True,
                "owner": {"id": OWNER_ID, "login": "acme"},
            },
        )
    raise AssertionError((request.method, request.url))


def _app_handler(request):
    assert request.url.path == "/app/installations/404/access_tokens"
    return httpx.Response(
        201,
        json={
            "token": "ghs_synthetic_runtime_token",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=55))
            .isoformat()
            .replace("+00:00", "Z"),
            "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
            "repositories": [{"id": REPOSITORY_ID}],
        },
    )


def test_protected_publication_runtime_builds_real_gateway_and_asgi_without_network(tmp_path):
    environment, _config, _policy_value = _protected_runtime(tmp_path)
    protected = ProtectedPublicationConfig.from_environment(environment)
    assert "synthetic-ci-token-protected" not in repr(protected)
    gateway, app = build_publication_gateway(
        environment,
        github_transport=httpx.MockTransport(_github_handler),
        app_http_transport=httpx.MockTransport(_app_handler),
    )
    assert app.__class__ is PublicationGatewayASGI
    assert "synthetic-ci-token-protected" not in repr(app)
    assert gateway.github.repository_id == REPOSITORY_ID
    assert gateway.store.path == Path(_config["database_path"])
    gateway.close()
    shutil.rmtree(Path(_config["socket_path"]).parent)


def test_private_uds_cli_uses_concrete_runtime_and_mode_0600(tmp_path, monkeypatch):
    environment, config, _policy_value = _protected_runtime(tmp_path)
    calls = []
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    assert (
        publication_main(
            ["serve"],
            environment=environment,
            github_transport=httpx.MockTransport(_github_handler),
            app_http_transport=httpx.MockTransport(_app_handler),
        )
        == 0
    )
    assert calls[0][0].__class__ is PublicationGatewayASGI
    assert calls[0][1]["uds"] == config["socket_path"]
    assert calls[0][1]["uds_permissions"] == 0o600
    assert calls[0][1]["access_log"] is False
    shutil.rmtree(Path(config["socket_path"]).parent)


def test_publication_cli_checkconfig_and_invalid_digest_fail_closed(tmp_path):
    environment, _config, _policy_value = _protected_runtime(tmp_path)
    assert publication_main(["checkconfig"], environment=environment) == 0
    bad = {**environment, "RELEASE_CONTROL_PUBLICATION_CONFIG_SHA256": "0" * 64}
    with pytest.raises(SystemExit, match="invalid_configuration"):
        publication_main(["checkconfig"], environment=bad)


def test_bootstrap_routes_serve_only_digest_pinned_raw_artifacts(tmp_path):
    environment, config, _policy_value = _protected_runtime(tmp_path)
    gateway, app = build_publication_gateway(
        environment,
        github_transport=httpx.MockTransport(_github_handler),
        app_http_transport=httpx.MockTransport(_app_handler),
    )

    async def fetch(path: str, authorization: str = "Bearer synthetic-ci-token-protected"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            return await client.get(path, headers={"Authorization": authorization})

    source = Path(config["bootstrap_source_path"]).read_bytes()
    manifest = Path(config["bootstrap_manifest_path"]).read_bytes()
    archive = Path(config["bootstrap_archive_path"]).read_bytes()
    source_response = asyncio.run(fetch("/v1/ci/bootstrap/hosted-bootstrap.py"))
    manifest_response = asyncio.run(fetch("/v1/ci/bootstrap/manifest"))
    archive_response = asyncio.run(
        fetch(f"/v1/ci/bootstrap/artifacts/{config['bootstrap_archive_sha256']}")
    )
    wrong_digest = asyncio.run(fetch("/v1/ci/bootstrap/artifacts/" + "0" * 64))
    unauthenticated = asyncio.run(
        fetch("/v1/ci/bootstrap/manifest", authorization="Bearer wrong-token")
    )

    assert source_response.status_code == 200 and source_response.content == source
    assert source_response.headers["content-type"] == "application/octet-stream"
    assert manifest_response.status_code == 200 and manifest_response.content == manifest
    assert manifest_response.headers["content-type"] == "application/json"
    assert archive_response.status_code == 200 and archive_response.content == archive
    assert wrong_digest.status_code == 404
    assert unauthenticated.status_code == 401
    gateway.close()


def test_bootstrap_route_rechecks_protected_file_digest_and_never_reads_requested_paths(tmp_path):
    environment, config, _policy_value = _protected_runtime(tmp_path)
    gateway, app = build_publication_gateway(
        environment,
        github_transport=httpx.MockTransport(_github_handler),
        app_http_transport=httpx.MockTransport(_app_handler),
    )
    Path(config["bootstrap_archive_path"]).write_bytes(b"changed")
    Path(config["bootstrap_archive_path"]).chmod(0o600)

    async def call(path: str):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            return await client.get(
                path, headers={"Authorization": "Bearer synthetic-ci-token-protected"}
            )

    changed = asyncio.run(call(f"/v1/ci/bootstrap/artifacts/{config['bootstrap_archive_sha256']}"))
    unknown = asyncio.run(call("/v1/ci/bootstrap/artifacts/" + "f" * 64))

    assert changed.status_code == 503
    assert unknown.status_code == 404
    gateway.close()
