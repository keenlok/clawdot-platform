from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from chatcore_release_control.ledger import (
    CardNonce,
    Decision,
    DecisionCommand,
    HostReceipt,
    LedgerError,
    ReleaseBinding,
    ReleaseLedger,
    ReleaseStatus,
    RunAttemptBinding,
)
from chatcore_release_control.policy import DeploymentIdentity, MergeEvidence
from chatcore_release_control.telegram_ingress import TelegramIngress

_PG_BIN = Path(os.environ.get("PG_BIN", "/opt/homebrew/bin"))
# Anchored to the wall clock: record_decision defaults to datetime.now() and cards expire after 24h.
_T0 = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
_SAFE_ENV_KEYS = {"PATH", "HOME", "LANG", "LC_ALL"}
_CARD_NONCES: dict[UUID, CardNonce] = {}


@dataclass(frozen=True)
class PostgresServer:
    data_dir: Path
    socket_dir: Path
    env: Mapping[str, str]
    port: int
    user: str


@pytest.fixture(scope="session")
def isolated_postgres() -> PostgresServer:
    """Run only a temporary Unix-socket server with all inherited PG settings removed."""
    assert (_PG_BIN / "initdb").is_file()
    assert (_PG_BIN / "pg_ctl").is_file()
    root = Path(tempfile.mkdtemp(prefix="p05-", dir="/tmp"))
    os.chmod(root, 0o700)
    data_dir = root / "data"
    socket_dir = root / "socket"
    socket_dir.mkdir(mode=0o700)
    env = {
        "PATH": f"{_PG_BIN}:/usr/bin:/bin",
        "HOME": str(root),
        "LANG": "C",
        "LC_ALL": "C",
    }
    assert set(env) == _SAFE_ENV_KEYS
    port = 54873
    db_user = getpass.getuser()
    server_started = False
    try:
        init = subprocess.run(
            [
                str(_PG_BIN / "initdb"),
                "-D",
                str(data_dir),
                f"--username={db_user}",
                "--no-locale",
                "--encoding=UTF8",
                "--auth-local=trust",
                "--auth-host=reject",
            ],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert init.returncode == 0, init.stderr
        start_options = (
            "-c fsync=on -c full_page_writes=on -c synchronous_commit=on "
            "-c listen_addresses='' "
            f"-c unix_socket_directories='{socket_dir}' "
            "-c unix_socket_permissions=0700 "
            f"-c port={port}"
        )
        started = subprocess.run(
            [
                str(_PG_BIN / "pg_ctl"),
                "-D",
                str(data_dir),
                "-l",
                str(root / "server.log"),
                "-o",
                start_options,
                "-w",
                "start",
            ],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert started.returncode == 0, started.stderr
        server_started = True
        yield PostgresServer(data_dir, socket_dir, env, port, db_user)
    finally:
        status = subprocess.run(
            [str(_PG_BIN / "pg_ctl"), "-D", str(data_dir), "status"],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if status.returncode == 0:
            stopped = subprocess.run(
                [str(_PG_BIN / "pg_ctl"), "-D", str(data_dir), "-m", "fast", "-w", "stop"],
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if server_started:
                assert stopped.returncode == 0, stopped.stderr
        shutil.rmtree(root, ignore_errors=True)


@pytest_asyncio.fixture
async def ledger_pool(
    isolated_postgres: PostgresServer, request: pytest.FixtureRequest
) -> AsyncIterator[asyncpg.Pool]:
    database_name = f"ledger_{uuid4().hex}"
    admin = await asyncpg.connect(
        host=str(isolated_postgres.socket_dir),
        port=isolated_postgres.port,
        user=isolated_postgres.user,
        database="postgres",
        timeout=5,
    )
    database_created = False
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        database_created = True
        installer = await asyncpg.connect(
            host=str(isolated_postgres.socket_dir),
            port=isolated_postgres.port,
            user=isolated_postgres.user,
            database=database_name,
            timeout=5,
        )
        schema_path = Path(__file__).parents[1] / "src" / "chatcore_release_control" / "schema.sql"
        try:
            if getattr(request, "param", True):
                await installer.execute(schema_path.read_text())
        finally:
            await installer.close()
        pool = await asyncpg.create_pool(
            host=str(isolated_postgres.socket_dir),
            port=isolated_postgres.port,
            user=isolated_postgres.user,
            database=database_name,
            min_size=1,
            max_size=8,
            timeout=5,
        )
        yield pool
    finally:
        try:
            if pool is not None:
                await pool.close()
            if database_created:
                await admin.execute(f'DROP DATABASE "{database_name}"')
        finally:
            await admin.close()


def make_binding(**overrides: object) -> ReleaseBinding:
    fields: dict[str, object] = {
        "repository_id": 101,
        "owner_id": 202,
        "pr_number": 37,
        "target": "production",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "head_tree": "c" * 40,
        "deployed_identity": DeploymentIdentity(
            git_sha="d" * 40,
            image_digest="sha256:" + "e" * 64,
            config_generation=4,
            config_digest="f" * 64,
            policy_digest="1" * 64,
            target_sequence=8,
        ),
        "policy_digest": "1" * 64,
        "review_bundle": {
            "deployed_to_head": [{"path": "packages/plugin-web/src/plugin.py"}],
            "reviewed": True,
            "complete_delta_reviewed": True,
        },
    }
    fields.update(overrides)
    return ReleaseBinding(**fields)  # type: ignore[arg-type]


def make_run(**overrides: object) -> RunAttemptBinding:
    values: dict[str, object] = {
        "repository_id": 101,
        "run_id": 5001,
        "attempt": 1,
        "workflow_ref": ".github/workflows/release.yml@refs/heads/main",
        "workflow_sha": "a" * 40,
        "check_run_id": 6001,
    }
    values.update(overrides)
    return RunAttemptBinding(**values)  # type: ignore[arg-type]


def make_merge(**overrides: object) -> tuple[MergeEvidence, str]:
    values: dict[str, object] = {
        "merge_commit": "f" * 40,
        "parents": ("a" * 40, "b" * 40),
        "tree": "9" * 40,
    }
    values.update(overrides)
    return MergeEvidence(**values), "9" * 40  # type: ignore[arg-type]


def make_host_receipt(
    card: object,
    *,
    fence: int,
    status: ReleaseStatus = ReleaseStatus.HEALTHY,
    merge_commit: str = "f" * 40,
    observed_identity: DeploymentIdentity | None = None,
    operation_id: UUID | None = None,
    receipt_id: str = "host-receipt-1",
    observed_at: datetime = _T0 + timedelta(hours=2),
) -> HostReceipt:
    if observed_identity is None and status is ReleaseStatus.HEALTHY:
        observed_identity = DeploymentIdentity(
            git_sha=merge_commit,
            image_digest="sha256:" + "8" * 64,
            config_generation=9,
            config_digest="7" * 64,
            policy_digest="1" * 64,
            target_sequence=card.sequence,
        )
    return HostReceipt(
        approval_id=card.approval_id,
        target=card.target,
        operation_id=operation_id or uuid4(),
        receipt_id=receipt_id,
        fence=fence,
        status=status,
        merge_commit=merge_commit,
        observed_identity=observed_identity,
        observed_at=observed_at,
    )


def make_host_operation(
    card: object,
    *,
    operation_id: UUID | None = None,
    baseline_overrides: dict[str, object] | None = None,
    payload_overrides: dict[str, object] | None = None,
):
    from chatcore_release_control.ledger import HostOperationBinding

    baseline = DeploymentIdentity(**card.binding["deployed_identity"])
    if baseline_overrides:
        baseline = DeploymentIdentity(**{**asdict(baseline), **baseline_overrides})
    payload: dict[str, object] = {
        "git_sha": card.merge_evidence.merge_commit,
        "image_digest": "sha256:" + "8" * 64,
        "config_generation": baseline.config_generation,
        "config_digest": baseline.config_digest,
        "policy_digest": card.binding["policy_digest"],
        "target_sequence": card.sequence,
    }
    payload.update(payload_overrides or {})
    return HostOperationBinding(
        operation_id=operation_id or uuid4(),
        baseline=baseline,
        payload=DeploymentIdentity(**payload),
    )


def make_command(
    card: object,
    *,
    update_id: int = 9001,
    decision: Decision = Decision.APPROVE,
    **overrides: object,
) -> DecisionCommand:
    command: dict[str, object] = {
        "update_id": update_id,
        "approval_id": card.approval_id,
        "owner_user_id": 404,
        "chat_id": 404,
        "message_id": 7001,
        "nonce": _CARD_NONCES[card.approval_id].value,
        "decision": decision,
        "binding_digest": card.binding_digest,
    }
    command.update(overrides)
    return DecisionCommand(**command)  # type: ignore[arg-type]


async def create_card(ledger: ReleaseLedger, **kwargs: object):
    nonce = kwargs.pop("nonce", None) or CardNonce()
    defaults: dict[str, object] = {
        "binding": make_binding(),
        "owner_user_id": 404,
        "chat_id": 404,
        "message_id": 7001,
        "nonce": nonce,
        "now": _T0,
    }
    defaults.update(kwargs)
    card = await ledger.create_card(**defaults)  # type: ignore[arg-type]
    _CARD_NONCES[card.approval_id] = nonce
    return card


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger_pool", [False], indirect=True)
async def test_ledger_initializes_no_schema_and_schema_is_explicitly_installed(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    assert not hasattr(ledger, "install_schema")
    async with ledger_pool.acquire() as connection:
        assert (
            await connection.fetchval("SELECT to_regclass('release_control.release_card')") is None
        )
        assert await connection.fetchval("SHOW listen_addresses") == ""
        assert await connection.fetchval("SELECT inet_server_addr() IS NULL") is True
        assert await connection.fetchval("SHOW unix_socket_permissions") == "0700"


@pytest.mark.asyncio
async def test_default_privileges_deny_public_execute_on_future_schema_functions(
    ledger_pool,
) -> None:
    async with ledger_pool.acquire() as connection:
        await connection.execute(
            "CREATE FUNCTION release_control.future_test_function() RETURNS integer "
            "LANGUAGE sql AS 'SELECT 1'"
        )
        assert (
            await connection.fetchval(
                "SELECT has_function_privilege('public', "
                "'release_control.future_test_function()', 'EXECUTE')"
            )
            is False
        )


@pytest.mark.asyncio
async def test_card_binds_private_owner_card_nonce_review_bundle_and_fixed_expiry(
    ledger_pool,
) -> None:
    ledger = ReleaseLedger(ledger_pool)
    now = _T0
    binding = make_binding()
    card = await create_card(ledger, binding=binding, now=now)
    assert card.status is ReleaseStatus.AWAITING_APPROVAL
    assert card.sequence >= binding.deployed_identity.target_sequence + 1
    assert card.expires_at == now + timedelta(hours=24)
    assert card.binding_digest == binding.digest
    assert card.approval_id

    async with ledger_pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT nonce_sha256, binding, binding_sha256 "
            "FROM release_control.release_card WHERE id=$1",
            card.approval_id,
        )
    assert bytes(row["nonce_sha256"]) != b"n" * 48
    assert json.loads(row["binding"]) == json.loads(binding.to_json())
    stored_deployment = json.loads(row["binding"])["deployed_identity"]
    assert stored_deployment == {
        "git_sha": binding.deployed_identity.git_sha,
        "image_digest": binding.deployed_identity.image_digest,
        "config_generation": binding.deployed_identity.config_generation,
        "config_digest": binding.deployed_identity.config_digest,
        "policy_digest": binding.deployed_identity.policy_digest,
        "target_sequence": binding.deployed_identity.target_sequence,
    }
    assert bytes(row["binding_sha256"]).hex() == binding.digest
    assert b"n" * 48 not in bytes(row["nonce_sha256"])


@pytest.mark.asyncio
async def test_release_binding_snapshot_is_stable_after_input_dict_mutation() -> None:
    bundle = {
        "deployed_to_head": [],
        "reviewed": True,
        "complete_delta_reviewed": True,
    }
    binding = make_binding(review_bundle=bundle)
    digest = binding.digest
    canonical = binding.to_json()
    bundle["complete_delta_reviewed"] = False
    exposed = binding.review_bundle
    exposed["complete_delta_reviewed"] = False
    assert binding.digest == digest
    assert binding.review_bundle["complete_delta_reviewed"] is True
    assert binding.to_json() == canonical
    assert json.loads(binding.to_json())["review_bundle"]["complete_delta_reviewed"] is True


def test_release_binding_rejects_noncanonical_review_numbers() -> None:
    with pytest.raises(LedgerError) as error:
        make_binding(review_bundle={"complete_delta_reviewed": True, "score": 0.5})
    assert error.value.code == "invalid_binding"


def test_card_nonce_is_generated_and_never_accepts_caller_selected_value() -> None:
    from chatcore_release_control.ledger import CardNonce

    nonce = CardNonce()
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,52}", nonce.value)
    assert nonce.value not in repr(nonce)
    with pytest.raises(TypeError):
        CardNonce("caller-chosen-nonce")


@pytest.mark.asyncio
async def test_create_card_rejects_raw_caller_selected_nonce(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    with pytest.raises(LedgerError) as error:
        await ledger.create_card(
            binding=make_binding(),
            owner_user_id=404,
            chat_id=404,
            message_id=7001,
            nonce="caller-chosen-nonce",  # type: ignore[arg-type]
        )
    assert error.value.code == "invalid_card"


@pytest.mark.asyncio
async def test_duplicate_generated_nonce_digest_is_rejected_by_postgres(
    ledger_pool, monkeypatch
) -> None:
    import chatcore_release_control.ledger as ledger_module
    from chatcore_release_control.ledger import CardNonce

    monkeypatch.setattr(ledger_module.secrets, "token_urlsafe", lambda _size: "x" * 48)
    first_nonce = CardNonce()
    second_nonce = CardNonce()
    ledger = ReleaseLedger(ledger_pool)
    await create_card(ledger, message_id=7001, nonce=first_nonce)
    with pytest.raises(LedgerError) as error:
        await create_card(ledger, message_id=7002, nonce=second_nonce)
    assert error.value.code == "storage_error"
    with pytest.raises(LedgerError) as error:
        make_binding(review_bundle={"complete_delta_reviewed": True, "text": chr(0xD800)})
    assert error.value.code == "invalid_binding"


@pytest.mark.asyncio
async def test_database_rejects_mutating_card_binding_or_decision(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger)
    async with ledger_pool.acquire() as connection:
        with pytest.raises(asyncpg.PostgresError):
            await connection.execute(
                "UPDATE release_control.release_card SET status='queued' WHERE id=$1",
                card.approval_id,
            )
        with pytest.raises(asyncpg.PostgresError):
            await connection.execute(
                "UPDATE release_control.release_card SET binding_sha256=$2 WHERE id=$1",
                card.approval_id,
                b"x" * 32,
            )
    await ledger.record_decision(make_command(card))
    async with ledger_pool.acquire() as connection:
        with pytest.raises(asyncpg.PostgresError):
            await connection.execute(
                "UPDATE release_control.release_card SET decision='reject' WHERE id=$1",
                card.approval_id,
            )


@pytest.mark.asyncio
async def test_concurrent_card_creation_serializes_target_sequences(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    now = _T0
    cards = await asyncio.gather(
        create_card(ledger, binding=make_binding(pr_number=37), message_id=7001, now=now),
        create_card(ledger, binding=make_binding(pr_number=38), message_id=7002, now=now),
        create_card(ledger, binding=make_binding(pr_number=39), message_id=7003, now=now),
    )
    assert sorted(card.sequence for card in cards) == [9, 10, 11]
    assert all(card.sequence > 8 for card in cards)
    assert (
        await ledger_pool.fetchval(
            "SELECT next_sequence FROM release_control.target_queue WHERE target='production'"
        )
        == 12
    )


@pytest.mark.asyncio
async def test_trusted_callback_lookup_resolves_exact_private_card_message(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger)
    resolved = await ledger.get_card_by_message(404, 7001)
    assert resolved.approval_id == card.approval_id
    assert resolved.binding_digest == card.binding_digest
    with pytest.raises(LedgerError) as error:
        await ledger.get_card_by_message(404, 7002)
    assert error.value.code == "invalid_card"
    for invalid in ((-404, 7001), (404, 0), (True, 7001)):
        with pytest.raises(LedgerError) as error:
            await ledger.get_card_by_message(*invalid)
        assert error.value.code == "invalid_card"


@pytest.mark.asyncio
async def test_ingress_real_ledger_callback_replay_and_ordinary_relay(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    nonce = CardNonce()
    card = await create_card(ledger, nonce=nonce)
    events: list[str] = []

    async def acknowledge(query_id: str) -> None:
        assert query_id == "query-71001"
        async with ledger_pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT decision, nonce_consumed_at FROM release_control.release_card WHERE id=$1",
                card.approval_id,
            )
            outbox_count = await connection.fetchval(
                "SELECT count(*) FROM release_control.decision_outbox WHERE approval_id=$1",
                card.approval_id,
            )
        assert row["decision"] == "approve"
        assert row["nonce_consumed_at"] is not None
        assert outbox_count == 1
        events.append("ack")

    async def relay(_body: bytes, _signature: str) -> None:
        events.append("relay")

    ingress = TelegramIngress(
        webhook_secret="synthetic-webhook-secret-32-bytes__",
        relay_key=b"synthetic-distinct-relay-key-32bytes",
        current_owner=lambda: 404,
        ledger=ledger,
        acknowledge=acknowledge,
        relay=relay,
    )
    callback = json.dumps(
        {
            "update_id": 71001,
            "callback_query": {
                "id": "query-71001",
                "from": {"id": 404},
                "data": f"release:a:{nonce.value}",
                "message": {
                    "message_id": 7001,
                    "chat": {"id": 404, "type": "private"},
                },
            },
        }
    ).encode()
    assert await ingress.handle(callback, secret_header="synthetic-webhook-secret-32-bytes__") == (
        "release"
    )
    assert events == ["ack"]

    # Telegram may retry the exact update; the durable update and outbox stay singular.
    assert await ingress.handle(callback, secret_header="synthetic-webhook-secret-32-bytes__") == (
        "release"
    )
    assert events == ["ack", "ack"]
    async with ledger_pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM release_control.telegram_update WHERE update_id=71001"
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM release_control.decision_outbox WHERE approval_id=$1",
                card.approval_id,
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT decision FROM release_control.release_card WHERE id=$1",
                card.approval_id,
            )
            == "approve"
        )

    ordinary = b'{"update_id":71002,"message":{"text":"ordinary"}}'
    assert await ingress.handle(ordinary, secret_header="synthetic-webhook-secret-32-bytes__") == (
        "ordinary"
    )
    assert events == ["ack", "ack", "relay"]
    async with ledger_pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM release_control.telegram_update WHERE update_id=71002"
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM release_control.decision_outbox WHERE approval_id=$1",
                card.approval_id,
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT decision FROM release_control.release_card WHERE id=$1",
                card.approval_id,
            )
            == "approve"
        )


@pytest.mark.asyncio
async def test_approve_records_explicit_choice_and_one_dispatch_outbox(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger)
    result = await ledger.record_decision(make_command(card))
    assert result.decision is Decision.APPROVE
    assert result.status is ReleaseStatus.QUEUED
    async with ledger_pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT decision, nonce_consumed_at, status "
            "FROM release_control.release_card WHERE id=$1",
            card.approval_id,
        )
        outbox = await connection.fetch(
            "SELECT approval_id, event_type, state "
            "FROM release_control.decision_outbox WHERE approval_id=$1",
            card.approval_id,
        )
    assert row["decision"] == "approve"
    assert row["nonce_consumed_at"] is not None
    assert row["status"] == "queued"
    assert len(outbox) == 1
    assert outbox[0]["event_type"] == "dispatch_release"


