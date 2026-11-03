"""Observable accounting invariants described by the plugin's public explainer."""

from copy import deepcopy

from chatcore.events import ChatRef, UserRef
from chatcore_expenses.balances import ledger_balances
from chatcore_expenses.repository import MemoryLedgerRepo


async def test_membership_changes_and_fx_override_preserve_existing_receipt_shares_and_rate():
    ledger = MemoryLedgerRepo()
    chat, actor = ChatRef("fake", "contract"), UserRef("fake", "owner")
    await ledger.commit(
        chat,
        "start_trip",
        {
            "name": "Trip",
            "countries": ["MY"],
            "currencies": ["MYR"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
            "members": [actor.key, "fake:friend"],
        },
        actor=actor,
    )
    trip = await ledger.trip(chat, "Trip")
    for description, rate in (("Captured rate", "0.5"), ("Missing rate", None)):
        await ledger.commit(
            chat,
            "add_expense",
            {
                "description": description,
                "amount": 1000,
                "currency": "MYR",
                "paid_by": actor.key,
                "created_by": actor.key,
                "spent_on": "2026-11-04",
                "trip_id": trip["id"],
                "shares": {actor.key: 500, "fake:friend": 500},
                "fx_rate_to_settle": rate,
            },
            actor=actor,
        )
    before = deepcopy(await ledger.expenses(chat, trip_id=trip["id"]))
    await ledger.commit(
        chat,
        "update_trip_members",
        {
            "trip_id": trip["id"],
            "remove": ["fake:friend"],
            "add": ["fake:new_friend"],
        },
        actor=actor,
    )
    after_members = await ledger.expenses(chat, trip_id=trip["id"])
    assert [row["shares"] for row in after_members] == [row["shares"] for row in before]
    await ledger.commit(
        chat,
        "set_fx_rate",
        {
            "trip_id": trip["id"],
            "currency": "MYR",
            "rate": "0.25",
            "expense_ids": [str(row["id"]) for row in before],
        },
        actor=actor,
    )
    expenses = await ledger.expenses(chat, trip_id=trip["id"])
    rates = {row["description"]: str(row["fx_rate_to_settle"]) for row in expenses}
    assert rates == {"Captured rate": "0.5", "Missing rate": "0.25"}
    balances, missing = ledger_balances(trip, expenses, [])
    assert balances == {"SGD": {actor.key: 375, "fake:friend": -375}}
    assert not missing
