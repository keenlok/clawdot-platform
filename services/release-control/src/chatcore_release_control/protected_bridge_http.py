"""Protected webhook/card/report routes around the token-free ordinary bridge API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from .ledger import ReleaseBinding
from .policy import DeploymentIdentity
from .release_card_view import CardPresentation

_LOG = logging.getLogger(__name__)

_DISPATCH_STATES = {"sending", "accepted", "uncertain", "retired"}
_RUN_STATES = {"queued", "in_progress", "completed", "waiting", "pending", "requested"}
_CONCLUSIONS = {
    "cancelled",
    "failure",
    "timed_out",
    "skipped",
    "action_required",
    "startup_failure",
    "success",
    "neutral",
    "stale",
}
_ALERT_LABELS = {
    "service_inactive": "service inactive",
    "systemd_unavailable": "service status unavailable",
    "cpu_pressure": "CPU pressure",
    "load_pressure": "load pressure",
    "memory_pressure": "memory pressure",
    "disk_pressure": "disk space low",
    "metrics_unavailable": "host metrics unavailable",
    "host_state_unavailable": "host state unavailable",
    "host_unhealthy": "host health failure",
    "journal_unavailable": "host journal unavailable",
    "journal_deadline": "host operation deadline expired",
    "journal_recovery_failed": "host recovery failed",
}
_ALERT_SERVICES = {"bridge", "broker", "host", "worker", "metrics", "journal"}
_ALERT_SERVICE_LABELS = {
    "bridge": "bridge service",
    "broker": "broker service",
    "host": "host service",
    "worker": "worker service",
    "metrics": "host resources",
    "journal": "deployment journal",
}
_ALERT_FIELDS = {"request_id", "kind", "service", "state"}


async def report_observation(pool, card) -> dict:
    """Read only typed protected worker records; raw workflow output is excluded."""
    if (
        await pool.fetchval(
            "SELECT to_regclass('release_control.broker_dispatch_intent') IS NOT NULL "
            "AND to_regclass('release_control.broker_run_observation') IS NOT NULL"
        )
        is not True
    ):
        return {}
    intent = await pool.fetchval(
        "SELECT state FROM release_control.broker_dispatch_intent "
        "WHERE approval_id=$1 AND dispatch_fence=$2",
        card.approval_id,
        card.fence,
    )
    run = await pool.fetchrow(
        "SELECT status,conclusion FROM release_control.broker_run_observation "
        "WHERE approval_id=$1 AND dispatch_fence=$2 ORDER BY observed_at DESC LIMIT 1",
        card.approval_id,
        card.fence,
    )
    result = {}
    if intent is not None:
        if intent not in _DISPATCH_STATES:
            raise ValueError("invalid_request")
        result["dispatch"] = intent
    if run is not None:
        if run["status"] not in _RUN_STATES or run["conclusion"] not in _CONCLUSIONS | {None}:
            raise ValueError("invalid_request")
        result["run"] = run["status"]
        if run["conclusion"]:
            result["conclusion"] = run["conclusion"]
    return result


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class BridgeSendStore:
    """Send intents survive restart; ambiguous external effects require reconciliation."""

    def __init__(self, pool):
        self._pool = pool

    async def ready(self) -> bool:
        return (
            await self._pool.fetchval(
                "SELECT to_regclass('release_control.bridge_broker_send') IS NOT NULL"
            )
            is True
        )

    async def state(self, request_id: UUID) -> str | None:
        return await self._pool.fetchval(
            "SELECT state FROM release_control.bridge_broker_send WHERE request_id=$1", request_id
        )

    async def send_once(
        self, request_id: UUID, value: dict, effect: Callable[[], Awaitable[dict]]
    ) -> dict:
        if not isinstance(request_id, UUID):
            raise ValueError("invalid_request")
        raw = _canonical(value)
        if len(raw) > 1024 * 1024:
            raise ValueError("invalid_request")
        digest = hashlib.sha256(raw).digest()
        try:
            async with self._pool.acquire() as connection:
                inserted = await connection.fetchval(
                    "INSERT INTO release_control.bridge_broker_send"
                    "(request_id,request_digest,state) "
                    "VALUES($1,$2,'sending') ON CONFLICT DO NOTHING RETURNING request_id",
                    request_id,
                    digest,
                )
                if inserted is None:
                    row = await connection.fetchrow(
                        "SELECT * FROM release_control.bridge_broker_send WHERE request_id=$1",
                        request_id,
                    )
                    if row is None or bytes(row["request_digest"]) != digest:
                        raise ValueError("binding_mismatch")
                    if row["state"] != "complete":
                        raise ValueError("send_uncertain")
                    return (
                        json.loads(row["response"])
                        if isinstance(row["response"], str)
                        else row["response"]
                    )
            try:
                response = await effect()
                encoded = _canonical(response)
                if not isinstance(response, dict) or len(encoded) > 8192:
                    raise ValueError
            except Exception:
                await self._pool.execute(
                    "UPDATE release_control.bridge_broker_send SET state='uncertain' "
                    "WHERE request_id=$1 AND state='sending'",
                    request_id,
                )
                raise ValueError("send_uncertain") from None
            await self._pool.execute(
                "UPDATE release_control.bridge_broker_send SET state='complete',response=$2::jsonb,"
                "completed_at=clock_timestamp() WHERE request_id=$1 AND state='sending'",
                request_id,
                encoded.decode(),
            )
            return response
        except ValueError:
            raise
        except Exception:
            raise ValueError("send_uncertain") from None


class ProtectedBridgeApplication:
    def __init__(
        self,
        *,
        ordinary,
        ingress,
        cards,
        ledger,
        send_store: BridgeSendStore,
        broker_credential: str,
        current_owner: Callable[[], int],
        send_report,
        allow_private_uds: bool = False,
        report_summary=None,
        card_views=None,
    ):
        if not isinstance(broker_credential, str) or not 32 <= len(broker_credential) <= 4096:
            raise ValueError("invalid_configuration")
        self._ordinary, self._ingress, self._cards, self._ledger = ordinary, ingress, cards, ledger
        self._store, self._broker, self._owner = send_store, broker_credential, current_owner
        self._send_report = send_report
        self._report_summary = report_summary
        self._card_views = card_views
        self._private = allow_private_uds

    def __repr__(self):
        return "ProtectedBridgeApplication(secrets=[REDACTED])"

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") not in {
            "/v1/telegram/webhook",
            "/v1/broker/cards",
            "/v1/broker/reports",
            "/v1/broker/alerts",
        }:
            await self._ordinary(scope, receive, send)
            return
        try:
            async with asyncio.timeout(20):
                if (scope.get("scheme") != "https" and not self._private) or scope.get(
                    "method"
                ) != "POST":
                    raise ValueError("forbidden")
                headers = {}
                for key, value in scope.get("headers", []):
                    name = key.decode("ascii").lower()
                    if name in headers:
                        raise ValueError("invalid_request")
                    headers[name] = value.decode("ascii")
                webhook = scope["path"] == "/v1/telegram/webhook"
                if not webhook:
                    if not hmac.compare_digest(
                        headers.get("authorization", ""), "Bearer " + self._broker
                    ):
                        raise ValueError("forbidden")
                body = bytearray()
                while True:
                    event = await receive()
                    if event.get("type") != "http.request":
                        raise ValueError("invalid_request")
                    chunk = event.get("body", b"")
                    if not isinstance(chunk, bytes) or len(body) + len(chunk) > 1024 * 1024:
                        raise ValueError("invalid_request")
                    body.extend(chunk)
                    if not event.get("more_body", False):
                        break
                if webhook:
                    try:
                        await self._ingress.handle(
                            bytes(body),
                            secret_header=headers.get("x-telegram-bot-api-secret-token", ""),
                        )
                    except Exception:
                        raise ValueError("forbidden") from None
                    response = {"ok": True}
                else:
                    value = json.loads(body, object_pairs_hook=_unique)
                    if not isinstance(value, dict):
                        raise ValueError("invalid_request")
                    response = await self._broker_request(scope["path"], value)
                await _respond(send, 200, response)
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            code = (
                str(error)
                if str(error) in {"forbidden", "send_uncertain", "binding_mismatch"}
                else "invalid_request"
            )
            await _respond(
                send,
                403 if code == "forbidden" else 409 if code != "invalid_request" else 400,
                {"error": code},
            )
        except Exception:
            await _respond(send, 503, {"error": "bridge_unavailable"})

    async def _broker_request(self, path: str, value: dict) -> dict:
        expected = (
            {"request_id", "binding"}
            if path.endswith("cards") and set(value) == {"request_id", "binding"}
            else {"request_id", "binding", "presentation"}
            if path.endswith("cards")
            else _ALERT_FIELDS
            if path.endswith("alerts")
            else {"request_id", "approval_id"}
        )
        if set(value) != expected:
            raise ValueError("invalid_request")
        request_id = UUID(value["request_id"])
        if str(request_id) != value["request_id"]:
            raise ValueError("invalid_request")
        if path.endswith("cards"):
            raw = value["binding"]
            if not isinstance(raw, dict) or set(raw) != {
                "repository_id",
                "owner_id",
                "pr_number",
                "target",
                "base_sha",
                "head_sha",
                "head_tree",
                "deployed_identity",
                "policy_digest",
                "review_bundle",
            }:
                raise ValueError("invalid_request")
            binding = ReleaseBinding(
                **{**raw, "deployed_identity": DeploymentIdentity(**raw["deployed_identity"])}
            )
            try:
                presentation = (
                    CardPresentation.from_json(value["presentation"], binding)
                    if "presentation" in value
                    else None
                )
            except Exception:
                raise ValueError("invalid_request") from None

            async def effect():
                receipt = await self._cards.issue(
                    binding=binding,
                    owner_user_id=self._owner(),
                    summary="",
                    presentation=presentation,
                )
                if presentation is not None and self._card_views is not None:
                    try:
                        patch = (
                            presentation.patch.encode("utf-8")
                            if presentation.patch is not None
                            else None
                        )
                        await self._card_views.put(
                            receipt.approval_id,
                            presentation.to_json(),
                            patch,
                            presentation.patch_state,
                        )
                    except Exception as exc:
                        _LOG.warning(
                            "release card view store failed error_class=%s", type(exc).__name__
                        )
                return {
                    "approval_id": str(receipt.approval_id),
                    "target": receipt.target,
                    "sequence": receipt.sequence,
                    "status": receipt.status.value,
                    "binding_digest": receipt.binding_digest,
                    "expires_at": receipt.expires_at.isoformat(),
                }
        elif path.endswith("alerts"):
            kind, service, state = value["kind"], value["service"], value["state"]
            if (
                not isinstance(kind, str)
                or kind not in _ALERT_LABELS
                or not isinstance(service, str)
                or service not in _ALERT_SERVICES
                or not isinstance(state, str)
                or state not in {"firing", "resolved"}
                or not isinstance(value["request_id"], str)
            ):
                raise ValueError("invalid_request")
            request_id = UUID(value["request_id"])
            if str(request_id) != value["request_id"]:
                raise ValueError("invalid_request")
            label = _ALERT_LABELS[kind]
            if kind in {"service_inactive", "systemd_unavailable"} and service not in {
                "bridge",
                "broker",
                "host",
                "worker",
            }:
                raise ValueError("invalid_request")
            if (
                kind
                in {
                    "cpu_pressure",
                    "load_pressure",
                    "memory_pressure",
                    "disk_pressure",
                    "metrics_unavailable",
                }
                and service != "metrics"
            ):
                raise ValueError("invalid_request")
            if (
                kind
                in {
                    "host_state_unavailable",
                    "host_unhealthy",
                }
                and service != "host"
            ):
                raise ValueError("invalid_request")
            if kind in {"journal_unavailable", "journal_deadline", "journal_recovery_failed"} and (
                service != "journal"
            ):
                raise ValueError("invalid_request")

            async def effect():
                owner = self._owner()
                if type(owner) is not int or owner <= 0 or not callable(self._send_report):
                    raise ValueError("invalid_request")
                message_id = await self._send_report(
                    owner,
                    f"Release-control alert: {label} on {_ALERT_SERVICE_LABELS[service]} {state}.",
                )
                if type(message_id) is not int or message_id <= 0:
                    raise ValueError("invalid_request")
                return {"request_id": str(request_id), "state": state, "message_id": message_id}

            return await self._store.send_once(request_id, {"route": path, **value}, effect)
        else:
            approval_id = UUID(value["approval_id"])
            card = await self._ledger.get_card(approval_id)
            if card.owner_user_id != self._owner():
                raise ValueError("forbidden")
            observation = await self._report_summary(card) if self._report_summary else {}

            async def effect():
                text = (
                    f"Release {card.approval_id} · PR #{card.binding['pr_number']}\n"
                    f"H: {card.binding['head_sha']}\nStatus: {card.status.value}"
                )
                if card.host_receipt_id:
                    text += f"\nHost receipt: {card.host_receipt_id}"
                for key, allowed in (
                    ("dispatch", _DISPATCH_STATES),
                    ("run", _RUN_STATES),
                    ("conclusion", _CONCLUSIONS),
                ):
                    if key in observation:
                        if observation[key] not in allowed:
                            raise ValueError("invalid_request")
                        text += f"\n{key.title()}: {observation[key]}"
                if observation.get("dispatch") in {"sending", "uncertain"}:
                    text += "\nDispatch outcome requires reconciliation."
                message_id = await self._send_report(card.owner_user_id, text)
                return {
                    "approval_id": str(card.approval_id),
                    "status": card.status.value,
                    "message_id": message_id,
                }

        return await self._store.send_once(request_id, {"route": path, **value}, effect)


async def _respond(send, status, value):
    body = _canonical(value)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
