from chatcore.confirm import ConfirmService
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
