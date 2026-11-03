from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from chatcore_google.browser_client import (
    BrowserClientError,
    GoogleBrowserClient,
)

TOKEN = "t" * 48
JOB_ID = "1234567890123456"
BASE_URL = "http://google-browser-editor:8080"


def _token_file(path, value=TOKEN, mode=0o400):
    path.write_text(value)
    path.chmod(mode)
    return path


def _job(**changes):
    value = {
        "job_id": JOB_ID,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
        "editor_email": "editor@example.invalid",
        "destination_file_id": "new-copy-id-123",
        "destination_tab_id": "t.tab-1",
        "expected_revision": "revision-1",
        "operation": {
            "kind": "replace_text",
            "find": "Dinner at 7pm",
            "replacement": "Dinner at 8pm",
        },
    }
    value.update(changes)
    return value


def _record(phase="accepted"):
    return {
        "job_id": JOB_ID,
        "phase": phase,
        "destination_file_id": "new-copy-id-123",
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "error_category": None,
    }


def test_client_sends_bearer_job_once_and_parses_only_bounded_status(tmp_path):
    calls = []
    request_job = _job()

    async def handle(request):
        calls.append(request)
        assert request.url == httpx.URL(f"{BASE_URL}/jobs")
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert json.loads(request.content) == request_job
        return httpx.Response(202, json=_record())

    async def exercise():
        client = GoogleBrowserClient(
            BASE_URL,
            _token_file(tmp_path / "token"),
            transport=httpx.MockTransport(handle),
        )
        try:
            result = await client.create_job(request_job)
            assert result.phase == "accepted"
            assert result.job_id == JOB_ID
            assert result.destination_file_id == "new-copy-id-123"
            assert len(calls) == 1
            assert client._client._trust_env is False
            assert client._client.follow_redirects is False
        finally:
            await client.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pass@google-browser-editor:8080",
        "http://google-browser-editor:8080/path",
        "http://google-browser-editor:8080?q=private",
        "http://google-browser-editor:8080#fragment",
        "https://docs.google.com",
        "file:///etc/passwd",
    ],
)
def test_client_rejects_non_fixed_service_urls(url, tmp_path):
    with pytest.raises(ValueError):
        GoogleBrowserClient(url, _token_file(tmp_path / "token"))


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_client_rejects_token_file_with_group_or_world_access(tmp_path, mode):
    token = _token_file(tmp_path / "token", mode=mode)
    with pytest.raises(ValueError, match="token file"):
        GoogleBrowserClient(BASE_URL, token)


def test_client_accepts_owner_only_mode_0600_token(tmp_path):
    client = GoogleBrowserClient(BASE_URL, _token_file(tmp_path / "token", mode=0o600))
    asyncio.run(client.aclose())


def test_client_rejects_symlink_token_file(tmp_path):
    target = _token_file(tmp_path / "real-token")
    link = tmp_path / "token"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="token file"):
        GoogleBrowserClient(BASE_URL, link)


def test_client_rejects_oversized_or_arbitrary_job_before_http_request(tmp_path):
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(202, json=_record())

    async def exercise():
        client = GoogleBrowserClient(
            BASE_URL,
            _token_file(tmp_path / "token"),
            transport=httpx.MockTransport(handle),
        )
        try:
            huge = _job(operation={"kind": "replace_text", "find": "x" * 20_000, "replacement": ""})
            with pytest.raises(ValueError, match="job request"):
                await client.create_job(huge)
            multiline = _job(operation={"kind": "replace_text", "find": "x\ny", "replacement": "z"})
            with pytest.raises(ValueError, match="job request"):
                await client.create_job(multiline)
            unknown = _job(extra="not allowed")
            with pytest.raises(ValueError, match="job request"):
                await client.create_job(unknown)
            assert not calls
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_client_does_not_follow_worker_redirects_or_echo_raw_response(tmp_path):
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(
            302,
            headers={"Location": "http://attacker.invalid/steal"},
            json={"detail": "private document body"},
        )

    async def exercise():
        client = GoogleBrowserClient(
            BASE_URL,
            _token_file(tmp_path / "token"),
            transport=httpx.MockTransport(handle),
        )
        try:
            with pytest.raises(BrowserClientError) as caught:
                await client.create_job(_job())
            assert caught.value.category == "unexpected_response"
            assert "private document body" not in str(caught.value)
            assert len(calls) == 1
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_client_rejects_oversized_worker_response(tmp_path):
    async def handle(_request):
        value = _record()
        value["padding"] = "x" * 5000
        return httpx.Response(200, json=value)

    async def exercise():
        client = GoogleBrowserClient(
            BASE_URL,
            _token_file(tmp_path / "token"),
            transport=httpx.MockTransport(handle),
        )
        try:
            with pytest.raises(BrowserClientError, match="invalid_worker_response"):
                await client.job(JOB_ID)
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_client_timeout_after_submit_is_uncertain_and_never_retries(tmp_path):
    calls = []

    async def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("private worker response details", request=request)

    async def exercise():
        client = GoogleBrowserClient(
            BASE_URL,
            _token_file(tmp_path / "token"),
            transport=httpx.MockTransport(handle),
        )
        try:
            with pytest.raises(BrowserClientError) as caught:
                await client.create_job(_job())
            assert caught.value.uncertain is True
            assert "private worker response details" not in str(caught.value)
            assert len(calls) == 1
        finally:
            await client.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["unexpected", "running", "awaiting_verification", "uncertain"])
def test_client_accepts_only_known_worker_phases(phase, tmp_path):
    async def handle(request):
        return httpx.Response(200, json=_record(phase))

    async def exercise():
        client = GoogleBrowserClient(
            BASE_URL,
            _token_file(tmp_path / "token"),
            transport=httpx.MockTransport(handle),
        )
        try:
            if phase == "unexpected":
                with pytest.raises(BrowserClientError):
                    await client.job(JOB_ID)
            else:
                assert (await client.job(JOB_ID)).phase == phase
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_client_status_cancel_verification_and_delete_use_fixed_endpoints(tmp_path):
    requests = []

    async def handle(request):
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={"job_id": JOB_ID, "deleted": True})
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            return httpx.Response(200, json=_record("failed_before_edit"))
        if request.method == "POST":
            return httpx.Response(200, json=_record("verified"))
        return httpx.Response(200, json=_record("awaiting_verification"))

    async def exercise():
        client = GoogleBrowserClient(
            BASE_URL,
            _token_file(tmp_path / "token"),
            transport=httpx.MockTransport(handle),
        )
        try:
            assert (await client.job(JOB_ID)).phase == "awaiting_verification"
            assert (await client.cancel(JOB_ID)).phase == "failed_before_edit"
            assert (await client.verify(JOB_ID, "verified")).phase == "verified"
            assert await client.delete(JOB_ID) is True
            assert [item.method for item in requests] == ["GET", "POST", "POST", "DELETE"]
            assert requests[1].url.path == f"/jobs/{JOB_ID}/cancel"
            assert requests[2].url.path == f"/jobs/{JOB_ID}/verification"
            assert json.loads(requests[2].content) == {"result": "verified"}
            assert requests[3].url.path == f"/jobs/{JOB_ID}"
        finally:
            await client.aclose()

    asyncio.run(exercise())
