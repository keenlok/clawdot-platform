from unittest.mock import AsyncMock

import pytest
from chatcore_google.expense_service import ExpenseService
from chatcore_google.itinerary import HEADER, VERSION
from chatcore_google.itinerary_service import ItineraryService

URL = "https://docs.google.com/spreadsheets/d/file/edit"


def ledger(chat="fake:group"):
    return {
        "chat_key": chat,
        "trip_id": "trip-id",
        "trip_name": "Japan",
        "csv": b"record,description,currency,amount\nexpense,Lunch,JPY,1000\n",
        "totals": {"JPY": "1000"},
    }


async def test_expense_export_freezes_real_ledger_rows_and_rechecks_scope(google_service):
    service, client, ctx, commit = google_service
    provider = AsyncMock(return_value=ledger())
    bridge = ExpenseService(service, provider)
    action = await bridge.propose_export(ctx, trip="Japan")
    assert "Lunch" in action.summary
    client.request.assert_not_awaited()
    provider.return_value = ledger("fake:other")
    with pytest.raises(ValueError, match="scope|group"):
        await bridge.commit(action, commit)
    client.request.assert_not_awaited()


async def test_link_is_confirmed_chat_scoped_and_summary_compares_currencies(google_service):
    service, client, ctx, commit = google_service
    provider = AsyncMock(return_value=ledger())
    bridge = ExpenseService(service, provider)
    action = await bridge.propose_link(ctx, URL, "Japan")
    assert await service.repo.linked_trip(ctx.chat.key, "file") is None
    await bridge.commit(action, commit)
    assert await service.repo.linked_trip(ctx.chat.key, "file") == "trip-id"
    assert await service.repo.linked_trip("fake:other", "file") is None
    client.values.return_value = {
        "values": [
            VERSION,
            HEADER,
            ["Day 1", "09:00-10:00", "Food", "Food", "JPY 1200", "", "Confirmed", ""],
        ]
    }
    result = await ItineraryService(service, expenses=provider).summary(ctx, URL)
    assert "actual_totals" in result and "JPY" in result and "USD" not in result


async def test_selected_day_or_unknown_costs_do_not_compare_to_whole_trip_actuals(google_service):
    import json

    service, client, ctx, _ = google_service
    provider = AsyncMock(return_value=ledger())
    await service.repo.link(ctx.chat.key, "file", "trip-id", ctx.user.ref.key)
    client.values.return_value = {
        "values": [VERSION, HEADER, ["Day 1", "", "Food", "Food", "", "", "Confirmed", ""]]
    }
    result = json.loads(
        await ItineraryService(service, expenses=provider).summary(ctx, URL, tab="Tokyo")
    )
    assert "planned_minus_actual" not in result
    assert "comparison_note" in result
