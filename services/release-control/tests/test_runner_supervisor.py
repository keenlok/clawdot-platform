from __future__ import annotations

import hashlib
import json
import os
import secrets
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import pytest
from chatcore_release_control.runner_bootstrap import (
    RunnerImageManifest,
    compute_runner_closure_digest,
)
from chatcore_release_control.runner_supervisor import (
    CapabilitySnapshot,
    DockerCLIContainerAdapter,
    ExecResult,
    GitHubAppRole,
    GitHubRunnerAppInstallationTokenProvider,
    GitHubRunnerRegistrationAPI,
    HTTPXOIDCRelayTransport,
    InstalledCapabilityProbe,
    RelayRequest,
    SQLiteRunnerOwnershipRegistry,
    SupervisorError,
    SupervisorPolicy,
    WholeRunnerSupervisor,
    load_installed_config,
    main,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

SYNTHETIC_OIDC_TOKEN = jwt.encode(
    {"sub": "synthetic-test"}, secrets.token_bytes(32), algorithm="HS256"
)


def policy() -> SupervisorPolicy:
    return SupervisorPolicy(
        docker_binary="/usr/bin/docker",
        runtime_path="/opt/gvisor/20261026.0/runsc",
        runtime_name="runsc",
        runtime_manifest_digest="a" * 64,
        image="registry.invalid/runner@sha256:" + "b" * 64,
        runner_closure_digest="c" * 64,
        repository_url="https://github.com/owner/repo",
        runner_python_binary="/usr/bin/python3",
        runner_user="10001:10001",
        runner_entrypoint="/opt/p09/runner_bootstrap.py",
        registration_stdin_command=(
            "/usr/bin/python3",
            "/opt/p09/runner_bootstrap.py",
            "receive-registration",
            "--manifest",
            "/opt/p09/runner-image-manifest.json",
            "--expected-closure",
            "c" * 64,
            "--socket",
            "/tmp/p09-registration.sock",
        ),
        cpu_limit=2.0,
        memory_limit_bytes=512 * 1024 * 1024,
        pid_limit=128,
        lifetime_seconds=900,
        scratch_limit_bytes=256 * 1024 * 1024,
        output_limit_bytes=4096,
        egress_network="p09-egress-v1",
        egress_destinations=(
            "github.com:443",
            "broker.invalid:443",
            "pipelines.actions.githubusercontent.com:443",
        ),
        relay_origin="https://broker.invalid",
        oidc_audience="https://broker.invalid/release",
    )


def capability_snapshot(value: SupervisorPolicy) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        runtime_path=value.runtime_path,
        runtime_name=value.runtime_name,
        runtime_manifest_digest=value.runtime_manifest_digest,
        image=value.image,
        runner_closure_digest=value.runner_closure_digest,
        cgroup_v2=True,
        resource_limits=True,
        read_only_root=True,
        disposable_scratch=True,
        egress_enforced=True,
        egress_network=value.egress_network,
        egress_destinations=value.egress_destinations,
        no_host_mounts=True,
        no_host_socket=True,
        no_host_namespaces=True,
        isolated_user_namespace=True,
        no_privilege=True,
    )


class FakeContainers:
    def __init__(self, *, fail_wait: BaseException | None = None):
        self.calls: list[tuple] = []
        self.fail_wait = fail_wait

    def create(self, owner_id: str) -> str:
        self.calls.append(("create", owner_id))
        return "c" * 64

    def configure(self, container_id: str, token: str) -> None:
        self.calls.append(("configure", container_id, token))

    def start(self, container_id: str) -> None:
        self.calls.append(("start", container_id))

    def wait(self, container_id: str, timeout_seconds: int) -> int:
        self.calls.append(("wait", container_id, timeout_seconds))
        if self.fail_wait:
            raise self.fail_wait
        return 0

    def remove(self, owner_id: str) -> None:
        self.calls.append(("remove", owner_id))


class FakeRegistration:
    def __init__(self, *, fail_create: BaseException | None = None):
        self.calls: list[tuple] = []
        self.fail_create = fail_create

    def create_ephemeral(self, owner_id: str) -> str:
        self.calls.append(("create", owner_id))
        if self.fail_create:
            raise self.fail_create
        return "registration-secret-test-only"

    def remove(self, owner_id: str) -> None:
        self.calls.append(("remove", owner_id))


class FakeRelay:
    def __init__(self):
        self.calls: list[tuple] = []

    def post(self, url: str, payload: dict[str, str], bearer_token: str) -> dict[str, str]:
        self.calls.append((url, payload, bearer_token))
        return {"status": "accepted", "receipt_id": payload["receipt_id"]}


class FakeOwnership:
    def __init__(self, owners=()):
        self.owners = list(owners)
        self.calls: list[tuple] = []

    def locked(self):
        return nullcontext()

    def list_owners(self):
        self.calls.append(("list",))
        return tuple(self.owners)

    def reserve(self, owner_id):
        self.calls.append(("reserve", owner_id))
        if self.owners:
            raise SupervisorError("active owner")
        self.owners.append(owner_id)

    def release(self, owner_id):
        self.calls.append(("release", owner_id))
        if owner_id in self.owners:
            self.owners.remove(owner_id)


def run(supervisor: WholeRunnerSupervisor) -> None:
    supervisor.run_once()


