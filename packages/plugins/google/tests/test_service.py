import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from chatcore.access import AccessPolicy
from chatcore.plugin import CommandContext
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext, TurnState
from chatcore_google.client import GoogleError
from chatcore_google.repository import GoogleRepository
from chatcore_google.service import SHEET, GoogleService
from chatcore_google.settings import GoogleSettings

URL = "https://docs.google.com/spreadsheets/d/file/edit#gid=7"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.parametrize("kind", ["add_sheet_tab", "replace_sheet_tab"])
async def test_sheet_tab_arguments_validate_before_identity_or_metadata(kind):
    service, client, ctx, _ = setup()
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    tab = {"name": "Valid", "columns": [], "rows": []}
    expected_error = "columns" if kind == "add_sheet_tab" else "name"
    with pytest.raises(ValueError, match=expected_error):
        await service.propose(
            ctx,
            kind,
            url=URL,
            tab=tab,
            tab_name="Different" if kind == "replace_sheet_tab" else None,
        )
    client.identity.assert_not_awaited()
    client.metadata.assert_not_awaited()


async def test_replace_sheet_tab_invalid_content_stops_before_identity_or_metadata():
    service, client, ctx, _ = setup()
    with pytest.raises(ValueError, match="columns"):
        await service.propose(
            ctx,
            "replace_sheet_tab",
            url=URL,
            tab_name="Matching",
            tab={"name": "Matching", "columns": [], "rows": []},
        )
    client.identity.assert_not_awaited()
    client.metadata.assert_not_awaited()


@pytest.mark.parametrize("kind", ["add_sheet_tab", "replace_sheet_tab", "trash_file"])
async def test_output_edit_reauthorizes_after_last_read_before_write(kind):
    service, client, ctx, _ = setup()
    service.settings.create_users = "members"
    member = user("member")
    ctx.user = member
    ctx.is_admin = False
    client.metadata.return_value.update(
        ownedByMe=True,
        trashed=False,
        owners=[{"permissionId": "owner-id"}],
    )
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, member.ref.key, ctx.thread_id, "owner-id", "created"
    )
    tab = {"name": "Next", "columns": [{"header": "Place", "type": "text"}], "rows": []}
    if kind == "add_sheet_tab":
        client.sheet.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]}
        action = await service.propose(ctx, kind, url=URL, tab=tab)

        async def final_sheet_read(_file_id):
            service.settings.create_users = "writers"
            return {"sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]}

        client.sheet.side_effect = final_sheet_read
    elif kind == "replace_sheet_tab":
        sheet = {"sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]}
        client.sheet_for_replace.return_value = sheet
        tab["name"] = "Day 1"
        action = await service.propose(ctx, kind, url=URL, tab_name="Day 1", tab=tab)

        async def final_replace_read(_file_id):
            service.settings.create_users = "writers"
            return sheet

        client.sheet_for_replace.side_effect = final_replace_read
    else:
        action = await service.propose(ctx, kind, url=URL)

        async def final_metadata_read(_file_id):
            service.settings.create_users = "writers"
            return client.metadata.return_value

        client.metadata.side_effect = final_metadata_read

    with pytest.raises(PermissionError, match="creation|create"):
        await service.commit(action, ctx)
    client.batch.assert_not_awaited()
    client.trash_file.assert_not_awaited()


@pytest.mark.parametrize("kind", ["add_sheet_tab", "replace_sheet_tab"])
async def test_sheet_tab_commit_refuses_freshly_trashed_file(kind):
    service, client, ctx, _ = setup()
    client.metadata.return_value.update(ownedByMe=True, trashed=False)
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    tab = {"name": "Next", "columns": [{"header": "Place", "type": "text"}], "rows": []}
    client.sheet.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]}
    client.sheet_for_replace.return_value = {
        "sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]
    }
    if kind == "add_sheet_tab":
        action = await service.propose(ctx, kind, url=URL, tab=tab)
    else:
        tab["name"] = "Day 1"
        action = await service.propose(ctx, kind, url=URL, tab_name="Day 1", tab=tab)
    client.metadata.return_value["trashed"] = True
    with pytest.raises(ValueError, match="trash"):
        await service.commit(action, ctx)
    client.batch.assert_not_awaited()


def created_doc(text):
    boundary = 1 + len(text.encode("utf-16-le")) // 2
    return {
        "revisionId": "rev-1",
        "tabs": [
            {
                "tabProperties": {"tabId": "created-tab", "title": "Output"},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "startIndex": 1,
                                "endIndex": boundary + 1,
                                "paragraph": {
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": boundary,
                                            "textRun": {"content": text},
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                },
            }
        ],
    }


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


async def test_rich_doc_validation_precedes_google_identity():
    service, client, ctx, _ = setup()
    with pytest.raises(ValueError, match=r"Block 1 \(paragraph\)"):
        await service.propose(
            ctx,
            "create_rich_doc",
            title="Bad",
            blocks=[{"type": "paragraph", "text": "line\nbreak"}],
        )
    client.identity.assert_not_awaited()
    client.request.assert_not_awaited()


async def test_add_sheet_tab_freezes_owned_output_and_rejects_name_collision():
    service, client, ctx, _ = setup()
    client.metadata.return_value.update(ownedByMe=True, trashed=False)
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    tab = {"name": "Next", "columns": [{"header": "Place", "type": "text"}], "rows": []}
    action = await service.propose(ctx, "add_sheet_tab", url=URL, tab=tab)
    assert action.kind == "add_sheet_tab"
    assert action.payload["via_output"] is True
    assert action.payload["target"] == "own_output"
    assert action.payload["tab"] == tab
    client.sheet.return_value = {
        "sheets": [
            {"properties": {"sheetId": 7, "title": "Day 1"}},
            {"properties": {"sheetId": 8, "title": "NEXT"}},
        ]
    }
    with pytest.raises(ValueError, match="name"):
        await service.propose(ctx, "add_sheet_tab", url=URL, tab=tab)


async def test_human_sheet_add_tab_is_marked_for_confirmation():
    from chatcore_google.plugin import GooglePlugin

    service, client, ctx, _ = setup()
    client.metadata.return_value.update(ownedByMe=False, trashed=False)
    tab = {"name": "Next", "columns": [{"header": "Place", "type": "text"}], "rows": []}
    await service.repo.configure_fields(ctx.chat.key, confirm_writes=False, expected_revision=0)
    plugin = GooglePlugin(service.settings, service.access, service=service)
    result = await plugin.add_sheet_tab(ctx, URL, tab)
    assert result.pending_action is not None
    assert result.pending_action.payload["via_output"] is False
    assert result.pending_action.payload["target"] == "human"


async def test_own_output_tab_edit_runs_when_previews_are_off():
    from chatcore_google.plugin import GooglePlugin

    service, client, ctx, _ = setup()
    client.metadata.return_value.update(ownedByMe=True, trashed=False)
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    await service.repo.configure_fields(ctx.chat.key, confirm_writes=False, expected_revision=0)
    client.sheet.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]}
    plugin = GooglePlugin(service.settings, service.access, service=service)
    tab = {"name": "Next", "columns": [{"header": "Place", "type": "text"}], "rows": []}
    result = await plugin.add_sheet_tab(ctx, URL, tab)
    assert result.pending_action is None
    assert "Added tab" in result.content
    client.batch.assert_awaited()


async def test_member_can_edit_own_output_but_not_another_members_output():
    from chatcore.testing import user

    service, client, ctx, _ = setup()
    service.settings.create_users = "members"
    member = user("member")
    ctx.user = member
    ctx.is_admin = False
    ctx.turn.human_urls = frozenset({URL})
    client.metadata.return_value.update(ownedByMe=True, trashed=False)
    tab = {"name": "Next", "columns": [{"header": "Place", "type": "text"}], "rows": []}
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, member.ref.key, ctx.thread_id, "owner-id", "created"
    )
    action = await service.propose(ctx, "add_sheet_tab", url=URL, tab=tab)
    assert action.payload["target"] == "own_output"

    service, client, ctx, _ = setup()
    service.settings.create_users = "members"
    ctx.user = user("other-member")
    ctx.is_admin = False
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, "fake:requester", ctx.thread_id, "owner-id", "created"
    )
    client.metadata.return_value.update(ownedByMe=True, trashed=False)
    with pytest.raises(PermissionError):
        await service.propose(ctx, "add_sheet_tab", url=URL, tab=tab)


async def test_replace_sheet_tab_clears_and_rewrites_only_selected_tab():
    service, client, ctx, _ = setup()
    client.metadata.return_value.update(ownedByMe=True, trashed=False)
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    sheet = {
        "sheets": [
            {
                "properties": {"sheetId": 7, "title": "Day 1"},
                "bandedRanges": [{"bandedRangeId": 3, "range": {"sheetId": 7}}],
                "basicFilter": {"range": {"sheetId": 7}},
                "conditionalFormats": [{}, {}],
                "merges": [{"sheetId": 7}],
            },
            {"properties": {"sheetId": 8, "title": "Other"}},
        ]
    }
    client.sheet_for_replace.return_value = sheet
    tab = {
        "name": "Day 1",
        "columns": [{"header": "Place", "type": "text"}],
        "rows": [["New"]],
    }
    action = await service.propose(ctx, "replace_sheet_tab", url=URL, tab_name="Day 1", tab=tab)
    result = await service.commit(action, ctx)
    assert 'Replaced tab "Day 1"' in result
    clear = client.batch.await_args_list[0].args[1]
    assert [next(iter(request)) for request in clear] == [
        "deleteBanding",
        "clearBasicFilter",
        "deleteConditionalFormatRule",
        "deleteConditionalFormatRule",
        "unmergeCells",
        "updateCells",
        "updateSheetProperties",
    ]
    assert clear[2]["deleteConditionalFormatRule"]["index"] == 1
    assert clear[3]["deleteConditionalFormatRule"]["index"] == 0
    all_requests = [request for call in client.batch.await_args_list for request in call.args[1]]
    assert all(
        sheet_id == 7 for request in all_requests for sheet_id in _request_sheet_ids(request)
    )
    assert all("updateSpreadsheetProperties" not in request for request in all_requests)


