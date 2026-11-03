from __future__ import annotations

import base64
from collections.abc import Mapping

import pytest
from chatcore_release_control.telegram_bridge import (
    MAX_FILE_BYTES,
    BridgeError,
    MessageOwnershipStore,
    TelegramBridge,
    TelegramBridgeClient,
)


class FakeBotApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.response: object | None = None
        self.file_bytes = b"test-file"

    async def call(self, method: str, parameters: Mapping[str, object], *, token: str) -> object:
        self.calls.append((method, dict(parameters)))
        if method == "answerCallbackQuery":
            return {"ok": True}
        if method == "getMe":
            return {
                "id": 123,
                "is_bot": True,
                "first_name": "bridgebot",
                "username": "bridge_bot",
                "token_url": "must-not-escape",
            }
        if method == "setMyCommands":
            return True
        if method == "getChatMember":
            return {
                "status": "restricted",
                "is_member": True,
                "user": {"id": parameters["user_id"], "first_name": "member"},
                "token_url": "must-not-escape",
            }
        if self.response is not None:
            return self.response
        return {"message_id": 41, "chat": {"id": parameters["chat_id"]}}

    async def download_file(self, file_id: str, max_bytes: int, *, token: str) -> bytes:
        self.calls.append(("download_file", {"file_id": file_id, "max_bytes": max_bytes}))
        return self.file_bytes


def bridge(
    api: FakeBotApi | None = None,
    *,
    token: str = "sentinel-bot-token",
    ownership: MessageOwnershipStore | None = None,
    load_protected_messages=lambda: (),
) -> TelegramBridge:
    return TelegramBridge(
        token=token,
        authenticate=lambda credential: "ordinary-bot" if credential == "client-key" else None,
        allowed_chats={
            "ordinary-bot": frozenset({"-100", "7"}),
            "another-principal": frozenset({"-100"}),
        },
        transport=api or FakeBotApi(),
        ownership=ownership,
        load_protected_messages=load_protected_messages,
    )


@pytest.mark.asyncio
async def test_authenticated_client_can_send_text_only_to_configured_chat() -> None:
    api = FakeBotApi()
    service = bridge(api)
    result = await service.handle(
        "client-key",
        {"operation": "send_message", "payload": {"chat_id": "-100", "text": "hello"}},
    )
    assert result == {"chat_id": "-100", "message_id": "41"}
    assert api.calls == [
        (
            "sendMessage",
            {"chat_id": "-100", "text": "hello", "link_preview_options": {"is_disabled": True}},
        )
    ]


@pytest.mark.asyncio
async def test_authentication_chat_and_request_schema_fail_with_fixed_errors() -> None:
    service = bridge()
    cases = [
        (
            "invalid-key",
            {"operation": "send_message", "payload": {"chat_id": "7", "text": "x"}},
            "unauthorized",
        ),
        (
            "client-key",
            {"operation": "send_message", "payload": {"chat_id": "999", "text": "x"}},
            "forbidden",
        ),
        ("client-key", {"operation": "setWebhook", "payload": {}}, "unsupported_operation"),
        (
            "client-key",
            {
                "operation": "send_message",
                "payload": {"chat_id": "7", "text": "x", "reply_markup": {}},
            },
            "invalid_request",
        ),
        (
            "client-key",
            {"operation": "send_message", "payload": {"chat_id": "7"}},
            "invalid_request",
        ),
        (
            "client-key",
            {"operation": "send_message", "payload": {"chat_id": 7, "text": "x"}},
            "invalid_request",
        ),
    ]
    for credential, request, expected_code in cases:
        with pytest.raises(BridgeError) as error:
            await service.handle(credential, request)
        assert error.value.code == expected_code


@pytest.mark.asyncio
async def test_transport_errors_and_representations_never_expose_bot_token() -> None:
    class FailingApi(FakeBotApi):
        async def call(
            self, method: str, parameters: Mapping[str, object], *, token: str
        ) -> object:
            raise RuntimeError(
                "sentinel-bot-token appeared in https://api.telegram.org/botsentinel-bot-token"
            )

    service = bridge(FailingApi())
    with pytest.raises(BridgeError) as error:
        await service.handle(
            "client-key",
            {"operation": "send_message", "payload": {"chat_id": "7", "text": "x"}},
        )
    assert error.value.code == "upstream_error"
    assert "sentinel-bot-token" not in repr(service)
    assert "sentinel-bot-token" not in repr(error.value)
    assert "sentinel-bot-token" not in str(error.value)


