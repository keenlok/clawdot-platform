from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from chatcore.access import AccessPolicy
from chatcore.plugin import CommandContext
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext, TurnState
from chatcore_google.repository import GoogleRepository
from chatcore_google.service import GoogleService
from chatcore_google.settings import GoogleSettings

URL = "https://docs.google.com/spreadsheets/d/file/edit#gid=7"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def setup():
    stores, platform, actor = memory_stores(), FakePlatform(), user("owner")
    access = AccessPolicy([GROUP.key], [actor.ref.key], owners=[actor.ref.key])
    settings = GoogleSettings(owner_email="owner@example.com", oauth_credentials_file="unused")
    client = AsyncMock()
    client.identity.return_value = "owner-id"
    client.metadata.return_value = {
        "id": "file",
        "name": "Trip",
        "mimeType": "application/vnd.google-apps.spreadsheet",
    }
    client.sheet.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]}
    client.values.return_value = {"values": [["Day", "Place"], ["1", "Lunch"]]}
    service = GoogleService(settings, access, client, GoogleRepository())
    ctx = ToolContext(
        GROUP,
        actor,
        False,
        platform,
        stores,
        turn=TurnState(human_urls=frozenset({URL})),
        is_admin=True,
    )
    commit = CommandContext(GROUP, actor, False, True, platform, stores)
    return service, client, ctx, commit


async def test_docx_read_uses_mime_and_preserves_native_doc_path(docx_bytes):
    service, client, ctx, _ = setup()
    client.metadata.return_value = {
        "mimeType": DOCX,
        "name": "Trip.docx",
        "size": str(len(docx_bytes)),
        "capabilities": {"canDownload": True},
    }
    client.media.return_value = docx_bytes
    result = await service.read_doc(ctx, URL)
    assert "Tokyo itinerary" in result and "main body" in result
    client.media.assert_awaited_once_with("file")
    client.document.assert_not_awaited()
    client.metadata.return_value = {
        "mimeType": "application/vnd.google-apps.document",
        "name": "Doc",
    }
    client.document.return_value = {"body": {"content": []}}
    await service.read_doc(ctx, URL)
    client.document.assert_awaited_once_with("file")


@pytest.mark.parametrize(
    "metadata",
    [
        {"mimeType": DOCX, "capabilities": {"canDownload": False}},
        {"mimeType": DOCX},
        {"mimeType": DOCX, "size": "999999999", "capabilities": {"canDownload": True}},
    ],
)
async def test_docx_download_restrictions_and_size_fail_before_media(metadata):
    service, client, ctx, _ = setup()
    client.metadata.return_value = metadata
    with pytest.raises(ValueError):
        await service.read_doc(ctx, URL)
    client.media.assert_not_awaited()


async def test_docx_provenance_optout_and_write_types_remain_closed(docx_bytes):
    service, client, ctx, _ = setup()
    client.metadata.return_value = {"mimeType": DOCX, "capabilities": {"canDownload": True}}
    client.media.return_value = docx_bytes
    ctx.turn.human_urls = frozenset()
    with pytest.raises(ValueError, match="human"):
        await service.read_doc(ctx, URL)
    client.identity.assert_not_awaited()
    ctx.turn.human_urls = frozenset({URL})
    with pytest.raises(ValueError):
        await service.propose(ctx, "append", url=URL, text="no")
    with pytest.raises(ValueError):
        await service.read_sheet(ctx, URL)
    client.media.assert_not_awaited()
    await service.repo.configure(ctx.chat.key, False, 0)
    with pytest.raises(PermissionError):
        await service.read_doc(ctx, URL)
    client.media.assert_not_awaited()


async def test_read_checks_provenance_before_authentication_and_returns_capped_data():
    service, client, ctx, _ = setup()
    ctx.turn.human_urls = frozenset()
    ctx.turn.seen_urls.add(URL)
    with pytest.raises(ValueError, match="human"):
        await service.read_sheet(ctx, URL)
    client.identity.assert_not_awaited()
    ctx.turn.human_urls = frozenset({URL})
    result = await service.read_sheet(ctx, URL)
    assert "Lunch" in result and "Day 1" in result
    assert client.values.await_args.args[1].startswith("'Day 1'!")


async def test_append_does_nothing_before_confirmation_and_rechecks_optout():
    service, client, ctx, commit = setup()
    action = await service.propose(ctx, "append", url=URL, rows=[["=BAD()", "Lunch"]])
    client.batch.assert_not_awaited()
    await service.repo.configure(GROUP.key, False, 0)
    with pytest.raises((ValueError, PermissionError), match="off|changed"):
        await service.commit(action, commit)
    client.batch.assert_not_awaited()


async def test_confirmed_append_uses_server_side_literal_append_and_cannot_replay():
    service, client, ctx, commit = setup()
    action = await service.propose(ctx, "append", url=URL, rows=[["=BAD()", "Lunch"]])
    result = await service.commit(action, commit)
    assert "appended" in result.lower()
    requests = client.batch.await_args.args[1]
    assert requests[0]["appendCells"]["sheetId"] == 7
    assert (
        requests[0]["appendCells"]["rows"][0]["values"][0]["userEnteredValue"]["stringValue"]
        == "'=BAD()"
    )
    with pytest.raises(ValueError, match="handled|uncertain"):
        await service.commit(action, commit)
    assert client.batch.await_count == 1


