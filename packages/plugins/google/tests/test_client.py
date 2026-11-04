import json

import httpx
import pytest
from chatcore_google.client import GoogleClient, GoogleError, GoogleHttp


def credentials(tmp_path):
    path = tmp_path / "google.json"
    path.write_text(
        json.dumps(
            {
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "refresh-secret",
                "owner_email": "owner@example.com",
                "permission_id": "owner-id",
            }
        )
    )
    path.chmod(0o600)
    return path


async def test_tokens_are_cached_and_owner_is_verified(tmp_path):
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            assert b"refresh-secret" in request.content
            return httpx.Response(200, json={"access_token": "access-secret", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer access-secret"
        return httpx.Response(
            200, json={"user": {"emailAddress": "owner@example.com", "permissionId": "owner-id"}}
        )

    async with GoogleHttp(transport=httpx.MockTransport(transport)) as http:
        client = GoogleClient(http, credentials(tmp_path), "owner@example.com")
        assert await client.identity() == "owner-id"
        assert await client.identity() == "owner-id"
        assert sum(r.url.host == "oauth2.googleapis.com" for r in calls) == 1


async def test_edit_reads_and_fixed_write_endpoints_include_revision_and_literal_cell_types(
    tmp_path,
):
    calls = []

    def endpoint(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        if request.method == "GET" and "/files/file" in request.url.path:
            return httpx.Response(200, json={"id": "file", "version": "3"})
        return httpx.Response(200, json={"ok": True})

    async with GoogleHttp(transport=httpx.MockTransport(endpoint)) as http:
        client = GoogleClient(http, credentials(tmp_path), "owner@example.com")
        await client.metadata("file")
        await client.document_for_edit("file")
        await client.grid_for_edit("file", 42, "A1:B1")
        await client.edit_document(
            "file", [{"insertText": {"location": {"index": 3}, "text": "=x"}}], "rev-1"
        )
        await client.edit_cells(
            "file",
            {
                "requests": [
                    {
                        "updateCells": {
                            "range": {"sheetId": 42},
                            "rows": [{"values": [{"userEnteredValue": {"stringValue": "-2 pax"}}]}],
                            "fields": "userEnteredValue",
                        }
                    }
                ]
            },
        )

    metadata = next(r for r in calls if r.url.path.endswith("/files/file"))
    assert "version" in metadata.url.params["fields"] and "trashed" in metadata.url.params["fields"]
    assert "canEdit" in metadata.url.params["fields"]
    doc_read = next(r for r in calls if r.url.host == "docs.googleapis.com")
    assert doc_read.url.params["suggestionsViewMode"] == "SUGGESTIONS_INLINE"
    doc_write = next(
        r
        for r in calls
        if r.url.path.endswith(":batchUpdate") and r.url.host == "docs.googleapis.com"
    )
    assert b'"requiredRevisionId":"rev-1"' in doc_write.content
    sheet_read = next(
        r for r in calls if r.url.host == "sheets.googleapis.com" and r.method == "GET"
    )
    assert "userEnteredValue" in sheet_read.url.params["fields"]
    assert "columnMetadata" in sheet_read.url.params["fields"]
    field_mask = sheet_read.url.params["fields"]
    depth = 0
    for char in field_mask:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            assert depth >= 0
    assert depth == 0 and "rowData(values(" in field_mask
    sheet_write = next(
        r for r in calls if r.url.host == "sheets.googleapis.com" and r.method == "POST"
    )
    assert b'"stringValue":"-2 pax"' in sheet_write.content


async def test_copy_and_docx_import_are_fixed_native_creation_requests(tmp_path):
    calls = []

    def endpoint(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        return httpx.Response(
            200, json={"id": "new-id", "mimeType": "application/vnd.google-apps.document"}
        )

    async with GoogleHttp(transport=httpx.MockTransport(endpoint)) as http:
        client = GoogleClient(http, credentials(tmp_path), "owner@example.com")
        copied = await client.copy_file("source", "Edited copy")
        imported = await client.import_docx("source", "Native Doc", b"PK bytes")
    assert copied["id"] == imported["id"] == "new-id"
    copy = next(r for r in calls if r.url.path.endswith("/files/source/copy"))
    assert copy.method == "POST" and b'"name":"Edited copy"' in copy.content
    assert b'"parents":["root"]' in copy.content
    assert copy.url.params["ignoreDefaultVisibility"] == "true"
    upload = next(r for r in calls if r.url.path == "/upload/drive/v3/files")
    assert upload.url.params["uploadType"] == "multipart"
    assert upload.url.params["ignoreDefaultVisibility"] == "true"
    assert upload.headers["content-type"].startswith("multipart/related; boundary=")
    assert b"application/json; charset=UTF-8" in upload.content
    assert b"application/vnd.google-apps.document" in upload.content
    assert b"PK bytes" in upload.content


async def test_browser_trial_permissions_are_fixed_to_individual_writer_grants(tmp_path):
    calls = []

    def endpoint(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        if request.method == "GET":
            return httpx.Response(200, json={"permissions": []})
        return httpx.Response(200, json={"id": "perm", "type": "user", "role": "writer"})

    async with GoogleHttp(transport=httpx.MockTransport(endpoint)) as http:
        client = GoogleClient(http, credentials(tmp_path), "owner@example.com")
        await client.grant_writer("new-copy", "editor@example.com")
        await client.list_permissions("new-copy")
        with pytest.raises(GoogleError, match="individual email"):
            await client.grant_writer("new-copy", "anyone")

    grant = next(
        request
        for request in calls
        if request.method == "POST" and request.url.host == "www.googleapis.com"
    )
    assert grant.url.path.endswith("/files/new-copy/permissions")
    assert grant.url.params["sendNotificationEmail"] == "false"
    assert json.loads(grant.content) == {
        "type": "user",
        "role": "writer",
        "emailAddress": "editor@example.com",
    }
    read = next(request for request in calls if request.method == "GET")
    assert "allowFileDiscovery" in read.url.params["fields"]


async def test_link_viewer_grant_is_fixed_and_readback_includes_discovery_flag(tmp_path):
    calls = []

    def endpoint(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        if request.method == "GET":
            return httpx.Response(200, json={"permissions": []})
        return httpx.Response(
            200,
            json={
                "id": "permission-id",
                "type": "anyone",
                "role": "reader",
                "allowFileDiscovery": False,
            },
        )

    async with GoogleHttp(transport=httpx.MockTransport(endpoint)) as http:
        client = GoogleClient(http, credentials(tmp_path), "owner@example.com")
        await client.grant_link_viewer("new-doc")
        await client.list_permissions("new-doc")

    grant = next(
        request
        for request in calls
        if request.method == "POST" and request.url.host == "www.googleapis.com"
    )
    assert grant.url.path.endswith("/files/new-doc/permissions")
    assert json.loads(grant.content) == {
        "type": "anyone",
        "role": "reader",
        "allowFileDiscovery": False,
    }
    read = next(request for request in calls if request.method == "GET")
    assert "allowFileDiscovery" in read.url.params["fields"]


async def test_account_change_and_revoked_credentials_fail_closed(tmp_path):
    def transport(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access-secret", "expires_in": 3600})
        return httpx.Response(
            200, json={"user": {"emailAddress": "other@example.com", "permissionId": "other-id"}}
        )

    async with GoogleHttp(transport=httpx.MockTransport(transport)) as http:
        with pytest.raises(GoogleError, match="owner"):
            await GoogleClient(http, credentials(tmp_path), "owner@example.com").identity()
    async with GoogleHttp(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                400, json={"error": "invalid_grant", "detail": "refresh-secret"}
            )
        )
    ) as http:
        with pytest.raises(GoogleError, match="reauthorize") as error:
            await GoogleClient(http, credentials(tmp_path), "owner@example.com").identity()
        assert "secret" not in str(error.value)


async def test_private_profile_permissions_and_fixed_https_hosts(tmp_path):
    path = credentials(tmp_path)
    path.chmod(0o644)
    async with GoogleHttp(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as http:
        with pytest.raises(GoogleError, match="600"):
            await GoogleClient(http, path, "owner@example.com").identity()
        for url in [
            "http://docs.googleapis.com/v1/x",
            "https://evil.test/x",
            "https://docs.googleapis.com:444/x",
            "https://x@docs.googleapis.com/v1/x",
        ]:
            with pytest.raises(GoogleError):
                await http.request("GET", url)


async def test_redirects_oversized_errors_and_titles_never_leak_secrets():
    for response in [
        httpx.Response(302, headers={"Location": "https://evil.test"}),
        httpx.Response(403, json={"secret": "token-secret"}),
    ]:
        async with GoogleHttp(
            transport=httpx.MockTransport(lambda r, value=response: value)
        ) as http:
            with pytest.raises(GoogleError) as error:
                await http.request("GET", "https://www.googleapis.com/drive/v3/files/abc")
            assert "secret" not in str(error.value) and "evil" not in str(error.value)
    async with GoogleHttp(
        max_bytes=8,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"123456789")),
    ) as http:
        with pytest.raises(GoogleError, match="size"):
            await http.request("GET", "https://docs.googleapis.com/v1/documents/a")


async def test_revoked_cached_authorization_reports_reauthorization():
    async with GoogleHttp(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"secret": "do-not-show"}))
    ) as http:
        with pytest.raises(GoogleError, match="reauthorize"):
            await http.request("GET", "https://docs.googleapis.com/v1/documents/a")


async def test_docx_media_uses_fixed_authenticated_drive_download(tmp_path):
    calls = []

    def endpoint(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        assert request.url.host == "www.googleapis.com"
        assert request.url.path == "/drive/v3/files/file"
        assert request.url.params["alt"] == "media"
        assert request.url.params["supportsAllDrives"] == "true"
        assert request.headers["authorization"] == "Bearer access"
        return httpx.Response(200, content=b"PK-binary")

    async with GoogleHttp(transport=httpx.MockTransport(endpoint)) as http:
        client = GoogleClient(http, credentials(tmp_path), "owner@example.com")
        assert await client.media("file") == b"PK-binary"
    assert len(calls) == 2


@pytest.mark.parametrize("status", [302, 401, 403, 404, 429])
async def test_media_errors_are_sanitized_and_403_explains_scope(status):
    async with GoogleHttp(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                status, content=b"private-secret", headers={"location": "https://evil.test"}
            )
        )
    ) as http:
        with pytest.raises(GoogleError) as error:
            await http.request_bytes(
                "GET", "https://www.googleapis.com/drive/v3/files/a", download=True
            )
        assert "private-secret" not in str(error.value) and "evil.test" not in str(error.value)
        if status == 403:
            assert "reauthorize" in str(error.value) and "sharing" in str(error.value)


