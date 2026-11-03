from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from chatcore_release_control.broker import BrokerStore
from chatcore_release_control.database_runtime import (
    DatabaseRuntimeError,
    connection_initializer,
    verify_database_connection,
)
from chatcore_release_control.ledger import Decision, ReleaseLedger
from chatcore_release_control.revocation import PostgresReleaseRevocationStore

sys.path.insert(0, str(Path(__file__).parent))
pytest_plugins = ("test_ledger",)
from test_ledger import create_card, make_binding, make_command  # noqa: E402


@pytest.fixture
def maintenance_sources():
    package = Path(__file__).parents[1]
    root = package.parents[1]
    return (
        [
            (package / "src/chatcore_release_control" / name).read_text()
            for name in (
                "broker_schema.sql",
                "bridge_schema.sql",
                "relay_schema.sql",
                "worker_schema.sql",
            )
        ]
        + [
            (
                root
                / "services/release-control/src/chatcore_release_control"
                / "templates"
                / "p10-database-grants.sql.template"
            ).read_text()
        ]
        + [
            (package / "src/chatcore_release_control/revocation_schema.sql").read_text(),
            (
                root
                / "services/release-control/src/chatcore_release_control"
                / "templates"
                / "revocation-grants.sql.template"
            ).read_text(),
        ]
    )


async def test_actual_protected_roles_can_commit_decision_and_dispatch_without_owning_schema(
    ledger_pool, maintenance_sources, isolated_postgres
):
    for source in maintenance_sources:
        await ledger_pool.execute(source)
    database = await ledger_pool.fetchval("SELECT current_database()")
    prefix = "runtime_" + uuid4().hex
    bridge_login = prefix + "_bridge"
    broker_login = prefix + "_broker"
    await ledger_pool.execute(f'CREATE ROLE "{bridge_login}" LOGIN INHERIT')
    await ledger_pool.execute(f'CREATE ROLE "{broker_login}" LOGIN INHERIT')
    await ledger_pool.execute(f'GRANT release_control_bridge TO "{bridge_login}"')
    await ledger_pool.execute(f'GRANT release_control_broker TO "{broker_login}"')
    host = str(isolated_postgres.socket_dir)
    port = isolated_postgres.port
    bridge_pool = await asyncpg.create_pool(
        host=host,
        port=port,
        user=bridge_login,
        database=database,
        min_size=1,
        max_size=3,
        init=connection_initializer("release_control_bridge"),
    )
    broker_pool = await asyncpg.create_pool(
        host=host,
        port=port,
        user=broker_login,
        database=database,
        min_size=1,
        max_size=3,
        init=connection_initializer("release_control_broker"),
    )
    try:
        bridge = ReleaseLedger(bridge_pool)
        assert await PostgresReleaseRevocationStore(bridge_pool).ready() is True
        card = await create_card(bridge, binding=make_binding(owner_id=202))
        result = await bridge.record_decision(make_command(card, decision=Decision.APPROVE))
        assert result.status.value == "queued"
        assert (
            await ledger_pool.fetchval("SELECT count(*) FROM release_control.decision_outbox")
        ) == 1
        broker = ReleaseLedger(broker_pool)
        claim = await broker.claim_next_dispatch(card.target, "protected-worker")
        assert claim.approval_id == card.approval_id
        await broker.acknowledge_dispatch(card.approval_id, fence=claim.fence)
        assert await BrokerStore(broker_pool).schema_ready() is True
        async with bridge_pool.acquire() as connection:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(
                    "UPDATE release_control.broker_release_revocation "
                    "SET recorded_at=clock_timestamp()"
                )
        async with broker_pool.acquire() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM release_control.broker_release_revocation"
                )
                == 0
            )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(
                    "INSERT INTO release_control.broker_release_revocation "
                    "(update_id,approval_id,target,owner_user_id,request_digest,"
                    "receipt_id,recorded_at) "
                    "VALUES (1,$1,'production',7,$2,$3,clock_timestamp())",
                    card.approval_id,
                    b"x" * 32,
                    card.approval_id,
                )
        async with broker_pool.acquire() as connection:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(
                    "UPDATE release_control.release_card SET decision='reject'"
                )
            assert (
                await connection.fetchval(
                    "SELECT pg_has_role(session_user,'release_control_bridge','MEMBER')"
                )
                is False
            )
            assert (
                await connection.fetchval(
                    "SELECT pg_has_role(session_user,'release_control_broker','MEMBER')"
                )
                is True
            )
            assert (
                await connection.fetchval(
                    "SELECT nspowner::regrole::text=session_user "
                    "FROM pg_namespace WHERE nspname='release_control'"
                )
                is False
            )
    finally:
        await bridge_pool.close()
        await broker_pool.close()
        await ledger_pool.execute(f'DROP ROLE "{bridge_login}"')
        await ledger_pool.execute(f'DROP ROLE "{broker_login}"')


