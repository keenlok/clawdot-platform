"""Confirmed saved-file authority and compatibility; no real Drive or Telegram calls."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from chatcore.tools.registry import TurnState
from chatcore_google.service import DOC, DOCX, SHEET
from chatcore_google.trip_files import TripFilesService

URL = "https://drive.google.com/file/d/file/view"


@pytest.fixture
def trip_files(google_service):
    google, client, ctx, commit = google_service
    resolver = AsyncMock(
        return_value={
            "chat_key": ctx.chat.key,
            "trip_id": "existing-trip",
            "trip_name": "Sample & Example trip",
            "status": "planned",
        }
    )
    client.metadata.return_value = {
        "id": "file",
        "name": "Itinerary.docx",
        "mimeType": DOCX,
        "capabilities": {"canDownload": True},
    }
    return TripFilesService(google, resolver), resolver, client, ctx, commit


async def test_attach_preserves_trip_and_reads_later_without_posted_link(trip_files, docx_bytes):
    files, resolver, client, ctx, commit = trip_files
    action = await files.propose_attach(ctx, URL, "Sample & Example trip", "itinerary")
    assert "Sample & Example trip" in action.summary
    assert await files.google.repo.trip_files(ctx.chat.key, "existing-trip") == []
    client.media.assert_not_awaited()
    assert "attached" in (await files.commit(action, commit)).lower()
    client.media.return_value = docx_bytes
    later = replace(ctx, turn=TurnState())
    text = await files.read(later, "Sample & Example trip")
    assert "Tokyo itinerary" in text
    assert resolver.await_args.args[1] == "Sample & Example trip"
    assert (await files.google.repo.trip_files(ctx.chat.key, "existing-trip"))[0][
        "trip_id"
    ] == "existing-trip"
    client.request.assert_not_awaited()


async def test_any_binary_attaches_without_download_and_replacement_keeps_reference(trip_files):
    files, _, client, ctx, commit = trip_files
    client.metadata.return_value["mimeType"] = "image/png"
    a = await files.propose_attach(ctx, URL, "China", "itinerary")
    assert "not supported" in a.summary.lower()
    await files.commit(a, commit)
    assert "not supported" in (await files.read(replace(ctx, turn=TurnState()), "China")).lower()
    client.media.assert_not_awaited()
    other = replace(
        ctx, turn=TurnState(human_urls=frozenset({"https://drive.google.com/file/d/other/view"}))
    )
    client.metadata.return_value = {"id": "other", "name": "Other", "mimeType": DOC}
    a = await files.propose_attach(
        other, "https://drive.google.com/file/d/other/view", "China", "itinerary"
    )
    assert "reference" in a.summary.lower()
    await files.commit(a, commit)
    rows = await files.google.repo.trip_files(ctx.chat.key, "existing-trip")
    assert {r["file_id"]: r["role"] for r in rows} == {"file": "reference", "other": "itinerary"}


async def test_stale_confirmation_and_cross_trip_move_are_rejected(trip_files):
    files, resolver, _, ctx, commit = trip_files
    a = await files.propose_attach(ctx, URL, "China", "itinerary")
    stale = await files.propose_attach(ctx, URL, "China", "reference")
    await files.commit(a, commit)
    with pytest.raises(ValueError, match="changed|fresh"):
        await files.commit(stale, commit)
    resolver.return_value = {
        "chat_key": ctx.chat.key,
        "trip_id": "another-trip",
        "trip_name": "Other",
    }
    with pytest.raises(ValueError, match="another trip|other trip"):
        await files.propose_attach(ctx, URL, "Other", "reference")


async def test_remove_revokes_saved_authority_and_sheet_comparison_link(trip_files):
    files, _, client, ctx, commit = trip_files
    client.metadata.return_value["mimeType"] = SHEET
    await files.commit(await files.propose_attach(ctx, URL, "China", "itinerary"), commit)
    assert await files.google.repo.linked_trip(ctx.chat.key, "file") == "existing-trip"
    remove = await files.propose_remove(ctx, "China", "file")
    assert await files.google.repo.linked_trip(ctx.chat.key, "file") == "existing-trip"
    await files.commit(remove, commit)
    assert await files.google.repo.linked_trip(ctx.chat.key, "file") is None
    with pytest.raises(ValueError, match="itinerary|attached"):
        await files.read(replace(ctx, turn=TurnState()), "China", "file")
    client.request.assert_not_awaited()


@pytest.mark.parametrize("change", ["owner", "mime", "trashed", "scope", "off"])
async def test_confirm_rechecks_access_owner_type_scope_and_optout(trip_files, change):
    files, resolver, client, ctx, commit = trip_files
    action = await files.propose_attach(ctx, URL, "China", "itinerary")
    if change == "owner":
        client.identity.return_value = "new-owner"
    elif change == "mime":
        client.metadata.return_value["mimeType"] = SHEET
    elif change == "trashed":
        client.metadata.return_value["trashed"] = True
    elif change == "scope":
        resolver.return_value["chat_key"] = "fake:other"
    else:
        await files.google.repo.configure(ctx.chat.key, False, 0)
    with pytest.raises((ValueError, PermissionError)):
        await files.commit(action, commit)
    assert await files.google.repo.trip_files(ctx.chat.key, "existing-trip") == []


async def test_saved_read_rechecks_owner_and_cross_chat_cannot_inherit(trip_files):
    files, resolver, client, ctx, commit = trip_files
    await files.commit(await files.propose_attach(ctx, URL, "China", "itinerary"), commit)
    client.identity.return_value = "another-owner"
    with pytest.raises(ValueError, match="owner|identity"):
        await files.read(replace(ctx, turn=TurnState()), "China")
    client.media.assert_not_awaited()
    resolver.return_value["chat_key"] = "fake:other"
    with pytest.raises((ValueError, PermissionError), match="scope|group|chat"):
        await files.list(ctx, "China")


@pytest.mark.parametrize(
    "mime", ["application/vnd.google-apps.folder", "application/vnd.google-apps.shortcut"]
)
async def test_folder_and_shortcut_are_not_implicit_authority(trip_files, mime):
    files, _, client, ctx, _ = trip_files
    client.metadata.return_value["mimeType"] = mime
    with pytest.raises(ValueError, match="file|shortcut|folder"):
        await files.propose_attach(ctx, URL, "China", "reference")
    client.media.assert_not_awaited()
