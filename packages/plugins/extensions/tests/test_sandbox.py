from __future__ import annotations

import asyncio

import pytest
from chatcore_extensions.models import Artifact, ExampleCase
from chatcore_extensions.sandbox import DockerSandbox, SandboxPolicy


class _Stream:
    def __init__(self, value: bytes = b"", *, blocked: bool = False) -> None:
        self.value = value
        self.blocked = blocked

    async def read(self, _size: int) -> bytes:
        if self.blocked:
            await asyncio.Future()
        value, self.value = self.value, b""
        return value


class _Input:
    def __init__(self) -> None:
        self.data = b""

    def write(self, value: bytes) -> None:
        self.data = value

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Process:
    def __init__(self, *, output: bytes = b"", blocked: bool = False, returncode: int = 0) -> None:
        self.stdout = _Stream(output, blocked=blocked)
        self.stderr = _Stream(blocked=blocked)
        self.stdin = _Input()
        self.returncode = returncode
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"created", b""

    async def wait(self) -> int:
        if self.killed:
            self.returncode = -9
            return self.returncode
        if self.stdout.blocked:
            await asyncio.Future()
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.stdout.blocked = False
        self.stderr.blocked = False


@pytest.fixture
def artifact() -> Artifact:
    return Artifact("x", "def run(arguments): return 1", (ExampleCase({}, 1),))


def test_argv_has_fixed_image_and_hardening_flags() -> None:
    sandbox = DockerSandbox("sha256:" + "a" * 64)
    argv = sandbox.create_argv("sb-ext-" + "1" * 32)

    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == "65534:65534"
    assert argv[argv.index("--name") + 1] == "sb-ext-" + "1" * 32
    assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in argv and "no-new-privileges:true" in argv
    assert argv[argv.index("--memory") + 1] == "128m"
    assert argv[argv.index("--memory-swap") + 1] == "128m"
    assert argv[argv.index("--pids-limit") + 1] == "32"
    assert "--mount" not in argv and "--env" not in argv and "sh" not in argv
    assert argv[argv.index("--entrypoint") + 1] == "python"
    assert argv[-4:-2] == ["-I", "-B"]


def test_runtime_is_operator_selected_and_bound_to_policy_digest() -> None:
    from dataclasses import replace

    local = SandboxPolicy()
    runsc = replace(local, runtime="runsc", runtime_identity="a" * 64)
    argv = DockerSandbox("sha256:" + "a" * 64, policy=runsc, owner_id="b" * 32).create_argv(
        "sb-ext-" + "1" * 32
    )

    assert argv[argv.index("--runtime") + 1] == "runsc"
    labels = [arg for arg in argv if arg.startswith("chatcore.extensions.")]
    assert "chatcore.extensions.owner=" + "b" * 32 in labels
    assert "chatcore.extensions.image=sha256:" + "a" * 64 in labels
    assert runsc.digest != local.digest


def test_rejects_unverified_or_unknown_sandbox_runtime() -> None:
    with pytest.raises(ValueError):
        DockerSandbox(
            "sha256:" + "a" * 64,
            policy=SandboxPolicy(runtime="runsc", runtime_identity="a" * 64),
        )
    with pytest.raises(ValueError):
        SandboxPolicy(runtime="other")


@pytest.mark.asyncio
async def test_success_uses_container_and_removes_exact_owned_name(
    monkeypatch, artifact: Artifact
) -> None:
    import chatcore_extensions.sandbox as sandbox_module

    monkeypatch.setattr(sandbox_module.secrets, "token_hex", lambda _n: "a" * 32)
    calls: list[tuple[str, ...]] = []
    processes = [
        _Process(),
        _Process(output=b'{"ok":true,"value":42}'),
        _Process(),
    ]

    async def spawn(*args, **_kwargs):
        calls.append(tuple(args))
        return processes.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await DockerSandbox("sha256:" + "a" * 64).run(artifact, {})

    assert result.status == "ok" and result.value == 42
    assert calls[1][1:4] == ("start", "-ai", "sb-ext-" + "a" * 32)
    assert calls[2] == ("docker", "rm", "-f", "sb-ext-" + "a" * 32)


