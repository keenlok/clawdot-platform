from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from chatcore_release_control.github_evidence import (
    GitHubEvidenceClient,
    GitHubEvidenceSnapshot,
    VerifiedWorkflowRun,
)
from chatcore_release_control.merge_control import (
    GitHubMergeTransport,
    MergeControlError,
    MergeController,
)
from chatcore_release_control.oidc import CallerIdentity
from chatcore_release_control.policy import CheckEvidence, ReleaseBundle


def _git(repo: Path, *args: str) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(repo),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=env,
    )
    return result.stdout.decode("ascii", errors="strict").strip()


def _fixture(tmp_path: Path) -> tuple[ReleaseBundle, GitHubEvidenceSnapshot, str, str]:
    repo = tmp_path / "objects"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Synthetic")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    source = repo / "plugin.py"
    source.write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "plugin.py")
    _git(repo, "commit", "-qm", "baseline")
    base = _git(repo, "rev-parse", "HEAD")
    base_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    source.write_text("candidate\n", encoding="utf-8")
    _git(repo, "commit", "-am", "candidate", "-q")
    head = _git(repo, "rev-parse", "HEAD")
    head_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    merge = _git(repo, "commit-tree", head_tree, "-p", base, "-p", head)
    merge_tree = _git(repo, "rev-parse", f"{merge}^{{tree}}")
    bundle = ReleaseBundle(
        deployed_commit=base,
        base_commit=base,
        head_commit=head,
        deployed_tree=base_tree,
        base_tree=base_tree,
        head_tree=head_tree,
        policy_digest="a" * 64,
        base_is_ancestor_of_head=True,
        base_is_descendant_of_deployed=True,
        same_tree_continuation_recorded=True,
        complete_delta_reviewed=False,
        deployed_to_base=(),
        base_to_head=(),
        deployed_to_head=(),
    )
    check = CheckEvidence(
        name="candidate-tests",
        repository_id=101,
        event="pull_request",
        workflow_path=".github/workflows/release.yml",
        workflow_sha=base,
        workflow_digest="a" * 64,
        run_id=701,
        attempt=2,
        creator_app_id=303,
        head_commit=merge,
        head_tree=merge_tree,
        conclusion="success",
    )
    snapshot = GitHubEvidenceSnapshot(
        repository_id=101,
        pr_number=42,
        target_branch="main",
        event_name="pull_request",
        base_commit=base,
        head_commit=head,
        head_tree=head_tree,
        synthetic_merge_commit=merge,
        synthetic_merge_tree=merge_tree,
        synthetic_merge_parents=(base, head),
        checks=(check,),
        check_run_ids=(("candidate-tests", 901),),
        workflow_runs=(
            VerifiedWorkflowRun(
                801,
                701,
                2,
                ".github/workflows/release.yml",
                base,
                (901,),
                (1001,),
            ),
        ),
    )
    return bundle, snapshot, merge, merge_tree


def _caller() -> CallerIdentity:
    return CallerIdentity(101, 202, "b" * 40, 701, 1, 801, "github-hosted")


class MergeApi:
    def __init__(
        self,
        bundle: ReleaseBundle,
        *,
        synthetic_merge: str,
        merge_sha: str,
        tree: str,
        parents: tuple[str, ...],
    ) -> None:
        self.bundle = bundle
        self.synthetic_merge = synthetic_merge
        self.merge_sha = merge_sha
        self.tree = tree
        self.parents = parents
        self.requests: list[httpx.Request] = []
        self.puts = 0
        self.closed = False
        self.lost_response = False
        self.close_without_merge = False
        self.changed_base: str | None = None

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != "Bearer synthetic-token":
            return httpx.Response(401)
        path = request.url.path.removeprefix("/repos/acme/plugin/")
        if request.method == "GET" and path == "pulls/42":
            if self.changed_base:
                base = self.changed_base
            else:
                base = self.bundle.base_commit
            closed = self.closed or self.close_without_merge
            return httpx.Response(
                200,
                json={
                    "number": 42,
                    "state": "closed" if closed else "open",
                    "merged": self.closed,
                    "base": {
                        "ref": "main",
                        "sha": base,
                        "repo": {"id": 101, "owner": {"id": 202}},
                    },
                    "head": {
                        "sha": self.bundle.head_commit,
                        "repo": {"id": 101, "owner": {"id": 202}},
                    },
                    "merge_commit_sha": self.merge_sha if self.closed else self.synthetic_merge,
                },
            )
        if request.method == "PUT" and path == "pulls/42/merge":
            self.puts += 1
            assert json.loads(request.content) == {
                "sha": self.bundle.head_commit,
                "merge_method": "merge",
            }
            self.closed = True
            if self.lost_response:
                raise httpx.ReadTimeout("synthetic lost merge reply", request=request)
            return httpx.Response(200, json={"merged": True, "sha": self.merge_sha})
        if request.method == "GET" and path == f"git/commits/{self.merge_sha}":
            return httpx.Response(
                200,
                json={
                    "sha": self.merge_sha,
                    "tree": {"sha": self.tree},
                    "parents": [{"sha": parent} for parent in self.parents],
                },
            )
        return httpx.Response(404)


