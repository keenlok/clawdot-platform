"""Root-protected dispatch/poll/report service; schemas are installed separately."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import asyncpg
import httpx

from .bridge_service import _secure_read
from .broker_app import BrokerAppHTTPPost, BrokerAppTokenIssuer
from .broker_worker import BrokerDispatchWorker, BrokerGitHubClient, BrokerWorkerStore, _unique
from .database_runtime import connection_initializer, verify_database_pool
from .ledger import ReleaseLedger, ReleaseStatus


@dataclass(frozen=True, repr=False)
class WorkerServiceConfig:
    database_dsn_file: str
    github_app_id: int
    github_installation_id: int
    github_repository_id: int
    github_private_key_file: str
    targets: tuple[str, ...]
    bridge_socket: str
    bridge_credential_file: str
    worker_id: str
    poll_seconds: int

    @classmethod
    def from_file(cls, path, *, expected_uid=0):
        try:
            value = json.loads(
                _secure_read(os.fspath(path), expected_uid), object_pairs_hook=_unique
            )
            if (
                not isinstance(value, dict)
                or set(value)
                != {
                    "version",
                    "database_dsn_file",
                    "github_app_id",
                    "publication_app_id",
                    "runner_registration_app_id",
                    "github_installation_id",
                    "github_repository_id",
                    "github_private_key_file",
                    "targets",
                    "bridge_socket",
                    "bridge_credential_file",
                    "worker_id",
                    "poll_seconds",
                }
                or type(value["version"]) is not int
                or value["version"] != 1
            ):
                raise ValueError
            for key in (
                "github_app_id",
                "publication_app_id",
                "runner_registration_app_id",
                "github_installation_id",
                "github_repository_id",
            ):
                if type(value[key]) is not int or value[key] <= 0:
                    raise ValueError
            if (
                len(
                    {
                        value["github_app_id"],
                        value["publication_app_id"],
                        value["runner_registration_app_id"],
                    }
                )
                != 3
            ):
                raise ValueError
            for key in ("database_dsn_file", "github_private_key_file", "bridge_credential_file"):
                _secure_read(value[key], expected_uid)
            if (
                not isinstance(value["targets"], list)
                or not 1 <= len(value["targets"]) <= 32
                or any(
                    not isinstance(target, str)
                    or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", target) is None
                    for target in value["targets"]
                )
                or value["targets"] != sorted(set(value["targets"]))
                or not isinstance(value["worker_id"], str)
                or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value["worker_id"]) is None
                or type(value["poll_seconds"]) is not int
                or not 10 <= value["poll_seconds"] <= 60
                or not isinstance(value["bridge_socket"], str)
                or not Path(value["bridge_socket"]).is_absolute()
            ):
                raise ValueError
            return cls(
                **{
                    key: tuple(item) if key == "targets" else item
                    for key, item in value.items()
                    if key not in {"version", "publication_app_id", "runner_registration_app_id"}
                }
            )
        except Exception:
            raise ValueError("worker_configuration_denied") from None

    def __repr__(self):
        return "WorkerServiceConfig([protected])"


class ProtectedBridgeReporter:
    def __init__(self, *, socket_path, credential, transport=None):
        if (
            not isinstance(credential, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{32,4096}", credential) is None
        ):
            raise ValueError("worker_configuration_denied")
        if transport is None:
            path = Path(socket_path)
            info, parent = path.lstat(), path.parent.lstat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o077
                or not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != 0
                or parent.st_mode & 0o022
            ):
                raise ValueError("worker_configuration_denied")
        self._credential = credential
        self._http = httpx.AsyncClient(
            base_url="http://bridge.local",
            transport=transport or httpx.AsyncHTTPTransport(uds=socket_path),
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(10, connect=3),
        )

    def __repr__(self):
        return "ProtectedBridgeReporter(credential=[REDACTED])"

    async def report(self, request_id: UUID, approval_id: UUID):
        try:
            async with asyncio.timeout(15):
                async with self._http.stream(
                    "POST",
                    "/v1/broker/reports",
                    json={"request_id": str(request_id), "approval_id": str(approval_id)},
                    headers={"Authorization": "Bearer " + self._credential},
                ) as response:
                    if response.status_code != 200:
                        raise ValueError
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(8192):
                        if len(raw) + len(chunk) > 8192:
                            raise ValueError
                        raw.extend(chunk)
                result = json.loads(raw, object_pairs_hook=_unique)
                if (
                    not isinstance(result, dict)
                    or set(result) != {"approval_id", "status", "message_id"}
                    or result["approval_id"] != str(approval_id)
                    or result["status"] not in {status.value for status in ReleaseStatus}
                    or type(result["message_id"]) is not int
                    or result["message_id"] <= 0
                ):
                    raise ValueError
        except Exception:
            raise ValueError("report_unavailable") from None

    async def close(self):
        await self._http.aclose()


async def run(config: WorkerServiceConfig, *, once: bool):
    post = BrokerAppHTTPPost()
    pool = None
    reporter = None
    clients = {}
    try:
        dsn = _secure_read(config.database_dsn_file, 0).decode().strip()
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=5,
            timeout=5,
            command_timeout=10,
            init=connection_initializer("release_control_broker"),
        )
        await verify_database_pool(pool, "release_control_broker")
        store = BrokerWorkerStore(pool)
        if not await store.ready() or not await store.broker.schema_ready():
            raise ValueError("worker_configuration_denied")
        issuer = BrokerAppTokenIssuer(
            app_id=config.github_app_id,
            installation_id=config.github_installation_id,
            repository_id=config.github_repository_id,
            protected_key_provider=lambda: _secure_read(config.github_private_key_file, 0),
            http_post=post,
            now=lambda: datetime.now(UTC),
        )
        reporter = ProtectedBridgeReporter(
            socket_path=config.bridge_socket,
            credential=_secure_read(config.bridge_credential_file, 0).decode("ascii").strip(),
        )
        ledger = ReleaseLedger(pool)
        workers = {}
        for target in config.targets:
            state = await store.broker.get_target_state(target)
            if state.policy.repository_id != config.github_repository_id:
                raise ValueError("worker_configuration_denied")
            api = BrokerGitHubClient(
                repository=state.repository,
                repository_id=state.policy.repository_id,
                owner_id=state.policy.owner_id,
                token_provider=lambda: issuer.get_token().access_token,
            )
            clients[target] = api
            workers[target] = BrokerDispatchWorker(
                store=store, ledger=ledger, api=api, reporter=reporter
            )
        while True:
            failed = False
            for target, worker in workers.items():
                try:
                    outcome = await worker.tick(target, config.worker_id)
                    if once:
                        print(json.dumps({"target": target, "status": outcome}, sort_keys=True))
                except Exception:
                    failed = True
                    if once:
                        print(
                            json.dumps(
                                {"target": target, "status": "reconciliation_required"},
                                sort_keys=True,
                            )
                        )
            if once:
                return 2 if failed else 0
            await asyncio.sleep(config.poll_seconds)
    finally:
        for client in clients.values():
            await client.close()
        if reporter is not None:
            await reporter.close()
        if pool is not None:
            await pool.close()
        post.close()


def main(argv=None, *, expected_uid=0):
    parser = argparse.ArgumentParser(prog="chatcore-release-worker")
    parser.add_argument("--config", default="/etc/chatcore-release-control/worker.json")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = WorkerServiceConfig.from_file(args.config, expected_uid=expected_uid)
        if args.check_config:
            print("configuration valid")
            return 0
        if os.geteuid() != 0:
            raise ValueError
        return asyncio.run(run(config, once=args.once))
    except Exception:
        print("worker_unavailable", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
