from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import socket
import tempfile
from collections.abc import Mapping
from uuid import UUID, uuid4

import httpx
import pytest
from chatcore_release_control.bridge_http import (
    BridgeHTTPError,
    TelegramBridgeASGI,
    TelegramBridgeHTTPClient,
)
from chatcore_release_control.durable_relay import RelayError, RelayEvent
from chatcore_release_control.telegram_bridge import TelegramBridge


class FakeBotApi:
    async def call(self, method: str, parameters: Mapping[str, object], *, token: str) -> object:
        if method == "answerCallbackQuery":
            return {"ok": True}
        return {"message_id": 41, "chat": {"id": parameters["chat_id"]}}

    async def download_file(self, file_id: str, max_bytes: int, *, token: str) -> bytes:
        return b"bytes-only"


class FakeRelayStore:
    def __init__(self, maximum: int = 256) -> None:
        self.maximum = maximum
        self.items: dict[str, list[RelayEvent]] = {}
        self.seen: dict[tuple[str, int], tuple[UUID, str]] = {}
        self.acknowledged: list[tuple[str, UUID]] = []

    async def enqueue(self, principal: str, update_id: int, payload: Mapping[str, object]) -> UUID:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        key = (principal, update_id)
        if key in self.seen:
            relay_id, old = self.seen[key]
            if old != canonical:
                raise RelayError("replay_mismatch")
            return relay_id
        if len(self.items.get(principal, [])) >= self.maximum:
            raise RelayError("relay_full")
        relay_id = uuid4()
        self.seen[key] = (relay_id, canonical)
        self.items.setdefault(principal, []).append(
            RelayEvent(relay_id, update_id, dict(payload), 1)
        )
        return relay_id

    async def claim(self, principal: str, *, limit: int = 50) -> list[RelayEvent]:
        values = self.items.get(principal, [])[:limit]
        self.items[principal] = self.items.get(principal, [])[limit:]
        return values

    async def ack(self, principal: str, relay_ids: list[UUID]) -> None:
        self.acknowledged.extend((principal, relay_id) for relay_id in relay_ids)


async def claimed_events(
    app: TelegramBridgeASGI, principal: str = "bot"
) -> list[dict[str, object]]:
    return [app._materialize(principal, event) for event in await app._relay.claim(principal)]


def make_bridge() -> TelegramBridge:
    return TelegramBridge(
        token="broker-token-never-returned",
        authenticate=lambda value: "bot" if value == "client-secret" else None,
        allowed_chats={"bot": frozenset({"-100"}), "other": frozenset({"7"})},
        transport=FakeBotApi(),
        load_protected_messages=lambda: (),
    )


async def asgi_request(app, *, method: str, path: str, body: bytes = b"", headers=()):
    sent = []
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    normalized_headers = list(headers)
    if (
        method == "POST"
        and path == "/v1/request"
        and not any(name.lower() == b"content-type" for name, _ in normalized_headers)
    ):
        normalized_headers.append((b"content-type", b"application/json"))
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "scheme": "https",
            "method": method,
            "path": path,
            "headers": normalized_headers,
            "query_string": b"",
        },
        receive,
        send,
    )
    start = next(message for message in sent if message["type"] == "http.response.start")
    data = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    return start, data


@pytest.mark.asyncio
async def test_asgi_dispatch_authenticates_and_returns_fixed_json() -> None:
    app = TelegramBridgeASGI(
        make_bridge(),
        relay_store=FakeRelayStore(),
        relay_key=b"r" * 32,
        audiences={"bot": frozenset({"-100"})},
    )
    body = json.dumps(
        {"operation": "send_message", "payload": {"chat_id": "-100", "text": "hello"}}
    ).encode()
    start, response = await asgi_request(
        app,
        method="POST",
        path="/v1/request",
        body=body,
        headers=[
            (b"authorization", b"Bearer client-secret"),
            (b"content-type", b"application/json"),
        ],
    )
    assert start["status"] == 200
    assert json.loads(response) == {"ok": True, "result": {"chat_id": "-100", "message_id": "41"}}

    denied, response = await asgi_request(app, method="POST", path="/v1/request", body=body)
    assert denied["status"] == 401
    assert json.loads(response) == {"ok": False, "error": "unauthorized"}
    assert b"broker-token" not in response


