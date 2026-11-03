import json
from datetime import date

import pytest
from chatcore.events import ChatRef, User, UserRef
from chatcore.store.base import Stores
from chatcore.store.memory import MemoryCache, MemoryCoreRepo
from chatcore.testing import FakePlatform
from chatcore.tools.registry import ToolContext
from chatcore_expenses.plugin import ExpensesPlugin
from chatcore_expenses.repository import MemoryLedgerRepo


@pytest.fixture
def context():
    user = User(UserRef("fake", "alice"), "Alice", "alice")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    return ToolContext(ChatRef("fake", "group"), user, False, FakePlatform(), stores)


async def commit_start(ledger, context, name, status="active", start_date=None):
    return await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": name,
            "countries": ["CN"],
            "currencies": ["CNY"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
            "status": status,
            "start_date": start_date,
            "end_date": None,
        },
        actor=context.user.ref,
    )


def json_payload(payload):
    return json.loads(json.dumps(payload))


@pytest.mark.asyncio
async def test_multiple_active_and_planned_trips_have_scoped_default(context):
    ledger = MemoryLedgerRepo()
    await commit_start(ledger, context, "China", "planned", date(2026, 12, 1))
    await commit_start(ledger, context, "KL")
    await commit_start(ledger, context, "Tokyo")

    trips = await ledger.trips(context.chat)
    assert [(trip["name"], trip["status"]) for trip in trips] == [
        ("China", "planned"),
        ("KL", "active"),
        ("Tokyo", "active"),
    ]
    assert await ledger.default_trip_id(context.chat) == trips[-1]["id"]
    assert [trip["is_default"] for trip in trips] == [False, False, True]
    by_prefix = await ledger.trip(context.chat, trips[0]["id"].replace("-", "")[:8])
    assert by_prefix["id"] == trips[0]["id"]


@pytest.mark.asyncio
async def test_plugin_start_trip_decodes_entities_and_defaults_future_date_to_planned(context):
    ledger = MemoryLedgerRepo()
    plugin = ExpensesPlugin(ledger=ledger)
    tool = next(tool for tool in plugin.tools() if tool.name == "start_trip")
    result = await tool.handler(
        context,
        name="Sample &amp;amp; Example trip",
        countries=["China"],
        settle_mode="convert",
        start_date="2026-12-01",
        end_date="2026-12-10",
    )

    payload = json_payload(result.pending_action.payload)
    assert payload["name"] == "Sample & Example trip"
    assert payload["status"] == "planned"
    assert payload["start_date"] == "2026-12-01"
    assert payload["end_date"] == "2026-12-10"
    assert "Plan trip: Sample & Example trip" in result.content
    assert "2026-12-01 → 2026-12-10" in result.content
    await ledger.commit(context.chat, "start_trip", payload, actor=context.user.ref)
    stored = await ledger.trip(context.chat, "Sample & Example trip")
    assert stored["start_date"] == date(2026, 12, 1)


@pytest.mark.asyncio
async def test_explicit_planned_trip_can_receive_expense_but_closed_cannot(context):
    ledger = MemoryLedgerRepo()
    await commit_start(ledger, context, "China", "planned")
    await context.stores.repo.upsert_member(context.chat, context.user)
    plugin = ExpensesPlugin(ledger=ledger)
    add = next(tool for tool in plugin.tools() if tool.name == "add_expense")

    proposed = await add.handler(
        context,
        description="Fish &amp; chips",
        amount="12",
        paid_by="me",
        split_with=["me"],
        trip="China",
    )
    assert proposed.pending_action.payload["description"] == "Fish & chips"
    assert proposed.pending_action.payload["trip_id"]
    assert "(planned)" in proposed.pending_action.summary
    payload = json_payload(proposed.pending_action.payload)
    await ledger.commit(context.chat, "add_expense", payload, actor=context.user.ref)
    assert [
        row["description"]
        for row in await ledger.expenses(context.chat, trip_id=payload["trip_id"])
    ] == ["Fish & chips"]
    trip_row = await ledger.trip(context.chat, "China")
    await ledger.commit(
        context.chat,
        "end_trip",
        {"trip_id": trip_row["id"], "expected_status": "planned"},
        actor=context.user.ref,
    )
    rejected = await add.handler(
        context,
        description="Later meal",
        amount="3",
        paid_by="me",
        split_with=["me"],
        trip="China",
    )
    assert rejected.is_error and "closed" in rejected.content


