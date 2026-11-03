from __future__ import annotations

import json
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from chatcore_release_control.development import CommandResult
from chatcore_release_control.github_app import InstallationToken
from chatcore_release_control.publication import PublicationError, PublishCommand
from chatcore_release_control.publication_github import (
    GitHubAppHttpPost,
    GitHubPublicationError,
    GitHubPublicationGateway,
)

OWNER, REPOSITORY, REPOSITORY_ID, OWNER_ID = "acme", "plugin", 101, 202
TOKEN = "ghs_synthetic_publication_token"
HEAD = "a" * 40
BASE = "b" * 40


class _Issuer:
    def get_token(self, *, force_refresh=False):
        return InstallationToken(TOKEN, datetime.max.replace(tzinfo=UTC))


def _repo_payload():
    return {
        "id": REPOSITORY_ID,
        "full_name": f"{OWNER}/{REPOSITORY}",
        "private": True,
        "owner": {"id": OWNER_ID, "login": OWNER},
    }


def _pr_payload(*, number=9, head=HEAD, base=BASE, state="open", merged=False):
    return {
        "number": number,
        "state": state,
        "merged_at": None if not merged else "2026-10-31T00:00:00Z",
        "title": "Add safe candidate",
        "body": "Synthetic summary.",
        "head": {
            "sha": head,
            "ref": "clawdot/request-123",
            "repo": {"id": REPOSITORY_ID, "owner": {"id": OWNER_ID}},
        },
        "base": {
            "sha": base,
            "ref": "main",
            "repo": {"id": REPOSITORY_ID, "owner": {"id": OWNER_ID}},
        },
    }


def _fixture(tmp_path, handler, executor):
    client = GitHubPublicationGateway(
        owner=OWNER,
        repository=REPOSITORY,
        repository_id=REPOSITORY_ID,
        owner_id=OWNER_ID,
        token_issuer=_Issuer(),
        transport=httpx.MockTransport(handler),
        git_executor=executor,
        temporary_directory=tmp_path,
    )
    return client


def test_app_http_post_adapter_pins_api_origin_and_bounds_response():
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) == ("https://api.github.com/app/installations/456/access_tokens")
        assert json.loads(request.content) == {
            "repository_ids": [REPOSITORY_ID],
            "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
        }
        return httpx.Response(200, json={"token": "synthetic"})

    adapter = GitHubAppHttpPost(
        transport=httpx.MockTransport(handler),
        installation_id=456,
    )
    result = adapter(
        "https://api.github.com/app/installations/456/access_tokens",
        {"Authorization": "Bearer synthetic-jwt"},
        {
            "repository_ids": [REPOSITORY_ID],
            "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
        },
    )
    assert result == {"token": "synthetic"}
    assert len(seen) == 1
    with pytest.raises(GitHubPublicationError):
        adapter(
            "https://attacker.invalid/app/installations/456/access_tokens",
            {},
            {},
        )


def test_fixed_repository_push_uses_protected_askpass_and_exact_create_only_ref(tmp_path):
    refs: dict[str, str] = {}
    calls = []
    final = "refs/heads/clawdot/request-123"
    staging = f"refs/heads/clawdot/objects/request-123/{HEAD}"

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/repos/acme/plugin":
            return httpx.Response(200, json=_repo_payload())
        if request.method == "GET" and "/git/ref/heads/" in request.url.path:
            ref = "refs/heads/" + request.url.path.split("/git/ref/heads/", 1)[1]
            if ref not in refs:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "ref": ref,
                    "object": {"sha": refs[ref], "type": "commit"},
                },
            )
        if request.method == "POST" and request.url.path == "/repos/acme/plugin/git/refs":
            value = json.loads(request.content)
            assert value == {"ref": final, "sha": HEAD}
            if final in refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            refs[final] = value["sha"]
            return httpx.Response(201, json={"ref": final, "object": {"sha": HEAD}})
        if request.method == "DELETE" and "/git/refs/heads/" in request.url.path:
            ref = "refs/heads/" + request.url.path.split("/git/refs/heads/", 1)[1]
            refs.pop(ref, None)
            return httpx.Response(204)
        raise AssertionError((request.method, request.url))

    def executor(argv: Sequence[str], env: Mapping[str, str], timeout, output_limit):
        calls.append(("git", tuple(argv)))
        if "push" not in argv:
            return CommandResult(0, b"", b"")
        assert TOKEN not in " ".join(argv)
        assert TOKEN not in " ".join(env.values())
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS_REQUIRE"] == "force"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert "protocol.allow=never" in argv
        assert "protocol.https.allow=always" in argv
        assert not (Path(env["GIT_ASKPASS"]).stat().st_mode & 0o077)
        token_path = Path(env["GITHUB_APP_TOKEN_FILE"])
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
        assert token_path.read_text() == TOKEN
        command = " ".join(argv)
        assert "--force" not in command and "--mirror" not in command and "--tags" not in command
        assert f"{HEAD}:{staging}" in command
        refs[staging] = HEAD
        return CommandResult(0, b"ok\n", b"")

    gateway = _fixture(tmp_path, handler, executor)
    gateway.verify_repository()
    quarantine = tmp_path / "quarantine.git"
    (quarantine / "objects").mkdir(parents=True)
    (quarantine / "HEAD").write_text("ref: refs/heads/main\n")
    result = gateway.publish(
        PublishCommand(
            request_id="request-123",
            repository_id=REPOSITORY_ID,
            frozen_commit=HEAD,
            refspec=f"{HEAD}:refs/heads/clawdot/request-123",
            quarantine_git_dir=quarantine,
        )
    )
    assert result.commit == HEAD
    assert result.ref == final
    assert refs == {final: HEAD}
    assert not list(tmp_path.glob("publication-auth-*"))
    gateway.close()


