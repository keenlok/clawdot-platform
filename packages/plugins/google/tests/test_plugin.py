from unittest.mock import AsyncMock

import pytest
from chatcore.confirm import ConfirmService
from chatcore.plugin import CommandContext
from chatcore.skills import GateState
from chatcore.testing import GROUP, press
from chatcore.tools.registry import ToolRegistry
from chatcore_google.plugin import GOOGLE_INSTRUCTIONS, GooglePlugin
from chatcore_google.settings import GoogleSettings


async def test_unconfigured_google_is_inert_and_has_no_tools(google_service):
    service, _, _, _ = google_service
    plugin = GooglePlugin(GoogleSettings(), service.access)
    assert plugin.tools() == []
    await plugin.aclose()


async def test_edit_tools_are_gated_writes_in_google_workspace_skill(google_service):
    service, _, _, _ = google_service
    plugin = GooglePlugin(service.settings, service.access, service=service)
    tools = {tool.name: tool for tool in plugin.tools()}
    assert {"google_edit_doc", "google_edit_sheet"} <= tools.keys()
    assert all(
        tools[name].writes and not tools[name].direct
        for name in ("google_edit_doc", "google_edit_sheet")
    )
    assert "google_edit_doc" in plugin.skills()[0].tools
    assert "google_edit_sheet" in plugin.skills()[0].tools
    assert tools["google_edit_doc"].parameters["additionalProperties"] is False
    assert tools["google_edit_sheet"].parameters["additionalProperties"] is False
    assert tools["google_create_doc"].parameters["properties"]["link_viewer"]["type"] == "boolean"
    assert tools["google_edit_doc"].parameters["properties"]["link_viewer"]["type"] == "boolean"
    assert tools["google_create_doc"].parameters["properties"]["link_access"]["enum"] == [
        "private",
        "viewer",
        "editor",
    ]
    assert tools["google_edit_doc"].parameters["properties"]["link_access"]["enum"] == [
        "private",
        "viewer",
        "editor",
    ]
    assert "source_message_ref" in tools["google_edit_doc"].parameters["properties"]
    assert "link_viewer" not in tools["google_edit_sheet"].parameters["properties"]
    assert len(GOOGLE_INSTRUCTIONS) <= 800
    gates = plugin.tool_gates()
    assert (await gates["google_edit_doc"].scope(GROUP, False)).state == GateState.AVAILABLE


async def test_source_derived_doc_guidance_preserves_original_package_and_uses_copy(google_service):
    service, _, _, _ = google_service
    plugin = GooglePlugin(service.settings, service.access, service=service)
    tools = {tool.name: tool for tool in plugin.tools()}
    create = tools["google_create_doc"]
    edit = tools["google_edit_doc"]
    descriptions = " ".join(
        [GOOGLE_INSTRUCTIONS, create.description, edit.description]
        + [p["description"] for p in edit.parameters["properties"].values() if "description" in p]
    ).casefold()

    assert "source-derived" in descriptions
    assert "google_edit_doc" in descriptions
    assert "destination=copy" in descriptions
    assert "original package" in descriptions
    assert "bounded" in descriptions
    assert "from scratch" in create.description.casefold()
    assert "confirm writes" in GOOGLE_INSTRUCTIONS.casefold()


async def test_docx_tool_output_is_untrusted_and_not_persisted(google_service, docx_bytes):
    service, client, ctx, _ = google_service
    client.metadata.return_value = {
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "capabilities": {"canDownload": True},
    }
    client.media.return_value = docx_bytes
    plugin = GooglePlugin(service.settings, service.access, service=service)
    result = await ToolRegistry(plugin.tools()).call(
        "google_read_doc", {"url": "https://docs.google.com/spreadsheets/d/file/edit"}, ctx
    )
    assert not result.is_error and "Tokyo itinerary" in result.content
    assert result.untrusted and not result.persist_content


