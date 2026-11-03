from unittest.mock import AsyncMock

import pytest
from chatcore.tools.registry import ToolResult
from chatcore_google.itinerary import HEADER, VERSION
from chatcore_google.itinerary_service import ItineraryService

URL = "https://docs.google.com/spreadsheets/d/file/edit"
ROW = ["2026-12-01", "09:00-10:00", "Museum", "Sight", "JPY 1000", "", "Confirmed", ""]


async def test_drive_docx_import_is_read_and_previewed_without_writing(google_service, docx_bytes):
    service, client, ctx, commit = google_service
    client.metadata.return_value = {
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "capabilities": {"canDownload": True},
    }
    client.media.return_value = docx_bytes
    groups = [{"title": "Tokyo", "rows": [{"day": "Day 1", "place": "Museum"}]}]
    action = await ItineraryService(service).propose_import(
        ctx, groups, source_url=URL, title="Trip"
    )
    assert action.kind == "itinerary_import"
    assert action.payload["source_file_id"] == "file"
    assert "Tokyo itinerary" not in str(action.payload)
    client.media.assert_awaited_once_with("file")
    client.sheet.assert_not_awaited()
    client.create_sheet.assert_not_awaited()
    client.batch.assert_not_awaited()
    client.request.return_value = {"spreadsheetId": "new-trip"}
    await ItineraryService(service).commit(action, commit)
    assert client.media.await_count == 2  # Source content access is rechecked at confirmation.
    client.batch.assert_awaited_once()


async def test_docx_source_download_revocation_blocks_confirmed_import(google_service, docx_bytes):
    service, client, ctx, commit = google_service
    client.metadata.return_value = {
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "capabilities": {"canDownload": True},
    }
    client.media.return_value = docx_bytes
    itinerary = ItineraryService(service)
    action = await itinerary.propose_import(
        ctx, [{"title": "Day 1", "rows": [{"place": "Museum"}]}], source_url=URL, title="Trip"
    )
    client.metadata.return_value["capabilities"]["canDownload"] = False
    with pytest.raises(ValueError, match="download"):
        await itinerary.commit(action, commit)
    client.request.assert_not_awaited()
    client.batch.assert_not_awaited()


async def test_import_respects_total_destination_tab_limit(google_service):
    service, client, ctx, _ = google_service
    service.settings.max_tabs = 2
    itinerary = ItineraryService(
        service, attachment_reader=AsyncMock(return_value=ToolResult("CSV", untrusted=True))
    )
    with pytest.raises(ValueError, match="tab limit"):
        await itinerary.propose_import(
            ctx,
            [{"title": "Osaka", "rows": [{"day": "Day 3"}]}],
            source_message_ref="1",
            target=URL,
        )
    client.batch.assert_not_awaited()


async def test_pending_import_rechecks_total_destination_tab_limit(google_service):
    service, client, ctx, commit = google_service
    itinerary = ItineraryService(
        service, attachment_reader=AsyncMock(return_value=ToolResult("CSV", untrusted=True))
    )
    action = await itinerary.propose_import(
        ctx,
        [{"title": "Osaka", "rows": [{"day": "Day 3"}]}],
        source_message_ref="1",
        target=URL,
    )
    service.settings.max_tabs = 2
    with pytest.raises(ValueError, match="tab limit"):
        await itinerary.commit(action, commit)
    client.batch.assert_not_awaited()


async def test_summary_reads_all_tracker_tabs_and_reports_cross_tab_clashes(google_service):
    service, client, ctx, _ = google_service
    client.values.return_value = {"values": [VERSION, HEADER, ROW]}
    result = await ItineraryService(service).summary(ctx, URL)
    assert "Tokyo" in result and "Kyoto" in result and "overlaps" in result
    assert client.values.await_count == 2


async def test_csv_import_uses_one_source_and_creates_only_named_new_tabs(google_service):
    service, client, ctx, commit = google_service
    reader = AsyncMock(return_value=ToolResult("CSV rows", untrusted=True))
    itinerary = ItineraryService(service, attachment_reader=reader)
    client.values.return_value = {"values": [VERSION, HEADER, ROW]}
    groups = [{"title": "Day 2", "rows": [{"day": "2026-12-02", "place": "Temple"}]}]
    with pytest.raises(ValueError, match="one source"):
        await itinerary.propose_import(
            ctx, groups, source_url=URL, source_message_ref="1", target=URL
        )
    action = await itinerary.propose_import(ctx, groups, source_message_ref="1", target=URL)
    client.batch.assert_not_awaited()
    await itinerary.commit(action, commit)
    requests = client.batch.await_args.args[1]
    assert requests[0]["addSheet"]["properties"]["title"] == "Day 2"
    new_id = requests[0]["addSheet"]["properties"]["sheetId"]
    assert new_id not in (7, 8)
    assert requests[1]["updateCells"]["start"]["sheetId"] == new_id
    assert not any("deleteSheet" in value or "deleteDimension" in value for value in requests)