def test_lifecycle_uses_one_ephemeral_runner_and_exact_cleanup():
    value = policy()
    containers = FakeContainers()
    registration = FakeRegistration()
    probes: list[SupervisorPolicy] = []
    supervisor = WholeRunnerSupervisor(
        value,
        containers=containers,
        registration=registration,
        ownership=FakeOwnership(),
        capability_probe=lambda item: probes.append(item) or capability_snapshot(item),
        owner_id_factory=lambda: "runner-" + "d" * 32,
    )

    run(supervisor)

    owner_id = "runner-" + "d" * 32
    assert probes == [value]
    assert [call[0] for call in registration.calls] == ["create", "remove"]
    assert [call[0] for call in containers.calls] == [
        "create",
        "start",
        "configure",
        "wait",
        "remove",
    ]
    assert all(owner_id in call for call in registration.calls)
    assert containers.calls[0] == ("create", owner_id)
    assert containers.calls[-1] == ("remove", owner_id)
    assert containers.calls[2][2] == "registration-secret-test-only"


def test_capability_mismatch_fails_before_registration_or_container_creation():
    containers = FakeContainers()
    registration = FakeRegistration()
    supervisor = WholeRunnerSupervisor(
        policy(),
        containers=containers,
        registration=registration,
        ownership=FakeOwnership(),
        capability_probe=lambda value: replace(capability_snapshot(value), runtime_name="runc"),
    )

    with pytest.raises(SupervisorError):
        run(supervisor)

    assert containers.calls == []
    assert registration.calls == []


@pytest.mark.parametrize(
    "field",
    [
        "egress_enforced",
        "no_host_mounts",
        "no_host_socket",
        "no_host_namespaces",
        "isolated_user_namespace",
        "no_privilege",
    ],
)
def test_missing_required_isolation_capability_fails_closed(field):
    containers = FakeContainers()
    registration = FakeRegistration()
    supervisor = WholeRunnerSupervisor(
        policy(),
        containers=containers,
        registration=registration,
        ownership=FakeOwnership(),
        capability_probe=lambda value: replace(capability_snapshot(value), **{field: False}),
    )
    with pytest.raises(SupervisorError):
        run(supervisor)
    assert registration.calls == []
    assert containers.calls == []


def test_egress_destination_mismatch_fails_closed_before_runner_registration():
    registration = FakeRegistration()
    supervisor = WholeRunnerSupervisor(
        policy(),
        containers=FakeContainers(),
        registration=registration,
        ownership=FakeOwnership(),
        capability_probe=lambda value: replace(
            capability_snapshot(value), egress_destinations=("*",)
        ),
    )
    with pytest.raises(SupervisorError):
        supervisor.run_once()
    assert registration.calls == []


def test_wait_error_still_removes_exact_container_and_registration():
    containers = FakeContainers(fail_wait=TimeoutError("private runner diagnostics"))
    registration = FakeRegistration()
    supervisor = WholeRunnerSupervisor(
        policy(),
        containers=containers,
        registration=registration,
        ownership=FakeOwnership(),
        capability_probe=capability_snapshot,
        owner_id_factory=lambda: "runner-" + "e" * 32,
    )

    with pytest.raises(SupervisorError) as caught:
        run(supervisor)

    assert "private runner diagnostics" not in str(caught.value)
    assert containers.calls[-1] == ("remove", "runner-" + "e" * 32)
    assert registration.calls[-1] == ("remove", "runner-" + "e" * 32)


def test_cancelled_runner_still_cleans_up_exact_container_and_registration():
    containers = FakeContainers(fail_wait=KeyboardInterrupt())
    registration = FakeRegistration()
    supervisor = WholeRunnerSupervisor(
        policy(),
        containers=containers,
        registration=registration,
        ownership=FakeOwnership(),
        capability_probe=capability_snapshot,
        owner_id_factory=lambda: "runner-" + "7" * 32,
    )
    with pytest.raises(SupervisorError):
        supervisor.run_once()
    assert containers.calls[-1] == ("remove", "runner-" + "7" * 32)
    assert registration.calls[-1] == ("remove", "runner-" + "7" * 32)


def test_registration_creation_error_still_attempts_exact_owner_cleanup():
    containers = FakeContainers()
    registration = FakeRegistration(fail_create=RuntimeError("synthetic token failure"))
    supervisor = WholeRunnerSupervisor(
        policy(),
        containers=containers,
        registration=registration,
        ownership=FakeOwnership(),
        capability_probe=capability_snapshot,
        owner_id_factory=lambda: "runner-" + "f" * 32,
    )

    with pytest.raises(SupervisorError):
        run(supervisor)

    assert containers.calls == [
        ("create", "runner-" + "f" * 32),
        ("remove", "runner-" + "f" * 32),
    ]
    assert registration.calls[-1] == ("remove", "runner-" + "f" * 32)


def test_durable_owner_registry_reopens_and_recovers_exact_prior_owner(tmp_path):
    database = tmp_path / "private" / "runner-owners.sqlite3"
    owner_id = "runner-" + "9" * 32
    first = SQLiteRunnerOwnershipRegistry(database)
    with first.locked():
        first.reserve(owner_id)

    reopened = SQLiteRunnerOwnershipRegistry(database)
    containers = FakeContainers()
    registration = FakeRegistration()
    supervisor = WholeRunnerSupervisor(
        policy(),
        containers=containers,
        registration=registration,
        ownership=reopened,
        capability_probe=capability_snapshot,
    )

    supervisor.recover()

    assert containers.calls == [("remove", owner_id)]
    assert registration.calls == [("remove", owner_id)]
    assert reopened.list_owners() == ()
    assert database.stat().st_mode & 0o777 == 0o600
    assert database.parent.stat().st_mode & 0o777 == 0o700


