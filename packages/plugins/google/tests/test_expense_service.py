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


def workbook_ledger(chat="fake:group", trip="trip-id", description="Lunch"):
    return {
        "chat_key": chat,
        "trip_id": trip,
        "trip_name": "Japan",
        "workbook": {
            "schema": 1,
            "title": "Expenses — Japan",
            "sheets": [
                {
                    "title": "Summary",
                    "rows": [
                        [{"k": "text", "v": description}, {"k": "money", "v": "10", "exp": 0}]
                    ],
                },
                {"title": "Ana", "rows": [[{"k": "text", "v": "x"}]]},
            ],
        },
        "preview": "Japan: 1 expense · 0 recorded repayments.",
        "expense_count": 1,
        "settlement_count": 0,
        "snapshot": "15 Nov 2026 19:09 (Asia/Singapore)",
    }


async def test_workbook_proposal_is_small_and_previews_without_writing(google_service):
    service, client, ctx, _ = google_service
    provider = AsyncMock(return_value=workbook_ledger())
    bridge = ExpenseService(service, None, provider)
    action = await bridge.propose_export(ctx, trip="Japan")
    assert action.kind == "expenses_export"
    assert "Japan: 1 expense" in action.summary and "read again when you confirm" in action.summary
    assert "Target: new private Google Sheet (sharing unchanged)" in action.summary
    assert "workbook" in action.payload and "sheets" not in str(action.payload)
    assert action.payload["ledger_trip"] == "trip-id"
    provider.assert_awaited_once_with(ctx, "Japan", False)
    client.request.assert_not_awaited()
    client.batch.assert_not_awaited()


async def test_workbook_commit_writes_latest_snapshot_once(google_service):
    service, client, ctx, commit = google_service
    provider = AsyncMock(return_value=workbook_ledger())
    bridge = ExpenseService(service, None, provider)
    action = await bridge.propose_export(ctx, trip="Japan", title="Trip money")
    provider.return_value = workbook_ledger(description="Added after preview")
    client.request.return_value = {"spreadsheetId": "book"}
    result = await bridge.commit(action, commit)
    assert result.startswith("Created https://docs.google.com/spreadsheets/d/book/edit")
    assert "snapshot 15 Nov 2026" in result and "does not update" in result
    provider.assert_awaited_with(commit, "trip-id", False)
    create = client.request.await_args.kwargs["json"]
    assert create["properties"]["title"] == "Trip money (incomplete)"
    written = str(client.batch.await_args_list[0].args[1])
    assert "Added after preview" in written  # latest data at confirmation (owner decision)
    final = client.batch.await_args_list[-1].args[1][-1]
    assert final["updateSpreadsheetProperties"]["properties"]["title"] == "Trip money"
    with pytest.raises(ValueError, match="handled"):
        await bridge.commit(action, commit)  # reservation: one confirmation, one workbook
    assert client.request.await_count == 1


async def test_workbook_commit_rechecks_scope_before_creating(google_service):
    service, client, ctx, commit = google_service
    provider = AsyncMock(return_value=workbook_ledger())
    bridge = ExpenseService(service, None, provider)
    action = await bridge.propose_export(ctx)
    provider.return_value = workbook_ledger(chat="fake:other")
    with pytest.raises(ValueError, match="scope"):
        await bridge.commit(action, commit)
    client.request.assert_not_awaited()


async def test_workbook_partial_failure_is_reported_honestly(google_service):
    service, client, ctx, commit = google_service
    bridge = ExpenseService(service, None, AsyncMock(return_value=workbook_ledger()))
    action = await bridge.propose_export(ctx)
    client.request.return_value = {"spreadsheetId": "half"}
    client.batch.side_effect = RuntimeError("network")
    result = await bridge.commit(action, commit)
    assert "spreadsheets/d/half/edit" in result and "incomplete" in result
    assert "Nothing was retried" in result
    assert client.batch.await_count == 1


async def test_workbook_cell_cap_refuses_at_preview(google_service):
    service, client, ctx, _ = google_service
    service.settings.export_max_cells = 1000
    big = workbook_ledger()
    big["workbook"]["sheets"][0]["rows"] = [[{"k": "number", "v": "1"}] * 20] * 60
    bridge = ExpenseService(service, None, AsyncMock(return_value=big))
    with pytest.raises(ValueError, match="1,000 cells"):
        await bridge.propose_export(ctx)
    client.request.assert_not_awaited()


async def test_legacy_pending_export_still_commits_with_workbook_provider(google_service):
    service, client, ctx, commit = google_service
    legacy = ExpenseService(service, AsyncMock(return_value=ledger()))
    action = await legacy.propose_export(ctx, trip="Japan")
    upgraded = ExpenseService(
        service, AsyncMock(return_value=ledger()), AsyncMock(return_value=workbook_ledger())
    )
    client.request.return_value = {
        "spreadsheetId": "csv",
        "sheets": [{"properties": {"sheetId": 0}}],
    }
    result = await upgraded.commit(action, commit)
    assert "spreadsheets/d/csv/edit" in result
    upgraded.workbook_provider.assert_not_awaited()