@pytest.mark.asyncio
async def test_ordinary_client_contract_has_no_bot_token_configuration() -> None:
    sent: list[tuple[str, Mapping[str, object]]] = []

    async def submit(credential: str, request: Mapping[str, object]) -> object:
        sent.append((credential, request))
        return {"ok": True}

    client = TelegramBridgeClient(submit, credential="client-key")
    assert await client.request("send_message", {"chat_id": "7", "text": "hello"}) == {"ok": True}
    assert sent == [
        ("client-key", {"operation": "send_message", "payload": {"chat_id": "7", "text": "hello"}})
    ]
    assert "bot_token" not in vars(client)
    assert "sentinel-bot-token" not in repr(client)


@pytest.mark.asyncio
async def test_message_mutations_require_app_ownership_in_the_same_chat() -> None:
    api = FakeBotApi()
    service = bridge(api)
    sent = await service.handle(
        "client-key",
        {"operation": "send_message", "payload": {"chat_id": "-100", "text": "owned"}},
    )
    await service.handle(
        "client-key",
        {
            "operation": "edit_message",
            "payload": {"chat_id": "-100", "message_id": sent["message_id"], "text": "updated"},
        },
    )
    await service.handle(
        "client-key",
        {
            "operation": "delete_message",
            "payload": {"chat_id": "-100", "message_id": sent["message_id"]},
        },
    )
    assert [method for method, _ in api.calls] == [
        "sendMessage",
        "editMessageText",
        "deleteMessage",
    ]

    for request in (
        {
            "operation": "edit_message",
            "payload": {"chat_id": "-100", "message_id": "900", "text": "no"},
        },
        {"operation": "delete_message", "payload": {"chat_id": "7", "message_id": "41"}},
    ):
        with pytest.raises(BridgeError) as error:
            await service.handle("client-key", request)
        assert error.value.code == "forbidden"


@pytest.mark.asyncio
async def test_broker_protected_message_cannot_be_mutated_and_release_markup_is_not_public() -> (
    None
):
    api = FakeBotApi()
    service = bridge(api)
    service.protect_release_message("-100", "700")
    requests = (
        {
            "operation": "edit_message",
            "payload": {"chat_id": "-100", "message_id": "700", "text": "replace"},
        },
        {"operation": "delete_message", "payload": {"chat_id": "-100", "message_id": "700"}},
        {
            "operation": "send_message",
            "payload": {"chat_id": "-100", "text": "x", "reply_markup": {}},
        },
        {
            "operation": "confirm",
            "payload": {"chat_id": "-100", "prompt": "x", "action_id": "release:approve"},
        },
        {"operation": "copyMessage", "payload": {}},
        {"operation": "forwardMessage", "payload": {}},
        {"operation": "setWebhook", "payload": {}},
    )
    for request in requests:
        with pytest.raises(BridgeError):
            await service.handle("client-key", request)
    assert api.calls == []


@pytest.mark.asyncio
async def test_confirm_builds_only_bounded_ordinary_callback_namespace() -> None:
    api = FakeBotApi()
    service = bridge(api)
    result = await service.handle(
        "client-key",
        {
            "operation": "confirm",
            "payload": {"chat_id": "7", "prompt": "Continue?", "action_id": "ordinary_123"},
        },
    )
    assert result == {"chat_id": "7", "message_id": "41"}
    method, parameters = api.calls[0]
    assert method == "sendMessage"
    markup = parameters["reply_markup"]
    assert markup == {
        "inline_keyboard": [
            [
                {"text": "Confirm", "callback_data": "c:ordinary_123"},
                {"text": "Cancel", "callback_data": "x:ordinary_123"},
            ]
        ]
    }


@pytest.mark.asyncio
async def test_document_upload_decodes_bounded_base64_and_registers_message() -> None:
    api = FakeBotApi()
    service = bridge(api)
    data = b"document bytes"
    result = await service.handle(
        "client-key",
        {
            "operation": "send_document",
            "payload": {
                "chat_id": "7",
                "filename": "report.txt",
                "data_base64": base64.b64encode(data).decode("ascii"),
                "caption": "Report",
            },
        },
    )
    assert result == {"chat_id": "7", "message_id": "41"}
    assert api.calls == [
        (
            "sendDocument",
            {"chat_id": "7", "filename": "report.txt", "document": data, "caption": "Report"},
        )
    ]