def test_durable_owner_registry_lock_excludes_competing_supervisor(tmp_path):
    database = tmp_path / "private" / "runner-owners.sqlite3"
    first = SQLiteRunnerOwnershipRegistry(database)
    second = SQLiteRunnerOwnershipRegistry(database)

    with first.locked():
        with pytest.raises(SupervisorError):
            with second.locked():
                pass


def test_durable_owner_registry_admits_a_sequential_job_after_clean_close(tmp_path):
    owners = iter(("runner-" + "6" * 32, "runner-" + "5" * 32))
    registry = SQLiteRunnerOwnershipRegistry(tmp_path / "private" / "owners.sqlite3")
    containers = FakeContainers()
    registration = FakeRegistration()
    supervisor = WholeRunnerSupervisor(
        policy(),
        containers=containers,
        registration=registration,
        ownership=registry,
        capability_probe=capability_snapshot,
        owner_id_factory=lambda: next(owners),
    )

    supervisor.run_once()
    supervisor.run_once()

    assert registry.list_owners() == ()
    assert [call[1] for call in containers.calls if call[0] == "create"] == [
        "runner-" + "6" * 32,
        "runner-" + "5" * 32,
    ]


def test_recovery_failure_preserves_owner_and_blocks_new_runner(tmp_path):
    owner_id = "runner-" + "8" * 32
    ownership = SQLiteRunnerOwnershipRegistry(tmp_path / "private" / "owners.sqlite3")
    with ownership.locked():
        ownership.reserve(owner_id)
    containers = FakeContainers()

    class FailingRegistration(FakeRegistration):
        def remove(self, current_owner):
            self.calls.append(("remove", current_owner))
            raise RuntimeError("registration service unavailable")

    registration = FailingRegistration()
    supervisor = WholeRunnerSupervisor(
        policy(),
        containers=containers,
        registration=registration,
        ownership=SQLiteRunnerOwnershipRegistry(tmp_path / "private" / "owners.sqlite3"),
        capability_probe=capability_snapshot,
    )

    with pytest.raises(SupervisorError, match="requires recovery"):
        supervisor.run_once()

    assert containers.calls == [("remove", owner_id)]
    assert registration.calls == [("remove", owner_id)]
    assert ownership.list_owners() == (owner_id,)


def test_policy_rejects_unpinned_runtime_image_closure_and_unsafe_limits():
    for changes in (
        {"runtime_name": "runc"},
        {"runtime_path": "relative/runsc"},
        {"docker_binary": "/usr/bin/../bin/docker"},
        {"runtime_manifest_digest": "not-a-digest"},
        {"image": "registry.invalid/runner:latest"},
        {"runner_closure_digest": "bad"},
        {"cpu_limit": 0},
        {"pid_limit": 0},
        {"lifetime_seconds": 0},
        {"egress_destinations": ("*",)},
        {"egress_destinations": ("bad-.host:443",)},
        {"egress_network": "host"},
        {"relay_origin": "http://broker.invalid"},
    ):
        with pytest.raises(ValueError):
            replace(policy(), **changes)


def test_docker_command_has_no_host_mounts_socket_privilege_or_inherited_environment():
    calls = []

    def execute(argv, *, env, timeout, input_data=None):
        calls.append((argv, env, timeout, input_data))
        return ExecResult(returncode=0, stdout="c" * 64, stderr="")

    adapter = DockerCLIContainerAdapter(policy(), execute=execute)
    container_id = adapter.create("runner-" + "a" * 32)

    argv, env, timeout, input_data = calls[0]
    assert container_id == "c" * 64
    assert argv[:2] == ["/usr/bin/docker", "create"]
    assert "--runtime=runsc" in argv
    assert f"--memory={policy().memory_limit_bytes}" in argv
    assert f"--memory-swap={policy().memory_limit_bytes}" in argv
    assert "--network=p09-egress-v1" in argv
    assert "--pid=private" in argv
    assert "--ipc=private" in argv
    assert "--uts=private" in argv
    assert "--cgroupns=private" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges:true" in argv
    assert "--privileged" not in argv
    assert not any(arg.startswith("--mount") or arg.startswith("-v") for arg in argv)
    assert not any("docker.sock" in arg or "host" in arg.lower() for arg in argv)
    assert env == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert timeout == policy().lifetime_seconds
    assert input_data is None


def test_docker_register_token_is_stdin_only_and_docker_output_is_bounded():
    calls = []

    def execute(argv, *, env, timeout, input_data=None):
        calls.append((argv, env, timeout, input_data))
        return ExecResult(returncode=0, stdout="ok", stderr="")

    adapter = DockerCLIContainerAdapter(policy(), execute=execute)
    adapter.configure("c" * 64, "ephemeral-secret")

    argv, env, _, input_data = calls[0]
    assert "ephemeral-secret" not in " ".join(argv)
    assert env == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert input_data == b"ephemeral-secret"
    assert "--env" not in argv


