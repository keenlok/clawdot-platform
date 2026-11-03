from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from chatcore_release_control.host_journal import HostJournal, OperationConflict
from chatcore_release_control.host_service import (
    CommandResult,
    FixedBuildConfig,
    FixedComposeConfig,
    FixedComposeRuntime,
    FixedOfflineImageBuilder,
    GitExactCommitSource,
    GitSourceConfig,
    HostDeploymentContext,
    HostService,
    HostServiceError,
    P01HostEvidence,
)
from chatcore_release_control.policy import (
    DeploymentIdentity,
    JournalState,
    PolicyManifest,
    TrustedCheck,
)

BASE = "1" * 40
MERGE = "2" * 40
POLICY = "3" * 64
CONFIG = "4" * 64


def identity(sha: str, image: str, sequence: int = 7) -> DeploymentIdentity:
    return DeploymentIdentity(
        git_sha=sha,
        image_digest=f"sha256:{image * 64}",
        config_generation=11,
        config_digest=CONFIG,
        policy_digest=POLICY,
        target_sequence=sequence,
    )


@dataclass
class Broker:
    context: HostDeploymentContext
    allowed: bool = True
    events: list[str] | None = None

    def __post_init__(self) -> None:
        self.events = [] if self.events is None else self.events

    def get_context(self, approval_id: str) -> HostDeploymentContext:
        self.events.append("context")
        return self.context

    def prepare(
        self,
        approval_id: str,
        fence: int,
        operation_id: str,
        baseline: DeploymentIdentity,
        payload: DeploymentIdentity,
    ) -> None:
        self.events.append("prepare")
        assert (baseline, payload) == (self.context.deployed_identity, self.context.payload)

    def admit(self, approval_id: str, fence: int, operation_id: str) -> dict[str, object]:
        self.events.append("admit")
        return {
            "approval_id": self.context.approval_id,
            "target": self.context.target,
            "operation_id": operation_id,
            "dispatch_fence": fence,
            "admitted": self.allowed,
            "may_start": self.allowed,
            "start_deadline": 120,
            "recovery_deadline": 600,
            "baseline_identity": self.context.deployed_identity,
            "payload_identity": self.context.payload,
        }

    def started(self, approval_id: str, fence: int, operation_id: str) -> None:
        self.events.append("started")

    def restore_started(self, approval_id: str, fence: int, operation_id: str) -> None:
        self.events.append("restore_started")

    def receipt(self, approval_id: str, fence: int, operation_id: str, receipt: object) -> None:
        self.events.append("receipt")


class Evidence:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def verify(self, repository: Path, context: HostDeploymentContext) -> bool:
        return self.allowed and repository.is_dir()


class Source:
    def __init__(self, repository: Path) -> None:
        self.repository = repository

    @contextmanager
    def fetch(self, commit: str, baseline_commit: str):
        assert re.fullmatch(r"[0-9a-f]{40}", commit)
        assert re.fullmatch(r"[0-9a-f]{40}", baseline_commit)
        yield self.repository


class Builder:
    def __init__(self, events: list[str], digest: str = "5") -> None:
        self.events = events
        self.digest = digest

    def build(self, repository: Path, context: HostDeploymentContext) -> str:
        self.events.append("build")
        return f"sha256:{self.digest * 64}"


class Runtime:
    def __init__(self, events: list[str], actual: DeploymentIdentity) -> None:
        self.events = events
        self.actual = actual
        self.health = True
        self.smoke = True
        self.baseline_healthy = True

    def observe(self) -> DeploymentIdentity:
        self.events.append("observe")
        return self.actual

    def switch(self, identity: DeploymentIdentity) -> None:
        self.events.append("switch")
        self.actual = identity

    def restore(self, identity: DeploymentIdentity) -> None:
        self.events.append("restore")
        self.actual = identity

    def healthy(self, identity: DeploymentIdentity) -> bool:
        self.events.append("health")
        return (
            self.baseline_healthy if identity.git_sha == BASE else self.health
        ) and self.actual == identity

    def smoke_test(self, identity: DeploymentIdentity) -> bool:
        self.events.append("smoke")
        return self.smoke and self.actual == identity


def service(
    tmp: Path, *, allowed: bool = True, evidence: bool = True
) -> tuple[HostService, Broker, Runtime]:
    tmp.mkdir(parents=True, exist_ok=True)
    repository = tmp / "fixed-repository"
    repository.mkdir(exist_ok=True)
    baseline = identity(BASE, "a", sequence=6)
    payload = identity(MERGE, "5")
    context = HostDeploymentContext(
        approval_id="approval-1",
        target="sb-bot",
        sequence=7,
        dispatch_fence=9,
        status="merged_verified",
        merge_commit=MERGE,
        policy_digest=POLICY,
        deployed_identity=baseline,
        payload=payload,
    )
    broker = Broker(context, allowed=allowed)
    runtime = Runtime(broker.events, baseline)
    instance = HostService(
        source=Source(repository),
        target="sb-bot",
        policy_digest=POLICY,
        journal=HostJournal(tmp / "journal.json"),
        broker=broker,
        evidence=Evidence(evidence),
        builder=Builder(broker.events),
        runtime=runtime,
        now=lambda: 100,
    )
    return instance, broker, runtime


def test_deploy_journals_before_effect_and_requires_health_and_smoke(tmp_path: Path) -> None:
    instance, broker, runtime = service(tmp_path)

    result = instance.deploy("approval-1", operation_id="operation-1")

    assert result.phase is JournalState.HEALTHY
    assert (
        broker.events.index("admit")
        < broker.events.index("started")
        < broker.events.index("switch")
    )
    assert (
        broker.events.index("switch") < broker.events.index("health") < broker.events.index("smoke")
    )
    assert broker.events[-1] == "receipt"
    assert runtime.actual == broker.context.payload


