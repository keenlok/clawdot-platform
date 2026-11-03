"""Least-privilege GitHub App installation-token issuer.

All HTTP is injected. This module has no network client and never logs or
includes credentials in its exception messages or object representations.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import jwt

_API_ROOT = "https://api.github.com"
_API_VERSION = "2022-11-28"
_MAX_APP_JWT_SECONDS = 540
_MAX_TOKEN_SECONDS = 3600
_TOKEN_RENEW_BEFORE_SECONDS = 300
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.,-]{1,4096}\Z")
_EXPECTED_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "metadata": "read",
}


class GitHubAppError(ValueError):
    """Credential-safe App issuer error code."""

    def __init__(self, code: str = "github_app_error") -> None:
        if code not in {
            "invalid_configuration",
            "signing_failed",
            "request_failed",
            "invalid_response",
            "token_expired",
        }:
            code = "github_app_error"
        super().__init__(code)
        self.code = code


class ProtectedKeyProvider(Protocol):
    def __call__(self, /) -> str | bytes: ...


class HttpPost(Protocol):
    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object],
        /,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, repr=False)
class InstallationToken:
    access_token: str = field(repr=False)
    expires_at: datetime

    def __repr__(self) -> str:
        return f"InstallationToken(expires_at={self.expires_at.isoformat()!r})"

    def __str__(self) -> str:
        return repr(self)


class GitHubAppTokenIssuer:
    """Create and renew one repository-selected, least-privilege token."""

    def __init__(
        self,
        *,
        app_id: int,
        installation_id: int,
        repository_id: int,
        protected_key_provider: ProtectedKeyProvider,
        http_post: HttpPost,
        now: Callable[[], datetime],
        renew_before_seconds: int = _TOKEN_RENEW_BEFORE_SECONDS,
    ) -> None:
        if (
            type(app_id) is not int
            or app_id <= 0
            or type(installation_id) is not int
            or installation_id <= 0
            or type(repository_id) is not int
            or repository_id <= 0
            or not callable(protected_key_provider)
            or not callable(http_post)
            or not callable(now)
            or type(renew_before_seconds) is not int
            or not 1 <= renew_before_seconds < _MAX_TOKEN_SECONDS
        ):
            raise GitHubAppError("invalid_configuration")
        self._app_id = app_id
        self._installation_id = installation_id
        self._repository_id = repository_id
        self._key_provider = protected_key_provider
        self._http_post = http_post
        self._now = now
        self._renew_before = renew_before_seconds
        self._cached: InstallationToken | None = None

    def get_token(self, *, force_refresh: bool = False) -> InstallationToken:
        if type(force_refresh) is not bool:
            raise GitHubAppError("invalid_configuration")
        current = _aware_utc(self._now())
        cached = self._cached
        if (
            not force_refresh
            and cached is not None
            and (cached.expires_at - current).total_seconds() > self._renew_before
        ):
            return cached

        app_jwt = self._make_app_jwt(current)
        url = f"{_API_ROOT}/app/installations/{self._installation_id}/access_tokens"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {app_jwt}",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        body = {
            "repository_ids": [self._repository_id],
            "permissions": dict(_EXPECTED_PERMISSIONS),
        }
        try:
            response = self._http_post(url, headers, body)
        except Exception:
            raise GitHubAppError("request_failed") from None
        try:
            token = _parse_token_response(response, current, self._repository_id)
        except GitHubAppError:
            raise
        except Exception:
            raise GitHubAppError("invalid_response") from None
        self._cached = token
        return token

    def _make_app_jwt(self, current: datetime) -> str:
        try:
            key = self._key_provider()
            encoded = jwt.encode(
                {
                    "iss": str(self._app_id),
                    "iat": int(current.timestamp()) - 30,
                    "exp": int(current.timestamp()) + _MAX_APP_JWT_SECONDS,
                },
                key,
                algorithm="RS256",
                headers={"typ": "JWT"},
            )
        except Exception:
            raise GitHubAppError("signing_failed") from None
        if not isinstance(encoded, str) or len(encoded) > 4096:
            raise GitHubAppError("signing_failed")
        return encoded


def _parse_token_response(
    response: Mapping[str, object], now: datetime, expected_repository_id: int
) -> InstallationToken:
    if not isinstance(response, Mapping):
        raise GitHubAppError("invalid_response")
    token = response.get("token")
    expires = response.get("expires_at")
    permissions = response.get("permissions")
    repositories = response.get("repositories")
    if (
        not isinstance(token, str)
        or not _TOKEN_PATTERN.fullmatch(token)
        or not isinstance(expires, str)
        or not isinstance(permissions, Mapping)
        or dict(permissions) != _EXPECTED_PERMISSIONS
        or not isinstance(repositories, list)
        or len(repositories) != 1
        or not isinstance(repositories[0], Mapping)
        or repositories[0].get("id") != expected_repository_id
    ):
        raise GitHubAppError("invalid_response")
    try:
        expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        expires_at = _aware_utc(expires_at)
    except (ValueError, TypeError, GitHubAppError):
        raise GitHubAppError("invalid_response") from None
    if (
        type(repositories[0].get("id")) is not int
        or expires_at <= now
        or (expires_at - now).total_seconds() > _MAX_TOKEN_SECONDS
    ):
        raise GitHubAppError("invalid_response")
    # The selected repository ID is bound by the request and the response is
    # additionally constrained to one repository; issuer checks its ID below.
    return InstallationToken(token, expires_at)


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise GitHubAppError("invalid_configuration")
    return value.astimezone(UTC)
