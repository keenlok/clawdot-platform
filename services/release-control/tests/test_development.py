from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
from pathlib import Path

import pytest
from chatcore.llm.types import Completion, Message
from chatcore_release_control.development import (
    CommandResult,
    CommandTimeout,
    DevelopmentController,
    DevelopmentError,
    DockerSandboxRunner,
    GenerationRequest,
    ResourcePolicy,
    SandboxConfig,
    SubprocessExecutor,
)
from chatcore_release_control.policy import PolicyManifest

POLICY_DIGEST = "a" * 64
BASE_COMMIT = "b" * 40
IMAGE_REFERENCE = "example.invalid/release-worker@sha256:" + "c" * 64
WORKER_MANIFEST_DIGEST = "e" * 64
BASELINE_COMMIT_LABEL = "org.chatcore.release-control.baseline-commit"


def policy() -> PolicyManifest:
    return PolicyManifest.from_json(
        json.dumps(
            {
                "version": 1,
                "repository_id": 101,
                "owner_id": 202,
                "target": "production",
                "workflow_digests": {".github/workflows/ci.yml": "d" * 64},
                "allowed_paths": ["src/module.py", "tests/test_module.py"],
                "protected_paths": [".github/workflows/ci.yml", "uv.lock"],
                "checks": [
                    {
                        "name": "candidate-tests",
                        "creator_app_id": 303,
                        "workflow_path": ".github/workflows/ci.yml",
                        "workflow_sha": "d" * 64,
                    }
                ],
            }
        )
    )


def generation_request(**changes) -> GenerationRequest:
    values = {
        "specification": "Add one to the input value.",
        "synthetic_examples": ({"value": 1, "expected": 2},),
        "base_commit": BASE_COMMIT,
        "policy_digest": policy().digest,
    }
    values.update(changes)
    return GenerationRequest(**values)


def resource_policy(**changes) -> ResourcePolicy:
    values = {
        "cpu_millis": 500,
        "memory_bytes": 128 * 1024 * 1024,
        "pids_limit": 32,
        "timeout_seconds": 5,
        "max_output_bytes": 16 * 1024,
        "workspace_bytes": 4 * 1024 * 1024,
    }
    values.update(changes)
    return ResourcePolicy(**values)


def sandbox_config(runtime: Path, **changes) -> SandboxConfig:
    runtime.chmod(0o755)
    values = {
        "docker_path": "/usr/bin/docker",
        "runtime_name": "runsc",
        "runtime_path": str(runtime),
        "runtime_digest": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "image_reference": IMAGE_REFERENCE,
        "worker_manifest_digest": WORKER_MANIFEST_DIGEST,
        "policy_digest": policy().digest,
        "baseline_commit": BASE_COMMIT,
        "resources": resource_policy(),
    }
    values.update(changes)
    return SandboxConfig(**values)


class FakeProvider:
    def __init__(self, response: str, *, stop_reason: str = "end_turn", fail: bool = False):
        self.response = response
        self.stop_reason = stop_reason
        self.fail = fail
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.fail:
            raise RuntimeError("private provider error")
        return Completion(Message.assistant(self.response), self.stop_reason)


class FakeRunner:
    max_output_bytes = 16 * 1024
    max_input_bytes = 4 * 1024 * 1024
    worker_manifest_digest = WORKER_MANIFEST_DIGEST
    policy_digest = ""
    baseline_commit = BASE_COMMIT

    def __init__(self, report: bytes | None = None):
        self.report = (
            report
            if report is not None
            else json.dumps(
                {
                    "version": 2,
                    "base_commit": BASE_COMMIT,
                    "policy_digest": policy().digest,
                    "worker_manifest_digest": WORKER_MANIFEST_DIGEST,
                    "execution_kind": "monorepo-pytest-v1",
                    "exit_status": 0,
                    "output": "synthetic execution output, no authoritative test counts",
                }
            ).encode()
        )
        self.calls: list[bytes] = []

    def run(self, request_bytes: bytes) -> bytes:
        self.calls.append(request_bytes)
        return self.report