def _controller(
    api: MergeApi,
    *,
    lock_state: list[bool],
    persisted: list[object],
    deployed: str | None = None,
    policy_digest: str | None = None,
    admitted: bool = True,
    admission_is_live: bool = True,
):
    client = GitHubEvidenceClient(
        owner="acme",
        repository="plugin",
        repository_id=101,
        token_provider=lambda: "synthetic-token",
        transport=httpx.MockTransport(api.respond),
    )

    @contextmanager
    def target_lock(target: str):
        assert target == "production"
        assert not lock_state[0]
        lock_state[0] = True
        try:
            yield
        finally:
            lock_state[0] = False

    def admission(intent) -> bool:
        assert lock_state[0]
        assert intent.evidence.repository_id == 101
        return admitted

    def verify_caller(identity: CallerIdentity) -> bool:
        assert lock_state[0]
        return identity == _caller()

    return MergeController(
        client,
        repository_id=101,
        owner_id=202,
        target="production",
        merge_transport=GitHubMergeTransport(client),
        target_lock=target_lock,
        current_deployed_commit=lambda target: deployed or api.bundle.deployed_commit,
        current_policy_digest=lambda: policy_digest or api.bundle.policy_digest,
        verify_caller=verify_caller,
        admit_merge=admission,
        admission_live=lambda: admission_is_live,
        persist_merge=lambda outcome: persisted.append(outcome),
    )


@pytest.fixture
def merge_fixture(tmp_path: Path):
    yield _fixture(tmp_path)


def test_merges_only_exact_head_and_reconciles_authenticated_git_objects(
    merge_fixture,
) -> None:
    bundle, snapshot, merge, tree = merge_fixture
    api = MergeApi(
        bundle,
        synthetic_merge=merge,
        merge_sha=merge,
        tree=tree,
        parents=(bundle.base_commit, bundle.head_commit),
    )
    lock_state = [False]
    persisted: list[object] = []
    controller = _controller(api, lock_state=lock_state, persisted=persisted)
    assert snapshot.target_branch == "main"
    assert controller._target == "production"
    assert controller._base_branch == "main"

    result = controller.merge(snapshot, bundle, caller=_caller())

    assert result.outcome == "merged_verified"
    assert result.evidence.merge_commit == merge
    assert result.evidence.tree == tree
    assert api.puts == 1
    assert persisted == [result]
    assert not lock_state[0]
    merge_request = next(request for request in api.requests if request.method == "PUT")
    assert merge_request.url.path.endswith("/pulls/42/merge")


def test_lost_merge_response_reconciles_once_without_remerging(merge_fixture) -> None:
    bundle, snapshot, merge, tree = merge_fixture
    api = MergeApi(
        bundle,
        synthetic_merge=merge,
        merge_sha=merge,
        tree=tree,
        parents=(bundle.base_commit, bundle.head_commit),
    )
    api.lost_response = True
    persisted: list[object] = []
    controller = _controller(api, lock_state=[False], persisted=persisted)

    result = controller.merge(snapshot, bundle, caller=_caller())

    assert result.outcome == "merged_verified"
    assert api.puts == 1
    assert len(persisted) == 1


def test_merge_that_precedes_controller_is_recorded_as_blocked_without_put(merge_fixture) -> None:
    bundle, snapshot, merge, tree = merge_fixture
    api = MergeApi(
        bundle,
        synthetic_merge=merge,
        merge_sha=merge,
        tree=tree,
        parents=(bundle.base_commit, bundle.head_commit),
    )
    api.closed = True
    persisted: list[object] = []
    controller = _controller(api, lock_state=[False], persisted=persisted)

    result = controller.merge(snapshot, bundle, caller=_caller())

    assert result.outcome == "merged_blocked"
    assert api.puts == 0
    assert persisted == [result]


@pytest.mark.parametrize("mutation", ["reversed_parents", "wrong_tree", "extra_parent"])
def test_unexpected_merge_objects_are_persisted_as_merged_blocked(merge_fixture, mutation) -> None:
    bundle, snapshot, merge, expected_tree = merge_fixture
    observed_parents = (bundle.base_commit, bundle.head_commit)
    observed_tree = expected_tree
    if mutation == "reversed_parents":
        observed_parents = (bundle.head_commit, bundle.base_commit)
    elif mutation == "wrong_tree":
        observed_tree = "f" * len(expected_tree)
    else:
        observed_parents = (*observed_parents, "9" * len(bundle.head_commit))
    api = MergeApi(
        bundle,
        synthetic_merge=merge,
        merge_sha=merge,
        tree=observed_tree,
        parents=observed_parents,
    )
    persisted: list[object] = []
    controller = _controller(api, lock_state=[False], persisted=persisted)

    result = controller.merge(snapshot, bundle, caller=_caller())

    assert result.outcome == "merged_blocked"
    assert result.parents == observed_parents
    assert persisted == [result]