async def test_suggestions_are_frozen_confirmed_and_append_only_to_recognized_tabs(google_service):
    service, client, ctx, commit = google_service
    client.values.return_value = {"values": [VERSION, HEADER, ROW]}
    ctx.turn.seen_urls.add("https://example.com/food")
    itinerary = ItineraryService(service)
    groups = [
        {
            "title": "Kyoto",
            "rows": [
                {
                    "day": "2026-12-02",
                    "place": "Lunch",
                    "type": "Food",
                    "source_url": "https://example.com/food",
                    "status": "Confirmed",
                }
            ],
        }
    ]
    action = await itinerary.propose_suggest(ctx, URL, groups)
    groups[0]["rows"][0]["place"] = "changed"
    client.batch.assert_not_awaited()
    await itinerary.commit(action, commit)
    request = client.batch.await_args.args[1][0]
    assert request["appendCells"]["sheetId"] == 8
    assert "Proposed" in str(request) and "changed" not in str(request)


async def test_human_edits_between_preview_and_commit_force_a_fresh_preview(google_service):
    service, client, ctx, commit = google_service
    client.values.return_value = {"values": [VERSION, HEADER, ROW]}
    ctx.turn.seen_urls.add("https://example.com/food")
    itinerary = ItineraryService(service)
    action = await itinerary.propose_suggest(
        ctx,
        URL,
        [
            {
                "title": "Tokyo",
                "rows": [
                    {"day": "Day 2", "place": "Lunch", "source_url": "https://example.com/food"}
                ],
            }
        ],
    )
    client.values.return_value = {"values": [VERSION, HEADER, [*ROW[:6], "Done", "human edit"]]}
    with pytest.raises(ValueError, match="changed|preview"):
        await itinerary.commit(action, commit)
    client.batch.assert_not_awaited()


async def test_import_tab_collision_and_unknown_header_cannot_overwrite(google_service):
    service, client, ctx, _ = google_service
    reader = AsyncMock(return_value=ToolResult("CSV", untrusted=True))
    itinerary = ItineraryService(service, attachment_reader=reader)
    with pytest.raises(ValueError, match="exists|name"):
        await itinerary.propose_import(
            ctx,
            [{"title": "Tokyo", "rows": [{"day": "Day 2"}]}],
            source_message_ref="1",
            target=URL,
        )
    ctx.turn.seen_urls.add("https://example.com/food")
    client.values.return_value = {"values": [["wrong header"]]}
    with pytest.raises(ValueError, match="header|version"):
        await itinerary.propose_suggest(
            ctx,
            URL,
            [
                {
                    "title": "Tokyo",
                    "rows": [{"day": "Day 2", "source_url": "https://example.com/food"}],
                }
            ],
        )
    client.batch.assert_not_awaited()


async def test_new_trip_creates_one_spreadsheet_for_multiple_city_tabs(google_service):
    service, client, ctx, commit = google_service
    client.request.return_value = {"spreadsheetId": "newtrip"}
    itinerary = ItineraryService(
        service, attachment_reader=AsyncMock(return_value=ToolResult("CSV", untrusted=True))
    )
    groups = [
        {"title": title, "rows": [{"day": f"Day {i}", "place": title}]}
        for i, title in enumerate(["Tokyo", "Kyoto"], 1)
    ]
    action = await itinerary.propose_import(ctx, groups, source_message_ref="1", title="Japan")
    await itinerary.commit(action, commit)
    assert client.request.await_count == 1
    assert len(client.request.await_args.kwargs["json"]["sheets"]) == 2
    assert len(client.batch.await_args.args[1]) == 2
    assert (
        client.request.await_args.kwargs["json"]["sheets"][0]["properties"]["gridProperties"][
            "columnCount"
        ]
        == 8
    )


async def test_concurrent_suggestion_reservations_enforce_daily_limit(google_service):
    import asyncio

    service, _, ctx, _ = google_service
    service.settings.suggestion_rows_per_day = 3
    outcomes = await asyncio.gather(
        *[
            service.repo.reserve(ctx.chat.key, f"action-{index}", ctx.user.ref.key, 2, 3, 0)
            for index in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(value, ValueError) for value in outcomes) == 1


async def test_lowered_limits_reject_pending_itinerary_before_creating_file(google_service):
    service, client, ctx, commit = google_service
    client.request.return_value = {"spreadsheetId": "newtrip"}
    itinerary = ItineraryService(
        service, attachment_reader=AsyncMock(return_value=ToolResult("CSV", untrusted=True))
    )
    action = await itinerary.propose_import(
        ctx,
        [{"title": "Tokyo", "rows": [{"day": "Day 1"}, {"day": "Day 2"}]}],
        source_message_ref="1",
        title="Japan",
    )
    service.settings.max_rows = 1
    with pytest.raises(ValueError, match="row"):
        await itinerary.commit(action, commit)
    client.request.assert_not_awaited()
