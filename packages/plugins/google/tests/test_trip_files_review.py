"""Regression checks from the independent saved-file authority review."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from chatcore.events import ChatRef
from chatcore.tools.registry import TurnState
from chatcore_google.expense_service import ExpenseService
from chatcore_google.service import DOCX, SHEET
from chatcore_google.trip_files import TripFilesService

URL = "https://drive.google.com/file/d/file/view"


@pytest.fixture
def reviewed_files(google_service):
    google, client, ctx, command = google_service
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
    return TripFilesService(google, resolver), client, ctx, command


async def test_dm_commit_rejects_group_optout_while_metadata_is_in_flight(reviewed_files):
    files, client, ctx, command = reviewed_files
    dm = ChatRef(ctx.chat.platform, ctx.user.ref.id)
    dm_ctx = replace(ctx, chat=dm, is_dm=True)
    dm_command = replace(command, chat=dm, is_dm=True)
    action = await files.propose_attach(dm_ctx, URL, "China", "itinerary")
    metadata = dict(client.metadata.return_value)

    async def disable_group(_file_id):
        await files.google.repo.configure(ctx.chat.key, False, 0)
        return metadata

    client.metadata.side_effect = disable_group
    with pytest.raises((ValueError, PermissionError), match="changed|off|settings|revision"):
        await files.commit(action, dm_command)
    assert await files.google.repo.trip_files(ctx.chat.key, "existing-trip") == []


async def test_saved_read_does_not_return_content_after_association_is_removed(
    reviewed_files, docx_bytes
):
    files, client, ctx, command = reviewed_files
    await files.commit(await files.propose_attach(ctx, URL, "China", "itinerary"), command)

    async def revoke_during_download(_file_id):
        repo = files.google.repo
        rows = await repo.trip_files(ctx.chat.key, "existing-trip")
        await repo.apply_file(ctx.chat.key, "existing-trip", "file", None, repo.file_state(rows))
        return docx_bytes

    client.media.side_effect = revoke_during_download
    with pytest.raises((ValueError, PermissionError), match="changed|removed|attached|association"):
        await files.read(replace(ctx, turn=TurnState()), "China")


async def test_saved_read_does_not_return_content_after_group_opts_out(reviewed_files, docx_bytes):
    files, client, ctx, command = reviewed_files
    await files.commit(await files.propose_attach(ctx, URL, "China", "itinerary"), command)

    async def disable_during_download(_file_id):
        await files.google.repo.configure(ctx.chat.key, False, 0)
        return docx_bytes

    client.media.side_effect = disable_during_download
    with pytest.raises((ValueError, PermissionError), match="off|changed|settings"):
        await files.read(replace(ctx, turn=TurnState()), "China")


async def test_legacy_sheet_link_in_dm_uses_resolved_group_scope(reviewed_files):
    files, client, ctx, command = reviewed_files
    client.metadata.return_value["mimeType"] = SHEET
    provider = AsyncMock(
        return_value={
            "chat_key": ctx.chat.key,
            "trip_id": "existing-trip",
            "trip_name": "Sample & Example trip",
        }
    )
    expenses = ExpenseService(files.google, provider)
    dm = ChatRef(ctx.chat.platform, ctx.user.ref.id)
    dm_ctx = replace(ctx, chat=dm, is_dm=True)
    dm_command = replace(command, chat=dm, is_dm=True)
    action = await expenses.propose_link(dm_ctx, URL, "China")
    await expenses.commit(action, dm_command)
    assert await files.google.repo.trip_file(ctx.chat.key, "file") is not None
    assert await files.google.repo.trip_file(dm.key, "file") is None
    assert '"file_id": "file"' in await files.list(dm_ctx, "China")


async def test_legacy_sheet_link_in_dm_honors_target_group_optout(reviewed_files):
    files, client, ctx, _command = reviewed_files
    client.metadata.return_value["mimeType"] = SHEET
    provider = AsyncMock(
        return_value={
            "chat_key": ctx.chat.key,
            "trip_id": "existing-trip",
            "trip_name": "Sample & Example trip",
        }
    )
    expenses = ExpenseService(files.google, provider)
    await files.google.repo.configure(ctx.chat.key, False, 0)
    dm_ctx = replace(ctx, chat=ChatRef(ctx.chat.platform, ctx.user.ref.id), is_dm=True)
    with pytest.raises((ValueError, PermissionError), match="off|settings|group"):
        await expenses.propose_link(dm_ctx, URL, "China")


async def test_saved_read_rechecks_association_after_final_scope_refresh(
    reviewed_files, docx_bytes
):
    files, client, ctx, command = reviewed_files
    await files.commit(await files.propose_attach(ctx, URL, "China", "itinerary"), command)
    client.media.return_value = docx_bytes
    identity = dict(files.resolver.return_value)
    calls = 0

    async def revoke_during_final_scope_refresh(_ctx, _trip):
        nonlocal calls
        calls += 1
        if calls == 2:
            repo = files.google.repo
            rows = await repo.trip_files(ctx.chat.key, "existing-trip")
            await repo.apply_file(
                ctx.chat.key, "existing-trip", "file", None, repo.file_state(rows)
            )
        return identity

    files.resolver.side_effect = revoke_during_final_scope_refresh
    with pytest.raises((ValueError, PermissionError), match="changed|association|attached"):
        await files.read(replace(ctx, turn=TurnState()), "China")
