"""Bounded job admission, cancellation and execution lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from datetime import UTC, datetime
from typing import Protocol

from .journal import JobJournal
from .models import CreateJobRequest, validate_expiry


class WorkerFault(Exception):
    def __init__(self, category: str, *, phase: str = "blocked") -> None:
        self.category = category
        self.phase = phase
        super().__init__(category)


class CancelledAtBoundary(Exception):
    """Raised when cancellation is observed before a possible mutating interaction."""


class Engine(Protocol):
    async def run(self, request, operation, cancel_event, before_mutation) -> str: ...


class WorkerRuntime:
    def __init__(
        self,
        journal: JobJournal,
        engine: Engine,
        *,
        editor_email: str,
        job_timeout_seconds: int = 90,
    ) -> None:
        if not 1 <= job_timeout_seconds <= 90:
            raise ValueError("worker job deadline must be between 1 and 90 seconds")
        self.journal = journal
        self.engine = engine
        self.editor_email = editor_email
        self.job_timeout_seconds = job_timeout_seconds
        self._queue: deque[str] = deque()
        self._queue_changed = asyncio.Event()
        self._operations: dict[str, CreateJobRequest] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._submission_lock = asyncio.Lock()
        self._current_id: str | None = None
        self._runner: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False

    @staticmethod
    def _payload_hash(request: CreateJobRequest) -> str:
        return hashlib.sha256(request.model_dump_json().encode("utf-8")).hexdigest()

    async def submit(self, request: CreateJobRequest) -> tuple[str, object]:
        if request.editor_email != self.editor_email:
            raise WorkerFault("wrong_editor_account", phase="blocked")
        async with self._submission_lock:
            if self._closing:
                raise WorkerFault("worker_stopping", phase="blocked")
            payload_hash = self._payload_hash(request)
            existing = self.journal.get(request.job_id)
            if existing:
                return (
                    "replay" if existing.payload_hash == payload_hash else "conflict",
                    existing,
                )
            if self.journal.get_tombstone(request.job_id):
                return "tombstoned", None
            try:
                expiry = validate_expiry(request.expires_at)
            except ValueError as exc:
                raise WorkerFault("expired", phase="blocked") from exc
            result = self.journal.reserve(
                request.job_id,
                payload_hash,
                request.destination_file_id,
                expiry,
            )
            if result.kind == "accepted":
                self._operations[request.job_id] = request
                self._cancel_events[request.job_id] = asyncio.Event()
                self._queue.append(request.job_id)
                self._queue_changed.set()
                if self._runner is None or self._runner.done():
                    self._runner = asyncio.create_task(self._run_queue(), name="browser-job-queue")
            return result.kind, result.job

    async def _run_queue(self) -> None:
        while not self._closing:
            async with self._submission_lock:
                if self._queue:
                    job_id = self._queue.popleft()
                    self._current_id = job_id
                else:
                    self._queue_changed.clear()
                    job_id = None
            if job_id is None:
                await self._queue_changed.wait()
                continue
            record = self.journal.get(job_id)
            if record and record.phase == "accepted":
                await self._run_one(job_id)
            else:
                self._operations.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
                self._current_id = None

    async def _run_one(self, job_id: str) -> None:
        request = self._operations.get(job_id)
        cancel_event = self._cancel_events.get(job_id)
        record = self.journal.get(job_id)
        if request is None or cancel_event is None or record is None:
            if record and record.phase == "accepted":
                self.journal.transition(
                    job_id, "failed_before_edit", error_category="operation_unavailable"
                )
            return
        self._current_id = job_id

        async def before_mutation() -> None:
            if cancel_event.is_set():
                raise CancelledAtBoundary()
            latest = self.journal.get(job_id)
            if latest is None or latest.phase not in {"accepted", "running"}:
                raise CancelledAtBoundary()
            if latest.phase == "accepted":
                self.journal.transition(job_id, "running")

        try:
            remaining = (request.expires_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                self.journal.transition(job_id, "failed_before_edit", error_category="expired")
                return
            async with asyncio.timeout(min(remaining, self.job_timeout_seconds)):
                phase = await self.engine.run(
                    request, request.operation, cancel_event, before_mutation
                )
            latest = self.journal.get(job_id)
            if latest and cancel_event.is_set() and latest.phase == "running":
                self.journal.transition(job_id, "uncertain", error_category="cancelled_after_write")
            elif latest and latest.phase == "accepted" and phase == "awaiting_verification":
                self.journal.transition(job_id, "blocked", error_category="mutation_not_journaled")
            elif latest and latest.phase == "running":
                if phase == "awaiting_verification":
                    self.journal.transition(job_id, phase)
                else:
                    self.journal.transition(job_id, "uncertain", error_category="worker_incomplete")
        except CancelledAtBoundary:
            latest = self.journal.get(job_id)
            if latest and latest.phase == "accepted":
                self.journal.transition(job_id, "failed_before_edit", error_category="cancelled")
            elif latest and latest.phase == "running":
                self.journal.transition(job_id, "uncertain", error_category="cancelled_after_write")
        except WorkerFault as fault:
            latest = self.journal.get(job_id)
            if latest and latest.phase == "accepted":
                target = (
                    fault.phase
                    if fault.phase in {"blocked", "needs_login", "failed_before_edit"}
                    else "blocked"
                )
                self.journal.transition(job_id, target, error_category=fault.category)
            elif latest and latest.phase == "running":
                self.journal.transition(job_id, "uncertain", error_category=fault.category)
        except TimeoutError:
            latest = self.journal.get(job_id)
            if latest and latest.phase == "accepted":
                self.journal.transition(job_id, "failed_before_edit", error_category="deadline")
            elif latest and latest.phase == "running":
                self.journal.transition(job_id, "uncertain", error_category="deadline_after_write")
        except asyncio.CancelledError:
            latest = self.journal.get(job_id)
            if latest and latest.phase == "accepted":
                self.journal.transition(
                    job_id, "failed_before_edit", error_category="worker_stopping"
                )
            elif latest and latest.phase == "running":
                self.journal.transition(job_id, "uncertain", error_category="worker_stopping")
            raise
        except Exception:
            latest = self.journal.get(job_id)
            if latest and latest.phase == "accepted":
                self.journal.transition(job_id, "blocked", error_category="worker_error")
            elif latest and latest.phase == "running":
                self.journal.transition(
                    job_id, "uncertain", error_category="worker_error_after_write"
                )
        finally:
            if self._current_id == job_id:
                self._current_id = None
            self._operations.pop(job_id, None)
            self._cancel_events.pop(job_id, None)

    async def cancel(self, job_id: str):
        async with self._submission_lock:
            record = self.journal.get(job_id)
            if record is None:
                return None
            event = self._cancel_events.get(job_id)
            if record.phase == "accepted":
                if event:
                    event.set()
                self._queue = deque(queued for queued in self._queue if queued != job_id)
                changed = self.journal.transition(
                    job_id, "failed_before_edit", error_category="cancelled"
                )
                if job_id != self._current_id:
                    self._operations.pop(job_id, None)
                    self._cancel_events.pop(job_id, None)
                return changed
            if record.phase == "running":
                if event:
                    event.set()
                return record
            if record.phase == "awaiting_verification":
                return self.journal.transition(
                    job_id, "uncertain", error_category="cancelled_after_write"
                )
            return record

    def acknowledge_verification(self, job_id: str, result: str):
        record = self.journal.get(job_id)
        if record is None or record.phase != "awaiting_verification":
            return None
        return self.journal.transition(job_id, result)

    def delete_metadata(self, job_id: str) -> bool:
        return self.journal.delete_terminal(job_id)

    async def close(self) -> None:
        if self._closed:
            return
        async with self._submission_lock:
            self._closing = True
            for job_id, event in tuple(self._cancel_events.items()):
                event.set()
                record = self.journal.get(job_id)
                if job_id != self._current_id and record and record.phase == "accepted":
                    self.journal.transition(
                        job_id, "failed_before_edit", error_category="worker_stopping"
                    )
                    self._operations.pop(job_id, None)
                    self._cancel_events.pop(job_id, None)
            self._queue.clear()
            self._queue_changed.set()
        if self._runner and not self._runner.done():
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
        close_engine = getattr(self.engine, "close", None)
        if close_engine is not None:
            result = close_engine()
            if asyncio.iscoroutine(result):
                await result
        self.journal.close()
        self._closed = True
