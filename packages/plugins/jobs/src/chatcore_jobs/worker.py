"""Bounded durable scheduler worker; all network work is fenced by repository claims."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from chatcore.security.net_guard import BlockedURL

from chatcore_jobs.models import Claim, Job, JobKind, JobStatus
from chatcore_jobs.settings import JobsSettings
from chatcore_jobs.watch import (
    RenderedBlocked,
    RenderedBusy,
    RenderedFetcher,
    Snapshot,
    _quoted,
    change_notification,
    fetch_snapshot,
    safe_error_message,
)

log = logging.getLogger(__name__)
Authorize = Callable[[Job], Awaitable[bool]]
Sender = Callable[[str, str, str | None], Awaitable[Any]]
Fetcher = Callable[[str, JobsSettings], Awaitable[Snapshot]]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
RefreshAccess = Callable[[], Awaitable[None]]


class JobsWorker:
    def __init__(
        self,
        repo: Any,
        settings: JobsSettings,
        *,
        authorize: Authorize,
        sender: Sender,
        fetcher: Fetcher = fetch_snapshot,
        rendered_fetcher: RenderedFetcher | None = None,
        refresh_access: RefreshAccess | None = None,
        clock: Clock = lambda: datetime.now(UTC),
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.repo, self.settings = repo, settings
        self.authorize, self.sender, self.fetcher = authorize, sender, fetcher
        self.rendered_fetcher = rendered_fetcher
        self._rendered_semaphore = asyncio.Semaphore(settings.rendered_concurrency)
        self.refresh_access = refresh_access
        self.clock, self.sleep = clock, sleep
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    async def run_once(self, now: datetime | None = None) -> int:
        instant = now or self.clock()
        if self.refresh_access is not None:
            try:
                async with asyncio.timeout(self.settings.execution_deadline_seconds):
                    await self.refresh_access()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("job access refresh failed error=%s", type(exc).__name__)
                return 0
        claims = await self.repo.claim_due(instant, self.settings.worker_concurrency)
        if claims:
            await asyncio.gather(*(self._run_claim(claim, instant) for claim in claims))
        return len(claims)

    async def _eligible(self, claim: Claim) -> bool:
        try:
            return await self.authorize(claim.job)
        except Exception as exc:
            log.warning("job authorization failed id=%s error=%s", claim.job.id, type(exc).__name__)
            return False

    async def _run_claim(self, claim: Claim, now: datetime) -> None:
        try:
            async with asyncio.timeout(
                self.settings.execution_deadline_seconds + self.settings.send_deadline_seconds
            ):
                await self._process_claim(claim, now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "job run failed id=%s kind=%s error=%s",
                claim.job.id,
                claim.job.kind.value,
                type(exc).__name__,
            )
            await self.repo.release_claim(claim)

    async def _process_claim(self, claim: Claim, now: datetime) -> None:
        try:
            if not await self._eligible(claim):
                await self.repo.finish_run(
                    claim,
                    now=now,
                    next_run_at=None,
                    status=JobStatus.CANCELLED,
                    delivery_state="suppressed",
                )
                return
            if claim.job.kind is JobKind.REMINDER:
                content = "Reminder: “" + _quoted(claim.job.payload["text"], 1000) + "”"
                if not await self.repo.reserve_delivery(
                    claim,
                    content,
                    "reminder",
                    now=now,
                    next_run_at=None,
                    terminal_status="completed",
                ):
                    await self.repo.release_claim(claim)
                    return
                await self._send_reserved(claim, "reminder", content, terminal_status="completed")
                return
            await self._run_watch(claim, now)
        except asyncio.CancelledError:
            resolved = await self.repo.finish_claim_reservations(
                claim, status="unknown", now=self.clock()
            )
            if not resolved:
                await self.repo.release_claim(claim)
            raise
        except Exception as exc:
            log.warning(
                "job run failed id=%s kind=%s error=%s",
                claim.job.id,
                claim.job.kind.value,
                type(exc).__name__,
            )
            resolved = await self.repo.finish_claim_reservations(
                claim, status="unknown", now=self.clock()
            )
            if not resolved:
                await self.repo.release_claim(claim)

    async def _run_watch(self, claim: Claim, now: datetime) -> None:
        interval = claim.job.payload["interval_seconds"]
        url = claim.job.payload["url"]
        mode = claim.job.payload.get("mode", "plain")
        if mode not in {"plain", "rendered"}:
            await self.repo.finish_run(
                claim,
                now=now,
                next_run_at=None,
                status=JobStatus.CANCELLED,
                delivery_state="suppressed",
            )
            return
        if mode == "rendered" and self.rendered_fetcher is None:
            await self.repo.finish_run(
                claim,
                now=now,
                next_run_at=None,
                status=JobStatus.CANCELLED,
                delivery_state="suppressed",
            )
            return
        if mode == "rendered" and self._rendered_semaphore.locked():
            await self._reschedule_rendered_busy(claim, now, interval)
            return
        try:
            if mode == "rendered":
                async with self._rendered_semaphore:
                    async with asyncio.timeout(
                        self.settings.effective_rendered_fetch_timeout_seconds
                    ):
                        snapshot = await self.rendered_fetcher(url, self.settings)
            else:
                async with asyncio.timeout(self.settings.execution_deadline_seconds):
                    snapshot = await self.fetcher(url, self.settings)
        except RenderedBusy:
            await self._reschedule_rendered_busy(claim, now, interval)
            return
        except RenderedBlocked:
            if not await self._eligible(claim):
                await self.repo.finish_run(
                    claim,
                    now=now,
                    next_run_at=None,
                    status=JobStatus.CANCELLED,
                    delivery_state="suppressed",
                )
                return
            content = (
                "This site blocks automated access, so the website watch was stopped. "
                "You can open the page yourself."
            )
            if await self.repo.reserve_delivery(
                claim,
                content,
                "watch_site_blocked",
                now=now,
                next_run_at=None,
                terminal_status="failed",
            ):
                await self._send_reserved(
                    claim, "watch_site_blocked", content, terminal_status="failed"
                )
            return
        except BlockedURL as exc:
            if not await self._eligible(claim):
                await self.repo.finish_run(
                    claim,
                    now=now,
                    next_run_at=None,
                    status=JobStatus.CANCELLED,
                    delivery_state="suppressed",
                )
                return
            content = safe_error_message(exc)
            if await self.repo.reserve_delivery(
                claim,
                content,
                "watch_blocked",
                now=now,
                next_run_at=None,
                terminal_status="failed",
            ):
                await self._send_reserved(claim, "watch_blocked", content, terminal_status="failed")
            return
        except Exception as exc:
            await self._watch_failure(claim, now, interval, exc, rendered=mode == "rendered")
            return
        if not await self._eligible(claim):
            await self.repo.finish_run(
                claim,
                now=now,
                next_run_at=None,
                status=JobStatus.CANCELLED,
                delivery_state="suppressed",
            )
            return
        next_run = now + timedelta(seconds=interval)
        if claim.job.snapshot_hash is None:
            await self.repo.finish_run(
                claim,
                now=now,
                next_run_at=next_run,
                snapshot=snapshot.text,
                snapshot_hash=snapshot.digest,
                failure_count=0,
                error_episode=False,
                delivery_state="quiet",
            )
            return
        if snapshot.digest == claim.job.snapshot_hash:
            await self.repo.finish_run(
                claim,
                now=now,
                next_run_at=next_run,
                failure_count=0,
                error_episode=False,
                delivery_state="quiet",
            )
            return
        content = change_notification(claim.job.snapshot or "", snapshot.text)
        if await self.repo.reserve_delivery(
            claim,
            content,
            "watch_changed",
            now=now,
            next_run_at=next_run,
            snapshot=snapshot.text,
            snapshot_hash=snapshot.digest,
            failure_count=0,
            error_episode=False,
        ):
            await self._send_reserved(claim, "watch_changed", content)

    async def _reschedule_rendered_busy(self, claim: Claim, now: datetime, interval: int) -> None:
        await self.repo.finish_run(
            claim,
            now=now,
            next_run_at=now + timedelta(seconds=min(300, interval)),
            delivery_state="renderer_busy",
        )

    async def _watch_failure(
        self,
        claim: Claim,
        now: datetime,
        interval: int,
        error: Exception,
        *,
        rendered: bool = False,
    ) -> None:
        failures = claim.job.failure_count + 1
        delay = min(30 * (2 ** min(failures - 1, 8)), interval)
        next_run = now + timedelta(seconds=delay)
        notify = isinstance(error, BlockedURL) or (failures >= 3 and not claim.job.error_episode)
        if not notify:
            await self.repo.finish_run(
                claim,
                now=now,
                next_run_at=next_run,
                failure_count=failures,
                error_episode=claim.job.error_episode,
                delivery_state="fetch_error",
            )
            return
        content = safe_error_message(error, rendered=rendered)
        if await self.repo.reserve_delivery(
            claim,
            content,
            "watch_error",
            now=now,
            next_run_at=next_run,
            failure_count=failures,
            error_episode=True,
        ):
            await self._send_reserved(claim, "watch_error", content)

    async def _send_reserved(
        self,
        claim: Claim,
        notification_kind: str,
        content: str,
        *,
        terminal_status: str | None = None,
    ) -> None:
        try:
            eligible = await self._eligible(claim)
            allowed = eligible and await self.repo.delivery_allowed(
                claim, notification_kind, now=self.clock()
            )
        except asyncio.CancelledError:
            await self.repo.finish_delivery(
                claim,
                notification_kind,
                status="suppressed",
                now=self.clock(),
                terminal_status=terminal_status,
            )
            raise
        except Exception as exc:
            log.warning(
                "job delivery preflight failed id=%s error=%s",
                claim.job.id,
                type(exc).__name__,
            )
            allowed = False
        if not allowed:
            await self.repo.finish_delivery(
                claim,
                notification_kind,
                status="suppressed",
                now=self.clock(),
                terminal_status=terminal_status,
            )
            return
        try:
            async with asyncio.timeout(self.settings.send_deadline_seconds):
                result = await self.sender(
                    claim.job.scope.chat_key, content, claim.job.scope.thread_id
                )
            message_id = getattr(result, "message_id", result if isinstance(result, str) else None)
            await self.repo.finish_delivery(
                claim,
                notification_kind,
                status="sent",
                message_id=message_id,
                terminal_status=terminal_status,
                now=self.clock(),
            )
        except asyncio.CancelledError:
            await self.repo.finish_delivery(
                claim,
                notification_kind,
                status="unknown",
                terminal_status=terminal_status,
                now=self.clock(),
            )
            raise
        except Exception as exc:
            log.warning("job delivery unknown id=%s error=%s", claim.job.id, type(exc).__name__)
            await self.repo.finish_delivery(
                claim,
                notification_kind,
                status="unknown",
                terminal_status=terminal_status,
                now=self.clock(),
            )

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._closing = False
            self._task = asyncio.create_task(self._run_loop(), name="chatcore-jobs-worker")

    async def _run_loop(self) -> None:
        while not self._closing:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("jobs poll failed error=%s", type(exc).__name__)
            await self.sleep(self.settings.poll_seconds)

    async def aclose(self) -> None:
        self._closing = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
