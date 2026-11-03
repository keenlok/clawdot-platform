import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from chatcore.security.net_guard import BlockedURL, FetchError
from chatcore_jobs.models import Actor, Claim, Job, JobKind, JobStatus, Scope
from chatcore_jobs.settings import JobsSettings
from chatcore_jobs.watch import RenderedBlocked, RenderedBusy, Snapshot
from chatcore_jobs.worker import JobsWorker

NOW = datetime(2026, 10, 31, tzinfo=UTC)


class Repo:
    def __init__(self, job):
        self.job = job
        self.claimed = []
        self.reserved = []
        self.finished = []
        self.delivery_ok = True
        self.release_count = 0

    async def claim_due(self, now, limit=None):
        if self.job.status is not JobStatus.ACTIVE or self.job.next_run_at > now:
            return []
        claim = Claim(self.job, uuid4(), now + timedelta(seconds=60))
        self.claimed.append(claim)
        return [claim]

    async def finish_run(self, claim, **kwargs):
        self.finished.append((claim, kwargs))
        self.job = replace(
            self.job,
            status=kwargs.get("status", JobStatus.ACTIVE),
            next_run_at=kwargs.get("next_run_at") or self.job.next_run_at,
            snapshot=kwargs.get("snapshot") or self.job.snapshot,
            snapshot_hash=kwargs.get("snapshot_hash") or self.job.snapshot_hash,
            failure_count=kwargs.get("failure_count")
            if kwargs.get("failure_count") is not None
            else self.job.failure_count,
            error_episode=kwargs.get("error_episode")
            if kwargs.get("error_episode") is not None
            else self.job.error_episode,
            occurrence=self.job.occurrence + 1,
            last_delivery_state=kwargs.get("delivery_state") or self.job.last_delivery_state,
        )
        return True

    async def reserve_delivery(self, claim, content, kind, **kwargs):
        self.reserved.append((claim, content, kind, kwargs))
        self.job = replace(
            self.job,
            next_run_at=kwargs.get("next_run_at") or self.job.next_run_at,
            snapshot=kwargs.get("snapshot") or self.job.snapshot,
            snapshot_hash=kwargs.get("snapshot_hash") or self.job.snapshot_hash,
            failure_count=kwargs.get("failure_count")
            if kwargs.get("failure_count") is not None
            else self.job.failure_count,
            error_episode=kwargs.get("error_episode")
            if kwargs.get("error_episode") is not None
            else self.job.error_episode,
            occurrence=self.job.occurrence + 1,
            last_delivery_state="reserved",
        )
        return True

    async def delivery_allowed(self, claim, kind, **_kwargs):
        return self.delivery_ok

    async def finish_delivery(self, claim, kind, **kwargs):
        self.finished.append((claim, kind, kwargs))
        final_status = kwargs.get("terminal_status")
        if kwargs.get("status") in {"unknown", "suppressed"} and claim.job.kind is JobKind.REMINDER:
            final_status = JobStatus.FAILED
        self.job = replace(
            self.job,
            status=JobStatus(final_status) if final_status else self.job.status,
            last_delivery_state=kwargs.get("status"),
        )
        return True

    async def finish_claim_reservations(self, claim, **kwargs):
        return 0

    async def release_claim(self, claim):
        self.release_count += 1
        return True


def job(
    kind=JobKind.REMINDER,
    payload=None,
    *,
    due=NOW,
    snapshot=None,
    failure_count=0,
    error_episode=False,
):
    return Job(
        uuid4(),
        Scope("tg:-100", False, "7"),
        Actor("tg:7"),
        kind,
        JobStatus.ACTIVE,
        payload or {"text": "Call mom"},
        NOW - timedelta(minutes=1),
        due,
        occurrence=0,
        revision=0,
        snapshot=snapshot,
        failure_count=failure_count,
        error_episode=error_episode,
    )


