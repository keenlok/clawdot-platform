import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from chatcore_google_browser_editor.engine import (
    BrowserEngine,
    chromium_args,
    clean_browser_environment,
    matches_destination_url,
)
from chatcore_google_browser_editor.models import CreateJobRequest
from chatcore_google_browser_editor.worker import CancelledAtBoundary


def test_document_url_must_match_exact_host_file_and_tab() -> None:
    good = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit?tab=t.0"
    assert matches_destination_url(good, "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "t.0")
    assert not matches_destination_url(
        good.replace("docs.google.com", "accounts.google.com"),
        "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "t.0",
    )
    assert not matches_destination_url(
        good.replace("t.0", "t.1"), "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "t.0"
    )
    assert not matches_destination_url(
        good.replace("1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "otherid"),
        "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "t.0",
    )


def test_chromium_flags_keep_sandbox_and_disable_direct_udp() -> None:
    args = chromium_args()
    assert "--disable-quic" in args
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert "--proxy-bypass-list=<-loopback>" in args
    assert "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1" in args
    assert not any("no-sandbox" in arg for arg in args)


def test_browser_environment_drops_proxy_and_injection_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/tmp/payload.js")
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid")
    env = clean_browser_environment(str(tmp_path))
    assert "HTTPS_PROXY" not in env and "HTTP_PROXY" not in env
    assert "NODE_OPTIONS" not in env
    assert env["HOME"] == str(tmp_path)


@pytest.mark.asyncio
async def test_cancellation_before_browser_launch_stops_without_reading_session(tmp_path) -> None:
    class Settings:
        editor_email = "editor@example.com"

        def read_storage_state(self):
            raise AssertionError("session state must not be loaded after cancellation")

    request = CreateJobRequest.model_validate(
        {
            "job_id": "a1b2c3d4e5f6478899aabbccddeeff00",
            "expires_at": datetime.now(UTC) + timedelta(seconds=30),
            "editor_email": "editor@example.com",
            "destination_file_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
            "destination_tab_id": "t.0",
            "expected_revision": "revision-1",
            "operation": {"kind": "replace_text", "find": "before", "replacement": "after"},
        }
    )
    event = asyncio.Event()
    event.set()
    engine = BrowserEngine(Settings())
    with pytest.raises(CancelledAtBoundary):
        await engine.run(request, request.operation, event, lambda: None)
