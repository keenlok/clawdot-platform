from __future__ import annotations

import asyncio
import errno
import fcntl
import hmac
import json
import os
import re
import socket
import socketserver
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import httpx
from chatcore.events import ChatRef

from chatcore_extensions.models import Artifact, RunResult, canonical_json
from chatcore_extensions.runtime import runc_identity, verify_runsc_manifest
from chatcore_extensions.sandbox import HARNESS_DIGEST, SandboxPolicy

MAX_BODY = 128 * 1024
MAX_RESPONSE = 32 * 1024


class ExtensionWorkerError(RuntimeError):
    """A safe worker failure with no payload, token or subprocess details."""


class WorkerServer:
    def __init__(self, socket_path: str | Path, token: str, sandbox: Any):
        if len(token) < 32:
            raise ValueError("invalid worker token")
        self.socket_path = Path(socket_path)
        self.token = token
        self.sandbox = sandbox
        self._semaphore = threading.BoundedSemaphore(2)
        self._server: _UnixHTTPServer | None = None
        self._owned_socket = False
        self._socket_identity: tuple[int, int] | None = None
        self._connections_lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._stopping = False

    def serve_forever(self) -> None:
        parent = self.socket_path.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("invalid socket directory")
        parent_metadata = parent.stat()
        if parent_metadata.st_uid != os.getuid() or stat.S_IMODE(parent_metadata.st_mode) != 0o700:
            raise ValueError("unsafe socket directory")
        if os.path.lexists(self.socket_path):
            _remove_stale_socket(self.socket_path)
        server = _UnixHTTPServer(str(self.socket_path), _Handler, bind_and_activate=False)
        server.owner = self
        server.server_bind()
        os.chmod(self.socket_path, 0o600)
        metadata = self.socket_path.lstat()
        self._socket_identity = (metadata.st_dev, metadata.st_ino)
        self._owned_socket = True
        server.server_activate()
        self._server = server
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            # Stop client reads before joining request threads. Sandbox handlers
            # continue independently and finish their bounded Docker cleanup.
            self._close_client_connections()
            server.server_close()
            if self._owned_socket and os.path.lexists(self.socket_path):
                metadata = self.socket_path.lstat()
                if (
                    stat.S_ISSOCK(metadata.st_mode)
                    and metadata.st_uid == os.getuid()
                    and (metadata.st_dev, metadata.st_ino) == self._socket_identity
                ):
                    self.socket_path.unlink()
            self._owned_socket = False

    def _track_connection(self, connection: socket.socket) -> None:
        with self._connections_lock:
            stopping = self._stopping
            if not stopping:
                self._connections.add(connection)
        if stopping:
            self._close_connection(connection)

    def _untrack_connection(self, connection: socket.socket) -> None:
        with self._connections_lock:
            self._connections.discard(connection)

    def _close_client_connections(self) -> None:
        with self._connections_lock:
            self._stopping = True
            connections = tuple(self._connections)
        for connection in connections:
            self._close_connection(connection)

    @staticmethod
    def _close_connection(connection: socket.socket) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()

    def _run(self, payload: dict[str, Any]) -> RunResult:
        if not self._semaphore.acquire(blocking=False):
            raise _Busy
        try:
            if set(payload) != {
                "version",
                "chat_key",
                "run_id",
                "artifact",
                "artifact_digest",
                "arguments",
            }:
                raise ValueError
            if type(payload["version"]) is not int or payload["version"] != 1:
                raise ValueError
            ChatRef.parse(payload["chat_key"])
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", payload["run_id"]):
                raise ValueError
            raw_artifact = payload["artifact"]
            if not isinstance(raw_artifact, dict):
                raise ValueError
            artifact_bytes = canonical_json(raw_artifact)
            if len(artifact_bytes) > MAX_BODY:
                raise ValueError
            artifact = Artifact.from_json(artifact_bytes)
            if not hmac.compare_digest(artifact.digest, payload["artifact_digest"]):
                raise ValueError
            if not isinstance(payload["arguments"], dict):
                raise ValueError
            if len(canonical_json(payload["arguments"])) > 16 * 1024:
                raise ValueError
            result = asyncio.run(self.sandbox.run(artifact, payload["arguments"]))
            return result
        finally:
            self._semaphore.release()

    def __repr__(self) -> str:
        return (
            f"WorkerServer(socket_path={str(self.socket_path)!r}, "
            f"sandbox={type(self.sandbox).__name__})"
        )


