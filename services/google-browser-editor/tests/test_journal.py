import sqlite3
from datetime import UTC, datetime, timedelta

from chatcore_google_browser_editor.journal import JobJournal


def test_reservation_persists_hash_and_metadata_without_operation_text(tmp_path) -> None:
    path = tmp_path / "jobs.sqlite3"
    journal = JobJournal(path)
    result = journal.reserve(
        job_id="opaque-job-000000000001",
        payload_hash="a" * 64,
        destination_file_id="file-id-1234567890",
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    assert result.kind == "accepted"
    journal.close()
    raw = path.read_bytes()
    assert b"private phrase" not in raw
    assert journal.__class__(path).get("opaque-job-000000000001").phase == "accepted"


def test_same_hash_replays_status_and_other_hash_conflicts(tmp_path) -> None:
    journal = JobJournal(tmp_path / "jobs.sqlite3")
    args = dict(
        job_id="opaque-job-000000000001",
        payload_hash="a" * 64,
        destination_file_id="file-id-1234567890",
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    assert journal.reserve(**args).kind == "accepted"
    assert journal.reserve(**args).kind == "replay"
    args["payload_hash"] = "b" * 64
    assert journal.reserve(**args).kind == "conflict"


def test_only_one_active_and_one_queued_job_are_reserved(tmp_path) -> None:
    journal = JobJournal(tmp_path / "jobs.sqlite3")
    expires = datetime.now(UTC) + timedelta(seconds=30)
    for job_id in ("opaque-job-000000000001", "opaque-job-000000000002"):
        assert (
            journal.reserve(job_id, job_id[-1] * 64, "file-id-1234567890", expires).kind
            == "accepted"
        )
    assert (
        journal.reserve("opaque-job-000000000003", "c" * 64, "file-id-1234567890", expires).kind
        == "busy"
    )


def test_restart_never_resumes_accepted_or_running_jobs(tmp_path) -> None:
    path = tmp_path / "jobs.sqlite3"
    journal = JobJournal(path)
    expiry = datetime.now(UTC) + timedelta(seconds=30)
    journal.reserve("opaque-job-000000000001", "a" * 64, "file-id-1234567890", expiry)
    journal.reserve("opaque-job-000000000002", "b" * 64, "file-id-1234567890", expiry)
    journal.transition("opaque-job-000000000002", "running")
    journal.close()

    recovered = JobJournal(path)
    recovered.recover_after_restart()
    assert recovered.get("opaque-job-000000000001").phase == "failed_before_edit"
    assert recovered.get("opaque-job-000000000002").phase == "uncertain"


def test_tombstone_blocks_replay_after_metadata_expiry_and_purge(tmp_path) -> None:
    journal = JobJournal(tmp_path / "jobs.sqlite3", terminal_retention=10, tombstone_retention=400)
    now = datetime.now(UTC)
    journal.reserve(
        "opaque-job-000000000001", "a" * 64, "file-id-1234567890", now + timedelta(seconds=30)
    )
    journal.transition("opaque-job-000000000001", "failed_before_edit", now=now)
    journal.purge_expired(now=now + timedelta(seconds=11))
    assert journal.get("opaque-job-000000000001") is None
    replay = journal.reserve(
        "opaque-job-000000000001", "a" * 64, "file-id-1234567890", now + timedelta(seconds=30)
    )
    assert replay.kind == "tombstoned"
    tombstone = journal.get_tombstone("opaque-job-000000000001")
    assert tombstone.payload_hash == "a" * 64
    assert tombstone.day is not None


def test_delete_terminal_metadata_keeps_tombstone(tmp_path) -> None:
    journal = JobJournal(tmp_path / "jobs.sqlite3")
    journal.reserve(
        "opaque-job-000000000001",
        "a" * 64,
        "file-id-1234567890",
        datetime.now(UTC) + timedelta(seconds=30),
    )
    journal.transition("opaque-job-000000000001", "blocked")
    journal.delete_terminal("opaque-job-000000000001")
    assert journal.get("opaque-job-000000000001") is None
    assert journal.get_tombstone("opaque-job-000000000001") is not None


def test_terminal_transition_and_tombstone_are_atomic(tmp_path) -> None:
    journal = JobJournal(tmp_path / "jobs.sqlite3")
    journal.reserve(
        "opaque-job-000000000001",
        "a" * 64,
        "file-id-1234567890",
        datetime.now(UTC) + timedelta(seconds=30),
    )
    journal.transition("opaque-job-000000000001", "running")
    journal._db.execute(
        "CREATE TRIGGER fail_tombstone BEFORE INSERT ON tombstones "
        "BEGIN SELECT RAISE(ABORT,'test'); END"
    )
    try:
        journal.transition("opaque-job-000000000001", "uncertain")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("the forced tombstone failure should abort the transition")
    assert journal.get("opaque-job-000000000001").phase == "running"
    assert journal.get_tombstone("opaque-job-000000000001") is None


def test_awaiting_verification_can_be_cancelled_to_uncertain(tmp_path) -> None:
    journal = JobJournal(tmp_path / "jobs.sqlite3")
    journal.reserve(
        "opaque-job-000000000001",
        "a" * 64,
        "file-id-1234567890",
        datetime.now(UTC) + timedelta(seconds=30),
    )
    journal.transition("opaque-job-000000000001", "running")
    journal.transition("opaque-job-000000000001", "awaiting_verification")
    assert journal.transition("opaque-job-000000000001", "uncertain").phase == "uncertain"


def test_journal_rejects_symlink_and_requires_secure_permissions(tmp_path) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)
    try:
        JobJournal(link)
    except ValueError:
        pass
    else:
        raise AssertionError("database symlinks must be refused")


def test_journal_has_single_process_owner(tmp_path) -> None:
    path = tmp_path / "jobs.sqlite3"
    owner = JobJournal(path)
    try:
        try:
            JobJournal(path)
        except RuntimeError as exc:
            assert "already owned" in str(exc)
        else:
            raise AssertionError("a second worker must not own the same account journal")
    finally:
        owner.close()
    reopened = JobJournal(path)
    reopened.close()
