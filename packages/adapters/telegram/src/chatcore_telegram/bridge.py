"""Token-free Telegram platform that communicates only with the protected bridge."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import re
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from chatcore.events import (
    Attachment,
    AttachmentKind,
    BotAddedToChat,
    ButtonPress,
    ChatRef,
    Event,
    IncomingMessage,
    MemberJoined,
    MemberLeft,
    MessageRef,
    PollAnswer,
    User,
    UserRef,
)
from chatcore.files import FilePlatform
from chatcore.images import error_label
from chatcore.platform import Capabilities, EventHandler, PlatformError
from chatcore.polls import PollLimits, PollPlatform, PollResult, SentPoll
from pydantic import BaseModel, Field

from chatcore_telegram.formatting import render_markdown, split_message, utf16_length
from chatcore_telegram.menu import fit_menu

NAME = "tg"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_POLL_WAIT_MS = 25_000
logger = logging.getLogger(__name__)
_COMMAND = re.compile(r"^[cx]:([A-Za-z0-9_-]{1,56})$")


class BridgeAdapterError(RuntimeError):
    """Fixed transport error with no credentials or HTTP diagnostics."""

    def __init__(self, code: str = "bridge_error") -> None:
        super().__init__(code if re.fullmatch(r"[a-z_]{1,64}", code) else "bridge_error")


class BridgeDownloadSettings(BaseModel):
    retries: int = Field(default=3, ge=0, le=5)
    backoff_s: float = Field(default=0.5, ge=0, le=5)


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
            raise BridgeAdapterError("invalid_configuration")
    except BridgeAdapterError:
        raise
    except OSError:
        raise BridgeAdapterError("invalid_configuration") from None


class BridgeClient(Protocol):
    async def request(self, operation: str, payload: Mapping[str, object]) -> object: ...

    async def poll_events(self, wait_ms: int = 0) -> list[object]: ...

    async def ack_events(self, relay_ids: Sequence[str]) -> None: ...


class TelegramBridgeHTTPClient:
    """Small ordinary-side client. This module has no Bot API or release-key imports."""

    def __init__(
        self,
        *,
        credential: str,
        origin: str | None = None,
        uds_path: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(credential, str) or not credential or len(credential) > 4096:
            raise BridgeAdapterError("invalid_configuration")
        if (origin is None) == (uds_path is None):
            raise BridgeAdapterError("invalid_configuration")
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
            except (TypeError, ValueError):
                raise BridgeAdapterError("invalid_configuration") from None
            base_url = origin.rstrip("/")
            client_transport = transport
        else:
            if not isinstance(uds_path, str) or not uds_path.startswith("/") or "\x00" in uds_path:
                raise BridgeAdapterError("invalid_configuration")
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
        try:
            response = await self._client.post(
                "/v1/request",
                json={"operation": operation, "payload": dict(payload)},
                headers=self._headers(),
            )
            if response.headers.get("content-type", "").startswith("application/octet-stream"):
                return response.content
            value = self._decode(response)
            if (
                not isinstance(value, dict)
                or value.get("ok") is not True
                or set(value) != {"ok", "result"}
            ):
                raise BridgeAdapterError("bridge_error")
            return value["result"]
        except BridgeAdapterError:
            raise
        except Exception:
            raise BridgeAdapterError("bridge_error") from None

    async def poll_events(self, wait_ms: int = 0) -> list[object]:
        if type(wait_ms) is not int or not 0 <= wait_ms <= MAX_POLL_WAIT_MS:
            raise BridgeAdapterError("invalid_request")
        try:
            response = await self._client.get(
                "/v1/events", params={"wait_ms": wait_ms}, headers=self._headers()
            )
            value = self._decode(response)
            events = value.get("events") if isinstance(value, dict) else None
            if not isinstance(events, list):
                raise BridgeAdapterError("bridge_error")
            return events
        except BridgeAdapterError:
            raise
        except Exception:
            raise BridgeAdapterError("bridge_error") from None

    async def ack_events(self, relay_ids: Sequence[str]) -> None:
        if (
            not isinstance(relay_ids, Sequence)
            or isinstance(relay_ids, (str, bytes))
            or not 1 <= len(relay_ids) <= 50
            or any(not isinstance(value, str) for value in relay_ids)
        ):
            raise BridgeAdapterError("invalid_request")
        try:
            response = await self._client.post(
                "/v1/events/ack",
                json={"relay_ids": list(relay_ids)},
                headers=self._headers(),
            )
            value = self._decode(response)
            if value != {"ok": True}:
                raise BridgeAdapterError("bridge_error")
        except BridgeAdapterError:
            raise
        except Exception:
            raise BridgeAdapterError("bridge_error") from None

    def _decode(self, response: httpx.Response) -> object:
        try:
            value = response.json()
        except Exception:
            raise BridgeAdapterError("bridge_error") from None
        if not response.is_success:
            code = value.get("error") if isinstance(value, dict) else None
            raise BridgeAdapterError(code if isinstance(code, str) else "bridge_error")
        return value

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._credential}"}

    async def close(self) -> None:
        await self._client.aclose()

    def __repr__(self) -> str:
        return "TelegramBridgeHTTPClient(credential=[REDACTED])"


class BridgeTelegramPlatform(FilePlatform, PollPlatform):
    """ChatPlatform implementation that talks only to the authenticated bridge."""

    name = NAME
    capabilities = Capabilities(
        buttons=True,
        edit=True,
        threads=True,
        files=True,
        dm=True,
        max_message_len=4096,
        markup="markdown",
        pin=True,
    )
    poll_limits = PollLimits(12, 300, 100)

    def __init__(
        self,
        client: BridgeClient,
        *,
        poll_wait_ms: int = MAX_POLL_WAIT_MS,
        download_retries: int = 3,
        download_backoff_s: float = 0.5,
    ) -> None:
        if (
            not callable(getattr(client, "request", None))
            or not callable(getattr(client, "poll_events", None))
            or not callable(getattr(client, "ack_events", None))
        ):
            raise BridgeAdapterError("invalid_configuration")
        if type(poll_wait_ms) is not int or not 0 <= poll_wait_ms <= MAX_POLL_WAIT_MS:
            raise BridgeAdapterError("invalid_configuration")
        self._client = client
        self._download_settings = BridgeDownloadSettings(
            retries=download_retries, backoff_s=download_backoff_s
        )
        self._poll_wait_ms = poll_wait_ms
        self._handler: EventHandler | None = None
        self._stopped = asyncio.Event()
        self._sent_parts: dict[tuple[str, str], tuple[str, ...]] = {}
        self._bot_id: int | None = None
        self._bot_username: str | None = None
        self._private_commands: list[dict[str, str]] = []
        self._group_commands: list[dict[str, str]] = []

    def set_menu_commands(
        self,
        private: Sequence[tuple[str, str]],
        group: Sequence[tuple[str, str]],
    ) -> None:
        self._private_commands = fit_menu(self._valid_commands(private), scope="private")
        self._group_commands = fit_menu(self._valid_commands(group), scope="group")

    async def start(self, handler: EventHandler) -> None:
        self._handler = handler
        self._stopped.clear()
        identity = await self._request("get_me", {})
        if (
            not isinstance(identity, Mapping)
            or type(identity.get("id")) is not int
            or identity["id"] <= 0
            or identity.get("is_bot") is not True
            or not isinstance(identity.get("first_name"), str)
        ):
            raise PlatformError("BridgeResponse")
        self._bot_id = identity["id"]
        self._bot_username = (
            identity.get("username") if isinstance(identity.get("username"), str) else None
        )
        for scope, commands in (
            ("private", self._private_commands),
            ("groups", self._group_commands),
        ):
            await self._request("set_commands", {"scope": scope, "commands": commands})
        while not self._stopped.is_set():
            try:
                events = await self._client.poll_events(self._poll_wait_ms)
            except Exception as exc:
                if self._stopped.is_set():
                    return
                raise PlatformError(type(exc).__name__) from None
            for item in events:
                if self._stopped.is_set():
                    return
                if isinstance(item, Mapping):
                    relay_id = item.get("relay_id")
                    update_id = item.get("update_id")
                    update = item.get("update")
                    if (
                        not isinstance(relay_id, str)
                        or type(update_id) is not int
                        or not isinstance(update, Mapping)
                        or update.get("update_id") != update_id
                    ):
                        raise PlatformError("BridgeResponse")
                    try:
                        UUID(relay_id)
                    except ValueError:
                        raise PlatformError("BridgeResponse") from None
                    await self._deliver(item)
                    await self._client.ack_events([relay_id])

    async def stop(self) -> None:
        self._stopped.set()

    async def send(
        self,
        chat: ChatRef,
        text: str,
        *,
        reply_to: MessageRef | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        rendered = render_markdown(text)
        chunks = split_message(rendered, self.capabilities.max_message_len)
        sent: list[str] = []
        for index, chunk in enumerate(chunks):
            payload: dict[str, object] = {
                "chat_id": chat.id,
                "text": chunk.text,
                "entities": [self._entity(entity) for entity in chunk.entities],
            }
            if index == 0 and reply_to is not None:
                payload["reply_to"] = reply_to.message_id
            if thread_id is not None:
                payload["thread_id"] = thread_id
            result = await self._request("send_message", payload)
            sent.append(self._message_id(result, chat))
        ref = MessageRef(chat, sent[-1], thread_id)
        self._sent_parts[(chat.key, ref.message_id)] = tuple(sent)
        while len(self._sent_parts) > 1000:
            self._sent_parts.pop(next(iter(self._sent_parts)))
        return ref

    def sent_message_ids(self, ref: MessageRef) -> tuple[str, ...]:
        return self._sent_parts.get((ref.chat.key, ref.message_id), (ref.message_id,))

    async def send_poll(
        self,
        chat: ChatRef,
        question: str,
        options: Sequence[str],
        *,
        multiple: bool,
        anonymous: bool,
        thread_id: str | None = None,
    ) -> SentPoll:
        payload: dict[str, object] = {
            "chat_id": chat.id,
            "question": question,
            "options": list(options),
            "multiple": multiple,
            "anonymous": anonymous,
        }
        if thread_id is not None:
            payload["thread_id"] = thread_id
        result = await self._request("send_poll", payload)
        message = MessageRef(chat, self._message_id(result, chat), thread_id)
        poll_id = result.get("poll_id") if isinstance(result, Mapping) else None
        if not isinstance(poll_id, str) or not poll_id:
            raise PlatformError("BridgeResponse")
        return SentPoll(message, poll_id)

    async def send_file(
        self,
        chat: ChatRef,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
            raise PlatformError("FileTooLarge")
        payload: dict[str, object] = {
            "chat_id": chat.id,
            "filename": filename,
            "data_base64": base64.b64encode(data).decode("ascii"),
        }
        if caption is not None:
            payload["caption"] = caption
        if thread_id is not None:
            payload["thread_id"] = thread_id
        result = await self._request("send_document", payload)
        return MessageRef(chat, self._message_id(result, chat), thread_id)

    async def stop_poll(self, message: MessageRef) -> PollResult:
        result = await self._request(
            "stop_poll", {"chat_id": message.chat.id, "message_id": message.message_id}
        )
        if not isinstance(result, Mapping):
            raise PlatformError("BridgeResponse")
        counts, total = result.get("counts"), result.get("total_voters")
        if not isinstance(counts, list) or type(total) is not int:
            raise PlatformError("BridgeResponse")
        return PollResult(tuple(counts), total)

    async def edit(self, message: MessageRef, text: str) -> None:
        rendered = render_markdown(text)
        if utf16_length(rendered.text) > self.capabilities.max_message_len:
            raise ValueError("edited message exceeds Telegram's message limit")
        await self._request(
            "edit_message",
            {
                "chat_id": message.chat.id,
                "message_id": message.message_id,
                "text": rendered.text,
                "entities": [self._entity(entity) for entity in rendered.entities],
            },
        )

    async def typing(self, chat: ChatRef, thread_id: str | None = None) -> None:
        payload: dict[str, object] = {"chat_id": chat.id}
        if thread_id is not None:
            payload["thread_id"] = thread_id
        await self._request("typing", payload)

    async def confirm(
        self,
        chat: ChatRef,
        prompt: str,
        action_id: str,
        requester: UserRef,
        *,
        reply_to: MessageRef | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        if not prompt:
            raise ValueError("confirmation prompt cannot be empty")
        payload: dict[str, object] = {"chat_id": chat.id, "prompt": prompt, "action_id": action_id}
        if reply_to is not None:
            payload["reply_to"] = reply_to.message_id
        if thread_id is not None:
            payload["thread_id"] = thread_id
        result = await self._request("confirm", payload)
        return MessageRef(chat, self._message_id(result, chat), thread_id)

    async def download(self, attachment: Attachment, *, chat: ChatRef, max_bytes: int) -> bytes:
        limit = min(max(max_bytes, 0), MAX_FILE_BYTES)
        if attachment.size_bytes is not None and attachment.size_bytes > limit:
            raise PlatformError("FileTooLarge")
        payload = {"chat_id": chat.id, "file_handle": attachment.id, "max_bytes": limit}
        slept = 0.0
        for attempt in range(self._download_settings.retries + 1):
            try:
                result = await self._client.request("download_file", payload)
                break
            except BridgeAdapterError as exc:
                code = str(exc)
                if attempt < self._download_settings.retries and code in {
                    "upstream_error",
                    "bridge_error",
                }:
                    delay = self._download_settings.backoff_s * (2**attempt)
                    delay += random.uniform(0, delay * 0.1)  # noqa: S311 - non-security jitter
                    delay = min(delay, max(0.0, 8.0 - slept))
                    logger.warning(
                        "bridge download retry attempt=%s error=%s",
                        attempt + 1,
                        error_label(exc),
                    )
                    await asyncio.sleep(delay)
                    slept += delay
                    continue
                safe_code = code if re.fullmatch(r"[a-z_]{1,40}", code) else "BridgeAdapterError"
                raise PlatformError(safe_code) from None
            except Exception:
                raise PlatformError("BridgeAdapterError") from None
        if not isinstance(result, bytes) or len(result) > limit:
            raise PlatformError("FileTooLarge" if isinstance(result, bytes) else "BridgeResponse")
        return result

    async def is_member(self, chat: ChatRef, user: UserRef) -> bool:
        result = await self._request("get_chat_member", {"chat_id": chat.id, "user_id": user.id})
        if not isinstance(result, Mapping):
            raise PlatformError("BridgeResponse")
        status = result.get("status")
        if status == "restricted":
            return result.get("is_member") is True
        return status in {"creator", "administrator", "member"}

    async def is_group_admin(self, chat: ChatRef, user: UserRef) -> bool:
        """Check current membership through the bridge, without a local role cache."""
        if chat.platform != NAME or user.platform != NAME:
            return False
        result = await self._request("get_chat_member", {"chat_id": chat.id, "user_id": user.id})
        if not isinstance(result, Mapping):
            raise PlatformError("BridgeResponse")
        return result.get("status") in {"creator", "administrator"}

    async def leave(self, chat: ChatRef) -> None:
        await self._request("leave_chat", {"chat_id": chat.id})

    async def pin(self, message: MessageRef) -> None:
        await self._request(
            "pin_message", {"chat_id": message.chat.id, "message_id": message.message_id}
        )

    async def _request(self, operation: str, payload: Mapping[str, object]) -> object:
        try:
            return await self._client.request(operation, payload)
        except Exception as exc:
            raise PlatformError(type(exc).__name__) from None

    @staticmethod
    def _message_id(result: object, chat: ChatRef) -> str:
        if not isinstance(result, Mapping):
            raise PlatformError("BridgeResponse")
        message_id = result.get("message_id")
        chat_id = result.get("chat_id")
        if (
            not isinstance(message_id, str)
            or not message_id.isdecimal()
            or int(message_id) <= 0
            or str(chat_id) != chat.id
        ):
            raise PlatformError("BridgeResponse")
        return message_id

    @staticmethod
    def _valid_commands(items: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            return []
        commands = []
        for item in items:
            if (
                isinstance(item, (tuple, list))
                and len(item) == 2
                and isinstance(item[0], str)
                and re.fullmatch(r"[a-z0-9_]{1,32}", item[0])
                and isinstance(item[1], str)
            ):
                commands.append({"command": item[0], "description": item[1][:256]})
        return commands

    @staticmethod
    def _entity(entity: object) -> dict[str, object]:
        result: dict[str, object] = {
            "type": entity.type,
            "offset": entity.offset,
            "length": entity.length,
        }
        if entity.url is not None:
            result["url"] = entity.url
        if entity.language is not None:
            result["language"] = entity.language
        return result

    async def _deliver(self, envelope: Mapping[str, object]) -> None:
        update = envelope.get("update")
        if not isinstance(update, Mapping) or type(update.get("update_id")) is not int:
            return
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            await self._deliver_callback(callback, envelope.get("callback_handle"))
            return
        if isinstance(update.get("poll_answer"), Mapping):
            await self._deliver_poll_answer(update["poll_answer"])
            return
        message = update.get("message")
        if isinstance(message, Mapping):
            await self._deliver_message(message)
            return
        member_update = update.get("chat_member")
        if isinstance(member_update, Mapping):
            await self._deliver_member(member_update)
            return
        bot_update = update.get("my_chat_member")
        if isinstance(bot_update, Mapping):
            await self._deliver_bot_membership(bot_update, update["update_id"])

    async def _emit(self, event: Event) -> None:
        if self._handler is not None:
            await self._handler(event)

    async def _deliver_callback(self, callback: Mapping[str, object], handle: object) -> None:
        data = callback.get("data")
        if isinstance(data, str) and data.startswith("release:"):
            return
        match = _COMMAND.fullmatch(data) if isinstance(data, str) else None
        if isinstance(handle, str) and handle:
            await self._request("answer_callback", {"callback_handle": handle})
        message = callback.get("message")
        sender = callback.get("from")
        if not isinstance(sender, Mapping) or not isinstance(message, Mapping):
            return
        if match is None:
            return
        ref = self._message_ref(message)
        await self._emit(
            ButtonPress(
                chat=ref.chat,
                user=self._user(sender),
                action_id=match.group(1),
                choice="confirm" if data.startswith("c:") else "cancel",
                message=ref,
            )
        )

    async def _deliver_poll_answer(self, answer: Mapping[str, object]) -> None:
        sender = answer.get("user")
        poll_id, options = answer.get("poll_id"), answer.get("option_ids")
        if (
            isinstance(sender, Mapping)
            and isinstance(poll_id, str)
            and isinstance(options, list)
            and all(type(item) is int and item >= 0 for item in options)
        ):
            await self._emit(PollAnswer(poll_id, self._user(sender), tuple(options)))

    async def _deliver_message(self, message: Mapping[str, object]) -> None:
        if not isinstance(message.get("chat"), Mapping):
            return
        chat_obj = message["chat"]
        chat = ChatRef(NAME, str(chat_obj.get("id")))
        for new_member in (
            message.get("new_chat_members", [])
            if isinstance(message.get("new_chat_members"), list)
            else []
        ):
            if isinstance(new_member, Mapping):
                await self._emit(MemberJoined(chat, self._user(new_member)))
        left = message.get("left_chat_member")
        if isinstance(left, Mapping):
            await self._emit(MemberLeft(chat, self._user(left)))
        if not self._has_content(message) or not isinstance(message.get("from"), Mapping):
            return
        sender = self._user(message["from"])
        content = message.get("text") or message.get("caption") or ""
        if not isinstance(content, str):
            return
        thread = (
            str(message.get("message_thread_id"))
            if message.get("is_topic_message") and message.get("message_thread_id") is not None
            else None
        )
        reply = message.get("reply_to_message")
        if isinstance(reply, Mapping) and isinstance(reply.get("forum_topic_created"), Mapping):
            reply = None
        reply_ref = self._message_ref(reply) if isinstance(reply, Mapping) else None
        sent_at = message.get("date")
        try:
            when = (
                datetime.fromtimestamp(sent_at, UTC) if type(sent_at) is int else datetime.now(UTC)
            )
        except (OSError, OverflowError, ValueError):
            when = datetime.now(UTC)
        chat_type = chat_obj.get("type")
        event = IncomingMessage(
            chat=chat,
            message_id=str(message.get("message_id")),
            user=sender,
            text=content,
            is_dm=chat_type == "private",
            thread_id=thread,
            attachments=tuple(self._attachments(message)),
            reply_to=reply_ref,
            reply_to_bot=bool(
                isinstance(reply, Mapping)
                and isinstance(reply.get("from"), Mapping)
                and reply["from"].get("id") == self._bot_id
            ),
            mentions_bot=self._mentions(message, content),
            sent_at=when,
            chat_title=chat_obj.get("title")
            if chat_type in {"group", "supergroup"} and isinstance(chat_obj.get("title"), str)
            else None,
        )
        await self._emit(event)

    @staticmethod
    def _has_content(message: Mapping[str, object]) -> bool:
        return bool(
            message.get("text")
            or message.get("caption")
            or message.get("photo")
            or message.get("document")
            or message.get("audio")
            or message.get("video")
            or message.get("voice")
        )

    def _mentions(self, message: Mapping[str, object], content: str) -> bool:
        found = [
            *(message.get("entities", []) if isinstance(message.get("entities"), list) else []),
            *(
                message.get("caption_entities", [])
                if isinstance(message.get("caption_entities"), list)
                else []
            ),
        ]
        for entity in found:
            if not isinstance(entity, Mapping):
                continue
            if (
                entity.get("type") == "mention"
                and self._bot_username
                and type(entity.get("offset")) is int
                and type(entity.get("length")) is int
            ):
                try:
                    encoded = content.encode("utf-16-le")
                    mention = encoded[
                        entity["offset"] * 2 : (entity["offset"] + entity["length"]) * 2
                    ].decode("utf-16-le")
                except (UnicodeError, IndexError):
                    continue
                if mention.casefold() == f"@{self._bot_username}".casefold():
                    return True
            if (
                entity.get("type") == "text_mention"
                and isinstance(entity.get("user"), Mapping)
                and entity["user"].get("id") == self._bot_id
            ):
                return True
        return False

    @staticmethod
    def _attachments(message: Mapping[str, object]) -> list[Attachment]:
        result: list[Attachment] = []
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            photo = max(
                (item for item in photos if isinstance(item, Mapping)),
                key=lambda item: (
                    item.get("file_size") or 0,
                    (item.get("width") or 0) * (item.get("height") or 0),
                ),
                default=None,
            )
            if photo is not None and isinstance(photo.get("file_id"), str):
                result.append(
                    Attachment(
                        photo["file_id"],
                        AttachmentKind.PHOTO,
                        size_bytes=photo.get("file_size")
                        if type(photo.get("file_size")) is int
                        else None,
                    )
                )
        for field, kind in (
            ("document", AttachmentKind.DOCUMENT),
            ("audio", AttachmentKind.AUDIO),
            ("video", AttachmentKind.VIDEO),
            ("voice", AttachmentKind.AUDIO),
        ):
            item = message.get(field)
            if isinstance(item, Mapping) and isinstance(item.get("file_id"), str):
                result.append(
                    Attachment(
                        item["file_id"],
                        kind,
                        filename=item.get("file_name")
                        if isinstance(item.get("file_name"), str)
                        else None,
                        mime_type=item.get("mime_type")
                        if isinstance(item.get("mime_type"), str)
                        else None,
                        size_bytes=item.get("file_size")
                        if type(item.get("file_size")) is int
                        else None,
                    )
                )
        return result

    async def _deliver_member(self, update: Mapping[str, object]) -> None:
        chat, old, new = (
            update.get("chat"),
            update.get("old_chat_member"),
            update.get("new_chat_member"),
        )
        if not all(isinstance(item, Mapping) for item in (chat, old, new)):
            return
        old_member = self._member_status(old.get("status"), old.get("is_member"))
        new_member = self._member_status(new.get("status"), new.get("is_member"))
        user = new.get("user")
        if not isinstance(user, Mapping):
            return
        ref = ChatRef(NAME, str(chat.get("id")))
        if not old_member and new_member:
            await self._emit(MemberJoined(ref, self._user(user)))
        elif old_member and not new_member:
            await self._emit(MemberLeft(ref, self._user(user)))

    async def _deliver_bot_membership(self, update: Mapping[str, object], update_id: int) -> None:
        chat, old, new, actor = (
            update.get(key) for key in ("chat", "old_chat_member", "new_chat_member", "from")
        )
        if not all(isinstance(item, Mapping) for item in (chat, old, new)):
            return
        new_user = new.get("user")
        if (
            isinstance(new_user, Mapping)
            and chat.get("type") in {"group", "supergroup"}
            and new_user.get("id") == self._bot_id
            and not self._member_status(old.get("status"), old.get("is_member"))
            and self._member_status(new.get("status"), new.get("is_member"))
        ):
            await self._emit(
                BotAddedToChat(
                    ChatRef(NAME, str(chat.get("id"))),
                    self._user(actor) if isinstance(actor, Mapping) else None,
                    f"{self._bot_id}:{update_id}",
                )
            )

    @staticmethod
    def _member_status(status: object, restricted_member: object) -> bool:
        return status in {"creator", "administrator", "member"} or (
            status == "restricted" and restricted_member is True
        )

    @staticmethod
    def _user(value: Mapping[str, object]) -> User:
        first = value.get("first_name") if isinstance(value.get("first_name"), str) else ""
        last = value.get("last_name") if isinstance(value.get("last_name"), str) else ""
        return User(
            UserRef(NAME, str(value.get("id"))),
            " ".join(part for part in (first, last) if part),
            username=value.get("username") if isinstance(value.get("username"), str) else None,
            is_bot=value.get("is_bot") is True,
        )

    @staticmethod
    def _message_ref(value: Mapping[str, object]) -> MessageRef:
        chat_obj = value.get("chat") if isinstance(value.get("chat"), Mapping) else {}
        thread = (
            str(value.get("message_thread_id"))
            if value.get("is_topic_message") and value.get("message_thread_id") is not None
            else None
        )
        return MessageRef(
            ChatRef(NAME, str(chat_obj.get("id"))), str(value.get("message_id")), thread
        )


__all__ = ["BridgeAdapterError", "BridgeTelegramPlatform", "TelegramBridgeHTTPClient"]
