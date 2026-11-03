"""GitHub OIDC verification; callers supply JWKS from the fixed trusted issuer.

This authenticates an Actions job. It does not approve or admit a release. The
workflow closure and GitHub job correlation must be independently verified by
protected code; both callbacks are mandatory and outages deny the caller.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx
import jwt

from .git_evidence import verify_installed_blob_digests

ISSUER = "https://token.actions.githubusercontent.com"
_MAX_TOKEN = 16384
_MAX_JWKS = 65536
JWKS_TOTAL_TIMEOUT_SECONDS = 5.0


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OIDCError()
        result[key] = value
    return result


async def fetch_github_jwks(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, object]:
    """Fetch public keys with a total deadline in addition to I/O timeouts."""
    try:
        async with asyncio.timeout(JWKS_TOTAL_TIMEOUT_SECONDS):
            return await _fetch_github_jwks_snapshot(transport=transport)
    except Exception:
        raise OIDCError() from None


async def _fetch_github_jwks_snapshot(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, object]:
    """Fetch a fresh bounded public-key snapshot from the installed issuer.

    A fresh fetch handles public-key rotation without stale-cache acceptance.
    The injected transport is for protected service configuration/tests only.
    No inherited proxies, credentials, cookies or caller-selected URLs are used.
    """
    try:
        async with httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False, timeout=5.0
        ) as client:
            async with client.stream("GET", ISSUER + "/.well-known/jwks") as response:
                if response.status_code != 200:
                    raise OIDCError()
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if len(data) + len(chunk) > _MAX_JWKS:
                        raise OIDCError()
                    data.extend(chunk)
        value = json.loads(bytes(data), object_pairs_hook=_unique_keys)
        if not isinstance(value, dict) or set(value) != {"keys"}:
            raise OIDCError()
        keys = value["keys"]
        if not isinstance(keys, list) or not 1 <= len(keys) <= 16:
            raise OIDCError()
        return value
    except Exception:
        raise OIDCError() from None


class OIDCError(ValueError):
    """Fixed safe error; never contains a token, key or upstream diagnostics."""

    def __init__(self) -> None:
        super().__init__("caller_denied")


def github_main_subject(
    repository: str, owner_id: int, repository_id: int, subject_format: str
) -> str:
    """Build one explicitly installed GitHub subject format, never a wildcard."""
    if (
        not isinstance(repository, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
        or any(type(value) is not int or value <= 0 for value in (owner_id, repository_id))
        or subject_format not in {"legacy", "immutable"}
    ):
        raise OIDCError()
    owner, name = repository.split("/")
    identity = (
        repository if subject_format == "legacy" else f"{owner}@{owner_id}/{name}@{repository_id}"
    )
    return f"repo:{identity}:ref:refs/heads/main"


@dataclass(frozen=True)
class OIDCPolicy:
    audience: str
    repository_id: int
    owner_id: int
    repository: str
    workflow_path: str
    workflow_sha: str
    run_id: int
    run_attempt: int
    check_run_id: int
    runner_environment: str
    subject: str
    max_lifetime_seconds: int = 600

    def __post_init__(self) -> None:
        if (
            not isinstance(self.audience, str)
            or not 1 <= len(self.audience) <= 512
            or not isinstance(self.repository, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None
            or not isinstance(self.workflow_path, str)
            or re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", self.workflow_path) is None
            or not isinstance(self.workflow_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", self.workflow_sha) is None
            or self.runner_environment not in {"github-hosted", "self-hosted"}
            or not isinstance(self.subject, str)
            or not 1 <= len(self.subject) <= 512
            or any(
                type(value) is not int or value <= 0
                for value in (
                    self.repository_id,
                    self.owner_id,
                    self.run_id,
                    self.run_attempt,
                    self.check_run_id,
                    self.max_lifetime_seconds,
                )
            )
            or self.max_lifetime_seconds > 3600
        ):
            raise OIDCError()


@dataclass(frozen=True)
class CallerIdentity:
    repository_id: int
    owner_id: int
    workflow_sha: str
    run_id: int
    run_attempt: int
    check_run_id: int
    runner_environment: str


def installed_workflow_verifier(
    repository: str | os.PathLike[str], expected_blobs: Mapping[str, str]
) -> Callable[[CallerIdentity], bool]:
    """Bind verified OIDC workflow commits to an installed exact blob map.

    The protected service supplies the object store and complete installed map;
    candidate payloads must never supply either. Copy the mapping so a later
    caller mutation cannot change what a previously configured verifier checks.
    """
    frozen_blobs = dict(expected_blobs)

    def verify(identity: CallerIdentity) -> bool:
        verify_installed_blob_digests(repository, identity.workflow_sha, frozen_blobs)
        return True

    return verify


def verify_oidc(
    token: str,
    jwks: Mapping[str, object],
    policy: OIDCPolicy,
    *,
    verify_workflow: Callable[[CallerIdentity], bool],
    verify_job: Callable[[CallerIdentity], bool],
) -> CallerIdentity:
    """Verify a bounded token using public RSA keys, then independent facts.

    JWKS must be fetched from the installed issuer's fixed HTTPS endpoint by the
    service, never from token-supplied URLs. No caller may use an unverified decode
    as an identity. Callbacks must fetch/check the pinned installed execution
    closure and job ID/run/attempt/runner independently of workflow outputs.
    """
    try:
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= _MAX_TOKEN
            or not isinstance(policy, OIDCPolicy)
            or not isinstance(jwks, Mapping)
            or len(json.dumps(dict(jwks), allow_nan=False)) > _MAX_JWKS
        ):
            raise OIDCError()
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(kid, str)
            or not 1 <= len(kid) <= 128
            or any(name in header for name in ("jku", "x5u", "crit", "jwk"))
        ):
            raise OIDCError()
        keys = jwks.get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= 16:
            raise OIDCError()
        if any(not isinstance(item, dict) for item in keys):
            raise OIDCError()
        matches = [item for item in keys if item.get("kid") == kid]
        if len(matches) != 1:
            raise OIDCError()
        key_data = matches[0]
        if (
            key_data.get("kty") != "RSA"
            or key_data.get("use", "sig") != "sig"
            or key_data.get("alg", "RS256") != "RS256"
            or any(name in key_data for name in ("d", "p", "q", "dp", "dq", "qi"))
        ):
            raise OIDCError()
        key = jwt.PyJWK.from_dict(key_data, algorithm="RS256").key
        if key.key_size < 2048:
            raise OIDCError()
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=policy.audience,
            issuer=ISSUER,
            options={"require": ["iss", "aud", "sub", "exp", "iat", "nbf"]},
        )
        expected = {
            "sub": policy.subject,
            "repository_id": str(policy.repository_id),
            "repository_owner_id": str(policy.owner_id),
            "repository": policy.repository,
            "repository_visibility": "private",
            "event_name": "workflow_dispatch",
            "ref": "refs/heads/main",
            "workflow_ref": f"{policy.repository}/{policy.workflow_path}@refs/heads/main",
            "workflow_sha": policy.workflow_sha,
            "run_id": str(policy.run_id),
            "run_attempt": str(policy.run_attempt),
            "check_run_id": str(policy.check_run_id),
            "runner_environment": policy.runner_environment,
        }
        if any(claims.get(name) != value for name, value in expected.items()):
            raise OIDCError()
        if claims.get("aud") != policy.audience:
            raise OIDCError()
        if any(type(claims.get(name)) is not int for name in ("exp", "iat", "nbf")):
            raise OIDCError()
        if not 0 < claims["exp"] - claims["iat"] <= policy.max_lifetime_seconds:
            raise OIDCError()
        if claims["nbf"] > claims["exp"]:
            raise OIDCError()
        identity = CallerIdentity(
            policy.repository_id,
            policy.owner_id,
            policy.workflow_sha,
            policy.run_id,
            policy.run_attempt,
            policy.check_run_id,
            policy.runner_environment,
        )
        if verify_workflow(identity) is not True or verify_job(identity) is not True:
            raise OIDCError()
        return identity
    except Exception:
        raise OIDCError() from None
