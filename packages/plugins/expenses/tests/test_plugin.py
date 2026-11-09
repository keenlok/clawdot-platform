from datetime import date
from decimal import Decimal

import pytest
from chatcore.events import ChatRef, User, UserRef
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


@pytest.mark.asyncio
async def test_write_tool_only_returns_pending_action_and_exact_summary(context):
    ledger = MemoryLedgerRepo()
    await context.stores.repo.upsert_member(context.chat, context.user)
    plugin = ExpensesPlugin(ledger=ledger)
    tool = next(t for t in plugin.tools() if t.name == "add_expense")
    result = await tool.handler(
        context,
        description="Coffee",
        amount="12.50",
        paid_by="me",
        split_with=["me"],
        split_type="equal",
    )
    assert result.pending_action is not None
    assert result.pending_action.summary == (
        "Expense: Coffee\nAmount: S$12.50 SGD\nPaid by: Alice\nShares:\n"
        "- Alice: S$12.50 SGD\nTrip: General ledger"
    )
    assert await ledger.expenses(context.chat) == []


@pytest.mark.asyncio
async def test_commit_pending_adds_expense(context):
    ledger = MemoryLedgerRepo()
    await context.stores.repo.upsert_member(context.chat, context.user)
    plugin = ExpensesPlugin(ledger=ledger)
    result = await next(t for t in plugin.tools() if t.name == "add_expense").handler(
        context, description="Lunch", amount="10", paid_by="me", split_with=["me"]
    )
    handler = plugin.action_handlers()["add_expense"]
    await handler.commit(result.pending_action, context)
    rows = await ledger.expenses(context.chat)
    assert len(rows) == 1 and rows[0]["amount"] == 1000


@pytest.mark.asyncio
async def test_add_without_split_uses_trip_members_including_payer(context):
    ledger = MemoryLedgerRepo()
    await context.stores.repo.upsert_member(context.chat, context.user)
    friend = User(UserRef("fake", "bob"), "Bob", "bob")
    await context.stores.repo.upsert_member(context.chat, friend)
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "Example city trip",
            "countries": ["MY"],
            "currencies": ["MYR"],
            "settle_mode": "per_currency",
            "settle_currency": "SGD",
            "members": [context.user.ref.key, friend.ref.key],
        },
        actor=context.user.ref,
    )
    plugin = ExpensesPlugin(ledger=ledger)
    result = await next(t for t in plugin.tools() if t.name == "add_expense").handler(
        context, description="Lunch", amount="30", paid_by="me", currency="MYR"
    )
    assert result.pending_action is not None
    assert result.pending_action.payload["shares"] == {
        context.user.ref.key: 1500,
        friend.ref.key: 1500,
    }
    assert "split 2 ways: Alice, Bob" in result.pending_action.payload["echo"]


@pytest.mark.asyncio
async def test_equal_split_add_echo_names_members_and_truncates_long_lists(context):
    ledger = MemoryLedgerRepo()
    users = [context.user]
    await context.stores.repo.upsert_member(context.chat, context.user)
    for index in range(1, 10):
        person = User(UserRef("fake", f"member-{index}"), f"Member {index}", f"m{index}")
        users.append(person)
        await context.stores.repo.upsert_member(context.chat, person)
    plugin = ExpensesPlugin(ledger=ledger)
    result = await next(t for t in plugin.tools() if t.name == "add_expense").handler(
        context,
        description="Dinner",
        amount="90",
        paid_by="me",
        split_with=["me", *[person.display_name for person in users[1:]]],
    )
    assert result.pending_action
    assert (
        "split 10 ways: Alice, Member 1, Member 2, Member 3, Member 4, Member 5, "
        "Member 6, Member 7, +2 more" in result.pending_action.payload["echo"]
    )
    committed = await plugin.action_handlers()["add_expense"].commit(result.pending_action, context)
    assert (
        "split 10 ways: Alice, Member 1, Member 2, Member 3, Member 4, Member 5, "
        "Member 6, Member 7, +2 more" in committed
    )


