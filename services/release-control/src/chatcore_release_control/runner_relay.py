"""Fixed-origin GitHub Actions OIDC relay for P09 deployment jobs.

The command accepts approved release and operation UUIDs only. It exchanges the
runner's short-lived Actions OIDC request capability, sends that token to one
configured broker origin, and emits a small allowlisted receipt summary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

_ACTIONS_OIDC_ORIGIN = "https://pipelines.actions.githubusercontent.com"
_ACTIONS_TOKEN_PATH = re.compile(
    r"/[0-9a-fA-F-]{36}/_apis/pipelines/[A-Za-z0-9/_-]{1,512}/idtoken\Z"
)
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,4096}\Z")
_JWT = re.compile(r"(?=.{1,8192}\Z)[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{16,}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RECEIPT = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_PHASES = frozenset(
    {
        "prepared",
        "switching",
        "checking",
        "healthy",
        "restoring",
        "restored_healthy",
        "recovery_failed",
    }
)
_TERMINAL_PHASES = frozenset({"healthy", "restored_healthy", "recovery_failed"})
_RELAY_TIMEOUT_SECONDS = 900.0
_MAX_BODY_BYTES = 16 * 1024
_MAX_TOKEN_BODY_BYTES = 16 * 1024
_NOT_FOUND_RETRIES = 3
_REQUEST_CAPABILITY_TOKEN_NAME = "ACTIONS_ID_TOKEN_REQUEST_TOKEN"  # noqa: S105 - variable name, not a credential


class RelayError(RuntimeError):
    """Sanitized relay failure that contains no HTTP body or credential."""


class _RelayNotFound(RelayError):
    pass


class _RelayTransportFailure(RelayError):
    pass


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise RelayError("invalid relay IDs")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise RelayError("invalid relay IDs") from None
    return value


def _origin(value: object) -> str:
    if not isinstance(value, str):
        raise RelayError("fixed broker origin is unavailable")
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme == "https"
            and bool(parts.hostname)
            and parts.path in {"", "/"}
            and not parts.query
            and not parts.fragment
            and parts.username is None
            and parts.password is None
            and parts.port in {None, 443}
            and not any(ord(character) < 33 for character in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise RelayError("fixed broker origin is unavailable")
    return value.rstrip("/")


def _json_body(raw: bytes) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("object expected")
    return value


@dataclass(frozen=True, slots=True)
class RelayHTTPResponse:
    status_code: int
    body: bytes


class RelayHTTPClient:
    """Tiny injectable HTTP boundary; production implementation uses only stdlib."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
        max_body_bytes: int,
    ) -> RelayHTTPResponse:
        raise NotImplementedError

    def close(self) -> None:
        pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class UrllibRelayHTTPClient(RelayHTTPClient):
    """Direct HTTPS with no proxy inheritance, redirect handling, or cookie jar."""

    def __init__(self) -> None:
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
        max_body_bytes: int,
    ) -> RelayHTTPResponse:
        try:
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.fragment
            ):
                raise ValueError
        except ValueError:
            raise RelayError("HTTPS destination is invalid") from None
        started = time.monotonic()
        request = Request(  # noqa: S310 - destination is restricted to HTTPS above
            url, data=body, headers=dict(headers), method=method
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                result = RelayHTTPResponse(response.status, response.read(max_body_bytes + 1))
        except HTTPError as response:
            result = RelayHTTPResponse(response.code, response.read(max_body_bytes + 1))
        except (URLError, TimeoutError, OSError):
            raise _RelayTransportFailure("HTTPS request failed") from None
        if len(result.body) > max_body_bytes or time.monotonic() - started > timeout_seconds:
            raise RelayError("relay response exceeded its limit")
        return result


class ActionsOIDCTokenProvider:
    """Exchange the built-in Actions OIDC capability against the fixed issuer."""

    def __init__(
        self,
        request_url: str,
        request_token: str,
        audience: str,
        *,
        transport: RelayHTTPClient | None = None,
    ) -> None:
        try:
            parts = urlsplit(request_url)
            query = parse_qs(parts.query, keep_blank_values=True)
            if (
                f"{parts.scheme}://{parts.hostname}" != _ACTIONS_OIDC_ORIGIN
                or parts.port is not None
                or parts.username is not None
                or parts.password is not None
                or parts.fragment
                or not _ACTIONS_TOKEN_PATH.fullmatch(parts.path)
                or set(query) != {"api-version"}
                or len(query["api-version"]) != 1
                or re.fullmatch(r"[0-9.]{1,16}", query["api-version"][0]) is None
                or not isinstance(request_token, str)
                or not _TOKEN.fullmatch(request_token)
                or not isinstance(audience, str)
                or not 1 <= len(audience) <= 512
                or any(ord(character) < 32 for character in audience)
            ):
                raise ValueError
            self._request_url = request_url
            separator = "&" if parts.query else "?"
            self._token_url = request_url + separator + urlencode({"audience": audience})
            self._request_token = request_token
            self._audience = audience
            self._client = transport or UrllibRelayHTTPClient()
            self._owns_client = transport is None
        except Exception:
            raise RelayError("Actions OIDC configuration is invalid") from None

    def __repr__(self) -> str:
        return "ActionsOIDCTokenProvider([protected])"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __call__(self) -> str:
        try:
            response = self._client.request(
                "GET",
                self._token_url,
                headers={
                    "Authorization": f"Bearer {self._request_token}",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                body=None,
                timeout_seconds=5.0,
                max_body_bytes=_MAX_TOKEN_BODY_BYTES,
            )
            if len(response.body) > _MAX_TOKEN_BODY_BYTES or response.status_code != 200:
                raise RelayError("Actions OIDC token exchange failed")
            value = _json_body(response.body)
            token = value.get("value")
            if not isinstance(token, str) or not _JWT.fullmatch(token):
                raise RelayError("Actions OIDC token response was invalid")
            return token
        except RelayError:
            raise
        except Exception:
            raise RelayError("Actions OIDC token exchange failed") from None


class BrokerRunnerRelay:
    """POST only approval/operation IDs to fixed protected broker routes."""

    def __init__(
        self,
        *,
        origin: str,
        token_provider: Callable[[], str],
        transport: RelayHTTPClient | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._origin = _origin(origin)
        if not callable(token_provider) or not callable(sleeper):
            raise RelayError("relay configuration is invalid")
        self._token_provider = token_provider
        self._sleeper = sleeper
        self._client = transport or UrllibRelayHTTPClient()
        self._owns_client = transport is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def execute(self, action: str, approval_id: str, operation_id: str) -> dict[str, object]:
        if action not in {"deploy", "reconcile"}:
            raise RelayError("relay action is invalid")
        approval, operation = _canonical_uuid(approval_id), _canonical_uuid(operation_id)
        if action == "reconcile":
            return self._reconcile_bounded(approval, operation)
        try:
            return self._post("deploy", approval, operation)
        except _RelayTransportFailure:
            try:
                return self._reconcile_bounded(approval, operation)
            except _RelayNotFound:
                # A request may have failed before the broker durably received it.
                # Retrying the same ID is idempotent under the broker's binding.
                return self._post("deploy", approval, operation)

    def _reconcile_bounded(self, approval: str, operation: str) -> dict[str, object]:
        for attempt in range(_NOT_FOUND_RETRIES):
            try:
                return self._post("reconcile", approval, operation)
            except (_RelayNotFound, _RelayTransportFailure):
                if attempt + 1 == _NOT_FOUND_RETRIES:
                    raise _RelayNotFound("operation is not yet journaled") from None
                self._sleeper(0.2 * (attempt + 1))
        raise _RelayNotFound("operation is not yet journaled")

    def _post(self, action: str, approval: str, operation: str) -> dict[str, object]:
        token = self._token_provider()
        if not isinstance(token, str) or not _JWT.fullmatch(token):
            raise RelayError("Actions OIDC token is invalid")
        suffix = "host-deploy" if action == "deploy" else "host-reconcile"
        url = f"{self._origin}/v1/releases/{approval}/{suffix}"
        try:
            response = self._client.request(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                body=json.dumps(
                    {"operation_id": operation}, separators=(",", ":"), ensure_ascii=True
                ).encode("ascii"),
                timeout_seconds=_RELAY_TIMEOUT_SECONDS,
                max_body_bytes=_MAX_BODY_BYTES,
            )
            if len(response.body) > _MAX_BODY_BYTES:
                raise RelayError("broker response exceeded its limit")
            if response.status_code == 404:
                try:
                    error = _json_body(response.body)
                except (ValueError, UnicodeError):
                    raise RelayError("broker rejected the operation") from None
                if error == {"error": "not_found"} and action == "reconcile":
                    raise _RelayNotFound("operation is not journaled")
                raise RelayError("broker rejected the operation")
            if response.status_code != 200:
                raise RelayError("broker relay request failed")
            result = _json_body(response.body)
            if set(result) != {"operation_id", "phase", "receipt", "payload_digest"}:
                raise RelayError("broker receipt schema is invalid")
            phase = result["phase"]
            receipt = result["receipt"]
            if (
                result["operation_id"] != operation
                or phase not in _PHASES
                or not isinstance(result["payload_digest"], str)
                or not _SHA256.fullmatch(result["payload_digest"])
                or (
                    phase in _TERMINAL_PHASES
                    and (not isinstance(receipt, str) or not _RECEIPT.fullmatch(receipt))
                )
                or (phase not in _TERMINAL_PHASES and receipt is not None)
            ):
                raise RelayError("broker receipt binding is invalid")
            return {
                "operation_id": operation,
                "phase": phase,
                "receipt": receipt,
            }
        except (_RelayNotFound, RelayError):
            raise
        except Exception:
            raise _RelayTransportFailure("broker relay transport failed") from None
        finally:
            token = ""


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="chatcore-runner-relay")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("deploy", "reconcile"):
        command = commands.add_parser(action)
        command.add_argument("--approval-id", required=True)
        command.add_argument("--operation-id", required=True)
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    transport: RelayHTTPClient | None = None,
    output_writer: Callable[[str], None] = print,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    token_provider: ActionsOIDCTokenProvider | None = None
    relay: BrokerRunnerRelay | None = None
    try:
        args = _arguments(argv)
        source = os.environ if environment is None else environment
        origin = _origin(source.get("RELEASE_CONTROL_BROKER_ORIGIN"))
        audience = source.get("RELEASE_CONTROL_OIDC_AUDIENCE")
        if (
            not isinstance(audience, str)
            or not 1 <= len(audience) <= 512
            or any(ord(character) < 32 for character in audience)
        ):
            raise RelayError("fixed OIDC audience is unavailable")
        token_provider = ActionsOIDCTokenProvider(
            source.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""),
            source.get(_REQUEST_CAPABILITY_TOKEN_NAME, ""),
            audience,
            transport=transport,
        )
        relay = BrokerRunnerRelay(
            origin=origin,
            token_provider=token_provider,
            transport=transport,
            sleeper=sleeper,
        )
        receipt = relay.execute(args.action, args.approval_id, args.operation_id)
        safe = {
            "approval_id": _canonical_uuid(args.approval_id),
            "operation_id": receipt["operation_id"],
            "phase": receipt["phase"],
            "receipt": receipt["receipt"],
        }
        output_writer(json.dumps(safe, sort_keys=True, separators=(",", ":")))
        return 0 if receipt["phase"] in {"healthy", "restored_healthy"} else 1
    except SystemExit as error:
        return int(error.code or 0)
    except Exception:
        return 1
    finally:
        if relay is not None:
            relay.close()
        if token_provider is not None:
            token_provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