def test_terminal_retry_is_read_only_and_next_operation_can_advance(tmp_path: Path) -> None:
    instance, broker, _runtime = service(tmp_path)
    first = instance.deploy("approval-1", operation_id="operation-1")
    before = list(broker.events)

    retried = instance.deploy("approval-1", operation_id="operation-1")
    second_baseline = broker.context.payload
    second_merge = "3" * 40
    second_payload = identity(second_merge, "6", sequence=8)
    broker.context = HostDeploymentContext(
        approval_id="approval-1",
        target="sb-bot",
        sequence=8,
        dispatch_fence=10,
        status="merged_verified",
        merge_commit=second_merge,
        policy_digest=POLICY,
        deployed_identity=second_baseline,
        payload=second_payload,
    )
    instance.builder.digest = "6"
    second = instance.deploy("approval-1", operation_id="operation-2")

    assert retried == first
    assert broker.events.count("build") == 2
    assert broker.events.count("switch") == 2
    assert broker.events.count("receipt") == 3
    assert before.count("switch") == 1
    assert second.operation_id == "operation-2"


def test_terminal_retry_uses_saved_baseline_after_out_of_band_maintenance(tmp_path: Path) -> None:
    operation = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    instance, broker, runtime = service(tmp_path)
    terminal = instance.deploy("approval-1", operation_id=operation)
    baseline = terminal.baseline
    payload = terminal.payload
    broker.context = HostDeploymentContext(
        approval_id="approval-1",
        target="sb-bot",
        sequence=7,
        dispatch_fence=9,
        status="healthy",
        merge_commit=payload.git_sha,
        policy_digest=POLICY,
        deployed_identity=baseline,
        payload=payload,
        host_operation_id=operation,
        host_operation_payload_digest=terminal.payload_digest,
        host_operation_state="healthy",
        host_operation_start_deadline=terminal.start_deadline,
        host_operation_recovery_deadline=terminal.recovery_deadline,
        host_operation_receipt_id=terminal.receipt,
    )
    runtime.actual = DeploymentIdentity(
        git_sha=payload.git_sha,
        image_digest=payload.image_digest,
        config_generation=12,
        config_digest="5" * 64,
        policy_digest=payload.policy_digest,
        target_sequence=payload.target_sequence,
    )
    before = list(broker.events)

    retried = instance.deploy("approval-1", operation_id=operation)

    assert retried == terminal
    assert broker.events[len(before) :] == ["context", "receipt"]


def test_broker_denial_and_evidence_failure_have_no_host_effect(tmp_path: Path) -> None:
    instance, broker, runtime = service(tmp_path, allowed=False)
    with pytest.raises(HostServiceError):
        instance.deploy("approval-1", operation_id="operation-1")
    assert "switch" not in broker.events and "restore" not in broker.events

    other, _, other_runtime = service(tmp_path / "other", evidence=False)
    with pytest.raises(HostServiceError):
        other.deploy("approval-1", operation_id="operation-2")
    assert "switch" not in other_runtime.events and "build" not in other_runtime.events


def test_fresh_deployment_requires_a_newer_monotonic_target_sequence(tmp_path: Path) -> None:
    instance, broker, runtime = service(tmp_path)
    same_sequence_baseline = replace(broker.context.deployed_identity, target_sequence=7)
    broker.context = replace(
        broker.context,
        deployed_identity=same_sequence_baseline,
        payload=replace(broker.context.payload, target_sequence=7),
    )
    runtime.actual = same_sequence_baseline

    with pytest.raises(HostServiceError):
        instance.deploy("approval-1", operation_id="operation-1")

    assert "build" not in broker.events and "prepare" not in broker.events


def test_switch_admission_accepts_broker_utc_iso_deadlines(tmp_path: Path) -> None:
    instance, broker, _runtime = service(tmp_path)
    original_admit = broker.admit

    def iso_admit(*args):
        value = original_admit(*args)
        value["start_deadline"] = "1970-01-01T00:02:00.900000Z"
        value["recovery_deadline"] = "1970-01-01T00:10:00.900000Z"
        return value

    broker.admit = iso_admit  # type: ignore[method-assign]

    result = instance.deploy("approval-1", operation_id="operation-1")

    assert result.phase is JournalState.HEALTHY


def test_switch_admission_rejects_mismatched_fence_before_host_effect(tmp_path: Path) -> None:
    instance, broker, runtime = service(tmp_path)
    original_admit = broker.admit

    def mismatched_admit(*args):
        value = original_admit(*args)
        value["dispatch_fence"] += 1
        return value

    broker.admit = mismatched_admit  # type: ignore[method-assign]
    with pytest.raises(HostServiceError):
        instance.deploy("approval-1", operation_id="operation-1")
    assert "switch" not in runtime.events


def test_unknown_actual_state_fails_before_admission_or_effect(tmp_path: Path) -> None:
    instance, broker, runtime = service(tmp_path)
    runtime.observe = lambda: None  # type: ignore[method-assign]

    with pytest.raises(HostServiceError):
        instance.deploy("approval-1", operation_id="operation-1")

    assert "admit" not in broker.events and "switch" not in broker.events


def test_broker_current_state_callback_does_not_reenter_host_effect_lock(tmp_path: Path) -> None:
    from contextlib import contextmanager

    instance, broker, runtime = service(tmp_path)
    baseline = runtime.actual
    lock_active = False
    original_lock = instance._locked
    snapshots = []

    @contextmanager
    def checked_lock():
        nonlocal lock_active
        if lock_active:
            raise AssertionError("read-only broker callback re-entered target effect lock")
        with original_lock():
            lock_active = True
            try:
                yield
            finally:
                lock_active = False

    instance._locked = checked_lock  # type: ignore[method-assign]
    original_admit = broker.admit

    def admit(*args):
        snapshots.append(instance.current_state())
        return original_admit(*args)

    broker.admit = admit  # type: ignore[method-assign]

    result = instance.deploy("approval-1", operation_id="operation-1")

    assert result.phase is JournalState.HEALTHY
    assert snapshots == [
        {
            "deployed_identity": baseline.__dict__,
            "healthy": True,
            "policy_digest": POLICY,
            "target_sequence": baseline.target_sequence,
        }
    ]
    payload_snapshot = instance.current_state()
    assert payload_snapshot["deployed_identity"] == runtime.actual.__dict__
    assert runtime.actual == broker.context.payload
    assert payload_snapshot["healthy"] is True
    assert broker.events.count("switch") == 1


def test_failed_new_health_restores_only_same_baseline_and_checks_both(tmp_path: Path) -> None:
    instance, broker, runtime = service(tmp_path)
    runtime.health = False

    result = instance.deploy("approval-1", operation_id="operation-1")

    assert result.phase is JournalState.RESTORED_HEALTHY
    assert runtime.actual == broker.context.deployed_identity
    assert "restore" in broker.events
    assert broker.events[-3:] == ["health", "smoke", "receipt"]


