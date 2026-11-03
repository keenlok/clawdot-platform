"""SQLite replay, crash and retention journal. Operation text is never persisted."""

from __future__ import annotations

import fcntl
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

TERMINAL = {"verified", "needs_login", "blocked", "failed_before_edit", "uncertain"}
ACTIVE = {"accepted", "running"}


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    payload_hash: str
    destination_file_id: str
    phase: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    error_category: str | None


@dataclass(frozen=True, slots=True)
class Tombstone:
    payload_hash: str
    day: str


@dataclass(frozen=True, slots=True)
class ReserveResult:
    kind: str
    job: JobRecord | None = None


def _stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class JobJournal:
    def __init__(
        self,
        path: str | Path,
        *,
        terminal_retention: int = 24 * 60 * 60,
        tombstone_retention: int = 400 * 24 * 60 * 60,
    ) -> None:
        self.path = Path(path)
        if self.path.parent.is_symlink():
            raise ValueError("journal directory cannot be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        if self.path.is_symlink():
            raise ValueError("journal path cannot be a symlink")
        lock_path = Path(f"{self.path}.lock")
        if lock_path.is_symlink():
            raise ValueError("journal lock cannot be a symlink")
        self._lock_fd = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(self._lock_fd, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._lock_fd)
            raise RuntimeError("journal is already owned by another worker") from exc
        self.terminal_retention = terminal_retention
        self.tombstone_retention = tombstone_retention
        try:
            self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA secure_delete=ON")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    destination_file_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    error_category TEXT
                );
                CREATE TABLE IF NOT EXISTS tombstones (
                    job_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    day TEXT NOT NULL
                );
                """
            )
            self._secure_file()
        except BaseException:
            os.close(self._lock_fd)
            raise

    def _secure_file(self) -> None:
        self.path.chmod(0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            if sidecar.exists():
                sidecar.chmod(0o600)

    def close(self) -> None:
        try:
            self.checkpoint()
            self._db.close()
        finally:
            os.close(self._lock_fd)

    def checkpoint(self) -> None:
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._secure_file()

    def _record(self, row: sqlite3.Row | None) -> JobRecord | None:
        if row is None:
            return None
        return JobRecord(
            row["job_id"],
            row["payload_hash"],
            row["destination_file_id"],
            row["phase"],
            _dt(row["created_at"]),
            _dt(row["updated_at"]),
            _dt(row["expires_at"]),
            row["error_category"],
        )

    def get(self, job_id: str) -> JobRecord | None:
        return self._record(
            self._db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        )

    def get_tombstone(self, job_id: str) -> Tombstone | None:
        row = self._db.execute(
            "SELECT payload_hash, day FROM tombstones WHERE job_id=?", (job_id,)
        ).fetchone()
        return Tombstone(row["payload_hash"], row["day"]) if row else None

    def reserve(
        self,
        job_id: str,
        payload_hash: str,
        destination_file_id: str,
        expires_at: datetime,
        *,
        now: datetime | None = None,
    ) -> ReserveResult:
        current = now or datetime.now(UTC)
        now_text = _stamp(current)
        expiry_text = _stamp(expires_at)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            existing = self._record(
                self._db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            )
            if existing:
                self._db.execute("COMMIT")
                return ReserveResult(
                    "replay" if existing.payload_hash == payload_hash else "conflict", existing
                )
            tombstone = self.get_tombstone(job_id)
            if tombstone:
                self._db.execute("COMMIT")
                return ReserveResult("tombstoned")
            if _dt(expiry_text) <= current.astimezone(UTC):
                self._db.execute("COMMIT")
                return ReserveResult("expired")
            pending = self._db.execute(
                "SELECT COUNT(*) FROM jobs WHERE phase IN ('accepted', 'running')"
            ).fetchone()[0]
            if pending >= 2:
                self._db.execute("COMMIT")
                return ReserveResult("busy")
            self._db.execute(
                "INSERT INTO jobs(job_id,payload_hash,destination_file_id,phase,created_at,"
                "updated_at,expires_at)"
                " VALUES(?,?,?,'accepted',?,?,?)",
                (job_id, payload_hash, destination_file_id, now_text, now_text, expiry_text),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._secure_file()
        return ReserveResult("accepted", self.get(job_id))

    def transition(
        self,
        job_id: str,
        phase: str,
        *,
        error_category: str | None = None,
        now: datetime | None = None,
    ) -> JobRecord | None:
        if phase not in {"accepted", "running", "awaiting_verification", *TERMINAL}:
            raise ValueError("invalid phase")
        current = now or datetime.now(UTC)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._record(
                self._db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            )
            if row is None:
                self._db.execute("COMMIT")
                return None
            allowed = {
                "accepted": {"running", "failed_before_edit", "needs_login", "blocked"},
                "running": {"awaiting_verification", "uncertain"},
                "awaiting_verification": {"verified", "uncertain"},
            }
            if phase != row.phase and phase not in allowed.get(row.phase, set()):
                raise ValueError(f"invalid phase transition: {row.phase} -> {phase}")
            self._db.execute(
                "UPDATE jobs SET phase=?,updated_at=?,error_category=? WHERE job_id=?",
                (phase, _stamp(current), error_category, job_id),
            )
            if phase in TERMINAL:
                self._insert_tombstone(job_id, row.payload_hash, current)
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._secure_file()
        return self.get(job_id)

    def _insert_tombstone(self, job_id: str, payload_hash: str, now: datetime) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO tombstones(job_id,payload_hash,day) VALUES(?,?,?)",
            (job_id, payload_hash, now.astimezone(UTC).date().isoformat()),
        )

    def recover_after_restart(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            rows = self._db.execute(
                "SELECT job_id,payload_hash,phase FROM jobs WHERE phase IN ('accepted','running')"
            ).fetchall()
            for row in rows:
                phase = "failed_before_edit" if row["phase"] == "accepted" else "uncertain"
                self._db.execute(
                    "UPDATE jobs SET phase=?,updated_at=?,error_category='worker_restarted' "
                    "WHERE job_id=?",
                    (phase, _stamp(current), row["job_id"]),
                )
                self._insert_tombstone(row["job_id"], row["payload_hash"], current)
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._secure_file()

    def delete_terminal(self, job_id: str) -> bool:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._record(
                self._db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            )
            if row is None or row.phase not in TERMINAL:
                self._db.execute("COMMIT")
                return False
            self._insert_tombstone(job_id, row.payload_hash, row.updated_at)
            self._db.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self.checkpoint()
        self._secure_file()
        return True

    def purge_expired(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        metadata_before = _stamp(current - timedelta(seconds=self.terminal_retention))
        tombstone_before = (current - timedelta(seconds=self.tombstone_retention)).astimezone(UTC)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            old_rows = self._db.execute(
                "SELECT job_id,payload_hash,updated_at FROM jobs WHERE phase IN "
                "('verified','needs_login','blocked','failed_before_edit','uncertain') "
                "AND updated_at < ?",
                (metadata_before,),
            ).fetchall()
            for row in old_rows:
                self._insert_tombstone(row["job_id"], row["payload_hash"], _dt(row["updated_at"]))
            self._db.execute(
                "DELETE FROM jobs WHERE phase IN "
                "('verified','needs_login','blocked','failed_before_edit','uncertain')"
                " AND updated_at < ?",
                (metadata_before,),
            )
            self._db.execute(
                "DELETE FROM tombstones WHERE day < ?", (tombstone_before.date().isoformat(),)
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self.checkpoint()
        self._secure_file()