@pytest.mark.asyncio
async def test_reject_is_explicit_and_never_creates_dispatch_work(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger)
    result = await ledger.record_decision(make_command(card, decision=Decision.REJECT))
    assert result.decision is Decision.REJECT
    assert result.status is ReleaseStatus.REJECTED
    async with ledger_pool.acquire() as connection:
        count = await connection.fetchval(
            "SELECT count(*) FROM release_control.decision_outbox WHERE approval_id=$1",
            card.approval_id,
        )
    assert count == 0


@pytest.mark.asyncio
async def test_same_update_and_same_decision_retries_return_recorded_result(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger)
    command = make_command(card)
    first, concurrent = await asyncio.gather(
        ledger.record_decision(command), ledger.record_decision(command)
    )
    assert concurrent == first
    replay = await ledger.record_decision(command)
    second_tap = await ledger.record_decision(make_command(card, update_id=9002))
    assert replay == first
    assert second_tap.decision is Decision.APPROVE
    assert second_tap.status is ReleaseStatus.QUEUED
    async with ledger_pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM release_control.decision_outbox WHERE approval_id=$1",
                card.approval_id,
            )
            == 1
        )


@pytest.mark.asyncio
async def test_update_replay_mismatch_and_conflicting_tap_are_rejected(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger)
    await ledger.record_decision(make_command(card))
    with pytest.raises(LedgerError) as replay_error:
        await ledger.record_decision(make_command(card, update_id=9001, decision=Decision.REJECT))
    assert replay_error.value.code == "replay_mismatch"
    with pytest.raises(LedgerError) as decision_error:
        await ledger.record_decision(make_command(card, update_id=9002, decision=Decision.REJECT))
    assert decision_error.value.code == "decision_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"owner_user_id": 405}, "forbidden"),
        ({"chat_id": 405}, "forbidden"),
        ({"message_id": 7002}, "forbidden"),
        ({"nonce": "x" * 48}, "forbidden"),
        ({"binding_digest": "0" * 64}, "binding_mismatch"),
    ],
)
async def test_wrong_card_identity_nonce_or_binding_fails_closed(
    ledger_pool, overrides, expected
) -> None:
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger)
    with pytest.raises(LedgerError) as error:
        await ledger.record_decision(make_command(card, **overrides))
    assert error.value.code == expected


