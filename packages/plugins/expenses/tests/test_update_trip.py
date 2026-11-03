from datetime import date
from decimal import Decimal

import pytest
from chatcore.events import ChatRef, User, UserRef
from chatcore.plugin import CommandContext
from chatcore.store.base import Stores
from chatcore.store.memory import MemoryCache, MemoryCoreRepo
from chatcore.testing import FakePlatform
from chatcore.tools.registry import ToolContext
from chatcore_expenses.plugin import ExpensesPlugin
from chatcore_expenses.repository import MemoryLedgerRepo


async def _active_trip(ledger, chat):
    active = await ledger.trips(chat, statuses=("active",))
    return active[0] if len(active) == 1 else None


@pytest.fixture
def context():
    user = User(UserRef("fake", "alice"), "Alice", "alice")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    return ToolContext(ChatRef("fake", "group"), user, False, FakePlatform(), stores)


async def china_trip(context, *, currencies=None):
    ledger = MemoryLedgerRepo()
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "China",
            "countries": ["China"],
            "currencies": currencies or ["CNY"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=context.user.ref,
    )
    return ledger


async def propose(plugin, context, **changes):
    return await next(t for t in plugin.tools() if t.name == "update_trip").handler(
        context, **changes
    )


async def confirm(plugin, context, action):
    cmd_ctx = CommandContext(
        context.chat, context.user, False, False, context.platform, context.stores
    )
    return await plugin.action_handlers()["update_trip"].commit(action, cmd_ctx)


@pytest.mark.asyncio
async def test_add_country_proposes_then_commits_country_and_derived_currency(context):
    ledger = await china_trip(context)
    plugin = ExpensesPlugin(ledger=ledger)

    result = await propose(plugin, context, add_countries=["Hong Kong"])

    assert result.pending_action.summary == (
        "Update trip: China (active)\n"
        "Countries: China → China, Hong Kong\nCurrencies: CNY → CNY, HKD"
    )
    before = await _active_trip(ledger, context.chat)
    assert before["countries"] == ["China"]
    assert before["currencies"] == ["CNY"]
    reply = await confirm(plugin, context, result.pending_action)
    trip = await _active_trip(ledger, context.chat)
    assert trip["countries"] == ["China", "Hong Kong"]
    assert trip["currencies"] == ["CNY", "HKD"]
    assert reply.startswith("Updated trip China:\n")
    assert 'Say "undo" to change it back.' in reply


@pytest.mark.asyncio
async def test_currency_addition_and_rename_are_independent_changes(context):
    ledger = await china_trip(context)
    plugin = ExpensesPlugin(ledger=ledger)

    currency = await propose(plugin, context, add_currencies=["hkd"])
    assert currency.pending_action.payload["add_currencies"] == ["HKD"]
    assert "Countries:" not in currency.pending_action.summary
    await confirm(plugin, context, currency.pending_action)
    trip = await _active_trip(ledger, context.chat)
    assert trip["countries"] == ["China"]
    assert trip["currencies"] == ["CNY", "HKD"]

    rename = await propose(plugin, context, name="Example & City")
    assert "Name: China → Example & City" in rename.pending_action.summary
    await confirm(plugin, context, rename.pending_action)
    assert (await _active_trip(ledger, context.chat))["name"] == "Example & City"


@pytest.mark.asyncio
async def test_multi_currency_trip_without_explicit_expense_currency_asks(context):
    ledger = await china_trip(context, currencies=["CNY", "HKD"])
    await context.stores.repo.upsert_member(context.chat, context.user)
    plugin = ExpensesPlugin(ledger=ledger)

    result = await next(t for t in plugin.tools() if t.name == "add_expense").handler(
        context, description="tea", amount="10", paid_by="me", split_with=["me"]
    )

    assert result.is_error
    assert result.content == "Which currency? This trip uses: CNY, HKD."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"add_countries": ["China"]}, "Nothing to change on this trip."),
        ({"add_countries": ["Atlantis"]}, "No currency mapping"),
        ({"add_currencies": ["ZZZ"]}, "Unknown ISO currency code"),
        ({"remove_currencies": ["HKD"]}, "HKD is not on this trip."),
        ({"add_currencies": "HKD"}, "add_currencies must be a list of strings."),
        ({"add_currencies": ["HKD", 3]}, "add_currencies must be a list of strings."),
        ({"add_currencies": ["  "]}, "add_currencies cannot contain empty values."),
        ({"name": "  "}, "Trip name must be between 1 and 100 characters."),
        ({"name": 3}, "name must be a string."),
    ],
)
async def test_invalid_or_unchanged_updates_return_errors(context, changes, error):
    plugin = ExpensesPlugin(ledger=await china_trip(context))

    result = await propose(plugin, context, **changes)

    assert result.is_error
    assert error in result.content
    assert result.pending_action is None


