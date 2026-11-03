"""Format dispatch and saved-trip authority through the real confirmation flow."""

import copy
import io
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from chatcore.confirm import EXPIRED, ConfirmService
from chatcore.testing import press
from chatcore.tools.registry import ToolRegistry, TurnState
from chatcore_google.plugin import GooglePlugin
from chatcore_google.service import DOC, DOCX, SHEET
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

URL = "https://drive.google.com/file/d/file/view"
TRIP = "Sample & Example trip"
FORMATS = [DOC, SHEET, DOCX, "application/pdf", "text/csv", "image/png"]


def _pdf_bytes():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Hong Kong itinerary) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _setup(google_service, mime, docx_bytes):
    service, client, ctx, command = google_service
    ctx = replace(ctx, turn=TurnState(human_urls=frozenset({URL})))
    trip = {
        "chat_key": ctx.chat.key,
        "trip_id": "existing-trip",
        "trip_name": TRIP,
        "status": "planned",
    }
    resolver = AsyncMock(return_value=trip)
    plugin = GooglePlugin(service.settings, service.access, service=service, trip_resolver=resolver)
    registry = ToolRegistry(plugin.tools())
    client.metadata.return_value = {
        "id": "file",
        # Misleading suffix ensures dispatch uses verified MIME rather than the filename.
        "name": "Trip.docx",
        "mimeType": mime,
        "modifiedTime": "2026-11-02T00:00:00Z",
        "capabilities": {"canDownload": True},
    }
    client.document.return_value = {
        "body": {
            "content": [
                {"paragraph": {"elements": [{"textRun": {"content": "Hong Kong itinerary"}}]}}
            ]
        }
    }
    client.values.return_value = {"values": [["City", "Activity"], ["Hong Kong", "Lunch"]]}
    client.media.return_value = {
        DOCX: docx_bytes,
        "application/pdf": _pdf_bytes(),
        "text/csv": b"City,Activity\r\nHong Kong,Lunch\r\n",
    }.get(mime, b"not downloaded")
    confirm = ConfirmService(
        ctx.platform,
        ctx.stores.repo,
        {"google." + key: handler for key, handler in plugin.action_handlers().items()},
    )
    return service, client, ctx, command, registry, confirm, trip


async def _attach(setup):
    service, client, ctx, command, registry, confirm, trip = setup
    result = await registry.call(
        "trip_attach_file", {"trip": TRIP, "url": URL, "role": "itinerary"}, ctx
    )
    assert not result.is_error and result.pending_action
    assert await service.repo.trip_files(ctx.chat.key, trip["trip_id"]) == []
    client.media.assert_not_awaited()
    client.document.assert_not_awaited()
    client.values.assert_not_awaited()
    await confirm.request(result.pending_action, ctx.user)
    assert await service.repo.trip_files(ctx.chat.key, trip["trip_id"]) == []
    answer = await confirm.on_press(press(result.pending_action.id, sender=ctx.user), command)
    assert "attached" in answer.lower()
    assert (
        await confirm.on_press(press(result.pending_action.id, sender=ctx.user), command) == EXPIRED
    )


@pytest.mark.parametrize("mime", FORMATS)
async def test_generic_reader_dispatches_by_mime_and_marks_private_content(
    google_service, docx_bytes, mime
):
    service, client, ctx, _, registry, _, _ = _setup(google_service, mime, docx_bytes)
    original = copy.deepcopy(client.metadata.return_value)
    result = await registry.call("google_read_file", {"url": URL}, ctx)
    assert not result.is_error and result.untrusted and not result.persist_content
    expected = {DOCX: "Tokyo itinerary", "image/png": "not supported"}.get(mime, "Hong Kong")
    assert expected in result.content
    if mime == DOC:
        client.document.assert_awaited_once_with("file")
        client.media.assert_not_awaited()
    elif mime == SHEET:
        client.sheet.assert_awaited_once_with("file")
        client.media.assert_not_awaited()
    elif mime == "image/png":
        client.media.assert_not_awaited()
        assert "content was not read" in result.content.lower()
    else:
        client.media.assert_awaited_once_with("file")
    assert client.metadata.return_value == original
    client.request.assert_not_awaited()
    client.batch.assert_not_awaited()
    assert await service.repo.trip_files(ctx.chat.key, "existing-trip") == []


