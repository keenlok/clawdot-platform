from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import signal
import subprocess
import tempfile
from pathlib import Path

import pytest
from chatcore.events import ChatRef
from chatcore_extensions.models import Artifact, ExampleCase, RunResult
from chatcore_extensions.runtime import verify_runsc_manifest
from chatcore_extensions.sandbox import DockerSandbox, SandboxPolicy
from chatcore_extensions.worker import WorkerClient

pytestmark = pytest.mark.sandbox_integration


@pytest.fixture(scope="module")
def image_id() -> str:
    if os.environ.get("SB_EXTENSION_SANDBOX_TESTS") != "1":
        pytest.skip("set SB_EXTENSION_SANDBOX_TESTS=1 to run real Docker sandbox tests")
    reference = os.environ.get("SB_EXTENSION_IMAGE", "python:3.12-slim")
    result = subprocess.run(
        ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
        capture_output=True,
        timeout=10,
        check=False,
        text=True,
    )
    image_id = result.stdout.strip()
    if result.returncode != 0 or not image_id.startswith("sha256:") or len(image_id) != 71:
        pytest.fail("configured local sandbox image is unavailable")
    return image_id


def _artifact(source: str) -> Artifact:
    return Artifact("sandbox adversarial probe", source, (ExampleCase({}, None),))


def _selected_policy() -> SandboxPolicy:
    runtime = os.environ.get("SB_EXTENSION_TEST_RUNTIME", "runc")
    if runtime == "runc":
        return SandboxPolicy()
    if runtime != "runsc":
        pytest.fail("SB_EXTENSION_TEST_RUNTIME must be runc or runsc")
    manifest_path = os.environ.get("SB_EXTENSION_TEST_MANIFEST")
    if not manifest_path:
        pytest.fail("SB_EXTENSION_TEST_MANIFEST is required for runsc tests")
    identity = verify_runsc_manifest(manifest_path)
    return SandboxPolicy(
        runtime="runsc",
        runtime_identity=identity.manifest_digest,
        runtime_version=identity.version,
    )


def _sandbox(image_id: str, **kwargs) -> DockerSandbox:
    policy = kwargs.pop("policy", _selected_policy())
    if policy.runtime == "runsc":
        kwargs.setdefault("owner_id", "a" * 32)
    return DockerSandbox(image_id, policy=policy, **kwargs)


class _InspectingSandbox(DockerSandbox):
    inspect: dict[str, object] | None = None

    async def _remove(self, name: str) -> bool:
        proc = await __import__("asyncio").create_subprocess_exec(
            self.docker,
            "inspect",
            "--format",
            "{{json .}}",
            name,
            stdout=__import__("asyncio").subprocess.PIPE,
            stderr=__import__("asyncio").subprocess.DEVNULL,
        )
        output, _ = await proc.communicate()
        if proc.returncode == 0:
            self.inspect = json.loads(output)
        return await super()._remove(name)


class _PidsObservedSandbox(DockerSandbox):
    container_name: str | None = None

    def create_argv(self, name: str) -> list[str]:
        self.container_name = name
        return super().create_argv(name)