def test_out_of_band_config_generation_blocks_restore(tmp_path: Path) -> None:
    instance, broker, runtime = service(tmp_path)
    runtime.health = False
    original_switch = runtime.switch

    def switch_and_race(identity_value: DeploymentIdentity) -> None:
        original_switch(identity_value)
        runtime.actual = DeploymentIdentity(
            git_sha=identity_value.git_sha,
            image_digest=identity_value.image_digest,
            config_generation=12,
            config_digest="6" * 64,
            policy_digest=identity_value.policy_digest,
            target_sequence=identity_value.target_sequence,
        )

    runtime.switch = switch_and_race  # type: ignore[method-assign]
    with pytest.raises(HostServiceError):
        instance.deploy("approval-1", operation_id="operation-1")

    assert "restore" not in broker.events
    assert instance.journal.get_record("operation-1").phase is JournalState.SWITCHING


def test_restore_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    instance, _, runtime = service(tmp_path)
    runtime.health = False
    runtime.smoke = False
    runtime.baseline_healthy = False

    with pytest.raises(HostServiceError):
        instance.deploy("approval-1", operation_id="operation-1")

    assert instance.journal.get_record("operation-1").phase is JournalState.RECOVERY_FAILED


def test_reopen_reconciles_actual_payload_instead_of_repeating_switch(tmp_path: Path) -> None:
    instance, broker, runtime = service(tmp_path)
    runtime.switch = lambda identity_value: setattr(runtime, "actual", identity_value)  # type: ignore[method-assign]
    # Simulate a crash after the durable switching transition and runtime effect.
    record = instance.journal.prepare(
        operation_id="operation-1",
        baseline=broker.context.deployed_identity,
        payload=broker.context.payload,
        start_deadline=120,
        recovery_deadline=600,
        now=100,
    )
    instance.journal.begin_switch(record.operation_id, now=100)
    runtime.actual = broker.context.payload
    reopened = HostService(
        source=Source(tmp_path / "fixed-repository"),
        target="sb-bot",
        policy_digest=POLICY,
        journal=HostJournal(tmp_path / "journal.json"),
        broker=broker,
        evidence=Evidence(),
        builder=Builder(broker.events),
        runtime=runtime,
        now=lambda: 101,
    )

    assert reopened.reconcile("approval-1", "operation-1").phase is JournalState.HEALTHY
    assert "switch" not in broker.events


def test_deploy_retry_resumes_same_prepared_operation_before_deadline(tmp_path: Path) -> None:
    instance, broker, runtime = service(tmp_path)
    instance.journal.prepare(
        operation_id="operation-1",
        baseline=broker.context.deployed_identity,
        payload=broker.context.payload,
        start_deadline=120,
        recovery_deadline=600,
        now=100,
    )

    result = instance.deploy("approval-1", operation_id="operation-1")

    assert result.phase is JournalState.HEALTHY
    assert broker.events.count("switch") == 1
    assert broker.events.count("admit") == 1
    assert broker.events.count("started") == 1
    assert runtime.actual == broker.context.payload


def test_deploy_retry_reconciles_switching_payload_without_repeating_effect(
    tmp_path: Path,
) -> None:
    instance, broker, runtime = service(tmp_path)
    record = instance.journal.prepare(
        operation_id="operation-1",
        baseline=broker.context.deployed_identity,
        payload=broker.context.payload,
        start_deadline=120,
        recovery_deadline=600,
        now=100,
    )
    instance.journal.begin_switch(record.operation_id, now=100)
    runtime.actual = broker.context.payload

    result = instance.deploy("approval-1", operation_id="operation-1")

    assert result.phase is JournalState.HEALTHY
    assert broker.events.count("switch") == 0
    assert broker.events.count("build") == 0
    assert broker.events.count("receipt") == 1


def test_deploy_retry_keeps_switching_baseline_ambiguous(tmp_path: Path) -> None:
    instance, broker, runtime = service(tmp_path)
    record = instance.journal.prepare(
        operation_id="operation-1",
        baseline=broker.context.deployed_identity,
        payload=broker.context.payload,
        start_deadline=120,
        recovery_deadline=600,
        now=100,
    )
    instance.journal.begin_switch(record.operation_id, now=100)

    with pytest.raises(HostServiceError) as error:
        instance.deploy("approval-1", operation_id="operation-1")

    assert error.value.code == "reconciliation_required"
    assert runtime.actual == record.baseline
    assert broker.events.count("switch") == 0
    assert instance.journal.get_record("operation-1").phase is JournalState.SWITCHING


def test_reconcile_requires_an_existing_journaled_operation(tmp_path: Path) -> None:
    instance, broker, runtime = service(tmp_path)

    with pytest.raises(HostServiceError):
        instance.reconcile("approval-1", "operation-1")

    assert broker.events == []
    assert runtime.events == []


def test_reopen_finishes_checking_phase_from_real_health_and_smoke(tmp_path: Path) -> None:
    instance, broker, runtime = service(tmp_path)
    record = instance.journal.prepare(
        operation_id="operation-1",
        baseline=broker.context.deployed_identity,
        payload=broker.context.payload,
        start_deadline=120,
        recovery_deadline=600,
        now=100,
    )
    instance.journal.begin_switch(record.operation_id, now=100)
    runtime.actual = broker.context.payload
    instance.journal.reconcile("operation-1", actual=runtime.actual, now=101)
    reopened = HostService(
        source=Source(tmp_path / "fixed-repository"),
        target="sb-bot",
        policy_digest=POLICY,
        journal=HostJournal(tmp_path / "journal.json"),
        broker=broker,
        evidence=Evidence(),
        builder=Builder(broker.events),
        runtime=runtime,
        now=lambda: 102,
    )

    result = reopened.reconcile("approval-1", "operation-1")

    assert result.phase is JournalState.HEALTHY
    assert broker.events.count("switch") == 0
    assert broker.events.count("health") == 1
    assert broker.events.count("smoke") == 1