class _Busy(Exception):
    pass


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    # server_close joins accepted request threads before container cleanup. Each
    # handler has a socket timeout and each Docker operation has its own deadline.
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False

    @property
    def owner(self) -> WorkerServer:
        return self.worker_owner

    @owner.setter
    def owner(self, value: WorkerServer) -> None:
        self.worker_owner = value

    def process_request(self, request: socket.socket, client_address: object) -> None:
        self.owner._track_connection(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.owner._untrack_connection(request)
            raise

    def process_request_thread(self, request: socket.socket, client_address: object) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.owner._untrack_connection(request)

    def handle_error(self, request: socket.socket, client_address: object) -> None:
        error = sys.exc_info()[1]
        expected_disconnects = {
            errno.EBADF,
            errno.EPIPE,
            errno.ECONNRESET,
            errno.ECONNABORTED,
            errno.ENOTCONN,
        }
        if isinstance(error, OSError) and error.errno in expected_disconnects:
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    server: _UnixHTTPServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, _format: str, *_args: object) -> None:
        return

    @property
    def owner(self) -> WorkerServer:
        return self.server.owner

    def _send(self, status: int, value: object) -> None:
        body = canonical_json(value)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            return

    def _authorized(self) -> bool:
        auth_values = self.headers.get_all("Authorization", [])
        if len(auth_values) != 1:
            return False
        supplied = auth_values[0]
        expected = "Bearer " + self.owner.token
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send(404, {"error": "not_found"})
        elif not self._authorized():
            self._send(401, {"error": "unauthorized"})
        else:
            self._send(
                200,
                {
                    "status": "ok",
                    "image_id": self.owner.sandbox.image_id,
                    "policy_digest": self.owner.sandbox.policy.digest,
                    "harness_digest": HARNESS_DIGEST,
                    "repo_digest": getattr(self.owner.sandbox, "repo_digest", ""),
                    "runtime": getattr(self.owner.sandbox.policy, "runtime", "runc"),
                    "runtime_version": getattr(
                        self.owner.sandbox.policy, "runtime_version", "operator-default"
                    ),
                    "runtime_manifest_digest": getattr(
                        self.owner.sandbox.policy, "runtime_identity", "builtin:runc-v1"
                    ),
                },
            )

    def do_POST(self) -> None:
        if self.path != "/run":
            self._send(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if (
            self.headers.get("Transfer-Encoding") is not None
            or self.headers.get("Content-Type") != "application/json"
        ):
            self._send(400, {"error": "invalid_request"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise ValueError
            length_text = lengths[0]
            if length_text is None or not length_text.isdecimal():
                raise ValueError
            length = int(length_text)
            if length <= 0 or length > MAX_BODY:
                self._send(413, {"error": "request_too_large"})
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError
            payload = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_unique_pairs,
                parse_constant=_reject_constant,
            )
            if not isinstance(payload, dict):
                raise ValueError
            result = self.owner._run(payload)
            self._send(200, _result_dict(result))
        except _Busy:
            self._send(503, {"error": "busy"})
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            self._send(400, {"error": "invalid_request"})
        except Exception:
            self._send(503, {"error": "worker_unavailable"})


class WorkerClient:
    def __init__(self, socket_path: str | Path, token: str):
        self.socket_path = str(socket_path)
        self._token = token
        transport = httpx.AsyncHTTPTransport(uds=self.socket_path, retries=0)
        self._client = httpx.AsyncClient(
            transport=transport,
            base_url="http://extension-worker",
            timeout=httpx.Timeout(60.0),
            follow_redirects=False,
            trust_env=False,
            headers={"Authorization": "Bearer " + token},
        )

    async def health(self) -> dict[str, str]:
        data = await self._request("GET", "/health")
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise ExtensionWorkerError("worker unavailable")
        return {str(key): str(value) for key, value in data.items()}

    async def run(
        self, *, chat: ChatRef, run_id: str, artifact: Artifact, arguments: dict[str, object]
    ) -> RunResult:
        payload = {
            "version": 1,
            "chat_key": chat.key,
            "run_id": run_id,
            "artifact": json.loads(artifact.to_json()),
            "artifact_digest": artifact.digest,
            "arguments": arguments,
        }
        data = await self._request("POST", "/run", json=payload)
        try:
            if not isinstance(data, dict) or set(data) != {
                "status",
                "value",
                "duration_ms",
                "image_id",
                "policy_digest",
                "harness_digest",
            }:
                raise ValueError
            return RunResult(**data)
        except (TypeError, ValueError):
            raise ExtensionWorkerError("invalid worker response") from None

    async def _request(self, method: str, path: str, **kwargs: object) -> Any:
        try:
            async with self._client.stream(method, path, **kwargs) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE:
                        raise ExtensionWorkerError("invalid worker response")
                if response.status_code == 503:
                    raise ExtensionWorkerError("worker unavailable")
                if response.status_code != 200:
                    raise ExtensionWorkerError("worker rejected request")
                return json.loads(
                    bytes(content).decode("utf-8"),
                    object_pairs_hook=_unique_pairs,
                    parse_constant=_reject_constant,
                )
        except ExtensionWorkerError:
            raise
        except Exception:
            raise ExtensionWorkerError("worker unavailable") from None

    async def aclose(self) -> None:
        await self._client.aclose()

    def __repr__(self) -> str:
        return f"WorkerClient(socket_path={self.socket_path!r})"


def _result_dict(result: RunResult) -> dict[str, object]:
    return {
        "status": result.status,
        "value": result.value,
        "duration_ms": result.duration_ms,
        "image_id": result.image_id,
        "policy_digest": result.policy_digest,
        "harness_digest": result.harness_digest,
    }


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite")


def read_token_file(token_path: str | Path) -> str:
    try:
        fd = os.open(token_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            os.close(fd)
            raise ValueError("invalid worker token file")
        with os.fdopen(fd, encoding="utf-8") as handle:
            token = handle.read(256).strip()
        if len(token) < 32 or len(token) > 128:
            raise ValueError("invalid worker token file")
        return token
    except OSError:
        raise ValueError("invalid worker token file") from None


def read_owner_id_file(owner_path: str | Path) -> str:
    try:
        fd = os.open(owner_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            os.close(fd)
            raise ValueError("invalid worker owner file")
        with os.fdopen(fd, encoding="ascii") as handle:
            owner_id = handle.read(64).strip()
        if not re.fullmatch(r"[0-9a-f]{32}", owner_id):
            raise ValueError("invalid worker owner file")
        return owner_id
    except OSError:
        raise ValueError("invalid worker owner file") from None


def _acquire_worker_lock(token_path: str | Path) -> int:
    try:
        fd = os.open(token_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            os.close(fd)
            raise ValueError
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, ValueError):
        raise ValueError("worker already running or token file is invalid") from None


def _remove_stale_socket(path: Path) -> None:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError
        identity = metadata.st_dev, metadata.st_ino
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.25)
        try:
            probe.connect(str(path))
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED:
                raise ValueError from None
        else:
            raise ValueError
        finally:
            probe.close()
        current = path.lstat()
        if (
            not stat.S_ISSOCK(current.st_mode)
            or current.st_uid != os.getuid()
            or (current.st_dev, current.st_ino) != identity
        ):
            raise ValueError
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        raise ValueError("socket already exists") from None
    except ValueError:
        raise ValueError("socket already exists") from None


def main() -> None:
    import argparse
    import signal
    import threading

    from chatcore_extensions.sandbox import DockerSandbox

    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--runtime", choices=("runc", "runsc"), default="runc")
    parser.add_argument("--runtime-manifest")
    parser.add_argument("--owner-id-file")
    parser.add_argument("--cleanup-owned-only", action="store_true")
    args = parser.parse_args()
    if (args.runtime == "runsc") != bool(args.runtime_manifest):
        raise SystemExit("Invalid runtime configuration")
    if args.runtime == "runsc" and not args.owner_id_file:
        raise SystemExit("Invalid runtime configuration")
    try:
        lock_fd = _acquire_worker_lock(args.token_file)
    except ValueError:
        raise SystemExit("Invalid token file or worker already running") from None
    try:
        token = read_token_file(args.token_file)
        owner_id = read_owner_id_file(args.owner_id_file) if args.owner_id_file else None
        if args.runtime == "runsc":
            try:
                identity = verify_runsc_manifest(args.runtime_manifest)
            except ValueError:
                raise SystemExit("Runtime verification failed") from None
        else:
            identity = runc_identity()
        image_id, repo_digest = (
            ("sha256:" + "0" * 64, "")
            if args.cleanup_owned_only
            else asyncio.run(_resolve_image(args.image))
        )
        policy = SandboxPolicy(
            runtime=identity.runtime,
            runtime_identity=identity.manifest_digest,
            runtime_version=identity.version,
        )
        sandbox = DockerSandbox(image_id, repo_digest=repo_digest, policy=policy, owner_id=owner_id)
        asyncio.run(sandbox.recover_owned())
        if args.cleanup_owned_only:
            return
        server = WorkerServer(args.socket, token, sandbox)
        signal.signal(
            signal.SIGTERM,
            lambda *_: threading.Thread(target=server.shutdown, daemon=True).start(),
        )
        try:
            server.serve_forever()
        finally:
            asyncio.run(sandbox.aclose())
    finally:
        os.close(lock_fd)


async def _resolve_image(reference: str) -> tuple[str, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "image",
        "inspect",
        reference,
        "--format",
        "{{.Id}} {{index .RepoDigests 0}}",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise SystemExit("Image inspection failed") from None
    fields = out.decode("ascii", errors="ignore").strip().split()
    if len(fields) != 2:
        raise SystemExit("Image unavailable")
    image_id, repo_digest = fields
    if (
        proc.returncode != 0
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
        or not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}", repo_digest)
    ):
        raise SystemExit("Image unavailable")
    return image_id, repo_digest