@pytest.mark.asyncio
async def test_update_split_echo_names_members_added_and_removed(context):
    from chatcore.tools.registry import ToolRegistry

    ledger = MemoryLedgerRepo()
    await context.stores.repo.upsert_member(context.chat, context.user)
    bob = User(UserRef("fake", "bob"), "Bob", "bob")
    carol = User(UserRef("fake", "carol"), "Carol", "carol")
    await context.stores.repo.upsert_member(context.chat, bob)
    await context.stores.repo.upsert_member(context.chat, carol)
    plugin = ExpensesPlugin(ledger=ledger)
    registry = ToolRegistry(plugin.tools())
    added = await registry.call(
        "add_expense",
        {"description": "Dinner", "amount": "10", "paid_by": "me", "split_with": ["me", "Bob"]},
        context,
    )
    assert added.pending_action
    await plugin.action_handlers()["add_expense"].commit(added.pending_action, context)
    expense = (await ledger.expenses(context.chat))[0]

    update = await registry.call(
        "update_expense",
        {"expense_id": expense["id"], "split_with": ["me", "Carol"]},
        context,
    )
    assert update.pending_action
    echo = update.pending_action.payload["echo"]
    assert "split: added Carol; removed Bob" in echo
    assert "2 ways → 2 ways" not in echo
    committed = await plugin.action_handlers()["update_expense"].commit(
        update.pending_action, context
    )
    assert "split: added Carol; removed Bob" in committed
    assert "2 ways → 2 ways" not in committed

    count_change = await registry.call(
        "update_expense",
        {"expense_id": expense["id"], "split_with": ["me", "Carol", "Bob"]},
        context,
    )
    assert count_change.pending_action
    assert "split: added Bob (2 ways → 3 ways)" in count_change.pending_action.payload["echo"]


@pytest.mark.asyncio
async def test_update_split_echo_names_people_when_amounts_change(context):
    from chatcore.tools.registry import ToolRegistry

    ledger = MemoryLedgerRepo()
    await context.stores.repo.upsert_member(context.chat, context.user)
    bob = User(UserRef("fake", "bob"), "Bob", "bob")
    await context.stores.repo.upsert_member(context.chat, bob)
    plugin = ExpensesPlugin(ledger=ledger)
    registry = ToolRegistry(plugin.tools())
    added = await registry.call(
        "add_expense",
        {"description": "Dinner", "amount": "10", "paid_by": "me", "split_with": ["me", "Bob"]},
        context,
    )
    assert added.pending_action
    await plugin.action_handlers()["add_expense"].commit(added.pending_action, context)
    expense = (await ledger.expenses(context.chat))[0]

    update = await registry.call(
        "update_expense",
        {
            "expense_id": expense["id"],
            "split_with": ["me", "Bob"],
            "split_type": "shares",
            "shares": {"me": "3", "Bob": "1"},
        },
        context,
    )
    assert update.pending_action
    echo = update.pending_action.payload["echo"]
    assert "split amounts changed (Alice, Bob)" in echo
    committed = await plugin.action_handlers()["update_expense"].commit(
        update.pending_action, context
    )
    assert "split amounts changed (Alice, Bob)" in committed


@pytest.mark.asyncio
async def test_add_without_split_asks_when_no_trip_members(context):
    await context.stores.repo.upsert_member(context.chat, context.user)
    result = await next(
        t for t in ExpensesPlugin(ledger=MemoryLedgerRepo()).tools() if t.name == "add_expense"
    ).handler(context, description="Lunch", amount="30", paid_by="me")
    assert result.is_error and "Who shares this?" in result.content


@pytest.mark.asyncio
async def test_payer_outside_trip_members_is_asked_who_shares(context):
    ledger = MemoryLedgerRepo()
    await context.stores.repo.upsert_member(context.chat, context.user)
    friend = User(UserRef("fake", "bob"), "Bob", "bob")
    await context.stores.repo.upsert_member(context.chat, friend)
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "KL",
            "countries": ["MY"],
            "currencies": ["MYR"],
            "settle_mode": "per_currency",
            "settle_currency": "SGD",
            "members": [friend.ref.key],
        },
        actor=context.user.ref,
    )
    result = await next(
        t for t in ExpensesPlugin(ledger=ledger).tools() if t.name == "add_expense"
    ).handler(context, description="Lunch", amount="30", paid_by="me")
    assert result.is_error and "isn't on the KL's member list" in result.content


@pytest.mark.asyncio
async def test_explicit_split_with_overrides_trip_members(context):
    ledger = MemoryLedgerRepo()
    await context.stores.repo.upsert_member(context.chat, context.user)
    members = [context.user]
    for identifier in ("bob", "carol"):
        person = User(UserRef("fake", identifier), identifier.title(), identifier)
        await context.stores.repo.upsert_member(context.chat, person)
        members.append(person)
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "KL",
            "countries": ["MY"],
            "currencies": ["MYR"],
            "settle_mode": "per_currency",
            "settle_currency": "SGD",
            "members": [person.ref.key for person in members],
        },
        actor=context.user.ref,
    )
    result = await next(
        t for t in ExpensesPlugin(ledger=ledger).tools() if t.name == "add_expense"
    ).handler(
        context,
        description="Lunch",
        amount="30",
        paid_by="me",
        split_with=["me", "Bob"],
    )
    assert result.pending_action
    assert result.pending_action.payload["shares"] == {
        context.user.ref.key: 1500,
        members[1].ref.key: 1500,
    }