def test_reopen_restores_same_operation_when_crash_preceded_restore(tmp_path: Path) -> None:
    instance, broker, runtime = service(tmp_path)
    record = instance.journal.prepare(
        operation_id="operation-1",
        baseline=broker.context.deployed_identity,
        payload=broker.context.payload,
        start_deadline=120,
        recovery_deadline=600,
        now=100,
    )
    instance.journal.begin_switch(record.operation_id, now=100)
    runtime.actual = broker.context.payload
    instance.journal.reconcile("operation-1", actual=runtime.actual, now=101)
    instance.journal.reconcile("operation-1", actual=runtime.actual, healthy=False, now=102)
    reopened = HostService(
        source=Source(tmp_path / "fixed-repository"),
        target="sb-bot",
        policy_digest=POLICY,
        journal=HostJournal(tmp_path / "journal.json"),
        broker=broker,
        evidence=Evidence(),
        builder=Builder(broker.events),
        runtime=runtime,
        now=lambda: 103,
    )

    result = reopened.reconcile("approval-1", "operation-1")

    assert result.phase is JournalState.RESTORED_HEALTHY
    assert runtime.actual == broker.context.deployed_identity
    assert broker.events.count("restore_started") == 1
    assert broker.events.count("switch") == 0


def test_newer_config_during_restoration_is_terminal_failure_without_restore(
    tmp_path: Path,
) -> None:
    instance, broker, runtime = service(tmp_path)
    record = instance.journal.prepare(
        operation_id="operation-1",
        baseline=broker.context.deployed_identity,
        payload=broker.context.payload,
        start_deadline=120,
        recovery_deadline=600,
        now=100,
    )
    instance.journal.begin_switch(record.operation_id, now=100)
    runtime.actual = broker.context.payload
    instance.journal.reconcile("operation-1", actual=runtime.actual, now=101)
    instance.journal.reconcile("operation-1", actual=runtime.actual, healthy=False, now=102)
    runtime.actual = DeploymentIdentity(
        git_sha=broker.context.payload.git_sha,
        image_digest=broker.context.payload.image_digest,
        config_generation=12,
        config_digest="6" * 64,
        policy_digest=broker.context.payload.policy_digest,
        target_sequence=broker.context.payload.target_sequence,
    )

    result = instance.reconcile("approval-1", "operation-1")

    assert result.phase is JournalState.RECOVERY_FAILED
    assert "restore" not in broker.events and "restore_started" not in broker.events


def test_expired_checking_operation_requires_reconciliation_without_receipt_or_restore(
    tmp_path: Path,
) -> None:
    instance, broker, runtime = service(tmp_path)
    record = instance.journal.prepare(
        operation_id="operation-1",
        baseline=broker.context.deployed_identity,
        payload=broker.context.payload,
        start_deadline=120,
        recovery_deadline=600,
        now=100,
    )
    instance.journal.begin_switch(record.operation_id, now=100)
    runtime.actual = broker.context.payload
    instance.journal.reconcile("operation-1", actual=runtime.actual, now=101)
    instance._now = lambda: 600

    with pytest.raises(HostServiceError) as error:
        instance.reconcile("approval-1", "operation-1")

    assert error.value.code == "reconciliation_required"
    assert instance.journal.get_record("operation-1").phase is JournalState.CHECKING
    assert "restore" not in broker.events
    assert "restore_started" not in broker.events
    assert "receipt" not in broker.events


def test_competing_operation_is_rejected_while_nonterminal_journal_exists(tmp_path: Path) -> None:
    instance, broker, _ = service(tmp_path)
    record = instance.journal.prepare(
        operation_id="operation-1",
        baseline=broker.context.deployed_identity,
        payload=broker.context.payload,
        start_deadline=120,
        recovery_deadline=600,
        now=100,
    )
    with pytest.raises(OperationConflict):
        instance.journal.prepare(
            operation_id="operation-2",
            baseline=record.baseline,
            payload=record.payload,
            start_deadline=120,
            recovery_deadline=600,
            now=100,
        )


class FakeCommandRunner:
    def __init__(self, expected: DeploymentIdentity) -> None:
        self.expected = expected
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def run(self, argv, *, env, timeout_seconds, max_output_bytes) -> CommandResult:
        self.calls.append((argv, env))
        if any("Config.Labels" in item for item in argv):
            labels = {
                "org.chatcore.release.identity": json.dumps(
                    self.expected.__dict__, sort_keys=True, separators=(",", ":")
                ),
                "org.chatcore.release-control.commit": self.expected.git_sha,
                "org.chatcore.release-control.policy-digest": self.expected.policy_digest,
            }
            return CommandResult(0, json.dumps(labels).encode())
        if "--format={{.Image}}" in argv:
            return CommandResult(0, self.expected.image_digest.encode())
        if "image" in argv:
            return CommandResult(0, self.expected.image_digest.encode())
        if "compose" in argv:
            return CommandResult(0, b"")
        if any("Health.Status" in item for item in argv):
            return CommandResult(0, b"healthy")
        return CommandResult(0, b"ok")


def _fixed_compose_definition(app_env_file: Path) -> dict[str, object]:
    return {
        "name": "sb-bot",
        "services": {
            "sb-bot": {
                "image": "${SB_BOT_IMAGE:?host service must select a verified local image}",
                "container_name": "sb-bot",
                "restart": "unless-stopped",
                "env_file": [str(app_env_file)],
                "environment": {
                    "HF_HUB_OFFLINE": "1",
                    "EMBED_CACHE_DIR": "/models/embeddinggemma-300m",
                    "SB_BOT_RELEASE_IDENTITY": (
                        "${SB_BOT_RELEASE_IDENTITY:?host service must provide identity}"
                    ),
                    "TELEGRAM_TRANSPORT": "bridge",
                    "TELEGRAM_BRIDGE_CREDENTIAL_FILE": "/tmp/telegram-bridge-credential",
                },
                "labels": {
                    "org.chatcore.release.identity": (
                        "${SB_BOT_RELEASE_IDENTITY:?host service must provide identity}"
                    )
                },
                "user": "10001:10001",
                "read_only": True,
                "tmpfs": ["/tmp:size=64m"],
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "mem_limit": "${SB_BOT_MEMORY_LIMIT:?operator measured runtime limit required}",
                "cpus": "${SB_BOT_CPU_LIMIT:?operator measured runtime limit required}",
                "pids_limit": "${SB_BOT_PIDS_LIMIT:?operator measured runtime limit required}",
                "networks": ["app-backend", "app-internal"],
                "volumes": ["app-models:/models:ro"],
                "healthcheck": {
                    "test": ["CMD", "python", "-I", "/usr/local/bin/chatcore-smoke"],
                    "interval": "30s",
                    "timeout": "3s",
                    "start_period": "60s",
                    "retries": 3,
                },
                "logging": {
                    "driver": "json-file",
                    "options": {"max-size": "10m", "max-file": "3"},
                },
            }
        },
        "networks": {
            "app-backend": {"external": True},
            "app-internal": {"external": True},
        },
        "volumes": {"app-models": {"external": True}},
    }