def test_final_ref_race_is_rejected_without_overwriting_existing_ref(tmp_path):
    final = "refs/heads/clawdot/request-123"
    staging = f"refs/heads/clawdot/objects/request-123/{HEAD}"
    refs = {}

    def handler(request):
        if request.url.path == "/repos/acme/plugin":
            return httpx.Response(200, json=_repo_payload())
        if request.method == "GET" and "/git/ref/heads/" in request.url.path:
            ref = "refs/heads/" + request.url.path.split("/git/ref/heads/", 1)[1]
            if ref not in refs:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={"ref": ref, "object": {"sha": refs[ref], "type": "commit"}},
            )
        if request.method == "POST" and request.url.path == "/repos/acme/plugin/git/refs":
            assert json.loads(request.content) == {"ref": final, "sha": HEAD}
            return httpx.Response(422, json={"message": "Reference already exists"})
        raise AssertionError((request.method, request.url))

    def executor(argv, _env, _timeout, _limit):
        if "push" not in argv:
            return CommandResult(0, b"", b"")
        assert f"{HEAD}:{staging}" in " ".join(argv)
        refs[staging] = HEAD
        # Simulate a concurrent writer creating the final ref at B after the
        # initial absence read but before the final atomic create request.
        refs[final] = BASE
        return CommandResult(0, b"ok\n", b"")

    gateway = _fixture(tmp_path, handler, executor)
    gateway.verify_repository()
    quarantine = tmp_path / "quarantine.git"
    (quarantine / "objects").mkdir(parents=True)
    (quarantine / "HEAD").write_text("ref: refs/heads/main\n")
    command = PublishCommand("request-123", REPOSITORY_ID, HEAD, f"{HEAD}:{final}", quarantine)

    with pytest.raises(PublicationError) as rejected:
        gateway.publish(command)

    assert rejected.value.code == "publication_mismatch"
    assert refs[final] == BASE


def test_staging_cleanup_failure_discloses_published_ref_and_reconciles_retry(tmp_path):
    final = "refs/heads/clawdot/request-123"
    staging = f"refs/heads/clawdot/objects/request-123/{HEAD}"
    refs = {}
    fail_delete = True

    def handler(request):
        if request.url.path == "/repos/acme/plugin":
            return httpx.Response(200, json=_repo_payload())
        if request.method == "GET" and "/git/ref/heads/" in request.url.path:
            ref = "refs/heads/" + request.url.path.split("/git/ref/heads/", 1)[1]
            if ref not in refs:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={"ref": ref, "object": {"sha": refs[ref], "type": "commit"}},
            )
        if request.method == "POST" and request.url.path == "/repos/acme/plugin/git/refs":
            if final in refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            refs[final] = HEAD
            return httpx.Response(201, json={"ref": final, "object": {"sha": HEAD}})
        if request.method == "DELETE" and "/git/refs/heads/" in request.url.path:
            if fail_delete:
                return httpx.Response(503, json={"message": "temporary failure"})
            refs.pop(staging, None)
            return httpx.Response(204)
        raise AssertionError((request.method, request.url))

    def executor(argv, _env, _timeout, _limit):
        if "push" in argv:
            refs[staging] = HEAD
        return CommandResult(0, b"", b"")

    gateway = _fixture(tmp_path, handler, executor)
    gateway.verify_repository()
    quarantine = tmp_path / "quarantine.git"
    (quarantine / "objects").mkdir(parents=True)
    (quarantine / "HEAD").write_text("ref: refs/heads/main\n")
    command = PublishCommand("request-123", REPOSITORY_ID, HEAD, f"{HEAD}:{final}", quarantine)

    with pytest.raises(GitHubPublicationError) as cleanup_pending:
        gateway.publish(command)
    assert cleanup_pending.value.code == "publication_cleanup_pending"
    assert refs[final] == HEAD
    assert refs[staging] == HEAD

    fail_delete = False
    result = gateway.publish(command)
    assert result.commit == HEAD
    assert refs == {final: HEAD}


