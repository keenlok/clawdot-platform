from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from chatcore_release_control.host_journal import (
    HostJournal,
    HostJournalError,
    OperationConflict,
    ReconciliationAction,
    ReconciliationRequired,
    UnsafeTransition,
)
from chatcore_release_control.policy import DeploymentIdentity, JournalState


def identity(
    *,
    git_sha: str = "a" * 40,
    image_digest: str = "sha256:" + "b" * 64,
    config_generation: int = 4,
    config_digest: str = "c" * 64,
    policy_digest: str = "d" * 64,
    target_sequence: int = 7,
) -> DeploymentIdentity:
    return DeploymentIdentity(
        git_sha=git_sha,
        image_digest=image_digest,
        config_generation=config_generation,
        config_digest=config_digest,
        policy_digest=policy_digest,
        target_sequence=target_sequence,
    )


def test_prepare_is_durable_across_reopen(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    baseline = identity()
    payload = identity(
        git_sha="e" * 40,
        image_digest="sha256:" + "f" * 64,
        config_generation=5,
        target_sequence=8,
    )

    record = HostJournal(path).prepare(
        operation_id="release-7",
        baseline=baseline,
        payload=payload,
        start_deadline=180,
        recovery_deadline=500,
        now=100,
    )
    reopened = HostJournal(path).get_record("release-7")

    assert record == reopened
    assert reopened.operation_id == "release-7"
    assert reopened.baseline == baseline
    assert reopened.payload == payload
    assert len(reopened.payload_digest) == 64
    assert reopened.start_deadline == 180
    assert reopened.phase is JournalState.PREPARED
    assert reopened.receipt is None


def test_current_record_reads_only_live_journal_without_mutation(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    journal = HostJournal(path)
    assert journal.current_record() is None
    prepared = journal.prepare(
        operation_id="release-7",
        baseline=identity(),
        payload=identity(git_sha="e" * 40),
        start_deadline=180,
        recovery_deadline=500,
        now=100,
    )
    before = path.read_bytes()
    assert journal.current_record() == prepared
    assert path.read_bytes() == before


def test_prepare_retry_is_idempotent_and_changed_request_conflicts(tmp_path) -> None:
    journal = HostJournal(tmp_path / "host-journal.json")
    values = {
        "operation_id": "release-7",
        "baseline": identity(),
        "payload": identity(git_sha="e" * 40),
        "start_deadline": 180,
        "recovery_deadline": 500,
        "now": 100,
    }

    original = journal.prepare(**values)
    assert journal.prepare(**values) == original

    with pytest.raises(OperationConflict):
        journal.prepare(**{**values, "payload": identity(git_sha="9" * 40)})
    with pytest.raises(OperationConflict):
        journal.prepare(**{**values, "start_deadline": 181})
    assert journal.get_record("release-7") == original


def test_journal_retains_terminal_operation_and_receipt(tmp_path) -> None:
    journal = HostJournal(tmp_path / "host-journal.json")
    values = {
        "operation_id": "release-7",
        "baseline": identity(),
        "payload": identity(git_sha="e" * 40),
        "start_deadline": 180,
        "recovery_deadline": 500,
        "now": 100,
    }
    journal.prepare(**values)
    journal.begin_switch("release-7", now=101)
    journal.reconcile("release-7", actual=values["payload"], now=102)
    completed = journal.reconcile(
        "release-7", actual=identity(git_sha="e" * 40), healthy=True, now=103
    )
    assert completed.record.phase is JournalState.HEALTHY
    assert completed.record.receipt
    assert completed.record.terminal_at == 103

    assert journal.prepare(**values) == completed.record
    with pytest.raises(OperationConflict):
        journal.prepare(**{**values, "operation_id": "release-8"})
    changed_after_maintenance = identity(
        config_generation=5,
        config_digest="9" * 64,
        target_sequence=8,
    )
    retry = journal.reconcile("release-7", actual=changed_after_maintenance, now=103)
    assert retry.action is ReconciliationAction.ALREADY_TERMINAL
    assert retry.record == completed.record


def test_competing_operations_are_serialized_across_journal_instances(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    barrier = Barrier(2)

    def prepare(operation_id: str) -> str:
        barrier.wait()
        try:
            HostJournal(path).prepare(
                operation_id=operation_id,
                baseline=identity(),
                payload=identity(git_sha="e" * 40),
                start_deadline=180,
                recovery_deadline=500,
                now=100,
            )
            return "prepared"
        except OperationConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(prepare, ("release-7", "release-8")))

    assert sorted(results) == ["conflict", "prepared"]
    record = HostJournal(path).get_record("release-7" if results[0] == "prepared" else "release-8")
    assert record.phase is JournalState.PREPARED


@pytest.mark.parametrize("now", [180, 181])
def test_prepared_operation_cannot_switch_at_or_after_deadline(tmp_path, now: int) -> None:
    path = tmp_path / "host-journal.json"
    journal = HostJournal(path)
    journal.prepare(
        operation_id="release-7",
        baseline=identity(),
        payload=identity(git_sha="e" * 40),
        start_deadline=180,
        recovery_deadline=500,
        now=100,
    )

    with pytest.raises(UnsafeTransition, match="start_deadline_expired"):
        journal.begin_switch("release-7", now=now)
    assert HostJournal(path).get_record("release-7").phase is JournalState.PREPARED


def test_reconcile_requires_observed_actual_state_without_mutating_record(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    journal = HostJournal(path)
    prepared = journal.prepare(
        operation_id="release-7",
        baseline=identity(),
        payload=identity(git_sha="e" * 40),
        start_deadline=180,
        recovery_deadline=500,
        now=100,
    )
    journal.begin_switch("release-7", now=101)

    with pytest.raises(ReconciliationRequired):
        journal.reconcile("release-7", actual=None, now=102)
    assert HostJournal(path).get_record("release-7").phase is JournalState.SWITCHING
    assert prepared.phase is JournalState.PREPARED


def test_expired_prepared_operation_reconciles_without_late_switch(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    journal = HostJournal(path)
    journal.prepare(
        operation_id="release-7",
        baseline=identity(),
        payload=identity(git_sha="e" * 40),
        start_deadline=180,
        recovery_deadline=500,
        now=100,
    )

    result = journal.reconcile("release-7", actual=identity(), now=180)
    assert result.action is ReconciliationAction.SWITCH_NOT_STARTED_EXPIRED
    assert result.record.phase is JournalState.PREPARED
    with pytest.raises(UnsafeTransition):
        journal.begin_switch("release-7", now=180)


def test_switch_crash_reconciliation_requires_health_before_terminal_success(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    journal = HostJournal(path)
    payload = identity(git_sha="e" * 40)
    journal.prepare(
        operation_id="release-7",
        baseline=identity(),
        payload=payload,
        start_deadline=180,
        recovery_deadline=500,
        now=100,
    )
    journal.begin_switch("release-7", now=101)

    observed = journal.reconcile("release-7", actual=payload, now=102)
    assert observed.action is ReconciliationAction.VERIFY_PAYLOAD
    assert observed.record.phase is JournalState.CHECKING
    with pytest.raises(ReconciliationRequired):
        journal.reconcile("release-7", actual=payload, now=103)
    assert journal.get_record("release-7").phase is JournalState.CHECKING

    failed_health = journal.reconcile("release-7", actual=payload, healthy=False, now=104)
    assert failed_health.action is ReconciliationAction.RESTORE_ALLOWED
    assert failed_health.record.phase is JournalState.RESTORING


def test_restoration_is_blocked_after_configuration_generation_changes(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    journal = HostJournal(path)
    payload = identity(git_sha="e" * 40, config_generation=5)
    journal.prepare(
        operation_id="release-7",
        baseline=identity(config_generation=4),
        payload=payload,
        start_deadline=180,
        recovery_deadline=500,
        now=100,
    )
    journal.begin_switch("release-7", now=101)
    journal.reconcile("release-7", actual=payload, now=102)
    journal.reconcile("release-7", actual=payload, healthy=False, now=103)

    changed = identity(
        git_sha="9" * 40,
        config_generation=6,
        config_digest="1" * 64,
        target_sequence=9,
    )
    result = journal.reconcile("release-7", actual=changed, now=104)
    assert result.record.phase is JournalState.RECOVERY_FAILED
    assert result.record.receipt
    assert journal.get_record("release-7") == result.record


def test_restored_healthy_requires_baseline_observation_and_health(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    journal = HostJournal(path)
    baseline = identity()
    payload = identity(git_sha="e" * 40)
    journal.prepare(
        operation_id="release-7",
        baseline=baseline,
        payload=payload,
        start_deadline=180,
        recovery_deadline=500,
        now=100,
    )
    journal.begin_switch("release-7", now=101)
    journal.reconcile("release-7", actual=payload, now=102)
    journal.reconcile("release-7", actual=payload, healthy=False, now=103)

    with pytest.raises(ReconciliationRequired):
        journal.reconcile("release-7", actual=baseline, now=104)
    restored = journal.reconcile("release-7", actual=baseline, healthy=True, now=105)
    assert restored.record.phase is JournalState.RESTORED_HEALTHY
    assert restored.record.receipt
    assert journal.reconcile("release-7", actual=payload, now=106).record == restored.record


def test_unbounded_start_window_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="invalid start window"):
        HostJournal(tmp_path / "host-journal.json", max_start_window_seconds=0)
    with pytest.raises(UnsafeTransition, match="invalid_prepare"):
        HostJournal(tmp_path / "host-journal.json").prepare(
            operation_id="release-7",
            baseline=identity(),
            payload=identity(git_sha="e" * 40),
            start_deadline=221,
            recovery_deadline=500,
            now=100,
        )


def test_archived_terminal_receipt_survives_next_operation_and_old_retry(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    journal = HostJournal(path)
    first_request = {
        "operation_id": "release-7",
        "baseline": identity(),
        "payload": identity(git_sha="e" * 40),
        "start_deadline": 180,
        "recovery_deadline": 500,
        "now": 100,
    }
    journal.prepare(**first_request)
    journal.begin_switch("release-7", now=101)
    journal.reconcile("release-7", actual=first_request["payload"], now=102)
    terminal = journal.reconcile(
        "release-7", actual=first_request["payload"], healthy=True, now=103
    ).record

    journal.archive_terminal("release-7", terminal.receipt)
    second_request = {
        "operation_id": "release-8",
        "baseline": first_request["payload"],
        "payload": identity(git_sha="9" * 40, target_sequence=9),
        "start_deadline": 220,
        "recovery_deadline": 500,
        "now": 150,
    }
    current = journal.prepare(**second_request)
    assert current.phase is JournalState.PREPARED
    assert journal.get_record("release-7") == terminal

    assert journal.prepare(**first_request) == terminal
    assert journal.get_record("release-8") == current
    with pytest.raises(OperationConflict):
        journal.prepare(**{**first_request, "payload": identity(git_sha="a" * 64)})


def test_archiving_requires_exact_terminal_receipt_and_is_idempotent(tmp_path) -> None:
    journal = HostJournal(tmp_path / "host-journal.json")
    request = {
        "operation_id": "release-7",
        "baseline": identity(),
        "payload": identity(git_sha="e" * 40),
        "start_deadline": 180,
        "recovery_deadline": 500,
        "now": 100,
    }
    record = journal.prepare(**request)

    with pytest.raises(UnsafeTransition):
        journal.archive_terminal("release-7", "wrong-receipt")
    with pytest.raises(OperationConflict):
        journal.archive_terminal("release-8", "wrong-receipt")
    assert journal.get_record("release-7") == record

    journal.begin_switch("release-7", now=101)
    journal.reconcile("release-7", actual=request["payload"], now=102)
    terminal = journal.reconcile(
        "release-7", actual=request["payload"], healthy=True, now=103
    ).record
    journal.archive_terminal("release-7", terminal.receipt)
    assert journal.archive_terminal("release-7", terminal.receipt) == terminal
    assert journal.get_record("release-7") == terminal


def test_archive_written_before_current_clear_is_safe_to_retry(tmp_path, monkeypatch) -> None:
    journal = HostJournal(tmp_path / "host-journal.json")
    payload = identity(git_sha="e" * 40)
    journal.prepare(
        operation_id="release-7",
        baseline=identity(),
        payload=payload,
        start_deadline=180,
        recovery_deadline=500,
        now=100,
    )
    journal.begin_switch("release-7", now=101)
    journal.reconcile("release-7", actual=payload, now=102)
    terminal = journal.reconcile("release-7", actual=payload, healthy=True, now=103).record

    def interrupted_clear() -> None:
        raise HostJournalError("simulated_interruption")

    monkeypatch.setattr(journal, "_clear_current_locked", interrupted_clear)
    with pytest.raises(HostJournalError, match="simulated_interruption"):
        journal.archive_terminal("release-7", terminal.receipt)
    assert journal.get_record("release-7") == terminal

    monkeypatch.undo()
    assert journal.archive_terminal("release-7", terminal.receipt) == terminal


def test_archived_operation_retries_are_read_only_while_newer_operation_is_active(tmp_path) -> None:
    journal = HostJournal(tmp_path / "host-journal.json")
    old_payload = identity(git_sha="e" * 40)
    journal.prepare(
        operation_id="release-7",
        baseline=identity(),
        payload=old_payload,
        start_deadline=180,
        recovery_deadline=500,
        now=100,
    )
    journal.begin_switch("release-7", now=101)
    journal.reconcile("release-7", actual=old_payload, now=102)
    terminal = journal.reconcile("release-7", actual=old_payload, healthy=True, now=103).record
    journal.archive_terminal("release-7", terminal.receipt)
    current = journal.prepare(
        operation_id="release-8",
        baseline=old_payload,
        payload=identity(git_sha="9" * 40, target_sequence=9),
        start_deadline=220,
        recovery_deadline=500,
        now=150,
    )

    assert journal.get_record("release-7") == terminal
    assert journal.begin_switch("release-7", now=151) == terminal
    replay = journal.reconcile("release-7", actual=None, now=152)
    assert replay.action is ReconciliationAction.ALREADY_TERMINAL
    assert replay.record == terminal
    assert journal.get_record("release-8") == current


@pytest.mark.parametrize("operation_id", ["../outside", "", "a/b", "."])
def test_public_journal_lookups_reject_invalid_operation_ids(tmp_path, operation_id: str) -> None:
    journal = HostJournal(tmp_path / "host-journal.json")
    journal.prepare(
        operation_id="release-7",
        baseline=identity(),
        payload=identity(git_sha="e" * 40),
        start_deadline=180,
        recovery_deadline=500,
        now=100,
    )

    with pytest.raises(UnsafeTransition):
        journal.get_record(operation_id)
    with pytest.raises(UnsafeTransition):
        journal.begin_switch(operation_id, now=101)
    with pytest.raises(UnsafeTransition):
        journal.reconcile(operation_id, actual=identity(), now=101)


def test_recovery_deadline_persists_and_ignores_new_constructor_limit(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    request = {
        "operation_id": "release-7",
        "baseline": identity(),
        "payload": identity(git_sha="e" * 40),
        "start_deadline": 180,
        "recovery_deadline": 250,
        "now": 100,
    }
    original = HostJournal(path, max_recovery_window_seconds=200).prepare(**request)
    reopened = HostJournal(path, max_recovery_window_seconds=1)

    assert reopened.get_record("release-7") == original
    assert original.recovery_deadline == 250
    assert reopened.prepare(**{**request, "now": 200}) == original


def test_expired_recovery_requires_reconciliation_without_authorizing_restore(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    journal = HostJournal(path, max_recovery_window_seconds=30)
    payload = identity(git_sha="e" * 40)
    journal.prepare(
        operation_id="release-7",
        baseline=identity(),
        payload=payload,
        start_deadline=120,
        recovery_deadline=130,
        now=100,
    )
    journal.begin_switch("release-7", now=101)
    journal.reconcile("release-7", actual=payload, now=102)
    journal.reconcile("release-7", actual=payload, healthy=False, now=129)

    expired = journal.reconcile("release-7", actual=payload, now=130)
    assert expired.action is ReconciliationAction.RECONCILIATION_REQUIRED
    assert expired.observed_actual == payload
    assert expired.record.phase is JournalState.RESTORING
    again = journal.reconcile("release-7", actual=payload, now=131)
    assert again.action is ReconciliationAction.RECONCILIATION_REQUIRED
    assert again.record == expired.record


def test_terminal_receipt_remains_reportable_after_recovery_deadline(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    journal = HostJournal(path)
    payload = identity(git_sha="e" * 40)
    journal.prepare(
        operation_id="release-7",
        baseline=identity(),
        payload=payload,
        start_deadline=120,
        recovery_deadline=130,
        now=100,
    )
    journal.begin_switch("release-7", now=101)
    journal.reconcile("release-7", actual=payload, now=102)
    terminal = journal.reconcile("release-7", actual=payload, healthy=True, now=129).record

    observed = identity(git_sha="9" * 40, config_generation=5, target_sequence=8)
    replay = journal.reconcile("release-7", actual=observed, now=1000)
    assert replay.action is ReconciliationAction.ALREADY_TERMINAL
    assert replay.record == terminal
    assert replay.observed_actual == observed


def test_terminal_receipt_binds_persisted_recovery_deadline(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    journal = HostJournal(path)
    payload = identity(git_sha="e" * 40)
    journal.prepare(
        operation_id="release-7",
        baseline=identity(),
        payload=payload,
        start_deadline=120,
        recovery_deadline=130,
        now=100,
    )
    journal.begin_switch("release-7", now=101)
    journal.reconcile("release-7", actual=payload, now=102)
    terminal = journal.reconcile("release-7", actual=payload, healthy=True, now=103).record
    journal.archive_terminal("release-7", terminal.receipt)

    archive = path.with_name(path.name + ".receipts") / "release-7.json"
    stored = json.loads(archive.read_text(encoding="utf-8"))
    assert stored["recovery_deadline"] == 130
    stored["recovery_deadline"] = 131
    archive.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(HostJournalError):
        journal.get_record("release-7")


def test_terminal_receipt_binds_observation_time_for_exact_broker_retry(tmp_path) -> None:
    path = tmp_path / "host-journal.json"
    journal = HostJournal(path)
    payload = identity(git_sha="e" * 40)
    journal.prepare(
        operation_id="release-7",
        baseline=identity(),
        payload=payload,
        start_deadline=120,
        recovery_deadline=130,
        now=100,
    )
    journal.begin_switch("release-7", now=101)
    journal.reconcile("release-7", actual=payload, now=102)
    terminal = journal.reconcile("release-7", actual=payload, healthy=True, now=103).record
    journal.archive_terminal("release-7", terminal.receipt)
    archive = path.with_name(path.name + ".receipts") / "release-7.json"
    stored = json.loads(archive.read_text(encoding="utf-8"))

    assert stored["terminal_at"] == 103
    assert journal.get_record("release-7").terminal_at == 103
    stored["terminal_at"] = 104
    archive.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(HostJournalError):
        journal.get_record("release-7")


def test_constructor_rejects_start_window_above_installed_hard_maximum(tmp_path):
    from chatcore_release_control.host_journal import MAX_START_WINDOW_SECONDS

    with pytest.raises(ValueError):
        HostJournal(
            tmp_path / "journal.json", max_start_window_seconds=MAX_START_WINDOW_SECONDS + 1
        )
