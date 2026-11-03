"""Fixed HTTPS Google REST endpoints, pinned public IPs and owner OAuth refresh.

No user-supplied token endpoint, redirects, environment proxy or automatic write retries.
Remote error bodies are deliberately never surfaced or logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from pathlib import Path

import httpx
from chatcore.security.net_guard import resolve_public

HOSTS = frozenset(
    {"oauth2.googleapis.com", "docs.googleapis.com", "sheets.googleapis.com", "www.googleapis.com"}
)


class GoogleError(ValueError):
    """A bounded, user-safe error without remote response text or credentials."""


class PublicGoogleTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.transport = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        host = request.url.host
        addresses = await resolve_public(host, 443)
        request.headers["host"] = host
        request.extensions["sni_hostname"] = host
        request.url = request.url.copy_with(host=addresses[0])
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        await self.transport.aclose()


class GoogleHttp:
    def __init__(self, *, transport=None, max_bytes=2_000_000, timeout_s=15):
        self.max_bytes, self.timeout_s = max_bytes, timeout_s
        self.client = httpx.AsyncClient(
            transport=transport or PublicGoogleTransport(),
            timeout=timeout_s,
            trust_env=False,
            follow_redirects=False,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        await self.client.aclose()

    async def request(self, method, url, **kwargs):
        raw = await self.request_bytes(method, url, **kwargs)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise GoogleError("Google returned an invalid response.")
            return value
        except (ValueError, UnicodeError):
            raise GoogleError("Google returned an invalid response.") from None

    async def request_bytes(self, method, url, *, download=False, **kwargs):
        parsed = httpx.URL(url)
        if (
            parsed.scheme != "https"
            or parsed.host not in HOSTS
            or parsed.port not in (None, 443)
            or parsed.userinfo
            or method not in {"GET", "POST"}
        ):
            raise GoogleError("Google requests require a fixed Google HTTPS endpoint.")
        try:
            async with asyncio.timeout(self.timeout_s):
                async with self.client.stream(method, parsed, **kwargs) as response:
                    if not 200 <= response.status_code < 300:
                        if parsed.host == "oauth2.googleapis.com" or response.status_code == 401:
                            raise GoogleError(
                                "Google authorization failed; ask the owner to reauthorize."
                            )
                        if response.status_code == 429:
                            raise GoogleError(
                                "Google's rate limit was reached; wait before requesting again."
                            )
                        if response.status_code == 403 and download:
                            raise GoogleError(
                                "Cannot download that file. Check sharing, download restrictions "
                                "and API quotas; the owner may need to reauthorize read-only "
                                "Google Drive content access."
                            )
                        if response.status_code in (403, 404):
                            raise GoogleError(
                                "Cannot open that file. Check sharing with the configured "
                                "owner account (Viewer for reads, Editor for writes) "
                                "and API quotas."
                            )
                        raise GoogleError(
                            f"Google returned HTTP {response.status_code}; no retry was made."
                        )
                    length = response.headers.get("content-length")
                    if length is not None and int(length) > self.max_bytes:
                        raise GoogleError("Google response exceeds the size limit.")
                    chunks = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise GoogleError(
                                "Google response exceeds the size limit; select a smaller range."
                            )
                        chunks.append(chunk)
            return b"".join(chunks)
        except GoogleError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, OSError):
            raise GoogleError(
                "Google request failed; a write may have completed. Check the file "
                "before making another request."
            ) from None


def load_profile(path: Path, owner_email: str) -> tuple[dict, str]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise GoogleError("Google credential file must be a private mode-600 regular file.")
        if path.stat().st_size > 64_000:
            raise GoogleError("Google credential file is invalid.")
        raw = path.read_bytes()
        profile = json.loads(raw)
        for name in ("client_id", "client_secret", "refresh_token", "owner_email", "permission_id"):
            if (
                not isinstance(profile.get(name), str)
                or not profile[name]
                or len(profile[name]) > 30_000
            ):
                raise GoogleError(
                    "Google credential profile is incomplete; ask the owner to reauthorize."
                )
        if profile["owner_email"].casefold() != owner_email.casefold():
            raise GoogleError("Google owner account changed; reauthorize before continuing.")
        return profile, hashlib.sha256(raw).hexdigest()
    except GoogleError:
        raise
    except (OSError, ValueError, AttributeError):
        raise GoogleError(
            "Google credentials are unavailable; ask the owner to reauthorize."
        ) from None


class GoogleClient:
    def __init__(self, http: GoogleHttp, profile_path: Path | str, owner_email: str):
        self.http, self.profile_path, self.owner_email = http, Path(profile_path), owner_email
        self._token = ""
        self._expires = 0.0
        self._fingerprint = ""
        self._lock = asyncio.Lock()

    async def token(self):
        async with self._lock:
            profile, fingerprint = load_profile(self.profile_path, self.owner_email)
            if (
                not self._token
                or time.monotonic() >= self._expires
                or fingerprint != self._fingerprint
            ):
                value = await self.http.request(
                    "POST",
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": profile["client_id"],
                        "client_secret": profile["client_secret"],
                        "refresh_token": profile["refresh_token"],
                        "grant_type": "refresh_token",
                    },
                )
                token, expiry = value.get("access_token"), value.get("expires_in", 3600)
                if not isinstance(token, str) or not token or type(expiry) not in (float, int):
                    raise GoogleError("Google returned an invalid authorization response.")
                self._token, self._fingerprint = token, fingerprint
                self._expires = time.monotonic() + max(0, min(expiry, 3600) - 60)
            return self._token

    async def request(self, method, host, path, **kwargs):
        return await self.http.request(
            method,
            f"https://{host}{path}",
            headers={"Authorization": "Bearer " + await self.token()},
            **kwargs,
        )

    async def identity(self):
        profile, _ = load_profile(self.profile_path, self.owner_email)
        value = await self.request(
            "GET",
            "www.googleapis.com",
            "/drive/v3/about",
            params={"fields": "user(emailAddress,permissionId)"},
        )
        user = value.get("user", {})
        if (
            str(user.get("emailAddress", "")).casefold() != self.owner_email.casefold()
            or user.get("permissionId") != profile["permission_id"]
        ):
            raise GoogleError("Google owner account changed; reauthorize before continuing.")
        return profile["permission_id"]

    async def metadata(self, file_id):
        return await self.request(
            "GET",
            "www.googleapis.com",
            f"/drive/v3/files/{file_id}",
            params={
                "fields": "id,name,mimeType,size,modifiedTime,trashed,version,headRevisionId,"
                "isAppAuthorized,copyRequiresWriterPermission,contentRestrictions,"
                "capabilities(canEdit,canModifyContent,canCopy,canDownload)",
                "supportsAllDrives": "true",
            },
        )

    async def media(self, file_id):
        return await self.http.request_bytes(
            "GET",
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            headers={"Authorization": "Bearer " + await self.token()},
            params={"alt": "media", "supportsAllDrives": "true"},
            download=True,
        )

    async def document(self, file_id):
        return await self.request(
            "GET",
            "docs.googleapis.com",
            f"/v1/documents/{file_id}",
            params={"includeTabsContent": "true"},
        )

    async def document_for_edit(self, file_id):
        """Read exact Docs indices with suggestions inline, as required by batchUpdate."""
        return await self.request(
            "GET",
            "docs.googleapis.com",
            f"/v1/documents/{file_id}",
            params={"includeTabsContent": "true", "suggestionsViewMode": "SUGGESTIONS_INLINE"},
        )

    async def edit_document(self, file_id, requests, required_revision_id):
        if not isinstance(required_revision_id, str) or not required_revision_id:
            raise GoogleError("A Docs revision precondition is required for edits.")
        return await self.request(
            "POST",
            "docs.googleapis.com",
            f"/v1/documents/{file_id}:batchUpdate",
            json={
                "writeControl": {"requiredRevisionId": required_revision_id},
                "requests": requests,
            },
        )

    async def sheet(self, file_id):
        return await self.request(
            "GET",
            "sheets.googleapis.com",
            f"/v4/spreadsheets/{file_id}",
            params={"fields": "spreadsheetId,properties(title),sheets(properties)"},
        )

    async def grid_for_edit(self, file_id, sheet_id, cell_range):
        if not isinstance(sheet_id, int) or isinstance(sheet_id, bool):
            raise GoogleError("A stable numeric sheet ID is required for edits.")
        return await self.request(
            "GET",
            "sheets.googleapis.com",
            f"/v4/spreadsheets/{file_id}",
            params={
                "includeGridData": "true",
                "ranges": cell_range,
                "fields": "spreadsheetId,properties(title),namedRanges(namedRangeId,range),"
                "sheets(properties(sheetId,title,gridProperties),merges,protectedRanges,"
                "conditionalFormats,"
                "data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue,"
                "userEnteredFormat,effectiveFormat,dataValidation,note)),rowMetadata,columnMetadata))",
            },
        )

    async def edit_cells(self, file_id, body):
        if not isinstance(body, dict) or not isinstance(body.get("requests"), list):
            raise GoogleError("A bounded Sheets update request is required.")
        return await self.request(
            "POST", "sheets.googleapis.com", f"/v4/spreadsheets/{file_id}:batchUpdate", json=body
        )

    async def copy_file(self, file_id, title):
        """Fixed Drive copy call; callers must first pass confirmed destination policy."""
        from urllib.parse import quote

        return await self.request(
            "POST",
            "www.googleapis.com",
            f"/drive/v3/files/{quote(file_id, safe='')}/copy",
            params={
                "supportsAllDrives": "true",
                "ignoreDefaultVisibility": "true",
                "fields": "id,name,mimeType,webViewLink",
            },
            json={"name": title, "parents": ["root"]},
        )

    async def grant_writer(self, file_id, email):
        """Fixed per-user writer grant for a confirmed new browser-trial copy."""
        from urllib.parse import quote

        if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", file_id):
            raise GoogleError("A valid Google destination ID is required for sharing.")
        if not isinstance(email, str) or not re.fullmatch(
            r"[^\s@]{1,64}@[^\s@.]+(?:\.[^\s@.]+)+", email
        ):
            raise GoogleError("A valid individual email address is required for sharing.")
        return await self.request(
            "POST",
            "www.googleapis.com",
            f"/drive/v3/files/{quote(file_id, safe='')}/permissions",
            params={
                "supportsAllDrives": "true",
                "sendNotificationEmail": "false",
                "fields": "id,type,role,emailAddress",
            },
            json={"type": "user", "role": "writer", "emailAddress": email},
        )

    async def list_permissions(self, file_id):
        """Read per-user role state for an action-created destination."""
        from urllib.parse import quote

        if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", file_id):
            raise GoogleError("A valid Google destination ID is required to read sharing.")
        return await self.request(
            "GET",
            "www.googleapis.com",
            f"/drive/v3/files/{quote(file_id, safe='')}/permissions",
            params={
                "supportsAllDrives": "true",
                "pageSize": "100",
                "fields": "nextPageToken,permissions(id,type,role,emailAddress)",
            },
        )

    async def import_docx(self, file_id, title, source_bytes):
        """Upload DOCX bytes as a new native Google Doc; never updates the DOCX source."""
        if not isinstance(source_bytes, bytes) or not source_bytes:
            raise GoogleError("DOCX content is required for native Doc conversion.")
        boundary = "clawdot-google-docx-" + secrets.token_hex(16)
        metadata = json.dumps(
            {"name": title, "mimeType": "application/vnd.google-apps.document"},
            separators=(",", ":"),
        ).encode("utf-8")
        while boundary.encode() in source_bytes or boundary.encode() in metadata:
            boundary = "clawdot-google-docx-" + secrets.token_hex(16)
        body = (
            b"--" + boundary.encode() + b"\r\n"
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            + metadata
            + b"\r\n--"
            + boundary.encode()
            + b"\r\n"
            b"Content-Type: application/"
            b"vnd.openxmlformats-officedocument.wordprocessingml.document\r\n"
            b"Content-Transfer-Encoding: binary\r\n\r\n"
            + source_bytes
            + b"\r\n--"
            + boundary.encode()
            + b"--\r\n"
        )
        raw = await self.http.request_bytes(
            "POST",
            "https://www.googleapis.com/upload/drive/v3/files",
            params={
                "uploadType": "multipart",
                "supportsAllDrives": "true",
                "ignoreDefaultVisibility": "true",
                "fields": "id,name,mimeType,webViewLink",
            },
            headers={
                "Authorization": "Bearer " + await self.token(),
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            content=body,
        )
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise GoogleError(
                "Google returned an invalid native Doc conversion response."
            ) from None
        if not isinstance(value, dict):
            raise GoogleError("Google returned an invalid native Doc conversion response.")
        return value

    async def values(self, file_id, cell_range):
        from urllib.parse import quote

        return await self.request(
            "GET",
            "sheets.googleapis.com",
            f"/v4/spreadsheets/{file_id}/values/{quote(cell_range, safe='')}",
            params={"valueRenderOption": "FORMATTED_VALUE"},
        )

    async def batch(self, file_id, requests):
        return await self.request(
            "POST",
            "sheets.googleapis.com",
            f"/v4/spreadsheets/{file_id}:batchUpdate",
            json={"requests": requests},
        )