@pytest.mark.asyncio
async def test_timeout_kills_process_and_removes_container(monkeypatch, artifact: Artifact) -> None:
    import chatcore_extensions.sandbox as sandbox_module

    monkeypatch.setattr(sandbox_module.secrets, "token_hex", lambda _n: "b" * 32)
    start = _Process(blocked=True)
    calls: list[tuple[str, ...]] = []
    processes = [_Process(), start, _Process()]

    async def spawn(*args, **_kwargs):
        calls.append(tuple(args))
        return processes.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    sandbox = DockerSandbox("sha256:" + "a" * 64, policy=SandboxPolicy(timeout_seconds=0.01))
    result = await sandbox.run(artifact, {})

    assert result.status == "timeout"
    assert start.killed
    assert calls[-1] == ("docker", "rm", "-f", "sb-ext-" + "b" * 32)


@pytest.mark.asyncio
async def test_overflow_and_cancellation_always_cleanup(monkeypatch, artifact: Artifact) -> None:
    import chatcore_extensions.sandbox as sandbox_module

    names = iter(("c" * 32, "d" * 32))
    monkeypatch.setattr(sandbox_module.secrets, "token_hex", lambda _n: next(names))
    calls: list[tuple[str, ...]] = []
    started = asyncio.Event()
    processes = [
        _Process(),
        _Process(output=b"x" * 64),
        _Process(),
        _Process(),
        _Process(blocked=True),
        _Process(),
    ]

    async def spawn(*args, **_kwargs):
        calls.append(tuple(args))
        if len(calls) == 5:
            started.set()
        return processes.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    sandbox = DockerSandbox("sha256:" + "a" * 64, policy=SandboxPolicy(output_bytes=32))
    overflow = await sandbox.run(artifact, {})
    assert overflow.status == "output_limit"

    task = asyncio.create_task(sandbox.run(artifact, {}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls[-1] == ("docker", "rm", "-f", "sb-ext-" + "d" * 32)


@pytest.mark.asyncio
async def test_cancel_during_create_reaps_child_before_removing_exact_name(
    monkeypatch, artifact: Artifact
) -> None:
    import chatcore_extensions.sandbox as sandbox_module

    monkeypatch.setattr(sandbox_module.secrets, "token_hex", lambda _n: "e" * 32)
    create = _Process(blocked=True)
    calls = []
    processes = [create, _Process()]
    entered = asyncio.Event()

    async def spawn(*args, **_kwargs):
        calls.append(tuple(args))
        entered.set()
        return processes.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(DockerSandbox("sha256:" + "a" * 64).run(artifact, {}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert create.killed
    assert calls[-1] == ("docker", "rm", "-f", "sb-ext-" + "e" * 32)


@pytest.mark.asyncio
async def test_large_valid_source_envelope_is_not_rejected_before_container(monkeypatch) -> None:
    import chatcore_extensions.sandbox as sandbox_module

    source = "\n" * 32768
    artifact = Artifact("x", source, (ExampleCase({"text": "\x00" * 2000}, None),))
    monkeypatch.setattr(sandbox_module.secrets, "token_hex", lambda _n: "f" * 32)
    start = _Process(output=b'{"ok":true,"value":null}')
    calls = []
    processes = [_Process(), start, _Process()]

    async def spawn(*args, **_kwargs):
        calls.append(tuple(args))
        return processes.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await DockerSandbox("sha256:" + "a" * 64).run(artifact, {"text": "\x00" * 2000})
    assert result.status == "ok"
    assert start.stdin.data.startswith(b'{"arguments":')
    assert len(start.stdin.data) > 49152


@pytest.mark.asyncio
async def test_cleanup_failure_fails_closed_and_keeps_owned_name_tracked(
    monkeypatch, artifact: Artifact
) -> None:
    import chatcore_extensions.sandbox as sandbox_module

    monkeypatch.setattr(sandbox_module.secrets, "token_hex", lambda _n: "9" * 32)
    processes = [_Process(), _Process(output=b'{"ok":true,"value":1}'), _Process(returncode=1)]

    async def spawn(*_args, **_kwargs):
        return processes.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    sandbox = DockerSandbox("sha256:" + "a" * 64)
    with pytest.raises(sandbox_module.SandboxCleanupError):
        await sandbox.run(artifact, {})
    assert sandbox._containers == {"sb-ext-" + "9" * 32}


@pytest.mark.asyncio
async def test_recovery_removes_only_verified_full_ids_for_exact_owner(
    tmp_path, monkeypatch
) -> None:
    import json

    owner = "b" * 32
    container_id = "c" * 64
    image_id = "sha256:" + "a" * 64
    record = {
        "Id": container_id,
        "Name": "/sb-ext-" + "d" * 32,
        "Config": {
            "Labels": {
                "chatcore.extensions.managed": "v1",
                "chatcore.extensions.owner": owner,
                "chatcore.extensions.image": image_id,
                "chatcore.extensions.runtime": "runsc",
            }
        },
        "Image": image_id,
        "HostConfig": {"Runtime": "runsc"},
    }
    log = tmp_path / "docker.log"
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['RECOVERY_LOG']).open('a').write(' '.join(sys.argv[1:])+'\\n')\n"
        "if sys.argv[1] == 'ps':\n"
        " assert sys.argv[-1] == 'label=chatcore.extensions.owner=' + os.environ['OWNER']\n"
        f" print('{container_id}')\n"
        "elif sys.argv[1:3] == ['inspect', '--format']:\n"
        f" print(json.dumps(json.loads({json.dumps(json.dumps(record))})))\n"
        "elif sys.argv[1] == 'rm': pass\n"
        "else: sys.exit(2)\n"
    )
    docker.chmod(0o700)
    monkeypatch.setenv("RECOVERY_LOG", str(log))
    monkeypatch.setenv("OWNER", owner)
    policy = SandboxPolicy(runtime="runsc", runtime_identity="e" * 64)
    sandbox = DockerSandbox(image_id, docker=str(docker), policy=policy, owner_id=owner)

    await sandbox.recover_owned()

    calls = log.read_text().splitlines()
    assert calls[0].startswith("ps --no-trunc -aq --filter label=chatcore.extensions.owner=")
    assert calls[-1] == "rm -f " + container_id


@pytest.mark.asyncio
async def test_recovery_refuses_inconsistent_owned_metadata_without_removing(
    tmp_path, monkeypatch
) -> None:
    import json

    owner = "b" * 32
    container_id = "c" * 64
    image_id = "sha256:" + "a" * 64
    record = {
        "Id": container_id,
        "Name": "/sb-ext-" + "d" * 32,
        "Config": {"Labels": {"chatcore.extensions.owner": "foreign"}},
        "Image": image_id,
        "HostConfig": {"Runtime": "runsc"},
    }
    log = tmp_path / "docker.log"
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['RECOVERY_LOG']).open('a').write(' '.join(sys.argv[1:])+'\\n')\n"
        f"if sys.argv[1] == 'ps': print('{container_id}')\n"
        "elif sys.argv[1:3] == ['inspect', '--format']:\n"
        f" print(json.dumps(json.loads({json.dumps(json.dumps(record))})))\n"
        "elif sys.argv[1] == 'rm': sys.exit(9)\n"
        "else: sys.exit(2)\n"
    )
    docker.chmod(0o700)
    monkeypatch.setenv("RECOVERY_LOG", str(log))
    sandbox = DockerSandbox(
        image_id,
        docker=str(docker),
        policy=SandboxPolicy(runtime="runsc", runtime_identity="e" * 64),
        owner_id=owner,
    )

    with pytest.raises(Exception, match="metadata could not be verified"):
        await sandbox.recover_owned()
    assert not any(line.startswith("rm ") for line in log.read_text().splitlines())
