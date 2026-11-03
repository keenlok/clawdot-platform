from __future__ import annotations

import asyncio
import json
import tempfile
import threading
from pathlib import Path

import httpx
import pytest
from chatcore.events import ChatRef
from chatcore_extensions.models import Artifact, ExampleCase, RunResult
from chatcore_extensions.worker import WorkerClient, WorkerServer

TOKEN = "t" * 48
IMAGE = "sha256:" + "a" * 64


class _Sandbox:
    image_id = IMAGE
    policy = type("Policy", (), {"digest": "b" * 64})()

    def __init__(self):
        self.runs = 0

    async def run(self, artifact, arguments):
        self.runs += 1
        return RunResult("ok", {"v": arguments.get("v")}, 1, IMAGE, "b" * 64, "c" * 64)


@pytest.fixture
def artifact() -> Artifact:
    return Artifact(
        "identity", "def run(arguments): return arguments", (ExampleCase({"v": 2}, {"v": 2}),)
    )


@pytest.fixture
async def worker():
    directory = Path(tempfile.mkdtemp(prefix="ext-", dir="/tmp"))
    await asyncio.to_thread(directory.chmod, 0o700)
    path = directory / "worker.sock"
    server = WorkerServer(path, TOKEN, _Sandbox())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for _ in range(100):
        if path.exists():
            break
        await asyncio.sleep(0.01)
    yield path, server, thread
    server.shutdown()
    thread.join(timeout=2)
    await asyncio.to_thread(directory.rmdir)


@pytest.mark.asyncio
async def test_real_unix_worker_authenticates_and_runs_scoped_job(
    worker, artifact: Artifact
) -> None:
    path, _, _ = worker
    client = WorkerClient(path, TOKEN)
    result = await client.run(
        chat=ChatRef("tg", "-1001"), run_id="run-123", artifact=artifact, arguments={"v": 7}
    )
    await client.aclose()

    assert result.status == "ok" and result.value == {"v": 7}
    assert TOKEN not in repr(client)


@pytest.mark.asyncio
async def test_wrong_token_and_bad_envelope_are_denied_before_sandbox(
    worker, artifact: Artifact
) -> None:
    path, _, _ = worker
    transport = httpx.AsyncHTTPTransport(uds=str(path))
    async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
        response = await client.post(
            "/run", headers={"Authorization": "Bearer " + "x" * 48}, content=b"{"
        )
    assert response.status_code == 401

    no_auth_transport = httpx.AsyncHTTPTransport(uds=str(path))
    async with httpx.AsyncClient(transport=no_auth_transport, base_url="http://worker") as client:
        response = await client.post("/run", content=b"not json")
    assert response.status_code == 401


def test_worker_client_has_no_token_in_repr() -> None:
    client = WorkerClient("/tmp/worker.sock", TOKEN)
    assert TOKEN not in repr(client)
    asyncio.run(client.aclose())


