from __future__ import annotations

import json
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from chatcore_release_control.runner_relay import (
    RelayHTTPClient,
    RelayHTTPResponse,
    main,
)

APPROVAL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OPERATION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
AUDIENCE = "https://broker.invalid/release"
OIDC_TOKEN = jwt.encode({"sub": "synthetic-test"}, secrets.token_bytes(32), algorithm="HS256")
REQUEST_TOKEN = "actions-request-token-value"
OIDC_URL = (
    "https://pipelines.actions.githubusercontent.com/"
    "00000000-0000-0000-0000-000000000001/_apis/pipelines/1/idtoken?api-version=2.0"
)


class HTTPXMockAdapter(RelayHTTPClient):
    """Adapt HTTPX MockTransport to the relay's stdlib-facing boundary."""

    def __init__(self, handler):
        self._client = httpx.Client(
            transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
        )

    def request(
        self,
        method,
        url,
        *,
        headers,
        body,
        timeout_seconds,
        max_body_bytes,
    ):
        response = self._client.request(
            method,
            url,
            headers=dict(headers),
            content=body,
            timeout=timeout_seconds,
        )
        return RelayHTTPResponse(response.status_code, response.content[: max_body_bytes + 1])

    def close(self):
        self._client.close()


def mock_transport(handler):
    return HTTPXMockAdapter(handler)


def environment() -> dict[str, str]:
    return {
        "RELEASE_CONTROL_BROKER_ORIGIN": "https://broker.invalid",
        "RELEASE_CONTROL_OIDC_AUDIENCE": AUDIENCE,
        "ACTIONS_ID_TOKEN_REQUEST_URL": OIDC_URL,
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": REQUEST_TOKEN,
    }


def terminal_response(
    *, operation_id: str = OPERATION, phase: str = "healthy"
) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "phase": phase,
        "receipt": "receipt_1234567890",
        "payload_digest": "d" * 64,
    }


def test_deploy_relay_exchanges_oidc_and_posts_only_operation_id():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "pipelines.actions.githubusercontent.com":
            assert request.method == "GET"
            assert parse_qs(urlsplit(str(request.url)).query)["audience"] == [AUDIENCE]
            assert request.headers["Authorization"] == f"Bearer {REQUEST_TOKEN}"
            return httpx.Response(200, json={"value": OIDC_TOKEN})
        assert request.url == f"https://broker.invalid/v1/releases/{APPROVAL}/host-deploy"
        assert request.headers["Authorization"] == f"Bearer {OIDC_TOKEN}"
        assert json.loads(request.content) == {"operation_id": OPERATION}
        return httpx.Response(200, json=terminal_response())

    output: list[str] = []
    result = main(
        ["deploy", "--approval-id", APPROVAL, "--operation-id", OPERATION],
        environment=environment(),
        transport=mock_transport(handler),
        output_writer=output.append,
    )

    assert result == 0
    assert json.loads(output[0]) == {
        "approval_id": APPROVAL,
        "operation_id": OPERATION,
        "phase": "healthy",
        "receipt": "receipt_1234567890",
    }
    assert len(requests) == 2
    assert REQUEST_TOKEN not in output[0] and OIDC_TOKEN not in output[0]


def test_reconcile_uses_fixed_route_and_retries_only_typed_not_found():
    requests: list[str] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.host == "pipelines.actions.githubusercontent.com":
            return httpx.Response(200, json={"value": OIDC_TOKEN})
        requests.append(str(request.url))
        attempts += 1
        if attempts == 1:
            return httpx.Response(404, json={"error": "not_found"})
        return httpx.Response(200, json=terminal_response(phase="restored_healthy"))

    output: list[str] = []
    result = main(
        ["reconcile", "--approval-id", APPROVAL, "--operation-id", OPERATION],
        environment=environment(),
        transport=mock_transport(handler),
        output_writer=output.append,
        sleeper=lambda _seconds: None,
    )

    assert result == 0
    assert requests == [
        f"https://broker.invalid/v1/releases/{APPROVAL}/host-reconcile",
        f"https://broker.invalid/v1/releases/{APPROVAL}/host-reconcile",
    ]
    assert json.loads(output[0])["phase"] == "restored_healthy"


