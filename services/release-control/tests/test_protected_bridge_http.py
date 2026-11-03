from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from chatcore_release_control.protected_bridge_http import (
    BridgeSendStore,
    ProtectedBridgeApplication,
)

sys.path.insert(0, str(Path(__file__).parent))
pytest_plugins = ("test_ledger",)


@pytest_asyncio.fixture
async def send_store(ledger_pool):
    schema = Path(__file__).parents[1] / "src/chatcore_release_control/bridge_schema.sql"
    await ledger_pool.execute(schema.read_text())
    return BridgeSendStore(ledger_pool)


async def test_bridge_send_intent_is_committed_before_effect_and_retry_returns_same_result(
    send_store,
):
    request_id = uuid4()
    observed = []

    async def effect():
        assert await send_store.state(request_id) == "sending"
        observed.append("sent")
        return {"approval_id": str(uuid4()), "message_id": 10}

    first = await send_store.send_once(request_id, {"binding": "synthetic"}, effect)
    second = await send_store.send_once(request_id, {"binding": "synthetic"}, effect)
    assert first == second and observed == ["sent"]


async def test_bridge_ambiguous_send_or_crashed_intent_is_never_blindly_repeated(send_store):
    request_id = uuid4()
    calls = []

    async def effect():
        calls.append("sent")
        raise RuntimeError("synthetic raw diagnostics")

    with pytest.raises(ValueError, match="send_uncertain"):
        await send_store.send_once(request_id, {"kind": "card"}, effect)
    with pytest.raises(ValueError, match="send_uncertain"):
        await send_store.send_once(request_id, {"kind": "card"}, effect)
    assert calls == ["sent"]


async def test_bridge_request_id_cannot_be_reused_for_a_different_binding(send_store):
    request_id = uuid4()

    async def effect():
        return {"stored": True}

    await send_store.send_once(request_id, {"head": "a"}, effect)
    with pytest.raises(ValueError, match="binding_mismatch"):
        await send_store.send_once(request_id, {"head": "b"}, effect)


class Ingress:
    def __init__(self):
        self.calls = []

    async def handle(self, raw, *, secret_header):
        self.calls.append((raw, secret_header))
        if secret_header != "synthetic-webhook-secret":
            raise ValueError
        return "ordinary"


class Ordinary:
    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def application(send_store):
    ingress, ordinary = Ingress(), Ordinary()
    app = ProtectedBridgeApplication(
        ordinary=ordinary,
        ingress=ingress,
        cards=None,
        ledger=None,
        send_store=send_store,
        broker_credential="b" * 32,
        current_owner=lambda: 7,
        send_report=None,
        allow_private_uds=True,
    )
    return app, ingress, ordinary


