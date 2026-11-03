from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from chatcore_release_control.broker_worker import (
    BrokerDispatchWorker,
    BrokerGitHubClient,
    BrokerWorkerStore,
)

sys.path.insert(0, str(Path(__file__).parent))
pytest_plugins = ("test_broker",)
from test_broker import _admitted_release, _queued_card, _run  # noqa: E402


@pytest_asyncio.fixture
async def worker_store(broker_pool):
    schema = Path(__file__).parents[1] / "src/chatcore_release_control/worker_schema.sql"
    await broker_pool.execute(schema.read_text())
    return BrokerWorkerStore(broker_pool)


async def test_dispatch_intent_commits_before_network_and_never_guesses_run_id(
    worker_store, broker_pool
):
    ledger, card, fence, now = await _queued_card(broker_pool)
    # Fixture has already acknowledged outbox; the worker may resume this exact claim.
    events = []

    class API:
        async def dispatch(self, context, dispatch_id):
            assert await worker_store.intent_state(card.approval_id, fence) == "sending"
            assert dispatch_id == await worker_store.dispatch_id(card.approval_id, fence)
            events.append(context.head_sha)

    worker = BrokerDispatchWorker(store=worker_store, ledger=ledger, api=API(), reporter=None)
    first = await worker.dispatch(card.approval_id, fence=fence, now=now)
    again = await worker.dispatch(card.approval_id, fence=fence, now=now)
    assert first == again == "awaiting_run_claim" and events == ["b" * 40]
    stored = await ledger.get_card(card.approval_id)
    assert stored.run_attempt is None


async def test_lost_dispatch_reply_is_durable_and_does_not_repeat_effect(worker_store, broker_pool):
    ledger, card, fence, now = await _queued_card(broker_pool)
    events = []

    class API:
        async def dispatch(self, context, dispatch_id):
            events.append("sent")
            raise RuntimeError("synthetic unsafe diagnostics")

    worker = BrokerDispatchWorker(store=worker_store, ledger=ledger, api=API(), reporter=None)
    assert await worker.dispatch(card.approval_id, fence=fence, now=now) == "dispatch_uncertain"
    assert await worker.dispatch(card.approval_id, fence=fence, now=now) == "dispatch_uncertain"
    assert events == ["sent"]


async def test_owner_policy_or_full_baseline_change_prevents_dispatch(worker_store, broker_pool):
    ledger, card, fence, now = await _queued_card(broker_pool)
    await broker_pool.execute(
        "UPDATE release_control.broker_target_state SET telegram_owner_user_id=8"
    )

    class API:
        async def dispatch(self, context, dispatch_id):
            pytest.fail("removed owner cannot dispatch")

    worker = BrokerDispatchWorker(store=worker_store, ledger=ledger, api=API(), reporter=None)
    assert await worker.dispatch(card.approval_id, fence=fence, now=now) == "invalidated"
    assert (await ledger.get_card(card.approval_id)).status.value == "invalidated"


async def test_revoked_release_is_invalidated_before_any_workflow_dispatch(
    worker_store, broker_pool
):
    from chatcore_release_control.revocation import PostgresReleaseRevocationStore

    ledger, card, fence, now = await _queued_card(broker_pool)
    await PostgresReleaseRevocationStore(broker_pool).revoke(99007, card.approval_id, 7, now=now)

    class API:
        async def dispatch(self, _context, _dispatch_id):
            pytest.fail("revoked release must not dispatch")

    worker = BrokerDispatchWorker(store=worker_store, ledger=ledger, api=API(), reporter=None)
    assert await worker.dispatch(card.approval_id, fence=fence, now=now) == "invalidated"
    assert (await ledger.get_card(card.approval_id)).status.value == "invalidated"
    assert await worker_store.intent_state(card.approval_id, fence) is None
    assert (
        await broker_pool.fetchval(
            "SELECT state FROM release_control.decision_outbox WHERE approval_id=$1",
            card.approval_id,
        )
        == "canceled"
    )


async def test_revoked_unadmitted_run_cannot_be_requeued(worker_store, broker_pool):
    from chatcore_release_control.revocation import PostgresReleaseRevocationStore

    ledger, card, fence, now = await _queued_card(broker_pool)
    run = _run()
    await ledger.bind_run_attempt(card.approval_id, fence=fence, run=run, now=now)
    await PostgresReleaseRevocationStore(broker_pool).revoke(99008, card.approval_id, 7, now=now)

    assert (
        await worker_store.requeue_before_admission(
            card.approval_id,
            fence=fence,
            run=run,
            conclusion="cancelled",
            current_base="a" * 40,
            current_head="b" * 40,
            pr_open=True,
            now=now + timedelta(seconds=1),
        )
        is False
    )
    assert (await ledger.get_card(card.approval_id)).status.value == "invalidated"
    assert (
        await broker_pool.fetchval(
            "SELECT count(*) FROM release_control.release_run_history WHERE approval_id=$1",
            card.approval_id,
        )
        == 0
    )


async def test_cancelled_bound_run_requeues_same_decision_with_new_fence_and_retained_history(
    worker_store, broker_pool
):
    ledger, card, fence, now = await _queued_card(broker_pool)
    run = _run()
    await ledger.bind_run_attempt(card.approval_id, fence=fence, run=run, now=now)
    assert (
        await worker_store.requeue_before_admission(
            card.approval_id,
            fence=fence,
            run=run,
            conclusion="cancelled",
            current_base="a" * 40,
            current_head="b" * 40,
            pr_open=True,
            now=now + timedelta(seconds=1),
        )
        is True
    )
    claim = await ledger.claim_next_dispatch(
        "production", "new-worker", now=now + timedelta(seconds=2)
    )
    assert claim is not None and claim.approval_id == card.approval_id and claim.fence > fence
    record = await ledger.get_card(card.approval_id)
    assert record.decision.value == "approve" and record.run_attempt is None
    assert (
        await broker_pool.fetchval("SELECT count(*) FROM release_control.release_run_history") == 1
    )