class FakeExecutor:
    def __init__(
        self,
        runtime_path: str,
        *,
        fail: str | None = None,
        report=b'{"ok":true}',
        repo_digests=None,
        volumes=b"null",
        labels=None,
    ):
        self.runtime_path = runtime_path
        self.fail = fail
        self.report = report
        self.repo_digests = [IMAGE_REFERENCE] if repo_digests is None else repo_digests
        self.volumes = volumes
        self.labels = labels or {
            "org.chatcore.release-control.worker-manifest-digest": WORKER_MANIFEST_DIGEST,
            "org.chatcore.release-control.policy-digest": policy().digest,
            BASELINE_COMMIT_LABEL: BASE_COMMIT,
        }
        self.calls: list[tuple[list[str], dict[str, str], float, int]] = []
        self.container_id = "1" * 64

    def run(self, argv, *, env, timeout_seconds, max_output_bytes):
        args = list(argv)
        self.calls.append((args, dict(env), timeout_seconds, max_output_bytes))
        action = next(
            (
                item
                for item in args
                if item in {"info", "inspect", "create", "start", "cp", "exec", "rm"}
            ),
            "",
        )
        if self.fail == action:
            raise CommandTimeout()
        if action == "info":
            output = json.dumps({"runsc": {"path": self.runtime_path}}).encode()
        elif action == "inspect" and "RepoDigests" in args[-2]:
            output = json.dumps(self.repo_digests).encode()
        elif action == "inspect" and "Config.Labels" in args[-2]:
            output = json.dumps(self.labels).encode()
        elif action == "inspect":
            output = self.volumes
        elif action == "create":
            output = self.container_id.encode()
        elif action == "exec":
            output = self.report
        else:
            output = b""
        return CommandResult(returncode=0, stdout=output, stderr=b"")


def fake_controller(
    *,
    provider: FakeProvider,
    runner: FakeRunner,
    scanner=lambda _report: True,
    installed_policy: PolicyManifest | None = None,
) -> DevelopmentController:
    selected_policy = installed_policy or policy()
    runner.policy_digest = selected_policy.digest
    return DevelopmentController(
        provider=provider,
        installed_policy=selected_policy,
        runner=runner,
        report_scanner=scanner,
        base_commit_verifier=lambda _base, _digest: True,
    )


def test_generation_request_binds_policy_base_and_synthetic_examples() -> None:
    request = GenerationRequest(
        specification="Return the value with one added.",
        synthetic_examples=({"value": 1, "expected": 2},),
        base_commit=BASE_COMMIT,
        policy_digest=POLICY_DIGEST,
    )

    assert request.base_commit == BASE_COMMIT
    assert request.policy_digest == POLICY_DIGEST
    assert request.synthetic_examples == ({"expected": 2, "value": 1},)

    with pytest.raises(DevelopmentError):
        GenerationRequest(
            specification="",
            synthetic_examples=(),
            base_commit=BASE_COMMIT,
            policy_digest=POLICY_DIGEST,
        )


def test_controller_generates_then_runs_only_allowlisted_text_files() -> None:
    provider = FakeProvider(
        json.dumps({"files": {"src/module.py": "def run(value):\n    return value + 1\n"}})
    )
    runner = FakeRunner()
    controller = fake_controller(provider=provider, runner=runner)

    result = asyncio.run(controller.generate_and_run(generation_request()))

    assert len(provider.calls) == 1
    messages, kwargs = provider.calls[0]
    prompt = messages[0].text
    assert "synthetic_examples" in prompt
    assert "policy_digest" in prompt
    assert kwargs["tools"] == ()
    assert len(runner.calls) == 1
    candidate_request = json.loads(runner.calls[0])
    assert candidate_request["base_commit"] == BASE_COMMIT
    assert candidate_request["files"] == {
        "src/module.py": "def run(value):\n    return value + 1\n"
    }
    assert result.report == runner.report
    assert result.candidate_files == (("src/module.py", "def run(value):\n    return value + 1\n"),)
    assert "return value + 1" not in repr(result)
    assert result.policy_digest == policy().digest
    assert result.base_commit == BASE_COMMIT


@pytest.mark.parametrize(
    "generated",
    [
        {"files": {"../outside.py": "x"}},
        {"files": {"uv.lock": "x"}},
        {"files": {"src/private.ini": "x"}},
        {"files": {"src/module.py": "\x00"}},
        {"files": {"src/module.py": "x" * 40_000}},
        {"files": {}},
        {"files": {"src/module.py": "x"}, "extra": True},
    ],
)
def test_candidate_path_and_text_failures_stop_before_runner(generated) -> None:
    provider = FakeProvider(json.dumps(generated))
    runner = FakeRunner()

    with pytest.raises(DevelopmentError):
        asyncio.run(
            fake_controller(provider=provider, runner=runner).generate_and_run(generation_request())
        )
    assert runner.calls == []


