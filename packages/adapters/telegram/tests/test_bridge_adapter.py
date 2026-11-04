from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile
from collections.abc import Mapping
from uuid import uuid4

import httpx
import pytest
from chatcore.events import (
    Attachment,
    AttachmentKind,
    BotAddedToChat,
    ChatRef,
    IncomingMessage,
    MemberJoined,
    MessageRef,
    PollAnswer,
    UserRef,
)
from chatcore.files import FilePlatform, VoicePlatform
from chatcore.platform import ChatPlatform, PlatformError
from chatcore.polls import PollPlatform, PollResult
from chatcore_telegram.bridge import (
    BridgeAdapterError,
    BridgeTelegramPlatform,
    TelegramBridgeHTTPClient,
)


class FakeClient:
    def __init__(self) -> None:
        self.calls = []
        self.events = []
        self.poll_calls = 0
        self.acked = []

    async def request(self, operation: str, payload: Mapping[str, object]) -> object:
        self.calls.append((operation, dict(payload)))
        if getattr(self, "fail_menu", False) and operation == "set_commands":
            raise RuntimeError("menu capability denied")
        if operation == "get_me":
            return {"id": 123, "is_bot": True, "first_name": "bridgebot", "username": "bridge_bot"}
        if operation == "send_poll":
            return {"chat_id": str(payload["chat_id"]), "message_id": "18", "poll_id": "poll-1"}
        if operation == "stop_poll":
            return {"counts": [2, 1], "total_voters": 3}
        if operation == "send_document":
            return {"chat_id": str(payload["chat_id"]), "message_id": "19"}
        if operation == "download_file":
            return b"file-bytes"
        if operation in {
            "edit_message",
            "delete_message",
            "pin_message",
            "typing",
            "leave_chat",
            "answer_callback",
        }:
            return {"ok": True}
        if operation == "get_chat_member":
            return {"status": getattr(self, "member_status", "member")}
        return {"chat_id": str(payload.get("chat_id", "-100")), "message_id": "17"}

    async def poll_events(self, wait_ms: int = 0) -> list[object]:
        await asyncio.sleep(0)
        self.poll_calls += 1
        values = self.events.pop(0) if self.events else []
        for value in values:
            if "relay_id" not in value:
                value["relay_id"] = str(uuid4())
                value["update_id"] = value["update"]["update_id"]
        return values

    async def ack_events(self, relay_ids) -> None:
        self.acked.append(list(relay_ids))


@pytest.mark.asyncio
async def test_bridge_adapter_implements_platform_and_preserves_thread_reply_semantics() -> None:
    client = FakeClient()
    platform = BridgeTelegramPlatform(client)
    assert isinstance(platform, ChatPlatform)
    assert isinstance(platform, FilePlatform)
    assert isinstance(platform, PollPlatform)
    assert not isinstance(platform, VoicePlatform)
    chat = ChatRef("tg", "-100")
    reply = MessageRef(chat, "12", "3")
    sent = await platform.send(chat, "**hello**", reply_to=reply, thread_id="3")
    assert sent == MessageRef(chat, "17", "3")
    op, payload = client.calls[-1]
    assert op == "send_message"
    assert payload["reply_to"] == "12" and payload["thread_id"] == "3"
    assert payload["entities"]
    poll = await platform.send_poll(
        chat, "question", ["a", "b"], multiple=False, anonymous=True, thread_id="3"
    )
    assert poll.message.thread_id == "3"
    assert client.calls[-1][1]["thread_id"] == "3"
    assert await platform.stop_poll(poll.message) == PollResult((2, 1), 3)
    document = await platform.send_file(chat, "file.txt", b"hello", thread_id="3")
    assert document.message_id == "19"
    assert client.calls[-1][1]["thread_id"] == "3"