@pytest.mark.asyncio
async def test_document_upload_rejects_bad_base64_and_release_callback_namespace() -> None:
    service = bridge()
    cases = (
        {
            "operation": "send_document",
            "payload": {"chat_id": "7", "filename": "x", "data_base64": "not base64!"},
        },
        {
            "operation": "confirm",
            "payload": {"chat_id": "7", "prompt": "approve", "action_id": "release:approve"},
        },
    )
    for request in cases:
        with pytest.raises(BridgeError) as error:
            await service.handle("client-key", request)
        assert error.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_file_download_returns_bounded_bytes_without_file_path_or_url() -> None:
    api = FakeBotApi()
    service = bridge(api)
    handle = service.register_file_handle("ordinary-bot", "-100", "telegram-file-id")
    result = await service.handle(
        "client-key",
        {
            "operation": "download_file",
            "payload": {"chat_id": "-100", "file_handle": handle, "max_bytes": 32},
        },
    )
    assert result == b"test-file"
    assert "sentinel-bot-token" not in repr(result)
    with pytest.raises(BridgeError) as error:
        await service.handle(
            "client-key",
            {
                "operation": "download_file",
                "payload": {"chat_id": "7", "file_handle": handle, "max_bytes": 32},
            },
        )
    assert error.value.code == "forbidden"

    foreign_handle = service.register_file_handle("another-principal", "-100", "other-file")
    with pytest.raises(BridgeError) as error:
        await service.handle(
            "client-key",
            {
                "operation": "download_file",
                "payload": {"chat_id": "-100", "file_handle": foreign_handle, "max_bytes": 32},
            },
        )
    assert error.value.code == "forbidden"


@pytest.mark.asyncio
async def test_file_download_rejects_oversized_actual_bytes() -> None:
    api = FakeBotApi()
    api.file_bytes = b"x" * 33
    service = bridge(api)
    handle = service.register_file_handle("ordinary-bot", "7", "telegram-file-id")
    with pytest.raises(BridgeError) as error:
        await service.handle(
            "client-key",
            {
                "operation": "download_file",
                "payload": {"chat_id": "7", "file_handle": handle, "max_bytes": 32},
            },
        )
    assert error.value.code == "file_too_large"

    with pytest.raises(BridgeError) as error:
        service.register_file_handle(
            "ordinary-bot", "7", "large-file", declared_size=MAX_FILE_BYTES + 1
        )
    assert error.value.code == "file_too_large"


@pytest.mark.asyncio
async def test_file_proxy_never_returns_a_token_bearing_url() -> None:
    api = FakeBotApi()
    service = bridge(api)
    handle = service.register_file_handle(
        "ordinary-bot", "7", "telegram-file-id", declared_size=128
    )
    with pytest.raises(BridgeError) as error:
        await service.handle(
            "client-key",
            {
                "operation": "download_file",
                "payload": {"chat_id": "7", "file_handle": handle, "max_bytes": 32},
            },
        )
    assert error.value.code == "file_too_large"
    assert api.calls == []

    async def return_url(file_id: str, max_bytes: int, *, token: str) -> bytes:
        return "https://api.telegram.org/file/botsentinel-bot-token/private.dat"  # type: ignore[return-value]

    api.download_file = return_url  # type: ignore[method-assign]
    safe_handle = service.register_file_handle("ordinary-bot", "7", "another-file")
    with pytest.raises(BridgeError) as error:
        await service.handle(
            "client-key",
            {
                "operation": "download_file",
                "payload": {
                    "chat_id": "7",
                    "file_handle": safe_handle,
                    "max_bytes": 32,
                },
            },
        )
    assert error.value.code == "upstream_error"
    assert "sentinel-bot-token" not in str(error.value)


@pytest.mark.asyncio
async def test_ownership_store_reloads_sent_message_ownership(tmp_path) -> None:
    store_path = tmp_path / "ownership.json"
    api = FakeBotApi()
    first = TelegramBridge(
        token="token",
        authenticate=lambda credential: "ordinary-bot" if credential == "client-key" else None,
        allowed_chats={"ordinary-bot": frozenset({"7"})},
        transport=api,
        ownership=MessageOwnershipStore(store_path),
        load_protected_messages=lambda: (),
    )
    sent = await first.handle(
        "client-key", {"operation": "send_message", "payload": {"chat_id": "7", "text": "owned"}}
    )
    reopened = TelegramBridge(
        token="token",
        authenticate=lambda credential: "ordinary-bot" if credential == "client-key" else None,
        allowed_chats={"ordinary-bot": frozenset({"7"})},
        transport=api,
        ownership=MessageOwnershipStore(store_path),
        load_protected_messages=lambda: (),
    )
    await reopened.handle(
        "client-key",
        {
            "operation": "delete_message",
            "payload": {"chat_id": "7", "message_id": sent["message_id"]},
        },
    )
    assert api.calls[-1][0] == "deleteMessage"