def test_docker_output_over_limit_and_nonzero_are_sanitized():
    def execute(argv, *, env, timeout, input_data=None):
        return ExecResult(returncode=1, stdout="x" * 10000, stderr="sensitive token")

    adapter = DockerCLIContainerAdapter(policy(), execute=execute)
    with pytest.raises(SupervisorError) as caught:
        adapter.start("c" * 64)
    assert "sensitive token" not in str(caught.value)


def test_fixed_relay_only_accepts_bounded_ids_and_uses_installed_origin():
    relay = FakeRelay()
    supervisor = WholeRunnerSupervisor(
        policy(),
        containers=FakeContainers(),
        registration=FakeRegistration(),
        ownership=FakeOwnership(),
        capability_probe=capability_snapshot,
        relay_transport=relay,
        oidc_token_provider=lambda: SYNTHETIC_OIDC_TOKEN,
    )

    result = supervisor.relay(RelayRequest(operation_id="op-" + "1" * 32, receipt_id="2" * 32))

    assert relay.calls == [
        (
            "https://broker.invalid/v1/runner/receipt",
            {"operation_id": "op-" + "1" * 32, "receipt_id": "2" * 32},
            SYNTHETIC_OIDC_TOKEN,
        )
    ]
    assert result == {"status": "accepted", "receipt_id": "2" * 32}
    assert "token" not in repr(result)


@pytest.mark.parametrize(
    "operation_id,receipt_id",
    [("../escape", "a" * 32), ("a" * 32, "https://evil.invalid")],
)
def test_relay_rejects_arbitrary_paths_hosts_and_oversized_ids(operation_id, receipt_id):
    relay = FakeRelay()
    with pytest.raises(ValueError):
        RelayRequest(operation_id=operation_id, receipt_id=receipt_id)
    assert relay.calls == []


def test_docker_adapter_uses_only_fixed_allowlisted_argv_and_rejects_bad_container_ids():
    calls = []

    def execute(argv, *, env, timeout, input_data=None):
        calls.append(argv)
        return ExecResult(returncode=0, stdout="", stderr="")

    adapter = DockerCLIContainerAdapter(policy(), execute=execute)
    adapter.start("c" * 64)
    assert calls == [["/usr/bin/docker", "start", "c" * 64]]
    with pytest.raises(SupervisorError):
        adapter.remove("$(touch /tmp/p09)")


def test_inactive_templates_have_no_install_target_or_credentials():
    from pathlib import Path

    root = (
        Path(__file__).parents[3]
        / "services/release-control/src/chatcore_release_control"
        / "templates"
    )
    files = sorted(root.glob("p09-*"))
    assert len(files) >= 2
    text = "\n".join(path.read_text() for path in files)
    assert "WantedBy=" not in text
    assert "token=" not in text.lower()
    assert "NoNewPrivileges=yes" in text
    assert "INACTIVE EXAMPLE" in text
    assert "-m chatcore_release_control.runner_supervisor run-once" in text


def test_relay_job_template_uses_only_custom_ephemeral_label_and_id_token_permission():
    template = (
        Path(__file__).parents[3]
        / "services/release-control/src/chatcore_release_control"
        / "templates"
        / "p09-runner-relay-job.yml.template"
    )
    text = template.read_text(encoding="utf-8")

    assert "runs-on: chatcore-p09-ephemeral" in text
    assert "id-token: write" in text
    assert "python3 -I /opt/p09/runner_relay.py" in text
    assert "RELEASE_CONTROL_BROKER_ORIGIN" in text
    assert "RELEASE_CONTROL_OIDC_AUDIENCE" in text
    assert "docker" not in text.lower()
    assert "GITHUB_TOKEN" not in text


def test_executor_result_contract_and_owner_ids_are_bounded():
    with pytest.raises(ValueError):
        RelayRequest(operation_id="", receipt_id="a" * 32)
    with pytest.raises(TypeError):
        RelayRequest(operation_id="a" * 32, receipt_id="a" * 32, host="evil.invalid")


def installed_json():
    return {
        "schema_version": 1,
        "docker_binary": "/usr/bin/docker",
        "runtime_path": "/opt/gvisor/20261026.0/runsc",
        "runtime_manifest_path": "/opt/gvisor/20261026.0/runtime-manifest.json",
        "runtime_manifest_sha256": "a" * 64,
        "image": "registry.invalid/runner@sha256:" + "b" * 64,
        "runner_manifest_path": "/opt/chatcore-release-control/runner-image-manifest.json",
        "runner_closure_sha256": "c" * 64,
        "runner_python_binary": "/usr/bin/python3",
        "runner_user": "10001:10001",
        "cpu_limit": 2,
        "memory_limit_bytes": 512 * 1024 * 1024,
        "pid_limit": 128,
        "lifetime_seconds": 900,
        "scratch_limit_bytes": 256 * 1024 * 1024,
        "output_limit_bytes": 4096,
        "egress_network": "p09-egress-v1",
        "egress_destinations": [
            "github.com:443",
            "broker.invalid:443",
            "pipelines.actions.githubusercontent.com:443",
        ],
        "egress_manifest_path": "/etc/chatcore-release-control/p09-egress.json",
        "egress_manifest_sha256": "d" * 64,
        "relay_origin": "https://broker.invalid",
        "oidc_audience": "https://broker.invalid/release",
        "repository_owner": "owner",
        "repository_name": "repo",
        "repository_id": 42,
        "github_app_roles": {
            "publication": 303,
            "broker": 404,
            "runner_registration": 101,
        },
        "github_installation_id": 202,
        "github_app_private_key_path": "/etc/chatcore-release-control/runner-app.pem",
        "ownership_database": "/var/lib/chatcore-release-control/p09/owners.sqlite3",
    }