@pytest.mark.parametrize("mime", FORMATS)
async def test_confirmed_trip_file_lifecycle_reads_without_reposting_and_never_edits_source(
    google_service, docx_bytes, mime
):
    setup = _setup(google_service, mime, docx_bytes)
    service, client, ctx, command, registry, confirm, trip = setup
    original_trip = copy.deepcopy(trip)
    original_metadata = copy.deepcopy(client.metadata.return_value)
    await _attach(setup)
    later = replace(ctx, turn=TurnState())
    listed = await registry.call("trip_list_files", {"trip": TRIP}, later)
    assert not listed.is_error and listed.untrusted and not listed.persist_content
    rows = json.loads(listed.content)["files"]
    assert len(rows) == 1 and rows[0]["role"] == "itinerary"
    assert rows[0]["readable"] == (mime != "image/png")
    result = await registry.call("trip_read_file", {"trip": TRIP}, later)
    assert not result.is_error and result.untrusted and not result.persist_content
    expected = {DOCX: "Tokyo itinerary", "image/png": "not supported"}.get(mime, "Hong Kong")
    assert expected in result.content
    assert not later.turn.human_urls
    assert trip == original_trip
    assert client.metadata.return_value == original_metadata
    if mime == "image/png":
        client.media.assert_not_awaited()
    client.request.assert_not_awaited()
    client.batch.assert_not_awaited()
    remove = await registry.call("trip_remove_file", {"trip": TRIP, "file_id": "file"}, later)
    assert not remove.is_error and remove.pending_action
    assert len(await service.repo.trip_files(ctx.chat.key, trip["trip_id"])) == 1
    await confirm.request(remove.pending_action, ctx.user)
    answer = await confirm.on_press(press(remove.pending_action.id, sender=ctx.user), command)
    assert "removed" in answer.lower() and "kept" in answer.lower()
    assert await service.repo.trip_files(ctx.chat.key, trip["trip_id"]) == []
    unreadable = await registry.call("trip_read_file", {"trip": TRIP}, later)
    assert unreadable.is_error
    assert trip == original_trip and client.metadata.return_value == original_metadata
    client.request.assert_not_awaited()
    client.batch.assert_not_awaited()


@pytest.mark.parametrize("mime", [DOCX, "application/pdf", "text/csv"])
@pytest.mark.parametrize("failure", ["corrupt", "download_denied"])
async def test_unreadable_content_can_still_be_attached_without_claiming_a_read(
    google_service, docx_bytes, mime, failure
):
    setup = _setup(google_service, mime, docx_bytes)
    service, client, ctx, _, registry, _, trip = setup
    if failure == "corrupt":
        client.media.return_value = b'\xff"malformed'
    else:
        client.metadata.return_value["capabilities"]["canDownload"] = False
    await _attach(setup)
    result = await registry.call("trip_read_file", {"trip": TRIP}, replace(ctx, turn=TurnState()))
    assert result.is_error
    assert len(await service.repo.trip_files(ctx.chat.key, trip["trip_id"])) == 1
    if failure == "download_denied":
        client.media.assert_not_awaited()
    client.request.assert_not_awaited()
    client.batch.assert_not_awaited()


@pytest.mark.parametrize("bad_metadata", [{"id": "different"}, {"trashed": True}])
async def test_generic_read_and_attach_reject_changed_or_trashed_metadata(
    google_service, docx_bytes, bad_metadata
):
    setup = _setup(google_service, DOCX, docx_bytes)
    service, client, ctx, _, registry, _, _ = setup
    client.metadata.return_value.update(bad_metadata)
    read = await registry.call("google_read_file", {"url": URL}, ctx)
    attached = await registry.call(
        "trip_attach_file", {"trip": TRIP, "url": URL, "role": "itinerary"}, ctx
    )
    assert read.is_error and attached.is_error and not attached.pending_action
    assert await service.repo.trip_files(ctx.chat.key, "existing-trip") == []
    client.media.assert_not_awaited()


async def test_generic_reader_and_attach_reject_unposted_links(google_service, docx_bytes):
    setup = _setup(google_service, DOCX, docx_bytes)
    service, client, ctx, _, registry, _, _ = setup
    unposted = replace(ctx, turn=TurnState())
    read = await registry.call("google_read_file", {"url": URL}, unposted)
    attached = await registry.call(
        "trip_attach_file", {"trip": TRIP, "url": URL, "role": "itinerary"}, unposted
    )
    assert read.is_error and attached.is_error
    assert await service.repo.trip_files(ctx.chat.key, "existing-trip") == []
    client.metadata.assert_not_awaited()
    client.media.assert_not_awaited()
