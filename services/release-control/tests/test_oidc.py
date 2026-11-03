from __future__ import annotations

import json
import time
from dataclasses import replace

import jwt
import pytest
from chatcore_release_control.oidc import OIDCError, OIDCPolicy, verify_oidc
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def policy():
    return OIDCPolicy(
        audience="https://broker.invalid/release",
        repository_id=101,
        owner_id=202,
        repository="owner/repo",
        workflow_path=".github/workflows/release.yml",
        workflow_sha="a" * 40,
        run_id=7,
        run_attempt=2,
        check_run_id=8,
        runner_environment="github-hosted",
        subject="repo:owner/repo:ref:refs/heads/main",
    )


def claims():
    now = int(time.time())
    return {
        "iss": "https://token.actions.githubusercontent.com",
        "aud": policy().audience,
        "sub": policy().subject,
        "iat": now - 1,
        "nbf": now - 1,
        "exp": now + 120,
        "repository_id": "101",
        "repository_owner_id": "202",
        "repository": "owner/repo",
        "repository_visibility": "private",
        "event_name": "workflow_dispatch",
        "ref": "refs/heads/main",
        "workflow_ref": "owner/repo/.github/workflows/release.yml@refs/heads/main",
        "workflow_sha": "a" * 40,
        "run_id": "7",
        "run_attempt": "2",
        "check_run_id": "8",
        "runner_environment": "github-hosted",
    }


def jwks(key):
    value = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {"keys": [{**value, "kid": "key1", "use": "sig", "alg": "RS256"}]}


def token(key, **changes):
    return jwt.encode({**claims(), **changes}, key, algorithm="RS256", headers={"kid": "key1"})


def test_installed_subject_format_supports_immutable_numeric_repository_identity(key):
    from chatcore_release_control.oidc import github_main_subject

    subject = github_main_subject("owner/repo", 202, 101, "immutable")
    assert subject == "repo:owner@202/repo@101:ref:refs/heads/main"
    result = verify_oidc(
        token(key, sub=subject),
        jwks(key),
        replace(policy(), subject=subject),
        verify_workflow=lambda _: True,
        verify_job=lambda _: True,
    )
    assert result.repository_id == 101
    with pytest.raises(OIDCError):
        verify_oidc(
            token(key, sub=subject),
            jwks(key),
            policy(),
            verify_workflow=lambda _: True,
            verify_job=lambda _: True,
        )
    with pytest.raises(OIDCError):
        github_main_subject("owner/repo", 202, 101, "unreviewed-format")


def test_signature_and_independent_closure_job_verification_are_required(key):
    calls = []
    result = verify_oidc(
        token(key),
        jwks(key),
        policy(),
        verify_workflow=lambda identity: calls.append(identity.workflow_sha) is None,
        verify_job=lambda identity: identity.check_run_id == 8,
    )
    assert result.run_id == 7
    assert calls == ["a" * 40]
    assert "eyJ" not in repr(result)
    for workflow, job in ((False, True), (True, False)):
        with pytest.raises(OIDCError, match="caller_denied"):
            verify_oidc(
                token(key),
                jwks(key),
                policy(),
                verify_workflow=lambda _, result=workflow: result,
                verify_job=lambda _, result=job: result,
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"aud": "wrong"},
        {"iss": "https://wrong.invalid"},
        {"repository_id": "999"},
        {"repository_visibility": "public"},
        {"run_attempt": "1"},
        {"runner_environment": "self-hosted"},
        {"ref": "refs/heads/other"},
        {"workflow_sha": "b" * 40},
        {"exp": 1},
        {"iat": 9999999999},
        {"nbf": 9999999999},
        {"check_run_id": None},
    ],
)
def test_wrong_or_missing_claims_are_denied_without_leaking_token(key, changes):
    value = token(key, **changes)
    with pytest.raises(OIDCError) as caught:
        verify_oidc(
            value, jwks(key), policy(), verify_workflow=lambda _: True, verify_job=lambda _: True
        )
    assert value not in str(caught.value)