async def test_registry_and_real_confirmation_commit_only_the_confirmed_write(google_service):
    service, client, ctx, commit = google_service
    client.values.return_value = {"values": []}
    plugin = GooglePlugin(service.settings, service.access, service=service)
    registry = ToolRegistry(plugin.tools())
    assert all(not tool.direct for tool in plugin.tools() if tool.writes)
    client.sheet.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": "Tokyo"}}]}
    url = "https://docs.google.com/spreadsheets/d/file/edit"
    result = await registry.call("google_append", {"url": url, "rows": [["Lunch", 12]]}, ctx)
    assert result.pending_action and not result.is_error
    client.batch.assert_not_awaited()
    confirm = ConfirmService(
        ctx.platform,
        ctx.stores.repo,
        {"google." + key: handler for key, handler in plugin.action_handlers().items()},
    )
    await confirm.request(result.pending_action, ctx.user)
    response = await confirm.on_press(press(result.pending_action.id, sender=ctx.user), commit)
    assert "appended" in response
    assert client.batch.await_count == 1
    assert (await registry.call("google_read_sheet", {"url": url}, ctx)).untrusted


async def test_preview_switch_runs_new_sheet_immediately_without_pending_action(google_service):
    service, client, ctx, _ = google_service
    client.request.return_value = {
        "spreadsheetId": "created-sheet",
        "sheets": [{"properties": {"sheetId": 0}}],
    }
    await service.repo.configure_fields(GROUP.key, confirm_writes=False, expected_revision=0)
    plugin = GooglePlugin(service.settings, service.access, service=service)
    result = await plugin.create_sheet(ctx, "Immediate", [["literal"]])
    assert not result.is_error
    assert result.pending_action is None
    assert "Created https://docs.google.com/spreadsheets/d/created-sheet/edit" in result.content
    client.batch.assert_awaited_once()


