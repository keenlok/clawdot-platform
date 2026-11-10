"""Expense speaker context: participants in listings, similar-name handles, member history."""

import re

import pytest
from chatcore.events import ChatRef, User, UserRef
from chatcore.store.base import Stores
from chatcore.store.memory import MemoryCache, MemoryCoreRepo
from chatcore.testing import FakePlatform
from chatcore.tools.registry import ToolContext
from chatcore_expenses.plugin import EXPENSES_INSTRUCTIONS, ExpensesPlugin
from chatcore_expenses.repository import MemoryLedgerRepo

ALICE = User(UserRef("fake", "alice"), "Alice", "alice")
BOB = User(UserRef("fake", "bob"), "Bob", "bob")
CAROL = User(UserRef("fake", "carol"), "Carol", "carol")
DANIEL = User(UserRef("fake", "daniel"), "Daniel", "daniel_example")
DANNY = User(UserRef("fake", "danny"), "Danny", "danny_example")
DANA = User(UserRef("fake", "dana"), "Dana")  # similar name, no username


@pytest.fixture
async def setup():
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    ctx = ToolContext(ChatRef("fake", "group"), ALICE, False, FakePlatform(), stores)
    for person in (ALICE, BOB, CAROL, DANIEL, DANNY, DANA):
        await stores.repo.upsert_member(ctx.chat, person)
    ledger = MemoryLedgerRepo()
    return ExpensesPlugin(ledger=ledger), ledger, ctx


def _tool(plugin, name):
    return next(t for t in plugin.tools() if t.name == name)


async def _trip(ledger, ctx, *, settle_mode="per_currency", currencies=("SGD",), members=()):
    await ledger.commit(
        ctx.chat,
        "start_trip",
        {
            "name": "Example trip",
            "countries": ["SG"],
            "currencies": list(currencies),
            "settle_mode": settle_mode,
            "settle_currency": "SGD",
            "members": [m.ref.key for m in members],
        },
        actor=ctx.user.ref,
    )
    return (await ledger.trips(ctx.chat, statuses=("active",)))[0]


async def _expense(
    ledger, ctx, trip, description, amount, payer, shares, currency="SGD", rate=None
):
    await ledger.commit(
        ctx.chat,
        "add_expense",
        {
            "description": description,
            "amount": amount,
            "currency": currency,
            "paid_by": payer.ref.key,
            "created_by": ctx.user.ref.key,
            "spent_on": "2026-11-09",
            "trip_id": trip["id"] if trip else None,
            "shares": {person.ref.key: value for person, value in shares},
            "fx_rate_to_settle": rate,
        },
        actor=ctx.user.ref,
    )
    rows = await ledger.expenses(ctx.chat, limit=None)
    return next(r for r in rows if r["description"] == description)


async def _settle(ledger, ctx, trip, source, target, amount, currency="SGD"):
    await ledger.commit(
        ctx.chat,
        "settle_up",
        {
            "from_user": source.ref.key,
            "to_user": target.ref.key,
            "amount": amount,
            "currency": currency,
            "trip_id": trip["id"],
        },
        actor=ctx.user.ref,
    )


# --- A1: list_expenses shows participants ---------------------------------------------


async def test_list_expenses_shows_equal_and_exact_participants(setup):
    plugin, ledger, ctx = setup
    trip = await _trip(ledger, ctx)
    equal = await _expense(
        ledger, ctx, trip, "Dinner", 900, ALICE, [(ALICE, 300), (BOB, 300), (CAROL, 300)]
    )
    await _expense(ledger, ctx, trip, "Taxi", 1000, BOB, [(ALICE, 700), (BOB, 300)])
    await _expense(ledger, ctx, trip, "Snacks", 100, BOB, [(ALICE, 34), (BOB, 33), (CAROL, 33)])
    content = (await _tool(plugin, "list_expenses").handler(ctx)).content
    lines = content.splitlines()
    dinner = next(line for line in lines if "Dinner" in line)
    assert dinner.startswith(f"#{str(equal['id']).replace('-', '')[:8]} Dinner: ")
    assert dinner.endswith("(paid by Alice; split: Alice, Bob, Carol)")
    taxi = next(line for line in lines if "Taxi" in line)
    assert taxi.endswith("(paid by Bob; split: Alice S$7.00 SGD, Bob S$3.00 SGD)")
    # An equal split's one-cent remainder still reads as an equal split.
    assert next(line for line in lines if "Snacks" in line).endswith(
        "(paid by Bob; split: Alice, Bob, Carol)"
    )