def test_wrong_signature_duplicate_kid_and_algorithm_confusion_are_denied(key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    duplicate = jwks(key)
    duplicate["keys"] *= 2
    forged = jwt.encode(
        claims(), "synthetic-test-key-only-32-bytes!!", algorithm="HS256", headers={"kid": "key1"}
    )
    for value, keys in ((token(other), jwks(key)), (token(key), duplicate), (forged, jwks(key))):
        with pytest.raises(OIDCError):
            verify_oidc(
                value, keys, policy(), verify_workflow=lambda _: True, verify_job=lambda _: True
            )


def test_verifier_outage_and_wrong_job_correlation_fail_closed(key):
    def outage(_):
        raise RuntimeError("synthetic private diagnostics")

    with pytest.raises(OIDCError, match="caller_denied") as caught:
        verify_oidc(
            token(key), jwks(key), policy(), verify_workflow=outage, verify_job=lambda _: True
        )
    assert "diagnostics" not in str(caught.value)
    with pytest.raises(OIDCError):
        verify_oidc(
            token(key),
            jwks(key),
            replace(policy(), check_run_id=9),
            verify_workflow=lambda _: True,
            verify_job=lambda _: True,
        )


def test_verified_caller_is_denied_when_installed_helper_blob_differs(key, tmp_path):
    import hashlib
    import os
    import subprocess

    from chatcore_release_control.oidc import installed_workflow_verifier

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], env=env).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Synthetic fixture")
    git("config", "user.email", "test@example.invalid")
    git("config", "core.hooksPath", os.devnull)
    (repo / ".github/workflows").mkdir(parents=True)
    source = "name: synthetic control only\n"
    (repo / ".github/workflows/release.yml").write_text(source)
    git("add", ".")
    git("commit", "-qm", "synthetic fixture")
    commit = git("rev-parse", "HEAD")
    trusted = replace(policy(), workflow_sha=commit)
    good = installed_workflow_verifier(
        repo, {".github/workflows/release.yml": hashlib.sha256(source.encode()).hexdigest()}
    )
    result = verify_oidc(
        token(key, workflow_sha=commit),
        jwks(key),
        trusted,
        verify_workflow=good,
        verify_job=lambda _: True,
    )
    assert result.workflow_sha == commit
    wrong = installed_workflow_verifier(repo, {".github/workflows/release.yml": "f" * 64})
    with pytest.raises(OIDCError):
        verify_oidc(
            token(key, workflow_sha=commit),
            jwks(key),
            trusted,
            verify_workflow=wrong,
            verify_job=lambda _: True,
        )


async def test_jwks_fetch_is_fixed_origin_bounded_and_does_not_follow_redirects(key):
    import httpx
    from chatcore_release_control.oidc import fetch_github_jwks

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=jwks(key))

    assert await fetch_github_jwks(transport=httpx.MockTransport(respond)) == jwks(key)
    assert str(requests[0].url) == "https://token.actions.githubusercontent.com/.well-known/jwks"
    assert "authorization" not in requests[0].headers

    for response in (
        httpx.Response(302, headers={"Location": "https://wrong.invalid"}),
        httpx.Response(200, content=b"x" * 65537),
        httpx.Response(200, content=b'{"keys":[],"keys":[]}'),
    ):
        with pytest.raises(OIDCError):
            await fetch_github_jwks(
                transport=httpx.MockTransport(lambda _, result=response: result)
            )


async def test_jwks_total_deadline_cancels_a_slow_stream(monkeypatch, key):
    import asyncio

    import chatcore_release_control.oidc as oidc
    import httpx

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.1)
            yield json.dumps(jwks(key)).encode()

    monkeypatch.setattr(oidc, "JWKS_TOTAL_TIMEOUT_SECONDS", 0.01, raising=False)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowBody()))
    with pytest.raises(OIDCError):
        await oidc.fetch_github_jwks(transport=transport)