@pytest.mark.asyncio
async def test_remove_used_currency_is_refused_but_soft_deleted_expense_does_not_block(context):
    ledger = await china_trip(context, currencies=["CNY", "HKD"])
    trip = await _active_trip(ledger, context.chat)
    await ledger.commit(
        context.chat,
        "add_expense",
        {
            "description": "tea",
            "amount": 100,
            "currency": "CNY",
            "paid_by": context.user.ref.key,
            "created_by": context.user.ref.key,
            "spent_on": date.today().isoformat(),
            "trip_id": trip["id"],
            "shares": {context.user.ref.key: 100},
            "fx_rate_to_settle": "0.01",
        },
        actor=context.user.ref,
    )
    plugin = ExpensesPlugin(ledger=ledger)

    refused = await propose(plugin, context, remove_currencies=["CNY"])
    assert refused.is_error
    assert refused.content == (
        "CNY is used by expenses or settlements on this trip; it can't be removed."
    )

    expense = (await ledger.expenses(context.chat, trip_id=trip["id"]))[0]
    await ledger.commit(
        context.chat, "delete_expense", {"expense_id": expense["id"]}, actor=context.user.ref
    )
    allowed = await propose(plugin, context, remove_currencies=["CNY"])
    assert allowed.pending_action is not None
    assert allowed.pending_action.payload["remove_currencies"] == ["CNY"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"remove_currencies": ["CNY"]}, "A trip needs at least one currency."),
        ({"remove_countries": ["China"]}, "A trip needs at least one country."),
        ({"remove_countries": ["Japan"]}, "Japan is not on this trip."),
    ],
)
async def test_removals_must_leave_trip_valid(context, changes, error):
    plugin = ExpensesPlugin(ledger=await china_trip(context))

    result = await propose(plugin, context, **changes)

    assert result.is_error
    assert result.content == error


@pytest.mark.asyncio
async def test_settle_fields_can_change_only_before_expenses_or_settlements(context):
    ledger = await china_trip(context)
    plugin = ExpensesPlugin(ledger=ledger)
    change = await propose(plugin, context, settle_mode="per_currency", settle_currency="HKD")
    assert not change.is_error
    await confirm(plugin, context, change.pending_action)
    trip = await _active_trip(ledger, context.chat)
    assert (trip["settle_mode"], trip["settle_currency"]) == ("per_currency", "HKD")

    expense_trip = await china_trip(context)
    trip = await _active_trip(expense_trip, context.chat)
    await expense_trip.commit(
        context.chat,
        "add_expense",
        {
            "description": "tea",
            "amount": 100,
            "currency": "CNY",
            "paid_by": context.user.ref.key,
            "created_by": context.user.ref.key,
            "spent_on": date.today().isoformat(),
            "trip_id": trip["id"],
            "shares": {context.user.ref.key: 100},
            "fx_rate_to_settle": "0.01",
        },
        actor=context.user.ref,
    )
    rejected = await propose(ExpensesPlugin(ledger=expense_trip), context, settle_currency="HKD")
    assert rejected.is_error
    assert rejected.content == (
        "Settle mode and settle currency can't change after expenses or settlements are recorded."
    )

    settlement_ledger = await china_trip(context)
    settlement_trip = await _active_trip(settlement_ledger, context.chat)
    await settlement_ledger.commit(
        context.chat,
        "settle_up",
        {
            "from_user": "fake:bob",
            "to_user": context.user.ref.key,
            "amount": 100,
            "currency": "SGD",
            "trip_id": settlement_trip["id"],
        },
        actor=context.user.ref,
    )
    settlement_change = await propose(
        ExpensesPlugin(ledger=settlement_ledger), context, settle_mode="per_currency"
    )
    assert settlement_change.is_error
    assert settlement_change.content == rejected.content


