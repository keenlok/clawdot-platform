"""Concrete P03 publication orchestration and private P04 CI gateway routes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import resource
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .candidate_bundle import build_candidate_bundle
from .development import (
    DevelopmentController,
    DevelopmentError,
    GenerationRequest,
)
from .git_evidence import (
    COMMAND_TIMEOUT_SECONDS,
    GitEvidenceError,
    _object_directory,
    collect_release_bundle,
)
from .github_app import GitHubAppTokenIssuer
from .hosted_bootstrap import (
    HostedBootstrapError,
    _extract_exact_archive,
    _validate_manifest,
)
from .policy import PolicyManifest
from .publication import (
    MAX_BUNDLE_BYTES,
    MAX_SCAN_ITEM_BYTES,
    MAX_TOTAL_SCAN_BYTES,
    ProvenanceRecord,
    PublicationError,
    PublicationReceipt,
    PublicationRequest,
    PublicationService,
    ScanInput,
    ScannerBinaryAdapter,
    ScanSurface,
    ScanVerdict,
)
from .publication_github import GitHubAppHttpPost, GitHubPublicationGateway, PullRequestReceipt

_REQUEST_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_.,-]{16,4096}\Z")
_MAX_HTTP_BODY = 1024 * 1024
_MAX_REPORT_BYTES = 512 * 1024
_MAX_CONFIG_BYTES = 256 * 1024
_MAX_BOOTSTRAP_SOURCE_BYTES = 1024 * 1024
_MAX_BOOTSTRAP_MANIFEST_BYTES = 64 * 1024
_MAX_BOOTSTRAP_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_SUBMISSION_RECORD_BYTES = 160 * 1024 * 1024
_MAX_SUBMISSION_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_SUBMISSION_COUNT = 256
_MAX_GATEWAY_DATABASE_BYTES = 1024 * 1024 * 1024


class PublicationGatewayError(ValueError):
    """Safe error codes from local publication state and the private CI API."""

    def __init__(self, code: str = "publication_gateway_failed") -> None:
        if code not in {
            "invalid_configuration",
            "invalid_request",
            "state_conflict",
            "state_unavailable",
            "context_not_found",
            "report_rejected",
            "runtime_unavailable",
        }:
            code = "publication_gateway_failed"
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, repr=False)
class PublishedCandidate:
    publication: PublicationReceipt
    pull_request: PullRequestReceipt

    def __repr__(self) -> str:
        return (
            "PublishedCandidate(request_id="
            f"{self.publication.request_id!r}, pull_request_number="
            f"{self.pull_request.pull_request_number}, head_sha={self.publication.head_commit!r})"
        )


@dataclass(frozen=True, repr=False)
class PublicationSubmission:
    """Private durable inputs needed to resume the exact frozen publication."""

    request: PublicationRequest = field(repr=False)
    synthetic_examples: list[object] = field(repr=False)
    development_report: bytes | None = field(repr=False)

    def __repr__(self) -> str:
        return f"PublicationSubmission(request_id={self.request.request_id!r}, [private])"


class PublicationGatewayStore:
    """Append-only SQLite provenance, PR context, and scanned CI report storage."""

    def __init__(self, database: str | os.PathLike[str]) -> None:
        try:
            self.path = Path(database).resolve(strict=False)
            if not self.path.is_absolute():
                raise ValueError
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent_info = self.path.parent.lstat()
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(parent_info.st_mode) & 0o077
            ):
                raise ValueError
            try:
                existing = self.path.lstat()
            except FileNotFoundError:
                existing = None
            if existing is not None and (
                not stat.S_ISREG(existing.st_mode)
                or existing.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(existing.st_mode) & 0o077
                or existing.st_size > 1024 * 1024 * 1024
            ):
                raise ValueError
            with sqlite3.connect(self.path, timeout=5.0) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS publication_provenance ("
                    "request_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, "
                    "record_sha256 TEXT NOT NULL, created_at REAL NOT NULL) WITHOUT ROWID"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS publication_contexts ("
                    "request_id TEXT PRIMARY KEY, pull_request_number INTEGER UNIQUE NOT NULL, "
                    "context_json TEXT NOT NULL, context_sha256 TEXT NOT NULL, "
                    "created_at REAL NOT NULL) WITHOUT ROWID"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ci_reports ("
                    "request_id TEXT PRIMARY KEY, pull_request_number INTEGER NOT NULL, "
                    "head_sha TEXT NOT NULL, policy_digest TEXT NOT NULL, "
                    "context_sha256 TEXT NOT NULL, "
                    "report_sha256 TEXT NOT NULL, report BLOB NOT NULL, "
                    "artifact_id TEXT UNIQUE NOT NULL, "
                    "created_at REAL NOT NULL) WITHOUT ROWID"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS publication_submissions ("
                    "request_id TEXT PRIMARY KEY, submission_json TEXT NOT NULL, "
                    "submission_sha256 TEXT NOT NULL, submission_bytes INTEGER NOT NULL, "
                    "created_at REAL NOT NULL) WITHOUT ROWID"
                )
                for table in (
                    "publication_provenance",
                    "publication_contexts",
                    "ci_reports",
                    "publication_submissions",
                ):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_update "
                        f"BEFORE UPDATE ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'immutable record'); END"
                    )
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete "
                        f"BEFORE DELETE ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'immutable record'); END"
                    )
            os.chmod(self.path, 0o600)
            final = self.path.lstat()
            if not stat.S_ISREG(final.st_mode) or final.st_uid not in {0, os.geteuid()}:
                raise ValueError
        except (OSError, sqlite3.Error, ValueError):
            raise PublicationGatewayError("state_unavailable") from None

    def __call__(self, record: ProvenanceRecord, /) -> None:
        if not isinstance(record, ProvenanceRecord):
            raise PublicationGatewayError("invalid_request")
        value = asdict(record)
        canonical = _canonical(value)
        digest = hashlib.sha256(canonical).hexdigest()
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT record_json FROM publication_provenance WHERE request_id=?",
                    (record.request_id,),
                ).fetchone()
                if row is not None:
                    if row[0] != canonical.decode():
                        raise PublicationGatewayError("state_conflict")
                    db.commit()
                    return
                db.execute(
                    "INSERT INTO publication_provenance VALUES (?, ?, ?, ?)",
                    (record.request_id, canonical.decode(), digest, time.time()),
                )
                db.commit()
        except PublicationGatewayError:
            raise
        except (OSError, sqlite3.Error):
            raise PublicationGatewayError("state_unavailable") from None

    def get_provenance(self, request_id: str) -> ProvenanceRecord | None:
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT record_json FROM publication_provenance WHERE request_id=?",
                    (request_id,),
                ).fetchone()
            if row is None:
                return None
            value = json.loads(row[0], object_pairs_hook=_unique_pairs)
            return ProvenanceRecord(**value)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            raise PublicationGatewayError("state_unavailable") from None

    def store_submission(
        self,
        request: PublicationRequest,
        synthetic_examples: object,
        *,
        development_report: bytes | None = None,
    ) -> None:
        """Persist exact private request bytes before any external publication effect.

        Records are immutable and never pruned. The gateway fails closed when a
        record, submission count, aggregate submission bytes, or database bound
        is exhausted.
        """

        value = _submission_data(request, synthetic_examples, development_report)
        canonical = _canonical(value)
        if len(canonical) > _MAX_SUBMISSION_RECORD_BYTES:
            raise PublicationGatewayError("state_unavailable")
        digest = hashlib.sha256(canonical).hexdigest()
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT submission_json FROM publication_submissions WHERE request_id=?",
                    (request.request_id,),
                ).fetchone()
                if row is not None:
                    if row[0] != canonical.decode("utf-8"):
                        raise PublicationGatewayError("state_conflict")
                    db.commit()
                    return
                count, total = db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(submission_bytes), 0) "
                    "FROM publication_submissions"
                ).fetchone()
                if count >= _MAX_SUBMISSION_COUNT or total + len(canonical) > (
                    _MAX_SUBMISSION_TOTAL_BYTES
                ):
                    raise PublicationGatewayError("state_unavailable")
                db.execute(
                    "INSERT INTO publication_submissions VALUES (?, ?, ?, ?, ?)",
                    (
                        request.request_id,
                        canonical.decode("utf-8"),
                        digest,
                        len(canonical),
                        time.time(),
                    ),
                )
                page_count = db.execute("PRAGMA page_count").fetchone()[0]
                page_size = db.execute("PRAGMA page_size").fetchone()[0]
                wal_size = _file_size_if_regular(Path(str(self.path) + "-wal"))
                shm_size = _file_size_if_regular(Path(str(self.path) + "-shm"))
                if page_count * page_size + wal_size + shm_size > _MAX_GATEWAY_DATABASE_BYTES:
                    raise PublicationGatewayError("state_unavailable")
                db.commit()
        except PublicationGatewayError:
            raise
        except (OSError, sqlite3.Error, UnicodeError, TypeError, ValueError):
            raise PublicationGatewayError("state_unavailable") from None

    def get_submission(self, request_id: str) -> PublicationSubmission | None:
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise PublicationGatewayError("invalid_request")
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT submission_json, submission_sha256, submission_bytes "
                    "FROM publication_submissions WHERE request_id=?",
                    (request_id,),
                ).fetchone()
            if row is None:
                return None
            raw = row[0].encode("utf-8", errors="strict")
            if (
                len(raw) != row[2]
                or len(raw) > _MAX_SUBMISSION_RECORD_BYTES
                or hashlib.sha256(raw).hexdigest() != row[1]
            ):
                raise ValueError
            value = json.loads(raw, object_pairs_hook=_unique_pairs)
            return _submission_from_data(value, request_id)
        except (OSError, sqlite3.Error, UnicodeError, ValueError, TypeError):
            raise PublicationGatewayError("state_unavailable") from None

    def store_context(self, context: Mapping[str, object]) -> None:
        try:
            _validate_persisted_context(context)
            canonical = _canonical(dict(context))
            digest = hashlib.sha256(canonical).hexdigest()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT pull_request_number, context_json FROM publication_contexts "
                    "WHERE request_id=? OR pull_request_number=?",
                    (context["request_id"], context["pull_request_number"]),
                ).fetchone()
                if row is not None:
                    if row != (context["pull_request_number"], canonical.decode()):
                        raise PublicationGatewayError("state_conflict")
                    db.commit()
                    return
                provenance_row = db.execute(
                    "SELECT record_json FROM publication_provenance WHERE request_id=?",
                    (context["request_id"],),
                ).fetchone()
                if provenance_row is None or provenance_row[0] != _canonical(
                    context["provenance"]
                ).decode("utf-8"):
                    raise PublicationGatewayError("state_conflict")
                db.execute(
                    "INSERT INTO publication_contexts VALUES (?, ?, ?, ?, ?)",
                    (
                        context["request_id"],
                        context["pull_request_number"],
                        canonical.decode(),
                        digest,
                        time.time(),
                    ),
                )
                db.commit()
        except PublicationGatewayError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, UnicodeError):
            raise PublicationGatewayError("invalid_request") from None

    def get_context(self, pull_request_number: int) -> dict[str, Any]:
        if type(pull_request_number) is not int or pull_request_number <= 0:
            raise PublicationGatewayError("invalid_request")
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT context_json FROM publication_contexts WHERE pull_request_number=?",
                    (pull_request_number,),
                ).fetchone()
            if row is None:
                raise PublicationGatewayError("context_not_found")
            value = json.loads(row[0], object_pairs_hook=_unique_pairs)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except PublicationGatewayError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError):
            raise PublicationGatewayError("state_unavailable") from None

    def store_report(
        self,
        context: Mapping[str, object],
        report: bytes,
        report_sha256: str,
    ) -> dict[str, object]:
        if (
            not isinstance(context, Mapping)
            or not isinstance(report, bytes)
            or not 1 <= len(report) <= _MAX_REPORT_BYTES
            or not isinstance(report_sha256, str)
            or not _SHA256.fullmatch(report_sha256)
            or hashlib.sha256(report).hexdigest() != report_sha256
        ):
            raise PublicationGatewayError("invalid_request")
        request_id = context["request_id"]
        artifact_id = f"ci-{request_id}-{report_sha256[:32]}"
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                stored_context = db.execute(
                    "SELECT context_json FROM publication_contexts WHERE request_id=? "
                    "AND pull_request_number=?",
                    (request_id, context["pull_request_number"]),
                ).fetchone()
                if stored_context is None or stored_context[0] != _canonical(context).decode(
                    "utf-8"
                ):
                    raise PublicationGatewayError("state_conflict")
                existing = db.execute(
                    "SELECT pull_request_number, head_sha, policy_digest, context_sha256, "
                    "report_sha256, report, artifact_id FROM ci_reports WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                expected = (
                    context["pull_request_number"],
                    context["head_commit"],
                    context["policy_digest"],
                    context["context_sha256"],
                    report_sha256,
                    report,
                    artifact_id,
                )
                if existing is not None:
                    if existing != expected:
                        raise PublicationGatewayError("state_conflict")
                    db.commit()
                else:
                    db.execute(
                        "INSERT INTO ci_reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (request_id, *expected, time.time()),
                    )
                    db.commit()
        except PublicationGatewayError:
            raise
        except (OSError, sqlite3.Error, TypeError, KeyError):
            raise PublicationGatewayError("state_unavailable") from None
        return {
            "request_id": request_id,
            "pull_request_number": context["pull_request_number"],
            "head_sha": context["head_commit"],
            "report_sha256": report_sha256,
            "artifact_id": artifact_id,
            "stored": True,
        }

    def get_report(self, request_id: str) -> tuple[str, bytes] | None:
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT report_sha256, report FROM ci_reports WHERE request_id=?",
                    (request_id,),
                ).fetchone()
            return None if row is None else (row[0], bytes(row[1]))
        except (OSError, sqlite3.Error):
            raise PublicationGatewayError("state_unavailable") from None

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        db.execute("PRAGMA synchronous=FULL")
        return db


class PublicationGatewayService:
    """Connect real isolated generation to scan, push, PR, and immutable context."""

    def __init__(
        self,
        *,
        publisher: PublicationService,
        github: Any,
        store: PublicationGatewayStore,
        repository_path: str | os.PathLike[str],
        policy: PolicyManifest,
        policy_manifest_json: bytes,
        repository: str,
        repository_id: int,
        owner_id: int,
    ) -> None:
        try:
            repo = Path(repository_path).resolve(strict=True)
            manifest = PolicyManifest.from_json(policy_manifest_json)
        except Exception:
            raise PublicationGatewayError("invalid_configuration") from None
        if (
            not isinstance(publisher, PublicationService)
            or not callable(getattr(github, "verify_repository", None))
            or not callable(getattr(github, "publish", None))
            or not callable(getattr(github, "create_or_get_pull_request", None))
            or not isinstance(store, PublicationGatewayStore)
            or not repo.is_dir()
            or manifest != policy
            or f"{getattr(github, 'owner', '')}/{getattr(github, 'repository', '')}" != repository
            or getattr(github, "repository_id", None) != repository_id
            or getattr(github, "owner_id", None) != owner_id
            or policy.repository_id != repository_id
            or policy.owner_id != owner_id
            or _canonical(json.loads(policy_manifest_json)) != policy_manifest_json
        ):
            raise PublicationGatewayError("invalid_configuration")
        self.publisher = publisher
        self.github = github
        self.store = store
        self.repository_path = repo
        self.policy = policy
        self.policy_manifest_json = policy_manifest_json
        self.repository = repository
        self.repository_id = repository_id
        self.owner_id = owner_id
        self.github.verify_repository()

    def close(self) -> None:
        close = getattr(self.github, "close", None)
        if callable(close):
            close()

    async def generate_and_publish(
        self,
        *,
        development: DevelopmentController,
        generation_request: GenerationRequest,
        request_id: str,
        deployed_commit: str,
        pull_request_title: str,
        pull_request_body: str,
        commit_timestamp: int,
        artifacts: tuple[ScanSurface, ...] = (),
    ) -> PublishedCandidate:
        if (
            not isinstance(development, DevelopmentController)
            or not isinstance(generation_request, GenerationRequest)
            or generation_request.base_commit
            != getattr(development.runner, "baseline_commit", None)
            or generation_request.policy_digest != self.policy.digest
        ):
            raise PublicationGatewayError("invalid_request")
        examples = _validate_synthetic_examples(generation_request.synthetic_examples)
        _validate_request_artifacts(artifacts)
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise PublicationGatewayError("invalid_request")
        if self.store.get_submission(request_id) is not None:
            if generation_request.base_commit != getattr(
                development.runner, "baseline_commit", None
            ):
                raise PublicationGatewayError("state_conflict")
            return self.resume_request(
                request_id,
                specification=generation_request.specification,
                pull_request_title=pull_request_title,
                pull_request_body=pull_request_body,
                synthetic_examples=examples,
                artifacts=artifacts,
            )
        try:
            result = await development.generate_and_run(generation_request)
        except DevelopmentError:
            raise PublicationGatewayError("report_rejected") from None
        if (
            not isinstance(result.report, bytes)
            or not result.report
            or len(result.report) > MAX_SCAN_ITEM_BYTES
            or hashlib.sha256(result.report).hexdigest() != result.report_digest
        ):
            raise PublicationGatewayError("report_rejected")
        request = build_candidate_bundle(
            repository=self.repository_path,
            result=result,
            request_id=request_id,
            deployed_commit=deployed_commit,
            base_commit=generation_request.base_commit,
            policy=self.policy,
            commit_timestamp=commit_timestamp,
            specification=generation_request.specification.encode("utf-8"),
            pull_request_title=pull_request_title.encode("utf-8"),
            pull_request_body=pull_request_body.encode("utf-8"),
            artifacts=artifacts,
        )
        return self.publish_request(
            request,
            examples,
            development_report=result.report,
        )

    def resume_request(
        self,
        request_id: str,
        *,
        specification: str,
        pull_request_title: str,
        pull_request_body: str,
        synthetic_examples: object,
        artifacts: tuple[ScanSurface, ...] = (),
    ) -> PublishedCandidate:
        """Resume a durable request without invoking the model or rebuilding H.

        The supplied logical request is compared against the private immutable
        record before rescanning, transport, or PR reconciliation.
        """

        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise PublicationGatewayError("invalid_request")
        examples = _validate_synthetic_examples(synthetic_examples)
        replay_artifacts = _validate_request_artifacts(artifacts)
        logical = (
            _logical_surface(specification, allow_empty=False),
            _logical_surface(pull_request_title, allow_empty=False),
            _logical_surface(pull_request_body, allow_empty=True),
            _canonical(examples),
            tuple((surface.name, surface.content) for surface in replay_artifacts),
        )
        submission = self.store.get_submission(request_id)
        if submission is None:
            raise PublicationGatewayError("context_not_found")
        saved = submission.request
        if logical != (
            saved.specification,
            saved.pull_request_title,
            saved.pull_request_body,
            _canonical(submission.synthetic_examples),
            tuple((surface.name, surface.content) for surface in saved.artifacts),
        ):
            raise PublicationGatewayError("state_conflict")
        return self.publish_request(
            saved,
            submission.synthetic_examples,
            development_report=submission.development_report,
        )

    def publish_request(
        self,
        request: PublicationRequest,
        synthetic_examples: tuple[object, ...],
        *,
        development_report: bytes | None = None,
    ) -> PublishedCandidate:
        if not isinstance(request, PublicationRequest):
            raise PublicationGatewayError("invalid_request")
        try:
            examples = _validate_synthetic_examples(synthetic_examples)
            _validate_request_artifacts(request.artifacts)
            raw_request = request
            surfaces = list(request.artifacts)
            surfaces.append(ScanSurface("synthetic-examples", _canonical(examples)))
            if development_report is not None:
                if (
                    not isinstance(development_report, bytes)
                    or not development_report
                    or len(development_report) > MAX_SCAN_ITEM_BYTES
                ):
                    raise PublicationGatewayError("invalid_request")
                surfaces.append(ScanSurface("development-report", development_report))
            request = replace(request, artifacts=tuple(surfaces))
            bundle = _inspect_bundle(request, self.policy)
            self.store.store_submission(
                raw_request,
                examples,
                development_report=development_report,
            )
            publication = self.publisher.publish(request)
            if publication.head_commit != request.head_commit:
                raise PublicationGatewayError("state_conflict")
            provenance = self.store.get_provenance(request.request_id)
            if provenance is None:
                raise PublicationGatewayError("state_conflict")
            title = request.pull_request_title.decode("utf-8", errors="strict")
            body = request.pull_request_body.decode("utf-8", errors="strict")
            try:
                pr = self.github.create_or_get_pull_request(
                    request_id=request.request_id,
                    head_sha=publication.head_commit,
                    base_sha=publication.base_commit,
                    title=title,
                    body=body,
                    base_branch="main",
                )
            except Exception:
                # The immutable submission allows an exact retry to reconcile a
                # response lost after the remote PR was created.
                raise PublicationGatewayError("state_conflict") from None
            if (
                pr.request_id != request.request_id
                or pr.head_sha != publication.head_commit
                or pr.base_sha != publication.base_commit
                or pr.head_ref != f"clawdot/{request.request_id}"
                or pr.base_ref != "main"
                or pr.state != "open"
            ):
                raise PublicationGatewayError("state_conflict")
            context = self._context(request, bundle, provenance, pr, examples)
            self.store.store_context(context)
            return PublishedCandidate(publication, pr)
        except PublicationGatewayError:
            raise
        except (PublicationError, GitEvidenceError, UnicodeError, ValueError, TypeError):
            raise PublicationGatewayError("state_conflict") from None

    def _context(
        self,
        request: PublicationRequest,
        bundle: Any,
        provenance: ProvenanceRecord,
        pr: PullRequestReceipt,
        examples: tuple[object, ...],
    ) -> dict[str, object]:
        try:
            if not isinstance(examples, (tuple, list)) or not 1 <= len(examples) <= 8:
                raise ValueError
            examples_value = list(examples)
            if len(_canonical(examples_value)) > 16 * 1024:
                raise ValueError
            specification = request.specification.decode("utf-8", errors="strict")
            title = request.pull_request_title.decode("utf-8", errors="strict")
            body = request.pull_request_body.decode("utf-8", errors="strict")
            artifacts = [
                {
                    "name": item.name,
                    "sha256": hashlib.sha256(item.content).hexdigest(),
                    "content_base64": base64.b64encode(item.content).decode("ascii"),
                }
                for item in request.artifacts
            ]
            value: dict[str, object] = {
                "version": 1,
                "request_id": request.request_id,
                "repository": self.repository,
                "repository_id": self.repository_id,
                "owner_id": self.owner_id,
                "pull_request_number": pr.pull_request_number,
                "base_branch": "main",
                "deployed_commit": request.deployed_commit,
                "base_commit": request.base_commit,
                "head_commit": request.head_commit,
                "head_tree": bundle.head_tree,
                "policy_digest": self.policy.digest,
                "policy_manifest": json.loads(self.policy_manifest_json),
                "provenance": asdict(provenance),
                "published_ref": f"refs/heads/clawdot/{request.request_id}",
                "specification": specification,
                "synthetic_examples": examples_value,
                "pull_request_title": title,
                "pull_request_body": body,
                "artifacts": artifacts,
            }
            value["context_sha256"] = hashlib.sha256(_canonical(value)).hexdigest()
            return value
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise PublicationGatewayError("invalid_request") from None


@dataclass(frozen=True, repr=False)
class BootstrapArtifactSet:
    """Fixed protected file binding for the hosted bootstrap download routes."""

    source_path: Path = field(repr=False)
    source_sha256: str
    manifest_path: Path = field(repr=False)
    manifest_sha256: str
    archive_path: Path = field(repr=False)
    archive_sha256: str
    archive_size: int

    def read(self, kind: str) -> bytes:
        if kind == "source":
            path, expected, maximum = (
                self.source_path,
                self.source_sha256,
                _MAX_BOOTSTRAP_SOURCE_BYTES,
            )
        elif kind == "manifest":
            path, expected, maximum = (
                self.manifest_path,
                self.manifest_sha256,
                _MAX_BOOTSTRAP_MANIFEST_BYTES,
            )
        elif kind == "archive":
            path, expected, maximum = (
                self.archive_path,
                self.archive_sha256,
                _MAX_BOOTSTRAP_ARCHIVE_BYTES,
            )
        else:
            raise PublicationGatewayError("invalid_request")
        try:
            data = _read_protected_file(path, max_bytes=maximum)
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError
            if kind == "archive" and len(data) != self.archive_size:
                raise ValueError
            return data
        except Exception:
            raise PublicationGatewayError("state_unavailable") from None


class PublicationGatewayASGI:
    """Bearer-authenticated, fixed-path ASGI API used by trusted GitHub-hosted CI."""

    def __init__(
        self,
        *,
        store: PublicationGatewayStore,
        ci_bearer_token: str,
        report_scanner: Any,
        bootstrap_artifacts: BootstrapArtifactSet | None = None,
    ) -> None:
        if (
            not isinstance(store, PublicationGatewayStore)
            or not isinstance(ci_bearer_token, str)
            or not _TOKEN.fullmatch(ci_bearer_token)
            or not callable(report_scanner)
        ):
            raise PublicationGatewayError("invalid_configuration")
        self._store = store
        self._token = ci_bearer_token
        self._scanner = report_scanner
        self._bootstrap = bootstrap_artifacts

    def __repr__(self) -> str:
        return "PublicationGatewayASGI([protected])"

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return
        method = scope.get("method")
        path = scope.get("path")
        if not isinstance(path, str) or scope.get("query_string"):
            await _asgi_response(send, 404, {"error": "not_found"})
            return
        header_items = scope.get("headers", [])
        names = [key.decode("latin-1").casefold() for key, _ in header_items]
        if any(
            names.count(name) > 1 for name in ("authorization", "content-type", "content-length")
        ):
            await _asgi_response(send, 400, {"error": "invalid_request"})
            return
        headers = {
            key.decode("latin-1").casefold(): value.decode("latin-1") for key, value in header_items
        }
        authorization = headers.get("authorization", "")
        supplied = authorization.removeprefix("Bearer ")
        if not authorization.startswith("Bearer ") or not hmac.compare_digest(
            supplied, self._token
        ):
            await _asgi_response(send, 401, {"error": "unauthorized"})
            return
        if method == "GET" and path in {
            "/v1/ci/bootstrap/hosted-bootstrap.py",
            "/v1/ci/bootstrap/manifest",
        }:
            if self._bootstrap is None:
                await _asgi_response(send, 404, {"error": "not_found"})
                return
            try:
                kind = "source" if path.endswith(".py") else "manifest"
                data = self._bootstrap.read(kind)
                content_type = (
                    "application/octet-stream" if kind == "source" else "application/json"
                )
                await _asgi_bytes_response(send, 200, data, content_type)
            except PublicationGatewayError as exc:
                await _asgi_response(send, 503, {"error": exc.code})
            return
        archive_match = re.fullmatch(r"/v1/ci/bootstrap/artifacts/([0-9a-f]{64})", path)
        if method == "GET" and archive_match is not None:
            if self._bootstrap is None or archive_match.group(1) != self._bootstrap.archive_sha256:
                await _asgi_response(send, 404, {"error": "not_found"})
                return
            try:
                data = self._bootstrap.read("archive")
                await _asgi_bytes_response(send, 200, data, "application/octet-stream")
            except PublicationGatewayError as exc:
                await _asgi_response(send, 503, {"error": exc.code})
            return
        match = re.fullmatch(r"/v1/ci/pull-requests/([1-9][0-9]{0,9})/(context|report)", path)
        if match is None:
            await _asgi_response(send, 404, {"error": "not_found"})
            return
        pull_number, operation = int(match.group(1)), match.group(2)
        if pull_number > 2**31 - 1:
            await _asgi_response(send, 404, {"error": "not_found"})
            return
        try:
            if operation == "context" and method == "GET":
                context = self._store.get_context(pull_number)
                await _asgi_response(send, 200, context)
                return
            if operation == "report" and method == "POST":
                if (
                    headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                    != "application/json"
                ):
                    await _asgi_response(send, 415, {"error": "invalid_content_type"})
                    return
                length = headers.get("content-length")
                if length is not None and (not length.isdigit() or int(length) > _MAX_HTTP_BODY):
                    await _asgi_response(send, 413, {"error": "request_too_large"})
                    return
                raw = await _read_asgi_body(receive)
                if length is not None and len(raw) != int(length):
                    await _asgi_response(send, 400, {"error": "invalid_request"})
                    return
                body = json.loads(raw, object_pairs_hook=_unique_pairs)
                context = self._store.get_context(pull_number)
                receipt = self._store_report(context, body)
                await _asgi_response(send, 201, receipt)
                return
            await _asgi_response(send, 405, {"error": "method_not_allowed"})
        except PublicationGatewayError as exc:
            status = (
                404
                if exc.code == "context_not_found"
                else 409
                if exc.code == "state_conflict"
                else 503
                if exc.code == "state_unavailable"
                else 422
            )
            await _asgi_response(send, status, {"error": exc.code})
        except (ValueError, TypeError, UnicodeError, RecursionError):
            await _asgi_response(send, 400, {"error": "invalid_request"})
        except TimeoutError:
            await _asgi_response(send, 408, {"error": "request_timeout"})

    def _store_report(self, context: Mapping[str, object], body: object) -> dict[str, object]:
        required = {
            "request_id",
            "head_sha",
            "policy_digest",
            "context_sha256",
            "report_sha256",
            "report_base64",
        }
        if (
            not isinstance(body, dict)
            or set(body) != required
            or body.get("request_id") != context.get("request_id")
            or body.get("head_sha") != context.get("head_commit")
            or body.get("policy_digest") != context.get("policy_digest")
            or body.get("context_sha256") != context.get("context_sha256")
            or not isinstance(body.get("report_sha256"), str)
            or not _SHA256.fullmatch(body["report_sha256"])
            or not isinstance(body.get("report_base64"), str)
            or len(body["report_base64"]) > ((_MAX_REPORT_BYTES + 2) // 3) * 4
        ):
            raise PublicationGatewayError("invalid_request")
        try:
            report = base64.b64decode(body["report_base64"], validate=True)
        except (ValueError, TypeError):
            raise PublicationGatewayError("invalid_request") from None
        if (
            not report
            or len(report) > _MAX_REPORT_BYTES
            or hashlib.sha256(report).hexdigest() != body["report_sha256"]
        ):
            raise PublicationGatewayError("invalid_request")
        try:
            verdict = self._scanner(ScanInput("ci-execution-report", report))
        except Exception:
            raise PublicationGatewayError("report_rejected") from None
        if verdict is not ScanVerdict.CLEAN:
            raise PublicationGatewayError("report_rejected")
        return self._store.store_report(context, report, body["report_sha256"])


@dataclass(frozen=True, repr=False)
class ProtectedPublicationConfig:
    values: Mapping[str, Any] = field(repr=False)
    digest: str
    ci_token: str = field(repr=False)

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> ProtectedPublicationConfig:
        source = os.environ if environment is None else environment
        try:
            config_path = Path(source["RELEASE_CONTROL_PUBLICATION_CONFIG_PATH"])
            digest = source["RELEASE_CONTROL_PUBLICATION_CONFIG_SHA256"]
            raw = _read_protected_file(config_path, max_bytes=_MAX_CONFIG_BYTES)
            if (
                not config_path.is_absolute()
                or not isinstance(digest, str)
                or not _SHA256.fullmatch(digest)
                or hashlib.sha256(raw).hexdigest() != digest
            ):
                raise ValueError
            values = json.loads(raw, object_pairs_hook=_unique_pairs)
            if not isinstance(values, dict) or _canonical(values) != raw:
                raise ValueError
            _validate_config(values)
            if source.get("RELEASE_CONTROL_PUBLICATION_MODE") != "gateway":
                raise ValueError
            token = _read_protected_file(Path(values["ci_token_path"]), max_bytes=4096).decode(
                "ascii"
            )
            if not _TOKEN.fullmatch(token.strip()) or token.strip() != token:
                raise ValueError
            return cls(values, digest, token)
        except Exception:
            raise PublicationGatewayError("invalid_configuration") from None


def build_publication_gateway(
    environment: Mapping[str, str] | None = None,
    *,
    github_transport: httpx.BaseTransport | None = None,
    app_http_transport: httpx.BaseTransport | None = None,
) -> tuple[PublicationGatewayService, PublicationGatewayASGI]:
    """Construct the complete fixed publication/API runtime from protected files."""
    config = ProtectedPublicationConfig.from_environment(environment)
    values = config.values
    try:
        bootstrap_artifacts = _validate_deployment_files(values)
        policy_raw = _read_protected_file(Path(values["policy_path"]), max_bytes=64 * 1024)
        if hashlib.sha256(policy_raw).hexdigest() != values["policy_sha256"]:
            raise ValueError
        policy = PolicyManifest.from_json(policy_raw)
        baseline_raw = _read_protected_file(
            Path(values["baseline_history_receipt_path"]), max_bytes=64 * 1024
        )
        if hashlib.sha256(baseline_raw).hexdigest() != values["baseline_history_receipt_sha256"]:
            raise ValueError
        baseline = json.loads(baseline_raw, object_pairs_hook=_unique_pairs)
        if (
            not isinstance(baseline, dict)
            or set(baseline)
            != {
                "version",
                "repository_id",
                "baseline_commit",
                "policy_digest",
                "scanner_identity_sha256",
                "history_complete",
                "scanned_commit_count",
                "scanned_bytes",
            }
            or baseline["version"] != 1
            or baseline["repository_id"] != policy.repository_id
            or baseline["baseline_commit"] != values["baseline_commit"]
            or baseline["policy_digest"] != policy.digest
            or baseline["scanner_identity_sha256"] != values["publication_scanner_identity_sha256"]
            or baseline["history_complete"] is not True
            or type(baseline["scanned_commit_count"]) is not int
            or baseline["scanned_commit_count"] < 1
            or type(baseline["scanned_bytes"]) is not int
            or baseline["scanned_bytes"] < 0
        ):
            raise ValueError
        scanner = ScannerBinaryAdapter(
            values["scanner_executable_path"],
            values["scanner_configuration_path"],
            expected_executable_sha256=values["scanner_executable_sha256"],
            expected_configuration_sha256=values["scanner_configuration_sha256"],
        )
        if scanner.identity_sha256 != values["publication_scanner_identity_sha256"]:
            raise ValueError
        issuer = GitHubAppTokenIssuer(
            app_id=values["app_id"],
            installation_id=values["installation_id"],
            repository_id=policy.repository_id,
            protected_key_provider=lambda: _read_protected_file(
                Path(values["app_private_key_path"]), max_bytes=32 * 1024
            ),
            http_post=GitHubAppHttpPost(
                installation_id=values["installation_id"], transport=app_http_transport
            ),
            now=lambda: datetime.now(UTC),
        )
        owner, repository = values["repository"].split("/", 1)
        github = GitHubPublicationGateway(
            owner=owner,
            repository=repository,
            repository_id=policy.repository_id,
            owner_id=policy.owner_id,
            token_issuer=issuer,
            transport=github_transport,
            git_executable=values["git_executable_path"],
            temporary_directory=values["temporary_directory"],
        )
        store = PublicationGatewayStore(values["database_path"])
        publisher = PublicationService(
            policy=policy,
            scanner=scanner,
            scanner_identity_sha256=scanner.identity_sha256,
            baseline_history_scan_complete=True,
            record_provenance=store,
            publish_transport=github.publish,
            git_executable=values["git_executable_path"],
        )
        gateway = PublicationGatewayService(
            publisher=publisher,
            github=github,
            store=store,
            repository_path=values["source_repository"],
            policy=policy,
            policy_manifest_json=policy_raw,
            repository=values["repository"],
            repository_id=policy.repository_id,
            owner_id=policy.owner_id,
        )
        app = PublicationGatewayASGI(
            store=store,
            ci_bearer_token=config.ci_token,
            report_scanner=scanner,
            bootstrap_artifacts=bootstrap_artifacts,
        )
        return gateway, app
    except PublicationGatewayError:
        raise
    except Exception:
        raise PublicationGatewayError("invalid_configuration") from None


def main(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    github_transport: httpx.BaseTransport | None = None,
    app_http_transport: httpx.BaseTransport | None = None,
) -> int:
    """Run `checkconfig` or the configured private UDS ASGI service."""
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments == ["--help"]:
        print("Usage: python -m chatcore_release_control.publication_service {checkconfig|serve}")
        return 0
    if arguments not in (["checkconfig"], ["serve"]):
        raise SystemExit("invalid_input")
    try:
        config = ProtectedPublicationConfig.from_environment(environment)
        if arguments == ["checkconfig"]:
            _validate_deployment_files(config.values)
            return 0
        socket_path = Path(config.values["socket_path"])
        _prepare_socket_path(socket_path)
        gateway, app = build_publication_gateway(
            environment,
            github_transport=github_transport,
            app_http_transport=app_http_transport,
        )
        import uvicorn

        try:
            uvicorn.run(
                app,
                uds=str(socket_path),
                uds_permissions=0o600,
                access_log=False,
                server_header=False,
                log_level="warning",
            )
        finally:
            gateway.close()
        return 0
    except PublicationGatewayError as exc:
        raise SystemExit(exc.code) from None
    except Exception:
        raise SystemExit("runtime_unavailable") from None


def _validate_config(value: object) -> None:
    required = {
        "version",
        "repository",
        "app_id",
        "installation_id",
        "source_repository",
        "policy_path",
        "policy_sha256",
        "baseline_commit",
        "baseline_history_receipt_path",
        "baseline_history_receipt_sha256",
        "app_private_key_path",
        "scanner_executable_path",
        "scanner_executable_sha256",
        "scanner_configuration_path",
        "scanner_configuration_sha256",
        "publication_scanner_identity_sha256",
        "git_executable_path",
        "database_path",
        "ci_token_path",
        "socket_path",
        "temporary_directory",
        "bootstrap_artifact_directory",
        "bootstrap_source_path",
        "bootstrap_source_sha256",
        "bootstrap_manifest_path",
        "bootstrap_manifest_sha256",
        "bootstrap_archive_path",
        "bootstrap_archive_sha256",
        "bootstrap_archive_size",
    }
    if not isinstance(value, dict) or set(value) != required or value["version"] != 1:
        raise PublicationGatewayError("invalid_configuration")
    if (
        not isinstance(value["repository"], str)
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", value["repository"]) is None
        or any(
            type(value[key]) is not int or value[key] <= 0 for key in ("app_id", "installation_id")
        )
        or not _OID.fullmatch(value["baseline_commit"])
        or type(value["bootstrap_archive_size"]) is not int
        or not 1 <= value["bootstrap_archive_size"] <= _MAX_BOOTSTRAP_ARCHIVE_BYTES
    ):
        raise PublicationGatewayError("invalid_configuration")
    for key in required - {"version", "repository", "app_id", "installation_id", "baseline_commit"}:
        if key.endswith("sha256") and (
            not isinstance(value[key], str) or not _SHA256.fullmatch(value[key])
        ):
            raise PublicationGatewayError("invalid_configuration")
        if key.endswith("path") or key in {
            "source_repository",
            "temporary_directory",
            "database_path",
            "bootstrap_artifact_directory",
        }:
            if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
                raise PublicationGatewayError("invalid_configuration")


def _validate_synthetic_examples(value: object) -> list[object]:
    if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= 8:
        raise PublicationGatewayError("invalid_request")
    try:
        result = list(value)
        if len(_canonical(result)) > 16 * 1024:
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PublicationGatewayError("invalid_request") from None


def _validate_request_artifacts(value: object) -> tuple[ScanSurface, ...]:
    reserved = {"synthetic-examples", "development-report"}
    if not isinstance(value, tuple) or len(value) > 32:
        raise PublicationGatewayError("invalid_request")
    names: set[str] = set()
    total = 0
    try:
        for item in value:
            if (
                not isinstance(item, ScanSurface)
                or not isinstance(item.name, str)
                or not item.name
                or len(item.name.encode("utf-8", errors="strict")) > 256
                or item.name in names
                or item.name in reserved
                or not isinstance(item.content, bytes)
                or not 1 <= len(item.content) <= MAX_SCAN_ITEM_BYTES
            ):
                raise ValueError
            names.add(item.name)
            total += len(item.content)
            if total > MAX_TOTAL_SCAN_BYTES:
                raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise PublicationGatewayError("invalid_request") from None
    return value


def _validate_deployment_files(values: Mapping[str, Any]) -> BootstrapArtifactSet:
    try:
        policy_raw = _read_protected_file(Path(values["policy_path"]), max_bytes=64 * 1024)
        if hashlib.sha256(policy_raw).hexdigest() != values["policy_sha256"]:
            raise ValueError
        policy = PolicyManifest.from_json(policy_raw)
        baseline_raw = _read_protected_file(
            Path(values["baseline_history_receipt_path"]), max_bytes=64 * 1024
        )
        if (
            hashlib.sha256(baseline_raw).hexdigest() != values["baseline_history_receipt_sha256"]
            or _canonical(json.loads(baseline_raw, object_pairs_hook=_unique_pairs)) != baseline_raw
        ):
            raise ValueError
        baseline = json.loads(baseline_raw, object_pairs_hook=_unique_pairs)
        scanner_raw = _read_protected_file(
            Path(values["scanner_executable_path"]), max_bytes=64 * 1024 * 1024
        )
        scanner_config = _read_protected_file(
            Path(values["scanner_configuration_path"]), max_bytes=1024 * 1024
        )
        if (
            set(baseline)
            != {
                "version",
                "repository_id",
                "baseline_commit",
                "policy_digest",
                "scanner_identity_sha256",
                "history_complete",
                "scanned_commit_count",
                "scanned_bytes",
            }
            or baseline.get("version") != 1
            or hashlib.sha256(scanner_raw).hexdigest() != values["scanner_executable_sha256"]
            or hashlib.sha256(scanner_config).hexdigest() != values["scanner_configuration_sha256"]
            or hashlib.sha256(
                bytes.fromhex(values["scanner_executable_sha256"])
                + bytes.fromhex(values["scanner_configuration_sha256"])
            ).hexdigest()
            != values["publication_scanner_identity_sha256"]
            or baseline.get("repository_id") != policy.repository_id
            or baseline.get("baseline_commit") != values["baseline_commit"]
            or baseline.get("policy_digest") != policy.digest
            or baseline.get("scanner_identity_sha256")
            != values["publication_scanner_identity_sha256"]
            or baseline.get("history_complete") is not True
            or type(baseline.get("scanned_commit_count")) is not int
            or not 1 <= baseline["scanned_commit_count"] <= 2048
            or type(baseline.get("scanned_bytes")) is not int
            or not 0 <= baseline["scanned_bytes"] <= MAX_TOTAL_SCAN_BYTES
        ):
            raise ValueError
        _read_protected_file(Path(values["app_private_key_path"]), max_bytes=32 * 1024)
        _read_protected_file(Path(values["ci_token_path"]), max_bytes=4096)
        source = Path(values["source_repository"]).resolve(strict=True)
        if not source.is_dir():
            raise ValueError
        source_objects = _object_directory(source)
        if not source_objects.is_dir():
            raise ValueError
        git_path = Path(values["git_executable_path"])
        git_stat = git_path.stat()
        if (
            not git_path.is_absolute()
            or not stat.S_ISREG(git_stat.st_mode)
            or not os.access(git_path, os.X_OK)
        ):
            raise ValueError
        for directory in (
            Path(values["temporary_directory"]),
            Path(values["database_path"]).parent,
            Path(values["socket_path"]).parent,
            Path(values["bootstrap_artifact_directory"]),
        ):
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ValueError
        if len(str(values["socket_path"]).encode()) > 100:
            raise ValueError
        return _load_bootstrap_artifacts(values, policy)
    except Exception:
        raise PublicationGatewayError("invalid_configuration") from None


def _load_bootstrap_artifacts(
    values: Mapping[str, Any], policy: PolicyManifest | None = None
) -> BootstrapArtifactSet:
    try:
        directory = Path(values["bootstrap_artifact_directory"]).resolve(strict=True)
        paths = {
            "source": Path(values["bootstrap_source_path"]),
            "manifest": Path(values["bootstrap_manifest_path"]),
            "archive": Path(values["bootstrap_archive_path"]),
        }
        expected_paths = {
            "source": directory / "hosted-bootstrap.py",
            "manifest": directory / "manifest.json",
            "archive": directory / "runtime.tar",
        }
        if any(
            paths[key].resolve(strict=True) != expected for key, expected in expected_paths.items()
        ):
            raise ValueError
        source = _read_protected_file(paths["source"], max_bytes=_MAX_BOOTSTRAP_SOURCE_BYTES)
        source_sha256 = hashlib.sha256(source).hexdigest()
        installed_source = _read_protected_file(
            Path(__file__).with_name("hosted_bootstrap.py"),
            max_bytes=_MAX_BOOTSTRAP_SOURCE_BYTES,
        )
        if (
            source_sha256 != values["bootstrap_source_sha256"]
            or hashlib.sha256(installed_source).hexdigest() != source_sha256
        ):
            raise ValueError
        manifest = _read_protected_file(paths["manifest"], max_bytes=_MAX_BOOTSTRAP_MANIFEST_BYTES)
        manifest_sha256 = hashlib.sha256(manifest).hexdigest()
        if manifest_sha256 != values["bootstrap_manifest_sha256"]:
            raise ValueError
        value = json.loads(manifest, object_pairs_hook=_unique_pairs)
        if not isinstance(value, dict) or _canonical(value) != manifest:
            raise ValueError
        if policy is None:
            policy_raw = _read_protected_file(Path(values["policy_path"]), max_bytes=64 * 1024)
            policy = PolicyManifest.from_json(policy_raw)
        _validate_manifest(
            value, repository=values["repository"], repository_id=policy.repository_id
        )
        archive = _read_protected_file(paths["archive"], max_bytes=_MAX_BOOTSTRAP_ARCHIVE_BYTES)
        archive_sha256 = hashlib.sha256(archive).hexdigest()
        if (
            manifest_sha256 != values["bootstrap_manifest_sha256"]
            or source_sha256 != value["bootstrap_source_sha256"]
            or archive_sha256 != values["bootstrap_archive_sha256"]
            or archive_sha256 != value["archive_sha256"]
            or len(archive) != values["bootstrap_archive_size"]
            or len(archive) != value["archive_size"]
        ):
            raise ValueError
        with tempfile.TemporaryDirectory(prefix="publication-bootstrap-verify-") as scratch:
            _extract_exact_archive(archive, value["files"], Path(scratch))
        return BootstrapArtifactSet(
            source_path=paths["source"],
            source_sha256=source_sha256,
            manifest_path=paths["manifest"],
            manifest_sha256=manifest_sha256,
            archive_path=paths["archive"],
            archive_sha256=archive_sha256,
            archive_size=len(archive),
        )
    except PublicationGatewayError:
        raise
    except (HostedBootstrapError, OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise PublicationGatewayError("invalid_configuration") from None


def _inspect_bundle(request: PublicationRequest, policy: PolicyManifest):
    if not isinstance(request.bundle, bytes) or not 1 <= len(request.bundle) <= MAX_BUNDLE_BYTES:
        raise PublicationGatewayError("invalid_request")
    git = shutil.which("git")
    if not git:
        raise PublicationGatewayError("invalid_configuration")
    try:
        with tempfile.TemporaryDirectory(prefix="publication-context-") as temp:
            root = Path(temp)
            bare = root / "objects.git"
            env = {
                "PATH": str(Path(git).parent),
                "HOME": str(root),
                "TMPDIR": str(root),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C",
            }
            _run_git(git, root, env, "init", "--bare", "--quiet", str(bare))
            bundle_file = root / "candidate.bundle"
            bundle_file.write_bytes(request.bundle)
            local_env = dict(env)
            local_env["GIT_DIR"] = str(bare)
            _run_git(git, root, local_env, "bundle", "verify", str(bundle_file))
            _run_git(git, root, local_env, "bundle", "unbundle", str(bundle_file))
            evidence = collect_release_bundle(
                bare,
                request.deployed_commit,
                request.base_commit,
                request.head_commit,
                policy,
            )
            return evidence
    except (GitEvidenceError, OSError, ValueError, subprocess.SubprocessError):
        raise PublicationGatewayError("invalid_request") from None


def _run_git(git: str, root: Path, env: Mapping[str, str], *args: str) -> bytes:
    command = [
        git,
        "--no-pager",
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.allow=never",
        *args,
    ]
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(  # noqa: S603 - fixed Git plumbing, never a shell.
                command,
                cwd=root,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                preexec_fn=_limit_context_git_output,
            )
            try:
                process.wait(timeout=COMMAND_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise ValueError from None
            stdout.seek(0, os.SEEK_END)
            stderr.seek(0, os.SEEK_END)
            if stdout.tell() > 1024 * 1024 or stderr.tell() > 64 * 1024:
                raise ValueError
            stdout.seek(0)
            data = stdout.read(1024 * 1024 + 1)
            if process.returncode != 0:
                raise ValueError
            return data
    except (OSError, subprocess.SubprocessError, ValueError):
        raise PublicationGatewayError("invalid_request") from None


def _limit_context_git_output() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))


async def _read_asgi_body(receive) -> bytes:
    body = bytearray()
    while True:
        message = await asyncio.wait_for(receive(), timeout=5.0)
        if message.get("type") == "http.disconnect":
            raise ValueError
        if message.get("type") != "http.request":
            raise ValueError
        chunk = message.get("body", b"")
        if not isinstance(chunk, bytes) or len(body) + len(chunk) > _MAX_HTTP_BODY:
            raise ValueError
        body.extend(chunk)
        if not message.get("more_body", False):
            return bytes(body)


async def _asgi_response(send, status: int, value: Mapping[str, object]) -> None:
    data = _canonical(value)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(data)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})


async def _asgi_bytes_response(send, status: int, data: bytes, content_type: str) -> None:
    if (
        not isinstance(data, bytes)
        or len(data) > _MAX_BOOTSTRAP_ARCHIVE_BYTES
        or content_type not in {"application/json", "application/octet-stream"}
    ):
        raise PublicationGatewayError("state_unavailable")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type.encode("ascii")),
                (b"content-length", str(len(data)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})


def _read_protected_file(path: Path, *, max_bytes: int) -> bytes:
    if not path.is_absolute():
        raise ValueError
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_size > max_bytes
        ):
            raise ValueError
        data = bytearray()
        while chunk := os.read(fd, min(65536, max_bytes + 1 - len(data))):
            data.extend(chunk)
            if len(data) > max_bytes:
                raise ValueError
        return bytes(data)
    finally:
        os.close(fd)


def _prepare_socket_path(path: Path) -> None:
    try:
        if not path.is_absolute():
            raise ValueError
        parent = path.parent
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError
        try:
            existing = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid not in {0, os.geteuid()}:
            raise ValueError
        path.unlink()
    except (OSError, ValueError):
        raise PublicationGatewayError("invalid_configuration") from None


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PublicationGatewayError("invalid_request") from None


def _logical_surface(value: object, *, allow_empty: bool) -> bytes:
    if not isinstance(value, str):
        raise PublicationGatewayError("invalid_request")
    try:
        raw = value.encode("utf-8", errors="strict")
        if len(raw) > MAX_SCAN_ITEM_BYTES or (not allow_empty and not raw.strip()) or b"\0" in raw:
            raise ValueError
        return raw
    except (UnicodeError, ValueError):
        raise PublicationGatewayError("invalid_request") from None


def _submission_data(
    request: PublicationRequest,
    synthetic_examples: object,
    development_report: bytes | None,
) -> dict[str, object]:
    if (
        not isinstance(request, PublicationRequest)
        or not isinstance(request.request_id, str)
        or not _REQUEST_ID.fullmatch(request.request_id)
        or any(
            not isinstance(oid, str) or not _OID.fullmatch(oid)
            for oid in (request.deployed_commit, request.base_commit, request.head_commit)
        )
        or len({len(request.deployed_commit), len(request.base_commit), len(request.head_commit)})
        != 1
        or request.head_commit in {request.deployed_commit, request.base_commit}
        or not isinstance(request.bundle, bytes)
        or not 1 <= len(request.bundle) <= MAX_BUNDLE_BYTES
        or not isinstance(request.specification, bytes)
        or not isinstance(request.pull_request_title, bytes)
        or not isinstance(request.pull_request_body, bytes)
        or any(
            len(value) > MAX_SCAN_ITEM_BYTES
            or (not allow_empty and not value.strip())
            or b"\0" in value
            for value, allow_empty in (
                (request.specification, False),
                (request.pull_request_title, False),
                (request.pull_request_body, True),
            )
        )
    ):
        raise PublicationGatewayError("invalid_request")
    try:
        for value in (request.specification, request.pull_request_title, request.pull_request_body):
            value.decode("utf-8", errors="strict")
    except UnicodeError:
        raise PublicationGatewayError("invalid_request") from None
    examples = _validate_synthetic_examples(synthetic_examples)
    artifacts = _validate_request_artifacts(request.artifacts)
    if development_report is not None and (
        not isinstance(development_report, bytes)
        or not 1 <= len(development_report) <= MAX_SCAN_ITEM_BYTES
    ):
        raise PublicationGatewayError("invalid_request")
    request_value = {
        "request_id": request.request_id,
        "deployed_commit": request.deployed_commit,
        "base_commit": request.base_commit,
        "head_commit": request.head_commit,
        "bundle_base64": base64.b64encode(request.bundle).decode("ascii"),
        "specification_base64": base64.b64encode(request.specification).decode("ascii"),
        "pull_request_title_base64": base64.b64encode(request.pull_request_title).decode("ascii"),
        "pull_request_body_base64": base64.b64encode(request.pull_request_body).decode("ascii"),
        "artifacts": [
            {
                "name": artifact.name,
                "content_base64": base64.b64encode(artifact.content).decode("ascii"),
            }
            for artifact in artifacts
        ],
    }
    return {
        "version": 1,
        "request": request_value,
        "synthetic_examples": examples,
        "development_report_base64": (
            None if development_report is None else base64.b64encode(development_report).decode()
        ),
    }


def _submission_from_data(value: object, request_id: str) -> PublicationSubmission:
    fields = {"version", "request", "synthetic_examples", "development_report_base64"}
    request_fields = {
        "request_id",
        "deployed_commit",
        "base_commit",
        "head_commit",
        "bundle_base64",
        "specification_base64",
        "pull_request_title_base64",
        "pull_request_body_base64",
        "artifacts",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["request"], dict)
        or set(value["request"]) != request_fields
        or value["request"]["request_id"] != request_id
        or not isinstance(value["request"]["artifacts"], list)
    ):
        raise ValueError

    def decode(name: str) -> bytes:
        encoded = value["request"][name]
        if not isinstance(encoded, str):
            raise ValueError
        decoded = base64.b64decode(encoded, validate=True)
        if base64.b64encode(decoded).decode("ascii") != encoded:
            raise ValueError
        return decoded

    request_data = value["request"]
    artifacts = []
    for item in request_data["artifacts"]:
        if not isinstance(item, dict) or set(item) != {"name", "content_base64"}:
            raise ValueError
        encoded = item["content_base64"]
        if not isinstance(encoded, str):
            raise ValueError
        content = base64.b64decode(encoded, validate=True)
        if base64.b64encode(content).decode("ascii") != encoded:
            raise ValueError
        artifacts.append(ScanSurface(item["name"], content))
    request = PublicationRequest(
        request_id=request_data["request_id"],
        deployed_commit=request_data["deployed_commit"],
        base_commit=request_data["base_commit"],
        head_commit=request_data["head_commit"],
        bundle=decode("bundle_base64"),
        specification=decode("specification_base64"),
        pull_request_title=decode("pull_request_title_base64"),
        pull_request_body=decode("pull_request_body_base64"),
        artifacts=tuple(artifacts),
    )
    examples = _validate_synthetic_examples(value["synthetic_examples"])
    report_value = value["development_report_base64"]
    if report_value is None:
        report = None
    elif isinstance(report_value, str):
        report = base64.b64decode(report_value, validate=True)
        if base64.b64encode(report).decode("ascii") != report_value:
            raise ValueError
    else:
        raise ValueError
    if _canonical(_submission_data(request, examples, report)) != _canonical(value):
        raise ValueError
    return PublicationSubmission(request, examples, report)


def _file_size_if_regular(path: Path) -> int:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}:
        raise ValueError
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o600, follow_symlinks=False)
    return info.st_size


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _validate_persisted_context(context: object) -> None:
    required = {
        "version",
        "request_id",
        "repository",
        "repository_id",
        "owner_id",
        "pull_request_number",
        "base_branch",
        "deployed_commit",
        "base_commit",
        "head_commit",
        "head_tree",
        "policy_digest",
        "policy_manifest",
        "provenance",
        "published_ref",
        "specification",
        "synthetic_examples",
        "pull_request_title",
        "pull_request_body",
        "artifacts",
        "context_sha256",
    }
    if not isinstance(context, Mapping) or set(context) != required:
        raise ValueError
    request_id = context["request_id"]
    if (
        type(context["version"]) is not int
        or context["version"] != 1
        or not isinstance(request_id, str)
        or not _REQUEST_ID.fullmatch(request_id)
        or not isinstance(context["repository"], str)
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", context["repository"])
        is None
        or any(
            type(context[name]) is not int or context[name] <= 0
            for name in ("repository_id", "owner_id", "pull_request_number")
        )
        or context["base_branch"] != "main"
        or context["published_ref"] != f"refs/heads/clawdot/{request_id}"
        or any(
            not isinstance(context[name], str) or not _OID.fullmatch(context[name])
            for name in ("deployed_commit", "base_commit", "head_commit", "head_tree")
        )
        or len({len(context[name]) for name in ("deployed_commit", "base_commit", "head_commit")})
        != 1
        or not isinstance(context["policy_digest"], str)
        or not _SHA256.fullmatch(context["policy_digest"])
        or not isinstance(context["specification"], str)
        or not context["specification"].strip()
        or len(context["specification"].encode("utf-8")) > 8192
        or not isinstance(context["pull_request_title"], str)
        or len(context["pull_request_title"].encode("utf-8")) > 16 * 1024
        or not isinstance(context["pull_request_body"], str)
        or len(context["pull_request_body"].encode("utf-8")) > 64 * 1024
        or not isinstance(context["synthetic_examples"], list)
        or not 1 <= len(context["synthetic_examples"]) <= 8
        or len(_canonical(context["synthetic_examples"])) > 16 * 1024
        or not isinstance(context["artifacts"], list)
        or len(context["artifacts"]) > 64
        or not isinstance(context["context_sha256"], str)
        or context["context_sha256"] != _context_digest(context)
    ):
        raise ValueError
    from .policy import PolicyError

    try:
        policy = PolicyManifest.from_json(_canonical(context["policy_manifest"]))
    except (PolicyError, TypeError, ValueError):
        raise ValueError from None
    if (
        policy.digest != context["policy_digest"]
        or policy.repository_id != context["repository_id"]
        or policy.owner_id != context["owner_id"]
    ):
        raise ValueError
    provenance = context["provenance"]
    provenance_fields = {
        "request_id",
        "deployed_commit",
        "base_commit",
        "head_commit",
        "policy_digest",
        "scanner_identity_sha256",
        "baseline_history_scan_complete",
        "bundle_sha256",
        "surface_manifest_sha256",
        "scanned_surface_count",
        "scanned_bytes",
    }
    if (
        not isinstance(provenance, Mapping)
        or set(provenance) != provenance_fields
        or any(
            provenance.get(key) != context[key]
            for key in (
                "request_id",
                "deployed_commit",
                "base_commit",
                "head_commit",
                "policy_digest",
            )
        )
        or provenance.get("baseline_history_scan_complete") is not True
        or any(
            not isinstance(provenance.get(key), str) or not _SHA256.fullmatch(provenance[key])
            for key in ("scanner_identity_sha256", "bundle_sha256", "surface_manifest_sha256")
        )
        or type(provenance.get("scanned_surface_count")) is not int
        or not 1 <= provenance["scanned_surface_count"] <= 100_000
        or type(provenance.get("scanned_bytes")) is not int
        or not 0 <= provenance["scanned_bytes"] <= MAX_TOTAL_SCAN_BYTES
    ):
        raise ValueError
    artifact_total = 0
    for artifact in context["artifacts"]:
        if (
            not isinstance(artifact, dict)
            or set(artifact) != {"name", "sha256", "content_base64"}
            or not isinstance(artifact["name"], str)
            or re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", artifact["name"]) is None
            or not isinstance(artifact["sha256"], str)
            or not _SHA256.fullmatch(artifact["sha256"])
            or not isinstance(artifact["content_base64"], str)
            or len(artifact["content_base64"]) > 350_000
        ):
            raise ValueError
        data = base64.b64decode(artifact["content_base64"], validate=True)
        artifact_total += len(data)
        if (
            not data
            or hashlib.sha256(data).hexdigest() != artifact["sha256"]
            or artifact_total > 512 * 1024
        ):
            raise ValueError


def _context_digest(context: Mapping[str, object]) -> str:
    value = dict(context)
    value.pop("context_sha256", None)
    return hashlib.sha256(_canonical(value)).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