@pytest.mark.asyncio
async def test_bridge_adapter_operations_and_authenticated_relay_events() -> None:
    client = FakeClient()
    platform = BridgeTelegramPlatform(client)
    chat = ChatRef("tg", "-100")
    user_ref = UserRef("tg", "5")
    message = MessageRef(chat, "18", "3")
    await platform.confirm(chat, "Proceed?", "act_1", user_ref, reply_to=message, thread_id="3")
    await platform.edit(message, "updated")
    await platform.typing(chat, "3")
    assert await platform.is_member(chat, user_ref)
    assert not await platform.is_group_admin(chat, user_ref)
    client.member_status = "administrator"
    assert await platform.is_group_admin(chat, user_ref)
    await platform.pin(message)
    await platform.leave(chat)
    attachment = Attachment("opaque-file-handle", AttachmentKind.DOCUMENT, size_bytes=10)
    assert await platform.download(attachment, chat=chat, max_bytes=20) == b"file-bytes"
    assert [name for name, _ in client.calls] == [
        "confirm",
        "edit_message",
        "typing",
        "get_chat_member",
        "get_chat_member",
        "get_chat_member",
        "pin_message",
        "leave_chat",
        "download_file",
    ]

    delivered = []

    async def handler(event):
        delivered.append(event)

    client.events = [
        [
            {
                "update": {
                    "update_id": 3,
                    "message": {
                        "message_id": 21,
                        "chat": {"id": -100, "type": "group", "title": "room"},
                        "from": {"id": 5, "first_name": "A", "is_bot": False},
                        "text": "hello",
                        "date": 1,
                    },
                },
                "callback_handle": None,
            }
        ]
    ]
    task = asyncio.create_task(platform.start(handler))
    await asyncio.sleep(0.03)
    await platform.stop()
    await task
    assert len(delivered) == 1 and isinstance(delivered[0], IncomingMessage)
    assert len(client.acked) == 1 and len(client.acked[0]) == 1
    assert delivered[0].text == "hello"
    assert not hasattr(platform, "_application")


@pytest.mark.asyncio
async def test_bridge_download_retries_only_transient_codes(monkeypatch) -> None:
    client = FakeClient()
    platform = BridgeTelegramPlatform(client)
    original = client.request
    attempts = 0

    async def flaky(operation, payload):
        nonlocal attempts
        if operation == "download_file":
            attempts += 1
            if attempts < 3:
                raise BridgeAdapterError("upstream_error")
        return await original(operation, payload)

    client.request = flaky
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("chatcore_telegram.bridge.asyncio.sleep", sleep)
    monkeypatch.setattr("chatcore_telegram.bridge.random.uniform", lambda *_: 0)
    attachment = Attachment("f", AttachmentKind.DOCUMENT)
    assert (
        await platform.download(attachment, chat=ChatRef("tg", "-100"), max_bytes=20)
        == b"file-bytes"
    )
    assert attempts == 3 and sleeps == [0.5, 1.0]

    async def forbidden(_operation, _payload):
        nonlocal attempts
        attempts += 1
        raise BridgeAdapterError("forbidden")

    client.request = forbidden
    with pytest.raises(PlatformError, match="forbidden"):
        await platform.download(attachment, chat=ChatRef("tg", "-100"), max_bytes=20)
    assert attempts == 4


@pytest.mark.asyncio
async def test_relayed_callback_acknowledges_before_ordinary_action_delivery() -> None:
    client = FakeClient()
    platform = BridgeTelegramPlatform(client, poll_wait_ms=0)
    client.events = [
        [
            {
                "update": {
                    "update_id": 7,
                    "callback_query": {
                        "data": "c:approve_1",
                        "from": {"id": 5, "first_name": "A", "is_bot": False},
                        "message": {"message_id": 22, "chat": {"id": -100}},
                    },
                },
                "callback_handle": "a" * 32,
            }
        ]
    ]
    delivered = []

    async def handler(event):
        delivered.append((event, list(client.calls)))
        await platform.stop()

    await platform.start(handler)
    assert delivered[0][1][-1] == ("answer_callback", {"callback_handle": "a" * 32})
    assert client.acked and len(client.acked[0]) == 1
    assert delivered[0][0].action_id == "approve_1"
    assert delivered[0][0].choice == "confirm"