def test_controller_rejects_policy_mismatch_before_provider_or_runner() -> None:
    provider = FakeProvider('{"files":{"src/module.py":"ok"}}')
    runner = FakeRunner()
    request = generation_request(policy_digest="f" * 64)

    with pytest.raises(DevelopmentError):
        asyncio.run(fake_controller(provider=provider, runner=runner).generate_and_run(request))
    assert provider.calls == []
    assert runner.calls == []


def test_controller_requires_and_uses_trusted_base_commit_verifier() -> None:
    provider = FakeProvider('{"files":{"src/module.py":"ok"}}')
    runner = FakeRunner()
    runner.policy_digest = policy().digest
    with pytest.raises(DevelopmentError):
        DevelopmentController(
            provider=provider,
            installed_policy=policy(),
            runner=runner,
            report_scanner=lambda _report: True,
            base_commit_verifier=None,
        )

    verified = []
    controller = DevelopmentController(
        provider=provider,
        installed_policy=policy(),
        runner=runner,
        report_scanner=lambda _report: True,
        base_commit_verifier=lambda base, digest: verified.append((base, digest)) or False,
    )
    with pytest.raises(DevelopmentError, match="policy_mismatch"):
        asyncio.run(controller.generate_and_run(generation_request()))
    assert verified == [(BASE_COMMIT, policy().digest)]
    assert provider.calls == []
    assert runner.calls == []


def test_controller_rejects_base_outside_the_installed_worker_image() -> None:
    provider = FakeProvider('{"files":{"src/module.py":"ok"}}')
    runner = FakeRunner()
    runner.policy_digest = policy().digest
    verified = []
    controller = DevelopmentController(
        provider=provider,
        installed_policy=policy(),
        runner=runner,
        report_scanner=lambda _report: True,
        base_commit_verifier=lambda base, digest: verified.append((base, digest)) or True,
    )

    with pytest.raises(DevelopmentError, match="policy_mismatch"):
        asyncio.run(controller.generate_and_run(generation_request(base_commit="f" * 40)))
    assert verified == []
    assert provider.calls == []
    assert runner.calls == []


@pytest.mark.parametrize(
    "provider",
    [
        FakeProvider("x" * 140_000),
        FakeProvider("{}", stop_reason="tool_use"),
        FakeProvider("{}", fail=True),
    ],
)
def test_provider_failure_or_oversized_completion_stops_before_runner(provider) -> None:
    runner = FakeRunner()
    controller = fake_controller(provider=provider, runner=runner)

    with pytest.raises(DevelopmentError):
        asyncio.run(controller.generate_and_run(generation_request()))
    assert runner.calls == []


def test_controller_requires_a_report_scanner() -> None:
    with pytest.raises(DevelopmentError):
        DevelopmentController(
            provider=FakeProvider('{"files":{"src/module.py":"ok"}}'),
            installed_policy=policy(),
            runner=FakeRunner(),
            report_scanner=None,
        )


@pytest.mark.parametrize(
    "response",
    ["not json", '{"files":{"src/module.py":"x"},"files":{"tests/test_module.py":"y"}}'],
)
def test_invalid_provider_responses_do_not_start_candidate(response: str) -> None:
    provider = FakeProvider(response)
    runner = FakeRunner()
    with pytest.raises(DevelopmentError):
        asyncio.run(
            fake_controller(provider=provider, runner=runner).generate_and_run(generation_request())
        )
    assert runner.calls == []


def test_scanner_must_accept_report_before_result_is_returned() -> None:
    provider = FakeProvider('{"files":{"src/module.py":"ok"}}')
    runner = FakeRunner(b"secret-sentinel")
    controller = fake_controller(provider=provider, runner=runner, scanner=lambda _report: False)

    with pytest.raises(DevelopmentError, match="report_rejected"):
        asyncio.run(controller.generate_and_run(generation_request()))


