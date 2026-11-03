"""Fixed, authenticated client for the isolated Google browser worker."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import httpx

MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 4096
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_FILE_ID = re.compile(r"[A-Za-z0-9_-]{10,128}\Z")
_TAB_ID = re.compile(r"(?:t\.[A-Za-z0-9_-]{1,80}|[0-9]{1,20})\Z")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_PHASES = frozenset(
    {
        "accepted",
        "running",
        "awaiting_verification",
        "verified",
        "needs_login",
        "blocked",
        "failed_before_edit",
        "uncertain",
    }
)


class BrowserClientError(RuntimeError):
    """Bounded worker failure; never includes response bodies or transport text."""

    def __init__(self, category: str, *, uncertain: bool = False):
        super().__init__(category)
        self.category = category if _SAFE_CODE.fullmatch(category) else "worker_error"
        self.uncertain = uncertain


@dataclass(frozen=True)
class BrowserJob:
    job_id: str
    phase: str
    destination_file_id: str | None
    created_at: datetime
    updated_at: datetime
    error_category: str | None = None


def _read_secret(path: str | Path) -> str:
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        ):
            raise ValueError
        with os.fdopen(fd, encoding="ascii") as handle:
            fd = None
            value = handle.read(129).strip()
        if not re.fullmatch(r"[A-Za-z0-9._~-]{32,128}", value):
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError):
        raise ValueError("invalid browser worker token file") from None
    finally:
        if fd is not None:
            os.close(fd)


def _validate_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ValueError("browser worker URL is invalid")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("browser worker URL is invalid") from None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("browser worker URL is invalid")
    try:
        private_host = ip_address(parts.hostname).is_private
    except ValueError:
        private_host = (
            "." not in parts.hostname
            or parts.hostname == "localhost"
            or parts.hostname.endswith((".localhost", ".internal"))
        )
    if not private_host:
        raise ValueError("browser worker URL must use a private service address")
    return value.rstrip("/")


def _aware_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _validate_job_request(value: object) -> bytes:
    required = {
        "job_id",
        "expires_at",
        "editor_email",
        "destination_file_id",
        "destination_tab_id",
        "expected_revision",
        "operation",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid browser job request")
    if not isinstance(value["job_id"], str) or not _JOB_ID.fullmatch(value["job_id"]):
        raise ValueError("invalid browser job request")
    if not isinstance(value["destination_file_id"], str) or not _FILE_ID.fullmatch(
        value["destination_file_id"]
    ):
        raise ValueError("invalid browser job request")
    if not isinstance(value["destination_tab_id"], str) or not _TAB_ID.fullmatch(
        value["destination_tab_id"]
    ):
        raise ValueError("invalid browser job request")
    email = value["editor_email"]
    if not isinstance(email, str) or len(email) > 254 or not _EMAIL.fullmatch(email):
        raise ValueError("invalid browser job request")
    revision = value["expected_revision"]
    if (
        not isinstance(revision, str)
        or not revision
        or len(revision) > 256
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in revision)
    ):
        raise ValueError("invalid browser job request")
    try:
        expiry = _aware_timestamp(value["expires_at"])
    except (TypeError, ValueError):
        raise ValueError("invalid browser job request") from None
    now = datetime.now(UTC)
    if expiry <= now or expiry > now + timedelta(seconds=90):
        raise ValueError("invalid browser job request")
    operation = value["operation"]
    if not isinstance(operation, dict):
        raise ValueError("invalid browser job request")
    kind = operation.get("kind")
    if kind == "replace_text":
        if set(operation) != {"kind", "find", "replacement"}:
            raise ValueError("invalid browser job request")
        if (
            not isinstance(operation["find"], str)
            or not operation["find"]
            or len(operation["find"]) > 2000
            or "\n" in operation["find"]
            or "\r" in operation["find"]
            or not isinstance(operation["replacement"], str)
            or len(operation["replacement"]) > 2000
            or "\n" in operation["replacement"]
            or "\r" in operation["replacement"]
        ):
            raise ValueError("invalid browser job request")
    elif kind == "bold_text":
        if set(operation) != {"kind", "text", "bold"}:
            raise ValueError("invalid browser job request")
        if (
            not isinstance(operation["text"], str)
            or not operation["text"]
            or len(operation["text"]) > 2000
            or "\n" in operation["text"]
            or "\r" in operation["text"]
            or type(operation["bold"]) is not bool
        ):
            raise ValueError("invalid browser job request")
    else:
        raise ValueError("invalid browser job request")
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("browser job request exceeds its size limit")
    return body


def _parse_record(value: object, job_id: str) -> BrowserJob:
    required = {
        "job_id",
        "phase",
        "destination_file_id",
        "created_at",
        "updated_at",
        "error_category",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("job_id") != job_id:
        raise ValueError
    phase = value.get("phase")
    if phase not in _PHASES:
        raise ValueError
    destination = value.get("destination_file_id")
    if destination is not None and (
        not isinstance(destination, str) or not _FILE_ID.fullmatch(destination)
    ):
        raise ValueError
    category = value.get("error_category")
    if category is not None and (
        not isinstance(category, str) or not _SAFE_CODE.fullmatch(category)
    ):
        raise ValueError
    return BrowserJob(
        job_id=job_id,
        phase=phase,
        destination_file_id=destination,
        created_at=_aware_timestamp(value["created_at"]),
        updated_at=_aware_timestamp(value["updated_at"]),
        error_category=category,
    )


class GoogleBrowserClient:
    """Calls only the configured worker's fixed job endpoints; POSTs are never retried."""

    def __init__(self, base_url: str, token_file: str | Path, *, transport=None, timeout_s=5):
        self.base_url = _validate_base_url(base_url)
        token = _read_secret(token_file)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 2.0)),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self):
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, json_body=None, content=None, headers=None):
        try:
            async with self._client.stream(
                method,
                path,
                headers=headers or self._headers,
                json=json_body,
                content=content,
            ) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise BrowserClientError(
                        "unexpected_response", uncertain=method in {"POST", "DELETE"}
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise BrowserClientError(
                            "invalid_worker_response", uncertain=method in {"POST", "DELETE"}
                        )
                    body.extend(chunk)
        except httpx.HTTPError:
            raise BrowserClientError(
                "worker_transport_error", uncertain=method in {"POST", "DELETE"}
            ) from None
        if status >= 500:
            raise BrowserClientError("worker_unavailable", uncertain=method in {"POST", "DELETE"})
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError):
            raise BrowserClientError(
                "invalid_worker_response", uncertain=method in {"POST", "DELETE"}
            ) from None
        return status, value

    async def create_job(self, value: object) -> BrowserJob:
        body = _validate_job_request(value)
        # `content` avoids an implicit re-serialization that might change the size check.
        status, response = await self._request(
            "POST",
            "/jobs",
            content=body,
            headers={**self._headers, "Content-Type": "application/json"},
        )
        if status not in {200, 202}:
            self._raise_safe_error(response)
        try:
            record = _parse_record(response, value["job_id"])
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise BrowserClientError("invalid_worker_response", uncertain=True) from None
        return record

    @staticmethod
    def _raise_safe_error(value):
        code = value.get("error") if isinstance(value, dict) and set(value) == {"error"} else None
        if not isinstance(code, str) or not _SAFE_CODE.fullmatch(code):
            code = "worker_request_rejected"
        raise BrowserClientError(code)

    async def job(self, job_id: str) -> BrowserJob:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ValueError("invalid browser job ID")
        status, value = await self._request("GET", f"/jobs/{job_id}")
        if status != 200:
            self._raise_safe_error(value)
        try:
            return _parse_record(value, job_id)
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise BrowserClientError("invalid_worker_response") from None

    async def cancel(self, job_id: str) -> BrowserJob:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ValueError("invalid browser job ID")
        status, value = await self._request("POST", f"/jobs/{job_id}/cancel", json_body={})
        if status != 200:
            self._raise_safe_error(value)
        try:
            return _parse_record(value, job_id)
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise BrowserClientError("invalid_worker_response", uncertain=True) from None

    async def verify(self, job_id: str, result: str) -> BrowserJob:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ValueError("invalid browser job ID")
        if result not in {"verified", "uncertain"}:
            raise ValueError("invalid browser verification result")
        status, value = await self._request(
            "POST", f"/jobs/{job_id}/verification", json_body={"result": result}
        )
        if status != 200:
            self._raise_safe_error(value)
        try:
            return _parse_record(value, job_id)
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise BrowserClientError("invalid_worker_response", uncertain=True) from None

    async def delete(self, job_id: str) -> bool:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ValueError("invalid browser job ID")
        status, value = await self._request("DELETE", f"/jobs/{job_id}")
        if status != 200:
            self._raise_safe_error(value)
        if value != {"job_id": job_id, "deleted": True}:
            raise BrowserClientError("invalid_worker_response", uncertain=True)
        return True