@pytest.mark.asyncio
async def test_expired_card_cannot_be_decided_and_expiry_is_persisted(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger)
    with pytest.raises(LedgerError) as error:
        await ledger.record_decision(
            make_command(card), now=card.expires_at + timedelta(microseconds=1)
        )
    assert error.value.code == "expired"
    assert await ledger.expire_pending(now=card.expires_at + timedelta(seconds=1)) == 1
    assert (await ledger.get_card(card.approval_id)).status is ReleaseStatus.EXPIRED


@pytest.mark.asyncio
async def test_outbox_insert_failure_rolls_back_decision_nonce_and_update(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger)
    async with ledger_pool.acquire() as connection:
        await connection.execute(
            """
            CREATE FUNCTION release_control.reject_outbox() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic failure'; END $$
            """
        )
        await connection.execute(
            """
            CREATE TRIGGER reject_outbox BEFORE INSERT ON release_control.decision_outbox
            FOR EACH ROW EXECUTE FUNCTION release_control.reject_outbox()
            """
        )
    with pytest.raises(LedgerError) as error:
        await ledger.record_decision(make_command(card))
    assert error.value.code == "storage_error"
    async with ledger_pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT decision, nonce_consumed_at, status "
            "FROM release_control.release_card WHERE id=$1",
            card.approval_id,
        )
        updates = await connection.fetchval(
            "SELECT count(*) FROM release_control.telegram_update WHERE update_id=9001"
        )
        outbox = await connection.fetchval(
            "SELECT count(*) FROM release_control.decision_outbox WHERE approval_id=$1",
            card.approval_id,
        )
    assert row["decision"] is None
    assert row["nonce_consumed_at"] is None
    assert row["status"] == "awaiting_approval"
    assert updates == 0
    assert outbox == 0