async def test_ordinary_app_role_cannot_read_forge_or_alter_control_objects(
    ledger_pool, maintenance_sources, isolated_postgres
):
    for source in maintenance_sources:
        await ledger_pool.execute(source)
    role = "ordinary_" + uuid4().hex
    await ledger_pool.execute(f'CREATE ROLE "{role}" LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB')
    database = await ledger_pool.fetchval("SELECT current_database()")
    connection = await asyncpg.connect(
        host=str(isolated_postgres.socket_dir),
        port=isolated_postgres.port,
        user=role,
        database=database,
        timeout=5,
    )
    try:
        with pytest.raises(DatabaseRuntimeError):
            await verify_database_connection(connection, "release_control_bridge")
        async with connection.transaction():
            for statement in (
                "SELECT * FROM release_control.release_card",
                "INSERT INTO release_control.telegram_update VALUES(1,NULL,NULL,NULL,now())",
                "SELECT * FROM release_control.broker_dispatch_intent",
                "SELECT * FROM release_control.telegram_relay_event",
                "ALTER TABLE release_control.release_card DISABLE TRIGGER ALL",
                "SET LOCAL ROLE release_control_broker",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with connection.transaction():
                        await connection.execute(statement)
    finally:
        await connection.close()
        await ledger_pool.execute(f'DROP ROLE "{role}"')


async def test_runtime_login_cannot_inherit_control_schema_or_table_ownership(
    ledger_pool, maintenance_sources, isolated_postgres
):
    for source in maintenance_sources:
        await ledger_pool.execute(source)
    database = await ledger_pool.fetchval("SELECT current_database()")
    owner_role = "control_owner_" + uuid4().hex
    runtime_login = "runtime_login_" + uuid4().hex
    await ledger_pool.execute(f'CREATE ROLE "{owner_role}" NOLOGIN')
    await ledger_pool.execute(f'CREATE ROLE "{runtime_login}" LOGIN INHERIT')
    await ledger_pool.execute(f'GRANT release_control_bridge TO "{runtime_login}"')
    await ledger_pool.execute(f'ALTER TABLE release_control.release_card OWNER TO "{owner_role}"')
    await ledger_pool.execute(f'GRANT "{owner_role}" TO "{runtime_login}"')
    connection = await asyncpg.connect(
        host=str(isolated_postgres.socket_dir),
        port=isolated_postgres.port,
        user=runtime_login,
        database=database,
        timeout=5,
    )
    try:
        with pytest.raises(DatabaseRuntimeError):
            await verify_database_connection(connection, "release_control_bridge")
    finally:
        await connection.close()
        await ledger_pool.execute(f'REVOKE "{owner_role}" FROM "{runtime_login}"')
        await ledger_pool.execute("ALTER TABLE release_control.release_card OWNER TO CURRENT_USER")
        await ledger_pool.execute(f'DROP ROLE "{runtime_login}"')
        await ledger_pool.execute(f'DROP ROLE "{owner_role}"')