@pytest.mark.asyncio
async def test_date_only_update_and_rename_are_previewed_and_committed(context):
    ledger = MemoryLedgerRepo()
    await commit_start(ledger, context, "Open")
    plugin = ExpensesPlugin(ledger=ledger)
    update = next(tool for tool in plugin.tools() if tool.name == "update_trip")
    proposal = await update.handler(
        context, trip="Open", start_date="2026-12-01", end_date="2026-12-10"
    )
    assert "Dates:" in proposal.content
    await ledger.commit(
        context.chat,
        "update_trip",
        json_payload(proposal.pending_action.payload),
        actor=context.user.ref,
    )
    renamed = await update.handler(context, trip="Open", name="Renamed", end_date="2026-12-12")
    assert "Name: Open → Renamed" in renamed.content
    assert "Dates:" in renamed.content
    await ledger.commit(
        context.chat,
        "update_trip",
        json_payload(renamed.pending_action.payload),
        actor=context.user.ref,
    )
    stored = await ledger.trip(context.chat, "Renamed")
    assert stored["start_date"] == date(2026, 12, 1)
    assert stored["end_date"] == date(2026, 12, 12)


@pytest.mark.asyncio
async def test_update_payload_omits_unchanged_scalars_before_a_later_update(context):
    ledger = MemoryLedgerRepo()
    await commit_start(ledger, context, "Original")
    trip = await ledger.trip(context.chat, "Original")
    await ledger.commit(
        context.chat,
        "update_trip",
        {"trip_id": trip["id"], "end_date": "2026-12-10"},
        actor=context.user.ref,
    )
    plugin = ExpensesPlugin(ledger=ledger)
    update = next(tool for tool in plugin.tools() if tool.name == "update_trip")
    proposal = await update.handler(
        context,
        trip="Original",
        name="Renamed",
        end_date="2026-12-10",
        settle_mode="convert",
    )
    payload = json_payload(proposal.pending_action.payload)
    assert payload["end_date"] is None
    assert payload["settle_mode"] is None
    assert "Name: Original → Renamed" in proposal.content
    assert "Dates:" not in proposal.content
    assert "Settle mode:" not in proposal.content

    await ledger.commit(
        context.chat,
        "update_trip",
        {"trip_id": trip["id"], "end_date": "2026-12-20"},
        actor=context.user.ref,
    )
    await ledger.commit(context.chat, "update_trip", payload, actor=context.user.ref)

    updated = await ledger.trip(context.chat, trip["id"])
    assert updated["name"] == "Renamed"
    assert updated["end_date"] == date(2026, 12, 20)


@pytest.mark.asyncio
async def test_case_only_rename_is_kept(context):
    ledger = MemoryLedgerRepo()
    await commit_start(ledger, context, "example city trip")
    plugin = ExpensesPlugin(ledger=ledger)
    update = next(tool for tool in plugin.tools() if tool.name == "update_trip")
    proposal = await update.handler(context, trip="example city trip", name="Example City Trip")
    assert "Name: example city trip → Example City Trip" in proposal.content
    await ledger.commit(
        context.chat,
        "update_trip",
        json_payload(proposal.pending_action.payload),
        actor=context.user.ref,
    )
    assert (await ledger.trip(context.chat, "Example City Trip"))["name"] == "Example City Trip"