@pytest.mark.asyncio
async def test_asgi_rejects_duplicate_fields_and_untrusted_relay_push_route() -> None:
    app = TelegramBridgeASGI(
        make_bridge(),
        relay_store=FakeRelayStore(),
        relay_key=b"r" * 32,
        audiences={"bot": frozenset({"-100"})},
    )
    start, response = await asgi_request(
        app,
        method="POST",
        path="/v1/request",
        body=b'{"operation":"send_message","operation":"leave_chat","payload":{}}',
        headers=[(b"authorization", b"Bearer client-secret")],
    )
    assert start["status"] == 400
    assert json.loads(response) == {"ok": False, "error": "invalid_request"}
    missing, _ = await asgi_request(
        app, method="POST", path="/v1/relay", headers=[(b"authorization", b"Bearer client-secret")]
    )
    assert missing["status"] == 404


@pytest.mark.asyncio
async def test_only_verified_internal_relay_events_are_pollable_by_audience() -> None:
    app = TelegramBridgeASGI(
        make_bridge(),
        relay_store=FakeRelayStore(),
        relay_key=b"r" * 32,
        audiences={"bot": frozenset({"-100"})},
    )
    raw = json.dumps(
        {"update_id": 9, "message": {"chat": {"id": -100}}}, separators=(",", ":")
    ).encode()
    signature = hmac.new(b"r" * 32, raw, hashlib.sha256).hexdigest()
    await app.accept_relay(raw, signature)
    start, response = await asgi_request(
        app, method="GET", path="/v1/events", headers=[(b"authorization", b"Bearer client-secret")]
    )
    assert start["status"] == 200
    payload = json.loads(response)
    assert len(payload["events"]) == 1
    assert payload["events"][0]["update"]["update_id"] == 9
    with pytest.raises(BridgeHTTPError):
        await app.accept_relay(raw, "0" * 64)


@pytest.mark.asyncio
async def test_relay_ack_endpoint_is_authenticated_and_uses_stable_ids() -> None:
    store = FakeRelayStore()
    app = TelegramBridgeASGI(
        make_bridge(),
        relay_store=store,
        relay_key=b"r" * 32,
        audiences={"bot": frozenset({"-100"})},
    )
    raw = json.dumps({"update_id": 19, "message": {"chat": {"id": -100}}}).encode()
    signature = hmac.new(b"r" * 32, raw, hashlib.sha256).hexdigest()
    await app.accept_relay(raw, signature)
    events = await claimed_events(app)
    relay_id = events[0]["relay_id"]
    start, response = await asgi_request(
        app,
        method="POST",
        path="/v1/events/ack",
        body=json.dumps({"relay_ids": [relay_id]}).encode(),
        headers=[
            (b"authorization", b"Bearer client-secret"),
            (b"content-type", b"application/json"),
        ],
    )
    assert start["status"] == 200 and json.loads(response) == {"ok": True}
    assert store.acknowledged == [("bot", UUID(relay_id))]