@pytest.mark.asyncio
async def test_relay_is_not_acknowledged_when_application_handler_fails() -> None:
    client = FakeClient()
    platform = BridgeTelegramPlatform(client, poll_wait_ms=0)
    client.events = [
        [
            {
                "update": {
                    "update_id": 70,
                    "message": {
                        "message_id": 22,
                        "chat": {"id": -100, "type": "group"},
                        "from": {"id": 5, "first_name": "A", "is_bot": False},
                        "text": "hello",
                    },
                }
            }
        ]
    ]

    async def handler(_event):
        raise ValueError("synthetic handler failure")

    with pytest.raises(ValueError, match="synthetic handler failure"):
        await platform.start(handler)
    assert client.acked == []


@pytest.mark.asyncio
async def test_bot_identity_drives_exact_reply_and_mention_semantics() -> None:
    client = FakeClient()
    platform = BridgeTelegramPlatform(client, poll_wait_ms=0)
    delivered = []

    async def handler(event):
        delivered.append(event)
        await platform.stop()

    client.events = [
        [
            {
                "update": {
                    "update_id": 8,
                    "message": {
                        "message_id": 23,
                        "chat": {"id": -100, "type": "group", "title": "room"},
                        "from": {"id": 5, "first_name": "A", "is_bot": False},
                        "text": "hello @bridge_bot",
                        "entities": [{"type": "mention", "offset": 6, "length": 11}],
                        "date": 1,
                        "reply_to_message": {
                            "message_id": 22,
                            "chat": {"id": -100},
                            "from": {"id": 123, "is_bot": True},
                        },
                    },
                },
                "callback_handle": None,
            }
        ]
    ]
    await platform.start(handler)
    assert delivered[0].mentions_bot is True
    assert delivered[0].reply_to_bot is True


@pytest.mark.asyncio
async def test_menu_commands_are_sanitized_and_sent_via_bridge_at_start() -> None:
    client = FakeClient()
    platform = BridgeTelegramPlatform(client, poll_wait_ms=0)
    platform.set_menu_commands(
        [("help", "Help"), ("BAD", "ignored"), ("status", "S" * 300)],
        [("jobs", "Jobs")],
    )
    client.events = [
        [
            {
                "update": {
                    "update_id": 9,
                    "message": {
                        "message_id": 24,
                        "chat": {"id": -100, "type": "group"},
                        "from": {"id": 5, "first_name": "A", "is_bot": False},
                        "text": "hello",
                    },
                },
                "callback_handle": None,
            }
        ]
    ]

    async def handler(_event):
        await platform.stop()

    await platform.start(handler)
    menu_calls = [payload for name, payload in client.calls if name == "set_commands"]
    assert menu_calls == [
        {
            "scope": "private",
            "commands": [
                {"command": "help", "description": "Help"},
                {"command": "status", "description": "S" * 256},
            ],
        },
        {"scope": "groups", "commands": [{"command": "jobs", "description": "Jobs"}]},
    ]


@pytest.mark.asyncio
async def test_missing_bridge_menu_capability_fails_start_without_bot_fallback() -> None:
    client = FakeClient()
    client.fail_menu = True
    platform = BridgeTelegramPlatform(client, poll_wait_ms=0)
    with pytest.raises(PlatformError):
        await platform.start(lambda _event: asyncio.sleep(0))
    assert client.poll_calls == 0
    assert not hasattr(platform, "_application")