@pytest.mark.asyncio
async def test_claims_are_target_ordered_fenced_and_stale_ack_is_denied(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    first = await create_card(ledger, message_id=7001)
    second_nonce = CardNonce()
    second = await create_card(
        ledger,
        binding=make_binding(pr_number=38),
        message_id=7002,
        nonce=second_nonce,
    )
    await ledger.record_decision(make_command(first, update_id=9101))
    await ledger.record_decision(make_command(second, update_id=9102, message_id=7002))
    now = _T0 + timedelta(hours=1)
    claim = await ledger.claim_next_dispatch("production", "broker-a", now=now)
    assert claim.approval_id == first.approval_id
    assert claim.sequence == first.sequence
    assert claim.fence == 1
    assert await ledger.claim_next_dispatch("production", "broker-b", now=now) is None
    with pytest.raises(LedgerError) as error:
        await ledger.acknowledge_dispatch(first.approval_id, fence=claim.fence + 1, now=now)
    assert error.value.code == "stale_fence"
    await ledger.acknowledge_dispatch(first.approval_id, fence=claim.fence, now=now)
    await ledger.acknowledge_dispatch(first.approval_id, fence=claim.fence, now=now)
    assert await ledger.claim_next_dispatch("production", "broker-b", now=now) is None
    with pytest.raises(LedgerError) as missing_run:
        await ledger.advance_status(
            first.approval_id,
            ReleaseStatus.MERGE_AUTHORIZED,
            fence=claim.fence,
        )
    assert missing_run.value.code == "invalid_transition"
    run_attempt = await ledger.bind_run_attempt(
        first.approval_id, fence=claim.fence, run=make_run()
    )
    assert run_attempt == make_run()
    with pytest.raises(LedgerError) as mismatched_run:
        await ledger.bind_run_attempt(
            first.approval_id,
            fence=claim.fence,
            run=make_run(run_id=5002),
        )
    assert mismatched_run.value.code == "binding_mismatch"
    await ledger.advance_status(
        first.approval_id,
        ReleaseStatus.MERGE_AUTHORIZED,
        fence=claim.fence,
    )
    with pytest.raises(LedgerError) as generic_merge:
        await ledger.advance_status(
            first.approval_id,
            ReleaseStatus.MERGED_VERIFIED,
            fence=claim.fence,
        )
    assert generic_merge.value.code == "invalid_transition"
    evidence, expected_tree = make_merge()
    merged = await ledger.record_merge_result(
        first.approval_id,
        fence=claim.fence,
        evidence=evidence,
        expected_merge_tree=expected_tree,
    )
    assert merged.status is ReleaseStatus.MERGED_VERIFIED
    assert merged.merge_evidence == evidence
    assert merged.run_attempt == run_attempt
    async with ledger_pool.acquire() as connection:
        with pytest.raises(asyncpg.PostgresError):
            await connection.execute(
                "UPDATE release_control.release_card SET run_id=5002 WHERE id=$1",
                first.approval_id,
            )
        with pytest.raises(asyncpg.PostgresError):
            await connection.execute(
                "UPDATE release_control.release_card SET merge_tree=$2 WHERE id=$1",
                first.approval_id,
                "8" * 40,
            )
    host_operation = make_host_operation(merged)
    with pytest.raises(LedgerError) as missing_host_operation:
        await ledger.advance_status(first.approval_id, ReleaseStatus.DEPLOYING, fence=claim.fence)
    assert missing_host_operation.value.code == "invalid_transition"
    for invalid_operation in (
        make_host_operation(merged, baseline_overrides={"config_generation": 5}),
        make_host_operation(merged, payload_overrides={"config_generation": 5}),
        make_host_operation(merged, payload_overrides={"config_digest": "2" * 64}),
        make_host_operation(merged, payload_overrides={"policy_digest": "2" * 64}),
        make_host_operation(merged, payload_overrides={"git_sha": "3" * 40}),
    ):
        with pytest.raises(LedgerError) as invalid_binding:
            await ledger.bind_host_operation(
                first.approval_id, fence=claim.fence, operation=invalid_operation
            )
        assert invalid_binding.value.code == "binding_mismatch"

    assert (
        await ledger.bind_host_operation(
            first.approval_id, fence=claim.fence, operation=host_operation
        )
        == host_operation
    )
    assert (await ledger.get_card(first.approval_id)).host_operation == host_operation

    with pytest.raises(LedgerError) as wrong_fence:
        await ledger.bind_host_operation(
            first.approval_id, fence=claim.fence + 1, operation=host_operation
        )
    assert wrong_fence.value.code == "stale_fence"
    with pytest.raises(LedgerError) as missing_host_operation:
        await ledger.advance_status(first.approval_id, ReleaseStatus.HEALTHY, fence=claim.fence)
    assert missing_host_operation.value.code == "invalid_transition"
    await ledger.advance_status(first.approval_id, ReleaseStatus.DEPLOYING, fence=claim.fence)
    with pytest.raises(LedgerError) as generic_host:
        await ledger.advance_status(
            first.approval_id,
            ReleaseStatus.HEALTHY,
            fence=claim.fence,
        )
    assert generic_host.value.code == "invalid_transition"
    receipt_wrong_operation = make_host_receipt(first, fence=claim.fence)
    with pytest.raises(LedgerError) as operation_mismatch:
        await ledger.record_host_receipt(
            first.approval_id,
            receipt_wrong_operation,
            now=receipt_wrong_operation.observed_at,
        )
    assert operation_mismatch.value.code == "binding_mismatch"
    with pytest.raises(LedgerError) as wrong_payload_receipt:
        await ledger.record_host_receipt(
            first.approval_id,
            make_host_receipt(
                first,
                fence=claim.fence,
                operation_id=host_operation.operation_id,
                observed_identity=DeploymentIdentity(
                    **{
                        **asdict(host_operation.payload),
                        "image_digest": "sha256:" + "7" * 64,
                    }
                ),
            ),
            now=_T0 + timedelta(hours=2),
        )
    assert wrong_payload_receipt.value.code == "binding_mismatch"
    async with ledger_pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT active_fence FROM release_control.target_queue WHERE target='production'"
            )
            == claim.fence
        )
        with pytest.raises(asyncpg.PostgresError):
            await connection.execute(
                "UPDATE release_control.release_card SET host_payload_identity='{}'::jsonb "
                "WHERE id=$1",
                first.approval_id,
            )

    receipt = make_host_receipt(
        first,
        fence=claim.fence,
        operation_id=host_operation.operation_id,
        observed_identity=host_operation.payload,
    )
    stored_receipt = await ledger.record_host_receipt(
        first.approval_id,
        receipt,
        now=receipt.observed_at,
    )
    assert stored_receipt == receipt
    assert await ledger.get_host_receipt(first.approval_id) == receipt
    async with ledger_pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT active_fence FROM release_control.target_queue WHERE target='production'"
            )
            is None
        )
    assert (
        await ledger.record_host_receipt(
            first.approval_id,
            receipt,
            now=receipt.observed_at + timedelta(seconds=1),
        )
        == receipt
    )
    assert await ledger.get_host_receipt(first.approval_id) == receipt
    async with ledger_pool.acquire() as connection:
        with pytest.raises(asyncpg.PostgresError):
            await connection.execute(
                "UPDATE release_control.release_card SET host_receipt_id='forged' WHERE id=$1",
                first.approval_id,
            )
    assert (await ledger.get_card(first.approval_id)).status is ReleaseStatus.HEALTHY
    second_claim = await ledger.claim_next_dispatch("production", "broker-b", now=now)
    assert second_claim.approval_id == second.approval_id
    assert second_claim.fence == 2