async def test_pending_owner_change_permission_revocation_and_wrong_chat_do_not_write():
    for change in ("owner", "permission", "chat"):
        service, client, ctx, commit = setup()
        action = await service.propose(ctx, "append", url=URL, rows=[["ok"]])
        if change == "owner":
            client.identity.return_value = "different"
        elif change == "permission":
            service.access.admin_users = frozenset()
            service.access.owners = frozenset()
        else:
            commit = replace(commit, chat=type(GROUP)("fake", "elsewhere"))
        with pytest.raises((ValueError, PermissionError)):
            await service.commit(action, commit)
        client.batch.assert_not_awaited()


async def test_optout_is_chat_scoped_and_can_be_reenabled_by_manager():
    service, client, ctx, commit = setup()
    action = await service.propose_configure(ctx, False)
    await service.commit(action, commit)
    with pytest.raises(PermissionError, match="off"):
        await service.read_sheet(ctx, URL)
    action = await service.propose_configure(ctx, True)
    await service.commit(action, commit)
    assert "Lunch" in await service.read_sheet(ctx, URL)
    assert (await service.repo.settings("fake:other")) == (True, 0)


async def test_ambiguous_tab_range_and_unknown_native_type_are_refused():
    service, client, ctx, _ = setup()
    for cell_range in ["A:A", "A1:ZZZ999999", 'IMPORTRANGE("x")', "Other!A1"]:
        with pytest.raises(ValueError):
            await service.read_sheet(ctx, URL, cell_range=cell_range)
    with pytest.raises(ValueError):
        await service.read_sheet(ctx, URL, tab="Other")
    client.metadata.return_value["mimeType"] = "text/csv"
    with pytest.raises(ValueError, match="attach|CSV"):
        await service.read_sheet(ctx, URL)


async def test_create_is_confirmed_with_frozen_rows_and_no_automatic_sharing():
    service, client, ctx, commit = setup()
    rows = [["Name", "Cost"], ["Lunch", 12]]
    client.request.return_value = {
        "spreadsheetId": "created",
        "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}],
    }
    action = await service.propose(ctx, "create_sheet", title="Trip", rows=rows)
    rows[1][0] = "changed after preview"
    assert "Lunch" in action.summary
    client.request.assert_not_awaited()
    result = await service.commit(action, commit)
    assert "/created" in result
    assert "Lunch" in str(client.batch.await_args)
    assert "permissions" not in str(client.request.await_args)


async def test_large_ledger_preview_is_attached_in_full_before_confirmation():
    service, client, ctx, _ = setup()
    rows = [[str(index), "x" * 100] for index in range(40)]
    action = await service.propose(ctx, "create_sheet", title="Ledger", rows=rows)
    assert len(action.summary) < 3500 and "attached" in action.summary
    assert len(ctx.platform.sent_files) == 1
    assert "39" in ctx.platform.sent_files[0].data.decode()
    assert len(action.payload["rows"]) == 40
    client.request.assert_not_awaited()


async def test_default_read_is_bounded_by_actual_tab_grid_not_just_configured_caps():
    service, client, ctx, _ = setup()
    client.sheet.return_value["sheets"][0]["properties"]["gridProperties"] = {
        "rowCount": 10,
        "columnCount": 26,
    }
    await service.read_sheet(ctx, URL)
    assert client.values.await_args.args[1] == "'Day 1'!A1:Z10"


async def test_append_rejects_rows_wider_than_existing_grid_before_confirmation():
    service, client, ctx, _ = setup()
    client.sheet.return_value["sheets"][0]["properties"]["gridProperties"] = {
        "rowCount": 10,
        "columnCount": 2,
    }
    with pytest.raises(ValueError, match="column|grid"):
        await service.propose(ctx, "append", url=URL, rows=[["one", "two", "three"]])
    client.batch.assert_not_awaited()


async def test_doc_append_refuses_sheet_tab_arguments_instead_of_ignoring_them():
    service, client, ctx, _ = setup()
    client.metadata.return_value["mimeType"] = "application/vnd.google-apps.document"
    with pytest.raises(ValueError, match="tab"):
        await service.propose(ctx, "append", url=URL, text="hello", tab="some tab")


async def test_lowered_limits_reject_a_pending_creation_before_any_remote_write():
    service, client, ctx, commit = setup()
    client.request.return_value = {
        "spreadsheetId": "created",
        "sheets": [{"properties": {"sheetId": 0}}],
    }
    action = await service.propose(ctx, "create_sheet", title="Trip", rows=[["a"], ["b"]])
    service.settings.max_rows = 1
    with pytest.raises(ValueError, match="row"):
        await service.commit(action, commit)
    client.request.assert_not_awaited()


async def test_lowered_text_limit_rejects_pending_doc_append_before_write():
    service, client, ctx, commit = setup()
    client.metadata.return_value["mimeType"] = "application/vnd.google-apps.document"
    action = await service.propose(ctx, "append", url=URL, text="Hello world")
    service.settings.max_chars = 5
    with pytest.raises(ValueError, match="character limit"):
        await service.commit(action, commit)
    client.request.assert_not_awaited()