async def test_media_enforces_actual_stream_size_even_without_content_length():
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"12345"
            yield b"67890"

    async with GoogleHttp(
        max_bytes=8, transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Chunks()))
    ) as http:
        with pytest.raises(GoogleError, match="size"):
            await http.request_bytes("GET", "https://www.googleapis.com/drive/v3/files/a")


async def test_media_rejects_advertised_oversize_and_unsafe_hosts():
    async with GoogleHttp(
        max_bytes=8,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"a", headers={"content-length": "999"})
        ),
    ) as http:
        with pytest.raises(GoogleError, match="size"):
            await http.request_bytes("GET", "https://www.googleapis.com/drive/v3/files/a")
        with pytest.raises(GoogleError, match="fixed"):
            await http.request_bytes("GET", "https://evil.test/file")


async def test_google_transport_pins_resolved_public_ip_and_tls_hostname(monkeypatch):
    from unittest.mock import AsyncMock

    from chatcore_google.client import PublicGoogleTransport

    transport = PublicGoogleTransport()
    await transport.transport.aclose()
    captured = []

    def endpoint(request):
        captured.append(request)
        return httpx.Response(200, json={})

    transport.transport = httpx.MockTransport(endpoint)
    monkeypatch.setattr(
        "chatcore.security.net_guard._resolve", AsyncMock(return_value=["93.184.216.34"])
    )
    async with GoogleHttp(transport=transport) as http:
        await http.request("GET", "https://docs.googleapis.com/v1/documents/a")
        assert captured[0].url.host == "93.184.216.34"
        assert captured[0].headers["host"] == "docs.googleapis.com"
        assert captured[0].extensions["sni_hostname"] == "docs.googleapis.com"
    monkeypatch.setattr(
        "chatcore.security.net_guard._resolve", AsyncMock(return_value=["127.0.0.1"])
    )
    async with GoogleHttp() as http:
        with pytest.raises(GoogleError):
            await http.request("GET", "https://docs.googleapis.com/v1/documents/a")