@pytest.mark.asyncio
async def test_due_reminder_sends_once_to_persisted_chat_and_topic():
    repo = Repo(job())
    sent = []

    async def sender(chat, text, thread_id):
        sent.append((chat, text, thread_id))
        return "msg-1"

    worker = JobsWorker(repo, JobsSettings(), authorize=lambda _job: _true(), sender=sender)
    await worker.run_once(NOW)
    assert len(repo.reserved) == 1
    assert sent[0][0:1] == ("tg:-100",) and sent[0][2] == "7"
    assert "Call mom" in sent[0][1]
    assert repo.finished[-1][2]["status"] == "sent"


@pytest.mark.asyncio
async def test_future_job_stays_queued():
    repo = Repo(job(due=NOW + timedelta(hours=1)))
    worker = JobsWorker(repo, JobsSettings(), authorize=lambda _job: _true(), sender=_sender)
    await worker.run_once(NOW)
    assert not repo.claimed and not repo.reserved


@pytest.mark.asyncio
async def test_send_exception_marks_delivery_unknown_without_retry():
    repo = Repo(job())

    async def failed(*_):
        raise TimeoutError("sensitive body")

    worker = JobsWorker(repo, JobsSettings(), authorize=lambda _job: _true(), sender=failed)
    await worker.run_once(NOW)
    assert repo.finished[-1][2]["status"] == "unknown"


@pytest.mark.asyncio
async def test_cancel_after_reservation_suppresses_pre_send():
    repo = Repo(job())
    repo.delivery_ok = False
    sent = []

    async def sender(*args):
        sent.append(args)

    worker = JobsWorker(repo, JobsSettings(), authorize=lambda _job: _true(), sender=sender)
    await worker.run_once(NOW)
    assert not sent
    assert repo.finished[-1][2]["status"] == "suppressed"


@pytest.mark.asyncio
async def test_delivery_preflight_error_finalizes_reservation_without_sending():
    repo = Repo(job())
    repo.delivery_allowed = AsyncMock(side_effect=RuntimeError("sensitive diagnostic"))
    sent = []

    async def sender(*args):
        sent.append(args)

    worker = JobsWorker(repo, JobsSettings(), authorize=lambda _job: _true(), sender=sender)
    await worker.run_once(NOW)
    assert not sent
    assert repo.finished[-1][2]["status"] == "suppressed"
    assert repo.job.status is JobStatus.FAILED


@pytest.mark.asyncio
async def test_watch_first_baseline_and_unchanged_are_silent():
    watch = job(JobKind.WATCH, {"url": "https://example.com", "interval_seconds": 300})
    repo = Repo(watch)
    fetched = []

    async def fetch(url, settings):
        fetched.append(url)
        return Snapshot("same", "hash")

    sent = []
    worker = JobsWorker(
        repo,
        JobsSettings(),
        authorize=lambda _job: _true(),
        sender=lambda *args: _append(sent, args),
        fetcher=fetch,
    )
    await worker.run_once(NOW)
    await worker.run_once(NOW + timedelta(minutes=5))
    assert fetched == ["https://example.com", "https://example.com"]
    assert not sent and not repo.reserved
    assert len(repo.finished) == 2


@pytest.mark.asyncio
async def test_blocked_site_stops_rendered_watch_without_retry():
    watch = job(
        JobKind.WATCH,
        {"url": "https://example.com", "interval_seconds": 3600, "mode": "rendered"},
    )
    repo = Repo(watch)
    sent = []

    async def rendered_fetch(_url, _settings):
        raise RenderedBlocked("untrusted renderer message")

    async def plain_fetch(*_args):
        pytest.fail("rendered blocked site must not fall back to plain fetch")

    async def sender(chat, content, thread):
        sent.append((chat, content, thread))
        return "message"

    worker = JobsWorker(
        repo,
        JobsSettings(),
        authorize=lambda _job: _true(),
        sender=sender,
        fetcher=plain_fetch,
        rendered_fetcher=rendered_fetch,
    )
    await worker.run_once(NOW)
    assert len(sent) == 1
    assert sent[0][1] == (
        "This site blocks automated access, so the website watch was stopped. "
        "You can open the page yourself."
    )
    assert repo.reserved[0][2] == "watch_site_blocked"
    assert repo.job.status is JobStatus.FAILED


