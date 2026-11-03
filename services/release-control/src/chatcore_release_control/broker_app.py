"""Separate renewable Broker App credentials, confined to the protected service."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from datetime import datetime

import httpx

from .github_app import (
    GitHubAppError,
    GitHubAppTokenIssuer,
    InstallationToken,
    _aware_utc,
)

BROKER_PERMISSIONS = {
    "actions": "write",
    "contents": "read",
    "pull_requests": "read",
    "checks": "read",
    "metadata": "read",
}
_ENDPOINT = re.compile(r"https://api\.github\.com/app/installations/[1-9][0-9]*/access_tokens\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_.,-]{1,4096}\Z")


class BrokerAppTokenIssuer(GitHubAppTokenIssuer):
    """Same constructor as the publication issuer, with a fixed disjoint role."""

    _permissions = BROKER_PERMISSIONS

    def get_token(self, *, force_refresh: bool = False) -> InstallationToken:
        if type(force_refresh) is not bool:
            raise GitHubAppError("invalid_configuration")
        current = _aware_utc(self._now())
        if (
            not force_refresh
            and self._cached is not None
            and (self._cached.expires_at - current).total_seconds() > self._renew_before
        ):
            return self._cached
        signed = self._make_app_jwt(current)
        try:
            value = self._http_post(
                f"https://api.github.com/app/installations/{self._installation_id}/access_tokens",
                {
                    "Authorization": f"Bearer {signed}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                {"repository_ids": [self._repository_id], "permissions": dict(self._permissions)},
            )
        except Exception:
            raise GitHubAppError("request_failed") from None
        try:
            if (
                not isinstance(value, Mapping)
                or value.get("permissions") != self._permissions
                or not isinstance(value.get("repositories"), list)
                or len(value["repositories"]) != 1
                or not isinstance(value["repositories"][0], Mapping)
                or type(value["repositories"][0].get("id")) is not int
                or value["repositories"][0]["id"] != self._repository_id
                or not isinstance(value.get("token"), str)
                or not _TOKEN.fullmatch(value["token"])
                or not isinstance(value.get("expires_at"), str)
            ):
                raise ValueError
            expires = _aware_utc(datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00")))
            if not 0 < (expires - current).total_seconds() <= 3600:
                raise ValueError
            result = InstallationToken(value["token"], expires)
        except Exception:
            raise GitHubAppError("invalid_response") from None
        self._cached = result
        return result


class SourceReadAppTokenIssuer(BrokerAppTokenIssuer):
    """The same protected Broker App, with no Actions or repository write grant."""

    _permissions = {"contents": "read", "metadata": "read"}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class BrokerAppHTTPPost:
    """Fixed token endpoint only, bounded TLS HTTP without proxies or redirects."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(5.0, connect=3.0),
        )

    def __call__(
        self, url: str, headers: Mapping[str, str], body: Mapping[str, object], /
    ) -> Mapping[str, object]:
        if not isinstance(url, str) or not _ENDPOINT.fullmatch(url):
            raise GitHubAppError("request_failed")
        try:
            deadline = time.monotonic() + 10
            with self._http.stream("POST", url, headers=headers, json=dict(body)) as response:
                if response.status_code != 201:
                    raise ValueError
                raw = bytearray()
                for chunk in response.iter_bytes(8192):
                    if len(raw) + len(chunk) > 64 * 1024 or time.monotonic() >= deadline:
                        raise ValueError
                    raw.extend(chunk)
            result = json.loads(raw, object_pairs_hook=_unique)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except Exception:
            raise GitHubAppError("request_failed") from None

    def close(self) -> None:
        self._http.close()
