"""Postgres ledger integration coverage; skips if the Phase 0 test service is unreachable."""

import asyncio
import csv
import io
import json
import os
import uuid
from datetime import UTC, date
from decimal import Decimal
from importlib import resources

import asyncpg
import pytest
from chatcore.events import ChatRef, User, UserRef
from chatcore.plugin import CommandContext
from chatcore.store import migrations
from chatcore.store.pg import PgDatabase
from chatcore.testing import GROUP, FakePlatform, memory_stores
from chatcore.tools.registry import ToolContext
from chatcore_expenses.balances import ledger_balances
from chatcore_expenses.export import build_export
from chatcore_expenses.plugin import ExpensesPlugin
from chatcore_expenses.repository import MemoryLedgerRepo, PostgresLedgerRepo


async def _active_trip(ledger, chat):
    active = await ledger.trips(chat, statuses=("active",))
    return active[0] if len(active) == 1 else None


pytestmark = pytest.mark.integration
DSN = os.environ.get("TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test")


@pytest.mark.asyncio
async def test_postgres_ledger_migration_transactions_and_chat_scope():
    schema = f"expense_{uuid.uuid4().hex[:12]}"
    admin = None
    pool = None
    try:
        admin = await asyncpg.connect(DSN, timeout=3)
        await admin.fetchval("SELECT 1")
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        if admin is not None:
            await admin.close()
        pytest.skip(f"Postgres not available: {type(exc).__name__}")
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        DSN,
        min_size=1,
        max_size=2,
        timeout=3,
        server_settings={"search_path": schema},
    )

    db = PgDatabase(pool)
    repo = PostgresLedgerRepo(db)
    chat_a = GROUP
    chat_b = ChatRef(GROUP.platform, "expenses-other")
    alice = UserRef(GROUP.platform, "alice")
    bob = UserRef(GROUP.platform, "bob")
    try:
        await migrations.apply(
            db, [("chatcore_expenses", resources.files("chatcore_expenses.migrations"))]
        )

        trip = {
            "name": "Taipei",
            "countries": ["TW"],
            "currencies": ["TWD"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        }
        await repo.commit(chat_a, "start_trip", trip, actor=alice)
        active = await _active_trip(repo, chat_a)
        update = {
            "trip_id": active["id"],
            "name": None,
            "add_countries": ["Hong Kong"],
            "remove_countries": None,
            "add_currencies": None,
            "remove_currencies": None,
            "settle_mode": None,
            "settle_currency": None,
        }
        await repo.commit(chat_a, "update_trip", update, actor=alice)
        active = await _active_trip(repo, chat_a)
        assert active["countries"] == ["TW", "Hong Kong"]
        assert active["currencies"] == ["TWD", "HKD"]
        with pytest.raises(ValueError, match="trip changed or ended"):
            await repo.commit(chat_b, "update_trip", update, actor=alice)
        with pytest.raises(asyncpg.UniqueViolationError):
            await repo.commit(chat_a, "start_trip", trip, actor=alice)
        assert await _active_trip(repo, chat_b) is None

        expense_payload = {
            "description": "tea",
            "amount": 100,
            "currency": "TWD",
            "paid_by": alice.key,
            "created_by": alice.key,
            "spent_on": date.today().isoformat(),
            "trip_id": active["id"],
            "shares": {bob.key: 100},
            "fx_rate_to_settle": None,
        }
        stale_expense = expense_payload | {
            "trip_settle_mode": "convert",
            "trip_settle_currency": "SGD",
        }
        await repo.commit(chat_a, "update_trip", update | {"settle_currency": "HKD"}, actor=alice)
        with pytest.raises(ValueError, match="trip's settle settings changed"):
            await repo.commit(chat_a, "add_expense", stale_expense, actor=alice)
        await repo.commit(chat_a, "update_trip", update | {"settle_currency": "SGD"}, actor=alice)
        stale_fx = {
            "trip_id": active["id"],
            "trip_settle_mode": "convert",
            "trip_settle_currency": "SGD",
            "currency": "TWD",
            "rate": "0.18",
            "expense_ids": [],
        }
        await repo.commit(chat_a, "update_trip", update | {"settle_currency": "HKD"}, actor=alice)
        with pytest.raises(ValueError, match="trip's settle settings changed"):
            await repo.commit(chat_a, "set_fx_rate", stale_fx, actor=alice)
        await repo.commit(chat_a, "update_trip", update | {"settle_currency": "SGD"}, actor=alice)
        await repo.commit(chat_a, "add_expense", expense_payload, actor=alice)
        expenses = await repo.expenses(chat_a, trip_id=active["id"])
        expense = expenses[0]
        with pytest.raises(ValueError, match="TWD is used by expenses or settlements"):
            await repo.commit(
                chat_a,
                "update_trip",
                update | {"remove_currencies": ["TWD"]},
                actor=alice,
            )
        with pytest.raises(ValueError, match="can't change after expenses or settlements"):
            await repo.commit(
                chat_a,
                "update_trip",
                update | {"settle_currency": "HKD"},
                actor=alice,
            )
        assert expense["shares"] == [{"user_key": bob.key, "amount": 100}]
        assert await repo.get_expense(chat_a, expense["id"][:8]) is not None
        assert await repo.get_expense(chat_b, expense["id"][:8]) is None
        assert await repo.expenses(chat_b) == []

        await repo.commit(
            chat_a,
            "set_fx_rate",
            {
                "trip_id": active["id"],
                "currency": "TWD",
                "rate": "0.04",
                "expense_ids": [expense["id"]],
            },
            actor=alice,
        )
        assert await repo.fx_override(chat_a, active["id"], "TWD", "SGD") == Decimal("0.04")
        assert (await repo.expenses(chat_a, trip_id=active["id"]))[0][
            "fx_rate_to_settle"
        ] == Decimal("0.04")

        await repo.commit(
            chat_a,
            "settle_up",
            {
                "from_user": bob.key,
                "to_user": alice.key,
                "amount": 4,
                "currency": "SGD",
                "trip_id": active["id"],
            },
            actor=alice,
        )
        settlement = await repo.settlements(chat_a, trip_id=active["id"])
        assert len(settlement) == 1 and settlement[0]["from_user"] == bob.key

        to_delete = expense_payload | {"description": "delete me", "fx_rate_to_settle": "0.04"}
        await repo.commit(chat_a, "add_expense", to_delete, actor=alice)
        deleted_candidate = (await repo.expenses(chat_a, trip_id=active["id"]))[0]
        await repo.commit(
            chat_a, "delete_expense", {"expense_id": deleted_candidate["id"]}, actor=alice
        )
        assert all(row["description"] != "delete me" for row in await repo.expenses(chat_a))

        await repo.commit(
            chat_b,
            "add_expense",
            expense_payload | {"description": "only B", "trip_id": None},
            actor=alice,
        )
        assert [row["description"] for row in await repo.expenses(chat_b)] == ["only B"]
        assert [
            row["description"] for row in await repo.expenses(chat_b, general_only=True, limit=None)
        ] == ["only B"]
        assert all(row["description"] != "only B" for row in await repo.expenses(chat_a))

        await repo.commit(
            chat_a,
            "end_trip",
            {
                "trip_id": active["id"],
                "expected_status": "active",
                "summary": "proposal-time summary",
            },
            actor=alice,
        )
        assert await _active_trip(repo, chat_a) is None

        rollback_payload = expense_payload | {
            "description": "must rollback",
            "trip_id": None,
            "shares": {alice.key: 101, bob.key: -1},
        }
        with pytest.raises(asyncpg.CheckViolationError):
            await repo.commit(chat_a, "add_expense", rollback_payload, actor=alice)
        assert all(row["description"] != "must rollback" for row in await repo.expenses(chat_a))

        await repo.purge_chat(chat_a)
        assert await repo.trip(chat_a) is None
        assert await repo.expenses(chat_a) == []
        assert await repo.settlements(chat_a) == []
        assert [row["description"] for row in await repo.expenses(chat_b)] == ["only B"]
    finally:
        await db.close()
        if admin is not None:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await admin.close()


@pytest.mark.asyncio
async def test_postgres_trip_members_and_undoable_expense_events():
    schema = f"expense_events_{uuid.uuid4().hex[:12]}"
    admin = None
    db = None
    try:
        admin = await asyncpg.connect(DSN, timeout=3)
        await admin.fetchval("SELECT 1")
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        if admin is not None:
            await admin.close()
        pytest.skip(f"Postgres not available: {type(exc).__name__}")
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        DSN, min_size=1, max_size=2, timeout=3, server_settings={"search_path": schema}
    )
    db = PgDatabase(pool)
    repo = PostgresLedgerRepo(db)
    chat = ChatRef("fake", f"events-{uuid.uuid4().hex[:8]}")
    other = ChatRef("fake", f"events-other-{uuid.uuid4().hex[:8]}")
    alice, bob = UserRef("fake", "alice"), UserRef("fake", "bob")
    try:
        await migrations.apply(
            db, [("chatcore_expenses", resources.files("chatcore_expenses.migrations"))]
        )
        await repo.commit(
            chat,
            "start_trip",
            {
                "name": "KL",
                "countries": ["MY"],
                "currencies": ["MYR"],
                "settle_mode": "per_currency",
                "settle_currency": "SGD",
                "members": [alice.key, bob.key],
            },
            actor=alice,
        )
        trip = await repo.trip(chat, "KL")
        assert trip["members"] == [alice.key, bob.key]
        assert await repo.trip(other, trip["id"]) is None
        await repo.commit(
            chat,
            "update_trip_members",
            {
                "trip_id": trip["id"],
                "add": [alice.key],
                "remove": [bob.key],
            },
            actor=alice,
        )
        assert [m["user_key"] for m in await repo.trip_members(chat, trip["id"])] == [alice.key]
        payload = {
            "description": "Lunch",
            "amount": 3000,
            "currency": "MYR",
            "paid_by": alice.key,
            "created_by": alice.key,
            "spent_on": "2026-10-31",
            "trip_id": trip["id"],
            "shares": {alice.key: 1500, bob.key: 1500},
            "fx_rate_to_settle": None,
        }
        await repo.commit(chat, "add_expense", payload, actor=alice)
        expense = (await repo.expenses(chat, trip_id=trip["id"]))[0]
        before = {
            "description": "Lunch",
            "amount": 3000,
            "currency": "MYR",
            "paid_by": alice.key,
            "spent_on": "2026-10-31",
            "trip_id": trip["id"],
            "fx_rate_to_settle": None,
            "shares": {alice.key: 1500, bob.key: 1500},
        }
        after = before | {"amount": 4000, "shares": {alice.key: 2000, bob.key: 2000}}
        await repo.commit(
            chat,
            "update_expense",
            {
                "expense_id": expense["id"],
                "expected": before,
                "after": after,
            },
            actor=alice,
        )
        await repo.commit(chat, "undo_expense_change", {}, actor=alice)
        assert (await repo.expenses(chat, trip_id=trip["id"]))[0]["amount"] == 3000
        await repo.commit(chat, "undo_expense_change", {}, actor=alice)
        assert await repo.expenses(chat, trip_id=trip["id"]) == []
        await repo.commit(chat, "restore_expense", {"expense_id": expense["id"]}, actor=alice)
        assert len(await repo.expenses(chat, trip_id=trip["id"])) == 1
        await repo.commit(chat, "undo_expense_change", {}, actor=alice)
        assert await repo.expenses(chat, trip_id=trip["id"]) == []
        # Every change is undone now; undo entries themselves are never undone.
        with pytest.raises(ValueError, match="no expense change"):
            await repo.commit(chat, "undo_expense_change", {}, actor=alice)
        await repo.commit(chat, "restore_expense", {"expense_id": expense["id"]}, actor=alice)
        await repo.commit(chat, "delete_expense", {"expense_id": expense["id"]}, actor=alice)
        await repo.commit(chat, "undo_expense_change", {}, actor=alice)
        assert len(await repo.expenses(chat, trip_id=trip["id"])) == 1
        with pytest.raises(ValueError, match="no expense change"):
            await repo.commit(other, "undo_expense_change", {}, actor=alice)
        await repo.commit(chat, "delete_expense", {"expense_id": expense["id"]}, actor=alice)
        assert await repo.get_expense(chat, expense["id"]) is None
        assert (await repo.get_expense(chat, expense["id"], include_deleted=True))["deleted_at"]
        with pytest.raises(ValueError, match="Expense not found"):
            await repo.commit(other, "restore_expense", {"expense_id": expense["id"]}, actor=alice)
        await repo.commit(chat, "restore_expense", {"expense_id": expense["id"]}, actor=alice)
        assert len(await repo.expenses(chat, trip_id=trip["id"])) == 1

        await repo.commit(chat, "delete_expense", {"expense_id": expense["id"]}, actor=alice)
        await repo.commit(
            chat,
            "update_trip",
            {
                "trip_id": trip["id"],
                "name": None,
                "add_countries": None,
                "remove_countries": None,
                "add_currencies": None,
                "remove_currencies": None,
                "settle_mode": None,
                "settle_currency": "USD",
            },
            actor=alice,
        )
        events_before = await db.fetchval(
            "SELECT count(*) FROM expense_events WHERE chat_key=$1 AND expense_id=$2",
            chat.key,
            uuid.UUID(expense["id"]),
        )
        delete_event = await db.fetchrow(
            """SELECT id,undone_at FROM expense_events WHERE chat_key=$1 AND expense_id=$2
               AND action='delete' ORDER BY id DESC LIMIT 1""",
            chat.key,
            uuid.UUID(expense["id"]),
        )
        with pytest.raises(ValueError, match="settle settings changed since"):
            await repo.commit(chat, "undo_expense_change", {}, actor=alice)
        assert (
            await db.fetchval(
                "SELECT count(*) FROM expense_events WHERE chat_key=$1 AND expense_id=$2",
                chat.key,
                uuid.UUID(expense["id"]),
            )
            == events_before
        )
        undone_at = await db.fetchval(
            "SELECT undone_at FROM expense_events WHERE id=$1", delete_event["id"]
        )
        assert delete_event["undone_at"] is None and undone_at is None
        assert await repo.get_expense(chat, expense["id"]) is None
        with pytest.raises(ValueError, match="settle settings changed since"):
            await repo.commit(chat, "restore_expense", {"expense_id": expense["id"]}, actor=alice)

        await repo.commit(
            chat,
            "start_trip",
            {
                "name": "Tokyo",
                "countries": ["JP"],
                "currencies": ["JPY"],
                "settle_mode": "per_currency",
                "settle_currency": "SGD",
            },
            actor=alice,
        )
        tokyo = await repo.trip(chat, "Tokyo")
        move_payload = payload | {
            "description": "Moved meal",
            "trip_id": trip["id"],
            "shares": {alice.key: 3000},
            "trip_settle_mode": "per_currency",
            "trip_settle_currency": "USD",
        }
        await repo.commit(chat, "add_expense", move_payload, actor=alice)
        moved = next(row for row in await repo.expenses(chat) if row["description"] == "Moved meal")
        move_before = {
            "description": "Moved meal",
            "amount": 3000,
            "currency": "MYR",
            "paid_by": alice.key,
            "spent_on": "2026-10-31",
            "trip_id": trip["id"],
            "fx_rate_to_settle": None,
            "shares": {alice.key: 3000},
        }
        move_after = move_before | {"trip_id": tokyo["id"]}
        await repo.commit(
            chat,
            "update_expense",
            {
                "expense_id": moved["id"],
                "expected": move_before,
                "after": move_after,
                "trip_settle_mode": tokyo["settle_mode"],
                "trip_settle_currency": tokyo["settle_currency"],
            },
            actor=alice,
        )
        await repo.commit(
            chat,
            "end_trip",
            {"trip_id": trip["id"], "expected_status": "active"},
            actor=alice,
        )
        with pytest.raises(ValueError, match="trip is closed"):
            await repo.commit(chat, "undo_expense_change", {}, actor=alice)
        bob_before = move_after | {"description": "Moved meal"}
        await repo.commit(
            chat,
            "update_expense",
            {
                "expense_id": moved["id"],
                "expected": bob_before,
                "after": bob_before | {"description": "Changed by Bob"},
                "trip_settle_mode": tokyo["settle_mode"],
                "trip_settle_currency": tokyo["settle_currency"],
            },
            actor=bob,
            admin=True,
        )
        with pytest.raises(ValueError, match="changed again after that"):
            await repo.commit(chat, "undo_expense_change", {}, actor=alice)
    finally:
        await db.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_postgres_update_waiting_for_expense_lock_reads_committed_shares():
    schema = f"expense_lock_{uuid.uuid4().hex[:12]}"
    admin = None
    db = None
    try:
        admin = await asyncpg.connect(DSN, timeout=3)
        await admin.fetchval("SELECT 1")
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        if admin is not None:
            await admin.close()
        pytest.skip(f"Postgres not available: {type(exc).__name__}")
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        DSN, min_size=2, max_size=2, timeout=3, server_settings={"search_path": schema}
    )
    db = PgDatabase(pool)
    repo = PostgresLedgerRepo(db)
    chat, alice, bob = (
        ChatRef("fake", f"lock-{uuid.uuid4().hex[:8]}"),
        UserRef("fake", "alice"),
        UserRef("fake", "bob"),
    )
    try:
        await migrations.apply(
            db, [("chatcore_expenses", resources.files("chatcore_expenses.migrations"))]
        )
        payload = {
            "description": "Dinner",
            "amount": 100,
            "currency": "SGD",
            "paid_by": alice.key,
            "created_by": alice.key,
            "spent_on": "2026-10-31",
            "trip_id": None,
            "shares": {alice.key: 100},
            "fx_rate_to_settle": None,
        }
        await repo.commit(chat, "add_expense", payload, actor=alice)
        expense = (await repo.expenses(chat))[0]
        expected = {
            key: payload[key]
            for key in (
                "description",
                "amount",
                "currency",
                "paid_by",
                "spent_on",
                "trip_id",
                "fx_rate_to_settle",
            )
        } | {"shares": {alice.key: 100}}
        async with pool.acquire() as holder:
            tx = holder.transaction()
            await tx.start()
            await holder.fetchval(
                "SELECT id FROM expenses WHERE chat_key=$1 AND id=$2 FOR UPDATE",
                chat.key,
                uuid.UUID(expense["id"]),
            )
            await holder.execute(
                "UPDATE expense_shares SET amount=90 WHERE expense_id=$1 AND user_key=$2",
                uuid.UUID(expense["id"]),
                alice.key,
            )
            await holder.execute(
                "INSERT INTO expense_shares(expense_id,user_key,amount) VALUES($1,$2,10)",
                uuid.UUID(expense["id"]),
                bob.key,
            )
            waiting = asyncio.create_task(
                repo.commit(
                    chat,
                    "update_expense",
                    {
                        "expense_id": expense["id"],
                        "expected": expected,
                        "after": expected | {"description": "Updated dinner"},
                    },
                    actor=alice,
                )
            )
            await asyncio.sleep(0.05)
            assert not waiting.done()
            await tx.commit()
            with pytest.raises(ValueError, match="expense changed since"):
                await waiting
    finally:
        await db.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_postgres_and_memory_exports_match():
    schema = f"expense_{uuid.uuid4().hex[:12]}"
    admin = None
    try:
        admin = await asyncpg.connect(DSN, timeout=3)
        await admin.fetchval("SELECT 1")
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        if admin is not None:
            await admin.close()
        pytest.skip(f"Postgres not available: {type(exc).__name__}")
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        DSN,
        min_size=1,
        max_size=2,
        timeout=3,
        server_settings={"search_path": schema},
    )
    db = PgDatabase(pool)
    pg = PostgresLedgerRepo(db)
    memory = MemoryLedgerRepo()
    chat = GROUP
    alice = UserRef(GROUP.platform, "alice")
    bob = UserRef(GROUP.platform, "bob")
    names = {alice.key: "Alice", bob.key: "Bob"}
    trip_payload = {
        "name": "Cross-store",
        "countries": ["JP"],
        "currencies": ["JPY"],
        "settle_mode": "convert",
        "settle_currency": "SGD",
    }
    try:
        await migrations.apply(
            db, [("chatcore_expenses", resources.files("chatcore_expenses.migrations"))]
        )
        await pg.commit(chat, "start_trip", trip_payload, actor=alice)
        await memory.commit(chat, "start_trip", trip_payload, actor=alice)
        pg_trip = await _active_trip(pg, chat)
        memory_trip = await _active_trip(memory, chat)
        for repo, trip in ((pg, pg_trip), (memory, memory_trip)):
            for description, amount, rate, spent_on in (
                ("rated", 1000, "0.0089123", date(2026, 10, 20)),
                ("missing", 600, None, date(2026, 10, 21)),
            ):
                await repo.commit(
                    chat,
                    "add_expense",
                    {
                        "description": description,
                        "amount": amount,
                        "currency": "JPY",
                        "paid_by": alice.key,
                        "created_by": alice.key,
                        "spent_on": spent_on.isoformat(),
                        "trip_id": trip["id"],
                        "shares": {bob.key: amount // 2, alice.key: amount // 2},
                        "fx_rate_to_settle": rate,
                    },
                    actor=alice,
                )
            await repo.commit(
                chat,
                "settle_up",
                {
                    "from_user": bob.key,
                    "to_user": alice.key,
                    "amount": 50,
                    "currency": "SGD",
                    "trip_id": trip["id"],
                },
                actor=alice,
            )

        today = date(2026, 10, 31)
        zone = UTC
        all_rows = []
        pg_trip_data = None
        for repo in (memory, pg):
            trip = await _active_trip(repo, chat)
            expenses = await repo.expenses(chat, trip_id=trip["id"], limit=None)
            settlements = await repo.settlements(chat, trip_id=trip["id"])
            result = build_export(trip, expenses, settlements, names, today=today, timezone=zone)
            parsed = list(csv.DictReader(io.StringIO(result.data.decode("utf-8-sig"))))
            for row in parsed:
                row["trip_id"] = ""
                row["id"] = ""
            all_rows.append(parsed)
            if repo is pg:
                pg_trip_data = (trip, expenses, settlements, parsed)
        assert all_rows[0] == all_rows[1]
        trip, expenses, settlements, parsed = pg_trip_data
        expected, _ = ledger_balances(trip, expenses, settlements)
        balances = {
            (row["currency"], row["person"]): row["amount"]
            for row in parsed
            if row["record"] == "balance"
        }
        assert balances == {
            (currency, names[user]): f"{amount / 100:.2f}"
            for currency, values in expected.items()
            for user, amount in values.items()
        }
    finally:
        await db.close()
        if admin is not None:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await admin.close()


@pytest.mark.asyncio
async def test_multi_trip_migration_preserves_existing_rows_and_relaxes_active_limit():
    schema = f"expense_multi_{uuid.uuid4().hex[:10]}"
    admin = None
    try:
        admin = await asyncpg.connect(DSN, timeout=3)
        await admin.fetchval("SELECT 1")
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        if admin is not None:
            await admin.close()
        pytest.skip(f"Postgres not available: {type(exc).__name__}")
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        DSN,
        min_size=1,
        max_size=2,
        timeout=3,
        server_settings={"search_path": schema},
    )
    db = PgDatabase(pool)
    try:
        migration_dir = resources.files("chatcore_expenses.migrations")
        await db.execute((migration_dir / "0001_ledger.sql").read_text("utf-8"))
        async with db.transaction() as tx:
            active_id = await tx.fetchval(
                """INSERT INTO trips(
                       chat_key,name,countries,currencies,settle_mode,settle_currency,status
                   ) VALUES(
                       'console:migration-a','China',ARRAY['CN','HK'],ARRAY['CNY','HKD'],
                       'convert','SGD','active'
                   ) RETURNING id"""
            )
            closed_id = await tx.fetchval(
                """INSERT INTO trips(
                       chat_key,name,countries,currencies,settle_mode,settle_currency,status,ended_at
                   ) VALUES(
                       'console:migration-a','Old trip',ARRAY['JP'],ARRAY['JPY'],
                       'per_currency','SGD','closed',now()
                   ) RETURNING id"""
            )
            await tx.execute(
                """INSERT INTO expenses(
                       chat_key,trip_id,description,amount,currency,paid_by,created_by,spent_on
                   ) VALUES(
                       'console:migration-a',$1,'kept expense',100,'SGD',
                       'console:alice','console:alice',$2
                   )""",
                closed_id,
                date.today(),
            )
            await tx.execute(
                """INSERT INTO expense_shares(expense_id,user_key,amount)
                   SELECT id,'console:alice',100 FROM expenses WHERE trip_id=$1""",
                closed_id,
            )
            second_chat_id = await tx.fetchval(
                """INSERT INTO trips(
                       chat_key,name,countries,currencies,settle_mode,settle_currency,status
                   ) VALUES(
                       'console:migration-b','Japan',ARRAY['JP'],ARRAY['JPY'],'convert','SGD','active'
                   ) RETURNING id"""
            )
        await db.execute((migration_dir / "0002_multi_trip.sql").read_text("utf-8"))
        rows = await db.fetch("SELECT * FROM trips ORDER BY chat_key,name")
        assert {str(row["id"]): row["status"] for row in rows} == {
            str(active_id): "active",
            str(closed_id): "closed",
            str(second_chat_id): "active",
        }
        assert all(row["start_date"] is None and row["end_date"] is None for row in rows)
        assert await db.fetchval("SELECT count(*) FROM expenses WHERE trip_id=$1", closed_id) == 1
        assert (
            await db.fetchval(
                "SELECT count(*) FROM trip_defaults WHERE trip_id=ANY($1::uuid[])",
                [active_id, second_chat_id],
            )
            == 2
        )
        await db.execute(
            """INSERT INTO trips(
                   chat_key,name,countries,currencies,settle_mode,settle_currency,status
               ) VALUES(
                   'console:migration-a','KL',ARRAY['MY'],ARRAY['MYR'],'convert','SGD','active'
               )"""
        )
        await db.execute(
            """INSERT INTO trips(
                   chat_key,name,countries,currencies,settle_mode,settle_currency,status
               ) VALUES
                   ('console:migration-a','Future one',ARRAY['CN'],ARRAY['CNY'],
                    'convert','SGD','planned'),
                   ('console:migration-a','Future two',ARRAY['HK'],ARRAY['HKD'],
                    'convert','SGD','planned')"""
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await db.execute(
                """INSERT INTO trips(
                       chat_key,name,countries,currencies,settle_mode,settle_currency,status
                   ) VALUES(
                       'console:migration-a','kl',ARRAY['MY'],ARRAY['MYR'],'convert','SGD','planned'
                   )"""
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await db.execute(
                """INSERT INTO trips(
                       chat_key,name,countries,currencies,settle_mode,settle_currency,status
                   ) VALUES(
                       'console:migration-a','Invalid',ARRAY['US'],ARRAY['USD'],'convert','SGD','paused'
                   )"""
            )
    finally:
        await db.close()
        if admin is not None:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await admin.close()


@pytest.mark.asyncio
async def test_postgres_multi_trip_lifecycle_dates_and_stale_pending_actions():
    schema = f"expense_lifecycle_{uuid.uuid4().hex[:10]}"
    admin = None
    try:
        admin = await asyncpg.connect(DSN, timeout=3)
        await admin.fetchval("SELECT 1")
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        if admin is not None:
            await admin.close()
        pytest.skip(f"Postgres not available: {type(exc).__name__}")
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        DSN,
        min_size=1,
        max_size=2,
        timeout=3,
        server_settings={"search_path": schema},
    )
    db = PgDatabase(pool)
    repo = PostgresLedgerRepo(db)
    chat = ChatRef("console", f"lifecycle-{uuid.uuid4().hex[:10]}")
    alice = User(UserRef("console", "alice"), "Alice", "alice")
    bob = User(UserRef("console", "bob"), "Bob", "bob")
    platform = FakePlatform()
    stores = memory_stores()
    await stores.repo.upsert_member(chat, alice)
    await stores.repo.upsert_member(chat, bob)
    tool_ctx = ToolContext(chat, alice, False, platform, stores)
    command_ctx = CommandContext(chat, alice, False, False, platform, stores)
    plugin = ExpensesPlugin(ledger=repo)
    try:
        await migrations.apply(
            db, [("chatcore_expenses", resources.files("chatcore_expenses.migrations"))]
        )
        planned_payload = json.loads(
            json.dumps(
                {
                    "name": "Planned",
                    "countries": ["SG"],
                    "currencies": ["SGD"],
                    "settle_mode": "per_currency",
                    "settle_currency": "SGD",
                    "status": "planned",
                    "start_date": "2026-12-01",
                    "end_date": None,
                }
            )
        )
        await repo.commit(chat, "start_trip", planned_payload, actor=alice.ref)
        await repo.commit(
            chat,
            "start_trip",
            {
                "name": "Active",
                "countries": ["SG"],
                "currencies": ["SGD"],
                "settle_mode": "per_currency",
                "settle_currency": "SGD",
                "status": "active",
            },
            actor=alice.ref,
        )
        planned = await repo.trip(chat, "Planned")
        active = await repo.trip(chat, "Active")
        assert planned["start_date"] == date(2026, 12, 1)
        assert await repo.default_trip_id(chat) == active["id"]
        date_update = json.loads(json.dumps({"trip_id": planned["id"], "end_date": "2026-12-10"}))
        date_echo = await repo.commit(chat, "update_trip", date_update, actor=alice.ref)
        assert date_echo.startswith("Updated trip Planned:\nDates:")
        assert date_echo.endswith('Say "undo" to change it back.')
        assert (await repo.trip(chat, "Planned"))["end_date"] == date(2026, 12, 10)

        await repo.commit(
            chat,
            "end_trip",
            {"trip_id": active["id"], "expected_status": "active"},
            actor=alice.ref,
        )
        assert await repo.default_trip_id(chat) is None
        with pytest.raises(ValueError, match="trip changed"):
            await repo.commit(
                chat,
                "end_trip",
                {"trip_id": planned["id"], "expected_status": "active"},
                actor=alice.ref,
            )
        await repo.commit(
            chat,
            "activate_trip",
            {"trip_id": planned["id"], "expected_status": "planned"},
            actor=alice.ref,
        )
        assert await repo.default_trip_id(chat) == planned["id"]
        await repo.commit(
            chat,
            "end_trip",
            {"trip_id": planned["id"], "expected_status": "active"},
            actor=alice.ref,
        )
        assert await repo.default_trip_id(chat) is None
        await repo.commit(
            chat,
            "reopen_trip",
            {"trip_id": planned["id"], "expected_status": "closed", "status": "planned"},
            actor=alice.ref,
        )
        planned = await repo.trip(chat, planned["id"])
        assert planned["status"] == "planned" and planned["ended_at"] is None
        assert await repo.default_trip_id(chat) is None
        await repo.commit(
            chat,
            "end_trip",
            {"trip_id": planned["id"], "expected_status": "planned"},
            actor=alice.ref,
        )
        await repo.commit(
            chat,
            "reopen_trip",
            {"trip_id": planned["id"], "expected_status": "closed", "status": "active"},
            actor=alice.ref,
        )
        planned = await repo.trip(chat, planned["id"])
        assert planned["status"] == "active" and planned["ended_at"] is None
        assert await repo.default_trip_id(chat) == planned["id"]

        await repo.commit(
            chat,
            "start_trip",
            {
                "name": "Other planned",
                "countries": ["SG"],
                "currencies": ["SGD"],
                "settle_mode": "per_currency",
                "settle_currency": "SGD",
                "status": "planned",
            },
            actor=alice.ref,
        )
        other_planned = await repo.trip(chat, "Other planned")
        with pytest.raises(ValueError, match="Only an active trip"):
            await repo.commit(
                chat, "set_default_trip", {"trip_id": other_planned["id"]}, actor=alice.ref
            )

        update_tool = next(tool for tool in plugin.tools() if tool.name == "update_trip")
        rename = await update_tool.handler(tool_ctx, trip="Planned", name="Other planned")
        assert rename.pending_action is not None
        result = await plugin.action_handlers()["update_trip"].commit(
            rename.pending_action, command_ctx
        )
        assert (
            result == "Not saved: a trip with that name is already planned or active in this chat."
        )

        add_tool = next(tool for tool in plugin.tools() if tool.name == "add_expense")
        add = await add_tool.handler(
            tool_ctx,
            description="prebooked",
            amount="12",
            currency="SGD",
            paid_by="me",
            split_with=["me"],
            trip="Planned",
        )
        settle_tool = next(tool for tool in plugin.tools() if tool.name == "settle_up")
        settle = await settle_tool.handler(
            tool_ctx,
            from_user="bob",
            to_user="alice",
            amount="3",
            currency="SGD",
            trip="Planned",
        )
        await repo.commit(
            chat,
            "end_trip",
            {"trip_id": planned["id"], "expected_status": "active"},
            actor=alice.ref,
        )
        assert (
            await plugin.action_handlers()["add_expense"].commit(add.pending_action, command_ctx)
            == "Not saved: The trip was closed; propose this again."
        )
        assert (
            await plugin.action_handlers()["settle_up"].commit(settle.pending_action, command_ctx)
            == "Not saved: The trip was closed; propose this again."
        )
    finally:
        await db.close()
        if admin is not None:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await admin.close()
