import pytest
from chatcore.events import ChatRef, UserRef
from chatcore_expenses.repository import MemoryLedgerRepo


async def _active_trip(ledger, chat):
    active = await ledger.trips(chat, statuses=("active",))
    return active[0] if len(active) == 1 else None


@pytest.mark.asyncio
async def test_memory_ledger_is_scoped_to_chat():
    repo = MemoryLedgerRepo()
    chat_a, chat_b = ChatRef("fake", "a"), ChatRef("fake", "b")
    actor = UserRef("fake", "u")
    payload = {
        "description": "tea",
        "amount": 100,
        "currency": "SGD",
        "paid_by": actor.key,
        "created_by": actor.key,
        "spent_on": "2026-10-30",
        "trip_id": None,
        "shares": {actor.key: 100},
        "fx_rate_to_settle": None,
    }
    await repo.commit(chat_a, "add_expense", payload, actor=actor)
    assert len(await repo.expenses(chat_a)) == 1
    assert await repo.expenses(chat_b) == []


@pytest.mark.asyncio
async def test_expense_events_support_update_delete_restore_and_undo_scoped_by_actor():
    repo = MemoryLedgerRepo()
    chat = ChatRef("fake", "a")
    actor, other = UserRef("fake", "u"), UserRef("fake", "v")
    payload = {
        "description": "tea",
        "amount": 100,
        "currency": "SGD",
        "paid_by": actor.key,
        "created_by": actor.key,
        "spent_on": "2026-10-30",
        "trip_id": None,
        "shares": {actor.key: 100},
        "fx_rate_to_settle": None,
    }
    await repo.commit(chat, "add_expense", payload, actor=actor)
    expense = (await repo.expenses(chat))[0]
    before = {
        "description": "tea",
        "amount": 100,
        "currency": "SGD",
        "paid_by": actor.key,
        "spent_on": "2026-10-30",
        "trip_id": None,
        "fx_rate_to_settle": None,
        "shares": {actor.key: 100},
    }
    after = before | {"amount": 200, "shares": {actor.key: 200}}
    await repo.commit(
        chat,
        "update_expense",
        {"expense_id": expense["id"], "expected": before, "after": after},
        actor=actor,
    )
    assert (await repo.expenses(chat))[0]["amount"] == 200
    await repo.commit(chat, "undo_expense_change", {}, actor=actor)
    assert (await repo.expenses(chat))[0]["amount"] == 100
    await repo.commit(chat, "undo_expense_change", {}, actor=actor)
    assert await repo.expenses(chat) == []
    await repo.commit(chat, "restore_expense", {"expense_id": expense["id"]}, actor=actor)
    await repo.commit(chat, "undo_expense_change", {}, actor=actor)
    assert await repo.get_expense(chat, expense["id"]) is None
    await repo.commit(chat, "restore_expense", {"expense_id": expense["id"]}, actor=actor)
    await repo.commit(chat, "delete_expense", {"expense_id": expense["id"]}, actor=actor)
    await repo.commit(chat, "undo_expense_change", {}, actor=actor)
    assert await repo.get_expense(chat, expense["id"]) is not None
    with pytest.raises(ValueError, match="no expense change"):
        await repo.commit(chat, "undo_expense_change", {}, actor=other)
    with pytest.raises(ValueError, match="no expense change"):
        await repo.commit(ChatRef("fake", "b"), "undo_expense_change", {}, actor=actor)