def test_installed_config_requires_exact_fields_pinned_inputs_and_fixed_paths():
    value = installed_json()
    config = load_installed_config(
        "/etc/chatcore-release-control/p09-policy.json",
        protected_reader=lambda _path, _limit: json.dumps(value).encode(),
    )
    assert config.repository_url == "https://github.com/owner/repo"
    assert config.github_app_id == 101
    assert len(set(config.github_app_roles.values())) == 3
    assert config.policy.registration_stdin_command == (
        "/usr/bin/python3",
        "/opt/p09/runner_bootstrap.py",
        "receive-registration",
        "--manifest",
        "/opt/p09/runner-image-manifest.json",
        "--expected-closure",
        "c" * 64,
        "--socket",
        "/tmp/p09-registration.sock",
    )
    for changes in (
        {"unreviewed": True},
        {"image": "registry.invalid/runner:latest"},
        {"runner_entrypoint": "/bin/sh"},
        {"egress_destinations": ["*"]},
        {"github_app_private_key_path": "relative.pem"},
        {"repository_id": True},
        {
            "github_app_roles": {
                "publication": 101,
                "broker": 404,
                "runner_registration": 101,
            }
        },
        {"github_app_roles": {"publication": 303, "broker": 404}},
    ):
        with pytest.raises(SupervisorError):
            load_installed_config(
                "/etc/chatcore-release-control/p09-policy.json",
                protected_reader=lambda _path, changes=changes, _limit=65536: json.dumps(
                    {**value, **changes}
                ).encode(),
            )


def test_cli_requires_explicit_policy_and_does_not_supply_defaults():
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 2
    with pytest.raises(SystemExit) as caught:
        main(["run-once"])
    assert caught.value.code == 2

    config = load_installed_config(
        "/etc/chatcore-release-control/p09-policy.json",
        protected_reader=lambda _path, _limit: json.dumps(installed_json()).encode(),
    )
    calls = []
    result = main(
        ["run-once", "--policy", "/etc/chatcore-release-control/p09-policy.json"],
        config_loader=lambda path: (
            config if path == "/etc/chatcore-release-control/p09-policy.json" else None
        ),
        service_runner=lambda current: calls.append(current),
    )
    assert result == 0
    assert calls == [config]


def test_cli_fails_closed_on_unexpected_service_adapter_error():
    result = main(
        ["run-once", "--policy", "/etc/chatcore-release-control/p09-policy.json"],
        config_loader=lambda _path: object(),
        service_runner=lambda _config: (_ for _ in ()).throw(RuntimeError("sensitive detail")),
    )
    assert result == 1


def test_github_app_token_provider_requests_single_repo_runner_permission_with_fake_api():
    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url == "https://api.github.com/app/installations/202/access_tokens"
        claims = jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            key.public_key(),
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
        assert claims["iss"] == "101"
        assert json.loads(request.content) == {
            "repository_ids": [42],
            "permissions": {"administration": "write"},
        }
        return httpx.Response(
            201,
            json={
                "token": "ghs-test-installation-token",
                "expires_at": (now + timedelta(minutes=50)).isoformat(),
                "permissions": {"administration": "write"},
                "repositories": [{"id": 42}],
            },
        )

    provider = GitHubRunnerAppInstallationTokenProvider(
        principal_role=GitHubAppRole.RUNNER_REGISTRATION,
        app_roles={"publication": 303, "broker": 404, "runner_registration": 101},
        app_id=101,
        installation_id=202,
        repository_id=42,
        private_key_provider=lambda: pem,
        now=lambda: now,
        transport=httpx.MockTransport(handler),
    )
    assert provider() == "ghs-test-installation-token"
    assert len(requests) == 1


def test_runner_token_provider_rejects_publication_or_broker_principal_role():
    for role in (GitHubAppRole.PUBLICATION, GitHubAppRole.BROKER):
        with pytest.raises(SupervisorError):
            GitHubRunnerAppInstallationTokenProvider(
                principal_role=role,
                app_roles={"publication": 303, "broker": 404, "runner_registration": 101},
                app_id=101,
                installation_id=202,
                repository_id=42,
                private_key_provider=lambda: b"unused",
                transport=httpx.MockTransport(lambda _request: pytest.fail("API must not run")),
            )
    with pytest.raises(SupervisorError):
        GitHubRunnerAppInstallationTokenProvider(
            principal_role=GitHubAppRole.RUNNER_REGISTRATION,
            app_roles={"publication": 303, "broker": 404, "runner_registration": 101},
            app_id=303,
            installation_id=202,
            repository_id=42,
            private_key_provider=lambda: b"unused",
        )