def test_changed_base_stops_before_merge_and_closed_pr_is_never_remerged(merge_fixture) -> None:
    bundle, snapshot, merge, tree = merge_fixture
    api = MergeApi(
        bundle,
        synthetic_merge=merge,
        merge_sha=merge,
        tree=tree,
        parents=(bundle.base_commit, bundle.head_commit),
    )
    api.changed_base = "9" * len(bundle.base_commit)
    controller = _controller(api, lock_state=[False], persisted=[])
    with pytest.raises(MergeControlError) as error:
        controller.merge(snapshot, bundle, caller=_caller())
    assert error.value.code == "stale_pull_request"
    assert api.puts == 0

    api.changed_base = None
    api.close_without_merge = True
    with pytest.raises(MergeControlError) as closed:
        controller.merge(snapshot, bundle, caller=_caller())
    assert closed.value.code == "pull_request_closed"
    assert api.puts == 0


def test_broker_caller_and_admission_are_required_under_lock(merge_fixture) -> None:
    bundle, snapshot, merge, tree = merge_fixture
    api = MergeApi(
        bundle,
        synthetic_merge=merge,
        merge_sha=merge,
        tree=tree,
        parents=(bundle.base_commit, bundle.head_commit),
    )
    persisted: list[object] = []
    controller = _controller(api, lock_state=[False], persisted=persisted)
    with pytest.raises(MergeControlError) as error:
        controller.merge(snapshot, bundle, caller=replace(_caller(), run_id=999))
    assert error.value.code == "caller_denied"
    assert api.puts == 0


@pytest.mark.parametrize(
    ("deployed", "policy_digest", "code"),
    [("9" * 40, None, "deployment_moved"), (None, "f" * 64, "policy_moved")],
)
def test_deployed_commit_and_policy_are_rechecked_under_target_lock(
    merge_fixture, deployed, policy_digest, code
) -> None:
    bundle, snapshot, merge, tree = merge_fixture
    api = MergeApi(
        bundle,
        synthetic_merge=merge,
        merge_sha=merge,
        tree=tree,
        parents=(bundle.base_commit, bundle.head_commit),
    )
    controller = _controller(
        api,
        lock_state=[False],
        persisted=[],
        deployed=deployed,
        policy_digest=policy_digest,
    )
    with pytest.raises(MergeControlError) as error:
        controller.merge(snapshot, bundle, caller=_caller())
    assert error.value.code == code
    assert api.puts == 0


def test_expired_admission_receipt_cannot_start_github_merge(merge_fixture) -> None:
    bundle, snapshot, merge, tree = merge_fixture
    api = MergeApi(
        bundle,
        synthetic_merge=merge,
        merge_sha=merge,
        tree=tree,
        parents=(bundle.base_commit, bundle.head_commit),
    )
    controller = _controller(api, lock_state=[False], persisted=[], admission_is_live=False)
    with pytest.raises(MergeControlError) as error:
        controller.merge(snapshot, bundle, caller=_caller())
    assert error.value.code == "admission_denied"
    assert api.puts == 0


def test_broker_admission_denial_stops_before_pull_request_write(merge_fixture) -> None:
    bundle, snapshot, merge, tree = merge_fixture
    api = MergeApi(
        bundle,
        synthetic_merge=merge,
        merge_sha=merge,
        tree=tree,
        parents=(bundle.base_commit, bundle.head_commit),
    )
    controller = _controller(api, lock_state=[False], persisted=[], admitted=False)
    with pytest.raises(MergeControlError) as error:
        controller.merge(snapshot, bundle, caller=_caller())
    assert error.value.code == "admission_denied"
    assert api.puts == 0


def test_preexisting_merge_is_recorded_even_when_broker_admission_denies(merge_fixture) -> None:
    bundle, snapshot, merge, tree = merge_fixture
    api = MergeApi(
        bundle,
        synthetic_merge=merge,
        merge_sha=merge,
        tree=tree,
        parents=(bundle.base_commit, bundle.head_commit),
    )
    api.closed = True
    persisted: list[object] = []
    controller = _controller(api, lock_state=[False], persisted=persisted, admitted=False)

    result = controller.merge(snapshot, bundle, caller=_caller())

    assert result.outcome == "merged_blocked"
    assert api.puts == 0
    assert persisted == [result]