@pytest.mark.asyncio
async def test_member_and_poll_events_are_mapped_from_authenticated_relay() -> None:
    client = FakeClient()
    platform = BridgeTelegramPlatform(client, poll_wait_ms=0)
    user = {"id": 5, "first_name": "A", "is_bot": False}
    bot = {"id": 123, "first_name": "bridgebot", "is_bot": True}
    client.events = [
        [
            {
                "update": {
                    "update_id": 40,
                    "chat_member": {
                        "chat": {"id": -100},
                        "old_chat_member": {"status": "left"},
                        "new_chat_member": {"status": "member", "user": user},
                    },
                },
                "callback_handle": None,
            },
            {
                "update": {
                    "update_id": 41,
                    "my_chat_member": {
                        "chat": {"id": -100},
                        "from": user,
                        "old_chat_member": {"status": "left"},
                        "new_chat_member": {"status": "member", "user": bot},
                    },
                },
                "callback_handle": None,
            },
            {
                "update": {
                    "update_id": 42,
                    "poll_answer": {"poll_id": "poll-1", "user": user, "option_ids": [0]},
                },
                "callback_handle": None,
            },
        ]
    ]
    delivered = []

    async def handler(event):
        delivered.append(event)
        if len(delivered) == 3:
            await platform.stop()

    await platform.start(handler)
    assert isinstance(delivered[0], MemberJoined)
    assert isinstance(delivered[1], BotAddedToChat)
    assert isinstance(delivered[2], PollAnswer)
    assert delivered[2].option_ids == (0,)


@pytest.mark.asyncio
async def test_adapter_http_client_uses_only_owner_private_uds_or_https() -> None:
    directory = tempfile.mkdtemp(prefix="uds-", dir="/tmp")
    os.chmod(directory, 0o700)
    try:
        socket_path = os.path.join(directory, "ordinary.sock")
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(socket_path)
        except PermissionError as exc:
            pytest.skip(f"Unix domain sockets are unavailable: {type(exc).__name__}")
        finally:
            server.close()
        os.chmod(socket_path, 0o600)
        client = TelegramBridgeHTTPClient(
            uds_path=socket_path,
            credential="ordinary-secret",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": True, "result": {}})
            ),
        )
        assert "ordinary-secret" not in repr(client)
        await client.close()
        os.chmod(socket_path, 0o666)
        with pytest.raises(BridgeAdapterError):
            TelegramBridgeHTTPClient(
                uds_path=socket_path,
                credential="ordinary-secret",
                transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            )
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("retries", [0, 3])
async def test_bridge_download_gives_up_after_configured_retries(monkeypatch, retries) -> None:
    client = FakeClient()
    platform = BridgeTelegramPlatform(client, download_retries=retries)
    attempts = 0

    async def failing(_operation, _payload):
        nonlocal attempts
        attempts += 1
        raise BridgeAdapterError("upstream_error")

    client.request = failing
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("chatcore_telegram.bridge.asyncio.sleep", sleep)
    monkeypatch.setattr("chatcore_telegram.bridge.random.uniform", lambda *_: 0)
    with pytest.raises(PlatformError, match="upstream_error"):
        await platform.download(
            Attachment("f", AttachmentKind.DOCUMENT), chat=ChatRef("tg", "-100"), max_bytes=20
        )
    assert attempts == 1 + retries
    assert sleeps == [0.5, 1.0, 2.0][:retries]


@pytest.mark.asyncio
async def test_bridge_download_backoff_cap_matches_direct(monkeypatch) -> None:
    client = FakeClient()
    platform = BridgeTelegramPlatform(client, download_retries=3, download_backoff_s=5)
    original = client.request
    attempts = 0

    async def flaky(operation, payload):
        nonlocal attempts
        if operation == "download_file":
            attempts += 1
            if attempts < 4:
                raise BridgeAdapterError("bridge_error")
        return await original(operation, payload)

    client.request = flaky
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("chatcore_telegram.bridge.asyncio.sleep", sleep)
    monkeypatch.setattr("chatcore_telegram.bridge.random.uniform", lambda *_: 0)
    assert (
        await platform.download(
            Attachment("f", AttachmentKind.DOCUMENT), chat=ChatRef("tg", "-100"), max_bytes=20
        )
        == b"file-bytes"
    )
    assert sleeps == [5, 3, 0]


def test_bridge_download_settings_are_bounded() -> None:
    with pytest.raises(ValueError):
        BridgeTelegramPlatform(FakeClient(), download_retries=6)
