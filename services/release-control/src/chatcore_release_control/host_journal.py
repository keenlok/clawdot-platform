"""Durable local journal primitives for the protected host deployment service.

This module records host operation intent and recovery observations. It does not
authorize an operation, contact a broker, inspect containers, or perform a deploy.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from chatcore_release_control.policy import (
    DeploymentIdentity,
    JournalState,
    validate_journal_transition,
)

MAX_START_WINDOW_SECONDS = 120
MAX_JOURNAL_BYTES = 64 * 1024
_OPERATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class HostJournalError(RuntimeError):
    """A bounded, non-sensitive journal error."""

    code = "host_journal_error"

    def __init__(self, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(self.code)


class OperationConflict(HostJournalError):
    """The journal is bound to a different operation or request."""

    code = "operation_conflict"


class JournalNotFound(HostJournalError):
    """No journaled operation exists yet."""

    code = "journal_not_found"


class ReconciliationRequired(HostJournalError):
    """Observed host state is insufficient to make a safe recovery decision."""

    code = "reconciliation_required"


class UnsafeTransition(HostJournalError):
    """A requested state transition is invalid or unsafe."""

    code = "unsafe_transition"


class ReconciliationAction(StrEnum):
    SWITCH_ALLOWED = "switch_allowed"
    SWITCH_NOT_STARTED_EXPIRED = "switch_not_started_expired"
    VERIFY_PAYLOAD = "verify_payload"
    RESTORE_ALLOWED = "restore_allowed"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    ALREADY_TERMINAL = "already_terminal"


@dataclass(frozen=True)
class JournalRecord:
    operation_id: str
    baseline: DeploymentIdentity
    payload: DeploymentIdentity
    payload_digest: str
    start_deadline: int
    recovery_deadline: int
    phase: JournalState
    receipt: str | None = None
    terminal_at: int | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    action: ReconciliationAction
    record: JournalRecord
    observed_actual: DeploymentIdentity | None = None
    observed_healthy: bool | None = None


def _identity_data(identity: DeploymentIdentity) -> dict[str, object]:
    return {
        "git_sha": identity.git_sha,
        "image_digest": identity.image_digest,
        "config_generation": identity.config_generation,
        "config_digest": identity.config_digest,
        "policy_digest": identity.policy_digest,
        "target_sequence": identity.target_sequence,
    }


def _payload_digest(payload: DeploymentIdentity) -> str:
    encoded = json.dumps(
        _identity_data(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _receipt(
    operation_id: str,
    phase: JournalState,
    baseline: DeploymentIdentity,
    payload_digest: str,
    start_deadline: int,
    recovery_deadline: int,
    terminal_at: int,
) -> str:
    receipt_data = json.dumps(
        {
            "operation_id": operation_id,
            "phase": phase.value,
            "baseline": _identity_data(baseline),
            "payload_digest": payload_digest,
            "start_deadline": start_deadline,
            "recovery_deadline": recovery_deadline,
            "terminal_at": terminal_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(receipt_data).hexdigest()


def _record_data(record: JournalRecord) -> dict[str, object]:
    return {
        "version": 2,
        "operation_id": record.operation_id,
        "baseline": _identity_data(record.baseline),
        "payload": _identity_data(record.payload),
        "payload_digest": record.payload_digest,
        "start_deadline": record.start_deadline,
        "recovery_deadline": record.recovery_deadline,
        "phase": record.phase.value,
        "receipt": record.receipt,
        "terminal_at": record.terminal_at,
    }


def _is_terminal(record: JournalRecord) -> bool:
    return record.phase in {
        JournalState.HEALTHY,
        JournalState.RESTORED_HEALTHY,
        JournalState.RECOVERY_FAILED,
    }


def _validate_operation_id(operation_id: object) -> str:
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise UnsafeTransition("invalid_operation_id")
    return operation_id


def _no_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _decode_identity(value: object) -> DeploymentIdentity:
    fields = {
        "git_sha",
        "image_digest",
        "config_generation",
        "config_digest",
        "policy_digest",
        "target_sequence",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("invalid identity")
    return DeploymentIdentity(**value)  # type: ignore[arg-type]


def _decode_record(raw: bytes) -> JournalRecord:
    if len(raw) > MAX_JOURNAL_BYTES:
        raise ValueError("oversized journal")
    value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_no_duplicate_pairs)
    expected = {
        "version",
        "operation_id",
        "baseline",
        "payload",
        "payload_digest",
        "start_deadline",
        "recovery_deadline",
        "phase",
        "receipt",
        "terminal_at",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or type(value["version"]) is not int
        or value["version"] != 2
    ):
        raise ValueError("invalid journal")
    operation_id = value["operation_id"]
    start_deadline = value["start_deadline"]
    recovery_deadline = value["recovery_deadline"]
    digest = value["payload_digest"]
    receipt = value["receipt"]
    terminal_at = value["terminal_at"]
    if (
        not isinstance(operation_id, str)
        or not _OPERATION_ID.fullmatch(operation_id)
        or type(start_deadline) is not int
        or start_deadline < 0
        or type(recovery_deadline) is not int
        or recovery_deadline <= start_deadline
        or not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
        or (receipt is not None and (not isinstance(receipt, str) or len(receipt) > 128))
        or (terminal_at is not None and (type(terminal_at) is not int or terminal_at < 0))
    ):
        raise ValueError("invalid journal")
    baseline = _decode_identity(value["baseline"])
    payload = _decode_identity(value["payload"])
    if _payload_digest(payload) != digest:
        raise ValueError("invalid payload digest")
    phase = JournalState(value["phase"])
    terminal = phase in {
        JournalState.HEALTHY,
        JournalState.RESTORED_HEALTHY,
        JournalState.RECOVERY_FAILED,
    }
    if (
        terminal != (receipt is not None)
        or terminal != (terminal_at is not None)
        or (
            terminal
            and receipt
            != _receipt(
                operation_id,
                phase,
                baseline,
                digest,
                start_deadline,
                recovery_deadline,
                terminal_at,
            )
        )
    ):
        raise ValueError("invalid receipt")
    return JournalRecord(
        operation_id=operation_id,
        baseline=baseline,
        payload=payload,
        payload_digest=digest,
        start_deadline=start_deadline,
        recovery_deadline=recovery_deadline,
        phase=phase,
        receipt=receipt,
        terminal_at=terminal_at,
    )


class HostJournal:
    """A single-operation durable journal guarded by an OS advisory lock."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_start_window_seconds: int = MAX_START_WINDOW_SECONDS,
        max_recovery_window_seconds: int = 600,
    ) -> None:
        if (
            type(max_start_window_seconds) is not int
            or not 0 < max_start_window_seconds <= MAX_START_WINDOW_SECONDS
            or type(max_recovery_window_seconds) is not int
            or not 1 <= max_recovery_window_seconds <= 3600
        ):
            raise ValueError("invalid start window")
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.max_start_window_seconds = max_start_window_seconds
        self.max_recovery_window_seconds = max_recovery_window_seconds

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        flags |= os.O_NONBLOCK
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise HostJournalError("journal_lock_failed") from exc
        locked = False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise HostJournalError("journal_lock_invalid")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            try:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _read_locked(self) -> JournalRecord | None:
        try:
            flags = os.O_RDONLY | os.O_NONBLOCK
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise HostJournalError("journal_read_failed") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("journal is not a regular file")
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(descriptor, 8192):
                size += len(chunk)
                if size > MAX_JOURNAL_BYTES:
                    raise ValueError("oversized journal")
                chunks.append(chunk)
            return _decode_record(b"".join(chunks))
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            raise HostJournalError("journal_corrupt") from exc
        finally:
            os.close(descriptor)

    def _write_locked(self, record: JournalRecord) -> None:
        data = json.dumps(
            _record_data(record), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        temporary_path: str | None = None
        descriptor = -1
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise HostJournalError("journal_write_failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def _receipt_dir(self, *, create: bool) -> Path | None:
        directory = self.path.with_name(self.path.name + ".receipts")
        created = False
        try:
            if create:
                try:
                    directory.mkdir(mode=0o700)
                    created = True
                except FileExistsError:
                    pass
            info = directory.lstat()
        except FileNotFoundError:
            if create:
                raise HostJournalError("receipt_archive_failed") from None
            return None
        except OSError as exc:
            raise HostJournalError("receipt_archive_failed") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise HostJournalError("receipt_archive_invalid")
        if created:
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                raise HostJournalError("receipt_archive_failed") from exc
            finally:
                os.close(directory_fd)
        return directory

    def _read_archive_locked(self, operation_id: str) -> JournalRecord | None:
        _validate_operation_id(operation_id)
        directory = self._receipt_dir(create=False)
        if directory is None:
            return None
        archive_path = directory / f"{operation_id}.json"
        try:
            flags = os.O_RDONLY | os.O_NONBLOCK
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(archive_path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise HostJournalError("receipt_archive_read_failed") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("receipt is not a regular file")
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(descriptor, 8192):
                size += len(chunk)
                if size > MAX_JOURNAL_BYTES:
                    raise ValueError("oversized receipt")
                chunks.append(chunk)
            record = _decode_record(b"".join(chunks))
            if record.operation_id != operation_id or not _is_terminal(record):
                raise ValueError("invalid archived receipt")
            return record
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            raise HostJournalError("receipt_archive_corrupt") from exc
        finally:
            os.close(descriptor)

    def _write_archive_locked(self, record: JournalRecord) -> None:
        directory = self._receipt_dir(create=True)
        if directory is None:  # pragma: no cover - create=True always returns a directory.
            raise HostJournalError("receipt_archive_failed")
        archive_path = directory / f"{record.operation_id}.json"
        existing = self._read_archive_locked(record.operation_id)
        if existing is not None:
            if existing != record:
                raise OperationConflict("receipt_archive_conflict")
            return

        data = json.dumps(
            _record_data(record), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        descriptor = -1
        temporary_path: str | None = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{record.operation_id}.", suffix=".tmp", dir=directory
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_path, archive_path, follow_symlinks=False)
            except FileExistsError:
                existing = self._read_archive_locked(record.operation_id)
                if existing != record:
                    raise OperationConflict("receipt_archive_conflict") from None
            os.unlink(temporary_path)
            temporary_path = None
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OperationConflict:
            raise
        except OSError as exc:
            raise HostJournalError("receipt_archive_write_failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def _clear_current_locked(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise HostJournalError("journal_clear_failed") from exc
        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise HostJournalError("journal_clear_failed") from exc
        finally:
            os.close(directory_fd)

    def prepare(
        self,
        *,
        operation_id: str,
        baseline: DeploymentIdentity,
        payload: DeploymentIdentity,
        start_deadline: int,
        recovery_deadline: int,
        now: int,
    ) -> JournalRecord:
        if (
            not isinstance(operation_id, str)
            or not _OPERATION_ID.fullmatch(operation_id)
            or not isinstance(baseline, DeploymentIdentity)
            or not isinstance(payload, DeploymentIdentity)
            or type(start_deadline) is not int
            or type(recovery_deadline) is not int
            or type(now) is not int
            or start_deadline < 0
            or recovery_deadline < 0
        ):
            raise UnsafeTransition("invalid_prepare")
        digest = _payload_digest(payload)
        with self._locked():
            current = self._read_locked()
            if current is not None:
                if (
                    current.operation_id == operation_id
                    and current.baseline == baseline
                    and current.payload == payload
                    and current.start_deadline == start_deadline
                    and current.recovery_deadline == recovery_deadline
                ):
                    return current
                archived = self._read_archive_locked(operation_id)
                if (
                    archived is not None
                    and archived.baseline == baseline
                    and archived.payload == payload
                    and archived.start_deadline == start_deadline
                    and archived.recovery_deadline == recovery_deadline
                ):
                    return archived
                raise OperationConflict()
            archived = self._read_archive_locked(operation_id)
            if archived is not None:
                if (
                    archived.baseline == baseline
                    and archived.payload == payload
                    and archived.start_deadline == start_deadline
                    and archived.recovery_deadline == recovery_deadline
                ):
                    return archived
                raise OperationConflict()
            if (
                not now < start_deadline <= now + self.max_start_window_seconds
                or not start_deadline < recovery_deadline <= now + self.max_recovery_window_seconds
            ):
                raise UnsafeTransition("invalid_prepare")
            record = JournalRecord(
                operation_id=operation_id,
                baseline=baseline,
                payload=payload,
                payload_digest=digest,
                start_deadline=start_deadline,
                recovery_deadline=recovery_deadline,
                phase=JournalState.PREPARED,
            )
            self._write_locked(record)
            return record

    def get_record(self, operation_id: str) -> JournalRecord:
        _validate_operation_id(operation_id)
        with self._locked():
            current = self._read_locked()
            if current is not None and current.operation_id == operation_id:
                return current
            archived = self._read_archive_locked(operation_id)
            if archived is not None:
                return archived
            if current is None:
                raise JournalNotFound()
            raise OperationConflict()

    def current_record(self) -> JournalRecord | None:
        """Read the live record without reconciling, archiving, or mutating it."""
        with self._locked():
            return self._read_locked()

    def archive_terminal(self, operation_id: str, receipt: str | None) -> JournalRecord:
        """Archive an exact terminal receipt before allowing another operation."""
        _validate_operation_id(operation_id)
        with self._locked():
            current = self._read_locked()
            if current is None or current.operation_id != operation_id:
                archived = self._read_archive_locked(operation_id)
                if archived is not None and archived.receipt == receipt:
                    return archived
                raise OperationConflict()
            if not _is_terminal(current) or current.receipt != receipt:
                raise UnsafeTransition("terminal_receipt_mismatch")
            self._write_archive_locked(current)
            self._clear_current_locked()
            return current

    def begin_switch(self, operation_id: str, *, now: int) -> JournalRecord:
        with self._locked():
            current = self._require_operation_locked(operation_id)
            if _is_terminal(current):
                if type(now) is not int:
                    raise UnsafeTransition("invalid_time")
                return current
            if current.phase is not JournalState.PREPARED or type(now) is not int:
                raise UnsafeTransition()
            if now >= current.recovery_deadline:
                raise UnsafeTransition("recovery_deadline_expired")
            if now >= current.start_deadline:
                raise UnsafeTransition("start_deadline_expired")
            updated = self._transition_record(current, JournalState.SWITCHING)
            self._write_locked(updated)
            return updated

    def reconcile(
        self,
        operation_id: str,
        *,
        actual: DeploymentIdentity | None,
        healthy: bool | None = None,
        now: int,
    ) -> ReconciliationResult:
        if type(now) is not int or (healthy is not None and type(healthy) is not bool):
            raise ReconciliationRequired()
        with self._locked():
            current = self._require_operation_locked(operation_id)
            if current.phase in {
                JournalState.HEALTHY,
                JournalState.RESTORED_HEALTHY,
                JournalState.RECOVERY_FAILED,
            }:
                return ReconciliationResult(
                    ReconciliationAction.ALREADY_TERMINAL,
                    current,
                    actual,
                    healthy,
                )
            if actual is None or not isinstance(actual, DeploymentIdentity):
                raise ReconciliationRequired()
            if now >= current.recovery_deadline:
                return ReconciliationResult(
                    ReconciliationAction.RECONCILIATION_REQUIRED,
                    current,
                    actual,
                    healthy,
                )

            if current.phase is JournalState.PREPARED:
                if actual != current.baseline:
                    raise ReconciliationRequired("unexpected_prepared_state")
                action = (
                    ReconciliationAction.SWITCH_NOT_STARTED_EXPIRED
                    if now >= current.start_deadline
                    else ReconciliationAction.SWITCH_ALLOWED
                )
                return ReconciliationResult(action, current, actual, healthy)

            if current.phase is JournalState.SWITCHING:
                if actual == current.payload:
                    updated = self._transition_record(current, JournalState.CHECKING)
                    self._write_locked(updated)
                    return ReconciliationResult(ReconciliationAction.VERIFY_PAYLOAD, updated)
                if actual == current.baseline:
                    raise ReconciliationRequired("switch_state_ambiguous")
                raise ReconciliationRequired("unexpected_switch_state")

            if current.phase is JournalState.CHECKING:
                if actual != current.payload:
                    if actual == current.baseline:
                        raise ReconciliationRequired("restore_state_unverified")
                    updated = self._finish(current, JournalState.RECOVERY_FAILED, now)
                    self._write_locked(updated)
                    return ReconciliationResult(ReconciliationAction.ALREADY_TERMINAL, updated)
                if healthy is None:
                    raise ReconciliationRequired("health_unknown")
                if healthy:
                    updated = self._finish(current, JournalState.HEALTHY, now)
                    self._write_locked(updated)
                    return ReconciliationResult(ReconciliationAction.ALREADY_TERMINAL, updated)
                updated = self._transition_record(current, JournalState.RESTORING)
                self._write_locked(updated)
                return ReconciliationResult(ReconciliationAction.RESTORE_ALLOWED, updated)

            if current.phase is JournalState.RESTORING:
                if actual == current.payload:
                    return ReconciliationResult(ReconciliationAction.RESTORE_ALLOWED, current)
                if actual != current.baseline:
                    updated = self._finish(current, JournalState.RECOVERY_FAILED, now)
                    self._write_locked(updated)
                    return ReconciliationResult(ReconciliationAction.ALREADY_TERMINAL, updated)
                if healthy is None:
                    raise ReconciliationRequired("health_unknown")
                phase = JournalState.RESTORED_HEALTHY if healthy else JournalState.RECOVERY_FAILED
                updated = self._finish(current, phase, now)
                self._write_locked(updated)
                return ReconciliationResult(ReconciliationAction.ALREADY_TERMINAL, updated)

            raise ReconciliationRequired("unsupported_phase")

    def _require_operation_locked(self, operation_id: str) -> JournalRecord:
        _validate_operation_id(operation_id)
        current = self._read_locked()
        if current is not None and current.operation_id == operation_id:
            return current
        archived = self._read_archive_locked(operation_id)
        if archived is not None:
            return archived
        if current is None:
            raise JournalNotFound()
        raise OperationConflict()

    @staticmethod
    def _transition_record(current: JournalRecord, requested: JournalState) -> JournalRecord:
        if not validate_journal_transition(
            current.phase,
            requested,
            current_operation_id=current.operation_id,
            requested_operation_id=current.operation_id,
        ):
            raise UnsafeTransition()
        return replace(current, phase=requested)

    @staticmethod
    def _finish(current: JournalRecord, phase: JournalState, terminal_at: int) -> JournalRecord:
        if type(terminal_at) is not int or terminal_at < 0:
            raise UnsafeTransition("invalid_time")
        updated = HostJournal._transition_record(current, phase)
        receipt = _receipt(
            current.operation_id,
            phase,
            current.baseline,
            current.payload_digest,
            current.start_deadline,
            current.recovery_deadline,
            terminal_at,
        )
        return replace(updated, receipt=receipt, terminal_at=terminal_at)