async def test_replace_sheet_tab_refuses_unsupported_metadata_and_changed_target():
    tab = {"name": "Day 1", "columns": [{"header": "Place", "type": "text"}], "rows": []}
    for key, value in (
        ("charts", [{"chartId": 1}]),
        ("protectedRanges", [{"protectedRangeId": 2}]),
        ("filterViews", [{"filterViewId": 3}]),
    ):
        service, client, ctx, _ = setup()
        client.metadata.return_value.update(ownedByMe=True, trashed=False)
        await service.repo.record_output(
            ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
        )
        client.sheet_for_replace.return_value = {
            "sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}, key: value}]
        }
        with pytest.raises(ValueError, match=key):
            await service.propose(ctx, "replace_sheet_tab", url=URL, tab_name="Day 1", tab=tab)

    service, client, ctx, _ = setup()
    client.metadata.return_value.update(ownedByMe=True, trashed=False)
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    client.sheet_for_replace.return_value = {
        "sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]
    }
    action = await service.propose(ctx, "replace_sheet_tab", url=URL, tab_name="Day 1", tab=tab)
    client.sheet_for_replace.return_value = {
        "sheets": [{"properties": {"sheetId": 9, "title": "Day 1"}}]
    }
    with pytest.raises(ValueError, match="changed"):
        await service.commit(action, ctx)
    client.batch.assert_not_awaited()


async def test_replace_sheet_tab_reports_partial_after_clear():
    service, client, ctx, _ = setup()
    client.metadata.return_value.update(ownedByMe=True, trashed=False)
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    client.sheet_for_replace.return_value = {
        "sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]
    }
    tab = {"name": "Day 1", "columns": [{"header": "Place", "type": "text"}], "rows": []}
    action = await service.propose(ctx, "replace_sheet_tab", url=URL, tab_name="Day 1", tab=tab)
    client.batch.side_effect = [{}, RuntimeError("request failed")]
    result = await service.commit(action, ctx)
    assert "the tab was cleared but not fully rewritten" in result
    assert "https://docs.google.com/spreadsheets/d/file/edit" in result


async def test_add_sheet_tab_commits_add_sheet_before_data_and_rewrites_ids():
    service, client, ctx, _ = setup()
    client.metadata.return_value.update(ownedByMe=True, trashed=False)
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    client.sheet.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]}
    service.sheet_id_generator = lambda low, high: 902
    tab = {"name": "Next", "columns": [{"header": "Place", "type": "text"}], "rows": []}
    action = await service.propose(ctx, "add_sheet_tab", url=URL, tab=tab)
    result = await service.commit(action, ctx)
    assert result == 'Added tab "Next" to https://docs.google.com/spreadsheets/d/file/edit.'
    calls = client.batch.await_args_list
    assert calls[0].args[0] == "file"
    add = calls[0].args[1][0]["addSheet"]
    assert add["properties"]["sheetId"] == 902
    assert add["properties"]["index"] == 1
    assert all(
        sheet_id == 902
        for call in calls
        for request in call.args[1]
        for sheet_id in _request_sheet_ids(request)
    )


def _request_sheet_ids(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "sheetId":
                yield item
            else:
                yield from _request_sheet_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _request_sheet_ids(item)


async def test_add_sheet_tab_collision_at_commit_and_partial_write_are_reported():
    service, client, ctx, _ = setup()
    client.metadata.return_value.update(ownedByMe=True, trashed=False)
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    client.sheet.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]}
    tab = {"name": "Next", "columns": [{"header": "Place", "type": "text"}], "rows": []}
    action = await service.propose(ctx, "add_sheet_tab", url=URL, tab=tab)
    client.sheet.return_value = {
        "sheets": [
            {"properties": {"sheetId": 7, "title": "Day 1"}},
            {"properties": {"sheetId": 8, "title": "next"}},
        ]
    }
    with pytest.raises(ValueError, match="now exists"):
        await service.commit(action, ctx)
    client.batch.assert_not_awaited()

    service, client, ctx, _ = setup()
    service.settings.max_tabs = 1
    client.metadata.return_value.update(ownedByMe=False, trashed=False)
    client.sheet.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]}
    with pytest.raises(ValueError, match="tab limit"):
        await service.propose(ctx, "add_sheet_tab", url=URL, tab=tab)

    service, client, ctx, _ = setup()
    client.metadata.return_value.update(ownedByMe=True, trashed=False)
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    client.sheet.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]}
    action = await service.propose(ctx, "add_sheet_tab", url=URL, tab=tab)
    service.sheet_id_generator = lambda low, high: 902
    client.batch.side_effect = [{}, RuntimeError("request failed")]
    result = await service.commit(action, ctx)
    assert "Partial" in result and "Next" in result and "Nothing was retried" in result
    assert client.batch.await_count == 2


async def test_trash_always_requires_confirmation_and_records_trash():
    from chatcore.confirm import ConfirmService
    from chatcore.testing import press
    from chatcore_google.plugin import GooglePlugin

    service, client, ctx, command = setup()
    client.metadata.return_value.update(
        ownedByMe=True,
        trashed=False,
        owners=[{"permissionId": "owner-id"}],
    )
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    await service.repo.configure_fields(ctx.chat.key, confirm_writes=False, expected_revision=0)
    plugin = GooglePlugin(service.settings, service.access, service=service)
    result = await plugin.trash_file(ctx, URL)
    assert result.pending_action is not None
    assert "30 days" in result.content
    client.trash_file.return_value = {"id": "file", "trashed": True}
    confirm = ConfirmService(
        ctx.platform,
        ctx.stores.repo,
        {"google." + name: handler for name, handler in plugin.action_handlers().items()},
    )
    await confirm.request(result.pending_action, ctx.user)
    completed = await confirm.on_press(press(result.pending_action.id, sender=ctx.user), command)
    assert "restorable for 30 days" in completed
    assert await service.repo.output(ctx.chat.key, ctx.thread_id, "file") is None
    assert (await service.repo.output_any(ctx.chat.key, "file"))["trashed_at"] is not None
    client.trash_file.assert_awaited_once_with("file")
    metadata_calls = client.metadata.await_count
    second = await plugin.trash_file(ctx, URL)
    assert second.is_error
    assert client.metadata.await_count == metadata_calls
    assert client.trash_file.await_count == 1


async def test_trash_requires_current_owner_permission_id_and_verifies_response():
    from chatcore_google.plugin import GooglePlugin

    service, client, ctx, _ = setup()
    client.metadata.return_value.update(
        ownedByMe=True,
        trashed=False,
        owners=[{"permissionId": "stale-owner"}],
    )
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    action = await service.propose(ctx, "trash_file", url=URL)
    client.trash_file.return_value = {"id": "file", "trashed": True}
    plugin = GooglePlugin(service.settings, service.access, service=service)
    result = await plugin.commit(action, ctx)
    assert "no longer owned" in result
    client.trash_file.assert_not_awaited()

    service, client, ctx, _ = setup()
    client.metadata.return_value.update(
        ownedByMe=True,
        trashed=False,
        owners=[{"permissionId": "owner-id"}],
    )
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    action = await service.propose(ctx, "trash_file", url=URL)
    client.trash_file.return_value = {"id": "file", "trashed": False}
    plugin = GooglePlugin(service.settings, service.access, service=service)
    result = await plugin.commit(action, ctx)
    assert "Uncertain" in result
    assert await service.repo.output(ctx.chat.key, ctx.thread_id, "file") is not None
    assert client.metadata.await_count == 3


async def test_trash_refuses_trip_attached_output_before_api_call():
    service, client, ctx, _ = setup()
    client.metadata.return_value.update(
        ownedByMe=True,
        trashed=False,
        owners=[{"permissionId": "owner-id"}],
    )
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    service.repo.trip_file = AsyncMock(return_value={"trip_id": "trip"})
    with pytest.raises(ValueError, match="trip"):
        await service.propose(ctx, "trash_file", url=URL)
    client.trash_file.assert_not_awaited()


async def test_rich_doc_propose_freezes_blocks_and_includes_access_preview():
    service, client, ctx, _ = setup()
    blocks = [{"type": "heading", "level": 1, "text": "Plan"}]
    action = await service.propose(
        ctx, "create_rich_doc", title="Itinerary", blocks=blocks, link_access="viewer"
    )
    blocks[0]["text"] = "changed"
    assert action.kind == "create_rich_doc"
    assert action.payload["blocks"][0]["text"] == "Plan"
    assert action.payload["link_access"] == "viewer"
    assert "Create Rich Doc: Itinerary" in action.summary
    assert "anyone with the link can view" in action.summary
    assert "Plan" in action.summary
    client.identity.assert_awaited_once()


async def test_rich_doc_member_gets_forced_viewer_and_writer_gate_is_rechecked():
    service, client, ctx, _ = setup()
    service.settings.create_users = "members"
    ctx.user = user("member")
    ctx.is_admin = False
    action = await service.propose(
        ctx,
        "create_rich_doc",
        title="Member Doc",
        blocks=[{"type": "paragraph", "text": "Hello"}],
        link_access="private",
    )
    assert action.payload["link_access"] == "viewer"
    assert action.payload["link_forced"] is True
    assert "required for files you create" in action.summary

    service.settings.create_users = "writers"
    client.identity.reset_mock()
    with pytest.raises(PermissionError):
        await service.propose(
            ctx,
            "create_rich_doc",
            title="Denied",
            blocks=[{"type": "paragraph", "text": "Hello"}],
        )
    client.identity.assert_not_awaited()


async def test_rich_doc_recompiles_frozen_blocks_before_commit_io():
    service, client, ctx, commit = setup()
    action = await service.propose(
        ctx,
        "create_rich_doc",
        title="At limit",
        blocks=[{"type": "paragraph", "text": "x"}],
    )
    action.payload["blocks"] = [{"type": "paragraph", "text": "x" * 1001}]
    service.settings.max_chars = 1000
    client.request.reset_mock()
    with pytest.raises(ValueError, match="GOOGLE_MAX_CHARS"):
        await service.commit(action, commit)
    client.request.assert_not_awaited()


