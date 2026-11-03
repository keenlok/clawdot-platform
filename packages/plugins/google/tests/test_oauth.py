import json

import httpx
import pytest
from chatcore_google.client import GoogleError, GoogleHttp, load_profile
from chatcore_google.oauth import (
    authorization_request,
    exchange_code,
    save_profile,
    validate_callback,
)


def test_oauth_has_state_pkce_offline_and_rejects_wrong_callback():
    from urllib.parse import parse_qs, urlsplit

    url, state, verifier = authorization_request("client", "http://127.0.0.1:1234/callback")
    query = parse_qs(urlsplit(url).query)
    assert "https://www.googleapis.com/auth/drive.readonly" in query["scope"][0].split()
    assert query["state"] == [state] and query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"] and len(verifier) >= 43
    assert validate_callback("/callback?state=" + state + "&code=abc", state) == "abc"
    for value in [
        "/callback?state=bad&code=abc",
        "/other?state=" + state + "&code=abc",
        "/callback?state=" + state + "&error=denied",
    ]:
        with pytest.raises(ValueError):
            validate_callback(value, state)


async def test_setup_verifies_owner_before_saving_and_writes_private_profile(tmp_path):
    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            assert b"code_verifier=" in request.content
            return httpx.Response(200, json={"access_token": "access", "refresh_token": "refresh"})
        return httpx.Response(
            200, json={"user": {"emailAddress": "owner@example.com", "permissionId": "owner-id"}}
        )

    async with GoogleHttp(transport=httpx.MockTransport(handle)) as http:
        profile = await exchange_code(
            http,
            {"client_id": "client", "client_secret": "secret"},
            "code",
            "verifier",
            "http://127.0.0.1:1/callback",
            "owner@example.com",
        )
        with pytest.raises(GoogleError, match="owner"):
            await exchange_code(
                http,
                {"client_id": "client", "client_secret": "secret"},
                "code",
                "verifier",
                "http://127.0.0.1:1/callback",
                "other@example.com",
            )
    path = tmp_path / "owner.json"
    save_profile(path, profile)
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_profile(path, "owner@example.com")[0]["permission_id"] == "owner-id"
    assert json.loads(path.read_text())["refresh_token"] == "refresh"