@pytest.mark.asyncio
async def test_undo_delete_and_restore_walk_back_and_settle_refusal_is_atomic():
    repo = MemoryLedgerRepo()
    chat, actor = ChatRef("fake", "undo-atomic"), UserRef("fake", "alice")
    await repo.commit(
        chat,
        "start_trip",
        {
            "name": "KL",
            "countries": ["MY"],
            "currencies": ["MYR"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=actor,
    )
    trip = await repo.trip(chat, "KL")
    payload = {
        "description": "meal",
        "amount": 100,
        "currency": "MYR",
        "paid_by": actor.key,
        "created_by": actor.key,
        "spent_on": "2026-10-31",
        "trip_id": trip["id"],
        "shares": {actor.key: 100},
        "fx_rate_to_settle": "0.2",
        "trip_settle_mode": "convert",
        "trip_settle_currency": "SGD",
    }
    await repo.commit(chat, "add_expense", payload, actor=actor)
    expense = (await repo.expenses(chat))[0]
    await repo.commit(chat, "delete_expense", {"expense_id": expense["id"]}, actor=actor)
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
        actor=actor,
    )
    before_events = len(repo.expense_events)
    before_undone = [event["undone_at"] for event in repo.expense_events]
    with pytest.raises(ValueError, match="settle settings changed since"):
        await repo.commit(chat, "undo_expense_change", {}, actor=actor)
    assert await repo.get_expense(chat, expense["id"]) is None
    assert len(repo.expense_events) == before_events
    assert [event["undone_at"] for event in repo.expense_events] == before_undone


@pytest.mark.asyncio
async def test_trip_members_are_ordered_and_chat_scoped():
    repo = MemoryLedgerRepo()
    actor = UserRef("fake", "owner")
    chat_a, chat_b = ChatRef("fake", "a"), ChatRef("fake", "b")
    await repo.commit(
        chat_a,
        "start_trip",
        {
            "name": "KL",
            "countries": ["MY"],
            "currencies": ["MYR"],
            "settle_mode": "per_currency",
            "settle_currency": "SGD",
            "members": ["fake:owner", "fake:friend"],
        },
        actor=actor,
    )
    trip = await repo.trip(chat_a, "KL")
    assert trip["members"] == ["fake:friend", "fake:owner"]
    assert await repo.trip(chat_b, trip["id"]) is None
    assert await repo.trip_members(chat_b, trip["id"]) == []


@pytest.mark.asyncio
async def test_undo_refuses_later_other_actor_change_and_uses_requester_events():
    repo = MemoryLedgerRepo()
    chat = ChatRef("fake", "a")
    other_chat = ChatRef("fake", "b")
    alice, bob = UserRef("fake", "alice"), UserRef("fake", "bob")
    for owner, description in ((alice, "Alice meal"), (bob, "Bob meal")):
        payload = {
            "description": description,
            "amount": 100,
            "currency": "SGD",
            "paid_by": owner.key,
            "created_by": owner.key,
            "spent_on": "2026-10-30",
            "trip_id": None,
            "shares": {owner.key: 100},
            "fx_rate_to_settle": None,
        }
        await repo.commit(chat, "add_expense", payload, actor=owner)
    alice_expense = next(
        row for row in await repo.expenses(chat) if row["description"] == "Alice meal"
    )
    before = {
        "description": "Alice meal",
        "amount": 100,
        "currency": "SGD",
        "paid_by": alice.key,
        "spent_on": "2026-10-30",
        "trip_id": None,
        "fx_rate_to_settle": None,
        "shares": {alice.key: 100},
    }
    await repo.commit(
        chat,
        "update_expense",
        {
            "expense_id": alice_expense["id"],
            "expected": before,
            "after": before | {"description": "changed by admin"},
        },
        actor=bob,
        admin=True,
    )
    with pytest.raises(ValueError, match="changed again after that"):
        await repo.commit(chat, "undo_expense_change", {}, actor=alice)
    await repo.commit(chat, "undo_expense_change", {}, actor=bob)
    rows = await repo.expenses(chat)
    assert (
        next(row for row in rows if row["id"] == alice_expense["id"])["description"] == "Alice meal"
    )
    assert any(row["description"] == "Bob meal" for row in rows)
    with pytest.raises(ValueError, match="no expense change"):
        await repo.commit(other_chat, "undo_expense_change", {}, actor=alice)


@pytest.mark.asyncio
async def test_restore_after_trip_settle_settings_change_is_refused():
    repo = MemoryLedgerRepo()
    chat = ChatRef("fake", "a")
    alice = UserRef("fake", "alice")
    await repo.commit(
        chat,
        "start_trip",
        {
            "name": "KL",
            "countries": ["MY"],
            "currencies": ["MYR"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=alice,
    )
    trip = await repo.trip(chat, "KL")
    payload = {
        "description": "meal",
        "amount": 100,
        "currency": "MYR",
        "paid_by": alice.key,
        "created_by": alice.key,
        "spent_on": "2026-10-30",
        "trip_id": trip["id"],
        "shares": {alice.key: 100},
        "fx_rate_to_settle": "0.2",
        "trip_settle_mode": "convert",
        "trip_settle_currency": "SGD",
    }
    await repo.commit(chat, "add_expense", payload, actor=alice)
    expense = (await repo.expenses(chat))[0]
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
    with pytest.raises(ValueError, match="settle settings changed since"):
        await repo.commit(chat, "restore_expense", {"expense_id": expense["id"]}, actor=alice)


@pytest.mark.asyncio
async def test_undo_trip_move_is_refused_when_destination_trip_closed():
    repo = MemoryLedgerRepo()
    chat = ChatRef("fake", "a")
    alice = UserRef("fake", "alice")
    for name, country in (("KL", "MY"), ("Tokyo", "JP")):
        await repo.commit(
            chat,
            "start_trip",
            {
                "name": name,
                "countries": [country],
                "currencies": ["SGD"],
                "settle_mode": "per_currency",
                "settle_currency": "SGD",
            },
            actor=alice,
        )
    kl = await repo.trip(chat, "KL")
    tokyo = await repo.trip(chat, "Tokyo")
    payload = {
        "description": "meal",
        "amount": 100,
        "currency": "SGD",
        "paid_by": alice.key,
        "created_by": alice.key,
        "spent_on": "2026-10-30",
        "trip_id": kl["id"],
        "shares": {alice.key: 100},
        "fx_rate_to_settle": None,
        "trip_settle_mode": kl["settle_mode"],
        "trip_settle_currency": kl["settle_currency"],
    }
    await repo.commit(chat, "add_expense", payload, actor=alice)
    expense = (await repo.expenses(chat, trip_id=kl["id"]))[0]
    before = {
        "description": "meal",
        "amount": 100,
        "currency": "SGD",
        "paid_by": alice.key,
        "spent_on": "2026-10-30",
        "trip_id": kl["id"],
        "fx_rate_to_settle": None,
        "shares": {alice.key: 100},
    }
    after = before | {"trip_id": tokyo["id"]}
    await repo.commit(
        chat,
        "update_expense",
        {
            "expense_id": expense["id"],
            "expected": before,
            "after": after,
            "trip_settle_mode": tokyo["settle_mode"],
            "trip_settle_currency": tokyo["settle_currency"],
        },
        actor=alice,
    )
    await repo.commit(
        chat,
        "end_trip",
        {"trip_id": kl["id"], "expected_status": "active"},
        actor=alice,
    )
    with pytest.raises(ValueError, match="trip is closed"):
        await repo.commit(chat, "undo_expense_change", {}, actor=alice)


@pytest.mark.asyncio
async def test_memory_ledger_allows_multiple_active_trips_but_rejects_duplicate_open_name():
    repo = MemoryLedgerRepo()
    chat, actor = ChatRef("fake", "a"), UserRef("fake", "u")
    payload = {
        "name": "Japan",
        "countries": ["JP"],
        "currencies": ["JPY"],
        "settle_mode": "convert",
        "settle_currency": "SGD",
    }
    await repo.commit(chat, "start_trip", payload, actor=actor)
    await repo.commit(chat, "start_trip", payload | {"name": "KL"}, actor=actor)
    assert len(await repo.trips(chat, statuses=("active",))) == 2
    with pytest.raises(ValueError, match="already planned or active"):
        await repo.commit(chat, "start_trip", payload, actor=actor)


@pytest.mark.asyncio
async def test_memory_expense_rows_expose_shares_as_rows_and_close_trip():
    repo = MemoryLedgerRepo()
    chat, actor = ChatRef("fake", "a"), UserRef("fake", "u")
    payload = {
        "name": "Japan",
        "countries": ["JP"],
        "currencies": ["JPY"],
        "settle_mode": "convert",
        "settle_currency": "SGD",
    }
    await repo.commit(chat, "start_trip", payload, actor=actor)
    trip = await _active_trip(repo, chat)
    await repo.commit(
        chat,
        "add_expense",
        {
            "description": "tea",
            "amount": 100,
            "currency": "SGD",
            "paid_by": actor.key,
            "created_by": actor.key,
            "spent_on": "2026-10-30",
            "trip_id": trip["id"],
            "shares": {actor.key: 100},
            "fx_rate_to_settle": "1",
        },
        actor=actor,
    )
    expense = (await repo.expenses(chat, trip_id=trip["id"]))[0]
    assert expense["shares"] == [{"user_key": actor.key, "amount": 100}]
    await repo.commit(
        chat,
        "end_trip",
        {"trip_id": trip["id"], "expected_status": "active", "summary": "Closed."},
        actor=actor,
    )
    assert await _active_trip(repo, chat) is None


@pytest.mark.asyncio
async def test_memory_update_trip_commits_deltas_and_rechecks_current_usage():
    repo = MemoryLedgerRepo()
    chat, actor = ChatRef("fake", "a"), UserRef("fake", "u")
    await repo.commit(
        chat,
        "start_trip",
        {
            "name": "China",
            "countries": ["China"],
            "currencies": ["CNY", "HKD"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=actor,
    )
    trip = await _active_trip(repo, chat)
    applied = await repo.commit(
        chat,
        "update_trip",
        {
            "trip_id": trip["id"],
            "name": None,
            "add_countries": ["Hong Kong"],
            "remove_countries": None,
            "add_currencies": None,
            "remove_currencies": None,
            "settle_mode": None,
            "settle_currency": None,
        },
        actor=actor,
    )
    assert applied == (
        'Updated trip China:\nCountries: China → China, Hong Kong\nSay "undo" to change it back.'
    )
    trip = await _active_trip(repo, chat)
    proposal = {
        "trip_id": trip["id"],
        "name": None,
        "add_countries": None,
        "remove_countries": None,
        "add_currencies": None,
        "remove_currencies": ["HKD"],
        "settle_mode": None,
        "settle_currency": None,
    }
    # A direct update is checked against usage that appears after proposal time.
    await repo.commit(
        chat,
        "add_expense",
        {
            "description": "tea",
            "amount": 100,
            "currency": "HKD",
            "paid_by": actor.key,
            "created_by": actor.key,
            "spent_on": "2026-10-30",
            "trip_id": trip["id"],
            "shares": {actor.key: 100},
            "fx_rate_to_settle": "0.1",
        },
        actor=actor,
    )
    assert len(await repo.expenses(chat, trip_id=trip["id"])) == 1
    with pytest.raises(ValueError, match="HKD is used by expenses or settlements"):
        await repo.commit(chat, "update_trip", proposal, actor=actor)
    current = await _active_trip(repo, chat)
    assert current["countries"] == ["China", "Hong Kong"]
    assert "HKD" in current["currencies"]
    with pytest.raises(ValueError, match="trip changed or ended"):
        await repo.commit(chat, "update_trip", proposal | {"trip_id": "wrong"}, actor=actor)


@pytest.mark.asyncio
async def test_memory_legacy_write_payloads_without_trip_settings_still_commit():
    repo = MemoryLedgerRepo()
    chat, actor = ChatRef("fake", "legacy"), UserRef("fake", "u")
    await repo.commit(
        chat,
        "start_trip",
        {
            "name": "China",
            "countries": ["China"],
            "currencies": ["CNY"],
            "settle_mode": "convert",
            "settle_currency": "HKD",
        },
        actor=actor,
    )
    trip = await _active_trip(repo, chat)
    result = await repo.commit(
        chat,
        "add_expense",
        {
            "description": "legacy tea",
            "amount": 100,
            "currency": "CNY",
            "paid_by": actor.key,
            "created_by": actor.key,
            "spent_on": "2026-10-30",
            "trip_id": trip["id"],
            "shares": {actor.key: 100},
            "fx_rate_to_settle": "0.01",
        },
        actor=actor,
    )
    assert result == "Saved legacy tea."
    assert len(await repo.expenses(chat, trip_id=trip["id"])) == 1
    result = await repo.commit(
        chat,
        "settle_up",
        {
            "from_user": "fake:bob",
            "to_user": actor.key,
            "amount": 100,
            "currency": "HKD",
            "trip_id": trip["id"],
        },
        actor=actor,
    )
    assert result == "Settlement recorded."
    assert len(await repo.settlements(chat, trip_id=trip["id"])) == 1
    result = await repo.commit(
        chat,
        "set_fx_rate",
        {"trip_id": trip["id"], "currency": "CNY", "rate": "0.18", "expense_ids": []},
        actor=actor,
    )
    assert result == "FX rate updated."
    assert await repo.fx_override(chat, trip["id"], "CNY", "HKD") == "0.18"


@pytest.mark.asyncio
async def test_memory_restore_of_pre_history_convert_expense_is_refused():
    repo = MemoryLedgerRepo()
    chat, actor = ChatRef("fake", "a"), UserRef("fake", "u")
    await repo.commit(
        chat,
        "start_trip",
        {
            "name": "KL",
            "countries": ["MY"],
            "currencies": ["MYR"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=actor,
    )
    trip = await repo.trip(chat, "KL")
    base = {
        "amount": 1000,
        "currency": "MYR",
        "paid_by": actor.key,
        "created_by": actor.key,
        "spent_on": "2026-10-30",
        "trip_id": trip["id"],
        "shares": {actor.key: 1000},
        "trip_settle_mode": "convert",
        "trip_settle_currency": "SGD",
    }
    await repo.commit(
        chat,
        "add_expense",
        base | {"description": "converted", "fx_rate_to_settle": "0.3"},
        actor=actor,
    )
    await repo.commit(
        chat, "add_expense", base | {"description": "same", "currency": "SGD"}, actor=actor
    )
    # Simulate rows deleted before 0003 existed: deleted, with no change history.
    for row in repo.expense_rows.values():
        row["deleted_at"] = "2026-10-30T00:00:00+00:00"
    repo.expense_events.clear()
    rows = {row["description"]: row for row in repo.expense_rows.values()}
    with pytest.raises(ValueError, match="before change history existed"):
        await repo.commit(
            chat, "restore_expense", {"expense_id": rows["converted"]["id"]}, actor=actor
        )
    assert rows["converted"]["deleted_at"] is not None
    # An expense already in the settle currency has no rate to go stale.
    await repo.commit(chat, "restore_expense", {"expense_id": rows["same"]["id"]}, actor=actor)
    assert [row["description"] for row in await repo.expenses(chat)] == ["same"]
