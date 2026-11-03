from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from chatcore_release_control.github_evidence import (
    MAX_RESPONSE_BYTES,
    GitHubEvidenceClient,
    GitHubEvidenceError,
    GitHubEvidenceResolver,
    evidence_digest,
)
from chatcore_release_control.policy import PolicyManifest, ReleaseBundle

B = "1" * 40
H = "2" * 40
M = "3" * 40
BT = "4" * 40
HT = "5" * 40
MT = "6" * 40
WORKFLOW_DIGEST = "a" * 64


def _policy(*, multiple: bool = False) -> PolicyManifest:
    workflows = {".github/workflows/release.yml": WORKFLOW_DIGEST}
    checks = [
        {
            "name": "candidate-tests",
            "creator_app_id": 303,
            "workflow_path": ".github/workflows/release.yml",
            "workflow_sha": WORKFLOW_DIGEST,
        }
    ]
    allowed = ["src/plugin.py"]
    protected = [".github/workflows/release.yml"]
    if multiple:
        workflows[".github/workflows/verify.yml"] = "c" * 64
        checks.append(
            {
                "name": "trusted-verification",
                "creator_app_id": 304,
                "workflow_path": ".github/workflows/verify.yml",
                "workflow_sha": "c" * 64,
            }
        )
        protected.append(".github/workflows/verify.yml")
    return PolicyManifest.from_json(
        json.dumps(
            {
                "version": 1,
                "repository_id": 101,
                "owner_id": 202,
                "target": "production",
                "workflow_digests": workflows,
                "allowed_paths": allowed,
                "protected_paths": protected,
                "checks": checks,
            }
        )
    )


def _bundle(policy: PolicyManifest | None = None) -> ReleaseBundle:
    return ReleaseBundle(
        deployed_commit=B,
        base_commit=B,
        head_commit=H,
        deployed_tree=BT,
        base_tree=BT,
        head_tree=HT,
        policy_digest=(policy or _policy()).digest,
        base_is_ancestor_of_head=True,
        base_is_descendant_of_deployed=True,
        same_tree_continuation_recorded=True,
        complete_delta_reviewed=False,
        deployed_to_base=(),
        base_to_head=(),
        deployed_to_head=(),
    )


def _check_run(
    *,
    creator: int = 303,
    conclusion: str = "success",
    name: str = "candidate-tests",
    run_id: int = 901,
    suite_id: int = 801,
) -> dict[str, object]:
    return {
        "id": run_id,
        "name": name,
        "app": {"id": creator},
        "status": "completed",
        "conclusion": conclusion,
        "head_sha": M,
        "check_suite": {"id": suite_id},
        "pull_requests": [{"number": 42, "base": {"sha": B}, "head": {"sha": H}}],
    }


def _workflow_run(
    *,
    attempt: int = 2,
    conclusion: str = "success",
    run_id: int = 701,
    suite_id: int = 801,
    workflow: str = ".github/workflows/release.yml",
) -> dict[str, object]:
    return {
        "id": run_id,
        "run_attempt": attempt,
        "check_suite_id": suite_id,
        "repository": {"id": 101},
        "event": "pull_request",
        "head_sha": M,
        "path": f"{workflow}@refs/pull/42/merge",
        "status": "completed",
        "conclusion": conclusion,
        "pull_requests": [{"number": 42, "base": {"sha": B}, "head": {"sha": H}}],
    }


def _job(
    job_id: int,
    *,
    conclusion: str = "success",
    attempt: int = 2,
    run_id: int = 701,
    check_run_id: int = 901,
) -> dict[str, object]:
    return {
        "id": job_id,
        "run_id": run_id,
        "run_attempt": attempt,
        "status": "completed",
        "conclusion": conclusion,
        "check_run_url": (f"https://api.github.com/repos/acme/plugin/check-runs/{check_run_id}"),
    }


