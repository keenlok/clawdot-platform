"""Bounded ASGI HTTP boundary for the protected local Telegram bridge."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx

from .durable_relay import RelayError, RelayEvent
from .telegram_bridge import BridgeError, TelegramBridge
from .telegram_ingress import verify_relay_body

MAX_HTTP_BODY = 28 * 1024 * 1024
MAX_RELAY_WAIT_MS = 25_000
MAX_RELAY_ACK_BATCH = 20
_WAIT_MS = re.compile(r"^(?:0|[1-9][0-9]{0,4})$")


class BridgeHTTPError(RuntimeError):
    """Fixed HTTP/client error with no transport diagnostics."""

    _CODES = frozenset(
        {
            "invalid_configuration",
            "unauthorized",
            "forbidden",
            "invalid_request",
            "unsupported_operation",
            "upstream_error",
            "file_too_large",
            "relay_unavailable",
            "http_error",
        }
    )

    def __init__(self, code: str = "http_error") -> None:
        self.code = code if code in self._CODES else "http_error"
        super().__init__(self.code)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BridgeHTTPError("invalid_request")
        result[key] = value
    return result


def _json_load(raw: bytes) -> object:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError, RecursionError):
        raise BridgeHTTPError("invalid_request") from None


def _status(code: str) -> int:
    return {
        "unauthorized": 401,
        "forbidden": 403,
        "invalid_request": 400,
        "unsupported_operation": 404,
        "file_too_large": 413,
        "upstream_error": 502,
        "relay_unavailable": 503,
    }.get(code, 400)


def _require_private_uds(path: str) -> None:
    try:
        socket = os.lstat(path)
        parent = os.lstat(os.path.dirname(path))
        if (
            not stat.S_ISSOCK(socket.st_mode)
            or socket.st_uid != os.getuid()
            or stat.S_IMODE(socket.st_mode) & 0o077
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise BridgeHTTPError("invalid_configuration")
    except BridgeHTTPError:
        raise
    except OSError:
        raise BridgeHTTPError("invalid_configuration") from None


class TelegramBridgeASGI:
    """ASGI app for strict ordinary RPC and authenticated ingress relay polling."""

    def __init__(
        self,
        bridge: TelegramBridge,
        *,
        relay_store: object,
        relay_key: bytes,
        audiences: Mapping[str, frozenset[str]],
        allow_private_uds: bool = False,
        global_event_principals: frozenset[str] = frozenset(),
    ) -> None:
        if (
            not isinstance(bridge, TelegramBridge)
            or not callable(getattr(relay_store, "enqueue", None))
            or not callable(getattr(relay_store, "claim", None))
            or not callable(getattr(relay_store, "ack", None))
            or not isinstance(relay_key, bytes)
            or len(relay_key) < 32
            or not isinstance(audiences, Mapping)
            or type(allow_private_uds) is not bool
            or not isinstance(global_event_principals, frozenset)
        ):
            raise BridgeHTTPError("invalid_configuration")
        self._bridge = bridge
        self._relay = relay_store
        self._relay_key = relay_key
        normalized: dict[str, frozenset[str]] = {}
        for principal, chats in audiences.items():
            if (
                not isinstance(principal, str)
                or not principal
                or not isinstance(chats, frozenset)
                or any(
                    not isinstance(chat, str) or not bridge.principal_allows_chat(principal, chat)
                    for chat in chats
                )
            ):
                raise BridgeHTTPError("invalid_configuration")
            normalized[principal] = chats
        self._audiences = normalized
        if not global_event_principals <= self._audiences.keys():
            raise BridgeHTTPError("invalid_configuration")
        self._global_event_principals = global_event_principals
        self._allow_private_uds = allow_private_uds

    def __repr__(self) -> str:
        return "TelegramBridgeASGI(secrets=[REDACTED])"

    async def accept_relay(self, raw: bytes, signature: str) -> None:
        """Trusted in-process ingress hook; never exposed as an HTTP route."""
        try:
            update = verify_relay_body(raw, signature, self._relay_key)
            update_id = update["update_id"]
            if type(update_id) is not int:
                raise BridgeHTTPError("invalid_request")
            callback = update.get("callback_query")
            data = callback.get("data") if isinstance(callback, dict) else None
            if isinstance(data, str) and data.startswith("release:"):
                raise BridgeHTTPError("forbidden")
            source = callback.get("message") if isinstance(callback, dict) else None
            if source is None:
                source = next(
                    (
                        update.get(key)
                        for key in ("message", "chat_member", "my_chat_member")
                        if isinstance(update.get(key), dict)
                    ),
                    None,
                )
            chat = source.get("chat") if isinstance(source, dict) else None
            raw_chat_id = chat.get("id") if isinstance(chat, dict) else None
            if type(raw_chat_id) is int:
                chat_id = str(raw_chat_id)
                recipients = tuple(
                    principal
                    for principal, allowed in self._audiences.items()
                    if chat_id in allowed
                )
            elif isinstance(update.get("poll_answer"), dict):
                chat_id = ""
                recipients = tuple(self._global_event_principals)
            else:
                return
            for principal in recipients:
                await self._relay.enqueue(principal, update_id, update)
        except RelayError:
            raise BridgeHTTPError("relay_unavailable") from None
        except BridgeHTTPError:
            raise
        except Exception:
            raise BridgeHTTPError("invalid_request") from None

    def _replace_file_ids(self, principal: str, chat_id: str, message: dict[str, object]) -> None:
        for field in ("photo", "document", "audio", "video", "voice"):
            value = message.get(field)
            items = value if isinstance(value, list) else [value]
            for item in items:
                if not isinstance(item, dict):
                    continue
                file_id = item.get("file_id")
                if not isinstance(file_id, str):
                    continue
                size = item.get("file_size")
                handle = self._bridge.register_file_handle(
                    principal,
                    chat_id,
                    file_id,
                    declared_size=size if type(size) is int else None,
                )
                item["file_id"] = handle

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            return
        try:
            scheme = scope.get("scheme")
            if scheme != "https" and not (scheme == "http" and self._allow_private_uds):
                raise BridgeHTTPError("forbidden")
            method, path = scope.get("method"), scope.get("path")
            credential = self._credential(scope.get("headers", []))
            principal = self._principal(credential)
            if method == "POST" and path == "/v1/request":
                content_types = [
                    value
                    for name, value in scope.get("headers", [])
                    if name.lower() == b"content-type"
                ]
                if (
                    len(content_types) != 1
                    or content_types[0].split(b";", 1)[0].strip().lower() != b"application/json"
                ):
                    raise BridgeHTTPError("invalid_request")
                raw = await self._body(receive)
                request = _json_load(raw)
                if not isinstance(request, dict) or set(request) != {"operation", "payload"}:
                    raise BridgeHTTPError("invalid_request")
                try:
                    result = await self._bridge.handle(credential, request)
                except BridgeError as exc:
                    raise BridgeHTTPError(exc.code) from None
                if isinstance(result, bytes):
                    await self._respond(send, 200, result, b"application/octet-stream")
                else:
                    await self._respond(send, 200, {"ok": True, "result": result})
                return
            if method == "GET" and path == "/v1/events":
                query = parse_qs(
                    scope.get("query_string", b"").decode("ascii", "strict"), strict_parsing=True
                )
                if set(query) - {"wait_ms"} or any(len(values) != 1 for values in query.values()):
                    raise BridgeHTTPError("invalid_request")
                raw_wait_ms = query.get("wait_ms", ["0"])[0]
                if not _WAIT_MS.fullmatch(raw_wait_ms):
                    raise BridgeHTTPError("invalid_request") from None
                wait_ms = int(raw_wait_ms)
                if wait_ms > MAX_RELAY_WAIT_MS:
                    raise BridgeHTTPError("invalid_request")
                deadline = asyncio.get_running_loop().time() + wait_ms / 1000
                claimed = await self._relay.claim(principal, limit=MAX_RELAY_ACK_BATCH)
                while not claimed and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(
                        min(0.1, max(0, deadline - asyncio.get_running_loop().time()))
                    )
                    claimed = await self._relay.claim(principal, limit=MAX_RELAY_ACK_BATCH)
                events = [self._materialize(principal, event) for event in claimed]
                await self._respond(send, 200, {"events": events})
                return
            if method == "POST" and path == "/v1/events/ack":
                content_types = [
                    value
                    for name, value in scope.get("headers", [])
                    if name.lower() == b"content-type"
                ]
                if (
                    len(content_types) != 1
                    or content_types[0].split(b";", 1)[0].strip().lower() != b"application/json"
                ):
                    raise BridgeHTTPError("invalid_request")
                body = _json_load(await self._body(receive))
                if (
                    not isinstance(body, dict)
                    or set(body) != {"relay_ids"}
                    or not isinstance(body["relay_ids"], list)
                ):
                    raise BridgeHTTPError("invalid_request")
                ids = body["relay_ids"]
                if not 1 <= len(ids) <= MAX_RELAY_ACK_BATCH or any(
                    not isinstance(value, str) for value in ids
                ):
                    raise BridgeHTTPError("invalid_request")
                try:
                    parsed_ids = [UUID(value) for value in ids]
                except (ValueError, TypeError):
                    raise BridgeHTTPError("invalid_request") from None
                if any(
                    str(parsed) != original
                    for parsed, original in zip(parsed_ids, ids, strict=True)
                ):
                    raise BridgeHTTPError("invalid_request")
                try:
                    await self._relay.ack(principal, parsed_ids)
                except RelayError:
                    raise BridgeHTTPError("invalid_request") from None
                await self._respond(send, 200, {"ok": True})
                return
            raise BridgeHTTPError("unsupported_operation")
        except BridgeHTTPError as exc:
            await self._respond(send, _status(exc.code), {"ok": False, "error": exc.code})
        except RelayError:
            await self._respond(send, 503, {"ok": False, "error": "relay_unavailable"})
        except Exception:
            await self._respond(send, 400, {"ok": False, "error": "invalid_request"})

    def _materialize(self, principal: str, event: RelayEvent) -> dict[str, object]:
        update = json.loads(json.dumps(event.payload, separators=(",", ":")))
        callback_handle = None
        callback = update.get("callback_query")
        source = callback.get("message") if isinstance(callback, dict) else None
        if source is None:
            source = next(
                (
                    update.get(key)
                    for key in ("message", "chat_member", "my_chat_member")
                    if isinstance(update.get(key), dict)
                ),
                None,
            )
        chat = source.get("chat") if isinstance(source, dict) else None
        raw_chat_id = chat.get("id") if isinstance(chat, dict) else None
        chat_id = str(raw_chat_id) if type(raw_chat_id) is int else ""
        if isinstance(callback, dict):
            raw_callback_id = callback.get("id")
            if isinstance(raw_callback_id, str) and raw_callback_id:
                callback_handle = self._bridge.register_callback_handle(
                    principal, chat_id, raw_callback_id, callback.get("data", "")
                )
                callback.pop("id", None)
        message = update.get("message")
        if isinstance(message, dict):
            self._replace_file_ids(principal, chat_id, message)
        return {
            "relay_id": str(event.relay_id),
            "update_id": event.update_id,
            "update": update,
            "callback_handle": callback_handle,
        }

    async def _body(self, receive) -> bytes:
        data = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                raise BridgeHTTPError("invalid_request")
            if message.get("type") != "http.request":
                continue
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes) or len(data) + len(chunk) > MAX_HTTP_BODY:
                raise BridgeHTTPError("invalid_request")
            data.extend(chunk)
            if not message.get("more_body", False):
                return bytes(data)

    def _principal(self, credential: str) -> str:
        try:
            return self._bridge.resolve_principal(credential)
        except BridgeError as exc:
            raise BridgeHTTPError(exc.code) from None
        except Exception:
            raise BridgeHTTPError("unauthorized") from None

    @staticmethod
    def _credential(headers) -> str:
        values = [value for name, value in headers if name.lower() == b"authorization"]
        if len(values) != 1 or not values[0].startswith(b"Bearer "):
            raise BridgeHTTPError("unauthorized")
        try:
            credential = values[0][7:].decode("ascii")
        except UnicodeError:
            raise BridgeHTTPError("unauthorized") from None
        if not credential or len(credential) > 4096:
            raise BridgeHTTPError("unauthorized")
        return credential

    @staticmethod
    async def _respond(
        send, status: int, value: object, content_type: bytes = b"application/json"
    ) -> None:
        if content_type == b"application/json":
            body = json.dumps(
                value, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            ).encode()
        else:
            body = value
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", content_type),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class TelegramBridgeHTTPClient:
    """Token-free ordinary HTTP client; network transport is HTTPS or explicit UDS."""

    def __init__(
        self,
        *,
        credential: str,
        origin: str | None = None,
        uds_path: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(credential, str) or not credential or len(credential) > 4096:
            raise BridgeHTTPError("unauthorized")
        if (origin is None) == (uds_path is None):
            raise BridgeHTTPError("invalid_configuration")
        if origin is not None:
            try:
                parsed = urlsplit(origin)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.path not in ("", "/")
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError
                base_url = origin.rstrip("/")
            except (ValueError, TypeError):
                raise BridgeHTTPError("invalid_configuration") from None
            client_transport = transport
        else:
            if not isinstance(uds_path, str) or not uds_path.startswith("/") or "\x00" in uds_path:
                raise BridgeHTTPError("invalid_configuration")
            _require_private_uds(uds_path)
            base_url = "http://bridge.local"
            client_transport = transport or httpx.AsyncHTTPTransport(uds=uds_path)
        self._credential = credential
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=client_transport,
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def request(self, operation: str, payload: Mapping[str, object]) -> object:
        if not isinstance(operation, str) or not isinstance(payload, Mapping):
            raise BridgeHTTPError("invalid_request")
        return await self._post({"operation": operation, "payload": dict(payload)})

    async def poll_events(self, wait_ms: int = 0) -> list[object]:
        if type(wait_ms) is not int or not 0 <= wait_ms <= MAX_RELAY_WAIT_MS:
            raise BridgeHTTPError("invalid_request")
        try:
            response = await self._client.get(
                "/v1/events", params={"wait_ms": wait_ms}, headers=self._headers()
            )
            result = self._decode(response)
            events = result.get("events") if isinstance(result, dict) else None
            if not isinstance(events, list):
                raise BridgeHTTPError("http_error")
            return events
        except BridgeHTTPError:
            raise
        except Exception:
            raise BridgeHTTPError("http_error") from None

    async def ack_events(self, relay_ids: Sequence[str]) -> None:
        if (
            not isinstance(relay_ids, Sequence)
            or isinstance(relay_ids, (str, bytes))
            or not 1 <= len(relay_ids) <= MAX_RELAY_ACK_BATCH
            or any(not isinstance(value, str) for value in relay_ids)
        ):
            raise BridgeHTTPError("invalid_request")
        await self._post({"relay_ids": list(relay_ids)}, path="/v1/events/ack")

    async def _post(self, value: dict[str, object], *, path: str = "/v1/request") -> object:
        try:
            response = await self._client.post(path, json=value, headers=self._headers())
            result = self._decode(response)
            if isinstance(result, bytes):
                return result
            if path == "/v1/events/ack" and result == {"ok": True}:
                return result
            if (
                not isinstance(result, dict)
                or set(result) != {"ok", "result"}
                or result["ok"] is not True
            ):
                raise BridgeHTTPError("http_error")
            return result["result"]
        except BridgeHTTPError:
            raise
        except Exception:
            raise BridgeHTTPError("http_error") from None

    def _decode(self, response: httpx.Response) -> object:
        if response.status_code < 200 or response.status_code >= 300:
            payload = (
                response.json()
                if response.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            code = payload.get("error") if isinstance(payload, dict) else None
            raise BridgeHTTPError(code if isinstance(code, str) else "http_error")
        if response.headers.get("content-type", "").startswith("application/octet-stream"):
            return response.content
        try:
            return response.json()
        except Exception:
            raise BridgeHTTPError("http_error") from None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._credential}"}

    async def close(self) -> None:
        await self._client.aclose()

    def __repr__(self) -> str:
        return "TelegramBridgeHTTPClient(credential=[REDACTED])"


__all__ = ["BridgeHTTPError", "TelegramBridgeASGI", "TelegramBridgeHTTPClient"]
