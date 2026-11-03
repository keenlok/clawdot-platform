from __future__ import annotations

import asyncio
import getpass
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from chatcore_release_control.bridge_http import BridgeHTTPError, TelegramBridgeASGI
from chatcore_release_control.durable_relay import DurableRelay, RelayError
from chatcore_release_control.telegram_bridge import TelegramBridge
from chatcore_release_control.telegram_ingress import verify_relay_body

_PG_BIN = Path(os.environ.get("PG_BIN", "/opt/homebrew/bin"))


@dataclass(frozen=True)
class _Server:
    root: Path
    socket: Path
    port: int
    user: str
    env: Mapping[str, str]


@pytest.fixture(scope="module")
def _postgres() -> _Server:
    root = Path(tempfile.mkdtemp(prefix="p06-relay-", dir="/tmp"))
    os.chmod(root, 0o700)
    data, socket = root / "data", root / "socket"
    socket.mkdir(mode=0o700)
    env = {"PATH": f"{_PG_BIN}:/usr/bin:/bin", "HOME": str(root), "LANG": "C", "LC_ALL": "C"}
    user, port = getpass.getuser(), 54981
    started = False
    try:
        result = subprocess.run(
            [
                str(_PG_BIN / "initdb"),
                "-D",
                str(data),
                f"--username={user}",
                "--no-locale",
                "--encoding=UTF8",
                "--auth-local=trust",
                "--auth-host=reject",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        options = (
            "-F -c listen_addresses='' "
            f"-c unix_socket_directories='{socket}' "
            f"-c unix_socket_permissions=0700 -c port={port}"
        )
        result = subprocess.run(
            [
                str(_PG_BIN / "pg_ctl"),
                "-D",
                str(data),
                "-l",
                str(root / "server.log"),
                "-o",
                options,
                "-w",
                "start",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        started = True
        yield _Server(root, socket, port, user, env)
    finally:
        if started:
            stopped = subprocess.run(
                [str(_PG_BIN / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert stopped.returncode == 0, stopped.stderr
        shutil.rmtree(root, ignore_errors=True)


@pytest_asyncio.fixture
async def relay_pool(_postgres: _Server) -> AsyncIterator[asyncpg.Pool]:
    name = f"relay_{uuid4().hex}"
    admin = await asyncpg.connect(
        host=str(_postgres.socket), port=_postgres.port, user=_postgres.user, database="postgres"
    )
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        connection = await asyncpg.connect(
            host=str(_postgres.socket), port=_postgres.port, user=_postgres.user, database=name
        )
        try:
            schema = (
                Path(__file__).parents[1] / "src" / "chatcore_release_control" / "relay_schema.sql"
            )
            await connection.execute(schema.read_text())
        finally:
            await connection.close()
        pool = await asyncpg.create_pool(
            host=str(_postgres.socket),
            port=_postgres.port,
            user=_postgres.user,
            database=name,
            min_size=1,
            max_size=4,
        )
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_exact_update_replay_returns_stable_id_but_mismatched_payload_rejects(
    relay_pool,
) -> None:
    relay = DurableRelay(relay_pool)
    first = await relay.enqueue("sb_bot", 41, {"update_id": 41, "message": {"text": "hello"}})
    repeated = await relay.enqueue("sb_bot", 41, {"update_id": 41, "message": {"text": "hello"}})
    assert repeated == first
    with pytest.raises(RelayError) as error:
        await relay.enqueue("sb_bot", 41, {"update_id": 41, "message": {"text": "different"}})
    assert error.value.code == "replay_mismatch"
    with pytest.raises(RelayError) as error:
        await relay.enqueue("sb_bot", 43, {"update_id": 42})
    assert error.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_relay_table_is_not_publicly_readable(relay_pool) -> None:
    async with relay_pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT has_table_privilege('public', "
                "'release_control.telegram_relay_event', 'SELECT')"
            )
            is False
        )


@pytest.mark.asyncio
async def test_authenticated_asgi_ingress_persists_stable_update_before_client_claim(
    relay_pool,
) -> None:
    class FakeBot:
        async def call(self, *args, **kwargs):
            return {"ok": True}

        async def download_file(self, *args, **kwargs):
            return b""

    bridge = TelegramBridge(
        token="synthetic-token",
        authenticate=lambda credential: "bot" if credential == "client" else None,
        allowed_chats={"bot": frozenset({"-100"})},
        transport=FakeBot(),
        load_protected_messages=lambda: (),
    )
    relay = DurableRelay(relay_pool)
    app = TelegramBridgeASGI(
        bridge,
        relay_store=relay,
        relay_key=b"r" * 32,
        audiences={"bot": frozenset({"-100"})},
    )
    update = {"update_id": 71, "message": {"chat": {"id": -100}, "text": "hello"}}
    raw = json.dumps(update, separators=(",", ":")).encode()
    signature = hmac.new(b"r" * 32, raw, hashlib.sha256).hexdigest()
    await app.accept_relay(raw, signature)
    first = await relay.claim("bot")
    assert len(first) == 1 and first[0].update_id == 71
    await relay.ack("bot", [first[0].relay_id])
    # An exact webhook retry resolves to the same persisted row and does not requeue it.
    assert verify_relay_body(raw, signature, b"r" * 32)["update_id"] == 71
    await app.accept_relay(raw, signature)
    assert await relay.claim("bot") == []
    changed = {"update_id": 71, "message": {"chat": {"id": -100}, "text": "changed"}}
    changed_raw = json.dumps(changed, separators=(",", ":")).encode()
    changed_sig = hmac.new(b"r" * 32, changed_raw, hashlib.sha256).hexdigest()
    with pytest.raises(BridgeHTTPError) as error:
        await app.accept_relay(changed_raw, changed_sig)
    assert error.value.code == "relay_unavailable"


@pytest.mark.asyncio
async def test_claim_lease_retries_at_least_once_and_ack_is_principal_scoped(relay_pool) -> None:
    relay = DurableRelay(relay_pool)
    now = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
    relay_id = await relay.enqueue("sb_bot", 42, {"update_id": 42}, now=now)
    first = await relay.claim("sb_bot", now=now, lease_seconds=15)
    assert [event.relay_id for event in first] == [relay_id]
    assert first[0].update_id == 42 and first[0].payload == {"update_id": 42}
    assert await relay.claim("sb_bot", now=now + timedelta(seconds=14), lease_seconds=15) == []
    restarted = DurableRelay(relay_pool)
    retry = await restarted.claim("sb_bot", now=now + timedelta(seconds=15), lease_seconds=15)
    assert [event.relay_id for event in retry] == [relay_id]
    with pytest.raises(RelayError) as error:
        await relay.ack("other_client", [relay_id], now=now + timedelta(seconds=16))
    assert error.value.code == "invalid_relay_event"
    await relay.ack("sb_bot", [relay_id], now=now + timedelta(seconds=16))
    assert await relay.claim("sb_bot", now=now + timedelta(seconds=40)) == []


@pytest.mark.asyncio
async def test_concurrent_claimers_never_receive_the_same_active_lease(relay_pool) -> None:
    relay = DurableRelay(relay_pool)
    await relay.enqueue("sb_bot", 61, {"update_id": 61, "message": {"text": "x"}})
    results = await asyncio.gather(
        relay.claim("sb_bot"),
        DurableRelay(relay_pool).claim("sb_bot"),
    )
    assert sorted(len(rows) for rows in results) == [0, 1]


@pytest.mark.asyncio
async def test_relay_bounds_pending_count_and_payload_bytes(relay_pool) -> None:
    relay = DurableRelay(relay_pool, max_pending_per_principal=1)
    await relay.enqueue("sb_bot", 50, {"update_id": 50, "ok": True})
    with pytest.raises(RelayError) as error:
        await relay.enqueue("sb_bot", 51, {"update_id": 51, "ok": True})
    assert error.value.code == "relay_full"
    with pytest.raises(RelayError) as error:
        await relay.enqueue("other", 52, {"update_id": 52, "text": "x" * (1024 * 1024)})
    assert error.value.code == "payload_too_large"
    assert "postgres" not in repr(error.value).lower()