async def test_list_expenses_truncates_participants_after_eight(setup):
    plugin, ledger, ctx = setup
    labels = ["Ann", "Ben", "Cat", "Dee", "Eve", "Fay", "Gus", "Hal", "Ida", "Jon"]
    people = [User(UserRef("fake", f"p{i}"), label) for i, label in enumerate(labels)]
    for person in people:
        await ctx.stores.repo.upsert_member(ctx.chat, person)
    trip = await _trip(ledger, ctx)
    await _expense(ledger, ctx, trip, "Party", 1000, people[0], [(p, 100) for p in people])
    content = (await _tool(plugin, "list_expenses").handler(ctx)).content
    assert content.endswith("(paid by Ann; split: Ann, Ben, Cat, Dee, Eve, Fay, Gus, Hal, +2 more)")


# --- A2: similar-name handles ---------------------------------------------------------


async def test_similar_names_get_handles_in_members_list_and_echoes(setup):
    plugin, ledger, ctx = setup
    await _trip(ledger, ctx, members=(ALICE, DANIEL, DANNY, DANA))
    trip_text = (await _tool(plugin, "get_trip").handler(ctx)).content
    assert "Members: Alice, Dana, Daniel (@daniel_example), Danny (@danny_example)" in trip_text

    added = await _tool(plugin, "add_expense").handler(
        ctx, description="Lunch", amount="40", paid_by="Daniel", split_with=["Danny", "Alice"]
    )
    assert "paid by Daniel (@daniel_example)" in added.pending_action.payload["echo"]
    assert "Danny (@danny_example), Alice" in added.pending_action.payload["echo"]
    committed = await plugin.action_handlers()["add_expense"].commit(added.pending_action, ctx)
    assert "paid by Daniel (@daniel_example)" in committed
    assert "Alice (@" not in committed

    listing = (await _tool(plugin, "list_expenses").handler(ctx)).content
    assert "paid by Daniel (@daniel_example); split: Danny (@danny_example), Alice" in listing

    row = (await ledger.expenses(ctx.chat))[0]
    updated = await _tool(plugin, "update_expense").handler(
        ctx, expense_id=str(row["id"]), split_with=["Danny", "Dana"]
    )
    echo = updated.pending_action.payload["echo"]
    assert "added Dana" in echo and "removed Alice" in echo
    assert "Dana (@" not in echo


async def test_unrelated_names_keep_plain_display_names(setup):
    plugin, ledger, ctx = setup
    await _trip(ledger, ctx, members=(ALICE, BOB, DANIEL))
    trip_text = (await _tool(plugin, "get_trip").handler(ctx)).content
    assert "Members: Alice, Bob, Daniel\n" in trip_text + "\n"


# --- A3/C7: instructions -------------------------------------------------------------


def test_expenses_instructions_mention_new_rules_within_cap():
    assert len(EXPENSES_INSTRUCTIONS) <= 800
    assert "similar names are different people; never suggest merging them" in (
        EXPENSES_INSTRUCTIONS
    )
    assert "Answer who shares an expense from list_expenses, not earlier replies." in (
        EXPENSES_INSTRUCTIONS
    )
    assert "call member_expenses" in EXPENSES_INSTRUCTIONS


def test_member_expenses_is_in_the_expenses_skill():
    skill = next(
        s for s in ExpensesPlugin(ledger=MemoryLedgerRepo()).skills() if s.name == "expenses"
    )
    assert "member_expenses" in skill.tools
    tool = _tool(ExpensesPlugin(ledger=MemoryLedgerRepo()), "member_expenses")
    assert not tool.writes and not tool.direct
    assert tool.parameters["required"] == ["member"]


# --- C5: member_expenses -------------------------------------------------------------


def _balance_line(text: str, name: str) -> str:
    match = re.search(rf"^- {name} (is owed|owes) (.+)$", text, re.MULTILINE)
    assert match, text
    return f"{name} {match.group(1)} {match.group(2)}"


