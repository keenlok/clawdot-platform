from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass

from chatcore_extensions.models import Artifact, RunResult, canonical_json

HARNESS = r"""import contextlib,io,json,sys

def reject(_): raise ValueError("invalid")
def pairs(items):
 d={}
 for k,v in items:
  if k in d: raise ValueError("duplicate")
  d[k]=v
 return d
try:
 raw=sys.stdin.buffer.read(131073)
 if len(raw)>131072: raise ValueError("input")
 env=json.loads(raw.decode("utf-8"),object_pairs_hook=pairs,parse_constant=reject)
 if set(env)!={"source","arguments"}: raise ValueError("input")
 source=env["source"]
 if not isinstance(source,str) or len(source.encode("utf-8"))>32768: raise ValueError("source")
 ns={"__builtins__":__builtins__}
 out=io.StringIO(); err=io.StringIO()
 with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
  exec(compile(source,"<extension>","exec"),ns,ns)
  result=ns["run"](env["arguments"])
 encoded=json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(",",":" )).encode("utf-8")
 if len(encoded)>16384: raise ValueError("output_limit")
 sys.stdout.buffer.write(b'{"ok":true,"value":'+encoded+b'}')
except BaseException as exc:
 status="output_limit" if str(exc)=="output_limit" else "execution_error"
 sys.stdout.write('{"ok":false,"status":"'+status+'"}')
"""
HARNESS_DIGEST = hashlib.sha256(HARNESS.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SandboxPolicy:
    timeout_seconds: float = 5.0
    output_bytes: int = 32 * 1024
    memory: str = "128m"
    cpus: str = "0.5"
    pids: int = 32
    tmpfs: str = "/tmp:rw,noexec,nosuid,size=16m"  # noqa: S108
    version: str = "docker-python-transform-v1"
    runtime: str = "runc"
    runtime_identity: str = "builtin:runc-v1"
    runtime_version: str = "operator-default"

    def __post_init__(self) -> None:
        if self.runtime not in {"runc", "runsc"}:
            raise ValueError("unsupported sandbox runtime")
        if self.runtime == "runsc":
            if not re.fullmatch(r"[0-9a-f]{64}", self.runtime_identity):
                raise ValueError("runsc requires a verified runtime manifest")
        elif self.runtime_identity != "builtin:runc-v1":
            raise ValueError("invalid runc identity")

    @property
    def digest(self) -> str:
        body = canonical_json(
            {
                "version": self.version,
                "timeout": self.timeout_seconds,
                "output": self.output_bytes,
                "memory": self.memory,
                "cpus": self.cpus,
                "pids": self.pids,
                "tmpfs": self.tmpfs,
                "runtime": self.runtime,
                "runtime_identity": self.runtime_identity,
            }
        )
        return hashlib.sha256(body).hexdigest()


class _OutputLimit(Exception):
    pass


class SandboxCleanupError(RuntimeError):
    """Raised when an owned container could not be removed safely."""


class OwnedContainerRecoveryError(RuntimeError):
    """Raised when worker-owned container state cannot be verified safely."""


class DockerSandbox:
    def __init__(
        self,
        image_id: str,
        *,
        docker: str = "docker",
        policy: SandboxPolicy | None = None,
        repo_digest: str = "",
        owner_id: str | None = None,
    ):
        if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("invalid worker image id")
        self.image_id = image_id
        self.repo_digest = repo_digest
        self.docker = docker
        self.policy = policy or SandboxPolicy()
        if owner_id is not None and not re.fullmatch(r"[0-9a-f]{32}", owner_id):
            raise ValueError("invalid worker owner id")
        if self.policy.runtime == "runsc" and owner_id is None:
            raise ValueError("runsc requires persistent worker ownership identity")
        self.owner_id = owner_id
        self._containers: set[str] = set()

    def create_argv(self, name: str) -> list[str]:
        p = self.policy
        argv = [
            self.docker,
            "create",
            "--name",
            name,
            "--interactive",
            "--network",
            "none",
            "--user",
            "65534:65534",
            "--read-only",
            "--runtime",
            p.runtime,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            p.memory,
            "--memory-swap",
            p.memory,
            "--cpus",
            p.cpus,
            "--pids-limit",
            str(p.pids),
            "--tmpfs",
            p.tmpfs,
            "--workdir",
            "/tmp",  # noqa: S108
            "--entrypoint",
            "python",
            self.image_id,
            "-I",
            "-B",
            "-c",
            HARNESS,
        ]
        if self.owner_id is not None:
            labels = (
                ("chatcore.extensions.managed", "v1"),
                ("chatcore.extensions.owner", self.owner_id),
                ("chatcore.extensions.image", self.image_id),
                ("chatcore.extensions.runtime", self.policy.runtime),
            )
            for key, value in labels:
                argv[argv.index("--name") : argv.index("--name")] = ["--label", f"{key}={value}"]
        return argv

    async def recover_owned(self) -> None:
        """Reap only exact owner-labeled containers whose inspect identity is consistent."""
        if self.owner_id is None:
            return
        query = await self._docker_output(
            [
                self.docker,
                "ps",
                "--no-trunc",
                "-aq",
                "--filter",
                f"label=chatcore.extensions.owner={self.owner_id}",
            ],
            limit=16 * 1024,
            budget_s=5,
        )
        ids = query.decode("ascii", errors="strict").splitlines()
        if len(ids) > 256 or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids):
            raise OwnedContainerRecoveryError("owned container listing is invalid")
        for container_id in ids:
            try:
                raw = await self._docker_output(
                    [self.docker, "inspect", "--format", "{{json .}}", container_id],
                    limit=32 * 1024,
                    budget_s=5,
                )
                inspected = json.loads(raw.decode("utf-8"))
                labels = inspected["Config"]["Labels"]
                name = inspected["Name"].removeprefix("/")
                actual_id = inspected["Id"]
                actual_image = inspected["Image"]
                actual_runtime = inspected["HostConfig"]["Runtime"]
                valid = (
                    actual_id == container_id
                    and re.fullmatch(r"sb-ext-[0-9a-f]{32}", name) is not None
                    and isinstance(labels, dict)
                    and labels.get("chatcore.extensions.managed") == "v1"
                    and labels.get("chatcore.extensions.owner") == self.owner_id
                    and labels.get("chatcore.extensions.image") == actual_image
                    and labels.get("chatcore.extensions.runtime") == actual_runtime
                    and actual_runtime in {"runc", "runsc"}
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", actual_image) is not None
                )
                if not valid:
                    raise ValueError
                await self._docker_wait([self.docker, "rm", "-f", container_id], budget_s=5)
            except (OSError, TimeoutError, _OutputLimit):
                raise OwnedContainerRecoveryError("owned container recovery failed") from None
            except Exception:
                raise OwnedContainerRecoveryError(
                    "owned container metadata could not be verified"
                ) from None

    async def _docker_output(self, argv: list[str], *, limit: int, budget_s: float) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            assert proc.stdout is not None
            output = bytearray()
            deadline = time.monotonic() + budget_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=remaining)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    raise _OutputLimit
            remaining = deadline - time.monotonic()
            if remaining <= 0 or await asyncio.wait_for(proc.wait(), timeout=remaining) != 0:
                raise OSError("docker command failed")
            return bytes(output)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1)
                except TimeoutError:
                    pass
            raise

    async def _docker_wait(self, argv: list[str], *, budget_s: float) -> None:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            if await asyncio.wait_for(proc.wait(), timeout=budget_s) != 0:
                raise OSError("docker command failed")
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1)
                except TimeoutError:
                    pass
            raise

    async def run(self, artifact: Artifact, arguments: dict[str, object]) -> RunResult:
        started = time.monotonic()
        name = "sb-ext-" + secrets.token_hex(16)
        self._containers.add(name)
        status, value = "unavailable", None
        try:
            envelope = canonical_json({"source": artifact.source, "arguments": arguments})
            if len(envelope) > 128 * 1024:
                return self._result("invalid", None, started)
            create = await asyncio.create_subprocess_exec(
                *self.create_argv(name),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                assert create.stdout is not None
                create_out = await asyncio.wait_for(create.stdout.read(4097), timeout=10)
                create_status = await asyncio.wait_for(create.wait(), timeout=10)
            except TimeoutError:
                create.kill()
                await asyncio.wait_for(create.wait(), timeout=1)
                return self._result("unavailable", None, started)
            except asyncio.CancelledError:
                create.kill()
                await asyncio.wait_for(create.wait(), timeout=1)
                raise
            if create_status != 0 or len(create_out) > 4096:
                return self._result("unavailable", None, started)
            process = await asyncio.create_subprocess_exec(
                self.docker,
                "start",
                "-ai",
                name,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                assert process.stdin is not None
                process.stdin.write(envelope)
                await process.stdin.drain()
                process.stdin.close()
                assert process.stdout is not None and process.stderr is not None
                tasks = [
                    asyncio.create_task(self._read_stream(process.stdout, process)),
                    asyncio.create_task(self._read_stream(process.stderr, process)),
                    asyncio.create_task(process.wait()),
                ]
                try:
                    out, err, _ = await asyncio.wait_for(
                        asyncio.gather(*tasks), timeout=self.policy.timeout_seconds
                    )
                except _OutputLimit:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=1)
                    return self._result("output_limit", None, started)
                except TimeoutError:
                    self._cancel_tasks(set(tasks))
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=1)
                    return self._result("timeout", None, started)
                if len(out) + len(err) > self.policy.output_bytes:
                    return self._result("output_limit", None, started)
                if process.returncode != 0:
                    if await self._oom_killed(name):
                        return self._result("resource_limit", None, started)
                    return self._result("execution_error", None, started)
                parsed = json.loads(
                    out.decode("utf-8"),
                    object_pairs_hook=_unique_pairs,
                    parse_constant=_reject_constant,
                )
                if not isinstance(parsed, dict) or parsed.get("ok") is not True:
                    return self._result("execution_error", None, started)
                if set(parsed) != {"ok", "value"}:
                    return self._result("execution_error", None, started)
                return self._result("ok", parsed["value"], started)
            except asyncio.CancelledError:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=1)
                raise
            except (TimeoutError, BrokenPipeError):
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=1)
                status = "timeout"
            except _OutputLimit:
                status = "output_limit"
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                status = "execution_error"
            return self._result(status, value, started)
        except (TimeoutError, OSError):
            return self._result("unavailable", None, started)
        finally:
            removed = await self._remove(name)
            if removed:
                self._containers.discard(name)
            elif sys.exception() is None:
                raise SandboxCleanupError("sandbox container cleanup failed")

    async def _read_stream(
        self, stream: asyncio.StreamReader, process: asyncio.subprocess.Process
    ) -> bytes:
        data = bytearray()
        while chunk := await stream.read(8192):
            data.extend(chunk)
            if len(data) > self.policy.output_bytes:
                process.kill()
                raise _OutputLimit
        return bytes(data)

    async def _oom_killed(self, name: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker,
                "inspect",
                "--format",
                "{{.State.OOMKilled}}",
                name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
            return proc.returncode == 0 and out[:8].strip() == b"true"
        except (TimeoutError, OSError):
            return False

    @staticmethod
    def _cancel_tasks(tasks: set[asyncio.Task[object]]) -> None:
        for task in tasks:
            task.cancel()

    async def _remove(self, name: str) -> bool:
        if not re.fullmatch(r"sb-ext-[0-9a-f]{32}", name):
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker,
                "rm",
                "-f",
                name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=3)
            return proc.returncode == 0
        except TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1)
            except TimeoutError:
                pass
            return False
        except OSError:
            return False

    async def aclose(self) -> None:
        for name in tuple(self._containers):
            if await self._remove(name):
                self._containers.discard(name)

    def _result(self, status: str, value: object | None, started: float) -> RunResult:
        return RunResult(
            status,
            value,
            int((time.monotonic() - started) * 1000),
            self.image_id,
            self.policy.digest,
            HARNESS_DIGEST,
        )


def _unique_pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite")