@pytest.mark.asyncio
async def test_commit_rechecks_currency_usage_and_applies_deltas_to_current_trip(context):
    ledger = await china_trip(context, currencies=["CNY", "HKD", "JPY"])
    plugin = ExpensesPlugin(ledger=ledger)
    proposed = await propose(plugin, context, remove_currencies=["HKD"])
    trip = await _active_trip(ledger, context.chat)
    await ledger.commit(
        context.chat,
        "add_expense",
        {
            "description": "milk tea",
            "amount": 100,
            "currency": "HKD",
            "paid_by": context.user.ref.key,
            "created_by": context.user.ref.key,
            "spent_on": date.today().isoformat(),
            "trip_id": trip["id"],
            "shares": {context.user.ref.key: 100},
            "fx_rate_to_settle": "0.1",
        },
        actor=context.user.ref,
    )

    reply = await confirm(plugin, context, proposed.pending_action)
    assert reply.startswith("Not saved: HKD is used by expenses or settlements")
    assert "HKD" in (await _active_trip(ledger, context.chat))["currencies"]

    first = await propose(plugin, context, add_countries=["Japan"])
    second = await propose(plugin, context, add_countries=["South Korea"])
    await confirm(plugin, context, first.pending_action)
    await confirm(plugin, context, second.pending_action)
    assert (await _active_trip(ledger, context.chat))["countries"] == [
        "China",
        "Japan",
        "South Korea",
    ]
    assert (await _active_trip(ledger, context.chat))["currencies"] == [
        "CNY",
        "HKD",
        "JPY",
        "KRW",
    ]


@pytest.mark.asyncio
async def test_commit_rejects_ended_trip_and_chat_scope(context):
    ledger = await china_trip(context)
    plugin = ExpensesPlugin(ledger=ledger)
    proposed = await propose(plugin, context, add_currencies=["HKD"])
    other = ChatRef("fake", "other")
    assert await _active_trip(ledger, other) is None
    context.chat = other
    hidden = await propose(plugin, context, add_currencies=["HKD"])
    assert hidden.is_error and hidden.content == "There are no open trips to update."
    context.chat = ChatRef("fake", "group")
    trip = await _active_trip(ledger, context.chat)
    await ledger.commit(
        context.chat,
        "end_trip",
        {"trip_id": trip["id"], "expected_status": "active"},
        actor=context.user.ref,
    )
    reply = await confirm(plugin, context, proposed.pending_action)
    assert reply == "Not saved: The trip changed or ended; propose the update again."


@pytest.mark.asyncio
async def test_no_active_trip_and_action_registry(context):
    plugin = ExpensesPlugin(ledger=MemoryLedgerRepo())
    result = await propose(plugin, context, add_currencies=["HKD"])
    assert result.is_error and result.content == "There are no open trips to update."
    assert "update_trip" in plugin.action_handlers()
    tool = next(t for t in plugin.tools() if t.name == "update_trip")
    assert tool.writes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rate", "approximate", "expected"),
    [
        (Decimal("0.1712"), False, "FX: 1 HKD = 0.1712 SGD"),
        (Decimal("0.1712"), True, "FX: 1 HKD = 0.1712 SGD (approximate)"),
        (None, False, "FX: no rate found yet; set one with set_fx_rate"),
    ],
)
async def test_add_expense_summary_shows_trip_currency_note_and_fx(
    context, monkeypatch, rate, approximate, expected
):
    from chatcore_expenses import plugin as plugin_module

    async def fake_rate(*_args, **_kwargs):
        return rate, approximate

    monkeypatch.setattr(plugin_module, "get_rate", fake_rate)
    ledger = await china_trip(context)
    await context.stores.repo.upsert_member(context.chat, context.user)
    plugin = ExpensesPlugin(ledger=ledger)

    off_trip = await next(t for t in plugin.tools() if t.name == "add_expense").handler(
        context,
        description="tea",
        amount="10",
        currency="HKD",
        paid_by="me",
        split_with=["me"],
    )
    assert "Note: HKD is not one of this trip's currencies (CNY). Use update_trip to add it." in (
        off_trip.pending_action.summary
    )
    assert expected in off_trip.pending_action.summary

    on_trip = await next(t for t in plugin.tools() if t.name == "add_expense").handler(
        context,
        description="noodle",
        amount="10",
        currency="CNY",
        paid_by="me",
        split_with=["me"],
    )
    assert "Note: " not in on_trip.pending_action.summary
    assert "Note: HKD" not in on_trip.pending_action.summary