async def test_rich_doc_commit_runs_batches_verifies_and_renames(request_driven_rich_doc_api):
    service, client, ctx, commit = setup()
    request_driven_rich_doc_api.wire(client)
    action = await service.propose(
        ctx,
        "create_rich_doc",
        title="Plan title",
        blocks=[
            {"type": "heading", "level": 1, "text": "Plan"},
            {"type": "table", "header": ["A"], "rows": [["B"]]},
        ],
    )

    result = await service.commit(action, commit)

    assert "Created and verified" in result
    assert request_driven_rich_doc_api.title == "Plan title"
    assert request_driven_rich_doc_api.batch_a
    assert request_driven_rich_doc_api.batch_b
    assert request_driven_rich_doc_api.batch_c
    client.edit_document.assert_awaited_once()
    client.rename_file.assert_awaited_once_with("rich-created-id", "Plan title")
    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert journal["kind"] == "create_doc" and journal["phase"] == "verified"
    assert await service.repo.output(ctx.chat.key, ctx.thread_id, "rich-created-id")


@pytest.mark.parametrize("failed_batch", ["A", "B", "C"])
async def test_rich_doc_batch_failure_never_renames_or_shares(
    failed_batch, request_driven_rich_doc_api
):
    service, client, ctx, commit = setup()
    request_driven_rich_doc_api.wire(client)
    request_driven_rich_doc_api.fail_batch = failed_batch
    action = await service.propose(
        ctx,
        "create_rich_doc",
        title="Partial",
        link_access="viewer",
        blocks=[
            {"type": "paragraph", "text": "Body"},
            {"type": "table", "header": ["A"], "rows": [["B"]]},
        ],
    )

    result = await service.commit(action, commit)

    assert result.startswith("Uncertain:")
    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert journal["phase"] == "uncertain"
    assert journal["failed_stage"] == "content_verification"
    client.rename_file.assert_not_awaited()
    client.grant_link_viewer.assert_not_awaited()


async def test_rich_doc_verification_failure_does_not_rename_or_share(request_driven_rich_doc_api):
    service, client, ctx, commit = setup()
    request_driven_rich_doc_api.wire(client)
    request_driven_rich_doc_api.corrupt_text = True
    service.settings.new_docs_link_access = "viewer"
    action = await service.propose(
        ctx,
        "create_rich_doc",
        title="Mismatch",
        blocks=[{"type": "paragraph", "text": "Expected"}],
    )
    result = await service.commit(action, commit)
    assert "verification failed (text_mismatch)" in result
    assert (await service.repo.edit_result(ctx.chat.key, action.id))["phase"] == "uncertain"
    client.rename_file.assert_not_awaited()
    client.grant_link_viewer.assert_not_awaited()


async def test_rich_doc_format_warning_still_shares_for_nonwriter(request_driven_rich_doc_api):
    service, client, ctx, commit = setup()
    request_driven_rich_doc_api.wire(client)
    request_driven_rich_doc_api.drop_text_style.add("link")
    service.settings.create_users = "members"
    ctx.user = user("member")
    ctx.is_admin = False
    action = await service.propose(
        ctx,
        "create_rich_doc",
        title="Shared Rich Doc",
        blocks=[{"type": "paragraph", "spans": [{"text": "Map", "link": "https://maps.example"}]}],
    )
    result = await service.commit(action, ctx)
    assert "Created and verified" in result
    assert "Formatting could not be fully verified: links." in result
    assert "anyone with the link can view" in result
    assert any(
        permission.get("type") == "anyone" for permission in request_driven_rich_doc_api.permissions
    )
    assert (await service.repo.edit_result(ctx.chat.key, action.id))["phase"] == "verified"


@pytest.mark.parametrize(
    "metadata_result,expected_phrase,expected_phase",
    [
        ("final", "Created and verified", "verified"),
        ("incomplete", 'title still ends with "(incomplete)"', "partial"),
        ("unavailable", "final title could not be confirmed", "partial"),
    ],
)
async def test_rich_doc_rename_failure_checks_actual_title(
    metadata_result, expected_phrase, expected_phase, request_driven_rich_doc_api
):
    service, client, ctx, commit = setup()
    request_driven_rich_doc_api.wire(client)
    request_driven_rich_doc_api.fail_rename = True
    request_driven_rich_doc_api.name_after_rename_error = {
        "final": "Final title",
        "incomplete": "Final title (incomplete)",
        "unavailable": None,
    }[metadata_result]
    request_driven_rich_doc_api.raise_metadata_after_rename = metadata_result == "unavailable"
    action = await service.propose(
        ctx, "create_rich_doc", title="Final title", blocks=[{"type": "paragraph", "text": "Ready"}]
    )
    result = await service.commit(action, commit)
    assert expected_phrase in result
    assert (await service.repo.edit_result(ctx.chat.key, action.id))["phase"] == expected_phase
    assert len(request_driven_rich_doc_api.metadata_reads) == 2


async def test_rich_doc_daily_cap_is_checked_at_propose_and_commit():
    service, client, ctx, commit = setup()
    service.settings.create_users = "members"
    service.settings.creates_per_user_day = 1
    ctx.user = user("member")
    ctx.is_admin = False
    await service.repo.reserve_create(ctx.chat.key, "used", ctx.user.ref.key, 1)
    with pytest.raises(PermissionError, match="today's limit"):
        await service.propose(
            ctx, "create_rich_doc", title="Capped", blocks=[{"type": "paragraph", "text": "x"}]
        )
    client.identity.assert_not_awaited()

    await service.repo.purge(ctx.chat.key, ctx.user.ref.key)
    action = await service.propose(
        ctx, "create_rich_doc", title="Commit cap", blocks=[{"type": "paragraph", "text": "x"}]
    )
    await service.repo.reserve_create(ctx.chat.key, "used-again", ctx.user.ref.key, 1)
    with pytest.raises(PermissionError, match="today's limit"):
        await service.commit(action, ctx)
    client.request.assert_not_awaited()


async def test_rich_doc_writer_demotion_refuses_frozen_private_payload():
    service, client, ctx, commit = setup()
    action = await service.propose(
        ctx, "create_rich_doc", title="Private", blocks=[{"type": "paragraph", "text": "x"}]
    )
    assert action.payload["link_access"] == "private"
    service.settings.create_users = "members"
    service.access = AccessPolicy([GROUP.key], [], owners=[])
    ctx.is_admin = False
    with pytest.raises(PermissionError, match="access changed"):
        await service.commit(action, ctx)
    client.request.assert_not_awaited()


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


def workbook_tabs():
    return [{"name": "Compare", "columns": [{"header": "Hotel", "type": "text"}], "rows": [["A"]]}]


async def test_create_workbook_proposes_then_creates_and_records_output():
    service, client, ctx, _ = setup()
    action = await service.propose(ctx, "create_workbook", title="Hotels", tabs=workbook_tabs())
    assert action.kind == "create_workbook"
    assert "Create Workbook: Hotels" in action.summary
    assert 'Tab "Compare": 1 rows' in action.summary
    client.request.return_value = {"spreadsheetId": "workbook-id"}
    result = await service.commit(action, ctx)
    assert (
        result
        == "Created https://docs.google.com/spreadsheets/d/workbook-id/edit (sharing unchanged)."
    )
    output = await service.repo.output(ctx.chat.key, ctx.thread_id, "workbook-id")
    assert {
        key: output[key]
        for key in (
            "chat_key",
            "file_id",
            "mime",
            "requester_key",
            "thread_id",
            "owner_id",
            "action_id",
            "trashed_at",
        )
    } == {
        "chat_key": ctx.chat.key,
        "file_id": "workbook-id",
        "mime": SHEET,
        "requester_key": ctx.user.ref.key,
        "thread_id": ctx.thread_id,
        "owner_id": "owner-id",
        "action_id": action.id,
        "trashed_at": None,
    }
    assert client.request.await_args.kwargs["json"]["properties"]["title"] == "Hotels (incomplete)"
    assert client.batch.await_count >= 2


async def test_create_workbook_validation_and_changed_limits_stop_before_write():
    service, client, ctx, _ = setup()
    bad = [{"name": "Bad", "columns": [{"header": "Price", "type": "money"}], "rows": [[True]]}]
    with pytest.raises(ValueError, match='Tab "Bad", row 1, column "Price"'):
        await service.propose(ctx, "create_workbook", title="Bad", tabs=bad)
    client.identity.assert_not_awaited()
    client.request.assert_not_awaited()
    tabs = [
        {
            "name": "Compare",
            "columns": [{"header": "Hotel", "type": "text"}],
            "rows": [["A"], ["B"]],
        }
    ]
    action = await service.propose(ctx, "create_workbook", title="Hotels", tabs=tabs)
    client.identity.reset_mock()
    service.settings.max_rows = 1
    service.settings.max_cell_chars = 100
    with pytest.raises(ValueError, match="rows"):
        await service.commit(action, ctx)
    client.identity.assert_not_awaited()
    client.request.assert_not_awaited()


@pytest.mark.parametrize("kind", ["create_sheet", "create_workbook"])
async def test_invalid_link_access_fails_before_google_identity(kind):
    service, client, ctx, _ = setup()
    if kind == "create_sheet":
        arguments = {"rows": [["x"]]}
    else:
        arguments = {"tabs": workbook_tabs()}
    with pytest.raises(ValueError):
        await service.propose(ctx, kind, title="Invalid access", link_access="anyone", **arguments)
    client.identity.assert_not_awaited()
    client.request.assert_not_awaited()


async def test_invalid_sheet_rows_fail_before_google_identity():
    service, client, ctx, _ = setup()
    with pytest.raises(ValueError):
        await service.propose(ctx, "create_sheet", title="Invalid rows", rows=[])
    client.identity.assert_not_awaited()
    client.request.assert_not_awaited()