@pytest.mark.asyncio
async def test_empty_explicit_trip_reference_is_rejected(context):
    ledger = MemoryLedgerRepo()
    await commit_start(ledger, context, "Open")
    await context.stores.repo.upsert_member(context.chat, context.user)
    plugin = ExpensesPlugin(ledger=ledger)
    end = await next(tool for tool in plugin.tools() if tool.name == "end_trip").handler(
        context, trip="  "
    )
    add = await next(tool for tool in plugin.tools() if tool.name == "add_expense").handler(
        context,
        description="coffee",
        amount="2",
        paid_by="me",
        split_with=["me"],
        trip="",
    )
    assert end.is_error and end.content == "Say which trip."
    assert add.is_error and add.content == "Say which trip."


@pytest.mark.asyncio
async def test_start_active_preserves_planned_trip_and_confirmation_sets_default(context):
    ledger = MemoryLedgerRepo()
    await commit_start(ledger, context, "China", "planned")
    plugin = ExpensesPlugin(ledger=ledger)
    action = await next(tool for tool in plugin.tools() if tool.name == "start_trip").handler(
        context, name="KL", countries=["Malaysia"], settle_mode="convert"
    )

    assert action.pending_action is not None
    assert await ledger.default_trip_id(context.chat) is None
    assert "Other trips stay as they are: China (planned)." in action.content
    from chatcore.plugin import CommandContext

    command = CommandContext(
        context.chat, context.user, False, False, context.platform, context.stores
    )
    await plugin.action_handlers()["start_trip"].commit(action.pending_action, command)
    trips = await ledger.trips(context.chat)
    assert {(trip["name"], trip["status"]) for trip in trips} == {
        ("China", "planned"),
        ("KL", "active"),
    }
    assert await ledger.default_trip_id(context.chat) == next(
        trip["id"] for trip in trips if trip["name"] == "KL"
    )


@pytest.mark.asyncio
async def test_ambiguous_implicit_scope_returns_question_without_pending(context):
    ledger = MemoryLedgerRepo()
    await commit_start(ledger, context, "KL")
    await commit_start(ledger, context, "Tokyo")
    ledger.defaults.clear()
    await context.stores.repo.upsert_member(context.chat, context.user)
    result = await next(
        tool for tool in ExpensesPlugin(ledger=ledger).tools() if tool.name == "add_expense"
    ).handler(context, description="tea", amount="5", paid_by="me", split_with=["me"])

    assert result.is_error
    assert "Several trips are active" in result.content
    assert result.pending_action is None


@pytest.mark.asyncio
async def test_end_named_trip_closes_only_that_trip(context):
    ledger = MemoryLedgerRepo()
    await commit_start(ledger, context, "China", "planned")
    await commit_start(ledger, context, "KL")
    plugin = ExpensesPlugin(ledger=ledger)
    end = await next(tool for tool in plugin.tools() if tool.name == "end_trip").handler(
        context, trip="China"
    )

    assert "End trip: China (planned)" in end.content
    from chatcore.plugin import CommandContext

    command = CommandContext(
        context.chat, context.user, False, False, context.platform, context.stores
    )
    await plugin.action_handlers()["end_trip"].commit(end.pending_action, command)
    trips = {trip["name"]: trip for trip in await ledger.trips(context.chat)}
    assert trips["China"]["status"] == "closed"
    assert trips["KL"]["status"] == "active"
    assert await ledger.default_trip_id(context.chat) == trips["KL"]["id"]
    detail = await next(tool for tool in plugin.tools() if tool.name == "get_trip").handler(
        context, trip="China"
    )
    assert "China (closed" in detail.content


@pytest.mark.asyncio
async def test_end_without_trip_uses_single_planned_trip(context):
    ledger = MemoryLedgerRepo()
    await commit_start(ledger, context, "Future", "planned")
    result = await next(
        tool for tool in ExpensesPlugin(ledger=ledger).tools() if tool.name == "end_trip"
    ).handler(context)

    assert result.pending_action.payload["expected_status"] == "planned"
    assert "End trip: Future (planned)" in result.content


