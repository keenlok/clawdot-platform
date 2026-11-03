"""Fixed-origin, bounded Bot API transport for the protected bridge service."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from urllib.parse import quote

import httpx

MAX_JSON_RESPONSE = 1024 * 1024
MAX_JSON_REQUEST = 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOKEN_LENGTH = 512
_METHODS = frozenset(
    {
        "sendMessage",
        "sendDocument",
        "sendPoll",
        "editMessageText",
        "deleteMessage",
        "pinChatMessage",
        "sendChatAction",
        "getChatMember",
        "leaveChat",
        "answerCallbackQuery",
        "getMe",
        "setMyCommands",
        "stopPoll",
        "getFile",
    }
)
_FILE_PATH = re.compile(r"^[A-Za-z0-9_./-]{1,512}$")


class BotTransportError(RuntimeError):
    def __init__(self, code: str = "upstream_error") -> None:
        allowed = {"upstream_error", "invalid_request", "file_too_large"}
        self.code = code if code in allowed else "upstream_error"
        super().__init__(self.code)


class BotApiHTTPTransport:
    """Call a narrow Bot API allowlist through api.telegram.org with no redirects."""

    def __init__(
        self,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if type(timeout_seconds) not in (int, float) or not 1 <= timeout_seconds <= 30:
            raise BotTransportError("invalid_request")
        self._client = httpx.AsyncClient(
            base_url="https://api.telegram.org",
            transport=http_transport,
            timeout=httpx.Timeout(float(timeout_seconds), connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def call(self, method: str, parameters: Mapping[str, object], *, token: str) -> object:
        self._validate_token(token)
        if (
            not isinstance(method, str)
            or method not in _METHODS
            or not isinstance(parameters, Mapping)
        ):
            raise BotTransportError("invalid_request")
        try:
            json_parameters = json.dumps(
                dict(parameters), separators=(",", ":"), ensure_ascii=True, allow_nan=False
            ).encode("ascii")
        except (TypeError, ValueError, RecursionError, UnicodeError):
            json_parameters = b""
        if method != "sendDocument" and (
            not json_parameters or len(json_parameters) > MAX_JSON_REQUEST
        ):
            raise BotTransportError("invalid_request")
        path = f"/bot{token}/{method}"
        try:
            if method == "sendDocument":
                document = parameters.get("document")
                filename = parameters.get("filename")
                if (
                    not isinstance(document, bytes)
                    or len(document) > MAX_FILE_BYTES
                    or not isinstance(filename, str)
                    or not 1 <= len(filename) <= 255
                    or "/" in filename
                    or "\\" in filename
                    or any(ord(char) < 32 or ord(char) == 127 for char in filename)
                ):
                    raise BotTransportError("invalid_request")
                fields = {
                    key: value
                    for key, value in parameters.items()
                    if key not in {"document", "filename"}
                }
                try:
                    encoded_fields = json.dumps(
                        fields,
                        separators=(",", ":"),
                        ensure_ascii=True,
                        allow_nan=False,
                    ).encode("ascii")
                except (TypeError, ValueError, RecursionError, UnicodeError):
                    raise BotTransportError("invalid_request") from None
                if len(encoded_fields) > MAX_JSON_REQUEST:
                    raise BotTransportError("invalid_request")
                async with self._client.stream(
                    "POST",
                    path,
                    data={key: self._form_value(value) for key, value in fields.items()},
                    files={"document": (filename, document, "application/octet-stream")},
                ) as response:
                    raw = await self._bounded_response(response)
            else:
                async with self._client.stream("POST", path, json=dict(parameters)) as response:
                    raw = await self._bounded_response(response)
            try:
                envelope = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise BotTransportError() from None
            if (
                not isinstance(envelope, dict)
                or envelope.get("ok") is not True
                or "result" not in envelope
            ):
                raise BotTransportError()
            return envelope["result"]
        except BotTransportError:
            raise
        except Exception:
            raise BotTransportError() from None

    async def download_file(self, file_id: str, max_bytes: int, *, token: str) -> bytes:
        self._validate_token(token)
        if (
            not isinstance(file_id, str)
            or not file_id
            or len(file_id) > 256
            or type(max_bytes) is not int
            or not 0 < max_bytes <= MAX_FILE_BYTES
        ):
            raise BotTransportError("invalid_request")
        metadata = await self.call("getFile", {"file_id": file_id}, token=token)
        file_path = metadata.get("file_path") if isinstance(metadata, Mapping) else None
        if not isinstance(file_path, str) or not _FILE_PATH.fullmatch(file_path):
            raise BotTransportError()
        parts = file_path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise BotTransportError()
        path = f"/file/bot{token}/" + quote(file_path, safe="/._-")
        try:
            async with self._client.stream("GET", path) as response:
                if response.status_code != 200:
                    raise BotTransportError()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > max_bytes:
                        raise BotTransportError("file_too_large")
                    content.extend(chunk)
                return bytes(content)
        except BotTransportError:
            raise
        except Exception:
            raise BotTransportError() from None

    @staticmethod
    def _validate_token(token: str) -> None:
        if (
            not isinstance(token, str)
            or not token
            or len(token) > MAX_TOKEN_LENGTH
            or not token.isascii()
            or any(ord(char) < 33 or char in "/?#" for char in token)
        ):
            raise BotTransportError("invalid_request")

    @staticmethod
    def _form_value(value: object) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"), ensure_ascii=True)
        if value is True:
            return "true"
        if value is False:
            return "false"
        return str(value)

    @staticmethod
    async def _bounded_response(response: httpx.Response) -> bytes:
        if response.status_code != 200:
            raise BotTransportError()
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > MAX_JSON_RESPONSE:
                raise BotTransportError()
            content.extend(chunk)
        return bytes(content)

    async def close(self) -> None:
        await self._client.aclose()

    def __repr__(self) -> str:
        return "BotApiHTTPTransport(origin=api.telegram.org, token=[REDACTED])"


__all__ = ["BotApiHTTPTransport", "BotTransportError"]
