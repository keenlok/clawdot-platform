"""Local protected Telegram Bot API bridge core.

This module deliberately has no HTTP server or Telegram dependency. An installed broker
must authenticate callers before delegating to :meth:`TelegramBridge.handle` and must
keep the Bot API transport/token outside the ordinary application process. Broker card
registration methods are trusted in-process controls, not part of the ordinary request API.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

MAX_REQUEST_BYTES = 64 * 1024
MAX_TEXT_CHARS = 4096
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILE_REQUEST_BYTES = 28 * 1024 * 1024
MAX_FILE_HANDLES = 10_000
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_CHAT_ID = re.compile(r"^-?[0-9]{1,20}$")
_ACTION_ID = re.compile(r"^[A-Za-z0-9_-]{1,56}$")
_ENTITY_TYPES = frozenset(
    {"bold", "italic", "underline", "strikethrough", "spoiler", "code", "pre", "text_link"}
)
_COMMAND_NAME = re.compile(r"^[a-z0-9_]{1,32}$")


class BridgeError(ValueError):
    """A fixed, non-sensitive bridge failure."""

    _CODES = frozenset(
        {
            "unauthorized",
            "forbidden",
            "invalid_request",
            "unsupported_operation",
            "upstream_error",
            "file_too_large",
        }
    )

    def __init__(self, code: str = "invalid_request") -> None:
        self.code = code if code in self._CODES else "invalid_request"
        super().__init__(self.code)


class BotApiTransport(Protocol):
    """Injectable Bot API transport; implementations must not return token URLs."""

    async def call(
        self, method: str, parameters: Mapping[str, object], *, token: str
    ) -> object: ...

    async def download_file(self, file_id: str, max_bytes: int, *, token: str) -> bytes: ...


class RequestTransport(Protocol):
    """Ordinary-client transport to an authenticated bridge endpoint."""

    async def __call__(self, credential: str, request: Mapping[str, object]) -> object: ...


class MessageOwnershipStore:
    """Small local ownership index; not a multi-process or production ledger."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._messages: set[tuple[str, str, str]] = set()
        self._protected: set[tuple[str, str]] = set()
        if self._path is not None:
            self._load()

    def owns(self, principal: str, chat_id: str, message_id: str) -> bool:
        return (principal, chat_id, message_id) in self._messages

    @property
    def protected_messages(self) -> frozenset[tuple[str, str]]:
        return frozenset(self._protected)

    def protect(self, chat_id: str, message_id: str) -> None:
        if (chat_id, message_id) not in self._protected:
            if len(self._protected) >= 100_000:
                raise BridgeError("invalid_request")
            self._protected.add((chat_id, message_id))
            self._save()

    def add(self, principal: str, chat_id: str, message_id: str) -> None:
        key = (principal, chat_id, message_id)
        if key not in self._messages:
            if len(self._messages) >= 100_000:
                raise BridgeError("invalid_request")
            self._messages.add(key)
            self._save()

    def remove(self, principal: str, chat_id: str, message_id: str) -> None:
        key = (principal, chat_id, message_id)
        if key in self._messages:
            self._messages.remove(key)
            self._save()

    def _load(self) -> None:
        assert self._path is not None
        try:
            self._check_store_path()
            raw = self._path.read_bytes()
            if len(raw) > MAX_REQUEST_BYTES * 16:
                raise BridgeError("invalid_request")
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or set(value) != {"version", "messages", "protected"}
                or type(value["version"]) is not int
                or value["version"] != 2
                or not isinstance(value["messages"], list)
                or not isinstance(value["protected"], list)
                or len(value["messages"]) > 100_000
                or len(value["protected"]) > 100_000
            ):
                raise BridgeError("invalid_request")
            rows: set[tuple[str, str, str]] = set()
            for row in value["messages"]:
                if (
                    not isinstance(row, list)
                    or len(row) != 3
                    or any(not isinstance(item, str) or not item for item in row)
                ):
                    raise BridgeError("invalid_request")
                rows.add((row[0], row[1], row[2]))
            protected: set[tuple[str, str]] = set()
            for row in value["protected"]:
                if (
                    not isinstance(row, list)
                    or len(row) != 2
                    or not isinstance(row[0], str)
                    or not _CHAT_ID.fullmatch(row[0])
                    or not isinstance(row[1], str)
                    or not row[1].isascii()
                    or not row[1].isdecimal()
                    or not 1 <= len(row[1]) <= 20
                    or int(row[1]) <= 0
                ):
                    raise BridgeError("invalid_request")
                protected.add((row[0], row[1]))
            self._messages = rows
            self._protected = protected
        except FileNotFoundError:
            return
        except BridgeError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
            raise BridgeError("invalid_request") from None

    def _save(self) -> None:
        if self._path is None:
            return
        parent = self._path.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._check_store_path()
            rows = json.dumps(
                {
                    "version": 2,
                    "messages": sorted(self._messages),
                    "protected": sorted(self._protected),
                },
                separators=(",", ":"),
            ).encode()
            descriptor, temporary = tempfile.mkstemp(prefix=".ownership-", dir=parent)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(rows)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self._path)
                directory_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        except BridgeError:
            raise
        except OSError:
            raise BridgeError("invalid_request") from None

    def _check_store_path(self) -> None:
        assert self._path is not None
        try:
            mode = self._path.lstat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISREG(mode):
            raise BridgeError("invalid_request")