@pytest.mark.asyncio
async def test_cli_process_resolves_image_serves_runs_and_removes_socket_on_sigterm(
    tmp_path: Path,
) -> None:
    import os
    import signal
    import sys
    import tempfile

    image = "sha256:" + "a" * 64
    repo_digest = "python@sha256:" + "b" * 64
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['image', 'inspect']:\n"
        f" print('{image} {repo_digest}')\n"
        "elif args[0] == 'create': print('container-id')\n"
        'elif args[0] == \'start\': print(\'{\\"ok\\":true,\\"value\\":{\\"cli\\":true}}\')\n'
        "elif args[0] == 'inspect': print('false')\n"
        "elif args[0] == 'rm': pass\n"
        "else: sys.exit(2)\n"
    )
    await asyncio.to_thread(docker.chmod, 0o700)
    socket_dir = Path(tempfile.mkdtemp(prefix="cli-", dir="/tmp"))
    await asyncio.to_thread(socket_dir.chmod, 0o700)
    token_file = socket_dir / "worker.token"
    token_file.write_text(TOKEN)
    await asyncio.to_thread(token_file.chmod, 0o600)
    socket_path = socket_dir / "worker.sock"
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from chatcore_extensions.worker import main; main()",
        "--socket",
        str(socket_path),
        "--token-file",
        str(token_file),
        "--image",
        "python:test",
        cwd=Path(__file__).parents[4],
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        for _ in range(300):
            if socket_path.exists() or process.returncode is not None:
                break
            await asyncio.sleep(0.01)
        error = await process.stderr.read() if process.returncode is not None else b""
        assert process.returncode is None and socket_path.exists(), error.decode()
        unauthenticated = httpx.AsyncHTTPTransport(uds=str(socket_path))
        async with httpx.AsyncClient(
            transport=unauthenticated, base_url="http://extension-worker"
        ) as http_client:
            response = await http_client.get("/health")
        assert response.status_code == 401
        client = WorkerClient(socket_path, TOKEN)
        health = await client.health()
        assert health["image_id"] == image and health["repo_digest"] == repo_digest
        artifact = Artifact(
            "identity", "def run(arguments): return arguments", (ExampleCase({}, 1),)
        )
        result = await client.run(
            chat=ChatRef("tg", "-1001"), run_id="cli-run", artifact=artifact, arguments={}
        )
        assert result.status == "ok" and result.value == {"cli": True}
        await client.aclose()
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), timeout=3) == 0
        assert not socket_path.exists()
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=2)
        await asyncio.to_thread(token_file.unlink, missing_ok=True)
        await asyncio.to_thread(socket_dir.rmdir)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shutdown_phase", "start_delay"),
    [("creating", "1"), ("running", "1"), ("running", "8")],
    ids=["delayed-create", "active-run", "run-timeout"],
)
async def test_cli_sigterm_drains_inflight_docker_children(
    tmp_path: Path, shutdown_phase: str, start_delay: str
) -> None:
    import os
    import signal
    import sys
    import tempfile

    image = "sha256:" + "a" * 64
    repo_digest = "python@sha256:" + "b" * 64
    marker_dir = Path(tempfile.mkdtemp(prefix="ext-cli-", dir="/tmp"))
    await asyncio.to_thread(marker_dir.chmod, 0o700)
    marker = marker_dir / "container"
    phase = marker_dir / "phase"
    child_pid = marker_dir / "child.pid"
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "marker = Path(os.environ['EXT_TEST_MARKER'])\n"
        "phase = marker.with_name('phase')\n"
        "pid = marker.with_name('child.pid')\n"
        "if args[:2] == ['image', 'inspect']:\n"
        f" print('{image} {repo_digest}')\n"
        "elif args[0] == 'create':\n"
        " phase.write_text('creating')\n"
        " pid.write_text(str(os.getpid()))\n"
        " time.sleep(1)\n"
        " marker.write_text('owned-container')\n"
        " print('container-id')\n"
        "elif args[0] == 'start':\n"
        " phase.write_text('running')\n"
        " pid.write_text(str(os.getpid()))\n"
        " time.sleep(float(os.environ['EXT_TEST_START_DELAY']))\n"
        " print(json.dumps({'ok': True, 'value': {'drained': True}}))\n"
        "elif args[0] == 'inspect': print('false')\n"
        "elif args[0] == 'rm':\n"
        " marker.unlink(missing_ok=True)\n"
        " phase.unlink(missing_ok=True)\n"
        "else: sys.exit(2)\n"
    )
    await asyncio.to_thread(docker.chmod, 0o700)
    socket_dir = Path(tempfile.mkdtemp(prefix="ext-sock-", dir="/tmp"))
    await asyncio.to_thread(socket_dir.chmod, 0o700)
    token_file = socket_dir / "worker.token"
    token_file.write_text(TOKEN)
    await asyncio.to_thread(token_file.chmod, 0o600)
    socket_path = socket_dir / "worker.sock"
    env = dict(
        os.environ,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        EXT_TEST_MARKER=str(marker),
        EXT_TEST_START_DELAY=start_delay,
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from chatcore_extensions.worker import main; main()",
        "--socket",
        str(socket_path),
        "--token-file",
        str(token_file),
        "--image",
        "python:test",
        cwd=Path(__file__).parents[4],
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    client = None
    call = None
    try:
        for _ in range(300):
            if socket_path.exists() or process.returncode is not None:
                break
            await asyncio.sleep(0.01)
        error = await process.stderr.read() if process.returncode is not None else b""
        assert process.returncode is None and socket_path.exists(), error.decode()
        client = WorkerClient(socket_path, TOKEN)
        assert (await client.health())["image_id"] == image
        artifact = Artifact("test", "def run(arguments): return 1", (ExampleCase({}, 1),))
        call = asyncio.create_task(
            client.run(
                chat=ChatRef("tg", "-1001"),
                run_id="shutdown-race",
                artifact=artifact,
                arguments={},
            )
        )
        expected_phase = "creating" if shutdown_phase == "creating" else "running"
        for _ in range(400):
            if phase.exists() and await asyncio.to_thread(phase.read_text) == expected_phase:
                break
            await asyncio.sleep(0.01)
        assert phase.exists() and await asyncio.to_thread(phase.read_text) == expected_phase
        pid = int(await asyncio.to_thread(child_pid.read_text))
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), timeout=12) == 0
        if call is not None:
            try:
                await asyncio.wait_for(call, timeout=1)
            except Exception:
                pass
        if client is not None:
            await client.aclose()
        for _ in range(300):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not marker.exists(), "an owned container was created after worker shutdown"
        assert not socket_path.exists()
    finally:
        if call is not None and not call.done():
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
        if client is not None:
            await client.aclose()
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=2)
        await asyncio.to_thread(marker.unlink, missing_ok=True)
        await asyncio.to_thread(phase.unlink, missing_ok=True)
        await asyncio.to_thread(child_pid.unlink, missing_ok=True)
        await asyncio.to_thread(token_file.unlink, missing_ok=True)
        await asyncio.to_thread(marker_dir.rmdir)
        await asyncio.to_thread(socket_dir.rmdir)


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_stage", ["request-line", "header", "body"])
async def test_cli_sigterm_closes_trickling_partial_http_requests(
    tmp_path: Path, partial_stage: str
) -> None:
    import os
    import signal
    import sys
    import tempfile

    image = "sha256:" + "a" * 64
    repo_digest = "python@sha256:" + "b" * 64
    socket_dir = Path(tempfile.mkdtemp(prefix="ext-slow-", dir="/tmp"))
    await asyncio.to_thread(socket_dir.chmod, 0o700)
    socket_path = socket_dir / "worker.sock"
    token_file = socket_dir / "worker.token"
    token_file.write_text(TOKEN)
    await asyncio.to_thread(token_file.chmod, 0o600)
    docker_log = socket_dir / "docker.log"
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "with Path(os.environ['EXT_TEST_DOCKER_LOG']).open('a') as log: "
        "log.write(sys.argv[1]+'\\n')\n"
        "if sys.argv[1:3] == ['image', 'inspect']:\n"
        f" print('{image} {repo_digest}')\n"
        "else: sys.exit(0)\n"
    )
    await asyncio.to_thread(docker.chmod, 0o700)
    env = dict(
        os.environ,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        EXT_TEST_DOCKER_LOG=str(docker_log),
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from chatcore_extensions.worker import main; main()",
        "--socket",
        str(socket_path),
        "--token-file",
        str(token_file),
        "--image",
        "python:test",
        cwd=Path(__file__).parents[4],
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    writer = None
    trickler = None
    try:
        for _ in range(300):
            if socket_path.exists() or process.returncode is not None:
                break
            await asyncio.sleep(0.01)
        error = await process.stderr.read() if process.returncode is not None else b""
        assert process.returncode is None and socket_path.exists(), error.decode()
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        del reader
        prefix = {
            "request-line": b"GET /health HTTP/1.1",
            "header": b"GET /health HTTP/1.1\r\nHost: local\r\nX-Slow:",
            "body": (
                b"POST /run HTTP/1.1\r\nHost: local\r\nAuthorization: Bearer "
                + TOKEN.encode()
                + b"\r\nContent-Type: application/json\r\nContent-Length: 10000\r\n\r\n{"
            ),
        }[partial_stage]
        writer.write(prefix)
        await writer.drain()
        await asyncio.sleep(0.2)

        async def trickle() -> None:
            while True:
                try:
                    writer.write(b"x")
                    await writer.drain()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                await asyncio.sleep(0.1)

        trickler = asyncio.create_task(trickle())
        await asyncio.sleep(0.2)
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), timeout=2) == 0
        assert not socket_path.exists()
        if docker_log.exists():
            calls = await asyncio.to_thread(docker_log.read_text)
            assert calls.strip() == "image"
    finally:
        if trickler is not None:
            trickler.cancel()
            await asyncio.gather(trickler, return_exceptions=True)
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=2)
        await asyncio.to_thread(token_file.unlink, missing_ok=True)
        await asyncio.to_thread(docker_log.unlink, missing_ok=True)
        await asyncio.to_thread(socket_path.unlink, missing_ok=True)
        await asyncio.to_thread(socket_dir.rmdir)