@pytest.mark.asyncio
async def test_update_description_preserves_existing_shares_and_fx(context, monkeypatch):
    from chatcore.tools.registry import ToolRegistry
    from chatcore_expenses import plugin as module

    calls = []

    async def rate(*_args, **_kwargs):
        calls.append(1)
        return Decimal("0.25"), False

    monkeypatch.setattr(module, "get_rate", rate)
    ledger = MemoryLedgerRepo()
    await context.stores.repo.upsert_member(context.chat, context.user)
    friend = User(UserRef("fake", "bob"), "Bob", "bob")
    await context.stores.repo.upsert_member(context.chat, friend)
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "KL",
            "countries": ["MY"],
            "currencies": ["MYR"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=context.user.ref,
    )
    plugin = ExpensesPlugin(ledger=ledger)
    registry = ToolRegistry(plugin.tools())
    added = await registry.call(
        "add_expense",
        {
            "description": "Dinner",
            "amount": "10",
            "currency": "MYR",
            "paid_by": "me",
            "split_with": ["me", "Bob"],
            "split_type": "exact",
            "shares": {"me": "8", "Bob": "2"},
        },
        context,
    )
    assert added.pending_action, added.content
    await plugin.action_handlers()["add_expense"].commit(added.pending_action, context)
    expense = (await ledger.expenses(context.chat))[0]
    update = await registry.call(
        "update_expense",
        {"expense_id": expense["id"], "description": "Dinner out"},
        context,
    )
    assert update.pending_action
    await plugin.action_handlers()["update_expense"].commit(update.pending_action, context)
    saved = (await ledger.expenses(context.chat))[0]
    assert saved["shares"] == expense["shares"]
    assert saved["fx_rate_to_settle"] == expense["fx_rate_to_settle"]
    assert len(calls) == 1
    rescaled = await registry.call(
        "update_expense", {"expense_id": expense["id"], "amount": "20"}, context
    )
    assert rescaled.pending_action
    await plugin.action_handlers()["update_expense"].commit(rescaled.pending_action, context)
    saved = (await ledger.expenses(context.chat))[0]
    assert {share["user_key"]: share["amount"] for share in saved["shares"]} == {
        context.user.ref.key: 1600,
        friend.ref.key: 400,
    }
    assert saved["fx_rate_to_settle"] == expense["fx_rate_to_settle"]
    same_trip = await registry.call(
        "update_expense",
        {
            "expense_id": expense["id"],
            "description": "Dinner again",
            "currency": "MYR",
            "trip": "KL",
        },
        context,
    )
    assert same_trip.pending_action
    await plugin.action_handlers()["update_expense"].commit(same_trip.pending_action, context)
    assert len(calls) == 1
    dated = await registry.call(
        "update_expense", {"expense_id": expense["id"], "spent_on": "2026-10-31"}, context
    )
    assert dated.pending_action
    await plugin.action_handlers()["update_expense"].commit(dated.pending_action, context)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_update_amount_rescales_existing_split_proportionally(context):
    from chatcore.tools.registry import ToolRegistry

    ledger = MemoryLedgerRepo()
    await context.stores.repo.upsert_member(context.chat, context.user)
    friend = User(UserRef("fake", "bob"), "Bob", "bob")
    await context.stores.repo.upsert_member(context.chat, friend)
    plugin = ExpensesPlugin(ledger=ledger)
    registry = ToolRegistry(plugin.tools())
    added = await registry.call(
        "add_expense",
        {
            "description": "Dinner",
            "amount": "10",
            "paid_by": "me",
            "split_with": ["me", "Bob"],
            "split_type": "exact",
            "shares": {"me": "8", "Bob": "2"},
        },
        context,
    )
    assert added.pending_action, added.content
    await plugin.action_handlers()["add_expense"].commit(added.pending_action, context)
    expense = (await ledger.expenses(context.chat))[0]
    update = await registry.call(
        "update_expense", {"expense_id": expense["id"], "amount": "20"}, context
    )
    assert update.pending_action
    await plugin.action_handlers()["update_expense"].commit(update.pending_action, context)
    saved = (await ledger.expenses(context.chat))[0]
    assert {share["user_key"]: share["amount"] for share in saved["shares"]} == {
        context.user.ref.key: 1600,
        friend.ref.key: 400,
    }