@pytest.mark.asyncio
async def test_expired_dispatch_lease_reclaims_same_operation_with_new_fence(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger)
    await ledger.record_decision(make_command(card))
    now = _T0 + timedelta(hours=1)
    old_claim = await ledger.claim_next_dispatch(
        "production", "broker-a", now=now, lease=timedelta(seconds=10)
    )
    new_claim = await ledger.claim_next_dispatch(
        "production", "broker-b", now=now + timedelta(seconds=11)
    )
    assert new_claim.approval_id == old_claim.approval_id == card.approval_id
    assert new_claim.fence == old_claim.fence + 1
    with pytest.raises(LedgerError) as error:
        await ledger.acknowledge_dispatch(
            card.approval_id,
            fence=old_claim.fence,
            now=now + timedelta(seconds=11),
        )
    assert error.value.code == "stale_fence"
    await ledger.acknowledge_dispatch(
        card.approval_id,
        fence=new_claim.fence,
        now=now + timedelta(seconds=11),
    )


@pytest.mark.asyncio
async def test_unexpected_merge_parent_or_tree_is_persisted_as_merged_blocked(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger)
    await ledger.record_decision(make_command(card))
    now = _T0 + timedelta(hours=1)
    claim = await ledger.claim_next_dispatch("production", "broker-a", now=now)
    await ledger.acknowledge_dispatch(card.approval_id, fence=claim.fence, now=now)
    await ledger.bind_run_attempt(card.approval_id, fence=claim.fence, run=make_run())
    await ledger.advance_status(card.approval_id, ReleaseStatus.MERGE_AUTHORIZED, fence=claim.fence)
    wrong_evidence = MergeEvidence(
        merge_commit="f" * 40,
        parents=("b" * 40, "a" * 40),
        tree="8" * 40,
    )
    result = await ledger.record_merge_result(
        card.approval_id,
        fence=claim.fence,
        evidence=wrong_evidence,
        expected_merge_tree="9" * 40,
    )
    assert result.status is ReleaseStatus.MERGED_BLOCKED
    assert result.merge_evidence == wrong_evidence
    assert result.expected_merge_tree == "9" * 40
    with pytest.raises(LedgerError) as error:
        await ledger.advance_status(
            card.approval_id,
            ReleaseStatus.DEPLOYING,
            fence=claim.fence,
        )
    assert error.value.code == "invalid_transition"
    with pytest.raises(LedgerError) as retry_error:
        await ledger.record_merge_result(
            card.approval_id,
            fence=claim.fence,
            evidence=make_merge()[0],
            expected_merge_tree="9" * 40,
        )
    assert retry_error.value.code == "binding_mismatch"