def _fixed_compose_runtime(tmp_path: Path, *, app_env: str | None = None, attack=None):
    app_env_file = tmp_path / "app-runtime.env"
    app_env_file.write_text(
        app_env
        or (
            "BOT_NAME=sb-bot\n"
            "ALLOWED_CHATS=tg:-100123\n"
            "PG_DSN=postgresql://ordinary-app@postgres/sb_bot\n"
            "LLM_PROVIDER=openai_compat\n"
            "LLM_API_KEY=synthetic-ordinary-llm-key\n"
            "TELEGRAM_TRANSPORT=bridge\n"
            "TELEGRAM_BRIDGE_ORIGIN=https://bridge.internal\n"
            "TELEGRAM_BRIDGE_CREDENTIAL=synthetic-ordinary-bridge-credential-0123456789\n"
        ),
        encoding="ascii",
    )
    app_env_file.chmod(0o600)
    compose = tmp_path / "installed-compose.yml"
    definition = _fixed_compose_definition(app_env_file)
    if attack is not None:
        attack(definition["services"]["sb-bot"])
    compose.write_text(json.dumps(definition, sort_keys=True), encoding="ascii")
    compose.chmod(0o644)
    runtime_env = tmp_path / "runtime-limits.env"
    runtime_env.write_text(
        "SB_BOT_MEMORY_LIMIT=4g\nSB_BOT_CPU_LIMIT=4\nSB_BOT_PIDS_LIMIT=256\n",
        encoding="ascii",
    )
    runtime_env.chmod(0o600)
    config_files = tuple(sorted((compose, runtime_env, app_env_file)))
    file_digests = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in config_files
    }
    config_generation = 11
    config_digest = hashlib.sha256(
        json.dumps(
            {"version": 1, "generation": config_generation, "files": file_digests},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    config_manifest = tmp_path / "runtime-config.json"
    config_manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "generation": config_generation,
                "files": file_digests,
                "digest": config_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="ascii",
    )
    config_manifest.chmod(0o600)
    expected = replace(identity(MERGE, "5"), config_digest=config_digest)
    runner = FakeCommandRunner(expected)
    runtime = FixedComposeRuntime(
        FixedComposeConfig(
            docker_path="/usr/bin/docker",
            compose_file=compose,
            runtime_env_file=runtime_env,
            app_env_file=app_env_file,
            config_manifest=config_manifest,
            config_files=config_files,
            docker_config=tmp_path / "docker-config",
            image_repository="local/sb-bot",
        ),
        runner,
    )
    return runtime, runner, expected, app_env_file, compose, config_manifest, file_digests


def test_installed_runtime_templates_use_the_validated_bridge_only_compose_contract(
    tmp_path: Path,
) -> None:
    runtime, _, _, *_ = _fixed_compose_runtime(tmp_path)
    repository = Path(__file__).parents[3]
    template_root = (
        repository / "services/release-control/src/chatcore_release_control" / "templates"
    )
    compose_template = json.loads(
        (template_root / "p08-runtime-compose.yml.template").read_text(encoding="utf-8")
    )
    expected = runtime._expected_compose_definition()
    expected["services"]["sb-bot"]["env_file"] = ["/etc/chatcore-release-control/app-runtime.env"]
    assert compose_template == expected

    entrypoint = (template_root / "p08-entrypoint.sh.template").read_text(encoding="utf-8")
    assert "/run/secrets/telegram_bot_token" not in entrypoint
    assert "export TELEGRAM_BOT_TOKEN" not in entrypoint
    assert "unset credential TELEGRAM_BRIDGE_CREDENTIAL" in entrypoint
    assert "TELEGRAM_BRIDGE_CREDENTIAL_FILE" in entrypoint

    manifest = json.loads(
        (template_root / "p08-runtime-config.json.template").read_text(encoding="utf-8")
    )
    assert "/etc/chatcore-release-control/app-runtime.env" in manifest["files"]
    assert not any("telegram_bot_token" in path for path in manifest["files"])

    host_config = json.loads(
        (template_root / "p08-host-service.json.template").read_text(encoding="utf-8")
    )
    assert host_config["source_app_config_file"] == (
        "/etc/chatcore-release-control/source-app.json"
    )
    assert host_config["app_runtime_env_file"] == ("/etc/chatcore-release-control/app-runtime.env")
    assert host_config["runtime_config_files"] == sorted(host_config["runtime_config_files"])
    assert "/etc/chatcore-release-control/app-runtime.env" in host_config["runtime_config_files"]
    assert not any("telegram_bot_token" in path for path in host_config["runtime_config_files"])


def test_runtime_entrypoint_allows_clean_bridge_env_and_denies_control_authority(
    tmp_path: Path,
) -> None:
    template = (
        Path(__file__).parents[3]
        / "services/release-control/src/chatcore_release_control"
        / "templates"
        / "p08-entrypoint.sh.template"
    ).read_text(encoding="utf-8")
    credential_file = tmp_path / "bridge-credential"
    script = tmp_path / "entrypoint.sh"
    script.write_text(
        template.replace("/tmp/telegram-bridge-credential", str(credential_file)),
        encoding="utf-8",
    )
    script.chmod(0o700)
    marker = tmp_path / "app-started"
    clean_env = {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "TELEGRAM_TRANSPORT": "bridge",
        "TELEGRAM_BRIDGE_CREDENTIAL": "synthetic-bridge-credential-0123456789-abcd",
    }

    clean = subprocess.run(
        [
            "/bin/sh",
            str(script),
            "/bin/sh",
            "-c",
            'test -f "$TELEGRAM_BRIDGE_CREDENTIAL_FILE" && : > "$APP_START_MARKER"',
        ],
        env={**clean_env, "APP_START_MARKER": str(marker)},
        capture_output=True,
        check=False,
    )
    assert clean.returncode == 0
    assert marker.is_file()
    assert credential_file.read_text(encoding="ascii") == clean_env["TELEGRAM_BRIDGE_CREDENTIAL"]
    assert credential_file.stat().st_mode & 0o777 == 0o600

    marker.unlink()
    dirty = subprocess.run(
        ["/bin/sh", str(script), "/bin/sh", "-c", ': > "$APP_START_MARKER"'],
        env={
            **clean_env,
            "GITHUB_APP_PRIVATE_KEY": "synthetic-control-secret",
            "APP_START_MARKER": str(marker),
        },
        capture_output=True,
        check=False,
    )
    assert dirty.returncode == 78
    assert not marker.exists()
    assert b"synthetic-control-secret" not in dirty.stdout + dirty.stderr


def test_fixed_compose_runtime_accepts_only_token_free_bridge_service_before_docker(
    tmp_path: Path,
) -> None:
    runtime, runner, expected, *_ = _fixed_compose_runtime(tmp_path)

    assert runtime.observe() == expected
    assert runner.calls
    assert all(
        not any(
            marker in key.casefold()
            for key in env
            for marker in (
                "telegram_bot_token",
                "bot_api_token",
                "github_token",
                "actions_token",
                "broker_credential",
                "release_control",
                "ledger_",
            )
        )
        for _, env in runner.calls
    )


@pytest.mark.parametrize(
    "credential",
    (
        "TELEGRAM_BOT_TOKEN=synthetic-token\n",
        "GITHUB_APP_PRIVATE_KEY=synthetic-token\n",
        "ACTIONS_RUNNER_TOKEN=synthetic-token\n",
        "ACTIONS_TOKEN=synthetic-token\n",
        "BROKER_CREDENTIAL=synthetic-token\n",
        "RELEASE_CONTROL_TOKEN=synthetic-token\n",
        "LEDGER_DSN=postgresql://privileged\n",
    ),
)
def test_fixed_compose_rejects_control_authority_env_before_docker(
    tmp_path: Path, credential: str
) -> None:
    runtime, runner, *_ = _fixed_compose_runtime(tmp_path, app_env=credential)

    with pytest.raises(HostServiceError) as error:
        runtime.observe()

    assert error.value.code == "invalid_configuration"
    assert runner.calls == []


@pytest.mark.parametrize(
    "attack",
    (
        lambda service: service.update({"privileged": True}),
        lambda service: service["volumes"].append("/var/run/docker.sock:/var/run/docker.sock"),
        lambda service: service.update({"entrypoint": ["/bin/sh", "-c", "unsafe"]}),
        lambda service: service.update({"command": ["/bin/sh", "-c", "unsafe"]}),
    ),
)
def test_fixed_compose_rejects_privilege_sockets_and_command_overrides_before_docker(
    tmp_path: Path, attack
) -> None:
    runtime, runner, *_ = _fixed_compose_runtime(tmp_path, attack=attack)

    with pytest.raises(HostServiceError) as error:
        runtime.observe()

    assert error.value.code == "invalid_configuration"
    assert runner.calls == []


def test_fixed_compose_runtime_uses_installed_files_and_digest_tag(tmp_path: Path) -> None:
    runtime, runner, expected, app_env_file, compose, config_manifest, file_digests = (
        _fixed_compose_runtime(tmp_path)
    )

    runtime.switch(expected)

    commands = [call[0] for call in runner.calls]
    assert commands[0] == (
        "/usr/bin/docker",
        "--context",
        "default",
        "image",
        "inspect",
        "--format={{.Id}}",
        f"local/sb-bot:release-{MERGE}",
    )
    assert commands[1][3] == "image" and "Config.Labels" in commands[1][5]
    assert commands[2][3:6] == ("compose", "--project-name", "sb-bot")
    assert "--no-deps" in commands[2] and "sb-bot" == commands[2][-1]
    assert commands[3][3] == "inspect" and "--format={{.Image}}" in commands[3]
    assert all("--mount" not in command and "-v" not in command for command in commands)
    assert all(
        not any(
            marker in key.casefold()
            for key in env
            for marker in (
                "telegram_bot_token",
                "bot_api_token",
                "github_token",
                "actions_token",
                "broker_credential",
                "release_control",
                "ledger_",
                "database_url",
            )
        )
        for _, env in runner.calls
    )

    compose.write_text("{}", encoding="ascii")
    with pytest.raises(HostServiceError):
        runtime.observe()
    compose_definition = _fixed_compose_definition(app_env_file)
    compose.write_text(json.dumps(compose_definition, sort_keys=True), encoding="ascii")

    app_env_file.write_text(
        "TELEGRAM_TRANSPORT=bridge\n"
        "TELEGRAM_BRIDGE_ORIGIN=https://bridge.internal\n"
        "TELEGRAM_BRIDGE_CREDENTIAL=changed-ordinary-bridge-credential-0123456789\n",
        encoding="ascii",
    )
    file_digests[str(app_env_file)] = hashlib.sha256(app_env_file.read_bytes()).hexdigest()
    config_generation = 12
    config_digest = hashlib.sha256(
        json.dumps(
            {"version": 1, "generation": config_generation, "files": file_digests},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    config_manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "generation": config_generation,
                "files": file_digests,
                "digest": config_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="ascii",
    )
    with pytest.raises(HostServiceError) as error:
        runtime.observe()
    assert error.value.code == "reconciliation_required"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_repo(repo: Path, *, change_migration: bool = False):
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Host test")
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/release.yml").write_text("name: fixed\n", encoding="utf-8")
    (repo / "apps/sb_bot").mkdir(parents=True)
    (repo / "apps/sb_bot/app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "apps/sb_bot/deploy").mkdir()
    (repo / "apps/sb_bot/deploy/backup.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (repo / "apps/sb_bot/migrations").mkdir()
    (repo / "apps/sb_bot/migrations/001.sql").write_text("select 1;\n", encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "apps/sb_bot/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    if change_migration:
        (repo / "apps/sb_bot/migrations/001.sql").write_text("select 2;\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "feature")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    (repo / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "merge", "--no-ff", "feature", "-m", "release")
    merge = _git(repo, "rev-parse", "HEAD")
    merge_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    workflow_digest = "5" * 64
    policy = PolicyManifest(
        repository_id=1,
        owner_id=2,
        target="sb-bot",
        workflow_digests=((".github/workflows/release.yml", workflow_digest),),
        allowed_paths=(
            "apps/sb_bot/app.py",
            "apps/sb_bot/migrations/001.sql",
            "uv.lock",
        ),
        protected_paths=(".github/workflows/release.yml",),
        checks=(
            TrustedCheck(
                name="verify",
                creator_app_id=7,
                workflow_path=".github/workflows/release.yml",
                workflow_digest=workflow_digest,
            ),
        ),
    )
    return baseline, base, head, merge, merge_tree, policy


def test_p01_host_evidence_checks_actual_git_history_and_migration_equality(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    baseline, base, head, merge, merge_tree, policy = _git_repo(repo)
    context = HostDeploymentContext(
        "approval",
        "sb-bot",
        8,
        3,
        "merged_verified",
        merge,
        policy.digest,
        identity(baseline, "a"),
        base_sha=base,
        head_sha=head,
        head_tree=_git(repo, "rev-parse", f"{head}^{{tree}}"),
        expected_merge_tree=merge_tree,
        merge_tree=merge_tree,
        merge_parents=(base, head),
    )
    verifier = P01HostEvidence(policy)

    assert verifier.verify(repo, context)
    changed_repo = tmp_path / "migration-repo"
    (
        changed_baseline,
        changed_base,
        changed_head,
        changed_merge,
        changed_tree,
        changed_policy,
    ) = _git_repo(changed_repo, change_migration=True)
    changed_context = HostDeploymentContext(
        "approval",
        "sb-bot",
        8,
        3,
        "merged_verified",
        changed_merge,
        changed_policy.digest,
        identity(changed_baseline, "a"),
        base_sha=changed_base,
        head_sha=changed_head,
        head_tree=_git(changed_repo, "rev-parse", f"{changed_head}^{{tree}}"),
        expected_merge_tree=changed_tree,
        merge_tree=changed_tree,
        merge_parents=(changed_base, changed_head),
    )
    assert not verifier.verify(changed_repo, changed_context)


def test_p01_host_evidence_rejects_wrong_merge_parents_and_expected_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    baseline, base, head, merge, merge_tree, policy = _git_repo(repo)
    wrong_parents = _git(
        repo,
        "commit-tree",
        merge_tree,
        "-p",
        head,
        "-p",
        base,
        "-m",
        "wrong parent order",
        "-m",
        "wrong parent order",
    )
    context = HostDeploymentContext(
        "approval",
        "sb-bot",
        8,
        3,
        "merged_verified",
        wrong_parents,
        policy.digest,
        identity(baseline, "a"),
        base_sha=base,
        head_sha=head,
        head_tree=_git(repo, "rev-parse", f"{head}^{{tree}}"),
        expected_merge_tree=merge_tree,
        merge_tree=merge_tree,
        merge_parents=(base, head),
    )

    assert not P01HostEvidence(policy).verify(repo, context)

    other_tree = _git(repo, "rev-parse", f"{head}^{{tree}}")
    wrong_tree = _git(
        repo,
        "commit-tree",
        other_tree,
        "-p",
        base,
        "-p",
        head,
        "-m",
        "wrong merge tree",
    )
    assert not P01HostEvidence(policy).verify(repo, replace(context, merge_commit=wrong_tree))


def test_git_source_fetches_only_fixed_repository_exact_commits_with_host_credential(
    tmp_path: Path,
) -> None:
    baseline = "1" * 40
    merge = "2" * 40
    credential = tmp_path / "source.token"
    token = "temporary-test-token-12345678901234567890"
    credential.write_text(token, encoding="ascii")
    credential.chmod(0o600)

    class GitRunner:
        calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

        def run(self, argv, *, env, timeout_seconds, max_output_bytes) -> CommandResult:
            self.calls.append((argv, env))
            return CommandResult(0, b"")

    runner = GitRunner()
    source = GitExactCommitSource(
        GitSourceConfig(
            repository_url="https://github.com/example/repository.git",
            git_path="/usr/bin/git",
            credential_file=credential,
            scratch_directory=tmp_path / "scratch",
        ),
        runner,
    )
    assert token not in repr(source)
    with source.fetch(merge, baseline) as fetched:
        assert fetched.name == "source.git"

    commands = [argv for argv, _ in runner.calls]
    fetch = next(argv for argv in commands if "fetch" in argv)
    assert fetch[-3:] == ("origin", merge, baseline)
    # The token is in a short-lived askpass file, never argv or child environment.
    assert all(token not in " ".join(argv) for argv in commands)
    assert all(token not in value for _, env in runner.calls for value in env.values())
    assert not any("--depth" in argv for argv in commands)
    assert any("http.followRedirects=false" in argv for argv in commands)
    assert not fetched.exists()


class FakeBuildRunner:
    def __init__(self, digest: str, *, resource_digest: str) -> None:
        self.digest = digest
        self.resource_digest = resource_digest
        self.builder_running = True
        self.memory_swap = 2_000_000_000
        self.endpoint = "unix:///var/run/docker.sock"
        self.mounts = []
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.labels: dict[str, str] = {}

    def run(self, argv, *, env, timeout_seconds, max_output_bytes) -> CommandResult:
        self.calls.append((argv, env))
        if "buildx" in argv and "inspect" in argv:
            return CommandResult(
                0,
                b"Name: chatcore-release-build\n"
                b"Driver: docker-container\n"
                b"Nodes:\nName: chatcore-release-build0\n"
                b"Endpoint: default\nStatus: running\n",
            )
        if "context" in argv and "inspect" in argv:
            return CommandResult(0, json.dumps(self.endpoint).encode())
        if "--type" in argv and "container" in argv:
            config = {
                "Image": "ghcr.io/moby/buildkit:v0.0.0@sha256:" + "a" * 64,
                "Labels": {
                    "org.chatcore.release-control.builder": "chatcore-release-build",
                    "org.chatcore.release-control.resource-manifest": self.resource_digest,
                },
            }
            host = {
                "Memory": 2_000_000_000,
                "MemorySwap": self.memory_swap,
                "CpuQuota": 500_000,
                "PidsLimit": 128,
                "CgroupParent": "/system.slice/chatcore-release-build.slice",
                "NetworkMode": "none",
                "Privileged": False,
                "ReadonlyRootfs": True,
                "Tmpfs": {"/var/lib/buildkit": "rw,nosuid,nodev,noexec,size=1000000000"},
                "Binds": [],
            }
            return CommandResult(
                0,
                json.dumps(
                    {
                        "Image": "sha256:" + "b" * 64,
                        "Config": {"Image": config["Image"], "Labels": config["Labels"]},
                        "HostConfig": host,
                        "Mounts": self.mounts,
                        "State": {"Running": self.builder_running},
                    }
                ).encode(),
            )
        if "buildx" in argv and "build" in argv:
            self.labels = {
                key: value
                for key, value in (
                    arg.removeprefix("--label=").split("=", 1)
                    for arg in argv
                    if arg.startswith("--label=")
                )
            }
            return CommandResult(0, b"fixed recipe build output")
        if "Config.Labels" in " ".join(argv):
            return CommandResult(0, json.dumps(self.labels).encode())
        return CommandResult(0, self.digest.encode())


def test_fixed_builder_uses_offline_installed_recipe_and_verified_git_artifact(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    baseline, _base, _head, merge, _merge_tree, policy = _git_repo(repo)
    context = HostDeploymentContext(
        "approval",
        "sb-bot",
        8,
        3,
        "merged_verified",
        merge,
        policy.digest,
        identity(baseline, "a"),
    )
    templates = (
        Path(__file__).parents[3]
        / "services/release-control/src/chatcore_release_control"
        / "templates"
    )
    cache = tmp_path / "dependency-cache.tar"
    cache_content = tmp_path / "fixed-wheel.whl"
    cache_content.write_bytes(b"fixed offline cache")
    with tarfile.open(cache, mode="w") as archive:
        archive.add(cache_content, arcname="wheels/fixed-wheel.whl")
    cache.chmod(0o600)
    worker = Path(__file__).parents[1] / "src/chatcore_release_control/development_worker.py"
    harness = Path(__file__).parents[1] / "src/chatcore_release_control/monorepo_harness.py"
    recipe = templates / "p08-app.Dockerfile.template"
    entrypoint = templates / "p08-entrypoint.sh.template"
    smoke = templates / "p08-smoke.py.template"
    manifest_path = tmp_path / "resource-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "builder": "chatcore-release-build",
                "builder_container": "buildx_buildkit_chatcore-release-build0",
                "driver": "docker-container",
                "docker_context": "default",
                "docker_endpoint": "unix:///var/run/docker.sock",
                "buildkit_image_ref": "ghcr.io/moby/buildkit:v0.0.0@sha256:" + "a" * 64,
                "buildkit_image_id": "sha256:" + "b" * 64,
                "builder_cgroup": "/system.slice/chatcore-release-build.slice",
                "cpu_quota_micros": 500_000,
                "memory_max_bytes": 2_000_000_000,
                "memory_swap_bytes": 2_000_000_000,
                "tasks_max": 128,
                "scratch_max_bytes": 1_000_000_000,
                "scratch_mount": "/var/lib/buildkit",
                "network": "none",
                "reviewed_source_exceptions": ["apps/sb_bot/deploy/backup.sh"],
            }
        ),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    builder = FixedOfflineImageBuilder(
        FixedBuildConfig(
            docker_path="/usr/bin/docker",
            recipe=recipe,
            entrypoint=entrypoint,
            smoke_probe=smoke,
            dependency_cache=cache,
            dependency_cache_sha256=hashlib.sha256(cache.read_bytes()).hexdigest(),
            resource_manifest=manifest_path,
            scratch_directory=tmp_path / "builds",
            image_repository="local/sb-bot",
            allowed_paths=(
                "apps/sb_bot/app.py",
                "apps/sb_bot/migrations/001.sql",
                "uv.lock",
            ),
            source_roots=("apps/sb_bot",),
            test_paths=("apps/sb_bot",),
            worker_source=worker,
            harness_source=harness,
            reviewed_source_exceptions=("apps/sb_bot/deploy/backup.sh",),
            cpu_quota_micros=500_000,
            memory_bytes=2_000_000_000,
            pids_max=128,
            scratch_bytes=1_000_000_000,
            buildx_builder="chatcore-release-build",
            buildkit_container="buildx_buildkit_chatcore-release-build0",
            buildkit_image_ref="ghcr.io/moby/buildkit:v0.0.0@sha256:" + "a" * 64,
            buildkit_image_id="sha256:" + "b" * 64,
        ),
        FakeBuildRunner(
            "sha256:" + "9" * 64,
            resource_digest=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        ),
    )
    runner = builder.runner

    result = builder.build(repo, context)

    assert result == "sha256:" + "9" * 64
    command, env = next(call for call in runner.calls if "buildx" in call[0] and "build" in call[0])
    assert command[3:6] == ("buildx", "build", "--builder=chatcore-release-build")
    assert (
        "--pull=false" in command
        and "--network=none" in command
        and "--no-cache" in command
        and "--cgroup-parent=/system.slice/chatcore-release-build.slice" in command
    )
    assert "--mount" not in command and "--secret" not in command
    assert "Dockerfile" in command[command.index("--file") + 1]
    assert not any(
        key.casefold().startswith(("telegram", "github", "release_control")) for key in env
    )
    assert runner.labels["org.chatcore.release-control.commit"] == merge
    assert runner.labels["org.chatcore.release-control.policy-digest"] == policy.digest
    builds_before = sum("build" in argv and "buildx" in argv for argv, _ in runner.calls)
    runner.memory_swap = 4_000_000_000
    with pytest.raises(HostServiceError):
        builder.build(repo, context)
    assert sum("build" in argv and "buildx" in argv for argv, _ in runner.calls) == builds_before
    runner.memory_swap = 2_000_000_000
    runner.endpoint = "tcp://untrusted.invalid:2375"
    with pytest.raises(HostServiceError):
        builder.build(repo, context)
    runner.endpoint = "unix:///var/run/docker.sock"
    runner.mounts = [{"Type": "bind", "Destination": "/workspace", "RW": True}]
    with pytest.raises(HostServiceError):
        builder.build(repo, context)
    assert sum("build" in argv and "buildx" in argv for argv, _ in runner.calls) == builds_before