@pytest.mark.asyncio
async def test_update_can_move_expense_to_trip_and_back_to_general(context):
    from chatcore.tools.registry import ToolRegistry

    ledger = MemoryLedgerRepo()
    await context.stores.repo.upsert_member(context.chat, context.user)
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "KL",
            "countries": ["MY"],
            "currencies": ["MYR"],
            "settle_mode": "per_currency",
            "settle_currency": "SGD",
        },
        actor=context.user.ref,
    )
    plugin = ExpensesPlugin(ledger=ledger)
    registry = ToolRegistry(plugin.tools())
    added = await registry.call(
        "add_expense",
        {
            "description": "Dinner",
            "amount": "10",
            "paid_by": "me",
            "split_with": ["me"],
            "general": True,
        },
        context,
    )
    await plugin.action_handlers()["add_expense"].commit(added.pending_action, context)
    expense = (await ledger.expenses(context.chat))[0]
    trip = await ledger.trip(context.chat, "KL")
    to_trip = await registry.call(
        "update_expense", {"expense_id": expense["id"], "trip": trip["id"]}, context
    )
    assert to_trip.pending_action
    await plugin.action_handlers()["update_expense"].commit(to_trip.pending_action, context)
    assert (await ledger.get_expense(context.chat, expense["id"]))["trip_id"] == trip["id"]
    to_general = await registry.call(
        "update_expense",
        {"expense_id": expense["id"], "description": "Dinner", "general": True},
        context,
    )
    assert to_general.pending_action
    await plugin.action_handlers()["update_expense"].commit(to_general.pending_action, context)
    assert (await ledger.get_expense(context.chat, expense["id"]))["trip_id"] is None


@pytest.mark.asyncio
async def test_update_refuses_non_creator_and_no_op(context):
    from chatcore.tools.registry import ToolRegistry

    ledger = MemoryLedgerRepo()
    await context.stores.repo.upsert_member(context.chat, context.user)
    plugin = ExpensesPlugin(ledger=ledger)
    registry = ToolRegistry(plugin.tools())
    added = await registry.call(
        "add_expense",
        {"description": "Dinner", "amount": "10", "paid_by": "me", "split_with": ["me"]},
        context,
    )
    await plugin.action_handlers()["add_expense"].commit(added.pending_action, context)
    expense = (await ledger.expenses(context.chat))[0]
    other = User(UserRef("fake", "other"), "Other", "other")
    other_context = ToolContext(context.chat, other, False, context.platform, context.stores)
    denied = await registry.call(
        "update_expense", {"expense_id": expense["id"], "description": "Changed"}, other_context
    )
    assert denied.is_error and "creator or an admin" in denied.content
    noop = await registry.call(
        "update_expense", {"expense_id": expense["id"], "description": "Dinner"}, context
    )
    assert noop.is_error and noop.content == "Nothing to change."


@pytest.mark.asyncio
async def test_update_trip_members_echo_reads_committed_members(context):
    from chatcore.tools.registry import ToolRegistry

    ledger = MemoryLedgerRepo()
    await context.stores.repo.upsert_member(context.chat, context.user)
    friend = User(UserRef("fake", "bob"), "Bob", "bob")
    added_later = User(UserRef("fake", "carol"), "Carol", "carol")
    await context.stores.repo.upsert_member(context.chat, friend)
    await context.stores.repo.upsert_member(context.chat, added_later)
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "KL",
            "countries": ["MY"],
            "currencies": ["MYR"],
            "settle_mode": "per_currency",
            "settle_currency": "SGD",
            "members": [context.user.ref.key],
        },
        actor=context.user.ref,
    )
    plugin = ExpensesPlugin(ledger=ledger)
    registry = ToolRegistry(plugin.tools())
    proposal = await registry.call("update_trip_members", {"add": ["Bob"]}, context)
    assert proposal.pending_action
    trip_id = proposal.pending_action.payload["trip_id"]
    await ledger.commit(
        context.chat,
        "update_trip_members",
        {"trip_id": trip_id, "add": [added_later.ref.key], "remove": []},
        actor=context.user.ref,
    )
    from chatcore.plugin import CommandContext

    command = CommandContext(
        context.chat, context.user, False, False, context.platform, context.stores
    )
    echo = await plugin.action_handlers()["update_trip_members"].commit(
        proposal.pending_action, command
    )
    assert "Alice" in echo and "Bob" in echo and "Carol" in echo
    removal = await registry.call("update_trip_members", {"remove": ["Bob"]}, context)
    removed_echo = await plugin.action_handlers()["update_trip_members"].commit(
        removal.pending_action, command
    )
    assert "Alice" in removed_echo and "Carol" in removed_echo
    assert "removed Bob" in removed_echo
    noop = await registry.call("update_trip_members", {"add": ["Carol"]}, context)
    noop_echo = await plugin.action_handlers()["update_trip_members"].commit(
        noop.pending_action, command
    )
    assert "no changes" in noop_echo
    raced = await registry.call("update_trip_members", {"add": ["Bob"]}, context)
    await ledger.commit(
        context.chat,
        "update_trip_members",
        {"trip_id": trip_id, "add": [friend.ref.key], "remove": []},
        actor=context.user.ref,
    )
    raced_echo = await plugin.action_handlers()["update_trip_members"].commit(
        raced.pending_action, command
    )
    assert "(no changes)." in raced_echo
    trip_list = await registry.call("list_trips", {}, context)
    trip_detail = await registry.call("get_trip", {"trip": "KL"}, context)
    assert "Members: Alice, Carol" in trip_list.content
    assert "Members: Alice, Carol" in trip_detail.content