class TelegramBridge:
    """Authenticated, bounded Bot API service core with broker-owned authority."""

    def __init__(
        self,
        *,
        token: str,
        authenticate: Callable[[str], str | None],
        allowed_chats: Mapping[str, frozenset[str]],
        transport: BotApiTransport,
        load_protected_messages: Callable[[], tuple[tuple[str, str], ...]],
        ownership: MessageOwnershipStore | None = None,
        max_file_bytes: int = MAX_FILE_BYTES,
        menu_principal: str | None = None,
        reserved_commands: Mapping[str, Sequence[tuple[str, str]]] | None = None,
    ) -> None:
        if not isinstance(token, str) or not token or len(token) > 512:
            raise BridgeError("invalid_request")
        if type(max_file_bytes) is not int or not 1 <= max_file_bytes <= MAX_FILE_BYTES:
            raise BridgeError("invalid_request")
        self._token = token
        self._authenticate = authenticate
        self._allowed_chats = {
            principal: frozenset(chats) for principal, chats in allowed_chats.items()
        }
        if menu_principal is not None and (
            not isinstance(menu_principal, str)
            or not menu_principal
            or menu_principal not in self._allowed_chats
        ):
            raise BridgeError("invalid_request")
        if reserved_commands is None:
            reserved_commands = {}
        if not isinstance(reserved_commands, Mapping) or set(reserved_commands) - {
            "private",
            "groups",
        }:
            raise BridgeError("invalid_request")
        self._menu_principal = menu_principal
        self._reserved_commands = {
            scope: self._validated_command_pairs(commands)
            for scope, commands in reserved_commands.items()
        }
        self._transport = transport
        self._ownership = ownership or MessageOwnershipStore()
        self._ready = False
        self._protected_messages = set(self._ownership.protected_messages)
        self._file_handles: dict[str, tuple[str, str, str, int | None]] = {}
        self._callback_handles: dict[str, tuple[str, str]] = {}
        self._max_file_bytes = max_file_bytes
        try:
            restored = load_protected_messages()
            if not isinstance(restored, (tuple, list)) or len(restored) > 100_000:
                raise BridgeError("invalid_request")
            for row in restored:
                if not isinstance(row, (tuple, list)) or len(row) != 2:
                    raise BridgeError("invalid_request")
                chat, message = self._chat(row[0]), self._message_id(row[1])
                self._ownership.protect(chat, message)
                self._protected_messages.add((chat, message))
        except Exception:
            raise BridgeError("upstream_error") from None
        self._ready = True

    async def handle(self, credential: str, request: Mapping[str, object]) -> object:
        if not self._ready:
            raise BridgeError("forbidden")
        principal = self._principal(credential)
        operation, payload = self._request(request)
        if operation not in {
            "send_message",
            "confirm",
            "send_poll",
            "send_document",
            "stop_poll",
            "answer_callback",
            "get_me",
            "set_commands",
            "edit_message",
            "delete_message",
            "pin_message",
            "typing",
            "get_chat_member",
            "leave_chat",
            "download_file",
        }:
            raise BridgeError("unsupported_operation")
        chat_id = payload.get("chat_id")
        if operation not in {
            "download_file",
            "answer_callback",
            "get_me",
            "set_commands",
        }:
            chat = self._chat(chat_id)
            if chat not in self._allowed_chats.get(principal, frozenset()):
                raise BridgeError("forbidden")
        elif operation == "download_file":
            chat = self._chat(chat_id)
            if chat not in self._allowed_chats.get(principal, frozenset()):
                raise BridgeError("forbidden")

        if operation == "send_message":
            self._exact(payload, {"chat_id", "text"}, {"reply_to", "thread_id", "entities"})
            text = self._text(payload["text"])
            params: dict[str, object] = {
                "chat_id": chat,
                "text": text,
                "link_preview_options": {"is_disabled": True},
            }
            self._add_message_options(params, payload, text)
            result = await self._call("sendMessage", params)
            return self._record_sent(principal, chat, result)
        if operation == "confirm":
            self._exact(payload, {"chat_id", "prompt", "action_id"}, {"reply_to", "thread_id"})
            prompt = self._text(payload["prompt"])
            action_id = payload["action_id"]
            if not isinstance(action_id, str) or not _ACTION_ID.fullmatch(action_id):
                raise BridgeError("invalid_request")
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "Confirm", "callback_data": f"c:{action_id}"},
                        {"text": "Cancel", "callback_data": f"x:{action_id}"},
                    ]
                ]
            }
            result = await self._call(
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": prompt,
                    "reply_markup": markup,
                    "link_preview_options": {"is_disabled": True},
                    **self._thread_reply(payload),
                },
            )
            return self._record_sent(principal, chat, result)
        if operation == "send_poll":
            self._exact(
                payload,
                {"chat_id", "question", "options", "multiple", "anonymous"},
                {"thread_id", "reply_to"},
            )
            question = self._text(payload["question"], maximum=300)
            options = payload["options"]
            if (
                not isinstance(options, list)
                or not 2 <= len(options) <= 12
                or any(not isinstance(item, str) or not item or len(item) > 100 for item in options)
                or type(payload["multiple"]) is not bool
                or type(payload["anonymous"]) is not bool
            ):
                raise BridgeError("invalid_request")
            result = await self._call(
                "sendPoll",
                {
                    "chat_id": chat,
                    "question": question,
                    "options": options,
                    "allows_multiple_answers": payload["multiple"],
                    "is_anonymous": payload["anonymous"],
                    "type": "regular",
                    **self._thread_reply(payload),
                },
            )
            sent = self._record_sent(principal, chat, result)
            if not isinstance(result, Mapping) or not isinstance(result.get("poll"), Mapping):
                raise BridgeError("upstream_error")
            poll_id = result["poll"].get("id")
            if not isinstance(poll_id, str) or not _IDENTIFIER.fullmatch(poll_id):
                raise BridgeError("upstream_error")
            return {**sent, "poll_id": poll_id}
        if operation == "send_document":
            self._exact(
                payload,
                {"chat_id", "filename", "data_base64"},
                {"caption", "thread_id", "reply_to"},
            )
            filename = payload["filename"]
            encoded_data = payload["data_base64"]
            caption = payload.get("caption")
            if (
                not isinstance(filename, str)
                or not filename
                or len(filename) > 255
                or "/" in filename
                or "\\" in filename
                or not isinstance(encoded_data, str)
                or len(encoded_data) > ((self._max_file_bytes + 2) // 3) * 4
                or (caption is not None and (not isinstance(caption, str) or len(caption) > 1024))
            ):
                raise BridgeError("invalid_request")
            try:
                data = base64.b64decode(encoded_data, validate=True)
            except (ValueError, base64.binascii.Error):
                raise BridgeError("invalid_request") from None
            if len(data) > self._max_file_bytes:
                raise BridgeError("file_too_large")
            params: dict[str, object] = {"chat_id": chat, "filename": filename, "document": data}
            if caption is not None:
                params["caption"] = caption
            params.update(self._thread_reply(payload))
            return self._record_sent(principal, chat, await self._call("sendDocument", params))
        if operation == "stop_poll":
            self._exact(payload, {"chat_id", "message_id"})
            message_id = self._message_id(payload["message_id"])
            self._require_owned(principal, chat, message_id)
            if (chat, message_id) in self._protected_messages:
                raise BridgeError("forbidden")
            result = await self._call("stopPoll", {"chat_id": chat, "message_id": int(message_id)})
            if not isinstance(result, Mapping):
                raise BridgeError("upstream_error")
            options = result.get("options")
            total = result.get("total_voter_count")
            if (
                not isinstance(options, list)
                or not 2 <= len(options) <= 12
                or type(total) is not int
                or total < 0
            ):
                raise BridgeError("upstream_error")
            counts = []
            for option in options:
                count = option.get("voter_count") if isinstance(option, Mapping) else None
                if type(count) is not int or count < 0:
                    raise BridgeError("upstream_error")
                counts.append(count)
            return {"counts": counts, "total_voters": total}
        if operation == "answer_callback":
            self._exact(payload, {"callback_handle"})
            handle = payload["callback_handle"]
            if not isinstance(handle, str) or not re.fullmatch(r"[0-9a-f]{32}", handle):
                raise BridgeError("invalid_request")
            registered = self._callback_handles.get(handle)
            if registered is None or registered[0] != principal:
                raise BridgeError("forbidden")
            self._callback_handles.pop(handle, None)
            await self._call("answerCallbackQuery", {"callback_query_id": registered[1]})
            return {"ok": True}
        if operation == "get_me":
            self._exact(payload, set())
            result = await self._call("getMe", {})
            if not isinstance(result, Mapping):
                raise BridgeError("upstream_error")
            bot_id = result.get("id")
            is_bot = result.get("is_bot")
            first_name = result.get("first_name")
            username = result.get("username")
            if (
                type(bot_id) is not int
                or bot_id <= 0
                or is_bot is not True
                or not isinstance(first_name, str)
                or not first_name
                or len(first_name) > 256
                or (
                    username is not None
                    and (
                        not isinstance(username, str)
                        or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
                    )
                )
            ):
                raise BridgeError("upstream_error")
            return {"id": bot_id, "is_bot": True, "first_name": first_name, "username": username}
        if operation == "set_commands":
            self._exact(payload, {"scope", "commands"})
            if self._menu_principal is None or principal != self._menu_principal:
                raise BridgeError("forbidden")
            scope = payload["scope"]
            if not isinstance(scope, str) or scope not in {"private", "groups"}:
                raise BridgeError("invalid_request")
            commands = self._validated_command_rows(payload["commands"])
            reserved = self._reserved_commands.get(scope, ())
            reserved_names = {name for name, _ in reserved}
            merged = list(reserved)
            merged.extend(
                (name, description) for name, description in commands if name not in reserved_names
            )
            if len(merged) > 100:
                raise BridgeError("invalid_request")
            api_scope = "all_private_chats" if scope == "private" else "all_group_chats"
            result = await self._call(
                "setMyCommands",
                {
                    "commands": [
                        {"command": name, "description": description}
                        for name, description in merged
                    ],
                    "scope": {"type": api_scope},
                },
            )
            if result is not True:
                raise BridgeError("upstream_error")
            return {"ok": True}
        if operation == "edit_message":
            self._exact(payload, {"chat_id", "message_id", "text"}, {"entities"})
            message_id = self._message_id(payload["message_id"])
            self._require_owned(principal, chat, message_id)
            if (chat, message_id) in self._protected_messages:
                raise BridgeError("forbidden")
            text = self._text(payload["text"])
            params = {
                "chat_id": chat,
                "message_id": int(message_id),
                "text": text,
                "link_preview_options": {"is_disabled": True},
            }
            if "entities" in payload:
                params["entities"] = self._entities(payload["entities"], text)
            await self._call("editMessageText", params)
            return {"ok": True}
        if operation == "delete_message":
            self._exact(payload, {"chat_id", "message_id"})
            message_id = self._message_id(payload["message_id"])
            self._require_owned(principal, chat, message_id)
            if (chat, message_id) in self._protected_messages:
                raise BridgeError("forbidden")
            await self._call("deleteMessage", {"chat_id": chat, "message_id": int(message_id)})
            self._ownership.remove(principal, chat, message_id)
            return {"ok": True}
        if operation == "pin_message":
            self._exact(payload, {"chat_id", "message_id"})
            message_id = self._message_id(payload["message_id"])
            self._require_owned(principal, chat, message_id)
            if (chat, message_id) in self._protected_messages:
                raise BridgeError("forbidden")
            await self._call(
                "pinChatMessage",
                {"chat_id": chat, "message_id": int(message_id), "disable_notification": True},
            )
            return {"ok": True}
        if operation == "typing":
            self._exact(payload, {"chat_id"}, {"thread_id"})
            await self._call(
                "sendChatAction",
                {"chat_id": chat, "action": "typing", **self._thread_reply(payload)},
            )
            return {"ok": True}
        if operation == "get_chat_member":
            self._exact(payload, {"chat_id", "user_id"})
            user_id = self._numeric_id(payload["user_id"])
            if int(user_id) <= 0:
                raise BridgeError("invalid_request")
            result = await self._call("getChatMember", {"chat_id": chat, "user_id": int(user_id)})
            if not isinstance(result, Mapping):
                raise BridgeError("upstream_error")
            status = result.get("status")
            if not isinstance(status, str) or status not in {
                "creator",
                "administrator",
                "member",
                "restricted",
                "left",
                "kicked",
            }:
                raise BridgeError("upstream_error")
            filtered: dict[str, object] = {"status": status}
            if status == "restricted":
                if type(result.get("is_member")) is not bool:
                    raise BridgeError("upstream_error")
                filtered["is_member"] = result["is_member"]
            return filtered
        if operation == "leave_chat":
            self._exact(payload, {"chat_id"})
            await self._call("leaveChat", {"chat_id": chat})
            return {"ok": True}
        if operation == "download_file":
            self._exact(payload, {"chat_id", "file_handle", "max_bytes"})
            handle = payload["file_handle"]
            limit = payload["max_bytes"]
            if (
                not isinstance(handle, str)
                or type(limit) is not int
                or not 0 < limit <= self._max_file_bytes
            ):
                raise BridgeError("invalid_request")
            binding = self._file_handles.get(handle)
            if binding is None or binding[:2] != (principal, chat):
                raise BridgeError("forbidden")
            if binding[3] is not None and binding[3] > limit:
                raise BridgeError("file_too_large")
            try:
                result = await self._transport.download_file(binding[2], limit, token=self._token)
            except Exception:
                raise BridgeError("upstream_error") from None
            if not isinstance(result, bytes):
                raise BridgeError("upstream_error")
            if len(result) > limit:
                raise BridgeError("file_too_large")
            return result
        raise BridgeError("unsupported_operation")

    def protect_release_message(self, chat_id: str, message_id: str) -> None:
        """Mark a broker-created approval card as immutable to ordinary clients."""
        if not self._ready:
            raise BridgeError("forbidden")
        chat, message = self._chat(chat_id), self._message_id(message_id)
        self._protected_messages.add((chat, message))
        try:
            self._ownership.protect(chat, message)
        except Exception:
            self._ready = False
            raise BridgeError("upstream_error") from None

    def principal_allows_chat(self, principal: str, chat_id: str) -> bool:
        """Trusted in-process policy query for HTTP relay audience validation."""
        if not self._ready or not isinstance(principal, str):
            return False
        try:
            chat = self._chat(chat_id)
        except BridgeError:
            return False
        return chat in self._allowed_chats.get(principal, frozenset())

    def resolve_principal(self, credential: str) -> str:
        """Expose the configured client identity resolver to the HTTP boundary."""
        if not self._ready:
            raise BridgeError("forbidden")
        return self._principal(credential)

    def register_file_handle(
        self, principal: str, chat_id: str, file_id: str, *, declared_size: int | None = None
    ) -> str:
        """Create an opaque handle from trusted update-ingress code for a received file."""
        if (
            not isinstance(principal, str)
            or not principal
            or not isinstance(file_id, str)
            or not _IDENTIFIER.fullmatch(file_id)
            or (declared_size is not None and type(declared_size) is not int)
            or (declared_size is not None and declared_size < 0)
        ):
            raise BridgeError("invalid_request")
        if declared_size is not None and declared_size > self._max_file_bytes:
            raise BridgeError("file_too_large")
        chat = self._chat(chat_id)
        if chat not in self._allowed_chats.get(principal, frozenset()):
            raise BridgeError("forbidden")
        if len(self._file_handles) >= MAX_FILE_HANDLES:
            raise BridgeError("invalid_request")
        handle = uuid4().hex
        self._file_handles[handle] = (principal, chat, file_id, declared_size)
        return handle

    def register_callback_handle(
        self, principal: str, chat_id: str, callback_id: str, callback_data: str
    ) -> str:
        """Register an ordinary callback only from trusted relay ingress code."""
        if not self._ready:
            raise BridgeError("forbidden")
        chat = self._chat(chat_id)
        if chat not in self._allowed_chats.get(principal, frozenset()):
            raise BridgeError("forbidden")
        if (
            not isinstance(callback_id, str)
            or not 1 <= len(callback_id) <= 256
            or not callback_id.isascii()
            or not isinstance(callback_data, str)
            or len(callback_data) > 64
            or callback_data.startswith("release:")
        ):
            raise BridgeError("forbidden")
        if len(self._callback_handles) >= 100_000:
            raise BridgeError("invalid_request")
        handle = uuid4().hex
        self._callback_handles[handle] = (principal, callback_id)
        return handle

    async def _call(self, method: str, parameters: Mapping[str, object]) -> object:
        try:
            return await self._transport.call(method, parameters, token=self._token)
        except Exception:
            raise BridgeError("upstream_error") from None

    def _principal(self, credential: str) -> str:
        if not isinstance(credential, str) or not credential or len(credential) > 4096:
            raise BridgeError("unauthorized")
        try:
            principal = self._authenticate(credential)
        except Exception:
            raise BridgeError("unauthorized") from None
        if not isinstance(principal, str) or not principal:
            raise BridgeError("unauthorized")
        return principal

    @staticmethod
    def _request(request: Mapping[str, object]) -> tuple[str, dict[str, object]]:
        try:
            encoded = json.dumps(request, separators=(",", ":"), ensure_ascii=True).encode()
        except (TypeError, ValueError, RecursionError):
            raise BridgeError("invalid_request") from None
        if not isinstance(request, Mapping):
            raise BridgeError("invalid_request")
        if set(request) != {"operation", "payload"}:
            raise BridgeError("invalid_request")
        operation, payload = request.get("operation"), request.get("payload")
        if not isinstance(operation, str) or not isinstance(payload, Mapping):
            raise BridgeError("invalid_request")
        request_limit = (
            MAX_FILE_REQUEST_BYTES if operation == "send_document" else MAX_REQUEST_BYTES
        )
        if len(encoded) > request_limit:
            raise BridgeError("invalid_request")
        if not operation or len(operation) > 64:
            raise BridgeError("invalid_request")
        return operation, dict(payload)

    @staticmethod
    def _exact(
        payload: Mapping[str, object], required: set[str], optional: set[str] | None = None
    ) -> None:
        optional = optional or set()
        if not required <= payload.keys() or payload.keys() - required - optional:
            raise BridgeError("invalid_request")

    @staticmethod
    def _chat(value: object) -> str:
        if not isinstance(value, str) or not _CHAT_ID.fullmatch(value):
            raise BridgeError("invalid_request")
        return value

    @staticmethod
    def _numeric_id(value: object) -> str:
        if not isinstance(value, str) or not value.isdecimal() or len(value) > 20:
            raise BridgeError("invalid_request")
        return value

    @classmethod
    def _message_id(cls, value: object) -> str:
        message_id = cls._numeric_id(value)
        if int(message_id) <= 0:
            raise BridgeError("invalid_request")
        return message_id

    @staticmethod
    def _text(value: object, *, maximum: int = MAX_TEXT_CHARS) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise BridgeError("invalid_request")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise BridgeError("invalid_request") from None
        return value

    def _require_owned(self, principal: str, chat: str, message: str) -> None:
        if not self._ownership.owns(principal, chat, message):
            raise BridgeError("forbidden")

    def _record_sent(self, principal: str, chat: str, result: object) -> dict[str, str]:
        if not isinstance(result, Mapping):
            raise BridgeError("upstream_error")
        raw_chat = result.get("chat")
        raw_chat_id = raw_chat.get("id") if isinstance(raw_chat, Mapping) else None
        raw_message_id = result.get("message_id")
        if str(raw_chat_id) != chat or type(raw_message_id) is not int or raw_message_id <= 0:
            raise BridgeError("upstream_error")
        message_id = str(raw_message_id)
        self._ownership.add(principal, chat, message_id)
        return {"chat_id": chat, "message_id": message_id}

    @classmethod
    def _thread_reply(cls, payload: Mapping[str, object]) -> dict[str, int]:
        result: dict[str, int] = {}
        if "thread_id" in payload:
            value = cls._message_id(payload["thread_id"])
            result["message_thread_id"] = int(value)
        if "reply_to" in payload:
            value = cls._message_id(payload["reply_to"])
            result["reply_parameters"] = {"message_id": int(value)}  # type: ignore[dict-item]
        return result

    @classmethod
    def _add_message_options(
        cls, params: dict[str, object], payload: Mapping[str, object], text: str
    ) -> None:
        params.update(cls._thread_reply(payload))
        if "entities" in payload:
            params["entities"] = cls._entities(payload["entities"], text)

    @classmethod
    def _entities(cls, value: object, text: str) -> list[dict[str, object]]:
        if not isinstance(value, list) or len(value) > 100:
            raise BridgeError("invalid_request")
        utf16_size = len(text.encode("utf-16-le")) // 2
        result = []
        for entity in value:
            if not isinstance(entity, Mapping):
                raise BridgeError("invalid_request")
            kind = entity.get("type")
            offset, length = entity.get("offset"), entity.get("length")
            expected = {"type", "offset", "length"}
            if kind == "text_link":
                expected.add("url")
            if kind == "pre" and "language" in entity:
                expected.add("language")
            if (
                kind not in _ENTITY_TYPES
                or set(entity) != expected
                or type(offset) is not int
                or type(length) is not int
                or offset < 0
                or length <= 0
                or offset + length > utf16_size
            ):
                raise BridgeError("invalid_request")
            copied: dict[str, object] = {"type": kind, "offset": offset, "length": length}
            if kind == "text_link":
                url = entity.get("url")
                try:
                    parsed = urlsplit(url) if isinstance(url, str) else None
                    valid_url = (
                        parsed
                        and parsed.scheme in {"http", "https", "tg", "mailto"}
                        and (parsed.hostname or parsed.scheme == "mailto")
                        and len(url) <= 2048
                    )
                except ValueError:
                    valid_url = False
                if not valid_url or any(char.isspace() or ord(char) < 32 for char in url):
                    raise BridgeError("invalid_request")
                copied["url"] = url
            if "language" in expected:
                language = entity.get("language")
                if (
                    not isinstance(language, str)
                    or len(language) > 64
                    or not re.fullmatch(r"[A-Za-z0-9_+-]*", language)
                ):
                    raise BridgeError("invalid_request")
                copied["language"] = language
            result.append(copied)
        return result

    @staticmethod
    def _validated_command_rows(value: object) -> tuple[tuple[str, str], ...]:
        if not isinstance(value, list) or len(value) > 100:
            raise BridgeError("invalid_request")
        commands: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, Mapping) or set(item) != {"command", "description"}:
                raise BridgeError("invalid_request")
            command, description = item["command"], item["description"]
            if (
                not isinstance(command, str)
                or not _COMMAND_NAME.fullmatch(command)
                or command in seen
                or not isinstance(description, str)
                or not 1 <= len(description) <= 256
                or any(ord(char) < 32 or ord(char) == 127 for char in description)
            ):
                raise BridgeError("invalid_request")
            try:
                description.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise BridgeError("invalid_request") from None
            seen.add(command)
            commands.append((command, description))
        return tuple(commands)

    @classmethod
    def _validated_command_pairs(cls, value: object) -> tuple[tuple[str, str], ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise BridgeError("invalid_request")
        rows = []
        for pair in value:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise BridgeError("invalid_request")
            rows.append({"command": pair[0], "description": pair[1]})
        return cls._validated_command_rows(rows)

    def __repr__(self) -> str:
        return "TelegramBridge(token=[REDACTED])"


class TelegramBridgeClient:
    """Token-free ordinary client contract for an authenticated bridge endpoint."""

    def __init__(self, transport: RequestTransport, *, credential: str) -> None:
        if not isinstance(credential, str) or not credential or len(credential) > 4096:
            raise BridgeError("unauthorized")
        self._transport = transport
        self._credential = credential

    async def request(self, operation: str, payload: Mapping[str, object]) -> object:
        if not isinstance(operation, str) or not isinstance(payload, Mapping):
            raise BridgeError("invalid_request")
        return await self._transport(
            self._credential, {"operation": operation, "payload": dict(payload)}
        )

    def __repr__(self) -> str:
        return "TelegramBridgeClient(credential=[REDACTED])"


__all__ = [
    "BotApiTransport",
    "BridgeError",
    "MessageOwnershipStore",
    "TelegramBridge",
    "TelegramBridgeClient",
]