def test_sandbox_preflight_and_fixed_docker_argv(tmp_path: Path) -> None:
    runtime = tmp_path / "runsc"
    runtime.write_bytes(b"synthetic runsc executable identity")
    executor = FakeExecutor(str(runtime))
    runner = DockerSandboxRunner(sandbox_config(runtime), executor=executor)

    report = runner.run(b'{"files":{}}')

    assert report == b'{"ok":true}'
    create = next(call for call in executor.calls if "create" in call[0])
    argv, env, timeout, output_cap = create
    for item in (
        "--runtime=runsc",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--security-opt=no-new-privileges",
        "--cap-drop=ALL",
        "--user=65532:65532",
        "--cpus=0.5",
        "--memory=134217728",
        "--memory-swap=134217728",
        "--pids-limit=32",
        "--tmpfs",
        IMAGE_REFERENCE,
    ):
        assert item in argv
    assert "--mount" not in argv
    assert "-v" not in argv
    create_index = argv.index("create")
    assert "/var/run/docker.sock" not in argv[create_index + 1 :]
    assert set(env) == {"PATH", "LANG", "LC_ALL", "HOME", "DOCKER_CONFIG"}
    assert timeout <= 5
    assert output_cap <= 64 * 1024
    assert "rm" in executor.calls[-1][0]
    assert executor.calls[-1][0][-1] == executor.container_id
    actions = [
        next(
            item
            for item in call[0]
            if item in {"info", "inspect", "create", "start", "cp", "exec", "rm"}
        )
        for call in executor.calls
    ]
    assert actions[-5:] == ["create", "start", "cp", "exec", "rm"]
    worker_command = next(call[0] for call in executor.calls if "exec" in call[0])
    assert "/usr/bin/env" in worker_command
    assert "-i" in worker_command
    assert "/opt/chatcore-release/development_worker.py" in worker_command
    assert WORKER_MANIFEST_DIGEST in worker_command
    assert "/bin/sh" not in worker_command


@pytest.mark.parametrize(
    "executor_factory,config_changes",
    [
        (lambda runtime: FakeExecutor(str(runtime) + ".other"), {}),
        (lambda runtime: FakeExecutor(str(runtime), repo_digests=[]), {}),
        (lambda runtime: FakeExecutor(str(runtime), volumes=b'{"/secret":{}}'), {}),
        (
            lambda runtime: FakeExecutor(
                str(runtime),
                labels={
                    "org.chatcore.release-control.worker-manifest-digest": WORKER_MANIFEST_DIGEST,
                    "org.chatcore.release-control.policy-digest": "0" * 64,
                },
            ),
            {},
        ),
        (
            lambda runtime: FakeExecutor(
                str(runtime),
                labels={
                    "org.chatcore.release-control.worker-manifest-digest": WORKER_MANIFEST_DIGEST,
                    "org.chatcore.release-control.policy-digest": policy().digest,
                    BASELINE_COMMIT_LABEL: "f" * 40,
                },
            ),
            {},
        ),
        (lambda runtime: FakeExecutor(str(runtime)), {"runtime_digest": "f" * 64}),
    ],
)
def test_unverified_runtime_or_image_stops_before_container_creation(
    tmp_path: Path, executor_factory, config_changes
) -> None:
    runtime = tmp_path / "runsc"
    runtime.write_bytes(b"synthetic runsc executable identity")
    executor = executor_factory(runtime)
    runner = DockerSandboxRunner(sandbox_config(runtime, **config_changes), executor=executor)

    with pytest.raises(DevelopmentError):
        runner.run(b'{"files":{}}')
    assert not any("create" in call[0] for call in executor.calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"image_reference": "example.invalid/worker:latest"},
        {"runtime_name": "runc"},
        {"runtime_digest": "not-a-digest"},
        {"docker_path": "docker"},
        {"resources": lambda: resource_policy(cpu_millis=True)},
        {"resources": lambda: resource_policy(memory_bytes=-1)},
    ],
)
def test_sandbox_configuration_fails_closed(tmp_path: Path, changes) -> None:
    runtime = tmp_path / "runsc"
    runtime.write_bytes(b"synthetic runsc executable identity")
    with pytest.raises(DevelopmentError):
        changes = {key: value() if callable(value) else value for key, value in changes.items()}
        sandbox_config(runtime, **changes)


@pytest.mark.parametrize("failure", ["start", "cp", "exec"])
def test_container_is_removed_by_exact_id_after_lifecycle_failures(
    tmp_path: Path, failure: str
) -> None:
    runtime = tmp_path / "runsc"
    runtime.write_bytes(b"synthetic runsc executable identity")
    executor = FakeExecutor(str(runtime), fail=failure)
    runner = DockerSandboxRunner(sandbox_config(runtime), executor=executor)

    with pytest.raises(DevelopmentError):
        runner.run(b'{"files":{}}')
    assert "rm" in executor.calls[-1][0]
    assert executor.calls[-1][0][-1] == executor.container_id