@pytest.mark.asyncio
async def test_update_preserves_duplicate_display_name_member_keys(context):
    from chatcore.events import User, UserRef
    from chatcore.tools.registry import ToolRegistry

    ledger = MemoryLedgerRepo()
    first = User(UserRef("fake", "sam-a"), "Sam", "sam_a")
    second = User(UserRef("fake", "sam-b"), "Sam", "sam_b")
    for person in (context.user, first, second):
        await context.stores.repo.upsert_member(context.chat, person)
    plugin = ExpensesPlugin(ledger=ledger)
    registry = ToolRegistry(plugin.tools())
    add = await registry.call(
        "add_expense",
        {
            "description": "Dinner",
            "amount": "10",
            "paid_by": "me",
            "split_with": ["sam_a", "sam_b"],
            "split_type": "equal",
        },
        context,
    )
    await plugin.action_handlers()["add_expense"].commit(add.pending_action, context)
    expense = (await ledger.expenses(context.chat))[0]
    result = await registry.call(
        "update_expense", {"expense_id": expense["id"], "description": "Late dinner"}, context
    )
    assert result.pending_action, result.content
    await plugin.action_handlers()["update_expense"].commit(result.pending_action, context)
    saved = await ledger.get_expense(context.chat, expense["id"])
    assert {share["user_key"] for share in saved["shares"]} == {first.ref.key, second.ref.key}


@pytest.mark.asyncio
async def test_pending_undo_is_bound_to_the_proposed_event(context):
    from chatcore.plugin import CommandContext
    from chatcore.tools.registry import ToolRegistry

    ledger = MemoryLedgerRepo()
    await context.stores.repo.upsert_member(context.chat, context.user)
    plugin = ExpensesPlugin(ledger=ledger)
    registry = ToolRegistry(plugin.tools())
    dinner = await registry.call(
        "add_expense",
        {"description": "Dinner", "amount": "10", "paid_by": "me", "split_with": ["me"]},
        context,
    )
    await plugin.action_handlers()["add_expense"].commit(dinner.pending_action, context)
    proposed_undo = await registry.call("undo_expense_change", {}, context)
    assert proposed_undo.pending_action
    taxi = await registry.call(
        "add_expense",
        {"description": "Taxi", "amount": "12", "paid_by": "me", "split_with": ["me"]},
        context,
    )
    await plugin.action_handlers()["add_expense"].commit(taxi.pending_action, context)
    command = CommandContext(
        context.chat, context.user, False, False, context.platform, context.stores
    )
    result = await plugin.action_handlers()["undo_expense_change"].commit(
        proposed_undo.pending_action, command
    )
    assert "Your latest change is different now; ask again." in result
    assert {row["description"] for row in await ledger.expenses(context.chat)} == {
        "Dinner",
        "Taxi",
    }


def test_direct_expense_tool_set_is_explicit():
    direct = {
        tool.name for tool in ExpensesPlugin(ledger=MemoryLedgerRepo()).tools() if tool.direct
    }
    assert direct == {
        "add_expense",
        "update_expense",
        "delete_expense",
        "restore_expense",
        "undo_expense_change",
        "update_trip_members",
        "update_trip",
    }