@pytest.mark.asyncio
async def test_bad_content_length_chunked_and_large_request_are_rejected(worker) -> None:
    import socket

    path, _, _ = worker
    cases = [
        (
            b"POST /run HTTP/1.1\r\nHost: worker\r\nAuthorization: Bearer "
            + TOKEN.encode()
            + b"\r\nContent-Type: application/json\r\n\r\n{}",
            b"400",
        ),
        (
            b"POST /run HTTP/1.1\r\nHost: worker\r\nAuthorization: Bearer "
            + TOKEN.encode()
            + b"\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n0\r\n\r\n",
            b"400",
        ),
        (
            b"POST /run HTTP/1.1\r\nHost: worker\r\nAuthorization: Bearer "
            + TOKEN.encode()
            + b"\r\nContent-Length: 131073\r\nContent-Type: application/json\r\n\r\n",
            b"413",
        ),
    ]
    for request, expected in cases:
        client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client_socket.settimeout(2)
        client_socket.connect(str(path))
        client_socket.sendall(request)
        response = await asyncio.to_thread(client_socket.recv, 4096)
        client_socket.close()
        assert expected in response.split(b"\r\n", 1)[0]


@pytest.mark.asyncio
async def test_unknown_artifact_fields_and_digest_fail_before_sandbox(
    worker, artifact: Artifact
) -> None:
    _, server, _ = worker
    sandbox = server.sandbox
    base = {
        "version": 1,
        "chat_key": "tg:-1001",
        "run_id": "run-123",
        "artifact": json.loads(artifact.to_json()),
        "artifact_digest": "0" * 64,
        "arguments": {},
    }
    with pytest.raises(ValueError):
        server._run({**base, "unexpected": "field"})
    with pytest.raises(ValueError):
        server._run(base)
    assert sandbox.runs == 0