def test_lost_deploy_reply_reconciles_same_operation_before_bounded_retry():
    requests: list[str] = []
    deploy_attempts = 0
    reconcile_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deploy_attempts, reconcile_attempts
        if request.url.host == "pipelines.actions.githubusercontent.com":
            return httpx.Response(200, json={"value": OIDC_TOKEN})
        requests.append(request.url.path)
        if request.url.path.endswith("/host-deploy"):
            deploy_attempts += 1
            if deploy_attempts == 1:
                raise httpx.ReadTimeout("hidden network detail")
            return httpx.Response(200, json=terminal_response())
        reconcile_attempts += 1
        return httpx.Response(404, json={"error": "not_found"})

    output: list[str] = []
    result = main(
        ["deploy", "--approval-id", APPROVAL, "--operation-id", OPERATION],
        environment=environment(),
        transport=mock_transport(handler),
        output_writer=output.append,
        sleeper=lambda _seconds: None,
    )

    assert result == 0
    assert deploy_attempts == 2
    assert reconcile_attempts == 3
    assert requests == [
        f"/v1/releases/{APPROVAL}/host-deploy",
        *[f"/v1/releases/{APPROVAL}/host-reconcile" for _ in range(3)],
        f"/v1/releases/{APPROVAL}/host-deploy",
    ]


@pytest.mark.parametrize(
    "response",
    [
        {**terminal_response(), "extra": "ignored is unsafe"},
        terminal_response(operation_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        {**terminal_response(), "receipt": "server text with spaces"},
        {**terminal_response(), "payload_digest": "not-a-digest"},
        {**terminal_response(), "receipt": None},
        {**terminal_response(phase="switching"), "receipt": "receipt_1234567890"},
    ],
)
def test_relay_rejects_unbound_or_untyped_broker_receipts(response):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pipelines.actions.githubusercontent.com":
            return httpx.Response(200, json={"value": OIDC_TOKEN})
        return httpx.Response(200, json=response)

    assert (
        main(
            ["deploy", "--approval-id", APPROVAL, "--operation-id", OPERATION],
            environment=environment(),
            transport=mock_transport(handler),
            output_writer=lambda _text: pytest.fail("invalid receipt must not be emitted"),
            sleeper=lambda _seconds: None,
        )
        == 1
    )


def test_relay_fails_closed_for_invalid_ids_or_missing_fixed_origin():
    assert (
        main(
            ["deploy", "--approval-id", "../host", "--operation-id", OPERATION],
            environment=environment(),
            output_writer=lambda _text: pytest.fail("invalid IDs must not be emitted"),
        )
        == 1
    )
    invalid = environment()
    invalid["RELEASE_CONTROL_BROKER_ORIGIN"] = "https://evil.invalid/path"
    assert (
        main(
            ["deploy", "--approval-id", APPROVAL, "--operation-id", OPERATION],
            environment=invalid,
            output_writer=lambda _text: pytest.fail("invalid origin must not be emitted"),
        )
        == 1
    )


def test_relay_cli_emits_only_allowlisted_ids_and_phase_from_receipt():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pipelines.actions.githubusercontent.com":
            return httpx.Response(200, json={"value": OIDC_TOKEN})
        return httpx.Response(200, json=terminal_response(phase="recovery_failed"))

    output: list[str] = []
    result = main(
        ["reconcile", "--approval-id", APPROVAL, "--operation-id", OPERATION],
        environment=environment(),
        transport=mock_transport(handler),
        output_writer=output.append,
        sleeper=lambda _seconds: None,
    )
    assert result == 1
    assert json.loads(output[0]) == {
        "approval_id": APPROVAL,
        "operation_id": OPERATION,
        "phase": "recovery_failed",
        "receipt": "receipt_1234567890",
    }