async def test_partial_workbook_is_journaled_and_sharing_is_still_attempted(monkeypatch):
    service, client, ctx, _ = setup()
    ctx.user = user("member")
    ctx.is_admin = False
    service.settings.create_users = "members"
    action = await service.propose(
        ctx, "create_workbook", title="Hotels", tabs=workbook_tabs(), link_access="viewer"
    )
    client.request.return_value = {"spreadsheetId": "partial-book"}
    client.batch.side_effect = [None, RuntimeError("private batch detail")]
    share = __import__("chatcore_google.service", fromlist=["publish_sheet_link_access"])
    publish = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(share, "publish_sheet_link_access", publish)
    result = await service.commit(action, ctx)
    assert "https://docs.google.com/spreadsheets/d/partial-book/edit" in result
    assert "(incomplete)" in result and "step 2 of 2 failed" in result
    assert await service.repo.output(ctx.chat.key, ctx.thread_id, "partial-book")
    publish.assert_awaited_once()


async def test_read_uses_retained_same_topic_output_binding_without_human_repost():
    service, client, ctx, _ = setup()
    doc_url = "https://docs.google.com/document/d/output-id/edit"
    ctx.turn.human_urls = frozenset()
    client.metadata.return_value = {
        "id": "output-id",
        "name": "Output",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canModifyContent": True},
    }
    client.document.return_value = created_doc("Existing\n")
    await service.repo.reserve_edit(
        ctx.chat.key,
        "output-action",
        ctx.user.ref.key,
        0,
        10,
        0,
        kind="edit_doc",
        source_id="source-id",
        output_binding={
            "owner_id": "owner-id",
            "owner_email": "owner@example.com",
            "thread_id": None,
            "mode": "copy",
        },
    )
    await service.repo.update_edit_result(
        ctx.chat.key, "output-action", phase="destination_created", destination_id="output-id"
    )

    result = await service.read_doc(ctx, doc_url)
    generic_result = await service.read_file(ctx, doc_url)

    assert "Existing" in result
    assert "Existing" in generic_result
    assert client.document.await_count == 2


@pytest.mark.parametrize("reader", ["read_sheet", "read_file"])
@pytest.mark.parametrize("restriction", [False, True], ids=["chat-off", "restricted"])
async def test_journaled_sheet_reads_still_enforce_chat_and_user_authorization(reader, restriction):
    service, client, ctx, _ = setup()
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, ctx.user.ref.key, ctx.thread_id, "owner-id", "create"
    )
    client.metadata.return_value.update(ownedByMe=True)
    if restriction:
        service.access.restriction_for = lambda *args, **kwargs: object()
    else:
        await service.repo.configure(ctx.chat.key, False, 0)
    operation = getattr(service, reader)
    with pytest.raises(PermissionError):
        await operation(ctx, URL)


async def test_own_output_append_commit_rechecks_create_permission_tier():
    service, client, ctx, _ = setup()
    member = user("member")
    ctx.user = member
    ctx.is_admin = False
    service.settings.create_users = "members"
    client.metadata.return_value.update(ownedByMe=True)
    await service.repo.record_output(
        ctx.chat.key, "file", SHEET, member.ref.key, ctx.thread_id, "owner-id", "created"
    )
    action = await service.propose(ctx, "append", url=URL, rows=[["append"]])
    service.settings.create_users = "writers"
    with pytest.raises(PermissionError, match="creation"):
        await service.commit(action, ctx)
    client.batch.assert_not_awaited()


async def test_members_can_append_only_to_their_own_output_at_propose_and_commit():
    service, client, ctx, _ = setup()
    alice = user("alice")
    ctx.user = alice
    ctx.is_admin = False
    service.settings.create_users = "members"
    client.metadata.return_value = {
        "id": "alice-output",
        "name": "Alice output",
        "mimeType": SHEET,
        "ownedByMe": True,
    }
    client.sheet.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": "Output"}}]}
    await service.repo.record_output(
        ctx.chat.key,
        "alice-output",
        SHEET,
        alice.ref.key,
        ctx.thread_id,
        "owner-id",
        "alice-action",
    )
    alice_url = "https://docs.google.com/spreadsheets/d/alice-output/edit"
    action = await service.propose(ctx, "append", url=alice_url, rows=[["own"]])
    result = await service.commit(action, ctx)
    assert "appended" in result.lower()
    assert client.batch.await_count == 1

    bob = user("bob")
    ctx.user = bob
    with pytest.raises(PermissionError, match="writer"):
        await service.propose(ctx, "append", url=alice_url, rows=[["other"]])

    ctx.user = alice
    pending = await service.propose(ctx, "append", url=alice_url, rows=[["own again"]])
    service.repo._outputs[(ctx.chat.key, "alice-output")]["requester_key"] = bob.ref.key
    with pytest.raises(PermissionError, match="binding"):
        await service.commit(pending, ctx)
    assert client.batch.await_count == 1


@pytest.mark.parametrize("during_metadata", [False, True])
async def test_own_output_append_refuses_if_journal_binding_disappears(during_metadata):
    service, client, ctx, _ = setup()
    ctx.user = user("member")
    ctx.is_admin = False
    service.settings.create_users = "members"
    client.metadata.return_value = {
        "id": "member-output",
        "mimeType": SHEET,
        "ownedByMe": True,
    }
    client.sheet.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]}
    await service.repo.record_output(
        ctx.chat.key,
        "member-output",
        SHEET,
        ctx.user.ref.key,
        ctx.thread_id,
        "owner-id",
        "source-action",
    )
    url = "https://docs.google.com/spreadsheets/d/member-output/edit"
    action = await service.propose(ctx, "append", url=url, rows=[["x"]])
    assert action.payload["via_output"] is True
    if during_metadata:
        metadata = dict(client.metadata.return_value)

        async def purge_while_fetching(*args, **kwargs):
            await service.repo.purge(ctx.chat.key)
            return metadata

        client.metadata.side_effect = purge_while_fetching
    else:
        await service.repo.purge(ctx.chat.key)
    with pytest.raises(PermissionError):
        await service.commit(action, ctx)
    client.batch.assert_not_awaited()


async def test_pending_private_sheet_refuses_after_writer_demotion():
    service, client, ctx, _ = setup()
    ctx.user = user("writer")
    service.settings.write_user_ids = ctx.user.ref.key
    action = await service.propose(ctx, "create_sheet", title="Private", rows=[["x"]])
    service.settings.write_user_ids = ""
    service.settings.create_users = "members"
    with pytest.raises(PermissionError, match="Your access changed"):
        await service.commit(action, ctx)
    client.request.assert_not_awaited()


@pytest.mark.parametrize("kind", ["create_sheet", "create_doc"])
async def test_legacy_private_preview_refuses_after_writer_demotion(kind):
    """Previews frozen before v0.21.0 have no Sheet link_access and only legacy Doc flags."""
    service, client, ctx, _ = setup()
    ctx.user = user("writer")
    service.settings.write_user_ids = ctx.user.ref.key
    if kind == "create_sheet":
        action = await service.propose(ctx, kind, title="Old", rows=[["x"]])
        payload = {k: v for k, v in action.payload.items() if k != "link_access"}
    else:
        action = await service.propose(ctx, kind, title="Old", text="x", link_access="private")
        payload = {k: v for k, v in action.payload.items() if k != "link_access"}
        payload["link_viewer"] = False
    action = replace(action, payload=payload)
    service.settings.write_user_ids = ""
    service.settings.create_users = "members"
    with pytest.raises(PermissionError, match="Your access changed"):
        await service.commit(action, ctx)
    client.request.assert_not_awaited()


async def test_stale_member_create_does_not_consume_daily_reservation():
    service, client, ctx, _ = setup()
    ctx.user = user("member")
    ctx.is_admin = False
    service.settings.create_users = "members"
    service.settings.creates_per_user_day = 1
    stale = await service.propose(ctx, "create_sheet", title="Stale", rows=[["x"]])
    await service.repo.configure_fields(
        ctx.chat.key, confirm_writes=False, expected_revision=stale.payload["revision"]
    )
    with pytest.raises(ValueError, match="settings changed"):
        await service.commit(stale, ctx)
    assert await service.repo.creates_today(ctx.chat.key, ctx.user.ref.key) == 0
    await service.repo.configure_fields(
        ctx.chat.key, confirm_writes=True, expected_revision=stale.payload["revision"] + 1
    )
    fresh = await service.propose(ctx, "create_sheet", title="Fresh", rows=[["x"]])
    assert fresh


@pytest.mark.parametrize(
    ("chat", "thread", "kind", "binding"),
    [
        ("fake:other", None, "edit_doc", True),
        (GROUP.key, "other-topic", "edit_doc", True),
        (GROUP.key, None, "browser_edit_doc", False),
    ],
)
async def test_output_read_authority_does_not_cross_chat_topic_or_browser(
    chat, thread, kind, binding
):
    service, client, ctx, _ = setup()
    doc_url = "https://docs.google.com/document/d/output-id/edit"
    ctx.turn.human_urls = frozenset()
    ctx.turn.seen_urls.add(doc_url)
    ctx.thread_id = "current-topic"
    await service.repo.reserve_edit(
        chat,
        "output-action",
        ctx.user.ref.key,
        0,
        10,
        0,
        kind=kind,
        source_id="source-id",
        output_binding=(
            {
                "owner_id": "owner-id",
                "owner_email": "owner@example.com",
                "thread_id": thread,
                "mode": "copy",
            }
            if binding
            else None
        ),
    )
    await service.repo.update_edit_result(
        chat, "output-action", phase="destination_created", destination_id="output-id"
    )

    with pytest.raises(ValueError, match="human"):
        await service.read_doc(ctx, doc_url)
    client.metadata.assert_not_awaited()