@pytest.mark.asyncio
async def test_protected_app_owned_message_stays_protected_after_store_reopen(tmp_path) -> None:
    api = FakeBotApi()
    store_path = tmp_path / "ownership.json"
    first_store = MessageOwnershipStore(store_path)
    first = bridge(api, ownership=first_store)
    await first.handle(
        "client-key",
        {"operation": "send_message", "payload": {"chat_id": "-100", "text": "owned"}},
    )
    first.protect_release_message("-100", "41")

    reopened = bridge(api, ownership=MessageOwnershipStore(store_path))
    with pytest.raises(BridgeError) as error:
        await reopened.handle(
            "client-key",
            {
                "operation": "edit_message",
                "payload": {"chat_id": "-100", "message_id": "41", "text": "change"},
            },
        )
    assert error.value.code == "forbidden"
    assert api.calls[-1][0] != "editMessageText"


def test_bridge_reconstructs_protected_messages_before_serving_requests() -> None:
    service = bridge(load_protected_messages=lambda: (("-100", "41"),))
    assert service


@pytest.mark.asyncio
async def test_protected_registry_write_failure_disables_service_until_reconstructed(
    tmp_path, monkeypatch
) -> None:
    api = FakeBotApi()
    store_path = tmp_path / "ownership.json"
    store = MessageOwnershipStore(store_path)
    service = bridge(api, ownership=store)
    await service.handle(
        "client-key",
        {"operation": "send_message", "payload": {"chat_id": "-100", "text": "owned"}},
    )

    def fail_write() -> None:
        raise BridgeError("invalid_request")

    monkeypatch.setattr(store, "_save", fail_write)
    with pytest.raises(BridgeError):
        service.protect_release_message("-100", "41")
    with pytest.raises(BridgeError):
        await service.handle(
            "client-key",
            {
                "operation": "edit_message",
                "payload": {"chat_id": "-100", "message_id": "41", "text": "change"},
            },
        )
    assert api.calls[-1][0] != "editMessageText"

    reopened = bridge(
        api,
        ownership=MessageOwnershipStore(store_path),
        load_protected_messages=lambda: (("-100", "41"),),
    )
    with pytest.raises(BridgeError) as error:
        await reopened.handle(
            "client-key",
            {
                "operation": "edit_message",
                "payload": {"chat_id": "-100", "message_id": "41", "text": "change"},
            },
        )
    assert error.value.code == "forbidden"


def test_ownership_store_rejects_legacy_unversioned_schema(tmp_path) -> None:
    store_path = tmp_path / "ownership.json"
    store_path.write_text('[["ordinary-bot","7","41"]]', encoding="utf-8")
    with pytest.raises(BridgeError):
        MessageOwnershipStore(store_path)


def test_bridge_does_not_start_when_protected_registry_reconstruction_fails() -> None:
    def fail_load():
        raise RuntimeError("ledger unavailable")

    with pytest.raises(BridgeError):
        bridge(load_protected_messages=fail_load)


@pytest.mark.asyncio
async def test_send_operations_preserve_thread_reply_and_fixed_entities() -> None:
    api = FakeBotApi()
    service = bridge(api)
    result = await service.handle(
        "client-key",
        {
            "operation": "send_message",
            "payload": {
                "chat_id": "-100",
                "text": "hello",
                "reply_to": "12",
                "thread_id": "3",
                "entities": [{"type": "bold", "offset": 0, "length": 5}],
            },
        },
    )
    assert result["message_id"] == "41"
    assert api.calls[-1][1] == {
        "chat_id": "-100",
        "text": "hello",
        "reply_parameters": {"message_id": 12},
        "message_thread_id": 3,
        "entities": [{"type": "bold", "offset": 0, "length": 5}],
        "link_preview_options": {"is_disabled": True},
    }
    with pytest.raises(BridgeError):
        await service.handle(
            "client-key",
            {
                "operation": "send_message",
                "payload": {
                    "chat_id": "-100",
                    "text": "x",
                    "entities": [{"type": "unknown", "offset": 0, "length": 1}],
                },
            },
        )