async def test_preview_switch_runs_doc_and_own_output_append_immediately(google_service):
    service, client, ctx, _ = google_service
    plugin = GooglePlugin(service.settings, service.access, service=service)
    await service.repo.configure_fields(GROUP.key, confirm_writes=False, expected_revision=0)
    client.request.side_effect = [{"documentId": "created-doc"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-doc",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = {
        "revisionId": "rev-1",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab", "title": "Output"},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "startIndex": 1,
                                "endIndex": 8,
                                "paragraph": {
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": 7,
                                            "textRun": {"content": "Hello\n"},
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
    doc_result = await plugin.create_doc(ctx, "Immediate", "Hello")
    assert doc_result.pending_action is None and "Created and verified" in doc_result.content
    assert client.request.await_count == 2

    client.metadata.return_value = {
        "id": "output-sheet",
        "name": "Output",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "ownedByMe": True,
    }
    client.sheet.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": "Output"}}]}
    await service.repo.record_output(
        ctx.chat.key,
        "output-sheet",
        "application/vnd.google-apps.spreadsheet",
        ctx.user.ref.key,
        ctx.thread_id,
        "owner-id",
        "output-action",
    )
    append_result = await plugin.append(
        ctx, "https://docs.google.com/spreadsheets/d/output-sheet/edit", rows=[["text"]]
    )
    assert append_result.pending_action is None
    assert "appended" in append_result.content.lower()
    client.batch.assert_awaited_once()


async def test_preview_switch_keeps_human_edits_configuration_and_expense_export_pending(
    google_service,
):
    service, client, ctx, _ = google_service
    await service.repo.configure_fields(GROUP.key, confirm_writes=False, expected_revision=0)
    plugin = GooglePlugin(service.settings, service.access, service=service)
    url = "https://docs.google.com/spreadsheets/d/file/edit"
    append = await plugin.append(ctx, url, rows=[["human"]])
    assert append.pending_action is not None

    edit_doc = await service.action(ctx, "edit_doc", {"revision": 0}, "edit preview")
    edit_sheet = await service.action(ctx, "edit_sheet", {"revision": 0}, "edit preview")
    expense = await service.action(ctx, "expenses_export", {"revision": 0}, "expense preview")
    plugin.edits.propose_doc = AsyncMock(return_value=edit_doc)
    plugin.edits.propose_sheet = AsyncMock(return_value=edit_sheet)
    plugin.expenses.propose_export = AsyncMock(return_value=expense)
    results = [
        await plugin.edit_doc(ctx, url, "before", "after"),
        await plugin.edit_sheet(ctx, url, "Sheet1", "A1", [["after"]]),
        await plugin.configure(ctx, enabled=False),
        await plugin.export(ctx),
    ]
    assert all(result.pending_action is not None for result in results)
    client.batch.assert_not_awaited()
    client.request.assert_not_awaited()


async def test_off_gate_still_exposes_configuration_and_handlers_enforce_it(google_service):
    service, client, ctx, _ = google_service
    plugin = GooglePlugin(service.settings, service.access, service=service)
    await service.repo.configure(GROUP.key, False, 0)
    gates = plugin.tool_gates()
    assert (await gates["google_read_sheet"].scope(GROUP, False)).state == GateState.OFF
    assert (await gates["configure_google"].user(ctx)).state == GateState.AVAILABLE
    result = await ToolRegistry(plugin.tools()).call(
        "google_read_sheet", {"url": "https://docs.google.com/spreadsheets/d/file/edit"}, ctx
    )
    assert result.is_error and "off" in result.content
    client.identity.assert_not_awaited()


async def test_uncertain_remote_write_does_not_become_generic_retry_advice(google_service):
    from chatcore_google.client import GoogleError

    service, client, ctx, commit = google_service
    plugin = GooglePlugin(service.settings, service.access, service=service)
    action = await service.propose(
        ctx, "append", url="https://docs.google.com/spreadsheets/d/file/edit", rows=[["Lunch"]]
    )
    client.batch.side_effect = GoogleError(
        "A write may have completed. Check the file before retrying."
    )
    confirm = ConfirmService(
        ctx.platform,
        ctx.stores.repo,
        {"google." + key: handler for key, handler in plugin.action_handlers().items()},
    )
    await confirm.request(action, ctx.user)
    result = await confirm.on_press(press(action.id, sender=ctx.user), commit)
    assert "may have completed" in result and "Check the file" in result
    assert "Please try again" not in result


async def test_expense_export_tool_follows_injected_providers(google_service):
    service, _, _, _ = google_service

    async def provider(ctx, trip, general):
        raise AssertionError("not called while listing tools")

    def tools(**kwargs):
        plugin = GooglePlugin(service.settings, service.access, service=service, **kwargs)
        return {tool.name: tool for tool in plugin.tools()}

    assert "expenses_export_to_sheet" not in tools()
    legacy = tools(expenses_provider=provider)
    assert legacy["expenses_export_to_sheet"].description.startswith("Export a real ledger")
    assert "itinerary_link_trip" in legacy
    only_workbook = tools(expenses_workbook_provider=provider)
    export = only_workbook["expenses_export_to_sheet"]
    assert export.writes and "formatted multi-tab" in export.description
    assert "itinerary_link_trip" not in only_workbook
    both = tools(expenses_provider=provider, expenses_workbook_provider=provider)
    assert {"expenses_export_to_sheet", "itinerary_link_trip"} <= both.keys()


async def test_configure_tool_requires_at_least_one_setting(google_service):
    service, _, ctx, _ = google_service
    plugin = GooglePlugin(service.settings, service.access, service=service)
    result = await plugin.configure(ctx)
    assert result.is_error and result.pending_action is None
    assert "Specify enabled or confirm_writes" in result.content


@pytest.mark.parametrize("value,expected", [("confirm off", False), ("confirm on", True)])
async def test_google_confirm_command_proposes_preview_switch(value, expected, google_service):
    service, _, _, commit = google_service
    plugin = GooglePlugin(service.settings, service.access, service=service)
    confirm = AsyncMock()
    command_ctx = CommandContext(
        commit.chat,
        commit.user,
        commit.is_dm,
        commit.is_admin,
        commit.platform,
        commit.stores,
        confirm=confirm,
    )
    await plugin.commands()[0].handler(command_ctx, value)
    pending = confirm.request.await_args.args[0]
    assert pending.payload["confirm_writes"] is expected


@pytest.mark.parametrize("confirm_writes", [False, True])
async def test_google_status_reports_preview_switch(confirm_writes, google_service):
    service, _, _, commit = google_service
    plugin = GooglePlugin(service.settings, service.access, service=service)
    await service.repo.configure_fields(
        commit.chat.key, confirm_writes=confirm_writes, expected_revision=0
    )
    command_ctx = CommandContext(
        commit.chat, commit.user, commit.is_dm, commit.is_admin, commit.platform, commit.stores
    )
    result = await plugin.commands()[0].handler(command_ctx, "")
    assert f"Write previews are {'on' if confirm_writes else 'off'} here." in result
