"""Polls durable jobs and delegates each fenced claim to the plugin."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from chatcore_research.jobs import ResearchJob, ResearchJobs
from chatcore_research.settings import ResearchSettings

log = logging.getLogger(__name__)
Clock = Callable[[], datetime]
RunJob = Callable[[ResearchJob], Awaitable[None]]
CanClaim = Callable[[], Awaitable[bool]]


class ResearchWorker:
    def __init__(
        self,
        jobs: ResearchJobs,
        run_job: RunJob,
        *,
        settings: ResearchSettings,
        clock: Clock = lambda: datetime.now(UTC),
        sleep=asyncio.sleep,
        can_claim: CanClaim | None = None,
    ) -> None:
        self.jobs, self.run_job, self.settings = jobs, run_job, settings
        self.clock, self.sleep = clock, sleep
        self.can_claim = can_claim
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    def start(self) -> None:
        if self._task is None:
            self._closing = False
            self._task = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        while not self._closing:
            try:
                await self.run_once()
                await self.sleep(self.settings.poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("research worker poll failed error=%s", type(exc).__name__)
                await self.sleep(self.settings.poll_seconds)

    async def run_once(self, now: datetime | None = None) -> int:
        if self.can_claim and not await self.can_claim():
            return 0
        claimed = []
        for _ in range(self.settings.worker_concurrency):
            job = await self.jobs.claim(
                now or self.clock(), int(self.settings.lease_seconds), self.settings.max_attempts
            )
            if job is None:
                break
            claimed.append(job)
        if claimed:
            results = await asyncio.gather(
                *(self.run_job(job) for job in claimed), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    log.warning("research worker job failed error=%s", type(result).__name__)
        return len(claimed)

    async def aclose(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