async def test_expired_or_changed_pr_cannot_requeue(worker_store, broker_pool):
    ledger, card, fence, now = await _queued_card(broker_pool)
    await ledger.bind_run_attempt(card.approval_id, fence=fence, run=_run(), now=now)
    assert (
        await worker_store.requeue_before_admission(
            card.approval_id,
            fence=fence,
            run=_run(),
            conclusion="cancelled",
            current_base="f" * 40,
            current_head="b" * 40,
            pr_open=True,
            now=now,
        )
        is False
    )
    assert (await ledger.get_card(card.approval_id)).run_attempt == _run()


async def test_an_admitted_merge_can_never_be_reset_by_a_cancelled_run(worker_store, broker_pool):
    _, card, fence, _, run, now = await _admitted_release(broker_pool)
    assert (
        await worker_store.requeue_before_admission(
            card.approval_id,
            fence=fence,
            run=run,
            conclusion="cancelled",
            current_base="a" * 40,
            current_head="b" * 40,
            pr_open=True,
            now=now,
        )
        is False
    )
    assert (
        await broker_pool.fetchval("SELECT count(*) FROM release_control.release_run_history") == 0
    )


async def test_retired_run_cannot_reclaim_new_dispatch_fence(worker_store, broker_pool):
    from chatcore_release_control.ledger import LedgerError

    ledger, card, fence, now = await _queued_card(broker_pool)
    await ledger.bind_run_attempt(card.approval_id, fence=fence, run=_run(), now=now)
    await worker_store.requeue_before_admission(
        card.approval_id,
        fence=fence,
        run=_run(),
        conclusion="cancelled",
        current_base="a" * 40,
        current_head="b" * 40,
        pr_open=True,
        now=now,
    )
    claim = await ledger.claim_next_dispatch("production", "new-worker", now=now)
    await ledger.acknowledge_dispatch(card.approval_id, fence=claim.fence, now=now)
    with pytest.raises(LedgerError, match="binding_mismatch"):
        await ledger.bind_run_attempt(card.approval_id, fence=claim.fence, run=_run(), now=now)


async def test_github_dispatch_is_fixed_workflow_main_and_minimum_immutable_inputs():
    calls = []

    def transport(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"id": 101, "owner": {"id": 202}, "full_name": "acme/plugin", "private": True},
            )
        assert json.loads(request.content) == {
            "ref": "main",
            "inputs": {
                "dispatch_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "approval_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "pr_number": "5",
                "head_sha": "b" * 40,
            },
        }
        return httpx.Response(204)

    api = BrokerGitHubClient(
        repository="acme/plugin",
        repository_id=101,
        owner_id=202,
        token_provider=lambda: "synthetic-token",
        transport=httpx.MockTransport(transport),
    )
    from types import SimpleNamespace

    context = SimpleNamespace(
        approval_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", pr_number=5, head_sha="b" * 40
    )
    try:
        await api.dispatch(context, UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))
        assert (
            str(calls[-1].url)
            == "https://api.github.com/repos/acme/plugin/actions/workflows/release.yml/dispatches"
        )
    finally:
        await api.close()


async def test_dispatch_identifier_changes_on_new_fence_and_old_run_rerun_is_retired(
    worker_store, broker_pool
):
    from dataclasses import replace

    from chatcore_release_control.ledger import LedgerError

    ledger, card, fence, now = await _queued_card(broker_pool)

    class API:
        async def dispatch(self, context, dispatch_id):
            pass

    worker = BrokerDispatchWorker(store=worker_store, ledger=ledger, api=API(), reporter=None)
    await worker.dispatch(card.approval_id, fence=fence, now=now)
    first_dispatch = await worker_store.dispatch_id(card.approval_id, fence)
    await ledger.bind_run_attempt(card.approval_id, fence=fence, run=_run(), now=now)
    await worker_store.requeue_before_admission(
        card.approval_id,
        fence=fence,
        run=_run(),
        conclusion="cancelled",
        current_base="a" * 40,
        current_head="b" * 40,
        pr_open=True,
        now=now,
    )
    claim = await ledger.claim_next_dispatch("production", "next-worker", now=now)
    await worker.dispatch(card.approval_id, fence=claim.fence, now=now)
    assert await worker_store.dispatch_id(card.approval_id, claim.fence) != first_dispatch
    with pytest.raises(LedgerError, match="binding_mismatch"):
        await ledger.bind_run_attempt(
            card.approval_id, fence=claim.fence, run=replace(_run(), attempt=2), now=now
        )


async def test_worker_reports_uncertain_dispatch_once_without_repeating_network(
    worker_store, broker_pool
):
    ledger, card, fence, now = await _queued_card(broker_pool)

    class API:
        async def dispatch(self, context, dispatch_id):
            raise RuntimeError("synthetic lost reply")

    reports = []

    class Reporter:
        async def report(self, request_id, approval_id):
            reports.append((request_id, approval_id))

    worker = BrokerDispatchWorker(store=worker_store, ledger=ledger, api=API(), reporter=Reporter())
    await worker.dispatch(card.approval_id, fence=fence, now=now)
    assert await worker.tick("production", "protected-worker", now=now) == "dispatch_uncertain"
    await worker.tick("production", "protected-worker", now=now)
    assert len(reports) == 1
    key = await broker_pool.fetchval(
        "SELECT release_status FROM release_control.broker_report_delivery"
    )
    assert "uncertain" in key