@pytest.mark.asyncio
async def test_relay_replaces_file_ids_with_opaque_handles_and_callback_ids_with_ack_handles() -> (
    None
):
    bridge = make_bridge()
    app = TelegramBridgeASGI(
        bridge,
        relay_store=FakeRelayStore(),
        relay_key=b"r" * 32,
        audiences={"bot": frozenset({"-100"})},
    )
    update = {
        "update_id": 10,
        "message": {
            "message_id": 3,
            "chat": {"id": -100},
            "from": {"id": 5, "first_name": "A", "is_bot": False},
            "document": {"file_id": "private-file-id", "file_size": 3},
        },
    }
    raw = json.dumps(update, separators=(",", ":")).encode()
    signature = hmac.new(b"r" * 32, raw, hashlib.sha256).hexdigest()
    await app.accept_relay(raw, signature)
    response = await claimed_events(app, "bot")
    handle = response[0]["update"]["message"]["document"]["file_id"]
    assert handle != "private-file-id"
    assert (
        await bridge.handle(
            "client-secret",
            {
                "operation": "download_file",
                "payload": {"chat_id": "-100", "file_handle": handle, "max_bytes": 20},
            },
        )
        == b"bytes-only"
    )

    callback = {
        "update_id": 11,
        "callback_query": {
            "id": "telegram-query-id",
            "from": {"id": 5, "first_name": "A", "is_bot": False},
            "data": "c:ordinary",
            "message": {"message_id": 4, "chat": {"id": -100}},
        },
    }
    raw = json.dumps(callback, separators=(",", ":")).encode()
    signature = hmac.new(b"r" * 32, raw, hashlib.sha256).hexdigest()
    await app.accept_relay(raw, signature)
    relayed = (await claimed_events(app, "bot"))[0]
    assert "id" not in relayed["update"]["callback_query"]
    assert isinstance(relayed["callback_handle"], str)
    assert await bridge.handle(
        "client-secret",
        {
            "operation": "answer_callback",
            "payload": {"callback_handle": relayed["callback_handle"]},
        },
    ) == {"ok": True}

    callback["update_id"] = 12
    callback["callback_query"]["data"] = "release:a:" + "x" * 32
    raw = json.dumps(callback, separators=(",", ":")).encode()
    signature = hmac.new(b"r" * 32, raw, hashlib.sha256).hexdigest()
    with pytest.raises(BridgeHTTPError):
        await app.accept_relay(raw, signature)


def test_http_client_requires_tls_origin_or_explicit_uds() -> None:
    with pytest.raises(BridgeHTTPError):
        TelegramBridgeHTTPClient(credential="client-secret")
    with pytest.raises(BridgeHTTPError):
        TelegramBridgeHTTPClient(origin="http://bridge.example", credential="client-secret")
    with pytest.raises(BridgeHTTPError):
        TelegramBridgeHTTPClient(
            origin="https://user:pass@bridge.example", credential="client-secret"
        )
    client = TelegramBridgeHTTPClient(
        origin="https://bridge.example",
        credential="client-secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True, "result": {"ok": True}})
        ),
    )
    assert "client-secret" not in repr(client)
    # The mock transport means this check never opens a network connection.


def test_asgi_rejects_relay_audiences_outside_bridge_chat_allowlist() -> None:
    with pytest.raises(BridgeHTTPError):
        TelegramBridgeASGI(
            make_bridge(),
            relay_store=FakeRelayStore(),
            relay_key=b"r" * 32,
            audiences={"bot": frozenset({"7"})},
        )


