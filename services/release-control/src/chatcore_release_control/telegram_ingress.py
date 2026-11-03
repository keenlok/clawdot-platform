"""Protected Telegram webhook routing before the ordinary application relay."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from .ledger import CardRecord, Decision, DecisionCommand, DecisionResult
from .revocation import RevocationReceipt

MAX_UPDATE_BYTES = 1024 * 1024
_CALLBACK = re.compile(r"release:([ar]):([A-Za-z0-9_-]{32,52})\Z")
_VIEW_CALLBACK = re.compile(r"release:([iv])\Z")
_REVOKE = re.compile(
    r"/release_revoke ([0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\Z"
)
_REVOKE_PREFIX = "/release_revoke"
_FORWARD_FIELDS = frozenset(
    {
        "forward_origin",
        "forward_from",
        "forward_from_chat",
        "forward_sender_name",
        "is_automatic_forward",
        "forward_date",
    }
)
_NON_TEXT_FIELDS = frozenset(
    {
        "caption",
        "photo",
        "document",
        "audio",
        "video",
        "video_note",
        "voice",
        "animation",
        "sticker",
        "contact",
        "location",
        "venue",
        "poll",
        "game",
        "reply_to_message",
        "message_thread_id",
        "is_topic_message",
    }
)


class IngressError(ValueError):
    def __init__(self) -> None:
        super().__init__("update_denied")


class DecisionLedger(Protocol):
    async def get_card_by_message(self, chat_id: int, message_id: int) -> CardRecord: ...
    async def record_decision(self, command: DecisionCommand) -> DecisionResult: ...


class RevocationStore(Protocol):
    async def revoke(
        self, update_id: int, approval_id: UUID, owner_user_id: int
    ) -> RevocationReceipt: ...


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise IngressError()
        value[key] = item
    return value


def _body(raw: bytes) -> dict[str, object]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_UPDATE_BYTES:
        raise IngressError()
    value = json.loads(raw, object_pairs_hook=_pairs)
    if not isinstance(value, dict) or type(value.get("update_id")) is not int:
        raise IngressError()
    if value["update_id"] <= 0:
        raise IngressError()
    return value


def verify_relay_body(raw: bytes, signature: str, key: bytes) -> dict[str, object]:
    """Ordinary ingress must verify bridge authentication before parsing an update."""
    try:
        if not isinstance(key, bytes) or len(key) < 32 or not isinstance(signature, str):
            raise IngressError()
        expected = hmac.new(key, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise IngressError()
        return _body(raw)
    except Exception:
        raise IngressError() from None


class TelegramIngress:
    def __init__(
        self,
        *,
        webhook_secret: str,
        relay_key: bytes,
        current_owner: Callable[[], int],
        ledger: DecisionLedger,
        acknowledge: Callable[[str], Awaitable[None]],
        relay: Callable[[bytes, str], Awaitable[None]],
        revocations: RevocationStore | None = None,
        report_revocation: Callable[[int, str, str], Awaitable[None]] | None = None,
        card_views: Callable[[str, CardRecord], Awaitable[None]] | None = None,
    ) -> None:
        if (
            not isinstance(webhook_secret, str)
            or not 32 <= len(webhook_secret) <= 256
            or re.fullmatch(r"[A-Za-z0-9_-]+", webhook_secret) is None
            or not isinstance(relay_key, bytes)
            or len(relay_key) < 32
            or relay_key == webhook_secret.encode()
            or (revocations is None) != (report_revocation is None)
            or (revocations is not None and not callable(getattr(revocations, "revoke", None)))
        ):
            raise IngressError()
        self._webhook_secret = webhook_secret
        self._relay_key = relay_key
        self._current_owner = current_owner
        self._ledger = ledger
        self._acknowledge = acknowledge
        self._relay = relay
        self._revocations = revocations
        self._report_revocation = report_revocation
        self._card_views = card_views

    def __repr__(self) -> str:
        return "TelegramIngress(secrets=[REDACTED])"

    async def handle(self, raw: bytes, *, secret_header: str) -> str:
        """Call only from the configured HTTPS webhook route, never ordinary RPC."""
        try:
            if not isinstance(secret_header, str) or not hmac.compare_digest(
                self._webhook_secret, secret_header
            ):
                raise IngressError()
            update = _body(raw)
            message = update.get("message")
            if isinstance(message, dict):
                message_text = message.get("text")
                caption = message.get("caption")
                if isinstance(message_text, str) and message_text.startswith(_REVOKE_PREFIX):
                    await self._handle_revocation(update, message, message_text)
                    return "revocation"
                if isinstance(caption, str) and caption.startswith(_REVOKE_PREFIX):
                    await self._handle_revocation(update, message, caption)
                    return "revocation"
            callback = update.get("callback_query")
            data = callback.get("data") if isinstance(callback, dict) else None
            if isinstance(data, str) and data.startswith("release:"):
                match = _CALLBACK.fullmatch(data)
                view_match = _VIEW_CALLBACK.fullmatch(data)
                owner = self._current_owner()
                if (match is None and view_match is None) or type(owner) is not int or owner <= 0:
                    raise IngressError()
                sender = callback.get("from")
                message = callback.get("message")
                if not isinstance(sender, dict) or not isinstance(message, dict):
                    raise IngressError()
                chat = message.get("chat")
                message_id = message.get("message_id")
                query_id = callback.get("id")
                if (
                    type(sender.get("id")) is not int
                    or sender["id"] != owner
                    or not isinstance(chat, dict)
                    or chat.get("type") != "private"
                    or type(chat.get("id")) is not int
                    or chat["id"] != owner
                    or type(message_id) is not int
                    or message_id <= 0
                    or not isinstance(query_id, str)
                    or not 1 <= len(query_id) <= 256
                ):
                    raise IngressError()
                card = await self._ledger.get_card_by_message(owner, message_id)
                if view_match is not None:
                    if self._card_views is None:
                        raise IngressError()
                    await self._card_views("details" if view_match[1] == "i" else "diff", card)
                    await self._acknowledge(query_id)
                    return "release_view"
                command = DecisionCommand(
                    update_id=update["update_id"],
                    approval_id=card.approval_id,
                    owner_user_id=owner,
                    chat_id=owner,
                    message_id=message_id,
                    nonce=match[2],
                    decision=Decision.APPROVE if match[1] == "a" else Decision.REJECT,
                    binding_digest=card.binding_digest,
                )
                await self._ledger.record_decision(command)
                # Commit/dedup/outbox must finish before acknowledging Telegram.
                await self._acknowledge(query_id)
                return "release"
            canonical = json.dumps(
                update, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
            signature = hmac.new(self._relay_key, canonical, hashlib.sha256).hexdigest()
            await self._relay(canonical, signature)
            return "ordinary"
        except Exception:
            raise IngressError() from None

    async def _handle_revocation(
        self, update: dict[str, object], message: dict[str, object], text: str
    ) -> None:
        match = _REVOKE.fullmatch(text)
        if match is None or self._revocations is None or self._report_revocation is None:
            raise IngressError()
        try:
            approval_id = UUID(match.group(1))
        except ValueError:
            raise IngressError() from None
        if str(approval_id) != match.group(1):
            raise IngressError()
        sender = message.get("from")
        chat = message.get("chat")
        owner = self._current_owner()
        if (
            not isinstance(sender, dict)
            or type(owner) is not int
            or owner <= 0
            or type(sender.get("id")) is not int
            or sender["id"] != owner
            or sender.get("is_bot") is not False
            or not isinstance(chat, dict)
            or chat.get("type") != "private"
            or type(chat.get("id")) is not int
            or chat["id"] != owner
            or any(key in message for key in _FORWARD_FIELDS | _NON_TEXT_FIELDS)
        ):
            raise IngressError()
        receipt = await self._revocations.revoke(update["update_id"], approval_id, owner)
        if (
            not isinstance(receipt, RevocationReceipt)
            or receipt.update_id != update["update_id"]
            or receipt.approval_id != approval_id
            or receipt.owner_user_id != owner
        ):
            raise IngressError()
        await self._report_revocation(owner, str(approval_id), str(receipt.receipt_id))