class FakeGitHub:
    def __init__(
        self, policy: PolicyManifest | None = None, *, base_branch: str = "production"
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.policy = policy or _policy()
        self.base_branch = base_branch
        self.pages: dict[str, list[list[dict[str, object]]]] = {
            "check_runs": [[_check_run()]],
            "workflow_runs": [[_workflow_run()]],
        }
        self.overrides: dict[tuple[str, str], httpx.Response] = {}
        self.jobs_by_run: dict[int, list[list[dict[str, object]]]] = {701: [[_job(1001)]]}
        self.latest_attempts: dict[int, int] = {}

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != "Bearer synthetic-token":
            return httpx.Response(401)
        path = request.url.path.removeprefix("/repos/acme/plugin/")
        override = self.overrides.get((request.method, path))
        if override is not None:
            return override
        if path == "pulls/42":
            body = {
                "number": 42,
                "state": "open",
                "merged": False,
                "base": {
                    "ref": self.base_branch,
                    "sha": B,
                    "repo": {"id": 101, "owner": {"id": 202}},
                },
                "head": {"sha": H, "repo": {"id": 101, "owner": {"id": 202}}},
                "merge_commit_sha": M,
            }
            return httpx.Response(200, json=body)
        if path == f"git/commits/{M}":
            return httpx.Response(
                200,
                json={"sha": M, "tree": {"sha": MT}, "parents": [{"sha": B}, {"sha": H}]},
            )
        query = dict(request.url.params)
        page = int(query.get("page", "1")) - 1
        if path == f"commits/{M}/check-runs":
            items = self.pages["check_runs"][page] if page < len(self.pages["check_runs"]) else []
            return httpx.Response(
                200,
                json={"total_count": sum(map(len, self.pages["check_runs"])), "check_runs": items},
            )
        if path == "actions/runs":
            items = (
                self.pages["workflow_runs"][page] if page < len(self.pages["workflow_runs"]) else []
            )
            return httpx.Response(
                200,
                json={
                    "total_count": sum(map(len, self.pages["workflow_runs"])),
                    "workflow_runs": items,
                },
            )
        run_parts = path.split("/")
        if len(run_parts) >= 3 and run_parts[:2] == ["actions", "runs"]:
            try:
                run_id = int(run_parts[2])
            except ValueError:
                return httpx.Response(404)
            all_runs = [item for pages in self.pages["workflow_runs"] for item in pages]
            run = next((item for item in all_runs if item.get("id") == run_id), None)
            if run is None:
                return httpx.Response(404)
            if len(run_parts) == 3:
                return httpx.Response(
                    200,
                    json={
                        **run,
                        "run_attempt": self.latest_attempts.get(run_id, run.get("run_attempt")),
                    },
                )
            if len(run_parts) == 5 and run_parts[3] == "attempts":
                if int(run_parts[4]) != run.get("run_attempt"):
                    return httpx.Response(404)
                return httpx.Response(200, json=run)
            if len(run_parts) == 6 and run_parts[3] == "attempts" and run_parts[5] == "jobs":
                job_pages = self.jobs_by_run.get(run_id, [])
                items = job_pages[page] if page < len(job_pages) else []
                return httpx.Response(
                    200,
                    json={"total_count": sum(map(len, job_pages)), "jobs": items},
                )
        return httpx.Response(404)


def _resolver(
    fake: FakeGitHub,
    closure=True,
    policy: PolicyManifest | None = None,
) -> GitHubEvidenceResolver:
    client = GitHubEvidenceClient(
        owner="acme",
        repository="plugin",
        repository_id=101,
        token_provider=lambda: "synthetic-token",
        transport=httpx.MockTransport(fake.respond),
        page_size=1,
    )
    return GitHubEvidenceResolver(
        client,
        policy or fake.policy,
        verify_workflow_closure=lambda commit, digests: (
            closure
            and commit == B
            and dict(digests) == dict((policy or fake.policy).workflow_digests)
        ),
        expected_base_branch="production",
    )


def test_production_policy_uses_main_pull_request_base_by_default() -> None:
    fake = FakeGitHub(base_branch="main")
    client = GitHubEvidenceClient(
        owner="acme",
        repository="plugin",
        repository_id=101,
        token_provider=lambda: "synthetic-token",
        transport=httpx.MockTransport(fake.respond),
    )
    resolver = GitHubEvidenceResolver(
        client,
        fake.policy,
        verify_workflow_closure=lambda commit, _: commit == B,
    )
    result = resolver.resolve(42, _bundle(fake.policy))
    assert fake.policy.target == "production"
    assert result.target_branch == "main"


def test_evidence_digest_is_canonical_and_binds_job_and_attempt_ids() -> None:
    fake = FakeGitHub()
    evidence = _resolver(fake).resolve(42, _bundle(fake.policy))
    digest = evidence_digest(evidence)
    assert digest == evidence_digest(evidence)
    changed_run = replace(evidence.workflow_runs[0], job_ids=(1002,))
    changed = replace(evidence, workflow_runs=(changed_run,))
    assert evidence_digest(changed) != digest


def test_resolves_paginated_checks_runs_and_jobs_with_synthetic_merge_binding() -> None:
    fake = FakeGitHub()
    fake.pages["check_runs"] = [
        [_check_run()],
        [{**_check_run(), "id": 902, "name": "unrequired-annotation"}],
    ]
    fake.pages["workflow_runs"] = [
        [_workflow_run()],
        [{**_workflow_run(), "id": 702, "check_suite_id": 802}],
    ]
    fake.jobs_by_run[701] = [[_job(1001)], [_job(1002, check_run_id=902)]]

    evidence = _resolver(fake).resolve(42, _bundle())

    assert evidence.repository_id == 101
    assert evidence.pr_number == 42
    assert evidence.event_name == "pull_request"
    assert (evidence.base_commit, evidence.head_commit, evidence.head_tree) == (B, H, HT)
    assert (evidence.synthetic_merge_commit, evidence.synthetic_merge_tree) == (M, MT)
    assert evidence.synthetic_merge_parents == (B, H)
    assert evidence.check_run_ids == (("candidate-tests", 901),)
    check = evidence.checks[0]
    assert (check.head_commit, check.head_tree, check.creator_app_id) == (M, MT, 303)
    assert evidence.workflow_runs[0].run_id == 701
    assert evidence.workflow_runs[0].attempt == 2
    assert evidence.workflow_runs[0].job_ids == (1001, 1002)
    assert any(request.url.params.get("page") == "2" for request in fake.requests)
    assert all(
        request.headers["authorization"] == "Bearer synthetic-token" for request in fake.requests
    )


def test_required_checks_can_bind_distinct_latest_runs_and_workflows() -> None:
    policy = _policy(multiple=True)
    fake = FakeGitHub(policy)
    fake.pages["check_runs"] = [
        [_check_run()],
        [
            _check_run(
                name="trusted-verification",
                creator=304,
                run_id=902,
                suite_id=802,
            )
        ],
    ]
    fake.pages["workflow_runs"] = [
        [_workflow_run()],
        [
            _workflow_run(
                run_id=702,
                suite_id=802,
                workflow=".github/workflows/verify.yml",
                attempt=1,
            )
        ],
    ]
    fake.jobs_by_run[701] = [[_job(1001)]]
    fake.jobs_by_run[702] = [[_job(2001, run_id=702, attempt=1, check_run_id=902)]]

    evidence = _resolver(fake, policy=policy).resolve(42, _bundle(policy))

    assert {item.name for item in evidence.checks} == {
        "candidate-tests",
        "trusted-verification",
    }
    assert [(run.run_id, run.attempt) for run in evidence.workflow_runs] == [(701, 2), (702, 1)]


@pytest.mark.parametrize(
    "changes",
    [
        {"base": {"ref": "production", "sha": "9" * 40, "repo": {"id": 101}}},
        {"head": {"sha": "9" * 40, "repo": {"id": 101}}},
        {"state": "closed", "merged": False},
        {"merge_commit_sha": "9" * 40},
    ],
)
def test_moved_or_unmapped_pull_request_fails_closed(changes) -> None:
    fake = FakeGitHub()
    original = fake.respond

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/42"):
            body = {
                "number": 42,
                "state": "open",
                "merged": False,
                "base": {
                    "ref": "production",
                    "sha": B,
                    "repo": {"id": 101, "owner": {"id": 202}},
                },
                "head": {"sha": H, "repo": {"id": 101, "owner": {"id": 202}}},
                "merge_commit_sha": M,
                **changes,
            }
            return httpx.Response(200, json=body)
        return original(request)

    fake.respond = respond
    with pytest.raises(GitHubEvidenceError):
        _resolver(fake).resolve(42, _bundle())


@pytest.mark.parametrize(
    ("check", "run", "job", "closure"),
    [
        (_check_run(creator=999), _workflow_run(), _job(1001), True),
        (_check_run(conclusion="skipped"), _workflow_run(), _job(1001), True),
        (_check_run(), _workflow_run(attempt=1), _job(1001), True),
        (_check_run(), _workflow_run(), _job(1001, conclusion="skipped"), True),
        (_check_run(), _workflow_run(), _job(1001, check_run_id=999), True),
        (_check_run(), _workflow_run(), _job(1001), False),
    ],
)
def test_foreign_failed_stale_or_untrusted_checks_are_denied(check, run, job, closure) -> None:
    fake = FakeGitHub()
    fake.pages["check_runs"] = [[check]]
    fake.pages["workflow_runs"] = [[run]]
    fake.jobs_by_run[701] = [[job]]
    if run.get("run_attempt") == 1:
        fake.latest_attempts[701] = 2
    with pytest.raises(GitHubEvidenceError):
        _resolver(fake, closure=closure).resolve(42, _bundle())


def test_missing_page_and_api_errors_fail_closed_without_raw_diagnostics() -> None:
    fake = FakeGitHub()
    fake.jobs_by_run[701] = [[_job(1001)]]
    fake.respond = lambda request: httpx.Response(503, text="synthetic private diagnostic")
    with pytest.raises(GitHubEvidenceError) as caught:
        _resolver(fake).resolve(42, _bundle())
    assert "private diagnostic" not in str(caught.value)


def test_pagination_total_mismatch_is_rejected() -> None:
    fake = FakeGitHub()
    fake.pages["check_runs"] = [[_check_run()]]
    original = fake.respond

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"total_count": 2, "check_runs": [_check_run()]})
        return original(request)

    fake.respond = respond
    with pytest.raises(GitHubEvidenceError):
        _resolver(fake).resolve(42, _bundle())


