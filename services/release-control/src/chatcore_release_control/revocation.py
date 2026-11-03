"""Append-only revocation records for protected owner commands."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4


class RevocationError(RuntimeError):
    """Sanitized, bounded revocation failure."""

    def __init__(self, code: str = "revocation_denied") -> None:
        allowed = {"revocation_denied", "revocation_conflict"}
        self.code = code if code in allowed else "revocation_denied"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class RevocationReceipt:
    update_id: int
    approval_id: UUID
    target: str
    owner_user_id: int
    receipt_id: UUID
    request_digest: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        if (
            type(self.update_id) is not int
            or self.update_id <= 0
            or not isinstance(self.approval_id, UUID)
            or not isinstance(self.target, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", self.target) is None
            or type(self.owner_user_id) is not int
            or self.owner_user_id <= 0
            or not isinstance(self.receipt_id, UUID)
            or not re.fullmatch(r"[0-9a-f]{64}", self.request_digest)
            or not isinstance(self.recorded_at, datetime)
            or self.recorded_at.tzinfo is None
            or self.recorded_at.utcoffset() != UTC.utcoffset(self.recorded_at)
        ):
            raise RevocationError()

    def __repr__(self) -> str:
        return (
            f"RevocationReceipt(update_id={self.update_id}, approval_id={self.approval_id}, "
            f"receipt_id={self.receipt_id})"
        )


def request_digest(update_id: int, approval_id: UUID, owner_user_id: int) -> str:
    if (
        type(update_id) is not int
        or update_id <= 0
        or not isinstance(approval_id, UUID)
        or type(owner_user_id) is not int
        or owner_user_id <= 0
    ):
        raise RevocationError()
    canonical = json.dumps(
        {
            "approval_id": str(approval_id),
            "owner_user_id": owner_user_id,
            "update_id": update_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


async def is_revoked(connection, approval_id: UUID) -> bool:
    if not isinstance(approval_id, UUID):
        raise RevocationError()
    return bool(
        await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM release_control.broker_release_revocation "
            "WHERE approval_id=$1)",
            approval_id,
        )
    )


class PostgresReleaseRevocationStore:
    """Write a Telegram owner revocation using read locks that serialize with admission."""

    def __init__(self, pool) -> None:
        if pool is None:
            raise RevocationError("revocation_denied")
        self._pool = pool

    def __repr__(self) -> str:
        return "PostgresReleaseRevocationStore(pool=[protected])"

    async def ready(self) -> bool:
        try:
            async with self._pool.acquire() as connection:
                return bool(
                    await connection.fetchval(
                        "SELECT to_regclass('release_control.broker_release_revocation') "
                        "IS NOT NULL AND has_table_privilege(current_user, "
                        "'release_control.broker_release_revocation', 'SELECT') AND "
                        "has_table_privilege(current_user, "
                        "'release_control.broker_release_revocation', 'INSERT') AND "
                        "has_function_privilege(current_user, "
                        "'release_control.lock_release_revocation_context(uuid)', 'EXECUTE')"
                    )
                )
        except Exception:
            return False

    async def revoke(
        self,
        update_id: int,
        approval_id: UUID,
        owner_user_id: int,
        *,
        now: datetime | None = None,
    ) -> RevocationReceipt:
        digest = request_digest(update_id, approval_id, owner_user_id)
        recorded_at = _utc(now or datetime.now(UTC))
        try:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    locked = await connection.fetchrow(
                        "SELECT * FROM release_control.lock_release_revocation_context($1)",
                        approval_id,
                    )
                    if (
                        locked is None
                        or locked["owner_user_id"] != owner_user_id
                        or locked["telegram_owner_user_id"] != owner_user_id
                    ):
                        raise RevocationError()
                    target = locked["target"]
                    existing = await connection.fetchrow(
                        "SELECT * FROM release_control.broker_release_revocation "
                        "WHERE update_id=$1",
                        update_id,
                    )
                    if existing is not None:
                        return _receipt(existing, digest=digest, approval_id=approval_id)
                    receipt_id = uuid4()
                    row = await connection.fetchrow(
                        "INSERT INTO release_control.broker_release_revocation "
                        "(update_id,approval_id,target,owner_user_id,request_digest,"
                        "receipt_id,recorded_at) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *",
                        update_id,
                        approval_id,
                        target,
                        owner_user_id,
                        bytes.fromhex(digest),
                        receipt_id,
                        recorded_at,
                    )
                    return _receipt(row, digest=digest, approval_id=approval_id)
        except RevocationError:
            raise
        except Exception:
            raise RevocationError() from None


def _receipt(row, *, digest: str, approval_id: UUID) -> RevocationReceipt:
    try:
        if (
            row["approval_id"] != approval_id
            or bytes(row["request_digest"]).hex() != digest
            or not isinstance(row["receipt_id"], UUID)
        ):
            raise RevocationError("revocation_conflict")
        return RevocationReceipt(
            update_id=row["update_id"],
            approval_id=row["approval_id"],
            target=row["target"],
            owner_user_id=row["owner_user_id"],
            receipt_id=row["receipt_id"],
            request_digest=bytes(row["request_digest"]).hex(),
            recorded_at=_utc(row["recorded_at"]),
        )
    except RevocationError:
        raise
    except Exception:
        raise RevocationError() from None


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RevocationError()
    try:
        normalized = value.astimezone(UTC)
    except (OverflowError, OSError, ValueError):
        raise RevocationError() from None
    if normalized != value:
        raise RevocationError()
    return normalized


__all__ = [
    "PostgresReleaseRevocationStore",
    "RevocationError",
    "RevocationReceipt",
    "is_revoked",
    "request_digest",
]