@pytest.mark.asyncio
async def test_dm_without_group_resolver_returns_actionable_error(context):
    context.is_dm = True
    plugin = ExpensesPlugin(ledger=MemoryLedgerRepo())
    result = await next(t for t in plugin.tools() if t.name == "list_expenses").handler(
        context, limit=10
    )
    assert result.is_error
    assert "group" in result.content.lower()


@pytest.mark.asyncio
async def test_multi_currency_trip_requires_explicit_currency(context):
    ledger = MemoryLedgerRepo()
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "Asia",
            "countries": ["JP", "KR"],
            "currencies": ["JPY", "KRW"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=context.user.ref,
    )
    await context.stores.repo.upsert_member(context.chat, context.user)
    plugin = ExpensesPlugin(ledger=ledger)
    result = await next(t for t in plugin.tools() if t.name == "add_expense").handler(
        context, description="snack", amount="100", paid_by="me", split_with=["me"]
    )
    assert result.is_error and "JPY, KRW" in result.content


@pytest.mark.asyncio
async def test_delete_permissions_are_checked_at_commit(context):
    ledger = MemoryLedgerRepo()
    payload = {
        "description": "coffee",
        "amount": 100,
        "currency": "SGD",
        "paid_by": "fake:alice",
        "created_by": "fake:other",
        "spent_on": date.today().isoformat(),
        "trip_id": None,
        "shares": {"fake:alice": 100},
        "fx_rate_to_settle": None,
    }
    await ledger.commit(context.chat, "add_expense", payload, actor=context.user.ref)
    expense = (await ledger.expenses(context.chat))[0]
    with pytest.raises(PermissionError):
        await ledger.commit(
            context.chat, "delete_expense", {"expense_id": expense["id"]}, actor=context.user.ref
        )


@pytest.mark.asyncio
async def test_explicit_currency_wins_over_single_trip_currency(context):
    ledger = MemoryLedgerRepo()
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "Japan",
            "countries": ["JP"],
            "currencies": ["JPY"],
            "settle_mode": "per_currency",
            "settle_currency": "SGD",
        },
        actor=context.user.ref,
    )
    await context.stores.repo.upsert_member(context.chat, context.user)
    plugin = ExpensesPlugin(ledger=ledger)
    result = await next(t for t in plugin.tools() if t.name == "add_expense").handler(
        context, description="soup", amount="2.50", currency="USD", paid_by="me", split_with=["me"]
    )
    assert result.pending_action.payload["currency"] == "USD"


async def _start_china_trip(context, ledger):
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "China",
            "countries": ["China"],
            "currencies": ["CNY"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=context.user.ref,
    )


@pytest.mark.asyncio
async def test_pending_expense_is_rejected_after_settle_currency_change(context, monkeypatch):
    from chatcore_expenses import plugin as plugin_module

    async def fake_rate(*_args, **_kwargs):
        return Decimal("0.13"), False

    monkeypatch.setattr(plugin_module, "get_rate", fake_rate)
    ledger = MemoryLedgerRepo()
    await _start_china_trip(context, ledger)
    await context.stores.repo.upsert_member(context.chat, context.user)
    plugin = ExpensesPlugin(ledger=ledger)

    expense = await next(t for t in plugin.tools() if t.name == "add_expense").handler(
        context,
        description="lunch",
        amount="100",
        currency="CNY",
        paid_by="me",
        split_with=["me"],
    )
    change = await next(t for t in plugin.tools() if t.name == "update_trip").handler(
        context, settle_currency="HKD"
    )
    await plugin.action_handlers()["update_trip"].commit(change.pending_action, context)

    reply = await plugin.action_handlers()["add_expense"].commit(expense.pending_action, context)

    assert reply == "Not saved: The trip's settle settings changed since; add it again instead."
    assert await ledger.expenses(context.chat) == []
    assert expense.pending_action.payload["trip_settle_mode"] == "convert"
    assert expense.pending_action.payload["trip_settle_currency"] == "SGD"


@pytest.mark.asyncio
async def test_pending_settlement_is_rejected_after_settle_currency_change(context):
    ledger = MemoryLedgerRepo()
    await _start_china_trip(context, ledger)
    await context.stores.repo.upsert_member(context.chat, context.user)
    await context.stores.repo.upsert_member(
        context.chat, User(UserRef("fake", "bob"), "Bob", "bob")
    )
    plugin = ExpensesPlugin(ledger=ledger)

    settlement = await next(t for t in plugin.tools() if t.name == "settle_up").handler(
        context, from_user="me", to_user="bob", amount="10"
    )
    change = await next(t for t in plugin.tools() if t.name == "update_trip").handler(
        context, settle_currency="HKD"
    )
    await plugin.action_handlers()["update_trip"].commit(change.pending_action, context)

    reply = await plugin.action_handlers()["settle_up"].commit(settlement.pending_action, context)

    assert reply == "Not saved: The trip's settle settings changed since; add it again instead."
    trip = await ledger.trip(context.chat)
    assert await ledger.settlements(context.chat, trip_id=trip["id"]) == []