def test_worker_token_file_and_socket_directory_are_private(tmp_path) -> None:
    from chatcore_extensions.worker import read_token_file

    token_path = tmp_path / "token"
    token_path.write_text(TOKEN)
    token_path.chmod(0o600)
    assert read_token_file(token_path) == TOKEN
    unsafe = tmp_path / "unsafe"
    unsafe.write_text(TOKEN)
    unsafe.chmod(0o644)
    with pytest.raises(ValueError):
        read_token_file(unsafe)
    target = tmp_path / "target"
    target.write_text(TOKEN)
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        read_token_file(link)

    tmp_path.chmod(0o755)
    server = WorkerServer(tmp_path / "worker.sock", TOKEN, _Sandbox())
    with pytest.raises(ValueError, match="unsafe socket directory"):
        server.serve_forever()


def test_startup_lock_prevents_a_second_worker_on_the_same_token(tmp_path) -> None:
    from chatcore_extensions.worker import _acquire_worker_lock

    token_path = tmp_path / "token"
    token_path.write_text(TOKEN)
    token_path.chmod(0o600)
    first = _acquire_worker_lock(token_path)
    try:
        with pytest.raises(ValueError, match="already running"):
            _acquire_worker_lock(token_path)
    finally:
        import os

        os.close(first)