async def test_member_history_matches_balances_in_convert_trip(setup):
    plugin, ledger, ctx = setup
    trip = await _trip(ledger, ctx, settle_mode="convert", currencies=("SGD", "JPY", "HKD"))
    await _expense(
        ledger,
        ctx,
        trip,
        "Ramen",
        3333,
        ALICE,
        [(ALICE, 1111), (BOB, 1111), (CAROL, 1111)],
        "JPY",
        "0.0089",
    )
    await _expense(
        ledger,
        ctx,
        trip,
        "Dim sum",
        31200,
        CAROL,
        [(ALICE, 20000), (CAROL, 11200)],
        "HKD",
        "0.1723",
    )
    await _expense(ledger, ctx, trip, "Museum", 2500, BOB, [(BOB, 1250), (CAROL, 1250)], rate="1")
    gone = await _expense(ledger, ctx, trip, "Mistake", 9999, BOB, [(BOB, 9999)])
    await ledger.commit(
        ctx.chat, "delete_expense", {"expense_id": str(gone["id"])}, actor=ctx.user.ref, admin=True
    )
    await _settle(ledger, ctx, trip, BOB, CAROL, 500)
    await _settle(ledger, ctx, trip, CAROL, BOB, 120)

    balances = (await _tool(plugin, "get_balances").handler(ctx)).content
    result = await _tool(plugin, "member_expenses").handler(ctx, member="Bob")
    assert not result.is_error
    text = result.content
    lines = text.splitlines()
    assert any("Ramen" in line and "Bob's share ¥1111 JPY" in line for line in lines), text
    assert any("Museum" in line and "paid by Bob" in line for line in lines)
    assert "Dim sum" not in text and "Mistake" not in text
    assert "Bob paid Carol S$5.00 SGD" in text and "Carol paid Bob S$1.20 SGD" in text
    totals = next(line for line in lines if line.startswith("Totals in SGD"))
    assert f"net: {_balance_line(balances, 'Bob')}" in totals

    for person in ("Alice", "Carol"):
        history = (await _tool(plugin, "member_expenses").handler(ctx, member=person)).content
        assert f"net: {_balance_line(balances, person)}" in history


async def test_member_history_per_currency_general_ledger(setup):
    plugin, ledger, ctx = setup
    await _expense(ledger, ctx, None, "Groceries", 3000, ALICE, [(ALICE, 1500), (BOB, 1500)])
    await _expense(ledger, ctx, None, "Books", 2000, BOB, [(BOB, 2000)])
    text = (await _tool(plugin, "member_expenses").handler(ctx, member="me", general=True)).content
    assert "Groceries" in text and "Books" not in text
    assert "Totals in SGD: paid S$30.00 SGD; own share S$15.00 SGD" in text
    assert "net: Alice is owed S$15.00 SGD" in text


async def test_member_history_resolver_errors(setup):
    plugin, ledger, ctx = setup
    await _trip(ledger, ctx)
    ambiguous = await _tool(plugin, "member_expenses").handler(ctx, member="Dan")
    assert ambiguous.is_error and "Ambiguous member 'Dan'" in ambiguous.content
    unknown = await _tool(plugin, "member_expenses").handler(ctx, member="Zed")
    assert unknown.is_error and "Unknown member 'Zed'" in unknown.content


async def test_member_history_truncates_rows_but_totals_cover_everything(setup):
    plugin, ledger, ctx = setup
    trip = await _trip(ledger, ctx)
    for index in range(105):
        await _expense(
            ledger, ctx, trip, f"Ride {index:03d}", 200, ALICE, [(ALICE, 100), (BOB, 100)]
        )
    text = (await _tool(plugin, "member_expenses").handler(ctx, member="Bob")).content
    assert text.count("Bob's share S$1.00 SGD") == 100
    assert "5 older expense(s) not shown; totals include them." in text
    assert "own share S$105.00 SGD" in text and "net: Bob owes S$105.00 SGD" in text


async def test_member_history_with_no_entries(setup):
    plugin, ledger, ctx = setup
    trip = await _trip(ledger, ctx)
    await _expense(ledger, ctx, trip, "Taxi", 500, ALICE, [(ALICE, 500)])
    text = (await _tool(plugin, "member_expenses").handler(ctx, member="Carol")).content
    assert "Taxi" not in text
    assert "0 expense(s)" in text and "net: Carol is settled" in text
