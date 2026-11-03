"""PostgreSQL-backed bounded at-least-once ordinary update relay."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_BATCH = 50
MAX_LEASE_SECONDS = 60
MAX_PENDING = 256
MAX_RETAINED = 4096
RETENTION = timedelta(days=7)
_PRINCIPAL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class RelayError(RuntimeError):
    _CODES = frozenset(
        {
            "invalid_request",
            "payload_too_large",
            "replay_mismatch",
            "relay_full",
            "storage_error",
            "invalid_relay_event",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = code if code in self._CODES else "storage_error"
        super().__init__(self.code)


@dataclass(frozen=True)
class RelayEvent:
    relay_id: UUID
    update_id: int
    payload: dict[str, object]
    attempts: int


def _timestamp(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise RelayError("invalid_request")
    return result.astimezone(UTC)


def _canonical_payload(value: Mapping[str, object]) -> tuple[str, bytes]:
    if not isinstance(value, Mapping):
        raise RelayError("invalid_request")
    try:
        canonical = json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
        encoded = canonical.encode("ascii")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise RelayError("invalid_request") from None
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise RelayError("payload_too_large")
    try:
        decoded = json.loads(encoded)
    except (ValueError, RecursionError):
        raise RelayError("invalid_request") from None
    if not isinstance(decoded, dict):
        raise RelayError("invalid_request")
    return canonical, hashlib.sha256(encoded).digest()


class DurableRelay:
    """Persist updates and redeliver expired leases until a client acknowledges them."""

    def __init__(self, pool: object, *, max_pending_per_principal: int = MAX_PENDING) -> None:
        if (
            not callable(getattr(pool, "acquire", None))
            or type(max_pending_per_principal) is not int
            or not 1 <= max_pending_per_principal <= MAX_PENDING
        ):
            raise RelayError("invalid_request")
        self._pool = pool
        self._max_pending = max_pending_per_principal

    async def enqueue(
        self,
        principal: str,
        update_id: int,
        payload: Mapping[str, object],
        *,
        now: datetime | None = None,
    ) -> UUID:
        principal = self._principal(principal)
        if type(update_id) is not int or update_id <= 0:
            raise RelayError("invalid_request")
        created = _timestamp(now)
        canonical, digest = _canonical_payload(payload)
        if type(payload.get("update_id")) is not int or payload["update_id"] != update_id:
            raise RelayError("invalid_request")
        relay_id = uuid4()
        try:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", principal
                    )
                    existing = await connection.fetchrow(
                        "SELECT relay_id, payload_sha256 FROM release_control.telegram_relay_event "
                        "WHERE principal=$1 AND update_id=$2 FOR UPDATE",
                        principal,
                        update_id,
                    )
                    if existing is not None:
                        if bytes(existing["payload_sha256"]) != digest:
                            raise RelayError("replay_mismatch")
                        return existing["relay_id"]
                    await connection.execute(
                        "DELETE FROM release_control.telegram_relay_event "
                        "WHERE principal=$1 AND acknowledged_at < $2",
                        principal,
                        created - RETENTION,
                    )
                    retained = await connection.fetchval(
                        "SELECT count(*) FROM release_control.telegram_relay_event "
                        "WHERE principal=$1",
                        principal,
                    )
                    if retained >= MAX_RETAINED:
                        raise RelayError("relay_full")
                    pending = await connection.fetchval(
                        "SELECT count(*) FROM release_control.telegram_relay_event "
                        "WHERE principal=$1 AND acknowledged_at IS NULL",
                        principal,
                    )
                    if pending >= self._max_pending:
                        raise RelayError("relay_full")
                    await connection.execute(
                        "INSERT INTO release_control.telegram_relay_event "
                        "(relay_id, principal, update_id, payload, payload_sha256, created_at) "
                        "VALUES ($1, $2, $3, $4::jsonb, $5, $6)",
                        relay_id,
                        principal,
                        update_id,
                        canonical,
                        digest,
                        created,
                    )
                    return relay_id
        except RelayError:
            raise
        except Exception:
            raise RelayError("storage_error") from None

    async def claim(
        self,
        principal: str,
        *,
        limit: int = MAX_BATCH,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> list[RelayEvent]:
        principal = self._principal(principal)
        if (
            type(limit) is not int
            or not 1 <= limit <= MAX_BATCH
            or type(lease_seconds) is not int
            or not 1 <= lease_seconds <= MAX_LEASE_SECONDS
        ):
            raise RelayError("invalid_request")
        instant = _timestamp(now)
        try:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    rows = await connection.fetch(
                        "SELECT relay_id, update_id, payload, attempts "
                        "FROM release_control.telegram_relay_event "
                        "WHERE principal=$1 AND acknowledged_at IS NULL "
                        "AND (lease_until IS NULL OR lease_until <= $2) "
                        "ORDER BY created_at, relay_id FOR UPDATE SKIP LOCKED LIMIT $3",
                        principal,
                        instant,
                        limit,
                    )
                    if not rows:
                        return []
                    ids = [row["relay_id"] for row in rows]
                    await connection.execute(
                        "UPDATE release_control.telegram_relay_event "
                        "SET lease_until=$2, attempts=attempts+1 WHERE relay_id=ANY($1::uuid[])",
                        ids,
                        instant + timedelta(seconds=lease_seconds),
                    )
                    return [
                        RelayEvent(
                            relay_id=row["relay_id"],
                            update_id=row["update_id"],
                            payload=json.loads(row["payload"]),
                            attempts=row["attempts"] + 1,
                        )
                        for row in rows
                    ]
        except RelayError:
            raise
        except Exception:
            raise RelayError("storage_error") from None

    async def ack(
        self,
        principal: str,
        relay_ids: Sequence[UUID],
        *,
        now: datetime | None = None,
    ) -> None:
        principal = self._principal(principal)
        instant = _timestamp(now)
        if (
            not isinstance(relay_ids, Sequence)
            or isinstance(relay_ids, (str, bytes))
            or not 1 <= len(relay_ids) <= MAX_BATCH
            or any(not isinstance(item, UUID) for item in relay_ids)
            or len(set(relay_ids)) != len(relay_ids)
        ):
            raise RelayError("invalid_request")
        try:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    rows = await connection.fetch(
                        "SELECT relay_id, acknowledged_at, lease_until "
                        "FROM release_control.telegram_relay_event "
                        "WHERE principal=$1 AND relay_id=ANY($2::uuid[]) FOR UPDATE",
                        principal,
                        list(relay_ids),
                    )
                    by_id = {row["relay_id"]: row for row in rows}
                    if len(by_id) != len(relay_ids):
                        raise RelayError("invalid_relay_event")
                    if any(
                        row["acknowledged_at"] is None
                        and (row["lease_until"] is None or row["lease_until"] <= instant)
                        for row in by_id.values()
                    ):
                        raise RelayError("invalid_relay_event")
                    await connection.execute(
                        "UPDATE release_control.telegram_relay_event SET acknowledged_at=$3 "
                        "WHERE principal=$1 AND relay_id=ANY($2::uuid[]) "
                        "AND acknowledged_at IS NULL",
                        principal,
                        list(relay_ids),
                        instant,
                    )
        except RelayError:
            raise
        except Exception:
            raise RelayError("storage_error") from None

    @staticmethod
    def _principal(value: str) -> str:
        if not isinstance(value, str) or not _PRINCIPAL.fullmatch(value):
            raise RelayError("invalid_request")
        return value

    def __repr__(self) -> str:
        return "DurableRelay(pool=[REDACTED])"


__all__ = ["DurableRelay", "RelayError", "RelayEvent"]