def test_create_timeout_cleans_up_using_exact_random_name(tmp_path: Path) -> None:
    runtime = tmp_path / "runsc"
    runtime.write_bytes(b"synthetic runsc executable identity")
    executor = FakeExecutor(str(runtime), fail="create")
    runner = DockerSandboxRunner(sandbox_config(runtime), executor=executor)

    with pytest.raises(DevelopmentError):
        runner.run(b'{"files":{}}')
    cleanup = executor.calls[-1][0]
    assert "rm" in cleanup
    assert cleanup[-1].startswith("chatcore-dev-")


def test_worker_output_limit_and_cleanup_failure_fail_closed(tmp_path: Path) -> None:
    runtime = tmp_path / "runsc"
    runtime.write_bytes(b"synthetic runsc executable identity")
    executor = FakeExecutor(str(runtime), report=b"x" * 20_000)
    runner = DockerSandboxRunner(sandbox_config(runtime), executor=executor)

    with pytest.raises(DevelopmentError):
        runner.run(b'{"files":{}}')
    assert "rm" in executor.calls[-1][0]
    assert executor.calls[-1][0][-1] == executor.container_id


def test_cleanup_failure_fails_closed_without_returning_report(tmp_path: Path) -> None:
    runtime = tmp_path / "runsc"
    runtime.write_bytes(b"synthetic runsc executable identity")
    executor = FakeExecutor(str(runtime), fail="rm")
    runner = DockerSandboxRunner(sandbox_config(runtime), executor=executor)

    with pytest.raises(DevelopmentError, match="sandbox_cleanup_failed"):
        runner.run(b'{"files":{}}')


def test_subprocess_executor_enforces_output_and_time_limits(tmp_path: Path) -> None:
    executor = SubprocessExecutor()
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(tmp_path),
        "DOCKER_CONFIG": str(tmp_path),
    }

    with pytest.raises(DevelopmentError, match="sandbox_output_limit"):
        executor.run(
            [sys.executable, "-c", "print('x' * 10000)"],
            env=env,
            timeout_seconds=5,
            max_output_bytes=1024,
        )
    with pytest.raises(CommandTimeout):
        executor.run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            env=env,
            timeout_seconds=0.05,
            max_output_bytes=1024,
        )


def test_controller_waits_for_runner_cleanup_before_propagating_cancellation() -> None:
    started = threading.Event()
    cleaned = threading.Event()

    class BlockingRunner(FakeRunner):
        def run(self, request_bytes: bytes) -> bytes:
            started.set()
            try:
                threading.Event().wait(0.05)
                return self.report
            finally:
                cleaned.set()

    async def exercise() -> None:
        task = asyncio.create_task(
            fake_controller(
                provider=FakeProvider('{"files":{"src/module.py":"ok"}}'),
                runner=BlockingRunner(),
            ).generate_and_run(generation_request())
        )
        await asyncio.to_thread(started.wait)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "change",
    [
        {"version": 1},
        {"base_commit": "f" * 40},
        {"policy_digest": "f" * 64},
        {"worker_manifest_digest": "f" * 64},
        {"execution_kind": "arbitrary-shell"},
        {"passed": True},
        {"exit_status": True},
        {"output": "\ud800"},
    ],
)
def test_controller_rejects_malformed_or_wrong_identity_execution_reports(change):
    report = {
        "version": 2,
        "base_commit": BASE_COMMIT,
        "policy_digest": policy().digest,
        "worker_manifest_digest": WORKER_MANIFEST_DIGEST,
        "execution_kind": "monorepo-pytest-v1",
        "exit_status": 0,
        "output": "synthetic output",
    }
    report.update(change)
    runner = FakeRunner(json.dumps(report).encode())
    controller = fake_controller(
        provider=FakeProvider('{"files":{"src/module.py":"safe"}}'), runner=runner
    )
    with pytest.raises(DevelopmentError, match="report_invalid"):
        asyncio.run(controller.generate_and_run(generation_request()))


def test_controller_refuses_failed_process_even_when_report_scanner_accepts():
    report = {
        "version": 2,
        "base_commit": BASE_COMMIT,
        "policy_digest": policy().digest,
        "worker_manifest_digest": WORKER_MANIFEST_DIGEST,
        "execution_kind": "monorepo-pytest-v1",
        "exit_status": 5,
        "output": "no tests collected",
    }
    runner = FakeRunner(json.dumps(report).encode())
    controller = fake_controller(
        provider=FakeProvider('{"files":{"src/module.py":"safe"}}'), runner=runner
    )
    with pytest.raises(DevelopmentError, match="candidate_tests_failed"):
        asyncio.run(controller.generate_and_run(generation_request()))