def test_repository_registration_api_create_and_remove_are_fixed_and_exact():
    owner_id = "runner-" + "4" * 32
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "api.github.com"
        assert request.headers["Authorization"] == "Bearer ghs-supervisor-only"
        if request.method == "POST":
            assert request.url.path == "/repos/owner/repo/actions/runners/registration-token"
            return httpx.Response(
                201,
                json={
                    "token": "runner-registration-ephemeral",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                },
            )
        if request.method == "GET":
            assert request.url.path == "/repos/owner/repo/actions/runners"
            return httpx.Response(
                200,
                json={"total_count": 1, "runners": [{"id": 77, "name": owner_id}]},
            )
        assert request.method == "DELETE"
        assert request.url.path.endswith("/actions/runners/77")
        return httpx.Response(204)

    api = GitHubRunnerRegistrationAPI(
        owner="owner",
        repository="repo",
        repository_id=42,
        installation_token_provider=lambda: "ghs-supervisor-only",
        transport=httpx.MockTransport(handler),
    )
    token = api.create_ephemeral(owner_id)
    assert token == "runner-registration-ephemeral"
    api.remove(owner_id)
    assert [call.method for call in calls] == ["POST", "GET", "DELETE"]
    assert token not in repr(api)
    api.close()


def test_registration_api_never_deletes_foreign_or_ambiguous_runners():
    owner_id = "runner-" + "3" * 32
    for runners in (
        [{"id": 1, "name": "another-runner"}],
        [{"id": 1, "name": owner_id}, {"id": 2, "name": owner_id}],
    ):
        calls = []

        def handler(request, runners=runners, request_log=calls):
            request_log.append(request.method)
            return httpx.Response(200, json={"total_count": len(runners), "runners": runners})

        api = GitHubRunnerRegistrationAPI(
            owner="owner",
            repository="repo",
            repository_id=42,
            installation_token_provider=lambda: "ghs-supervisor-only",
            transport=httpx.MockTransport(handler),
        )
        if len(runners) > 1:
            with pytest.raises(SupervisorError):
                api.remove(owner_id)
        else:
            api.remove(owner_id)
        assert "DELETE" not in calls
        api.close()


def test_fixed_origin_httpx_relay_sends_only_ids_and_oidc_bearer():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url == "https://broker.invalid/v1/runner/receipt"
        assert request.headers["Authorization"] == (f"Bearer {SYNTHETIC_OIDC_TOKEN}")
        assert json.loads(request.content) == {"operation_id": "op_abc", "receipt_id": "rcpt_123"}
        return httpx.Response(200, json={"status": "accepted", "receipt_id": "rcpt_123"})

    relay = HTTPXOIDCRelayTransport(
        origin="https://broker.invalid", transport=httpx.MockTransport(handler)
    )
    result = relay.post(
        "https://broker.invalid/v1/runner/receipt",
        {"operation_id": "op_abc", "receipt_id": "rcpt_123"},
        SYNTHETIC_OIDC_TOKEN,
    )
    assert result == {"status": "accepted", "receipt_id": "rcpt_123"}
    assert len(requests) == 1
    assert "Cookie" not in requests[0].headers
    relay.close()


def test_fixed_origin_relay_rejects_redirect_and_oversized_body():
    for response in (
        httpx.Response(302, headers={"Location": "https://evil.invalid"}),
        httpx.Response(200, content=b"x" * 8192),
    ):
        relay = HTTPXOIDCRelayTransport(
            origin="https://broker.invalid",
            transport=httpx.MockTransport(lambda _request, response=response: response),
        )
        with pytest.raises(SupervisorError):
            relay.post(
                "https://broker.invalid/v1/runner/receipt",
                {"operation_id": "op_abc", "receipt_id": "rcpt_123"},
                SYNTHETIC_OIDC_TOKEN,
            )
        relay.close()


@pytest.mark.parametrize("token", ["arbitrary-bearer-value", "a.b." + "c" * 8193])
def test_fixed_relay_rejects_non_jwt_or_oversized_bearer_before_transport(token):
    relay = FakeRelay()
    supervisor = WholeRunnerSupervisor(
        policy(),
        containers=FakeContainers(),
        registration=FakeRegistration(),
        ownership=FakeOwnership(),
        capability_probe=capability_snapshot,
        relay_transport=relay,
        oidc_token_provider=lambda: token,
    )
    with pytest.raises(SupervisorError):
        supervisor.relay(RelayRequest(operation_id="op-" + "1" * 32, receipt_id="2" * 32))
    assert relay.calls == []


def test_registration_http_client_does_not_reuse_response_cookies():
    owner_id = "runner-" + "4" * 32
    requests = []

    def handler(request):
        requests.append(request)
        assert "Cookie" not in request.headers
        if request.method == "POST":
            return httpx.Response(
                201,
                headers={"Set-Cookie": "session=not-for-reuse; Path=/"},
                json={
                    "token": "runner-registration-ephemeral",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                },
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={"total_count": 1, "runners": [{"id": 77, "name": owner_id}]},
        )

    api = GitHubRunnerRegistrationAPI(
        owner="owner",
        repository="repo",
        repository_id=42,
        installation_token_provider=lambda: "ghs-supervisor-only",
        transport=httpx.MockTransport(handler),
    )
    api.create_ephemeral(owner_id)
    api.remove(owner_id)
    assert len(requests) == 3
    api.close()