@pytest.mark.asyncio
async def test_rendered_busy_reschedules_without_failure_or_notification():
    watch = job(
        JobKind.WATCH,
        {"url": "https://example.com", "interval_seconds": 3600, "mode": "rendered"},
    )
    repo = Repo(watch)

    async def rendered_fetch(_url, _settings):
        raise RenderedBusy()

    worker = JobsWorker(
        repo,
        JobsSettings(),
        authorize=lambda _job: _true(),
        sender=lambda *_: pytest.fail("busy must not notify"),
        rendered_fetcher=rendered_fetch,
    )
    await worker.run_once(NOW)
    assert repo.job.failure_count == 0
    assert repo.finished[-1][1]["next_run_at"] == NOW + timedelta(seconds=300)


@pytest.mark.asyncio
async def test_rendered_watch_stores_silent_baseline_then_notifies_text_change():
    watch = job(
        JobKind.WATCH,
        {"url": "https://example.com/private-path", "interval_seconds": 3600, "mode": "rendered"},
    )
    repo = Repo(watch)
    outcomes = [Snapshot("baseline", "first"), Snapshot("new readable text", "second")]
    sent = []

    async def rendered_fetch(_url, _settings):
        return outcomes.pop(0)

    async def sender(_chat, content, _thread):
        sent.append(content)
        return "message"

    worker = JobsWorker(
        repo,
        JobsSettings(),
        authorize=lambda _job: _true(),
        sender=sender,
        rendered_fetcher=rendered_fetch,
    )
    await worker.run_once(NOW)
    assert not sent
    await worker.run_once(NOW + timedelta(hours=1))
    assert len(sent) == 1
    assert "https://example.com" not in sent[0]
    assert "Website text changed." in sent[0]


@pytest.mark.asyncio
async def test_rendered_fetch_timeout_uses_failure_path():
    watch = job(
        JobKind.WATCH,
        {"url": "https://example.com", "interval_seconds": 3600, "mode": "rendered"},
    )
    repo = Repo(watch)

    async def rendered_fetch(*_args):
        await asyncio.Future()

    worker = JobsWorker(
        repo,
        JobsSettings(
            execution_deadline_seconds=2,
            rendered_fetch_timeout_seconds=1,
            send_deadline_seconds=1,
        ),
        authorize=lambda _job: _true(),
        sender=_sender,
        rendered_fetcher=rendered_fetch,
    )
    await worker.run_once(NOW)
    assert repo.job.failure_count == 1