def test_api_client_uses_fixed_origin_and_does_not_follow_redirects() -> None:
    requests: list[httpx.Request] = []
    client = GitHubEvidenceClient(
        owner="acme",
        repository="plugin",
        repository_id=101,
        token_provider=lambda: "synthetic-token",
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request)
                or httpx.Response(302, headers={"Location": "https://attacker.invalid"})
            )
        ),
    )
    with pytest.raises(GitHubEvidenceError):
        client.get_json("pulls/42")
    assert len(requests) == 1
    assert str(requests[0].url).startswith("https://api.github.com/repos/acme/plugin/")
    assert "synthetic-token" not in repr(client)


def test_api_client_bounds_response_bytes_and_rejects_caller_urls() -> None:
    client = GitHubEvidenceClient(
        owner="acme",
        repository="plugin",
        repository_id=101,
        token_provider=lambda: "synthetic-token",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
        ),
    )
    with pytest.raises(GitHubEvidenceError) as oversized:
        client.get_json("pulls/42")
    assert oversized.value.code == "invalid_response"
    with pytest.raises(GitHubEvidenceError) as url:
        client.get_json("https://attacker.invalid")
    assert url.value.code == "invalid_input"


def test_p04_workflow_templates_are_inactive_and_keep_permissions_explicit() -> None:
    template_root = Path(__file__).parents[1] / "src" / "chatcore_release_control" / "templates"
    templates = sorted(template_root.glob("p04-*.yml.inactive"))
    assert len(templates) >= 2
    for template in templates:
        text = template.read_text(encoding="utf-8")
        assert template.suffix == ".inactive"
        assert "permissions:" in text
        assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in text
        assert "GITHUB_TOKEN" not in text
    ci_text = (template_root / "p04-ci.yml.inactive").read_text(encoding="utf-8")
    assert "without writing Git credentials" in ci_text