async def test_output_read_rejects_a_changed_google_owner_before_content_read():
    service, client, ctx, _ = setup()
    doc_url = "https://docs.google.com/document/d/output-id/edit"
    ctx.turn.human_urls = frozenset()
    client.metadata.return_value = {
        "id": "output-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "new-owner-id"}],
        "capabilities": {"canEdit": True, "canModifyContent": True},
    }
    await service.repo.reserve_edit(
        ctx.chat.key,
        "output-action",
        ctx.user.ref.key,
        0,
        10,
        0,
        kind="edit_doc",
        source_id="source-id",
        output_binding={
            "owner_id": "old-owner-id",
            "owner_email": "owner@example.com",
            "thread_id": None,
            "mode": "copy",
        },
    )
    await service.repo.update_edit_result(
        ctx.chat.key, "output-action", phase="destination_created", destination_id="output-id"
    )

    with pytest.raises(ValueError, match="retained output belongs to a different Google owner"):
        await service.read_doc(ctx, doc_url)
    client.document.assert_not_awaited()


async def test_output_binding_revocation_during_doc_get_stops_the_read():
    service, client, ctx, _ = setup()
    doc_url = "https://docs.google.com/document/d/output-id/edit"
    ctx.turn.human_urls = frozenset()
    client.metadata.return_value = {
        "id": "output-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canModifyContent": True},
    }
    await service.repo.reserve_edit(
        ctx.chat.key,
        "output-action",
        ctx.user.ref.key,
        0,
        10,
        0,
        kind="edit_doc",
        source_id="source-id",
        output_binding={
            "owner_id": "owner-id",
            "owner_email": "owner@example.com",
            "thread_id": None,
            "mode": "copy",
        },
    )
    await service.repo.update_edit_result(
        ctx.chat.key, "output-action", phase="destination_created", destination_id="output-id"
    )

    async def purge_during_get(_file_id):
        await service.repo.purge(ctx.chat.key)
        return created_doc("Existing\n")

    client.document.side_effect = purge_during_get
    with pytest.raises(ValueError, match="binding was removed or changed"):
        await service.read_doc(ctx, doc_url)
    client.document.assert_awaited_once_with("output-id")


async def test_output_binding_revocation_during_metadata_recheck_stops_the_read():
    service, client, ctx, _ = setup()
    doc_url = "https://docs.google.com/document/d/output-id/edit"
    ctx.turn.human_urls = frozenset()
    output_metadata = {
        "id": "output-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canModifyContent": True},
    }
    await service.repo.reserve_edit(
        ctx.chat.key,
        "output-action",
        ctx.user.ref.key,
        0,
        10,
        0,
        kind="edit_doc",
        source_id="source-id",
        output_binding={
            "owner_id": "owner-id",
            "owner_email": "owner@example.com",
            "thread_id": None,
            "mode": "copy",
        },
    )
    await service.repo.update_edit_result(
        ctx.chat.key, "output-action", phase="destination_created", destination_id="output-id"
    )
    metadata_calls = 0

    async def metadata_with_purge(_file_id):
        nonlocal metadata_calls
        metadata_calls += 1
        if metadata_calls == 2:
            await service.repo.purge(ctx.chat.key)
        return output_metadata

    client.metadata.side_effect = metadata_with_purge
    client.document.return_value = created_doc("Existing\n")

    with pytest.raises(ValueError, match="binding was removed or changed during the read"):
        await service.read_doc(ctx, doc_url)
    assert metadata_calls == 2
    client.document.assert_awaited_once_with("output-id")


async def test_append_does_nothing_before_confirmation_and_rechecks_optout():
    service, client, ctx, commit = setup()
    action = await service.propose(ctx, "append", url=URL, rows=[["=BAD()", "Lunch"]])
    client.batch.assert_not_awaited()
    await service.repo.configure(GROUP.key, False, 0)
    with pytest.raises((ValueError, PermissionError), match="off|changed"):
        await service.commit(action, commit)
    client.batch.assert_not_awaited()


async def test_create_doc_freezes_application_default_and_explicit_private_choice():
    service, _, ctx, _ = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    public = await service.propose(ctx, "create_doc", title="Public", text="Text")
    private = await service.propose(
        ctx, "create_doc", title="Private", text="Text", link_viewer=False
    )

    assert public.payload["link_viewer"] is True
    assert private.payload["link_viewer"] is False
    assert "anyone with the link can view" in public.summary.casefold()
    assert "private" in private.summary.casefold()


@pytest.mark.parametrize(
    ("configured", "explicit", "expected"),
    [
        ("private", None, "private"),
        ("viewer", None, "viewer"),
        ("editor", None, "editor"),
        ("editor", "private", "private"),
        ("private", "viewer", "viewer"),
        ("viewer", "editor", "editor"),
    ],
)
async def test_create_doc_freezes_canonical_link_access(configured, explicit, expected):
    service, _, ctx, _ = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_access=configured,
    )

    action = await service.propose(
        ctx, "create_doc", title="Output", text="Text", link_access=explicit
    )

    assert action.payload["link_access"] == expected


@pytest.mark.parametrize("invalid", ["anyone", "writer", "", 1, True])
async def test_create_doc_rejects_invalid_or_ambiguous_access(invalid):
    service, _, ctx, _ = setup()
    with pytest.raises(ValueError, match="link_access|private, viewer or editor"):
        await service.propose(
            ctx,
            "create_doc",
            title="Output",
            text="Text",
            link_access=invalid,
            link_viewer=False,
        )


@pytest.mark.parametrize("invalid", ["anyone", "writer", "", 1, True])
async def test_create_doc_rejects_invalid_access_level(invalid):
    service, _, ctx, _ = setup()
    with pytest.raises(ValueError, match="private, viewer or editor"):
        await service.propose(ctx, "create_doc", title="Output", text="Text", link_access=invalid)


@pytest.mark.parametrize("invalid", [0, 1, "true"])
async def test_create_doc_rejects_non_boolean_legacy_choice(invalid):
    service, _, ctx, _ = setup()
    with pytest.raises(ValueError, match="link_viewer must be a boolean"):
        await service.propose(ctx, "create_doc", title="Output", text="Text", link_viewer=invalid)


async def test_link_access_payload_compatibility_never_promotes_old_previews():
    from chatcore_google.link_sharing import payload_link_access

    assert payload_link_access({"link_viewer": True}) == "viewer"
    assert payload_link_access({"link_viewer": False}) == "private"
    assert payload_link_access({}) == "private"
    assert payload_link_access({"link_access": "editor"}) == "editor"
    with pytest.raises(ValueError, match="choices conflict"):
        payload_link_access({"link_access": "editor", "link_viewer": False})


async def test_legacy_viewer_helper_does_not_promote_missing_or_private_payload():
    service, client, ctx, _ = setup()
    from chatcore_google.link_sharing import publish_doc_link_viewer

    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")
    await service.repo.reserve_edit(
        ctx.chat.key,
        action.id,
        ctx.user.ref.key,
        0,
        10,
        0,
        kind="create_doc",
        source_id=None,
        output_binding={
            "owner_id": action.payload["owner"],
            "owner_email": action.payload["owner_email"],
            "thread_id": action.thread_id,
            "mode": "create",
        },
    )
    await service.repo.update_edit_result(
        ctx.chat.key, action.id, phase="destination_created", destination_id="new-doc"
    )
    await service.repo.update_edit_result(ctx.chat.key, action.id, phase="applying")
    variants = []
    missing = dict(action.payload)
    missing.pop("link_access")
    missing.pop("link_viewer")
    variants.append(missing)
    private = dict(missing, link_viewer=False)
    variants.append(private)
    for payload in variants:
        with pytest.raises(ValueError, match="no link-access grant"):
            await publish_doc_link_viewer(
                service, replace(action, payload=payload), ctx, payload, "new-doc"
            )
    client.grant_link_viewer.assert_not_awaited()


async def test_create_doc_editor_grant_is_frozen_and_verified():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_access="viewer",
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "name": "Output",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    owner_permission = {
        "id": "owner-permission",
        "type": "user",
        "role": "owner",
        "emailAddress": "owner@example.com",
    }
    client.list_permissions.side_effect = [
        {"permissions": [owner_permission]},
        {
            "permissions": [
                owner_permission,
                {
                    "id": "link-permission",
                    "type": "anyone",
                    "role": "writer",
                    "allowFileDiscovery": False,
                },
            ]
        },
    ]
    action = await service.propose(
        ctx, "create_doc", title="Output", text="Hello", link_access="editor"
    )
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_access="private",
    )

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert action.payload["link_access"] == "editor"
    assert "anyone with the link can edit" in result.casefold()
    assert journal["phase"] == "verified" and journal["destination_id"] == "created-id"
    assert journal["output_owner_id"] == "owner-id"
    client.grant_link_editor.assert_awaited_once_with("created-id")


async def test_create_doc_editor_refuses_existing_viewer_acl_without_rewriting():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com", oauth_credentials_file="unused"
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    client.list_permissions.return_value = {
        "permissions": [{"type": "anyone", "role": "reader", "allowFileDiscovery": False}]
    }
    action = await service.propose(
        ctx, "create_doc", title="Output", text="Hello", link_access="editor"
    )

    result = await service.commit(action, commit)

    assert "Partial" in result
    client.grant_link_editor.assert_not_awaited()


async def test_link_viewer_create_journals_known_id_verifies_text_then_shares():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "name": "Output",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    owner_permission = {
        "id": "owner-permission",
        "type": "user",
        "role": "owner",
        "emailAddress": "owner@example.com",
    }
    client.list_permissions.side_effect = [
        {"permissions": [owner_permission]},
        {
            "permissions": [
                owner_permission,
                {
                    "id": "anyoneWithLink",
                    "type": "anyone",
                    "role": "reader",
                    "allowFileDiscovery": False,
                },
            ]
        },
    ]
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert "anyone with the link can view" in result.casefold()
    assert journal["phase"] == "verified" and journal["destination_id"] == "created-id"
    client.grant_link_viewer.assert_awaited_once_with("created-id")


async def test_link_viewer_create_never_shares_unverified_content():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
    }
    client.document_for_edit.return_value = created_doc("Wrong\n")
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert "text_mismatch" in result and "no link-access change was attempted" in result
    assert journal["phase"] == "uncertain" and journal["destination_id"] == "created-id"
    assert journal["error_category"] == "text_mismatch"
    client.grant_link_viewer.assert_not_awaited()