def test_worker_owner_identity_file_is_private_and_fixed_width(tmp_path) -> None:
    from chatcore_extensions.worker import read_owner_id_file

    owner = "a" * 32
    owner_path = tmp_path / "owner-id"
    owner_path.write_text(owner)
    owner_path.chmod(0o600)
    assert read_owner_id_file(owner_path) == owner
    owner_path.write_text("../" + owner)
    with pytest.raises(ValueError, match="owner file"):
        read_owner_id_file(owner_path)
    owner_path.unlink()
    owner_path.symlink_to(tmp_path / "target")
    (tmp_path / "target").write_text(owner)
    (tmp_path / "target").chmod(0o600)
    with pytest.raises(ValueError, match="owner file"):
        read_owner_id_file(owner_path)


def test_worker_removes_only_an_owned_unresponsive_stale_socket() -> None:
    import socket
    import tempfile
    import threading
    import time
    from pathlib import Path

    socket_dir = Path(tempfile.mkdtemp(prefix="ext-stale-", dir="/tmp"))
    socket_dir.chmod(0o700)
    parent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale = socket_dir / "worker.sock"
    parent.bind(str(stale))
    parent.close()
    stale.chmod(0o600)
    server = WorkerServer(stale, TOKEN, _Sandbox())
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if stale.exists():
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.1)
                probe.connect(str(stale))
                probe.close()
                break
            except OSError:
                probe.close()
        time.sleep(0.01)
    assert stale.exists()
    server.shutdown()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert not stale.exists()
    socket_dir.rmdir()


@pytest.mark.asyncio
async def test_worker_returns_immediate_busy_when_two_jobs_are_running() -> None:
    from chatcore_extensions.worker import ExtensionWorkerError

    class SlowSandbox(_Sandbox):
        def __init__(self):
            super().__init__()
            self.started = 0
            self.lock = threading.Lock()
            self.two_started = threading.Event()
            self.release = threading.Event()

        async def run(self, artifact, arguments):
            with self.lock:
                self.started += 1
                if self.started == 2:
                    self.two_started.set()
            await asyncio.to_thread(self.release.wait, 5)
            return await super().run(artifact, arguments)

    directory = Path(tempfile.mkdtemp(prefix="busy-", dir="/tmp"))
    await asyncio.to_thread(directory.chmod, 0o700)
    path = directory / "worker.sock"
    sandbox = SlowSandbox()
    server = WorkerServer(path, TOKEN, sandbox)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for _ in range(100):
        if path.exists():
            break
        await asyncio.sleep(0.01)
    client = WorkerClient(path, TOKEN)
    artifact = Artifact("identity", "def run(arguments): return arguments", (ExampleCase({}, 1),))

    async def job(run_id):
        return await client.run(
            chat=ChatRef("tg", "-1"), run_id=run_id, artifact=artifact, arguments={"v": 1}
        )

    first = asyncio.create_task(job("run-1"))
    second = asyncio.create_task(job("run-2"))
    assert await asyncio.to_thread(sandbox.two_started.wait, 3)
    with pytest.raises(ExtensionWorkerError, match="worker unavailable"):
        await client.run(
            chat=ChatRef("tg", "-1"), run_id="run-3", artifact=artifact, arguments={"v": 3}
        )
    sandbox.release.set()
    await asyncio.gather(first, second)
    await client.aclose()
    server.shutdown()
    await asyncio.to_thread(thread.join, 2)
    await asyncio.to_thread(directory.rmdir)
