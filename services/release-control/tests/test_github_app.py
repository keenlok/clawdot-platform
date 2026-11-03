from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from chatcore_release_control.github_app import (
    GitHubAppError,
    GitHubAppTokenIssuer,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

APP_ID = 123
INSTALLATION_ID = 456
REPOSITORY_ID = 789
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def _keypair() -> tuple[bytes, object]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, private.public_key()


def _response(
    token: str = "ghs_synthetic_installation_token",
    expires_at: datetime | None = None,
    *,
    repository_id: int = REPOSITORY_ID,
    permissions: dict[str, str] | None = None,
) -> dict[str, object]:
    expires = expires_at or NOW + timedelta(minutes=55)
    return {
        "token": token,
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "permissions": permissions
        or {"contents": "write", "pull_requests": "write", "metadata": "read"},
        "repositories": [{"id": repository_id, "name": "private-repo"}],
    }


def _issuer(http_post, key_provider, now=lambda: NOW) -> GitHubAppTokenIssuer:
    return GitHubAppTokenIssuer(
        app_id=APP_ID,
        installation_id=INSTALLATION_ID,
        repository_id=REPOSITORY_ID,
        protected_key_provider=key_provider,
        http_post=http_post,
        now=now,
    )


def test_signs_short_lived_app_jwt_and_requests_repository_scoped_permissions() -> None:
    private_pem, public_key = _keypair()
    calls: list[tuple[str, dict[str, str], dict[str, object]]] = []

    def http_post(url, headers, body):
        calls.append((url, dict(headers), dict(body)))
        return _response()

    issuer = _issuer(http_post, lambda: private_pem)
    token = issuer.get_token()

    assert len(calls) == 1
    url, headers, body = calls[0]
    assert url == ("https://api.github.com/app/installations/456/access_tokens")
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert headers["Authorization"].startswith("Bearer ")
    claims = jwt.decode(
        headers["Authorization"].removeprefix("Bearer "),
        public_key,
        algorithms=["RS256"],
        options={"verify_iat": False, "verify_exp": False},
    )
    assert claims["iss"] == str(APP_ID)
    assert claims["iat"] == int(NOW.timestamp()) - 30
    assert claims["exp"] - claims["iat"] == 570
    assert body == {
        "repository_ids": [REPOSITORY_ID],
        "permissions": {
            "contents": "write",
            "pull_requests": "write",
            "metadata": "read",
        },
    }
    assert token.access_token == "ghs_synthetic_installation_token"
    assert "synthetic_installation_token" not in repr(token)
    assert "synthetic_installation_token" not in str(token)


def test_reuses_cached_token_then_renews_before_expiry() -> None:
    private_pem, _ = _keypair()
    clock = [NOW]
    responses = [
        _response("ghs_first", NOW + timedelta(minutes=20)),
        _response("ghs_second", NOW + timedelta(minutes=50)),
    ]
    calls = 0

    def http_post(*_):
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    issuer = _issuer(http_post, lambda: private_pem, now=lambda: clock[0])
    first = issuer.get_token()
    assert issuer.get_token() is first
    assert calls == 1

    clock[0] += timedelta(minutes=16)
    second = issuer.get_token()
    assert second.access_token == "ghs_second"
    assert calls == 2


def test_force_refresh_replaces_cached_token() -> None:
    private_pem, _ = _keypair()
    responses = [_response("ghs_first"), _response("ghs_second")]
    issuer = _issuer(lambda *_: responses.pop(0), lambda: private_pem)
    assert issuer.get_token().access_token == "ghs_first"
    assert issuer.get_token(force_refresh=True).access_token == "ghs_second"


@pytest.mark.parametrize(
    "response",
    [
        _response(repository_id=REPOSITORY_ID + 1),
        _response(permissions={"contents": "write", "pull_requests": "write"}),
        _response(expires_at=NOW - timedelta(seconds=1)),
        _response(expires_at=NOW + timedelta(hours=2)),
        {
            **_response(),
            "expires_at": (NOW + timedelta(minutes=55)).replace(tzinfo=None).isoformat(),
        },
        _response(token="bad token with spaces"),
    ],
)
def test_rejects_wrong_repository_permissions_or_invalid_tokens(response) -> None:
    private_pem, _ = _keypair()
    issuer = _issuer(lambda *_: response, lambda: private_pem)
    with pytest.raises(GitHubAppError) as error:
        issuer.get_token()
    assert error.value.code == "invalid_response"
    assert "bad token" not in str(error.value)


def test_sanitizes_key_and_http_failures() -> None:
    issuer = _issuer(
        lambda *_: (_ for _ in ()).throw(RuntimeError("leaked ghs_private")),
        lambda: (_ for _ in ()).throw(RuntimeError("private key material")),
    )
    with pytest.raises(GitHubAppError) as error:
        issuer.get_token()
    assert error.value.code == "signing_failed"
    assert "private key material" not in str(error.value)

    private_pem, _ = _keypair()
    issuer = _issuer(
        lambda *_: (_ for _ in ()).throw(RuntimeError("leaked ghs_private")),
        lambda: private_pem,
    )
    with pytest.raises(GitHubAppError) as error:
        issuer.get_token()
    assert error.value.code == "request_failed"
    assert "ghs_private" not in str(error.value)


def test_rejects_invalid_configuration_and_clock() -> None:
    private_pem, _ = _keypair()
    with pytest.raises(GitHubAppError) as error:
        GitHubAppTokenIssuer(
            app_id=0,
            installation_id=INSTALLATION_ID,
            repository_id=REPOSITORY_ID,
            protected_key_provider=lambda: private_pem,
            http_post=lambda *_: _response(),
            now=lambda: NOW,
        )
    assert error.value.code == "invalid_configuration"

    issuer = _issuer(lambda *_: _response(), lambda: private_pem, now=lambda: datetime.now())
    with pytest.raises(GitHubAppError) as error:
        issuer.get_token()
    assert error.value.code == "invalid_configuration"