async def test_link_viewer_create_rejects_matching_text_with_incomplete_snapshot():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    incomplete = created_doc("Hello\n")
    paragraph = incomplete["tabs"][0]["documentTab"]["body"]["content"][0]
    paragraph["endIndex"] += 1
    paragraph["paragraph"]["elements"].append(
        {"startIndex": 7, "endIndex": 8, "inlineObjectElement": {"inlineObjectId": "image"}}
    )
    client.document_for_edit.return_value = incomplete
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert "snapshot_error" in result
    assert journal["phase"] == "uncertain" and journal["error_category"] == "snapshot_error"
    client.grant_link_viewer.assert_not_awaited()


async def test_link_viewer_preflight_refuses_broader_acl_with_known_url():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    client.list_permissions.return_value = {
        "permissions": [{"type": "anyone", "role": "writer", "allowFileDiscovery": True}]
    }
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert "Partial" in result and "created-id" in result
    assert journal["phase"] == "partial" and journal["destination_id"] == "created-id"
    client.grant_link_viewer.assert_not_awaited()


async def test_link_viewer_journal_claim_failure_prevents_grant(monkeypatch):
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    client.list_permissions.return_value = {"permissions": []}
    update = service.repo.update_edit_result

    async def fail_sharing_stage(*args, **kwargs):
        if kwargs.get("failed_stage") == "sharing":
            raise RuntimeError("journal unavailable")
        return await update(*args, **kwargs)

    monkeypatch.setattr(service.repo, "update_edit_result", fail_sharing_stage)
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    assert "Partial" in result and "created-id" in result
    client.grant_link_viewer.assert_not_awaited()


@pytest.mark.parametrize(
    ("grant_error", "expected_result", "expected_phase"),
    [
        (
            GoogleError("Google request failed; a write may have completed."),
            "Uncertain",
            "uncertain",
        ),
        (GoogleError("Google returned an invalid response."), "Uncertain", "uncertain"),
        (GoogleError("Google response exceeds the size limit."), "Uncertain", "uncertain"),
        (GoogleError("Cannot open that file. Check sharing."), "Partial", "partial"),
    ],
)
async def test_link_viewer_post_failure_keeps_destination_and_blocks_replay(
    grant_error, expected_result, expected_phase
):
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    client.list_permissions.return_value = {"permissions": []}
    client.grant_link_viewer.side_effect = grant_error
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert expected_result in result and "created-id" in result
    assert journal["phase"] == expected_phase and journal["destination_id"] == "created-id"
    with pytest.raises(ValueError, match="handled|uncertain"):
        await service.commit(action, commit)


async def test_link_viewer_readback_mismatch_is_uncertain_with_known_url():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    client.list_permissions.side_effect = [
        {"permissions": []},
        {"permissions": [{"type": "anyone", "role": "reader", "allowFileDiscovery": True}]},
    ]
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert "Uncertain" in result and "created-id" in result
    assert journal["phase"] == "uncertain" and journal["destination_id"] == "created-id"
    client.grant_link_viewer.assert_awaited_once_with("created-id")
    with pytest.raises(ValueError, match="handled|uncertain"):
        await service.commit(action, commit)


async def test_create_doc_stops_when_returned_id_cannot_be_journaled(monkeypatch):
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.return_value = {"documentId": "created-id"}
    update = service.repo.update_edit_result

    async def fail_destination_journal(*args, **kwargs):
        if kwargs.get("phase") == "destination_created":
            raise RuntimeError("journal unavailable")
        return await update(*args, **kwargs)

    monkeypatch.setattr(service.repo, "update_edit_result", fail_destination_journal)
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    assert "Partial" in result and "created-id" in result
    assert client.request.await_count == 1
    client.metadata.assert_not_awaited()
    client.grant_link_viewer.assert_not_awaited()


async def test_create_doc_final_journal_failure_does_not_claim_success(monkeypatch):
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    client.list_permissions.side_effect = [
        {"permissions": []},
        {
            "permissions": [
                {
                    "id": "anyoneWithLink",
                    "type": "anyone",
                    "role": "reader",
                    "allowFileDiscovery": False,
                }
            ]
        },
    ]
    update = service.repo.update_edit_result

    async def fail_final_journal(*args, **kwargs):
        if kwargs.get("phase") == "verified":
            raise RuntimeError("journal unavailable")
        return await update(*args, **kwargs)

    monkeypatch.setattr(service.repo, "update_edit_result", fail_final_journal)
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    assert "Uncertain" in result and "created-id" in result
    assert "Created and verified" not in result
    client.grant_link_viewer.assert_awaited_once_with("created-id")


async def test_legacy_create_doc_payload_without_choice_stays_private_after_default_flip():
    service, client, ctx, commit = setup()
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")
    legacy_payload = dict(action.payload)
    legacy_payload.pop("link_viewer")
    action = replace(action, payload=legacy_payload)
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
    }
    client.document_for_edit.return_value = created_doc("Hello\n")

    result = await service.commit(action, commit)

    assert "sharing unchanged" in result
    client.grant_link_viewer.assert_not_awaited()


@pytest.mark.parametrize(
    "write_error",
    [
        GoogleError("Google returned an invalid response."),
        GoogleError("Google response exceeds the size limit."),
    ],
)
async def test_create_doc_malformed_success_response_is_uncertain(write_error):
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = write_error
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert "Uncertain" in result
    assert journal["phase"] == "uncertain" and journal["destination_id"] is None
    client.grant_link_viewer.assert_not_awaited()


async def test_confirmed_append_uses_server_side_literal_append_and_cannot_replay():
    service, client, ctx, commit = setup()
    action = await service.propose(ctx, "append", url=URL, rows=[["=BAD()", "Lunch"]])
    result = await service.commit(action, commit)
    assert "appended" in result.lower()
    requests = client.batch.await_args.args[1]
    assert requests[0]["appendCells"]["sheetId"] == 7
    assert (
        requests[0]["appendCells"]["rows"][0]["values"][0]["userEnteredValue"]["stringValue"]
        == "=BAD()"
    )
    assert "formulaValue" not in str(requests)
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
    assert "No tab named that" in await service.read_sheet(ctx, URL, tab="Other")
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
    assert "formulaValue" not in str(client.batch.await_args.args[1])
    client.grant_link_viewer.assert_not_awaited()


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


