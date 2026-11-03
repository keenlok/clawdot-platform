from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import respx
from chatcore.confirm import ConfirmService
from chatcore.events import ChatRef
from chatcore.plugin import CommandContext
from chatcore.testing import FakePlatform, memory_stores, press
from chatcore.testing import user as make_user
from chatcore.tools.registry import ToolContext
from chatcore_expenses.plugin import ExpensesPlugin
from chatcore_expenses.repository import MemoryLedgerRepo


async def _active_trip(ledger, chat):
    active = await ledger.trips(chat, statuses=("active",))
    return active[0] if len(active) == 1 else None


def _tool(plugin, name):
    return next(tool for tool in plugin.tools() if tool.name == name)


def _context(chat=None, stores=None, person=None):
    chat = chat or ChatRef("fake", "a")
    stores = stores or memory_stores()
    person = person or make_user("alice")
    return ToolContext(chat, person, False, FakePlatform(), stores)


def _command_context(ctx):
    return CommandContext(ctx.chat, ctx.user, False, False, ctx.platform, ctx.stores)


def _ledger_snapshot(repo):
    return deepcopy((repo.trip_rows, repo.expense_rows, repo.settlement_rows, repo.overrides))


@pytest.mark.asyncio
async def test_confirm_service_registers_plugin_write_handler_and_commits():
    stores = memory_stores()
    chat = ChatRef("fake", "confirm")
    alice = make_user("alice")
    await stores.repo.upsert_member(chat, alice)
    platform = FakePlatform()
    ctx = ToolContext(chat, alice, False, platform, stores)
    ledger = MemoryLedgerRepo()
    plugin = ExpensesPlugin(ledger=ledger)
    proposal = await _tool(plugin, "add_expense").handler(
        ctx, description="Lunch", amount="10", paid_by="me", split_with=["me"]
    )
    handlers = {
        f"{plugin.name}.{kind}": handler for kind, handler in plugin.action_handlers().items()
    }
    confirm = ConfirmService(platform, stores.repo, handlers)
    await confirm.request(proposal.pending_action, alice)
    response = await confirm.on_press(
        press(proposal.pending_action.id, sender=alice, chat=chat),
        _command_context(ctx),
    )
    assert response.startswith("Added: Lunch") and 'Say "undo" to revert.' in response
    assert len(await ledger.expenses(chat)) == 1


