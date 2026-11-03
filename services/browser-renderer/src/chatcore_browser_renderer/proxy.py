from __future__ import annotations

import asyncio
import base64
import contextlib
import secrets
from dataclasses import dataclass

from chatcore.security.net_guard import BlockedURL, FetchError

from .budget import RenderBudget
from .policy import EgressPolicy

MAX_HEADER_BYTES = 8 * 1024


@dataclass(slots=True)
class ProxyLease:
    token: str
    budget: RenderBudget


class EgressProxy:
    def __init__(self, policy: EgressPolicy | None = None, *, header_timeout_s: float = 5) -> None:
        self.policy = policy or EgressPolicy()
        self.header_timeout_s = header_timeout_s
        self.server: asyncio.AbstractServer | None = None
        self.leases: dict[str, RenderBudget] = {}
        self.handlers: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._budget_tasks: dict[int, set[asyncio.Task[None]]] = {}
        self._budget_writers: dict[int, set[asyncio.StreamWriter]] = {}

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    def register(self, budget: RenderBudget) -> ProxyLease:
        token = secrets.token_urlsafe(24)
        self.leases[token] = budget
        return ProxyLease(token, budget)

    def unregister(self, lease: ProxyLease) -> None:
        self.leases.pop(lease.token, None)
        self._abort_budget(lease.budget)

    def _abort_budget(
        self, budget: RenderBudget, *, keep_task: asyncio.Task[None] | None = None
    ) -> None:
        key = id(budget)
        for writer in tuple(self._budget_writers.get(key, ())):
            writer.close()
        current = asyncio.current_task()
        for task in tuple(self._budget_tasks.get(key, ())):
            if task is not current and task is not keep_task:
                task.cancel()

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in tuple(self._writers):
            writer.close()
        for task in tuple(self.handlers):
            task.cancel()
        if self.handlers:
            await asyncio.gather(*self.handlers, return_exceptions=True)

    async def _send(self, writer: asyncio.StreamWriter, status: str) -> None:
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        with contextlib.suppress(Exception):
            await writer.drain()

    async def _send_auth_challenge(self, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
            b'Proxy-Authenticate: Basic realm="render"\r\n'
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        with contextlib.suppress(Exception):
            await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self.handlers.add(task)
        self._writers.add(writer)
        upstream: asyncio.StreamWriter | None = None
        budget: RenderBudget | None = None
        opened = False
        authority = ""
        try:
            try:
                async with asyncio.timeout(self.header_timeout_s):
                    header = await reader.readuntil(b"\r\n\r\n")
            except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                await self._send(writer, "400 Bad Request")
                return
            if len(header) > MAX_HEADER_BYTES:
                await self._send(writer, "400 Bad Request")
                return
            lines = header.decode("latin-1").split("\r\n")
            request = lines[0].split(" ") if lines else []
            if len(request) != 3 or request[2] != "HTTP/1.1":
                await self._send(writer, "400 Bad Request")
                return
            if request[0] != "CONNECT":
                await self._send(writer, "403 Forbidden")
                return
            if any(not line or line[0].isspace() or ":" not in line for line in lines[1:-2]):
                await self._send(writer, "400 Bad Request")
                return
            authority = request[1]
            if not authority or any(c in authority for c in " /?#\r\n"):
                await self._send(writer, "400 Bad Request")
                return
            if getattr(reader, "_buffer", b""):
                await self._send(writer, "400 Bad Request")
                return
            token = self._proxy_token(lines[1:])
            budget = self.leases.get(token or "")
            if budget is None:
                await self._send_auth_challenge(writer)
                return
            if task:
                self._budget_tasks.setdefault(id(budget), set()).add(task)
            self._budget_writers.setdefault(id(budget), set()).add(writer)
            if not await budget.open_tunnel():
                await self._send(writer, "403 Forbidden")
                self._abort_budget(budget)
                return
            opened = True
            try:
                _target, upstream_reader, upstream = await self.policy.open_tunnel(authority)
            except (BlockedURL, FetchError, ValueError):
                budget.refuse(authority)
                await self._send(writer, "403 Forbidden")
                return
            except OSError:
                await self._send(writer, "502 Bad Gateway")
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await self._tunnel(reader, writer, upstream_reader, upstream, budget, task)
        except (ConnectionError, asyncio.CancelledError):
            if (
                isinstance(asyncio.current_task(), asyncio.Task)
                and asyncio.current_task().cancelling()
            ):
                raise
        finally:
            if opened and budget:
                await budget.close_tunnel()
            if upstream:
                upstream.close()
                with contextlib.suppress(Exception):
                    await upstream.wait_closed()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self._writers.discard(writer)
            if task:
                self.handlers.discard(task)
                if budget:
                    tasks = self._budget_tasks.get(id(budget))
                    if tasks:
                        tasks.discard(task)
                        if not tasks:
                            self._budget_tasks.pop(id(budget), None)
                if budget:
                    writers = self._budget_writers.get(id(budget))
                    if writers:
                        writers.discard(writer)
                        if not writers:
                            self._budget_writers.pop(id(budget), None)

    @staticmethod
    def _proxy_token(headers: list[str]) -> str | None:
        for line in headers:
            key, sep, value = line.partition(":")
            if sep and key.strip().lower() == "proxy-authorization":
                scheme, _, encoded = value.strip().partition(" ")
                if scheme.lower() != "basic":
                    return None
                try:
                    decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                except (ValueError, UnicodeError):
                    return None
                username, sep, credential = decoded.partition(":")
                return username if sep and credential == "render" else None
        return None

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        budget: RenderBudget,
        handler_task: asyncio.Task[None] | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        last_activity = loop.time()
        activity = asyncio.Event()

        async def copy(source: asyncio.StreamReader, dest: asyncio.StreamWriter) -> None:
            while True:
                data = await source.read(65536)
                if not data:
                    return
                nonlocal last_activity
                last_activity = loop.time()
                activity.set()
                if not await budget.charge(len(data)):
                    self._abort_budget(budget, keep_task=handler_task)
                    return
                dest.write(data)
                await dest.drain()

        async def idle_watchdog() -> None:
            while True:
                activity.clear()
                remaining = budget.idle_timeout_s - (loop.time() - last_activity)
                if remaining <= 0:
                    return
                try:
                    await asyncio.wait_for(activity.wait(), remaining)
                except TimeoutError:
                    if loop.time() - last_activity >= budget.idle_timeout_s:
                        return

        jobs = [
            asyncio.create_task(copy(client_reader, upstream_writer)),
            asyncio.create_task(copy(upstream_reader, client_writer)),
            asyncio.create_task(idle_watchdog()),
        ]
        try:
            await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