@pytest.mark.asyncio
async def test_recovery_failed_target_stays_fenced_from_later_queue_items(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    failed = await create_card(ledger, message_id=7001)
    queued_nonce = CardNonce()
    queued = await create_card(
        ledger,
        binding=make_binding(pr_number=38),
        message_id=7002,
        nonce=queued_nonce,
    )
    await ledger.record_decision(make_command(failed, update_id=9201))
    await ledger.record_decision(make_command(queued, update_id=9202, message_id=7002))
    now = _T0 + timedelta(hours=1)
    claim = await ledger.claim_next_dispatch("production", "broker-a", now=now)
    await ledger.acknowledge_dispatch(failed.approval_id, fence=claim.fence, now=now)
    await ledger.bind_run_attempt(failed.approval_id, fence=claim.fence, run=make_run())
    await ledger.advance_status(
        failed.approval_id, ReleaseStatus.MERGE_AUTHORIZED, fence=claim.fence
    )
    evidence, expected_tree = make_merge()
    merged_failed = await ledger.record_merge_result(
        failed.approval_id,
        fence=claim.fence,
        evidence=evidence,
        expected_merge_tree=expected_tree,
    )
    host_operation = make_host_operation(merged_failed)
    await ledger.bind_host_operation(
        failed.approval_id, fence=claim.fence, operation=host_operation
    )
    for status in (ReleaseStatus.DEPLOYING, ReleaseStatus.RESTORING):
        await ledger.advance_status(failed.approval_id, status, fence=claim.fence)
    failure_receipt = make_host_receipt(
        failed,
        fence=claim.fence,
        status=ReleaseStatus.RECOVERY_FAILED,
        observed_identity=None,
        operation_id=host_operation.operation_id,
    )
    await ledger.record_host_receipt(
        failed.approval_id,
        failure_receipt,
        now=failure_receipt.observed_at + timedelta(minutes=1),
    )
    assert await ledger.claim_next_dispatch("production", "broker-b", now=now) is None


@pytest.mark.asyncio
async def test_invalid_status_transition_rejected_with_fixed_error(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger)
    with pytest.raises(LedgerError) as error:
        await ledger.advance_status(card.approval_id, ReleaseStatus.HEALTHY)
    assert error.value.code == "invalid_transition"


@pytest.mark.asyncio
async def test_queued_release_can_be_invalidated_only_before_dispatch_claim(ledger_pool) -> None:
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger)
    await ledger.record_decision(make_command(card))
    await ledger.advance_status(card.approval_id, ReleaseStatus.INVALIDATED)
    async with ledger_pool.acquire() as connection:
        state = await connection.fetchval(
            "SELECT state FROM release_control.decision_outbox WHERE approval_id=$1",
            card.approval_id,
        )
    assert state == "canceled"
    assert await ledger.claim_next_dispatch("production", "broker-a") is None

    claimed_nonce = CardNonce()
    claimed = await create_card(
        ledger,
        binding=make_binding(pr_number=38),
        message_id=7002,
        nonce=claimed_nonce,
    )
    await ledger.record_decision(make_command(claimed, update_id=9302, message_id=7002))
    await ledger.claim_next_dispatch("production", "broker-a")
    with pytest.raises(LedgerError) as error:
        await ledger.advance_status(claimed.approval_id, ReleaseStatus.INVALIDATED)
    assert error.value.code in {"invalid_transition", "stale_fence"}