@pytest.mark.asyncio
async def test_poll_stop_requires_an_owned_message_and_returns_bounded_result() -> None:
    api = FakeBotApi()
    api.response = {"message_id": 41, "chat": {"id": "-100"}, "poll": {"id": "poll-1"}}
    service = bridge(api)
    await service.handle(
        "client-key",
        {
            "operation": "send_poll",
            "payload": {
                "chat_id": "-100",
                "question": "Q?",
                "options": ["a", "b"],
                "multiple": False,
                "anonymous": True,
                "thread_id": "4",
            },
        },
    )
    assert api.calls[-1][1]["message_thread_id"] == 4
    api.response = {"options": [{"voter_count": 2}, {"voter_count": 1}], "total_voter_count": 3}
    assert await service.handle(
        "client-key", {"operation": "stop_poll", "payload": {"chat_id": "-100", "message_id": "41"}}
    ) == {"counts": [2, 1], "total_voters": 3}
    assert api.calls[-1][0] == "stopPoll"
    with pytest.raises(BridgeError) as error:
        await service.handle(
            "client-key",
            {"operation": "stop_poll", "payload": {"chat_id": "-100", "message_id": "999"}},
        )
    assert error.value.code == "forbidden"


@pytest.mark.asyncio
async def test_callback_ack_requires_one_time_trusted_ingress_handle() -> None:
    api = FakeBotApi()
    service = bridge(api)
    handle = service.register_callback_handle(
        "ordinary-bot", "-100", "callback-query-1", "c:ordinary"
    )
    assert await service.handle(
        "client-key", {"operation": "answer_callback", "payload": {"callback_handle": handle}}
    ) == {"ok": True}
    assert api.calls[-1] == ("answerCallbackQuery", {"callback_query_id": "callback-query-1"})
    with pytest.raises(BridgeError):
        await service.handle(
            "client-key",
            {"operation": "answer_callback", "payload": {"callback_handle": "callback-query-1"}},
        )
    with pytest.raises(BridgeError):
        service.register_callback_handle(
            "ordinary-bot", "-100", "callback-query-2", "release:a:" + "x" * 32
        )


@pytest.mark.asyncio
async def test_get_me_returns_only_allowlisted_identity_fields() -> None:
    api = FakeBotApi()
    service = bridge(api)
    assert await service.handle("client-key", {"operation": "get_me", "payload": {}}) == {
        "id": 123,
        "is_bot": True,
        "first_name": "bridgebot",
        "username": "bridge_bot",
    }
    assert api.calls[-1] == ("getMe", {})


@pytest.mark.asyncio
async def test_set_commands_requires_one_principal_and_preserves_reserved_names() -> None:
    api = FakeBotApi()
    service = TelegramBridge(
        token="sentinel-bot-token",
        authenticate=lambda value: "ordinary-bot" if value == "client-key" else "other",
        allowed_chats={"ordinary-bot": frozenset({"-100"}), "other": frozenset({"-100"})},
        transport=api,
        load_protected_messages=lambda: (),
        menu_principal="ordinary-bot",
        reserved_commands={"private": (("help", "Broker help"),)},
    )
    result = await service.handle(
        "client-key",
        {
            "operation": "set_commands",
            "payload": {
                "scope": "private",
                "commands": [
                    {"command": "help", "description": "try to replace"},
                    {"command": "status", "description": "Show status"},
                ],
            },
        },
    )
    assert result == {"ok": True}
    method, params = api.calls[-1]
    assert method == "setMyCommands"
    assert params["scope"] == {"type": "all_private_chats"}
    assert params["commands"] == [
        {"command": "help", "description": "Broker help"},
        {"command": "status", "description": "Show status"},
    ]
    with pytest.raises(BridgeError) as denied:
        await service.handle(
            "other-key",
            {"operation": "set_commands", "payload": {"scope": "groups", "commands": []}},
        )
    assert denied.value.code == "forbidden"
    with pytest.raises(BridgeError):
        await service.handle(
            "client-key",
            {"operation": "set_commands", "payload": {"scope": "all", "commands": []}},
        )


@pytest.mark.asyncio
async def test_membership_result_filters_user_profiles_and_transport_extras() -> None:
    api = FakeBotApi()
    service = bridge(api)
    assert await service.handle(
        "client-key",
        {"operation": "get_chat_member", "payload": {"chat_id": "-100", "user_id": "7"}},
    ) == {"status": "restricted", "is_member": True}