def test_push_reuses_same_frozen_ref_but_rejects_different_existing_commit(tmp_path):
    ref = "refs/heads/clawdot/request-123"
    calls = []

    def handler(request):
        if request.url.path == "/repos/acme/plugin":
            return httpx.Response(200, json=_repo_payload())
        if request.url.path.endswith("/git/ref/heads/clawdot/request-123"):
            calls.append("ref")
            return httpx.Response(
                200,
                json={"ref": ref, "object": {"sha": HEAD, "type": "commit"}},
            )
        if "/git/ref/heads/" in request.url.path:
            return httpx.Response(404, json={"message": "Not Found"})
        raise AssertionError(request.url)

    def executor(*_):
        calls.append("push")
        return CommandResult(0, b"", b"")

    gateway = _fixture(tmp_path, handler, executor)
    gateway.verify_repository()
    command = PublishCommand("request-123", REPOSITORY_ID, HEAD, f"{HEAD}:{ref}", tmp_path)
    assert gateway.publish(command).commit == HEAD
    assert calls == ["ref"]

    def conflict_handler(request):
        if request.url.path == "/repos/acme/plugin":
            return httpx.Response(200, json=_repo_payload())
        if request.url.path.endswith("/git/ref/heads/clawdot/request-123"):
            return httpx.Response(
                200,
                json={"ref": ref, "object": {"sha": "c" * 40, "type": "commit"}},
            )
        return httpx.Response(404, json={"message": "Not Found"})

    conflicting = _fixture(tmp_path, conflict_handler, executor)
    conflicting.verify_repository()
    with pytest.raises(PublicationError) as error:
        conflicting.publish(command)
    assert error.value.code == "publication_mismatch"


def test_pull_request_creation_reconciles_lost_response_without_duplicate(tmp_path):
    created = 0

    def handler(request):
        nonlocal created
        if request.url.path == "/repos/acme/plugin":
            return httpx.Response(200, json=_repo_payload())
        if request.url.path.endswith("/git/ref/heads/clawdot/request-123"):
            return httpx.Response(
                200,
                json={
                    "ref": "refs/heads/clawdot/request-123",
                    "object": {"sha": HEAD, "type": "commit"},
                },
            )
        if request.url.path == "/repos/acme/plugin/pulls" and request.method == "GET":
            page = int(request.url.params.get("page", "1"))
            if page == 1:
                return httpx.Response(
                    200,
                    json=[],
                    headers={
                        "Link": (
                            '<https://api.github.com/repos/acme/plugin/pulls?page=2>; rel="next"'
                        )
                    },
                )
            return httpx.Response(200, json=[_pr_payload()] if created else [])
        if request.url.path == "/repos/acme/plugin/pulls" and request.method == "POST":
            created += 1
            return httpx.Response(502, json={"message": "synthetic lost response"})
        raise AssertionError((request.method, request.url))

    gateway = _fixture(tmp_path, handler, lambda *_: CommandResult(0, b"", b""))
    gateway.verify_repository()
    # This test requires an already-published branch; it exercises API reconciliation only.
    receipt = gateway.create_or_get_pull_request(
        request_id="request-123",
        head_sha=HEAD,
        base_sha=BASE,
        title="Add safe candidate",
        body="Synthetic summary.",
    )
    assert receipt.pull_request_number == 9
    assert receipt.head_sha == HEAD and receipt.base_sha == BASE
    assert created == 1


def test_pull_request_identity_conflict_is_denied(tmp_path):
    def handler(request):
        if request.url.path == "/repos/acme/plugin":
            return httpx.Response(200, json=_repo_payload())
        if request.url.path.endswith("/git/ref/heads/clawdot/request-123"):
            return httpx.Response(
                200,
                json={
                    "ref": "refs/heads/clawdot/request-123",
                    "object": {"sha": HEAD, "type": "commit"},
                },
            )
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[_pr_payload(head="c" * 40)])
        raise AssertionError(request.url)

    gateway = _fixture(tmp_path, handler, lambda *_: CommandResult(0, b"", b""))
    gateway.verify_repository()
    with pytest.raises(GitHubPublicationError):
        gateway.create_or_get_pull_request(
            request_id="request-123",
            head_sha=HEAD,
            base_sha=BASE,
            title="Add safe candidate",
            body="Synthetic summary.",
        )