@pytest.mark.asyncio
async def test_every_write_tool_is_pending_and_does_not_mutate_ledger():
    ctx = _context()
    await ctx.stores.repo.upsert_member(ctx.chat, ctx.user)
    bob = make_user("bob")
    await ctx.stores.repo.upsert_member(ctx.chat, bob)
    ledger = MemoryLedgerRepo()
    plugin = ExpensesPlugin(ledger=ledger)

    expense = {
        "description": "prior",
        "amount": 100,
        "currency": "SGD",
        "paid_by": ctx.user.ref.key,
        "created_by": ctx.user.ref.key,
        "spent_on": date.today().isoformat(),
        "trip_id": None,
        "shares": {ctx.user.ref.key: 100},
        "fx_rate_to_settle": None,
    }
    await ledger.commit(ctx.chat, "add_expense", expense, actor=ctx.user.ref)
    prior = (await ledger.expenses(ctx.chat))[0]

    proposals = []
    for name, kwargs in (
        (
            "add_expense",
            dict(description="new", amount="1", paid_by="me", split_with=["me"]),
        ),
        ("delete_expense", {"expense_id": prior["id"]}),
        (
            "settle_up",
            dict(from_user="me", to_user="bob", amount="1"),
        ),
        ("start_trip", dict(name="Japan", countries=["JP"], settle_mode="convert")),
    ):
        before = _ledger_snapshot(ledger)
        proposal = await _tool(plugin, name).handler(ctx, **kwargs)
        assert proposal.pending_action is not None
        assert _ledger_snapshot(ledger) == before
        proposals.append(proposal)

    await ledger.commit(
        ctx.chat,
        "start_trip",
        {
            "name": "Vietnam",
            "countries": ["VN"],
            "currencies": ["VND"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=ctx.user.ref,
    )
    for name, kwargs in (
        ("set_fx_rate", {"currency": "VND", "rate": "0.0001"}),
        ("end_trip", {}),
    ):
        before = _ledger_snapshot(ledger)
        proposal = await _tool(plugin, name).handler(ctx, **kwargs)
        assert proposal.pending_action is not None
        assert _ledger_snapshot(ledger) == before


@pytest.mark.asyncio
async def test_tools_isolate_chat_expenses_balances_and_trip():
    stores = memory_stores()
    chat_a, chat_b = ChatRef("fake", "a"), ChatRef("fake", "b")
    alice = make_user("alice")
    await stores.repo.upsert_member(chat_a, alice)
    await stores.repo.upsert_member(chat_b, alice)
    ledger = MemoryLedgerRepo()
    payload = {
        "description": "private dinner",
        "amount": 500,
        "currency": "SGD",
        "paid_by": alice.ref.key,
        "created_by": alice.ref.key,
        "spent_on": date.today().isoformat(),
        "trip_id": None,
        "shares": {"fake:bob": 500},
        "fx_rate_to_settle": None,
    }
    await ledger.commit(chat_a, "add_expense", payload, actor=alice.ref)
    await ledger.commit(
        chat_a,
        "start_trip",
        {
            "name": "Japan",
            "countries": ["JP"],
            "currencies": ["JPY"],
            "settle_mode": "per_currency",
            "settle_currency": "SGD",
        },
        actor=alice.ref,
    )
    plugin = ExpensesPlugin(ledger=ledger)
    ctx_b = ToolContext(chat_b, alice, False, FakePlatform(), stores)
    listing = await _tool(plugin, "list_expenses").handler(ctx_b)
    balances = await _tool(plugin, "get_balances").handler(ctx_b)
    trip = await _tool(plugin, "get_trip").handler(ctx_b)
    assert listing.content == "No expenses found."
    assert balances.content == "All balances are settled."
    assert trip.content == "There are no planned or active trips."
    assert (
        "private dinner"
        in (
            await _tool(plugin, "list_expenses").handler(
                ToolContext(chat_a, alice, False, FakePlatform(), stores), general=True
            )
        ).content
    )


@pytest.mark.asyncio
async def test_add_expense_exact_jpy_uneven_summary_and_payer_excluded():
    ctx = _context()
    alice = ctx.user
    bob, carol = make_user("bob"), make_user("carol")
    for member in (alice, bob, carol):
        await ctx.stores.repo.upsert_member(ctx.chat, member)
    ledger = MemoryLedgerRepo()
    plugin = ExpensesPlugin(ledger=ledger)
    proposal = await _tool(plugin, "add_expense").handler(
        ctx,
        description="ramen",
        amount="1000",
        currency="JPY",
        paid_by="me",
        split_with=["me", "bob", "carol"],
    )
    shares = proposal.pending_action.payload["shares"]
    assert shares == {"fake:alice": 334, "fake:bob": 333, "fake:carol": 333}
    assert proposal.pending_action.summary == (
        "Expense: ramen\nAmount: ¥1000 JPY\nPaid by: Alice\nShares:\n"
        "- Alice: ¥334 JPY\n- Bob: ¥333 JPY\n- Carol: ¥333 JPY\nTrip: General ledger"
    )

    payer_excluded = await _tool(plugin, "add_expense").handler(
        ctx, description="gift", amount="10", paid_by="me", split_with=["bob"]
    )
    assert payer_excluded.pending_action.payload["shares"] == {"fake:bob": 1000}
    await plugin.action_handlers()["add_expense"].commit(
        payer_excluded.pending_action, _command_context(ctx)
    )
    balances = await _tool(plugin, "get_balances").handler(ctx)
    assert balances.content == (
        "SGD balances:\n- Alice is owed S$10.00 SGD\n- Bob owes S$10.00 SGD\n"
        "- Bob pays Alice S$10.00 SGD"
    )


@pytest.mark.asyncio
async def test_settle_up_summary_and_convert_currency_rules():
    ctx = _context()
    await ctx.stores.repo.upsert_member(ctx.chat, ctx.user)
    await ctx.stores.repo.upsert_member(ctx.chat, make_user("bob"))
    ledger = MemoryLedgerRepo()
    plugin = ExpensesPlugin(ledger=ledger)
    general = await _tool(plugin, "settle_up").handler(
        ctx, from_user="bob", to_user="me", amount="12.50"
    )
    assert general.pending_action.payload["currency"] == "SGD"
    assert general.pending_action.summary == (
        "Settlement: Bob pays Alice S$12.50 SGD\nTrip: General ledger"
    )
    await ledger.commit(
        ctx.chat,
        "start_trip",
        {
            "name": "Japan",
            "countries": ["JP"],
            "currencies": ["JPY"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=ctx.user.ref,
    )
    defaulted = await _tool(plugin, "settle_up").handler(
        ctx, from_user="bob", to_user="me", amount="1"
    )
    assert defaulted.pending_action.payload["currency"] == "SGD"
    invalid = await _tool(plugin, "settle_up").handler(
        ctx, from_user="bob", to_user="me", amount="1", currency="JPY"
    )
    assert invalid.is_error and "Settlements in this trip are in SGD" in invalid.content


@pytest.mark.asyncio
async def test_per_currency_settlement_keeps_trip_currency():
    ctx = _context()
    await ctx.stores.repo.upsert_member(ctx.chat, ctx.user)
    await ctx.stores.repo.upsert_member(ctx.chat, make_user("bob"))
    ledger = MemoryLedgerRepo()
    await ledger.commit(
        ctx.chat,
        "start_trip",
        {
            "name": "Japan",
            "countries": ["JP"],
            "currencies": ["JPY"],
            "settle_mode": "per_currency",
            "settle_currency": "SGD",
        },
        actor=ctx.user.ref,
    )
    result = await _tool(ExpensesPlugin(ledger=ledger), "settle_up").handler(
        ctx, from_user="bob", to_user="me", amount="1000"
    )
    assert result.pending_action.payload["currency"] == "JPY"


@pytest.mark.asyncio
async def test_currency_resolution_and_tool_split_types():
    ctx = _context()
    for person in (ctx.user, make_user("bob"), make_user("carol")):
        await ctx.stores.repo.upsert_member(ctx.chat, person)
    plugin = ExpensesPlugin(ledger=MemoryLedgerRepo())
    general = await _tool(plugin, "add_expense").handler(
        ctx, description="tea", amount="1", paid_by="me", split_with=["me"]
    )
    assert general.pending_action.payload["currency"] == "SGD"
    for kind, values, expected in (
        ("shares", {"me": 1, "bob": 1, "carol": 1}, [3334, 3333, 3333]),
        ("percentage", {"me": 33, "bob": 33, "carol": 34}, [3301, 3300, 3400]),
    ):
        amount = "100.01" if kind == "percentage" else "100.00"
        proposal = await _tool(plugin, "add_expense").handler(
            ctx,
            description="meal",
            amount=amount,
            paid_by="me",
            split_with=["me", "bob", "carol"],
            split_type=kind,
            shares=values,
        )
        assert list(proposal.pending_action.payload["shares"].values()) == expected
    await plugin.action_handlers()["start_trip"].commit(
        (
            await _tool(plugin, "start_trip").handler(
                ctx, name="Japan", countries=["JP"], settle_mode="per_currency"
            )
        ).pending_action,
        _command_context(ctx),
    )
    trip_currency = await _tool(plugin, "add_expense").handler(
        ctx, description="tea", amount="1000", paid_by="me", split_with=["me"]
    )
    assert trip_currency.pending_action.payload["currency"] == "JPY"


@pytest.mark.asyncio
async def test_fx_override_prevents_tool_network_calls():
    ctx = _context()
    await ctx.stores.repo.upsert_member(ctx.chat, ctx.user)
    ledger = MemoryLedgerRepo()
    await ledger.commit(
        ctx.chat,
        "start_trip",
        {
            "name": "Vietnam",
            "countries": ["VN"],
            "currencies": ["VND"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=ctx.user.ref,
    )
    await ledger.commit(
        ctx.chat,
        "set_fx_rate",
        {
            "trip_id": (await _active_trip(ledger, ctx.chat))["id"],
            "currency": "VND",
            "rate": "0.00004",
        },
        actor=ctx.user.ref,
    )
    plugin = ExpensesPlugin(ledger=ledger)
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        proposal = await _tool(plugin, "add_expense").handler(
            ctx,
            description="pho",
            amount="20000",
            paid_by="me",
            split_with=["me"],
            spent_on=date.today().isoformat(),
        )
        assert router.calls.call_count == 0
    assert proposal.pending_action.payload["fx_rate_to_settle"] == "0.00004"


@pytest.mark.asyncio
async def test_invalid_and_future_spent_on_are_tool_errors():
    ctx = _context()
    await ctx.stores.repo.upsert_member(ctx.chat, ctx.user)
    plugin = ExpensesPlugin(ledger=MemoryLedgerRepo())
    # "Future" is judged in the plugin's zone (Asia/Singapore), not the machine's (UTC in CI).
    tomorrow = datetime.now(plugin.timezone).date() + timedelta(days=1)
    for spent_on in ("2026-1-2", tomorrow.isoformat()):
        result = await _tool(plugin, "add_expense").handler(
            ctx,
            description="tea",
            amount="1",
            paid_by="me",
            split_with=["me"],
            spent_on=spent_on,
        )
        assert result.is_error


@pytest.mark.asyncio
async def test_manual_start_trip_commit_failure_is_user_facing():
    ctx = _context()
    ledger = MemoryLedgerRepo()
    plugin = ExpensesPlugin(ledger=ledger)
    pending = await _tool(plugin, "start_trip").handler(
        ctx, name="First", countries=["JP"], settle_mode="convert"
    )
    await ledger.commit(
        ctx.chat,
        "start_trip",
        {
            "name": "First",
            "countries": ["FR"],
            "currencies": ["EUR"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=ctx.user.ref,
    )
    result = await plugin.action_handlers()["start_trip"].commit(
        pending.pending_action, _command_context(ctx)
    )
    assert result == "Not saved: A trip named First is already planned or active in this chat."


@pytest.mark.asyncio
async def test_end_trip_recomputes_summary_after_proposal():
    ctx = _context()
    bob = make_user("bob")
    for person in (ctx.user, bob):
        await ctx.stores.repo.upsert_member(ctx.chat, person)
    ledger = MemoryLedgerRepo()
    await ledger.commit(
        ctx.chat,
        "start_trip",
        {
            "name": "Japan",
            "countries": ["JP"],
            "currencies": ["JPY"],
            "settle_mode": "per_currency",
            "settle_currency": "SGD",
        },
        actor=ctx.user.ref,
    )
    plugin = ExpensesPlugin(ledger=ledger)
    proposal = await _tool(plugin, "end_trip").handler(ctx)
    active = await _active_trip(ledger, ctx.chat)
    await ledger.commit(
        ctx.chat,
        "add_expense",
        {
            "description": "sushi",
            "amount": 1000,
            "currency": "JPY",
            "paid_by": ctx.user.ref.key,
            "created_by": ctx.user.ref.key,
            "spent_on": date.today().isoformat(),
            "trip_id": active["id"],
            "shares": {bob.ref.key: 1000},
            "fx_rate_to_settle": None,
        },
        actor=ctx.user.ref,
    )
    committed = await plugin.action_handlers()["end_trip"].commit(
        proposal.pending_action, _command_context(ctx)
    )
    assert "Alice is owed ¥1000 JPY" in committed
    assert "Bob owes ¥1000 JPY" in committed


@pytest.mark.asyncio
async def test_delete_uses_short_ref_and_summary_has_date_and_shares():
    ctx = _context()
    bob = make_user("bob")
    for person in (ctx.user, bob):
        await ctx.stores.repo.upsert_member(ctx.chat, person)
    ledger = MemoryLedgerRepo()
    await ledger.commit(
        ctx.chat,
        "add_expense",
        {
            "description": "Dinner",
            "amount": 1000,
            "currency": "SGD",
            "paid_by": ctx.user.ref.key,
            "created_by": ctx.user.ref.key,
            "spent_on": "2026-10-29",
            "trip_id": None,
            "shares": {bob.ref.key: 1000},
            "fx_rate_to_settle": None,
        },
        actor=ctx.user.ref,
    )
    expense = (await ledger.expenses(ctx.chat))[0]
    plugin = ExpensesPlugin(ledger=ledger)
    listing = await _tool(plugin, "list_expenses").handler(ctx)
    short_ref = expense["id"].replace("-", "")[:8]
    assert f"#{short_ref}" in listing.content
    before = _ledger_snapshot(ledger)
    proposal = await _tool(plugin, "delete_expense").handler(ctx, expense_id=short_ref)
    assert proposal.pending_action.summary == (
        "Delete expense: Dinner\nAmount: S$10.00 SGD\nPaid by: Alice\nSpent on: 2026-10-29\n"
        "Shares:\n- Bob: S$10.00 SGD\nTrip: General ledger"
    )
    assert _ledger_snapshot(ledger) == before
    await plugin.action_handlers()["delete_expense"].commit(
        proposal.pending_action, _command_context(ctx)
    )
    assert await ledger.get_expense(ctx.chat, expense["id"]) is None
    assert await ledger.get_expense(ChatRef("fake", "other"), expense["id"]) is None


@pytest.mark.asyncio
async def test_general_balances_filter_before_limit_over_many_trip_rows():
    ctx = _context()
    bob = make_user("bob")
    for person in (ctx.user, bob):
        await ctx.stores.repo.upsert_member(ctx.chat, person)
    ledger = MemoryLedgerRepo()
    await ledger.commit(
        ctx.chat,
        "start_trip",
        {
            "name": "Japan",
            "countries": ["JP"],
            "currencies": ["JPY"],
            "settle_mode": "per_currency",
            "settle_currency": "SGD",
        },
        actor=ctx.user.ref,
    )
    trip = await _active_trip(ledger, ctx.chat)
    for _ in range(1001):
        await ledger.commit(
            ctx.chat,
            "add_expense",
            {
                "description": "trip",
                "amount": 1,
                "currency": "JPY",
                "paid_by": ctx.user.ref.key,
                "created_by": ctx.user.ref.key,
                "spent_on": date.today().isoformat(),
                "trip_id": trip["id"],
                "shares": {ctx.user.ref.key: 1},
                "fx_rate_to_settle": None,
            },
            actor=ctx.user.ref,
        )
    await ledger.commit(
        ctx.chat,
        "end_trip",
        {"summary": "closed", "trip_id": trip["id"], "expected_status": "active"},
        actor=ctx.user.ref,
    )
    await ledger.commit(
        ctx.chat,
        "add_expense",
        {
            "description": "old general",
            "amount": 100,
            "currency": "SGD",
            "paid_by": ctx.user.ref.key,
            "created_by": ctx.user.ref.key,
            "spent_on": date.today().isoformat(),
            "trip_id": None,
            "shares": {bob.ref.key: 100},
            "fx_rate_to_settle": None,
        },
        actor=ctx.user.ref,
    )
    result = await _tool(ExpensesPlugin(ledger=ledger), "get_balances").handler(ctx)
    assert "Alice is owed S$1.00 SGD" in result.content
    assert "Bob owes S$1.00 SGD" in result.content


def test_postgres_bind_values_are_native_date_and_decimal():
    from contextlib import asynccontextmanager

    from chatcore_expenses.repository import PostgresLedgerRepo

    class FakeDb:
        captured = []

        @asynccontextmanager
        async def transaction(self):
            yield self

        async def fetchrow(self, sql, *args):
            self.captured.append(args)
            return {"id": uuid4()}

        async def execute(self, sql, *args):
            self.captured.append(args)
            return "INSERT 0 1"

    db = FakeDb()
    repo = PostgresLedgerRepo(db)
    ctx = _context()
    import asyncio

    asyncio.run(
        repo.commit(
            ctx.chat,
            "add_expense",
            {
                "description": "tea",
                "amount": 100,
                "currency": "SGD",
                "paid_by": ctx.user.ref.key,
                "created_by": ctx.user.ref.key,
                "spent_on": "2026-10-30",
                "trip_id": None,
                "shares": {ctx.user.ref.key: 100},
                "fx_rate_to_settle": "1.25",
            },
            actor=ctx.user.ref,
        )
    )
    bound = db.captured[0]
    assert isinstance(bound[8], date)
    assert bound[9] == Decimal("1.25")


@pytest.mark.asyncio
async def test_tool_uses_configured_timezone_for_default_expense_date(monkeypatch):
    from datetime import datetime as RealDateTime

    import chatcore_expenses.plugin as plugin_module

    ctx = _context()
    await ctx.stores.repo.upsert_member(ctx.chat, ctx.user)

    class FixedDateTime(RealDateTime):
        @classmethod
        def now(cls, tz=None):
            assert tz.key == "Asia/Tokyo"
            return cls(2026, 10, 30, 7, tzinfo=tz)

    monkeypatch.setattr(plugin_module, "datetime", FixedDateTime)
    result = await _tool(
        ExpensesPlugin(ledger=MemoryLedgerRepo(), timezone="Asia/Tokyo"), "add_expense"
    ).handler(ctx, description="breakfast", amount="1", paid_by="me", split_with=["me"])
    assert result.pending_action.payload["spent_on"] == "2026-10-30"


@pytest.mark.asyncio
async def test_tool_fx_rate_json_keeps_decimal_precision():
    from chatcore_expenses.fx import FRANKFURTER_URL, get_rate

    cache = memory_stores().cache
    exact = "0.123456789123456789"
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{FRANKFURTER_URL}/{date.today().isoformat()}?base=USD&symbols=SGD").mock(
            return_value=httpx.Response(200, text=f'{{"rates":{{"SGD":{exact}}}}}')
        )
        rate, approximate = await get_rate(
            cache, ChatRef("fake", "precision"), date.today(), "USD", "SGD"
        )
    assert rate == Decimal(exact)
    assert approximate is False


def test_postgres_fx_rate_bind_value_is_decimal():
    from contextlib import asynccontextmanager

    from chatcore_expenses.repository import PostgresLedgerRepo

    class FakeDb:
        captured = []

        @asynccontextmanager
        async def transaction(self):
            yield self

        async def fetchrow(self, sql, *args):
            return {
                "id": uuid4(),
                "settle_currency": "SGD",
                "settle_mode": "convert",
                "status": "active",
            }

        async def execute(self, sql, *args):
            self.captured.append(args)
            return "INSERT 0 1"

    import asyncio

    ctx = _context()
    db = FakeDb()
    asyncio.run(
        PostgresLedgerRepo(db).commit(
            ctx.chat,
            "set_fx_rate",
            {
                "trip_id": str(uuid4()),
                "currency": "USD",
                "rate": "1.234567890123456789",
                "expense_ids": [],
            },
            actor=ctx.user.ref,
        )
    )
    assert db.captured[0][4] == Decimal("1.234567890123456789")


@pytest.mark.asyncio
async def test_delete_commit_permission_error_is_user_facing():
    ctx = _context()
    bob = make_user("bob")
    for person in (ctx.user, bob):
        await ctx.stores.repo.upsert_member(ctx.chat, person)
    ledger = MemoryLedgerRepo()
    await ledger.commit(
        ctx.chat,
        "add_expense",
        {
            "description": "shared",
            "amount": 100,
            "currency": "SGD",
            "paid_by": bob.ref.key,
            "created_by": bob.ref.key,
            "spent_on": date.today().isoformat(),
            "trip_id": None,
            "shares": {bob.ref.key: 100},
            "fx_rate_to_settle": None,
        },
        actor=bob.ref,
    )
    expense = (await ledger.expenses(ctx.chat))[0]
    bob_ctx = replace(ctx, user=bob)
    proposal = await _tool(ExpensesPlugin(ledger=ledger), "delete_expense").handler(
        bob_ctx, expense_id=expense["id"]
    )
    result = (
        await ExpensesPlugin(ledger=ledger)
        .action_handlers()["delete_expense"]
        .commit(proposal.pending_action, _command_context(ctx))
    )
    assert result == "Not saved: Only the creator or an admin can delete this expense."