async def test_webhook_is_mounted_separately_and_preserves_secret_header(send_store):
    app, ingress, ordinary = application(send_store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        raw = b'{"update_id":1}'
        result = await client.post(
            "/v1/telegram/webhook",
            content=raw,
            headers={"X-Telegram-Bot-Api-Secret-Token": "synthetic-webhook-secret"},
        )
        assert result.status_code == 200
        assert ingress.calls == [(raw, "synthetic-webhook-secret")]
        assert not ordinary.called
        result = await client.post("/v1/telegram/webhook", content=raw)
        assert result.status_code == 403


async def test_broker_routes_reject_ordinary_credential_and_unknown_fields(send_store):
    app, _, ordinary = application(send_store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        result = await client.post(
            "/v1/broker/cards", json={}, headers={"Authorization": "Bearer ordinary-credential"}
        )
        assert result.status_code == 403 and not ordinary.called
        result = await client.post(
            "/v1/broker/reports",
            json={"raw_logs": "synthetic"},
            headers={"Authorization": "Bearer " + "b" * 32},
        )
        assert result.status_code == 400 and not ordinary.called
        result = await client.get("/v1/events")
        assert result.status_code == 204 and ordinary.called


@pytest.mark.asyncio
async def test_card_presentation_valid_invalid_legacy_and_store_failure():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    class Store:
        async def send_once(self, request_id, value, effect):
            return await effect()

    class Cards:
        calls = 0

        async def issue(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                approval_id=uuid4(),
                target="prod",
                sequence=1,
                status=SimpleNamespace(value="pending"),
                binding_digest="a" * 64,
                expires_at=datetime.now(UTC),
            )

    class Views:
        rows = []

        async def put(self, *args):
            self.rows.append(args)
            raise RuntimeError("secret diagnostics")

    card = Cards()
    views = Views()
    app = ProtectedBridgeApplication(
        ordinary=Ordinary(),
        ingress=Ingress(),
        cards=card,
        ledger=None,
        send_store=Store(),
        broker_credential="b" * 32,
        current_owner=lambda: 7,
        send_report=None,
        allow_private_uds=True,
        card_views=views,
    )
    from test_ledger import make_binding

    binding = make_binding(owner_id=202)
    raw = json.loads(binding.to_json())
    raw.pop("version", None)
    raw.pop("review_bundle_digest", None)
    presentation = {
        "pr_url": f"https://github.com/acme/repo/pull/{binding.pr_number}",
        "file_stats": [{"path": "packages/plugin-web/src/plugin.py", "added": 1, "deleted": 0}],
        "patch_state": "unavailable",
        "redacted_files": 0,
    }
    request = {"request_id": str(uuid4()), "binding": raw, "presentation": presentation}
    response = await app._broker_request("/v1/broker/cards", request)
    assert response["status"] == "pending" and card.calls == 1 and len(views.rows) == 1
    invalid = {
        **request,
        "request_id": str(uuid4()),
        "presentation": {**presentation, "pr_url": "https://evil.example"},
    }
    with pytest.raises(ValueError, match="invalid_request"):
        await app._broker_request("/v1/broker/cards", invalid)
    assert card.calls == 1
    legacy = {"request_id": str(uuid4()), "binding": raw}
    await app._broker_request("/v1/broker/cards", legacy)
    assert card.calls == 2


async def test_card_webhook_commit_and_typed_report_cross_actual_database_routes(
    send_store, ledger_pool
):
    from chatcore_release_control.ledger import ReleaseLedger
    from chatcore_release_control.telegram_cards import ReleaseCardIssuer
    from chatcore_release_control.telegram_ingress import TelegramIngress
    from test_ledger import make_binding

    ledger = ReleaseLedger(ledger_pool)
    binding = make_binding(owner_id=202, base_sha="d" * 40)
    review = {
        "complete_delta_reviewed": True,
        "base_is_ancestor_of_head": True,
        "base_is_descendant_of_deployed": True,
        "same_tree_continuation_recorded": False,
        "deployed_commit": binding.deployed_identity.git_sha,
        "base_commit": binding.base_sha,
        "head_commit": binding.head_sha,
        "head_tree": binding.head_tree,
        "deployed_tree": "c" * 40,
        "base_tree": "c" * 40,
        "policy_digest": binding.policy_digest,
        "check_evidence_digest": "6" * 64,
        "deployed_to_base": [],
        "base_to_head": [],
        "deployed_to_head": [],
    }
    binding = make_binding(owner_id=202, base_sha=binding.base_sha, review_bundle=review)
    sends, protected, acknowledgements, reports = [], [], [], []

    class Bot:
        async def call(self, method, payload, *, token):
            assert method == "sendMessage" and token == "synthetic-bot-token"
            sends.append(payload)
            return {"message_id": 900, "chat": {"id": 7}}

    async def acknowledge(query_id):
        assert (
            await ledger_pool.fetchval("SELECT count(*) FROM release_control.decision_outbox") == 1
        )
        acknowledgements.append(query_id)

    async def relay(*args):
        pytest.fail("release callbacks cannot reach ordinary relay")

    async def report(owner, text):
        assert owner == 7
        reports.append(text)
        return 901

    cards = ReleaseCardIssuer(
        ledger=ledger,
        transport=Bot(),
        token="synthetic-bot-token",
        current_telegram_owner=lambda: 7,
        protect_message=lambda chat, message: protected.append((chat, message)),
    )
    ingress = TelegramIngress(
        webhook_secret="w" * 32,
        relay_key=b"r" * 32,
        current_owner=lambda: 7,
        ledger=ledger,
        acknowledge=acknowledge,
        relay=relay,
    )
    app = ProtectedBridgeApplication(
        ordinary=Ordinary(),
        ingress=ingress,
        cards=cards,
        ledger=ledger,
        send_store=send_store,
        broker_credential="b" * 32,
        current_owner=lambda: 7,
        send_report=report,
        allow_private_uds=True,
    )
    raw_binding = json.loads(binding.to_json())
    del raw_binding["version"], raw_binding["review_bundle_digest"]
    headers = {"Authorization": "Bearer " + "b" * 32}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        request = {"request_id": str(uuid4()), "binding": raw_binding}
        first = await client.post("/v1/broker/cards", json=request, headers=headers)
        assert first.status_code == 200, first.text
        assert (
            await client.post("/v1/broker/cards", json=request, headers=headers)
        ).json() == first.json()
        assert len(sends) == 1 and protected == [("7", "900")]
        callback = sends[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        update = {
            "update_id": 991,
            "callback_query": {
                "id": "query-1",
                "from": {"id": 7},
                "message": {"message_id": 900, "chat": {"id": 7, "type": "private"}},
                "data": callback,
            },
        }
        for _ in range(2):
            result = await client.post(
                "/v1/telegram/webhook",
                json=update,
                headers={"X-Telegram-Bot-Api-Secret-Token": "w" * 32},
            )
            assert result.status_code == 200, result.text
        assert acknowledgements == ["query-1", "query-1"]
        report_request = {"request_id": str(uuid4()), "approval_id": first.json()["approval_id"]}
        result = await client.post("/v1/broker/reports", json=report_request, headers=headers)
        assert result.status_code == 200 and result.json()["status"] == "queued"
        again = await client.post("/v1/broker/reports", json=report_request, headers=headers)
        assert again.json() == result.json() and len(reports) == 1
        assert "Status: queued" in reports[0]
        assert (
            await ledger_pool.fetchval("SELECT count(*) FROM release_control.decision_outbox") == 1
        )


async def test_watchdog_alert_route_uses_fixed_labels_current_owner_and_dedup(send_store):
    sends = []

    async def send_report(owner, text):
        sends.append((owner, text))
        return 902

    app = ProtectedBridgeApplication(
        ordinary=Ordinary(),
        ingress=Ingress(),
        cards=None,
        ledger=None,
        send_store=send_store,
        broker_credential="b" * 32,
        current_owner=lambda: 7,
        send_report=send_report,
        allow_private_uds=True,
    )
    request = {
        "request_id": str(uuid4()),
        "kind": "cpu_pressure",
        "service": "metrics",
        "state": "firing",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        headers = {"Authorization": "Bearer " + "b" * 32}
        first = await client.post("/v1/broker/alerts", json=request, headers=headers)
        again = await client.post("/v1/broker/alerts", json=request, headers=headers)
        assert first.status_code == 200 and again.json() == first.json()
        assert sends == [(7, "Release-control alert: CPU pressure on host resources firing.")]
        denied = await client.post(
            "/v1/broker/alerts",
            json={**request, "raw_logs": "must not be sent"},
            headers=headers,
        )
        assert denied.status_code == 400
        denied = await client.post(
            "/v1/broker/alerts",
            json={**request, "kind": "arbitrary message"},
            headers=headers,
        )
        assert denied.status_code == 400
        denied = await client.post(
            "/v1/broker/alerts",
            json=request,
            headers={"Authorization": "Bearer ordinary-credential"},
        )
        assert denied.status_code == 403
    assert len(sends) == 1


async def test_send_store_accepts_presentation_with_maximum_patch(send_store):
    from chatcore_release_control.release_card_view import MAX_PATCH_BYTES

    value = {
        "route": "/v1/broker/cards",
        "binding": "synthetic",
        "presentation": {"patch_state": "included", "patch": "+" * MAX_PATCH_BYTES},
    }

    async def effect():
        return {"ok": True}

    assert await send_store.send_once(uuid4(), value, effect) == {"ok": True}