async def test_trashed_doc_output_loses_legacy_edit_result_authority():
    from chatcore_google.service import DOC

    service, client, ctx, _ = setup()
    url = "https://docs.google.com/document/d/output-doc/edit"
    binding = {
        "owner_id": "owner-id",
        "owner_email": "owner@example.com",
        "thread_id": ctx.thread_id,
        "mode": "create",
    }
    await service.repo.reserve_edit(
        ctx.chat.key,
        "create-doc-action",
        ctx.user.ref.key,
        0,
        10,
        0,
        kind="create_doc",
        source_id=None,
        output_binding=binding,
    )
    await service.repo.update_edit_result(
        ctx.chat.key,
        "create-doc-action",
        phase="destination_created",
        destination_id="output-doc",
    )
    await service.repo.record_output(
        ctx.chat.key,
        "output-doc",
        DOC,
        ctx.user.ref.key,
        ctx.thread_id,
        "owner-id",
        "create-doc-action",
    )
    assert await service.repo.mark_output_trashed(ctx.chat.key, "output-doc", "create-doc-action")
    with pytest.raises(ValueError, match="link"):
        await service.doc_source(ctx, url)
    client.identity.assert_not_awaited()

    # Older Doc outputs without a google_outputs row keep their prior authority.
    await service.repo.reserve_edit(
        ctx.chat.key,
        "legacy-create-action",
        ctx.user.ref.key,
        0,
        10,
        0,
        kind="create_doc",
        source_id=None,
        output_binding=binding,
    )
    await service.repo.update_edit_result(
        ctx.chat.key,
        "legacy-create-action",
        phase="destination_created",
        destination_id="legacy-doc",
    )
    client.metadata.return_value.update(
        id="legacy-doc",
        name="Legacy",
        mimeType=DOC,
        isAppAuthorized=True,
        ownedByMe=True,
        owners=[{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        capabilities={"canEdit": True, "canModifyContent": True},
        trashed=False,
    )
    _, _, _, proof = await service.doc_source(
        ctx, "https://docs.google.com/document/d/legacy-doc/edit"
    )
    assert proof["kind"] == "output"


async def test_structured_doc_append_rejects_title_before_google_identity(google_service):
    service, client, ctx, _ = google_service
    with pytest.raises(ValueError, match="Title blocks cannot be appended"):
        await service.propose(
            ctx,
            "append_doc_blocks",
            url="https://docs.google.com/document/d/doc/edit",
            blocks=[{"type": "title", "text": "At the top only"}],
        )
    client.identity.assert_not_awaited()
    client.metadata.assert_not_awaited()


async def test_structured_doc_append_revalidates_limits_before_commit_identity(google_service):
    service, client, ctx, _ = google_service
    from chatcore.pending import PendingAction

    action = PendingAction.create(
        chat=ctx.chat,
        requester=ctx.user.ref,
        plugin="google",
        kind="append_doc_blocks",
        payload={
            "blocks": [{"type": "paragraph", "text": "x" * 1200}],
            "file_id": "doc",
            "url": "https://docs.google.com/document/d/doc/edit",
        },
        summary="append",
        thread_id=ctx.thread_id,
    )
    service.settings.max_chars = 1000
    with pytest.raises(ValueError, match="GOOGLE_MAX_CHARS"):
        await service.commit(action, ctx)
    client.identity.assert_not_awaited()
    client.document_for_edit.assert_not_awaited()
    client.edit_document.assert_not_awaited()


async def test_structured_doc_append_other_member_output_requires_writer(google_service):
    service, client, ctx, _ = google_service
    from chatcore_google.service import DOC

    service.settings.create_users = "members"
    ctx.user = user("member")
    ctx.is_admin = False
    url = "https://docs.google.com/document/d/doc/edit"
    ctx.turn.human_urls = frozenset({url})
    service.doc_source = AsyncMock(
        return_value=(
            type("Ref", (), {"file_id": "doc"})(),
            {
                "id": "doc",
                "ownedByMe": True,
                "mimeType": DOC,
                "isAppAuthorized": True,
                "trashed": False,
                "name": "Doc",
                "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
                "capabilities": {"canEdit": True, "canModifyContent": True},
            },
            "owner-id",
            None,
        )
    )
    await service.repo.record_output(
        ctx.chat.key, "doc", DOC, "another-member", ctx.thread_id, "owner-id", "old-action"
    )
    with pytest.raises(PermissionError, match="writer"):
        await service.propose(
            ctx, "append_doc_blocks", url=url, blocks=[{"type": "paragraph", "text": "No write"}]
        )
    client.document_for_edit.assert_not_awaited()


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


async def test_member_create_setting_allows_create_but_not_human_file_append():
    service, _, ctx, _ = setup()
    ctx.user = user("member")
    ctx.is_admin = False
    with pytest.raises(PermissionError, match="creation"):
        await service.propose(ctx, "create_sheet", title="New", rows=[["plain"]])
    with pytest.raises(PermissionError, match="creation"):
        await service.propose(ctx, "create_workbook", title="New", tabs=workbook_tabs())
    service.settings.create_users = "members"
    action = await service.propose(ctx, "create_sheet", title="New", rows=[["plain"]])
    assert action.kind == "create_sheet"
    with pytest.raises(PermissionError, match="writer"):
        await service.propose(ctx, "append", url=URL, rows=[["human"]])


async def test_member_workbook_forces_viewer_and_rechecks_daily_cap():
    service, _, ctx, _ = setup()
    ctx.user = user("member")
    ctx.is_admin = False
    service.settings.create_users = "members"
    service.settings.creates_per_user_day = 2
    action = await service.propose(
        ctx,
        "create_workbook",
        title="Member book",
        tabs=workbook_tabs(),
        link_access="private",
    )
    assert action.payload["link_access"] == "viewer"
    assert action.payload["link_forced"] is True
    assert "required for files you create" in action.summary
    for index in range(2):
        await service.repo.reserve_create(ctx.chat.key, f"crossed-{index}", ctx.user.ref.key, 2)
    with pytest.raises(PermissionError, match="today's limit"):
        await service.commit(action, ctx)
    with pytest.raises(PermissionError, match="today's limit"):
        await service.propose(ctx, "create_workbook", title="Over cap", tabs=workbook_tabs())


async def test_workbook_private_preview_is_refused_after_writer_demotion():
    service, client, ctx, _ = setup()
    action = await service.propose(ctx, "create_workbook", title="Private", tabs=workbook_tabs())
    service.access = AccessPolicy([GROUP.key], [], owners=[])
    service.settings.create_users = "members"
    with pytest.raises(PermissionError, match="access changed"):
        await service.commit(action, ctx)
    client.request.assert_not_awaited()


async def test_workbook_settings_revision_change_invalidates_preview():
    service, client, ctx, _ = setup()
    action = await service.propose(ctx, "create_workbook", title="Hotels", tabs=workbook_tabs())
    await service.repo.configure_fields(
        ctx.chat.key, confirm_writes=False, expected_revision=action.payload["revision"]
    )
    with pytest.raises(ValueError, match="settings changed"):
        await service.commit(action, ctx)
    client.request.assert_not_awaited()


async def test_member_private_sheet_is_forced_to_viewer_and_daily_cap_is_rechecked():
    service, _, ctx, _ = setup()
    ctx.user = user("member")
    ctx.is_admin = False
    service.settings.create_users = "members"
    action = await service.propose(
        ctx, "create_sheet", title="Member sheet", rows=[["x"]], link_access="private"
    )
    assert action.payload["link_access"] == "viewer"
    assert "required for files you create" in action.summary
    for index in range(19):
        await service.repo.reserve_create(ctx.chat.key, f"old-action-{index}", ctx.user.ref.key, 20)
    pending = await service.propose(ctx, "create_sheet", title="One left", rows=[["x"]])
    await service.repo.reserve_create(ctx.chat.key, "crossed", ctx.user.ref.key, 20)
    with pytest.raises(PermissionError, match="today's limit"):
        await service.commit(pending, ctx)
    with pytest.raises(PermissionError, match="today's limit"):
        await service.propose(ctx, "create_sheet", title="Over cap", rows=[["x"]])


async def test_writers_are_not_limited_by_member_create_cap():
    service, _, ctx, _ = setup()
    for index in range(20):
        await service.repo.reserve_create(ctx.chat.key, f"old-action-{index}", ctx.user.ref.key, 20)
    action = await service.propose(ctx, "create_sheet", title="Writer", rows=[["x"]])
    assert action.kind == "create_sheet"


async def test_nonwriter_doc_private_choice_is_raised_to_viewer_in_preview():
    service, _, ctx, _ = setup()
    ctx.user = user("member")
    ctx.is_admin = False
    service.settings.create_users = "members"
    action = await service.propose(
        ctx, "create_doc", title="Member doc", text="Hello", link_access="private"
    )
    assert action.payload["link_access"] == "viewer"
    assert "anyone with the link can view" in action.summary
    assert "required for files you create" in action.summary


@pytest.mark.parametrize("kind", ["create_sheet", "create_workbook"])
async def test_sheet_sharing_requires_output_binding_and_verifies_non_discoverable_acl(kind):
    from chatcore_google.link_sharing import publish_sheet_link_access

    service, client, ctx, _ = setup()
    if kind == "create_sheet":
        action = await service.propose(
            ctx, kind, title="Shared", rows=[["x"]], link_access="viewer"
        )
    else:
        action = await service.propose(
            ctx, kind, title="Shared", tabs=workbook_tabs(), link_access="viewer"
        )
    await service.repo.record_output(
        ctx.chat.key, "shared-id", SHEET, ctx.user.ref.key, action.thread_id, "owner-id", action.id
    )
    client.metadata.return_value = {
        "id": "shared-id",
        "mimeType": SHEET,
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.list_permissions.side_effect = [
        {"permissions": []},
        {
            "permissions": [
                {
                    "id": "anyoneWithLink",
                    "type": "anyone",
                    "role": "reader",
                    "allowFileDiscovery": False,
                }
            ]
        },
    ]
    shared, message = await publish_sheet_link_access(
        service, action, ctx, action.payload, "shared-id"
    )
    assert shared and not message
    client.grant_link_viewer.assert_awaited_once_with("shared-id")


async def test_sheet_sharing_rejects_unexpected_acl_delta():
    from chatcore_google.link_sharing import publish_sheet_link_access

    service, client, ctx, _ = setup()
    action = await service.propose(
        ctx, "create_sheet", title="Shared", rows=[["x"]], link_access="viewer"
    )
    await service.repo.record_output(
        ctx.chat.key, "shared-id", SHEET, ctx.user.ref.key, action.thread_id, "owner-id", action.id
    )
    client.metadata.return_value = {
        "id": "shared-id",
        "mimeType": SHEET,
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.list_permissions.side_effect = [
        {"permissions": []},
        {
            "permissions": [
                {
                    "id": "anyoneWithLink",
                    "type": "anyone",
                    "role": "reader",
                    "allowFileDiscovery": False,
                },
                {
                    "id": "1042",
                    "type": "user",
                    "role": "writer",
                    "emailAddress": "collaborator@example.com",
                },
            ]
        },
    ]
    shared, message = await publish_sheet_link_access(
        service, action, ctx, action.payload, "shared-id"
    )
    assert not shared and "Sharing failed" in message


async def test_sheet_sharing_refuses_unsafe_acl_without_grant():
    from chatcore_google.link_sharing import publish_sheet_link_access

    service, client, ctx, _ = setup()
    action = await service.propose(
        ctx, "create_sheet", title="Shared", rows=[["x"]], link_access="viewer"
    )
    await service.repo.record_output(
        ctx.chat.key, "shared-id", SHEET, ctx.user.ref.key, action.thread_id, "owner-id", action.id
    )
    client.metadata.return_value = {
        "id": "shared-id",
        "mimeType": SHEET,
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.list_permissions.return_value = {
        "permissions": [{"type": "anyone", "role": "writer", "allowFileDiscovery": True}]
    }
    shared, message = await publish_sheet_link_access(
        service, action, ctx, action.payload, "shared-id"
    )
    assert not shared and "unsafe" in message
    client.grant_link_viewer.assert_not_awaited()


async def test_sheet_sharing_grant_failure_links_file_and_does_not_retry():
    from chatcore_google.link_sharing import publish_sheet_link_access

    service, client, ctx, _ = setup()
    action = await service.propose(
        ctx, "create_sheet", title="Shared", rows=[["x"]], link_access="viewer"
    )
    await service.repo.record_output(
        ctx.chat.key,
        "shared-id",
        SHEET,
        ctx.user.ref.key,
        action.thread_id,
        "owner-id",
        action.id,
    )
    client.metadata.return_value = {
        "id": "shared-id",
        "mimeType": SHEET,
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.list_permissions.return_value = {"permissions": []}
    client.grant_link_viewer.side_effect = RuntimeError("fake grant failure")
    shared, message = await publish_sheet_link_access(
        service, action, ctx, action.payload, "shared-id"
    )
    assert not shared and "Sharing failed" in message and "shared-id" in message
    client.grant_link_viewer.assert_awaited_once_with("shared-id")


async def test_output_journal_is_chat_and_topic_scoped_and_drives_create_cap():
    from chatcore_google.repository import GoogleRepository

    repo = GoogleRepository()
    await repo.record_output("chat:a", "one", "sheet", "user:a", "topic:a", "owner", "action")
    assert await repo.output("chat:a", "topic:a", "one") is not None
    assert await repo.output("chat:b", "topic:a", "one") is None
    assert await repo.output("chat:a", "topic:b", "one") is None
    await repo.reserve_create("chat:a", "action", "user:a", 1)
    assert await repo.creates_today("chat:a", "user:a") == 1
    await repo.purge("chat:a", "user:a")
    assert await repo.output("chat:a", "topic:a", "one") is None


@pytest.mark.parametrize(
    ("enabled", "confirm_writes", "summary_text", "result_text"),
    [
        (True, None, "Turn Google tools on", "Google tools are on"),
        (None, False, "Turn off write previews", "Write previews are off"),
        (True, False, "Turn Google tools on", "Google tools are on"),
    ],
)
async def test_configure_summaries_and_results_cover_each_changed_field(
    enabled, confirm_writes, summary_text, result_text
):
    service, _, ctx, _ = setup()
    action = await service.propose_configure(ctx, enabled=enabled, confirm_writes=confirm_writes)
    assert summary_text in action.summary
    result = await service.commit(action, ctx)
    assert result_text in result
    if enabled is not None and confirm_writes is not None:
        assert "Write previews are off" in result
        assert "write previews" in action.summary


async def test_turning_previews_on_summary_promises_confirmation():
    service, _, ctx, _ = setup()
    action = await service.propose_configure(ctx, confirm_writes=True)
    assert "show a preview to confirm first" in action.summary


async def test_create_reservations_are_atomic_idempotent_and_purged():
    repo = GoogleRepository()
    results = await asyncio.gather(
        repo.reserve_create("fake:a", "action-1", "user:a", 1),
        repo.reserve_create("fake:a", "action-2", "user:a", 1),
        return_exceptions=True,
    )
    assert sum(isinstance(result, PermissionError) for result in results) == 1
    assert await repo.creates_today("fake:a", "user:a") == 1
    await repo.reserve_create("fake:a", "action-1", "user:a", 1)
    assert await repo.creates_today("fake:a", "user:a") == 1
    await repo.purge("fake:a", "user:a")
    assert await repo.creates_today("fake:a", "user:a") == 0


async def test_memory_purge_clears_chat_preview_setting():
    repo = GoogleRepository()
    await repo.configure_fields("fake:a", confirm_writes=False, expected_revision=0)
    assert await repo.confirm_writes("fake:a") is False
    await repo.purge("fake:a")
    assert await repo.confirm_writes("fake:a") is True


async def test_memory_retention_purges_old_outputs_and_reservations_only():
    from datetime import UTC, datetime, timedelta

    repo = GoogleRepository()
    cutoff = datetime.now(UTC) - timedelta(days=7)
    for file_id in ("old", "new"):
        await repo.record_output("fake:a", file_id, SHEET, "user:a", None, "owner", file_id)
    for action_id in ("old", "new"):
        await repo.reserve_create("fake:a", action_id, "user:a", 5)
    repo._outputs[("fake:a", "old")]["created_at"] = cutoff - timedelta(seconds=1)
    repo._outputs[("fake:a", "new")]["created_at"] = cutoff + timedelta(seconds=1)
    repo._create_reservations[("fake:a", "old")]["created_at"] = cutoff - timedelta(seconds=1)
    repo._create_reservations[("fake:a", "new")]["created_at"] = cutoff + timedelta(seconds=1)
    assert await repo.output("fake:a", None, "old") is not None
    assert await repo.output("fake:a", None, "new") is not None
    assert len(repo._outputs) == 2
    assert len(repo._create_reservations) == 2
    await repo.purge_older_than(cutoff)
    assert await repo.output("fake:a", None, "old") is None
    assert await repo.output("fake:a", None, "new") is not None
    assert ("fake:a", "old") not in repo._create_reservations
    assert ("fake:a", "new") in repo._create_reservations
    assert len(repo._outputs) == 1
    assert len(repo._create_reservations) == 1


async def test_last_allowed_member_sheet_is_shared_after_idempotent_create_reservation():
    service, client, ctx, _ = setup()
    ctx.user = user("member")
    ctx.is_admin = False
    service.settings.create_users = "members"
    service.settings.creates_per_user_day = 1
    client.request.return_value = {
        "spreadsheetId": "last-sheet",
        "sheets": [{"properties": {"sheetId": 0}}],
    }
    client.metadata.return_value = {
        "id": "last-sheet",
        "mimeType": SHEET,
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.list_permissions.side_effect = [
        {"permissions": []},
        {
            "permissions": [
                {
                    "id": "anyoneWithLink",
                    "type": "anyone",
                    "role": "reader",
                    "allowFileDiscovery": False,
                }
            ]
        },
    ]
    action = await service.propose(ctx, "create_sheet", title="Last", rows=[["x"]])
    result = await service.commit(action, ctx)
    assert "Created" in result and "Sharing failed" not in result
    client.grant_link_viewer.assert_awaited_once_with("last-sheet")


async def test_sheet_sharing_rechecks_authorization_after_acl_preflight():
    service, client, ctx, _ = setup()
    ctx.user = user("member")
    ctx.is_admin = False
    service.settings.create_users = "members"
    client.request.return_value = {
        "spreadsheetId": "revoked-sheet",
        "sheets": [{"properties": {"sheetId": 0}}],
    }
    client.metadata.return_value = {
        "id": "revoked-sheet",
        "mimeType": SHEET,
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }

    async def revoke_during_acl(_file_id):
        await service.repo.configure(ctx.chat.key, False, 0)
        return {"permissions": []}

    client.list_permissions.side_effect = revoke_during_acl
    action = await service.propose(
        ctx, "create_sheet", title="Revoked", rows=[["x"]], link_access="viewer"
    )
    result = await service.commit(action, ctx)
    assert "Sharing failed" in result
    client.grant_link_viewer.assert_not_awaited()


async def test_concurrent_member_sheet_commits_respect_atomic_daily_cap():
    service, client, ctx, _ = setup()
    ctx.user = user("member")
    ctx.is_admin = False
    service.settings.create_users = "members"
    service.settings.creates_per_user_day = 1
    client.request.return_value = {
        "spreadsheetId": "only-sheet",
        "sheets": [{"properties": {"sheetId": 0}}],
    }
    client.metadata.return_value = {
        "id": "only-sheet",
        "mimeType": SHEET,
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.list_permissions.side_effect = [
        {"permissions": []},
        {
            "permissions": [
                {
                    "id": "anyoneWithLink",
                    "type": "anyone",
                    "role": "reader",
                    "allowFileDiscovery": False,
                }
            ]
        },
    ]
    first = await service.propose(ctx, "create_sheet", title="First", rows=[["x"]])
    second = await service.propose(ctx, "create_sheet", title="Second", rows=[["x"]])
    outcomes = await asyncio.gather(
        service.commit(first, ctx), service.commit(second, ctx), return_exceptions=True
    )
    assert sum(isinstance(result, PermissionError) for result in outcomes) == 1
    assert client.request.await_count == 1


async def test_last_allowed_member_doc_is_shared_after_create_reservation():
    service, client, ctx, _ = setup()
    ctx.user = user("member")
    ctx.is_admin = False
    service.settings.create_users = "members"
    service.settings.creates_per_user_day = 1
    client.request.side_effect = [{"documentId": "last-doc"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "last-doc",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    owner_acl = {"type": "user", "role": "owner", "emailAddress": "owner@example.com"}
    client.list_permissions.side_effect = [
        {"permissions": [owner_acl]},
        {
            "permissions": [
                owner_acl,
                {
                    "id": "anyoneWithLink",
                    "type": "anyone",
                    "role": "reader",
                    "allowFileDiscovery": False,
                },
            ]
        },
    ]
    action = await service.propose(ctx, "create_doc", title="Last", text="Hello")
    result = await service.commit(action, ctx)
    assert "Created and verified" in result
    client.grant_link_viewer.assert_awaited_once_with("last-doc")


@pytest.mark.parametrize(
    "error,phrase",
    [(GoogleError("Google HTTP 400"), "Partial"), (RuntimeError("timeout"), "Uncertain")],
)
async def test_rich_doc_batch_refusal_is_partial_and_transport_failure_uncertain(
    error, phrase, request_driven_rich_doc_api
):
    service, client, ctx, commit = setup()
    request_driven_rich_doc_api.wire(client)
    request_driven_rich_doc_api.fail_batch = "A"
    request_driven_rich_doc_api.fail_error = error
    action = await service.propose(
        ctx, "create_rich_doc", title="Refused", blocks=[{"type": "paragraph", "text": "Body"}]
    )
    result = await service.commit(action, commit)
    assert result.startswith(phrase + ":")
    client.rename_file.assert_not_awaited()
    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert journal["phase"] == phrase.lower()
    assert journal["failed_stage"] == "content_verification"


async def test_rich_doc_rename_failure_still_shares_for_member(request_driven_rich_doc_api):
    service, client, ctx, _ = setup()
    request_driven_rich_doc_api.wire(client)
    request_driven_rich_doc_api.fail_rename = True
    request_driven_rich_doc_api.name_after_rename_error = "Notes (incomplete)"
    service.settings.create_users = "members"
    ctx.user = user("member")
    ctx.is_admin = False
    action = await service.propose(
        ctx, "create_rich_doc", title="Notes", blocks=[{"type": "paragraph", "text": "Body"}]
    )
    assert action.payload["link_access"] == "viewer" and action.payload["link_forced"]
    result = await service.commit(action, ctx)
    assert 'title still ends with "(incomplete)"' in result
    assert "anyone with the link can view" in result
    assert any(p.get("type") == "anyone" for p in request_driven_rich_doc_api.permissions)
    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert journal["phase"] == "partial" and journal["failed_stage"] == "title_rename"


async def test_rich_doc_unsafe_existing_acl_is_not_shared(request_driven_rich_doc_api):
    service, client, ctx, commit = setup()
    request_driven_rich_doc_api.wire(client)
    request_driven_rich_doc_api.permissions.append(
        {"id": "domain-1", "type": "domain", "role": "writer", "domain": "example.com"}
    )
    action = await service.propose(
        ctx,
        "create_rich_doc",
        title="Shared",
        link_access="viewer",
        blocks=[{"type": "paragraph", "text": "Body"}],
    )
    result = await service.commit(action, commit)
    assert "anyone with the link can view" not in result
    assert not any(p.get("type") == "anyone" for p in request_driven_rich_doc_api.permissions)
    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert journal["phase"] != "verified"