@pytest.mark.asyncio
async def test_uds_client_requires_an_owner_only_socket_in_a_private_directory() -> None:
    directory = tempfile.mkdtemp(prefix="uds-", dir="/tmp")
    os.chmod(directory, 0o700)
    try:
        socket_path = os.path.join(directory, "bridge.sock")
        server = socket.socket(socket.AF_UNIX)
        server.bind(socket_path)
        server.close()
        os.chmod(socket_path, 0o600)
        client = TelegramBridgeHTTPClient(
            uds_path=socket_path,
            credential="client-secret",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": True, "result": {}})
            ),
        )
        assert "client-secret" not in repr(client)
        await client.close()
        os.chmod(socket_path, 0o666)
        with pytest.raises(BridgeHTTPError):
            TelegramBridgeHTTPClient(
                uds_path=socket_path,
                credential="client-secret",
                transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            )
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.asyncio
async def test_http_client_sends_only_bridge_request_and_supports_byte_responses() -> None:
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/v1/request" and b'"download_file"' in request.content:
            return httpx.Response(
                200, content=b"bytes-only", headers={"content-type": "application/octet-stream"}
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": "1"}})

    client = TelegramBridgeHTTPClient(
        origin="https://bridge.example",
        credential="client-secret",
        transport=httpx.MockTransport(handler),
    )
    assert await client.request("send_message", {"chat_id": "-100", "text": "hello"}) == {
        "message_id": "1"
    }
    assert (
        await client.request(
            "download_file", {"chat_id": "-100", "file_handle": "x", "max_bytes": 10}
        )
        == b"bytes-only"
    )
    assert all(request.headers["authorization"] == "Bearer client-secret" for request in seen)
    assert all(b"broker-token" not in request.content for request in seen)
    await client.close()


@pytest.mark.asyncio
async def test_http_client_and_asgi_dispatch_end_to_end_without_network() -> None:
    bridge = make_bridge()
    app = TelegramBridgeASGI(
        bridge,
        relay_store=FakeRelayStore(),
        relay_key=b"r" * 32,
        audiences={"bot": frozenset({"-100"})},
    )
    client = TelegramBridgeHTTPClient(
        origin="https://bridge.example",
        credential="client-secret",
        transport=httpx.ASGITransport(app=app),
    )
    assert await client.request("send_message", {"chat_id": "-100", "text": "in-process"}) == {
        "chat_id": "-100",
        "message_id": "41",
    }
    raw = b'{"update_id":21,"message":{"chat":{"id":-100}}}'
    signature = hmac.new(b"r" * 32, raw, hashlib.sha256).hexdigest()
    await app.accept_relay(raw, signature)
    assert (await client.poll_events())[0]["update"]["update_id"] == 21
    await client.close()


@pytest.mark.asyncio
async def test_asgi_rejects_insecure_scope_by_default() -> None:
    app = TelegramBridgeASGI(
        make_bridge(),
        relay_store=FakeRelayStore(),
        relay_key=b"r" * 32,
        audiences={"bot": frozenset({"-100"})},
    )
    result = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        result.append(message)

    await app(
        {
            "type": "http",
            "scheme": "http",
            "method": "GET",
            "path": "/v1/events",
            "headers": [(b"authorization", b"Bearer client-secret")],
            "query_string": b"",
        },
        receive,
        send,
    )
    start = next(item for item in result if item["type"] == "http.response.start")
    assert start["status"] == 403


@pytest.mark.asyncio
async def test_relay_queue_is_bounded_and_fails_closed_when_full() -> None:
    app = TelegramBridgeASGI(
        make_bridge(),
        relay_store=FakeRelayStore(1),
        relay_key=b"r" * 32,
        audiences={"bot": frozenset({"-100"})},
    )
    for update_id in (1, 2):
        raw = json.dumps(
            {"update_id": update_id, "message": {"chat": {"id": -100}}},
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(b"r" * 32, raw, hashlib.sha256).hexdigest()
        if update_id == 1:
            await app.accept_relay(raw, signature)
        else:
            with pytest.raises(BridgeHTTPError) as error:
                await app.accept_relay(raw, signature)
            assert error.value.code == "relay_unavailable"


@pytest.mark.asyncio
async def test_relay_routes_members_and_configured_poll_audience() -> None:
    app = TelegramBridgeASGI(
        make_bridge(),
        relay_store=FakeRelayStore(),
        relay_key=b"r" * 32,
        audiences={"bot": frozenset({"-100"}), "other": frozenset({"7"})},
        global_event_principals=frozenset({"bot"}),
    )
    for update in (
        {
            "update_id": 31,
            "chat_member": {
                "chat": {"id": -100},
                "old_chat_member": {"status": "left"},
                "new_chat_member": {"status": "member"},
            },
        },
        {"update_id": 32, "poll_answer": {"poll_id": "poll-1", "option_ids": [0]}},
    ):
        raw = json.dumps(update, separators=(",", ":")).encode()
        signature = hmac.new(b"r" * 32, raw, hashlib.sha256).hexdigest()
        await app.accept_relay(raw, signature)
    assert [item["update"]["update_id"] for item in await claimed_events(app, "bot")] == [31, 32]
    assert await claimed_events(app, "other") == []
