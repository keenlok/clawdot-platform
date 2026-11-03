"""Small durable close scan; the service also handles manual closes."""

import asyncio
import logging
from datetime import timedelta

log = logging.getLogger(__name__)


class PollsCloser:
    def __init__(self, service, stores, *, sleep=asyncio.sleep):
        self.service, self.stores, self.sleep = service, stores, sleep
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> int:
        repo = self.service.repo(self.stores)
        failed_count = await repo.fail_stale_sending(self.service.clock())
        if failed_count:
            log.info("stale poll reservations failed count=%d", failed_count)
        try:
            await self.service.access.refresh()
        except Exception as exc:
            log.warning("poll close access refresh failed: %s", type(exc).__name__)
            return 0
        allowed = [chat.key for chat in self.service.access.groups_on(self.service.platform.name)]
        claims = await repo.claim_close(
            None,
            self.service.clock(),
            limit=5,
            allowed_chat_keys=allowed,
        )
        for poll in claims:
            try:
                await self.service.close_claim(poll, self.stores)
            except Exception as exc:
                log.warning("poll close failed id=%s error=%s", poll.id, type(exc).__name__)
        await repo.purge_older_than(
            self.service.clock() - timedelta(days=self.service.settings.retention_days)
        )
        return len(claims)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                log.warning("poll close scan failed: %s", type(exc).__name__)
            await self.sleep(self.service.settings.scan_seconds)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