async def _container_pids_cgroup(sandbox: _PidsObservedSandbox) -> Path | None:
    if sandbox.container_name is None:
        return None
    inspect = await asyncio.create_subprocess_exec(
        sandbox.docker,
        "inspect",
        "--format",
        "{{.Id}} {{.State.Pid}}",
        sandbox.container_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(inspect.communicate(), timeout=1)
    except TimeoutError:
        inspect.kill()
        await asyncio.wait_for(inspect.wait(), timeout=1)
        return None
    if inspect.returncode != 0:
        return None
    fields = output.decode("ascii", errors="ignore").split()
    if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{64}", fields[0]) or not fields[1].isdigit():
        return None
    container_id = fields[0]
    host_pid = int(fields[1])
    if host_pid <= 0:
        return None
    try:
        cgroup_contents = await asyncio.to_thread(Path(f"/proc/{host_pid}/cgroup").read_text)
    except OSError:
        return None
    cgroup_lines = cgroup_contents.splitlines()
    relative = next((line[3:] for line in cgroup_lines if line.startswith("0::/")), None)
    if relative is None or container_id not in relative:
        return None
    cgroup = Path("/sys/fs/cgroup").joinpath(relative.lstrip("/"))
    return cgroup if (cgroup / "pids.max").is_file() else None


def _pids_cgroup_sample(cgroup: Path) -> tuple[int, int, int]:
    current = int((cgroup / "pids.current").read_text().strip())
    limit = int((cgroup / "pids.max").read_text().strip())
    events = dict(line.split() for line in (cgroup / "pids.events").read_text().splitlines())
    return current, limit, int(events["max"])


async def _docker_container_absent(container_id: str) -> bool:
    listed = await asyncio.create_subprocess_exec(
        "docker",
        "ps",
        "--no-trunc",
        "-aq",
        "--filter",
        f"id={container_id}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(listed.communicate(), timeout=2)
    except TimeoutError:
        listed.kill()
        await asyncio.wait_for(listed.wait(), timeout=1)
        return False
    return listed.returncode == 0 and container_id not in output.decode("ascii").splitlines()


async def _run_fork_probe_with_pids_evidence(
    sandbox: _PidsObservedSandbox, artifact: Artifact
) -> tuple[RunResult, int, int, int]:
    task = asyncio.create_task(sandbox.run(artifact, {}))
    cgroup: Path | None = None
    peak = 0
    limit: int | None = None
    first_max_event: int | None = None
    final_max_event: int | None = None
    deadline = asyncio.get_running_loop().time() + sandbox.policy.timeout_seconds + 10
    while not task.done() and asyncio.get_running_loop().time() < deadline:
        if cgroup is None:
            cgroup = await _container_pids_cgroup(sandbox)
        if cgroup is not None:
            try:
                current, observed_limit, max_event = _pids_cgroup_sample(cgroup)
            except (OSError, KeyError, ValueError):
                current = observed_limit = max_event = -1
            if current >= 0:
                peak = max(peak, current)
                limit = observed_limit
                if first_max_event is None:
                    first_max_event = max_event
                final_max_event = max_event
        await asyncio.sleep(0.005)
    result = await asyncio.wait_for(task, timeout=1)
    return result, peak, limit or 0, (final_max_event or 0) - (first_max_event or 0)


@pytest.mark.asyncio
async def test_real_container_denies_host_paths_network_and_writes(
    image_id: str, monkeypatch
) -> None:
    host_secret = "host-secret-value-92f1"
    monkeypatch.setenv("SB_EXTENSION_HOST_ONLY_SECRET", host_secret)
    with tempfile.NamedTemporaryFile(prefix="sb-ext-sentinel-") as sentinel:
        sentinel.write(b"HOST_ONLY_SENTINEL_98b0")
        sentinel.flush()
        sentinel_path = sentinel.name
        source = (
            "def run(arguments):\n"
            f" target={sentinel_path!r}\n"
            " try: value=open(target).read(); return 'HOST_ONLY_SENTINEL_98b0' in value\n"
            " except OSError: return False"
        )
        relative = (
            "def run(arguments):\n"
            f" target={'../../..' + sentinel_path!r}\n"
            " try: value=open(target).read(); return 'HOST_ONLY_SENTINEL_98b0' in value\n"
            " except OSError: return False"
        )
        symlink = (
            "def run(arguments):\\n"
            " import os\\n"
            f" target={sentinel_path!r}\\n"
            " try: os.symlink(target, '/tmp/link'); return open('/tmp/link').read()\\n"
            " except OSError: return None"
        ).replace("\\n", "\n")
        sandbox = _sandbox(image_id)
        app_link = (
            "def run(arguments):\n"
            " import os\n"
            " try: os.symlink('/app', '/tmp/app-link'); "
            "open('/tmp/app-link/write','w').write('x'); return True\n"
            " except OSError: return False"
        )
        env_probe = (
            "def run(arguments): return "
            "__import__('os').environ.get('SB_EXTENSION_HOST_ONLY_SECRET')"
        )
        for code, expected in (
            (source, False),
            (relative, False),
            (symlink, None),
            (app_link, False),
            (env_probe, None),
        ):
            result = await sandbox.run(_artifact(code), {})
            assert result.status == "ok" and result.value == expected

    probes = [
        (
            "def run(arguments):\n import os\n return ["
            "os.path.exists('/var/run/docker.sock'), os.path.exists('/run/secrets'), "
            "os.path.exists('/Users/example-owner')]",
            [False, False, False],
        ),
        (
            "def run(arguments):\n import socket\n try: "
            "socket.create_connection(('1.1.1.1', 53), timeout=0.5); return True\n"
            " except OSError: return False",
            False,
        ),
        (
            "def run(arguments):\n import socket\n try: "
            "socket.create_connection(('2606:4700:4700::1111', 53), timeout=0.5); return True\n"
            " except OSError: return False",
            False,
        ),
        (
            "def run(arguments):\n import socket\n try: "
            "socket.getaddrinfo('example.com', 80); return True\n"
            " except OSError: return False",
            False,
        ),
        (
            "def run(arguments):\n try: open('/etc/extension-test','w').write('x'); return True\n"
            " except OSError: return False",
            False,
        ),
    ]
    for code, expected in probes:
        result = await sandbox.run(_artifact(code), {})
        assert result.status == "ok" and result.value == expected

    first = await sandbox.run(
        _artifact("def run(arguments): open('/tmp/job-data','w').write('secret'); return True"), {}
    )
    second = await sandbox.run(
        _artifact("def run(arguments): return __import__('os').path.exists('/tmp/job-data')"), {}
    )
    assert first.status == "ok" and first.value is True
    assert second.status == "ok" and second.value is False


@pytest.mark.asyncio
async def test_real_container_limits_recovery_and_effective_config(image_id: str) -> None:
    policy = _selected_policy()
    owner_id = "a" * 32 if policy.runtime == "runsc" else None
    sandbox = _InspectingSandbox(image_id, policy=policy, owner_id=owner_id)
    clean_code = "def run(arguments): return 42"
    clean = await sandbox.run(_artifact(clean_code), {})
    assert clean.status == "ok" and clean.value == 42
    assert sandbox.inspect is not None
    inspect = sandbox.inspect
    config = inspect["Config"]
    host_config = inspect["HostConfig"]
    assert config["User"] == "65534:65534"
    assert host_config["NetworkMode"] == "none"
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["Runtime"] == policy.runtime
    assert host_config["Privileged"] is False
    assert host_config["PidsLimit"] == 32
    assert host_config["Memory"] == 128 * 1024 * 1024
    assert host_config["MemorySwap"] == 128 * 1024 * 1024
    assert host_config["NanoCpus"] == 500_000_000
    assert host_config["PidMode"] in ("", None)
    assert host_config["Tmpfs"] == {"/tmp": "rw,noexec,nosuid,size=16m"}
    assert "no-new-privileges:true" in host_config["SecurityOpt"]
    assert config["Entrypoint"] == ["python"]
    assert config["Cmd"][:3] == ["-I", "-B", "-c"]
    assert config["WorkingDir"] == "/tmp"
    assert all("SB_EXTENSION_HOST_ONLY_SECRET" not in value for value in config["Env"])
    assert host_config["Binds"] in (None, [])
    assert host_config["CapDrop"] == ["ALL"]
    assert inspect["Mounts"] == []

    probes = [
        ("def run(arguments):\n while True: pass", "timeout", None),
        (
            "def run(arguments):\n import os\n while True: os.write(1,b'x'*4096)",
            "output_limit",
            None,
        ),
        (
            "def run(arguments):\n return bytearray(256*1024*1024).hex()",
            "resource_limit",
            None,
        ),
    ]
    for code, expected_status, expected_value in probes:
        result = await sandbox.run(_artifact(code), {})
        assert result.status == expected_status
        assert result.value == expected_value
        clean = await sandbox.run(_artifact(clean_code), {})
        assert clean.status == "ok" and clean.value == 42

    fork_code = (
        "def run(arguments):\n import os,time\n time.sleep(0.5)\n p=[]\n"
        " for _ in range(100):\n  try: pid=os.fork()\n  except OSError: break\n"
        "  if pid==0: os._exit(0)\n  p.append(pid)\n"
        " [os.waitpid(pid,0) for pid in p]\n return len(p)<100"
    )
    if policy.runtime == "runsc":
        pid_sandbox = _PidsObservedSandbox(image_id, policy=policy, owner_id=owner_id)
        (
            fork_result,
            peak,
            observed_limit,
            max_event_delta,
        ) = await _run_fork_probe_with_pids_evidence(pid_sandbox, _artifact(fork_code))
        assert observed_limit == policy.pids
        assert peak <= policy.pids
        if fork_result.status == "ok":
            assert fork_result.value is True
        else:
            # This pinned gVisor release can terminate the container with exit 2 when
            # the host pids controller refuses a Sentry helper clone. Accept that
            # denial only when this exact container's cgroup records max-event evidence.
            assert fork_result.status == "execution_error"
            assert max_event_delta > 0
        clean = await pid_sandbox.run(_artifact(clean_code), {})
        assert clean.status == "ok" and clean.value == 42
        await pid_sandbox.aclose()
    else:
        fork_result = await sandbox.run(_artifact(fork_code), {})
        assert fork_result.status == "ok" and fork_result.value is True
        clean = await sandbox.run(_artifact(clean_code), {})
        assert clean.status == "ok" and clean.value == 42

    tmp_fill = await sandbox.run(
        _artifact(
            "def run(arguments):\n f=open('/tmp/fill','wb'); n=0\n"
            " try:\n  while True: f.write(b'x'*1048576); f.flush(); n+=1048576\n"
            " except OSError: return n"
        ),
        {},
    )
    assert tmp_fill.status == "ok" and 0 < tmp_fill.value <= 16 * 1024 * 1024
    await sandbox.aclose()


@pytest.mark.asyncio
async def test_runsc_worker_recovers_killed_owned_job_before_health(image_id: str) -> None:
    if os.environ.get("SB_EXTENSION_TEST_RUNTIME") != "runsc":
        pytest.skip("set SB_EXTENSION_TEST_RUNTIME=runsc for owned-container recovery test")

    import sys

    runtime_manifest = os.environ["SB_EXTENSION_TEST_MANIFEST"]
    owner_id = secrets.token_hex(16)
    token = secrets.token_urlsafe(48)
    image_reference = os.environ.get("SB_EXTENSION_IMAGE", "python:3.12-slim")
    socket_dir = Path(tempfile.mkdtemp(prefix="ext-recovery-", dir="/tmp"))
    await asyncio.to_thread(socket_dir.chmod, 0o700)
    token_path = socket_dir / "token"
    owner_path = socket_dir / "owner-id"
    socket_path = socket_dir / "worker.sock"
    token_path.write_text(token)
    owner_path.write_text(owner_id)
    token_path.chmod(0o600)
    owner_path.chmod(0o600)
    worker_command = [
        sys.executable,
        "-c",
        "from chatcore_extensions.worker import main; main()",
        "--socket",
        str(socket_path),
        "--token-file",
        str(token_path),
        "--owner-id-file",
        str(owner_path),
        "--image",
        image_reference,
        "--runtime",
        "runsc",
        "--runtime-manifest",
        runtime_manifest,
    ]
    env = dict(os.environ)
    worker = None
    restarted = None
    client = None
    foreign_id = None
    try:
        worker = await asyncio.create_subprocess_exec(
            *worker_command,
            cwd=Path(__file__).parents[4],
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(500):
            if socket_path.exists() or worker.returncode is not None:
                break
            await asyncio.sleep(0.02)
        assert worker.returncode is None and socket_path.exists()
        client = WorkerClient(socket_path, token)
        await client.health()

        foreign_name = "sb-ext-" + secrets.token_hex(16)
        created = await asyncio.create_subprocess_exec(
            "docker",
            "create",
            "--name",
            foreign_name,
            "--label",
            "chatcore.extensions.owner=" + secrets.token_hex(16),
            "--runtime",
            "runsc",
            image_id,
            "python",
            "-c",
            "pass",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        foreign_output, _ = await asyncio.wait_for(created.communicate(), timeout=10)
        assert created.returncode == 0
        foreign_id = foreign_output.decode().strip()

        long_artifact = _artifact("def run(arguments):\n while True: pass")
        running = asyncio.create_task(
            client.run(
                chat=ChatRef("tg", "123"),
                run_id="crash-test",
                artifact=long_artifact,
                arguments={},
            )
        )
        owned_ids = []
        for _ in range(300):
            listed = await asyncio.create_subprocess_exec(
                "docker",
                "ps",
                "--no-trunc",
                "-aq",
                "--filter",
                "label=chatcore.extensions.owner=" + owner_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output, _ = await asyncio.wait_for(listed.communicate(), timeout=5)
            owned_ids = output.decode().splitlines()
            if owned_ids:
                break
            await asyncio.sleep(0.02)
        assert len(owned_ids) == 1 and len(owned_ids[0]) == 64
        worker.kill()
        await asyncio.wait_for(worker.wait(), timeout=3)
        await client.aclose()
        client = None
        await asyncio.gather(running, return_exceptions=True)
        assert socket_path.exists(), "SIGKILL should leave the stale socket entry for recovery"

        restarted = await asyncio.create_subprocess_exec(
            *worker_command,
            cwd=Path(__file__).parents[4],
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        client = WorkerClient(socket_path, token)
        health = None
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            assert restarted.returncode is None, "restarted worker exited before readiness"
            try:
                health = await asyncio.wait_for(client.health(), timeout=2)
            except Exception:
                health = None
            else:
                break
            await asyncio.sleep(0.02)
        assert restarted.returncode is None and socket_path.exists()
        assert health is not None, "worker did not become authenticated-ready before deadline"
        assert await asyncio.to_thread(token_path.read_text) == token
        assert await _docker_container_absent(owned_ids[0]), (
            "owned orphan must be absent after authenticated health succeeds"
        )
        assert health["runtime"] == "runsc"

        removed = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            owned_ids[0],
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert await asyncio.wait_for(removed.wait(), timeout=5) != 0
        foreign = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            foreign_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert await asyncio.wait_for(foreign.wait(), timeout=5) == 0

        clean = Artifact(
            "recovery",
            "def run(arguments): return arguments['value'] * 2",
            (ExampleCase({"value": 21}, 42),),
        )
        result = await client.run(
            chat=ChatRef("tg", "123"),
            run_id="after-recovery",
            artifact=clean,
            arguments={"value": 21},
        )
        assert result.status == "ok" and result.value == 42
        remaining = await asyncio.create_subprocess_exec(
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=chatcore.extensions.owner=" + owner_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        remaining_output, _ = await asyncio.wait_for(remaining.communicate(), timeout=5)
        assert remaining.returncode == 0 and not remaining_output.strip()
    finally:
        if client is not None:
            await client.aclose()
        for process in (worker, restarted):
            if process is not None and process.returncode is None:
                process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=3)
        if foreign_id:
            cleanup = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                foreign_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(cleanup.wait(), timeout=10)
        owned = await asyncio.create_subprocess_exec(
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=chatcore.extensions.owner=" + owner_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        owned_output, _ = await asyncio.wait_for(owned.communicate(), timeout=5)
        for container_id in owned_output.decode().splitlines():
            cleanup = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                container_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(cleanup.wait(), timeout=10)
        await asyncio.to_thread(token_path.unlink, missing_ok=True)
        await asyncio.to_thread(owner_path.unlink, missing_ok=True)
        await asyncio.to_thread(socket_path.unlink, missing_ok=True)
        await asyncio.to_thread(socket_dir.rmdir)