@pytest.mark.asyncio
async def test_rendered_fetches_obey_semaphore_limit():
    jobs = [
        job(
            JobKind.WATCH,
            {"url": "https://example.com", "interval_seconds": 3600, "mode": "rendered"},
        )
        for _ in range(2)
    ]

    class TwoClaims(Repo):
        async def claim_due(self, now, limit=None):
            return [Claim(item, uuid4(), now + timedelta(seconds=60)) for item in jobs]

    repo = TwoClaims(jobs[0])
    in_flight = 0
    maximum = 0

    async def rendered_fetch(_url, _settings):
        nonlocal in_flight, maximum
        in_flight += 1
        maximum = max(maximum, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return Snapshot("same", "same")

    worker = JobsWorker(
        repo,
        JobsSettings(rendered_concurrency=1),
        authorize=lambda _job: _true(),
        sender=_sender,
        rendered_fetcher=rendered_fetch,
    )
    await worker.run_once(NOW)
    assert maximum == 1


@pytest.mark.asyncio
async def test_rendered_saturated_slot_reschedules_without_waiting_for_claim_deadline():
    watches = [
        job(
            JobKind.WATCH,
            {
                "url": f"https://example.com/{name}",
                "interval_seconds": 3600,
                "mode": "rendered",
            },
        )
        for name in ("first", "second")
    ]

    class TwoJobRepo:
        def __init__(self):
            self.jobs = {item.id: item for item in watches}
            self.claimed = []
            self.released = []
            self.finished = []

        async def claim_due(self, now, limit=None):
            due = [item for item in self.jobs.values() if item.next_run_at <= now]
            claims = [Claim(item, uuid4(), now + timedelta(seconds=60)) for item in due[:limit]]
            self.claimed.extend(claims)
            return claims

        async def finish_run(self, claim, **kwargs):
            self.finished.append((claim, kwargs))
            current = self.jobs[claim.job.id]
            self.jobs[claim.job.id] = replace(
                current,
                status=kwargs.get("status", JobStatus.ACTIVE),
                next_run_at=kwargs.get("next_run_at") or current.next_run_at,
                failure_count=kwargs.get("failure_count", current.failure_count),
                occurrence=current.occurrence + 1,
            )
            return True

        async def release_claim(self, claim):
            self.released.append(claim)

    repo = TwoJobRepo()
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()
    second_rescheduled = asyncio.Event()
    fetches = []

    async def rendered_fetch(url, _settings):
        fetches.append(url)
        if url.endswith("/first"):
            first_started.set()
            await allow_first_to_finish.wait()
        return Snapshot("same", "same")

    original_finish_run = repo.finish_run

    async def finish_run(claim, **kwargs):
        await original_finish_run(claim, **kwargs)
        if claim.job.payload["url"].endswith("/second"):
            second_rescheduled.set()

    repo.finish_run = finish_run
    worker = JobsWorker(
        repo,
        JobsSettings(rendered_concurrency=1, worker_concurrency=2),
        authorize=lambda _job: _true(),
        sender=lambda *_args: pytest.fail("saturated renderer must not notify"),
        rendered_fetcher=rendered_fetch,
    )
    first_run = asyncio.create_task(worker.run_once(NOW))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await asyncio.wait_for(second_rescheduled.wait(), timeout=1)
    second = next(item for item in repo.jobs.values() if item.payload["url"].endswith("/second"))
    assert second.next_run_at == NOW + timedelta(seconds=300)
    assert second.failure_count == 0
    assert fetches == ["https://example.com/first"]
    assert not repo.released

    allow_first_to_finish.set()
    await asyncio.wait_for(first_run, timeout=1)
    await worker.run_once(NOW + timedelta(seconds=300))
    assert fetches == ["https://example.com/first", "https://example.com/second"]
    assert not repo.released


async def _true():
    return True


async def _sender(*_):
    return "msg"


async def _append(target, value):
    target.append(value)
    return "msg"


@pytest.mark.asyncio
async def test_changed_watch_reserves_one_delivery_and_updates_baseline():
    repo = Repo(
        job(
            JobKind.WATCH,
            {"url": "https://example.com", "interval_seconds": 300},
            snapshot="old text",
        )
    )
    repo.job = replace(repo.job, snapshot_hash="old-hash")
    sent = []

    async def fetch(_url, _settings):
        return Snapshot("new text", "new-hash")

    async def sender(*args):
        sent.append(args)
        return "watch-msg"

    worker = JobsWorker(
        repo, JobsSettings(), authorize=lambda _job: _true(), sender=sender, fetcher=fetch
    )
    await worker.run_once(NOW)
    assert len(repo.reserved) == 1
    assert len(sent) == 1
    assert repo.job.snapshot_hash == "new-hash"
    assert repo.job.last_delivery_state == "sent"
    await worker.run_once(NOW + timedelta(minutes=5))
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_watch_fetch_episode_notifies_once_then_recovers():
    repo = Repo(
        job(
            JobKind.WATCH,
            {"url": "https://example.com", "interval_seconds": 300},
            snapshot="baseline",
        )
    )
    repo.job = replace(repo.job, snapshot_hash="baseline-hash")
    sent = []
    outcomes = [
        FetchError("raw secret"),
        FetchError("raw secret"),
        FetchError("raw secret"),
        FetchError("raw secret"),
        Snapshot("recovered", "new"),
    ]

    async def fetch(_url, _settings):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def sender(chat, text, thread):
        sent.append((chat, text, thread))
        return "error-msg"

    worker = JobsWorker(
        repo, JobsSettings(), authorize=lambda _job: _true(), sender=sender, fetcher=fetch
    )
    await worker.run_once(NOW)
    await worker.run_once(NOW + timedelta(seconds=30))
    await worker.run_once(NOW + timedelta(seconds=90))
    assert len(sent) == 1
    assert "raw secret" not in sent[0][1]
    await worker.run_once(NOW + timedelta(seconds=210))
    assert len(sent) == 1
    assert repo.job.error_episode
    await worker.run_once(NOW + timedelta(minutes=10))
    assert repo.job.failure_count == 0
    assert not repo.job.error_episode


@pytest.mark.asyncio
async def test_blocked_watch_notifies_once_and_becomes_failed():
    repo = Repo(job(JobKind.WATCH, {"url": "https://127.0.0.1", "interval_seconds": 300}))

    async def fetch(_url, _settings):
        raise BlockedURL("private detail")

    worker = JobsWorker(
        repo, JobsSettings(), authorize=lambda _job: _true(), sender=_sender, fetcher=fetch
    )
    await worker.run_once(NOW)
    assert len(repo.reserved) == 1
    assert repo.finished[-1][2]["terminal_status"] == "failed"
    assert repo.job.status is JobStatus.FAILED


@pytest.mark.asyncio
async def test_stalled_authorization_is_bounded_by_worker_deadline():
    repo = Repo(job())
    started = asyncio.Event()

    async def authorize(_job):
        started.set()
        await asyncio.Future()

    worker = JobsWorker(
        repo,
        JobsSettings(execution_deadline_seconds=1, send_deadline_seconds=1),
        authorize=authorize,
        sender=_sender,
    )
    await worker.run_once(NOW)
    assert started.is_set()
    assert repo.release_count >= 1


@pytest.mark.asyncio
async def test_platform_acceptance_then_timeout_is_unknown_and_not_replayed():
    repo = Repo(job())
    accepted = []

    async def sender(chat, text, thread):
        accepted.append((chat, text, thread))
        raise TimeoutError("response lost after acceptance")

    worker = JobsWorker(repo, JobsSettings(), authorize=lambda _job: _true(), sender=sender)
    await worker.run_once(NOW)
    await worker.run_once(NOW + timedelta(minutes=1))
    assert len(accepted) == 1
    assert repo.finished[-1][2]["status"] == "unknown"
    assert repo.job.status is JobStatus.FAILED


@pytest.mark.asyncio
async def test_long_downtime_schedules_from_current_check_without_catchup():
    repo = Repo(
        job(
            JobKind.WATCH,
            {"url": "https://example.com", "interval_seconds": 300},
        )
    )
    fetches = []

    async def fetch(_url, _settings):
        fetches.append(True)
        return Snapshot("baseline", "baseline-hash")

    worker = JobsWorker(
        repo, JobsSettings(), authorize=lambda _job: _true(), sender=_sender, fetcher=fetch
    )
    resumed = NOW + timedelta(days=30)
    await worker.run_once(resumed)
    assert repo.job.next_run_at == resumed + timedelta(seconds=300)
    await worker.run_once(resumed + timedelta(seconds=1))
    assert len(fetches) == 1


@pytest.mark.asyncio
async def test_worker_shutdown_releases_pre_reservation_claim_without_sending():
    repo = Repo(job())
    started = asyncio.Event()
    sent = []

    async def authorize(_job):
        started.set()
        await asyncio.Future()

    async def sender(*args):
        sent.append(args)
        return "msg"

    worker = JobsWorker(repo, JobsSettings(), authorize=authorize, sender=sender)
    task = asyncio.create_task(worker.run_once(NOW))
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert repo.release_count == 1
    assert not repo.reserved and not sent


@pytest.mark.asyncio
async def test_access_refresh_failure_retries_next_poll_without_claiming(caplog):
    repo = Repo(job())
    refreshes = 0
    sent = []

    async def refresh():
        nonlocal refreshes
        refreshes += 1
        if refreshes == 1:
            raise RuntimeError("secret diagnostic must not be logged")

    async def sender(*args):
        sent.append(args)
        return "message"

    worker = JobsWorker(
        repo,
        JobsSettings(),
        authorize=lambda _job: _true(),
        sender=sender,
        refresh_access=refresh,
    )
    await worker.run_once(NOW)
    assert not repo.claimed and not sent
    assert "RuntimeError" in caplog.text
    assert "secret diagnostic" not in caplog.text
    await worker.run_once(NOW)
    assert len(repo.claimed) == 1
    assert len(sent) == 1