@pytest.mark.asyncio
async def test_trip_reference_accepts_id_prefix_and_ambiguous_substring_asks(context):
    ledger = MemoryLedgerRepo()
    await commit_start(ledger, context, "Sample & Example trip", "planned")
    await commit_start(ledger, context, "Malaysia trip")
    plugin = ExpensesPlugin(ledger=ledger)
    get_trip = next(tool for tool in plugin.tools() if tool.name == "get_trip")
    sample = next(
        trip for trip in await ledger.trips(context.chat) if trip["name"].startswith("Sample")
    )

    by_prefix = await get_trip.handler(context, trip=sample["id"].replace("-", "")[:8])
    ambiguous = await get_trip.handler(context, trip="trip")

    assert "Sample & Example trip (planned)" in by_prefix.content
    assert ambiguous.is_error
    assert "matches several trips" in ambiguous.content


@pytest.mark.asyncio
async def test_pending_expense_for_trip_closed_before_confirmation_is_not_saved(context):
    ledger = MemoryLedgerRepo()
    await commit_start(ledger, context, "KL")
    await context.stores.repo.upsert_member(context.chat, context.user)
    plugin = ExpensesPlugin(ledger=ledger)
    add = next(tool for tool in plugin.tools() if tool.name == "add_expense")
    proposal = await add.handler(
        context,
        description="Meal",
        amount="10",
        paid_by="me",
        split_with=["me"],
        trip="KL",
    )
    trip = (await ledger.trips(context.chat))[0]
    await ledger.commit(
        context.chat,
        "end_trip",
        {"trip_id": trip["id"], "expected_status": "active"},
        actor=context.user.ref,
    )
    from chatcore.plugin import CommandContext

    command = CommandContext(
        context.chat, context.user, False, False, context.platform, context.stores
    )
    saved = await plugin.action_handlers()["add_expense"].commit(proposal.pending_action, command)

    assert saved == "Not saved: The trip was closed; propose this again."
    assert await ledger.expenses(context.chat, trip_id=trip["id"]) == []


@pytest.mark.asyncio
async def test_activate_reopen_and_explicit_status_update_default(context):
    ledger = MemoryLedgerRepo()
    await commit_start(ledger, context, "China", "planned")
    plugin = ExpensesPlugin(ledger=ledger)
    activate = next(tool for tool in plugin.tools() if tool.name == "activate_trip")
    action = await activate.handler(context, trip="China")
    from chatcore.plugin import CommandContext

    command = CommandContext(
        context.chat, context.user, False, False, context.platform, context.stores
    )
    await plugin.action_handlers()["activate_trip"].commit(action.pending_action, command)
    trip = (await ledger.trips(context.chat))[0]
    assert trip["status"] == "active" and trip["is_default"]

    await ledger.commit(
        context.chat,
        "end_trip",
        {"trip_id": trip["id"], "expected_status": "active"},
        actor=context.user.ref,
    )
    reopen = next(tool for tool in plugin.tools() if tool.name == "reopen_trip")
    proposal = await reopen.handler(context, trip="China", status="planned")
    assert "informational only" in proposal.content
    await plugin.action_handlers()["reopen_trip"].commit(proposal.pending_action, command)
    reopened = (await ledger.trips(context.chat))[0]
    assert reopened["status"] == "planned"
    assert reopened["ended_at"] is None
    assert not reopened["is_default"]


@pytest.mark.asyncio
async def test_start_trip_rejects_bad_or_reversed_dates_and_respects_status(context):
    plugin = ExpensesPlugin(ledger=MemoryLedgerRepo())
    start = next(tool for tool in plugin.tools() if tool.name == "start_trip")
    bad = await start.handler(
        context,
        name="Bad date",
        countries=["JP"],
        settle_mode="convert",
        start_date="2026-13-01",
    )
    reversed_dates = await start.handler(
        context,
        name="Reversed",
        countries=["JP"],
        settle_mode="convert",
        start_date="2026-12-10",
        end_date="2026-12-01",
    )
    explicit = await start.handler(
        context,
        name="Future active",
        countries=["JP"],
        settle_mode="convert",
        status="active",
        start_date="2026-12-01",
    )

    assert bad.is_error
    assert reversed_dates.content == "End date must be on or after start date."
    assert explicit.pending_action.payload["status"] == "active"