@pytest.mark.asyncio
async def test_pending_fx_override_is_rejected_after_settle_currency_change(context):
    ledger = MemoryLedgerRepo()
    await _start_china_trip(context, ledger)
    plugin = ExpensesPlugin(ledger=ledger)

    fx = await next(t for t in plugin.tools() if t.name == "set_fx_rate").handler(
        context, currency="CNY", rate="0.18"
    )
    change = await next(t for t in plugin.tools() if t.name == "update_trip").handler(
        context, settle_currency="HKD"
    )
    await plugin.action_handlers()["update_trip"].commit(change.pending_action, context)

    reply = await plugin.action_handlers()["set_fx_rate"].commit(fx.pending_action, context)

    assert reply == "Not saved: The trip's settle settings changed since; add it again instead."
    assert ledger.overrides == []
    assert fx.pending_action.payload["trip_settle_mode"] == "convert"
    assert fx.pending_action.payload["trip_settle_currency"] == "SGD"
    assert fx.pending_action.payload["trip_id"] == change.pending_action.payload["trip_id"]


@pytest.mark.asyncio
async def test_settle_change_is_rejected_when_pending_expense_commits_first(context, monkeypatch):
    from chatcore_expenses import plugin as plugin_module

    async def fake_rate(*_args, **_kwargs):
        return Decimal("0.13"), False

    monkeypatch.setattr(plugin_module, "get_rate", fake_rate)
    ledger = MemoryLedgerRepo()
    await _start_china_trip(context, ledger)
    await context.stores.repo.upsert_member(context.chat, context.user)
    plugin = ExpensesPlugin(ledger=ledger)
    expense = await next(t for t in plugin.tools() if t.name == "add_expense").handler(
        context,
        description="lunch",
        amount="100",
        currency="CNY",
        paid_by="me",
        split_with=["me"],
    )
    change = await next(t for t in plugin.tools() if t.name == "update_trip").handler(
        context, settle_currency="HKD"
    )

    await plugin.action_handlers()["add_expense"].commit(expense.pending_action, context)
    reply = await plugin.action_handlers()["update_trip"].commit(change.pending_action, context)

    assert reply.startswith(
        "Not saved: Settle mode and settle currency can't change after expenses or settlements"
    )
    assert (await _active_trip(ledger, context.chat))["settle_currency"] == "SGD"


@pytest.mark.asyncio
async def test_ambiguous_member_returns_candidates(context):
    await context.stores.repo.upsert_member(context.chat, context.user)
    await context.stores.repo.upsert_member(
        context.chat, User(UserRef("fake", "alicia"), "Alicia", "alicia")
    )
    plugin = ExpensesPlugin(ledger=MemoryLedgerRepo())
    result = await next(t for t in plugin.tools() if t.name == "add_expense").handler(
        context, description="tea", amount="1", paid_by="al", split_with=["me"]
    )
    assert result.is_error
    assert "Alice" in result.content and "Alicia" in result.content


@pytest.mark.asyncio
async def test_start_trip_is_pending_and_end_trip_summary_is_confirmed(context):
    ledger = MemoryLedgerRepo()
    plugin = ExpensesPlugin(ledger=ledger)
    start = await next(t for t in plugin.tools() if t.name == "start_trip").handler(
        context, name="Japan", countries=["Japan"], settle_mode="per_currency"
    )
    assert start.pending_action.summary == (
        "Start trip: Japan\nCountries: Japan\nCurrencies: JPY\nSettle mode: per_currency\n"
        "Settle currency: SGD"
    )
    assert await _active_trip(ledger, context.chat) is None
    handler = plugin.action_handlers()["start_trip"]
    from chatcore.plugin import CommandContext

    cmd_ctx = CommandContext(
        context.chat, context.user, False, False, context.platform, context.stores
    )
    await handler.commit(start.pending_action, cmd_ctx)
    end = await next(t for t in plugin.tools() if t.name == "end_trip").handler(context)
    assert "Final settle-up summary" in end.pending_action.summary
    committed = await plugin.action_handlers()["end_trip"].commit(end.pending_action, cmd_ctx)
    assert committed == "Trip ended: Japan\nFinal settle-up summary:\nAll balances are settled."
    assert await _active_trip(ledger, context.chat) is None