def test_fixed_origin_relay_does_not_reuse_response_cookies():
    requests = []

    def handler(request):
        requests.append(request)
        assert "Cookie" not in request.headers
        return httpx.Response(
            200,
            headers={"Set-Cookie": "session=not-for-reuse; Path=/"},
            json={"status": "accepted", "receipt_id": "rcpt_123"},
        )

    relay = HTTPXOIDCRelayTransport(
        origin="https://broker.invalid", transport=httpx.MockTransport(handler)
    )
    for _ in range(2):
        relay.post(
            "https://broker.invalid/v1/runner/receipt",
            {"operation_id": "op_abc", "receipt_id": "rcpt_123"},
            SYNTHETIC_OIDC_TOKEN,
        )
    assert len(requests) == 2
    relay.close()


def test_installed_capability_probe_checks_manifests_runtime_image_and_network_with_fakes(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    runsc_path = runtime_root / "runsc"
    runsc_path.write_bytes(b"synthetic runsc package file")
    runsc_path.chmod(0o555)
    runtime_data = json.dumps(
        {
            "schema_version": 1,
            "release": "test-only",
            "archive_sha512": "e" * 128,
            "files": {"runsc": hashlib.sha256(runsc_path.read_bytes()).hexdigest()},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    runner_files = {
        "config.sh": hashlib.sha256(b"test config source").hexdigest(),
        "run.sh": hashlib.sha256(b"test run source").hexdigest(),
        "bin/Runner.Listener": hashlib.sha256(b"test runner source").hexdigest(),
    }
    bootstrap_sha256 = hashlib.sha256(
        (
            Path(__file__).parents[1] / "src/chatcore_release_control/runner_bootstrap.py"
        ).read_bytes()
    ).hexdigest()
    relay_helper_sha256 = hashlib.sha256(
        (Path(__file__).parents[1] / "src/chatcore_release_control/runner_relay.py").read_bytes()
    ).hexdigest()
    runner_fields = {
        "schema_version": 1,
        "base_image": "python:3.12-slim@sha256:" + "1" * 64,
        "runner_archive_sha256": "2" * 64,
        "bootstrap_sha256": bootstrap_sha256,
        "relay_helper_sha256": relay_helper_sha256,
        "runner_closure_sha256": "0" * 64,
        "runner_root": "/opt/p09/runner-files",
        "runner_binary": "config.sh",
        "runner_run_script": "run.sh",
        "runner_version": "0.0.0",
        "runner_files": runner_files,
        "registration_protocol": "actions-runner-config-prompt-v1",
    }
    runner_fields["runner_closure_sha256"] = compute_runner_closure_digest(runner_fields)
    runner_data = json.dumps(runner_fields, sort_keys=True, separators=(",", ":")).encode()
    image_manifest = RunnerImageManifest.from_mapping(runner_fields)

    egress_data = json.dumps(
        {
            "schema_version": 1,
            "network": "p09-egress-v1",
            "destinations": [
                "github.com:443",
                "broker.invalid:443",
                "pipelines.actions.githubusercontent.com:443",
            ],
            "egress_enforced": True,
            "no_host_mounts": True,
            "no_host_socket": True,
            "no_host_namespaces": True,
            "isolated_user_namespace": True,
            "no_privilege": True,
            "resource_limits": True,
            "read_only_root": True,
            "disposable_scratch": True,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    configured = installed_json()
    configured["runtime_path"] = str(runsc_path)
    configured["runtime_manifest_sha256"] = hashlib.sha256(runtime_data).hexdigest()
    configured["runner_closure_sha256"] = image_manifest.runner_closure_sha256
    configured["egress_manifest_sha256"] = hashlib.sha256(egress_data).hexdigest()
    config = load_installed_config(
        "/etc/chatcore-release-control/p09-policy.json",
        protected_reader=lambda _path, _limit: json.dumps(configured).encode(),
    )
    contents = {
        config.runtime_manifest_path: runtime_data,
        str(runsc_path): runsc_path.read_bytes(),
        config.runner_manifest_path: runner_data,
        config.egress_manifest_path: egress_data,
    }
    commands = []

    def execute(argv, *, env, timeout, input_data=None):
        commands.append((argv, env, timeout, input_data))
        if argv[1:3] == ["info", "--format"]:
            value = {
                "Runtimes": {"runsc": {"path": str(runsc_path)}},
                "CgroupVersion": "2",
                "SecurityOptions": ["name=seccomp", "name=userns"],
            }
        elif argv[1:4] == ["image", "inspect", "--format"]:
            value = {
                "RepoDigests": [config.policy.image],
                "Config": {
                    "User": "10001:10001",
                    "Entrypoint": ["/usr/bin/python3", "/opt/p09/runner_bootstrap.py"],
                    "Labels": {
                        "io.chatcore.runner-closure-sha256": image_manifest.runner_closure_sha256,
                        "io.chatcore.runner-version": image_manifest.runner_version,
                    },
                    "Volumes": None,
                    "Env": [],
                },
            }
        else:
            value = {
                "Name": config.policy.egress_network,
                "Driver": "bridge",
                "Labels": {"io.chatcore.p09-egress-sha256": config.egress_manifest_sha256},
            }
        return ExecResult(0, json.dumps(value), "")

    probe = InstalledCapabilityProbe(
        config,
        execute=execute,
        protected_reader=lambda path, _limit: contents[str(path)],
        host_reader=lambda _path: b"cpu memory pids",
        expected_owner_uid=os.getuid(),
    )
    snapshot = probe(config.policy)

    assert snapshot.runtime_path == str(runsc_path)
    assert snapshot.image == config.policy.image
    assert snapshot.egress_destinations == config.policy.egress_destinations
    assert len(commands) == 3
    assert all(env == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"} for _, env, _, _ in commands)
    assert all(timeout == 8 and input_data is None for _, _, timeout, input_data in commands)


def test_installed_capability_probe_rejects_daemon_without_user_namespace(tmp_path):
    # Reuse the full installed-manifest fixture, changing only daemon facts.
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    runsc_path = runtime_root / "runsc"
    runsc_path.write_bytes(b"synthetic runsc package file")
    runsc_path.chmod(0o555)
    runtime_data = json.dumps(
        {
            "schema_version": 1,
            "release": "test-only",
            "archive_sha512": "e" * 128,
            "files": {"runsc": hashlib.sha256(runsc_path.read_bytes()).hexdigest()},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    runner_files = {
        "config.sh": hashlib.sha256(b"test config source").hexdigest(),
        "run.sh": hashlib.sha256(b"test run source").hexdigest(),
        "bin/Runner.Listener": hashlib.sha256(b"test runner source").hexdigest(),
    }
    bootstrap_sha256 = hashlib.sha256(
        (
            Path(__file__).parents[1] / "src/chatcore_release_control/runner_bootstrap.py"
        ).read_bytes()
    ).hexdigest()
    relay_helper_sha256 = hashlib.sha256(
        (Path(__file__).parents[1] / "src/chatcore_release_control/runner_relay.py").read_bytes()
    ).hexdigest()
    runner_fields = {
        "schema_version": 1,
        "base_image": "python:3.12-slim@sha256:" + "1" * 64,
        "runner_archive_sha256": "2" * 64,
        "bootstrap_sha256": bootstrap_sha256,
        "relay_helper_sha256": relay_helper_sha256,
        "runner_closure_sha256": "0" * 64,
        "runner_root": "/opt/p09/runner-files",
        "runner_binary": "config.sh",
        "runner_run_script": "run.sh",
        "runner_version": "0.0.0",
        "runner_files": runner_files,
        "registration_protocol": "actions-runner-config-prompt-v1",
    }
    runner_fields["runner_closure_sha256"] = compute_runner_closure_digest(runner_fields)
    runner_data = json.dumps(runner_fields, sort_keys=True, separators=(",", ":")).encode()
    image_manifest = RunnerImageManifest.from_mapping(runner_fields)
    egress_data = json.dumps(
        {
            "schema_version": 1,
            "network": "p09-egress-v1",
            "destinations": [
                "github.com:443",
                "broker.invalid:443",
                "pipelines.actions.githubusercontent.com:443",
            ],
            "egress_enforced": True,
            "no_host_mounts": True,
            "no_host_socket": True,
            "no_host_namespaces": True,
            "isolated_user_namespace": True,
            "no_privilege": True,
            "resource_limits": True,
            "read_only_root": True,
            "disposable_scratch": True,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    configured = installed_json()
    configured["runtime_path"] = str(runsc_path)
    configured["runtime_manifest_sha256"] = hashlib.sha256(runtime_data).hexdigest()
    configured["runner_closure_sha256"] = image_manifest.runner_closure_sha256
    configured["egress_manifest_sha256"] = hashlib.sha256(egress_data).hexdigest()
    config = load_installed_config(
        "/etc/chatcore-release-control/p09-policy.json",
        protected_reader=lambda _path, _limit: json.dumps(configured).encode(),
    )
    contents = {
        config.runtime_manifest_path: runtime_data,
        str(runsc_path): runsc_path.read_bytes(),
        config.runner_manifest_path: runner_data,
        config.egress_manifest_path: egress_data,
    }

    def execute(argv, *, env, timeout, input_data=None):
        if argv[1:3] == ["info", "--format"]:
            value = {
                "Runtimes": {"runsc": {"path": str(runsc_path)}},
                "CgroupVersion": "2",
                "SecurityOptions": ["name=seccomp"],
            }
        elif argv[1:4] == ["image", "inspect", "--format"]:
            value = {
                "RepoDigests": [config.policy.image],
                "Config": {
                    "User": "10001:10001",
                    "Entrypoint": ["/usr/bin/python3", "/opt/p09/runner_bootstrap.py"],
                    "Labels": {
                        "io.chatcore.runner-closure-sha256": image_manifest.runner_closure_sha256,
                        "io.chatcore.runner-version": image_manifest.runner_version,
                    },
                    "Volumes": None,
                    "Env": [],
                },
            }
        else:
            value = {
                "Name": config.policy.egress_network,
                "Driver": "bridge",
                "Labels": {"io.chatcore.p09-egress-sha256": config.egress_manifest_sha256},
            }
        return ExecResult(0, json.dumps(value), "")

    probe = InstalledCapabilityProbe(
        config,
        execute=execute,
        protected_reader=lambda path, _limit: contents[str(path)],
        host_reader=lambda _path: b"cpu memory pids",
        expected_owner_uid=os.getuid(),
    )
    with pytest.raises(SupervisorError):
        probe(config.policy)
