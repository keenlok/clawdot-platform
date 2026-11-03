from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from chatcore_release_control.broker_app import BrokerAppHTTPPost, BrokerAppTokenIssuer
from chatcore_release_control.github_app import GitHubAppError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PERMISSIONS = {
    "actions": "write",
    "contents": "read",
    "pull_requests": "read",
    "checks": "read",
    "metadata": "read",
}


def issuer_fixture(response_change=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    clock = [datetime(2026, 10, 31, tzinfo=UTC)]
    calls = []

    def send(request):
        import json

        calls.append(request)
        claims = jwt.decode(
            request.headers["authorization"].split()[1],
            key.public_key(),
            algorithms=["RS256"],
            options={"verify_exp": False},
        )
        assert claims["iss"] == "11"
        assert json.loads(request.content) == {"repository_ids": [101], "permissions": PERMISSIONS}
        body = {
            "token": f"synthetic-token-{len(calls)}",
            "expires_at": (clock[0] + timedelta(hours=1)).isoformat(),
            "permissions": PERMISSIONS,
            "repositories": [{"id": 101}],
        }
        if response_change:
            body.update(response_change)
        return httpx.Response(201, json=body)

    post = BrokerAppHTTPPost(transport=httpx.MockTransport(send))
    issuer = BrokerAppTokenIssuer(
        app_id=11,
        installation_id=22,
        repository_id=101,
        protected_key_provider=lambda: pem,
        http_post=post,
        now=lambda: clock[0],
    )
    return issuer, post, clock, calls


def test_broker_app_has_distinct_least_privilege_scope_and_renews():
    issuer, post, clock, calls = issuer_fixture()
    try:
        first = issuer.get_token()
        assert first.access_token == "synthetic-token-1"
        assert issuer.get_token() is first
        assert "synthetic-token" not in repr(first)
        clock[0] += timedelta(minutes=56)
        assert issuer.get_token().access_token == "synthetic-token-2"
        assert len(calls) == 2
        assert str(calls[0].url) == "https://api.github.com/app/installations/22/access_tokens"
    finally:
        post.close()


@pytest.mark.parametrize(
    "change",
    [
        {"repositories": [{"id": 999}]},
        {"permissions": {**PERMISSIONS, "contents": "write"}},
        {"permissions": {**PERMISSIONS, "administration": "write"}},
        {"token": "secret\nsentinel"},
        {"expires_at": "2000-01-01T00:00:00Z"},
    ],
)
def test_broker_app_rejects_wrong_or_unsafe_token_scope(change):
    issuer, post, _, _ = issuer_fixture(change)
    try:
        with pytest.raises(GitHubAppError) as error:
            issuer.get_token()
        assert "sentinel" not in str(error.value)
    finally:
        post.close()


def test_broker_app_http_never_follows_redirects_or_accepts_other_origin():
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://foreign.invalid/token"})

    post = BrokerAppHTTPPost(transport=httpx.MockTransport(send))
    try:
        with pytest.raises(GitHubAppError):
            post("https://api.github.com/app/installations/22/access_tokens", {}, {})
        assert len(calls) == 1
        with pytest.raises(GitHubAppError):
            post("https://foreign.invalid/token", {}, {})
        assert len(calls) == 1
    finally:
        post.close()


def test_host_source_token_is_renewable_and_narrowed_to_contents_read_only():
    from chatcore_release_control.broker_app import SourceReadAppTokenIssuer

    parent, post, clock, _ = issuer_fixture()
    expected = {"contents": "read", "metadata": "read"}
    calls = []

    def send(url, headers, body):
        calls.append(body)
        assert body == {"repository_ids": [101], "permissions": expected}
        return {
            "token": f"source-token-{len(calls)}",
            "expires_at": (clock[0] + timedelta(hours=1)).isoformat(),
            "permissions": expected,
            "repositories": [{"id": 101}],
        }

    issuer = SourceReadAppTokenIssuer(
        app_id=11,
        installation_id=22,
        repository_id=101,
        protected_key_provider=parent._key_provider,
        http_post=send,
        now=lambda: clock[0],
    )
    try:
        assert issuer.get_token().access_token == "source-token-1"
        clock[0] += timedelta(minutes=56)
        assert issuer.get_token().access_token == "source-token-2"
        assert len(calls) == 2
    finally:
        post.close()
