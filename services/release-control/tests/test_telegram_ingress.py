from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from chatcore_release_control.ledger import Decision
from chatcore_release_control.revocation import RevocationReceipt
from chatcore_release_control.telegram_ingress import (
    IngressError,
    TelegramIngress,
    verify_relay_body,
)

SECRET = "synthetic-webhook-secret-32-bytes__"
RELAY = b"synthetic-distinct-relay-key-32bytes"
NONCE = "n" * 32


def update(*, actor=101, chat_type="private", data="release:a:" + NONCE):
    return json.dumps(
        {
            "update_id": 7,
            "callback_query": {
                "id": "synthetic-query",
                "from": {"id": actor},
                "data": data,
                "message": {"message_id": 9, "chat": {"id": 101, "type": chat_type}},
            },
        }
    ).encode()


class Ledger:
    def __init__(self):
        self.events = []
        self.card = SimpleNamespace(approval_id=uuid4(), binding_digest="a" * 64)

    async def get_card_by_message(self, chat_id, message_id):
        self.events.append(("lookup", chat_id, message_id))
        if message_id != 9:
            raise RuntimeError("not found")
        return self.card

    async def record_decision(self, command):
        self.events.append(("commit", command))
        return SimpleNamespace(status="queued")


def router(ledger, *, owner=lambda: 101):
    async def acknowledge(query_id):
        ledger.events.append(("ack", query_id))

    async def relay(body, signature):
        ledger.events.append(("relay", body, signature))

    return TelegramIngress(
        webhook_secret=SECRET,
        relay_key=RELAY,
        current_owner=owner,
        ledger=ledger,
        acknowledge=acknowledge,
        relay=relay,
    )


def owner_command_update(
    *,
    update_id=12,
    sender=101,
    chat_id=101,
    chat_type="private",
    text="/release_revoke aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    extra_message=None,
):
    message = {
        "message_id": 30,
        "date": 1_798_000_000,
        "from": {"id": sender, "is_bot": False},
        "chat": {"id": chat_id, "type": chat_type},
        "text": text,
    }
    if extra_message:
        message.update(extra_message)
    return json.dumps({"update_id": update_id, "message": message}).encode()


class Revocations:
    def __init__(self):
        self.events = []

    async def revoke(self, update_id, approval_id, owner_user_id):
        self.events.append(("revoke", update_id, approval_id, owner_user_id))
        return RevocationReceipt(
            update_id=update_id,
            approval_id=approval_id,
            target="sb-bot",
            owner_user_id=owner_user_id,
            receipt_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            request_digest="d" * 64,
            recorded_at=datetime(2026, 10, 31, tzinfo=UTC),
        )


def revocation_router(ledger, revocations, *, owner=lambda: 101):
    async def acknowledge(query_id):
        ledger.events.append(("ack", query_id))

    async def relay(body, signature):
        ledger.events.append(("relay", body, signature))

    async def report(owner_id, approval_id, receipt_id):
        ledger.events.append(("revocation_report", owner_id, approval_id, receipt_id))

    return TelegramIngress(
        webhook_secret=SECRET,
        relay_key=RELAY,
        current_owner=owner,
        ledger=ledger,
        acknowledge=acknowledge,
        relay=relay,
        revocations=revocations,
        report_revocation=report,
    )


async def test_authentic_release_commits_before_callback_ack():
    ledger = Ledger()
    assert await router(ledger).handle(update(), secret_header=SECRET) == "release"
    assert [item[0] for item in ledger.events] == ["lookup", "commit", "ack"]
    command = ledger.events[1][1]
    assert command.decision is Decision.APPROVE
    assert command.nonce == NONCE
    assert command.approval_id == ledger.card.approval_id


@pytest.mark.parametrize(("data", "kind"), [("release:i", "details"), ("release:v", "diff")])
async def test_release_view_is_owner_only_read_only_and_acknowledged_last(data, kind):
    ledger = Ledger()
    called = []

    async def view(actual_kind, card):
        called.append((actual_kind, card))
        ledger.events.append(("view", actual_kind))

    base = router(ledger)
    ingress = TelegramIngress(
        webhook_secret=SECRET,
        relay_key=RELAY,
        current_owner=lambda: 101,
        ledger=ledger,
        acknowledge=base._acknowledge,
        relay=base._relay,
        card_views=view,
    )
    assert await ingress.handle(update(data=data), secret_header=SECRET) == "release_view"
    assert called == [(kind, ledger.card)]
    assert [event[0] for event in ledger.events] == ["lookup", "view", "ack"]
    assert not any(event[0] == "commit" for event in ledger.events)