@pytest.mark.asyncio
async def test_set_fx_rate_saves_override_and_assigns_rate_to_listed_expenses(context):
    ledger = MemoryLedgerRepo()
    actor = context.user.ref
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "Vietnam",
            "countries": ["VN"],
            "currencies": ["VND"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=actor,
    )
    trip = await _active_trip(ledger, context.chat)
    await ledger.commit(
        context.chat,
        "add_expense",
        {
            "description": "soup",
            "amount": 25000,
            "currency": "VND",
            "paid_by": actor.key,
            "created_by": actor.key,
            "spent_on": "2026-10-30",
            "trip_id": trip["id"],
            "shares": {actor.key: 25000},
            "fx_rate_to_settle": None,
        },
        actor=actor,
    )
    plugin = ExpensesPlugin(ledger=ledger)
    pending = await next(t for t in plugin.tools() if t.name == "set_fx_rate").handler(
        context, currency="VND", rate="0.00005"
    )
    assert pending.pending_action.payload["expense_ids"]
    assert "Apply to unconverted expenses: soup" in pending.pending_action.summary
    from chatcore.plugin import CommandContext

    cmd_ctx = CommandContext(
        context.chat, context.user, False, False, context.platform, context.stores
    )
    await plugin.action_handlers()["set_fx_rate"].commit(pending.pending_action, cmd_ctx)
    expense = (await ledger.expenses(context.chat, trip_id=trip["id"]))[0]
    assert expense["fx_rate_to_settle"] == "0.00005"


@pytest.mark.asyncio
async def test_list_expenses_without_trip_uses_the_current_trip_only(context):
    ledger = MemoryLedgerRepo()
    actor = context.user.ref
    payload = {
        "description": "general",
        "amount": 100,
        "currency": "SGD",
        "paid_by": actor.key,
        "created_by": actor.key,
        "spent_on": "2026-10-30",
        "trip_id": None,
        "shares": {actor.key: 100},
        "fx_rate_to_settle": None,
    }
    await ledger.commit(context.chat, "add_expense", payload, actor=actor)
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "Japan",
            "countries": ["JP"],
            "currencies": ["JPY"],
            "settle_mode": "per_currency",
            "settle_currency": "SGD",
        },
        actor=actor,
    )
    trip = await _active_trip(ledger, context.chat)
    await ledger.commit(
        context.chat,
        "add_expense",
        payload | {"description": "trip", "trip_id": trip["id"]},
        actor=actor,
    )
    result = await next(
        t for t in ExpensesPlugin(ledger=ledger).tools() if t.name == "list_expenses"
    ).handler(context, limit=20)
    assert "trip" in result.content and "general" not in result.content


@pytest.mark.asyncio
async def test_balances_name_members_and_flag_unconverted_expenses(context):
    ledger = MemoryLedgerRepo()
    actor = context.user.ref
    await context.stores.repo.upsert_member(context.chat, context.user)
    await context.stores.repo.upsert_member(
        context.chat, User(UserRef("fake", "bob"), "Bob", "bob")
    )
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "Vietnam",
            "countries": ["VN"],
            "currencies": ["VND"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=actor,
    )
    trip = await _active_trip(ledger, context.chat)
    await ledger.commit(
        context.chat,
        "add_expense",
        {
            "description": "Pho",
            "amount": 25000,
            "currency": "VND",
            "paid_by": actor.key,
            "created_by": actor.key,
            "spent_on": "2026-10-30",
            "trip_id": trip["id"],
            "shares": {"fake:bob": 25000},
            "fx_rate_to_settle": None,
        },
        actor=actor,
    )
    await ledger.commit(
        context.chat,
        "add_expense",
        {
            "description": "Coffee",
            "amount": 200,
            "currency": "VND",
            "paid_by": actor.key,
            "created_by": actor.key,
            "spent_on": "2026-10-30",
            "trip_id": trip["id"],
            "shares": {"fake:bob": 200},
            "fx_rate_to_settle": "0.00005",
        },
        actor=actor,
    )
    result = await next(
        t for t in ExpensesPlugin(ledger=ledger).tools() if t.name == "get_balances"
    ).handler(context)
    assert result.content == (
        "SGD balances:\n- Alice is owed S$0.01 SGD\n- Bob owes S$0.01 SGD\n"
        "- Bob pays Alice S$0.01 SGD\n1 expense(s) have no FX rate; use set_fx_rate."
    )