@pytest.mark.asyncio
async def test_ordinary_synthetic_role_cannot_read_or_write_ledger(ledger_pool) -> None:
    role = f"ordinary_{uuid4().hex}"
    assert re.fullmatch(r"ordinary_[a-f0-9]+", role)
    async with ledger_pool.acquire() as connection:
        await connection.execute(f'CREATE ROLE "{role}" NOLOGIN')
        try:
            await connection.execute(f'SET ROLE "{role}"')
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetchval("SELECT count(*) FROM release_control.release_card")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(
                    "INSERT INTO release_control.telegram_update "
                    "(update_id, approval_id, request_sha256, result) "
                    "VALUES (1, $1, $2, '{}'::jsonb)",
                    uuid4(),
                    b"x" * 32,
                )
        finally:
            await connection.execute("RESET ROLE")
            await connection.execute(f'DROP ROLE "{role}"')


@pytest.mark.asyncio
async def test_database_acquisition_failures_use_fixed_errors_without_dsn_text() -> None:
    class BrokenPool:
        def acquire(self):
            raise RuntimeError("postgres://ledger-user:ledger-secret@database.invalid/private")

    ledger = ReleaseLedger(BrokenPool())  # type: ignore[arg-type]
    binding = make_binding()
    card_id = uuid4()
    command = DecisionCommand(
        update_id=99,
        approval_id=card_id,
        owner_user_id=404,
        chat_id=404,
        message_id=7001,
        nonce="n" * 48,
        decision=Decision.APPROVE,
        binding_digest=binding.digest,
    )
    operations = (
        ledger.create_card(
            binding=binding,
            owner_user_id=404,
            chat_id=404,
            message_id=7001,
            nonce=CardNonce(),
        ),
        ledger.record_decision(command),
        ledger.claim_next_dispatch("production", "broker-a"),
        ledger.acknowledge_dispatch(card_id, fence=1),
        ledger.advance_status(card_id, ReleaseStatus.INVALIDATED),
        ledger.get_card(card_id),
    )
    for operation in operations:
        with pytest.raises(LedgerError) as error:
            await operation
        assert error.value.code == "storage_error"
        assert "ledger-secret" not in str(error.value)


async def test_temporary_postgres_uses_durable_database_settings(ledger_pool):
    async with ledger_pool.acquire() as connection:
        settings = await connection.fetchrow(
            "SELECT current_setting('fsync') AS fsync, "
            "current_setting('full_page_writes') AS full_page_writes, "
            "current_setting('synchronous_commit') AS synchronous_commit"
        )
    assert dict(settings) == {
        "fsync": "on",
        "full_page_writes": "on",
        "synchronous_commit": "on",
    }