@pytest.mark.parametrize(
    ("body", "views"),
    [
        (update(actor=202, data="release:i"), True),
        (update(chat_type="group", data="release:v"), True),
        (update(data="release:x"), True),
    ],
)
async def test_invalid_release_view_never_calls_view_or_ack(body, views):
    ledger, called = Ledger(), []

    async def view(*args):
        called.append(args)

    base = router(ledger)
    ingress = TelegramIngress(
        webhook_secret=SECRET,
        relay_key=RELAY,
        current_owner=lambda: 101,
        ledger=ledger,
        acknowledge=base._acknowledge,
        relay=base._relay,
        card_views=view,
    )
    with pytest.raises(IngressError):
        await ingress.handle(body, secret_header=SECRET)
    assert called == [] and not any(event[0] == "ack" for event in ledger.events)


@pytest.mark.parametrize(
    "body,secret",
    [
        (update(), "wrong"),
        (update(actor=202), SECRET),
        (update(chat_type="group"), SECRET),
        (update(data="release:forged"), SECRET),
    ],
)
async def test_wrong_secret_owner_group_or_namespace_is_never_relayed(body, secret):
    ledger = Ledger()
    with pytest.raises(IngressError):
        await router(ledger).handle(body, secret_header=secret)
    assert not ledger.events


async def test_removed_owner_and_commit_outage_cannot_ack_or_relay():
    ledger = Ledger()
    with pytest.raises(IngressError):
        await router(ledger, owner=lambda: 202).handle(update(), secret_header=SECRET)
    assert not ledger.events

    async def outage(_):
        raise RuntimeError("synthetic database diagnostics")

    ledger.record_decision = outage
    with pytest.raises(IngressError) as error:
        await router(ledger).handle(update(), secret_header=SECRET)
    assert [item[0] for item in ledger.events] == ["lookup"]
    assert "diagnostics" not in str(error.value)


async def test_ordinary_updates_are_relayed_with_distinct_authentication():
    ledger = Ledger()
    body = b'{"update_id":8,"message":{"text":"hello"}}'
    assert await router(ledger).handle(body, secret_header=SECRET) == "ordinary"
    _, relayed, signature = ledger.events[0]
    assert json.loads(relayed) == json.loads(body)
    assert hmac.compare_digest(signature, hmac.new(RELAY, relayed, hashlib.sha256).hexdigest())
    assert SECRET.encode() not in relayed
    assert verify_relay_body(relayed, signature, RELAY) == json.loads(body)
    with pytest.raises(IngressError):
        verify_relay_body(relayed, "0" * 64, RELAY)


async def test_duplicate_json_and_forwarded_release_claims_are_denied():
    ledger = Ledger()
    with pytest.raises(IngressError):
        await router(ledger).handle(b'{"update_id":8,"update_id":9}', secret_header=SECRET)
    assert not ledger.events


async def test_owner_revocation_command_commits_then_reports_without_ordinary_relay():
    ledger, revocations = Ledger(), Revocations()
    revocations.events = ledger.events

    result = await revocation_router(ledger, revocations).handle(
        owner_command_update(), secret_header=SECRET
    )

    assert result == "revocation"
    assert ledger.events == [
        ("revoke", 12, UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), 101),
        (
            "revocation_report",
            101,
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        ),
    ]


@pytest.mark.parametrize(
    "body,owner",
    [
        (owner_command_update(sender=202), lambda: 101),
        (owner_command_update(chat_id=202), lambda: 101),
        (owner_command_update(chat_type="group"), lambda: 101),
        (owner_command_update(extra_message={"forward_origin": {"type": "user"}}), lambda: 101),
        (owner_command_update(extra_message={"forward_from": {"id": 99}}), lambda: 101),
        (owner_command_update(extra_message={"message_thread_id": 3}), lambda: 101),
        (owner_command_update(text="/release_revoke not-a-uuid"), lambda: 101),
        (
            owner_command_update(text="/release_revoke aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa extra"),
            lambda: 101,
        ),
        (owner_command_update(update_id=0), lambda: 101),
        (owner_command_update(), lambda: 202),
    ],
)
async def test_invalid_owner_revocation_provenance_is_never_relayed(body, owner):
    ledger, revocations = Ledger(), Revocations()
    with pytest.raises(IngressError):
        await revocation_router(ledger, revocations, owner=owner).handle(body, secret_header=SECRET)
    assert revocations.events == []
    assert ledger.events == []
