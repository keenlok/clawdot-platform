import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from chatcore.confirm import ConfirmService
from chatcore.plugin import CommandContext
from chatcore.skills import GateState
from chatcore.testing import GROUP, press, user
from chatcore.tools.registry import ToolRegistry
from chatcore_google.plugin import GOOGLE_INSTRUCTIONS, GooglePlugin
from chatcore_google.settings import GoogleSettings


async def test_unconfigured_google_is_inert_and_has_no_tools(google_service):
    service, _, _, _ = google_service
    plugin = GooglePlugin(GoogleSettings(), service.access)
    assert plugin.tools() == []
    await plugin.aclose()


async def test_edit_tools_are_gated_writes_in_google_workspace_skill(google_service):
    service, _, ctx, _ = google_service
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
    assert "google_append_doc_blocks for Docs" in GOOGLE_INSTRUCTIONS
    assert "google_trash_file for bot-made files in this chat" in GOOGLE_INSTRUCTIONS
    gates = plugin.tool_gates()
    assert (await gates["google_edit_doc"].scope(GROUP, False)).state == GateState.AVAILABLE
    new_tools = {
        "google_add_sheet_tab",
        "google_replace_sheet_tab",
        "google_trash_file",
        "google_append_doc_blocks",
    }
    assert new_tools <= set(plugin.skills()[0].tools)
    assert {name.removeprefix("google_") for name in new_tools} <= set(plugin.action_handlers())
    assert all(tools[name].writes for name in new_tools)
    for name in new_tools:
        assert (await gates[name].user(ctx)).state == GateState.AVAILABLE


async def test_rich_doc_schema_is_inline_and_registered_for_confirmation(google_service):
    service, _, _, _ = google_service
    plugin = GooglePlugin(service.settings, service.access, service=service)
    tool = next(tool for tool in plugin.tools() if tool.name == "google_create_rich_doc")
    parameters = tool.parameters

    def schema_keys(value):
        if isinstance(value, dict):
            yield from value.keys()
            for child in value.values():
                yield from schema_keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from schema_keys(child)

    assert "$ref" not in set(schema_keys(parameters))
    assert "$defs" not in parameters
    assert "create_rich_doc" in plugin.action_handlers()
    assert parameters["properties"]["blocks"]["items"]["oneOf"]


async def test_append_doc_blocks_tool_is_in_skill_and_uses_create_gate(google_service):
    service, _, ctx, _ = google_service
    plugin = GooglePlugin(service.settings, service.access, service=service)
    tool = next(tool for tool in plugin.tools() if tool.name == "google_append_doc_blocks")
    assert tool.writes
    assert tool.parameters["properties"]["blocks"]["items"]["oneOf"]
    assert "tab_id" in tool.parameters["properties"]
    assert "google_append_doc_blocks" in plugin.skills()[0].tools
    assert "append_doc_blocks" in plugin.action_handlers()
    assert (
        await plugin.tool_gates()["google_append_doc_blocks"].user(ctx)
    ).state == GateState.AVAILABLE


async def test_append_doc_blocks_requires_tab_id_for_multi_tab_docs(google_service):
    service, client, ctx, _ = google_service
    url = "https://docs.google.com/document/d/multi/edit"
    ctx.turn.human_urls = frozenset({url})
    content = [
        {"startIndex": 0, "endIndex": 1, "sectionBreak": {"sectionStyle": {}}},
        {
            "startIndex": 1,
            "endIndex": 2,
            "paragraph": {
                "elements": [{"startIndex": 1, "endIndex": 2, "textRun": {"content": "\n"}}],
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            },
        },
    ]
    client.document_for_edit.return_value = {
        "revisionId": "rev-1",
        "tabs": [
            {"tabProperties": {"tabId": "tab-a"}, "documentTab": {"body": {"content": content}}},
            {"tabProperties": {"tabId": "tab-b"}, "documentTab": {"body": {"content": content}}},
        ],
    }
    service.doc_source = AsyncMock(
        return_value=(
            type("Ref", (), {"file_id": "multi"})(),
            {"mimeType": "application/vnd.google-apps.document", "name": "Multi"},
            "owner-id",
            None,
        )
    )
    with pytest.raises(ValueError, match="Specify tab_id"):
        await service.propose(
            ctx, "append_doc_blocks", url=url, blocks=[{"type": "paragraph", "text": "x"}]
        )
    action = await service.propose(
        ctx,
        "append_doc_blocks",
        url=url,
        blocks=[{"type": "paragraph", "text": "x"}],
        tab_id="tab-b",
    )
    assert action.payload["tab_id"] == "tab-b"


async def test_append_doc_blocks_changes_only_the_selected_tab(
    google_service, request_driven_rich_doc_api
):
    service, client, ctx, commit = google_service
    from packages.plugins.google.tests.conftest import RequestDrivenRichDocApi

    url = f"https://docs.google.com/document/d/{request_driven_rich_doc_api.document_id}/edit"
    ctx.turn.human_urls = frozenset({url})
    target = request_driven_rich_doc_api
    target.tab_id = "selected-tab"
    target.title = "Multi tab Doc"
    target.nodes = [
        {
            "kind": "paragraph",
            "text": "Selected\n",
            "style": {"namedStyleType": "HEADING_1"},
            "inline": [],
        }
    ]
    other = RequestDrivenRichDocApi(
        tab_id="other-tab",
        initial_nodes=[
            {
                "kind": "paragraph",
                "text": "Keep this\n",
                "style": {"namedStyleType": "NORMAL_TEXT"},
                "inline": [],
            }
        ],
    )
    target.wire(client)

    async def read_both(file_id):
        document = target._document()
        document["tabs"].append(other._document()["tabs"][0])
        return document

    client.document_for_edit.side_effect = read_both
    client.edit_document.side_effect = target.edit_document
    before_other = copy.deepcopy(other.nodes)
    plugin = GooglePlugin(service.settings, service.access, service=service)
    result = await plugin.append_doc_blocks(
        ctx,
        url,
        [{"type": "paragraph", "text": "Only here"}],
        tab_id="selected-tab",
    )
    assert result.pending_action is not None
    confirm = ConfirmService(
        ctx.platform,
        ctx.stores.repo,
        {"google." + key: handler for key, handler in plugin.action_handlers().items()},
    )
    await confirm.request(result.pending_action, ctx.user)
    response = await confirm.on_press(press(result.pending_action.id, sender=ctx.user), commit)
    assert "Appended 1 blocks" in response
    assert "Only here" in "".join(node.get("text", "") for node in target.nodes)
    assert other.nodes == before_other
    for batch in target.batch_a:
        for request in batch:
            operation = next(iter(request.values()))
            location = operation.get("range") or operation.get("location")
            if location:
                assert location["tabId"] == "selected-tab"


@pytest.mark.parametrize("failed_batch", ["A", "B", "C"])
async def test_append_doc_blocks_reports_partial_without_retry(
    google_service, request_driven_rich_doc_api, failed_batch
):
    service, client, ctx, _ = google_service
    api = request_driven_rich_doc_api
    url = f"https://docs.google.com/document/d/{api.document_id}/edit"
    ctx.turn.human_urls = frozenset({url})
    api.fail_batch = failed_batch
    api.wire(client)
    plugin = GooglePlugin(service.settings, service.access, service=service)
    result = await plugin.append_doc_blocks(
        ctx,
        url,
        [
            {"type": "paragraph", "text": "Append"},
            {"type": "table", "header": ["A"], "rows": [["B"]]},
        ],
    )
    assert result.pending_action is not None
    response = await plugin.commit(result.pending_action, ctx)
    assert ("uncertain:" if failed_batch == "A" else "partial:") in response.casefold()
    assert url in response
    assert len(api.batch_a) == 1
    assert len(api.batch_b) == (1 if failed_batch in {"B", "C"} else 0)
    assert len(api.batch_c) == (1 if failed_batch == "C" else 0)


async def test_append_doc_blocks_reports_changed_prefix_as_uncertain(
    google_service, request_driven_rich_doc_api
):
    service, client, ctx, _ = google_service
    api = request_driven_rich_doc_api
    url = f"https://docs.google.com/document/d/{api.document_id}/edit"
    ctx.turn.human_urls = frozenset({url})
    api.nodes = [
        {
            "kind": "paragraph",
            "text": "Original\n",
            "style": {"namedStyleType": "HEADING_1"},
            "inline": [],
        },
        {
            "kind": "paragraph",
            "text": "\n",
            "style": {"namedStyleType": "NORMAL_TEXT"},
            "inline": [],
        },
    ]
    api.wire(client)
    plugin = GooglePlugin(service.settings, service.access, service=service)
    action_result = await plugin.append_doc_blocks(
        ctx, url, [{"type": "paragraph", "text": "New text"}]
    )
    assert action_result.pending_action is not None
    reads = 0

    async def corrupt_final_read(file_id):
        nonlocal reads
        reads += 1
        document = await api.document_for_edit(file_id)
        if reads == 2:
            paragraph = next(
                item["paragraph"]
                for item in document["tabs"][0]["documentTab"]["body"]["content"]
                if "paragraph" in item
            )
            paragraph["elements"][0]["textRun"]["content"] = "Changed\n"
        return document

    client.document_for_edit.side_effect = corrupt_final_read
    response = await plugin.commit(action_result.pending_action, ctx)
    assert "original content could not be verified" in response
    assert url in response


async def test_append_doc_blocks_reauthorizes_after_commit_document_read(
    google_service, request_driven_rich_doc_api
):
    from chatcore_google.service import DOC

    service, client, ctx, _ = google_service
    service.settings.create_users = "members"
    ctx.user = user("member")
    ctx.is_admin = False
    api = request_driven_rich_doc_api
    url = f"https://docs.google.com/document/d/{api.document_id}/edit"
    ctx.turn.human_urls = frozenset({url})
    api.wire(client)
    await service.repo.record_output(
        ctx.chat.key, api.document_id, DOC, ctx.user.ref.key, ctx.thread_id, "owner-id", "created"
    )
    plugin = GooglePlugin(service.settings, service.access, service=service)
    result = await plugin.append_doc_blocks(ctx, url, [{"type": "paragraph", "text": "Append"}])
    assert result.pending_action is not None

    async def revoke_during_final_read(file_id):
        service.settings.create_users = "writers"
        return await api.document_for_edit(file_id)

    client.document_for_edit.side_effect = revoke_during_final_read
    response = await plugin.commit(result.pending_action, ctx)
    assert "Google file creation" in response
    client.edit_document.assert_not_awaited()


async def test_append_prefix_verification_detects_second_paragraph_in_existing_cell(
    google_service, request_driven_rich_doc_api
):
    service, client, ctx, _ = google_service
    api = request_driven_rich_doc_api
    url = f"https://docs.google.com/document/d/{api.document_id}/edit"
    ctx.turn.human_urls = frozenset({url})
    api.nodes = [
        {
            "kind": "table",
            "rows": [[{"text": "old\n", "style": {}}]],
        },
        {
            "kind": "paragraph",
            "text": "\n",
            "style": {"namedStyleType": "NORMAL_TEXT"},
            "inline": [],
        },
    ]
    api.wire(client)
    plugin = GooglePlugin(service.settings, service.access, service=service)
    action_result = await plugin.append_doc_blocks(
        ctx, url, [{"type": "paragraph", "text": "Appended"}]
    )
    assert action_result.pending_action is not None
    reads = 0

    async def add_then_change_second_cell_paragraph(file_id):
        nonlocal reads
        reads += 1
        document = await api.document_for_edit(file_id)
        table = next(
            item["table"]
            for item in document["tabs"][0]["documentTab"]["body"]["content"]
            if "table" in item
        )
        cell = table["tableRows"][0]["tableCells"][0]
        second = copy.deepcopy(cell["content"][0])
        second["paragraph"]["elements"][0]["textRun"]["content"] = "second\n"
        cell["content"].append(second)
        if reads == 2:
            cell["content"][1]["paragraph"]["elements"][0]["textRun"]["content"] = "changed\n"
        return document

    client.document_for_edit.side_effect = add_then_change_second_cell_paragraph
    response = await plugin.commit(action_result.pending_action, ctx)
    assert "original content could not be verified" in response
    assert url in response


async def test_rich_doc_confirmation_request_runs_registered_commit(
    google_service, request_driven_rich_doc_api
):
    service, client, ctx, commit = google_service
    request_driven_rich_doc_api.wire(client)
    plugin = GooglePlugin(service.settings, service.access, service=service)
    blocks = [
        {"type": "title", "text": "Confirmation"},
        {"type": "table", "header": ["A"], "rows": [["B"]]},
    ]
    result = await plugin.create_rich_doc(ctx, "Confirm me", blocks)
    assert result.pending_action
    confirm = ConfirmService(
        ctx.platform,
        ctx.stores.repo,
        {"google." + key: handler for key, handler in plugin.action_handlers().items()},
    )
    await confirm.request(result.pending_action, ctx.user)
    response = await confirm.on_press(press(result.pending_action.id, sender=ctx.user), commit)
    assert "Created and verified" in response
    assert request_driven_rich_doc_api.batch_a
    assert request_driven_rich_doc_api.batch_b
    assert request_driven_rich_doc_api.batch_c


async def test_request_driven_docs_fake_uses_google_revision_shape_and_rejects_stale():
    from packages.plugins.google.tests.conftest import RequestDrivenRichDocApi

    api = RequestDrivenRichDocApi()
    client = AsyncMock()
    api.wire(client)
    response = await api.edit_document(api.document_id, [], "rich-revision-1")
    assert response == {
        "documentId": api.document_id,
        "replies": [],
        "writeControl": {"requiredRevisionId": "after-C"},
    }
    with pytest.raises(ValueError, match="revision"):
        await api.edit_document(api.document_id, [], "stale-revision")
    with pytest.raises(ValueError, match="revision"):
        await api.edit_document(api.document_id, [], None)


async def test_append_doc_blocks_stops_when_write_control_is_missing(
    google_service, request_driven_rich_doc_api
):
    service, client, ctx, commit = google_service
    api = request_driven_rich_doc_api
    api.omit_write_control = True
    url = f"https://docs.google.com/document/d/{api.document_id}/edit"
    ctx.turn.human_urls = frozenset({url})
    api.wire(client)
    plugin = GooglePlugin(service.settings, service.access, service=service)
    result = await plugin.append_doc_blocks(
        ctx,
        url,
        [
            {"type": "paragraph", "text": "First"},
            {"type": "table", "header": ["A"], "rows": [["B"]]},
        ],
    )
    response = await plugin.commit(result.pending_action, ctx)
    assert "partial:" in response.casefold()
    assert url in response
    assert api.revision_inputs == ["rich-revision-1"]
    assert not api.batch_b and not api.batch_c


async def test_append_doc_blocks_human_revision_change_stops_before_write(
    google_service, request_driven_rich_doc_api
):
    service, client, ctx, _ = google_service
    api = request_driven_rich_doc_api
    api.wire(client)
    url = f"https://docs.google.com/document/d/{api.document_id}/edit"
    ctx.turn.human_urls = frozenset({url})
    action = await service.propose(
        ctx, "append_doc_blocks", url=url, blocks=[{"type": "paragraph", "text": "Frozen"}]
    )
    api.revision = "collaborator-edit"
    with pytest.raises(ValueError, match="Doc changed since the preview"):
        await service.commit(action, ctx)
    assert not api.batch_a and not api.batch_b and not api.batch_c


@pytest.mark.parametrize("partial", [False, True])
async def test_append_doc_blocks_replay_reservation_blocks_second_commit(
    google_service, request_driven_rich_doc_api, partial
):
    service, client, ctx, _ = google_service
    api = request_driven_rich_doc_api
    api.wire(client)
    url = f"https://docs.google.com/document/d/{api.document_id}/edit"
    ctx.turn.human_urls = frozenset({url})
    await service.repo.record_output(
        ctx.chat.key,
        api.document_id,
        "application/vnd.google-apps.document",
        ctx.user.ref.key,
        ctx.thread_id,
        "owner-id",
        "creation-action",
    )
    if partial:
        api.fail_batch = "B"
    action = await service.propose(
        ctx,
        "append_doc_blocks",
        url=url,
        blocks=[
            {"type": "paragraph", "text": "One"},
            {"type": "table", "header": ["A"], "rows": [["B"]]},
        ],
    )
    await service.commit(action, ctx)
    writes = len(api.batch_a)
    with pytest.raises(Exception, match="already|reserved|action"):
        await service.commit(action, ctx)
    assert len(api.batch_a) == writes


async def test_append_doc_blocks_verification_read_failure_is_partial(
    google_service, request_driven_rich_doc_api
):
    service, client, ctx, _ = google_service
    api = request_driven_rich_doc_api
    api.wire(client)
    url = f"https://docs.google.com/document/d/{api.document_id}/edit"
    ctx.turn.human_urls = frozenset({url})
    original_read = client.document_for_edit.side_effect
    reads = 0

    async def fail_verification_read(file_id):
        nonlocal reads
        reads += 1
        if reads == 4:  # proposal, commit snapshot, table fill indices, verification
            raise RuntimeError("verification read failed")
        return await original_read(file_id)

    client.document_for_edit.side_effect = fail_verification_read
    action = await service.propose(
        ctx,
        "append_doc_blocks",
        url=url,
        blocks=[
            {"type": "paragraph", "text": "Inserted"},
            {"type": "table", "header": ["A"], "rows": [["B"]]},
        ],
    )
    result = await service.commit(action, ctx)
    assert "partial:" in result.casefold()
    assert url in result
    assert api.batch_a and api.batch_b and api.batch_c


@pytest.mark.parametrize("as_output", [True, False])
async def test_append_doc_blocks_output_switch_and_human_confirmation(
    google_service, request_driven_rich_doc_api, as_output
):
    service, client, ctx, commit = google_service
    url = f"https://docs.google.com/document/d/{request_driven_rich_doc_api.document_id}/edit"
    ctx.turn.human_urls = frozenset({url})
    request_driven_rich_doc_api.title = "Existing Doc"
    request_driven_rich_doc_api.nodes = [
        {
            "kind": "paragraph",
            "text": "Heading 😀\n",
            "style": {"namedStyleType": "HEADING_1"},
            "inline": [],
        },
        {
            "kind": "table",
            "rows": [[{"text": "Old table\n", "style": {}}]],
        },
        {
            "kind": "paragraph",
            "text": "Existing bullet\n",
            "style": {"namedStyleType": "HEADING_2"},
            "inline": [(0, 8, {"italic": True})],
            "bullet": {"listId": "old-list"},
        },
    ]
    request_driven_rich_doc_api.wire(client)
    if as_output:
        await service.repo.record_output(
            ctx.chat.key,
            request_driven_rich_doc_api.document_id,
            "application/vnd.google-apps.document",
            ctx.user.ref.key,
            ctx.thread_id,
            "owner-id",
            "old-action",
        )
    await service.repo.configure_fields(ctx.chat.key, confirm_writes=False, expected_revision=0)
    plugin = GooglePlugin(service.settings, service.access, service=service)
    before = copy.deepcopy(request_driven_rich_doc_api.nodes)
    result = await plugin.append_doc_blocks(
        ctx,
        url,
        [
            {"type": "paragraph", "spans": [{"text": "New 😀"}, {"text": " bold", "bold": True}]},
            {"type": "table", "header": ["Place"], "rows": [["Kyoto"]]},
            {"type": "table", "header": ["Time"], "rows": [["09:00"]]},
        ],
    )
    if as_output:
        assert result.pending_action is None
        assert "Appended 3 blocks" in result.content
    else:
        assert result.pending_action is not None
        assert not request_driven_rich_doc_api.batch_a
        confirm = ConfirmService(
            ctx.platform,
            ctx.stores.repo,
            {"google." + key: handler for key, handler in plugin.action_handlers().items()},
        )
        await confirm.request(result.pending_action, ctx.user)
        response = await confirm.on_press(press(result.pending_action.id, sender=ctx.user), commit)
        assert "Appended 3 blocks" in response
    assert request_driven_rich_doc_api.batch_a
    assert request_driven_rich_doc_api.batch_b
    assert request_driven_rich_doc_api.batch_c
    assert request_driven_rich_doc_api.revision_inputs == [
        "rich-revision-1",
        "after-A",
        "after-B",
    ]
    assert request_driven_rich_doc_api.nodes[0]["text"] == before[0]["text"]
    assert (
        request_driven_rich_doc_api.nodes[1]["rows"][0][0]["text"]
        == before[1]["rows"][0][0]["text"]
    )
    assert request_driven_rich_doc_api.nodes[2]["text"] == "Existing bullet\n"
    assert request_driven_rich_doc_api.nodes[2]["style"] == before[2]["style"]
    assert request_driven_rich_doc_api.nodes[2]["inline"] == before[2]["inline"]
    assert request_driven_rich_doc_api.nodes[2].get("bullet") == {"listId": "old-list"}
    new_paragraph = next(
        node for node in request_driven_rich_doc_api.nodes if "New 😀" in node.get("text", "")
    )
    assert new_paragraph["style"] == {"namedStyleType": "NORMAL_TEXT"}
    assert "bullet" not in new_paragraph
    assert new_paragraph["inline"]
    assert all("italic" not in marked for _, _, marked in new_paragraph["inline"])
    tables = [node for node in request_driven_rich_doc_api.nodes if node["kind"] == "table"]
    assert len(tables) == 3
    assert tables[0]["rows"][0][0]["text"] == before[1]["rows"][0][0]["text"]
    assert tables[1]["rows"][0][0]["text"] == "Place\n"
    assert tables[1]["rows"][1][0]["text"] == "Kyoto\n"
    assert tables[2]["rows"][0][0]["text"] == "Time\n"
    assert tables[2]["rows"][1][0]["text"] == "09:00\n"


async def test_workbook_tool_is_in_skill_and_runs_immediately_when_previews_are_off(google_service):
    service, client, ctx, _ = google_service
    plugin = GooglePlugin(service.settings, service.access, service=service)
    tool_map = {tool.name: tool for tool in plugin.tools()}
    assert "google_create_workbook" in tool_map
    assert "google_create_workbook" in plugin.skills()[0].tools
    assert "google_create_rich_doc" in plugin.skills()[0].tools
    assert tool_map["google_create_workbook"].writes
    assert tool_map["google_create_rich_doc"].writes
    assert len(GOOGLE_INSTRUCTIONS) <= 800
    assert "google_create_rich_doc" in GOOGLE_INSTRUCTIONS
    assert (
        await plugin.tool_gates()["google_create_workbook"].user(ctx)
    ).state == GateState.AVAILABLE
    assert (
        await plugin.tool_gates()["google_create_rich_doc"].user(ctx)
    ).state == GateState.AVAILABLE
    ctx.user = user("member")
    ctx.is_admin = False
    assert (
        await plugin.tool_gates()["google_create_workbook"].user(ctx)
    ).state == GateState.NOT_ALLOWED
    assert (
        await plugin.tool_gates()["google_create_rich_doc"].user(ctx)
    ).state == GateState.NOT_ALLOWED
    service.settings.create_users = "members"
    assert (
        await plugin.tool_gates()["google_create_workbook"].user(ctx)
    ).state == GateState.AVAILABLE
    assert (
        await plugin.tool_gates()["google_create_rich_doc"].user(ctx)
    ).state == GateState.AVAILABLE
    ctx.user = user("owner")
    ctx.is_admin = True
    tabs = [{"name": "Compare", "columns": [{"header": "Name", "type": "text"}], "rows": [["A"]]}]
    invalid = await plugin.create_workbook(
        ctx,
        "Bad",
        [{"name": "Bad", "columns": [{"header": "Price", "type": "money"}], "rows": [[True]]}],
    )
    assert invalid.is_error
    client.request.assert_not_awaited()
    pending = await plugin.create_workbook(ctx, "Hotels", tabs)
    assert pending.pending_action is not None
    client.request.assert_not_awaited()
    await service.repo.configure_fields(GROUP.key, confirm_writes=False, expected_revision=0)
    client.request.return_value = {"spreadsheetId": "immediate-workbook"}
    result = await plugin.create_workbook(ctx, "Hotels", tabs)
    assert result.pending_action is None
    assert (
        "Created https://docs.google.com/spreadsheets/d/immediate-workbook/edit" in result.content
    )
    assert client.request.await_count == 1


async def test_rich_doc_tool_previews_then_uses_immediate_commit_when_switch_is_off(google_service):
    service, _, ctx, _ = google_service
    plugin = GooglePlugin(service.settings, service.access, service=service)
    action = SimpleNamespace(summary="Rich Doc preview")
    service.propose = AsyncMock(return_value=action)
    plugin.commit = AsyncMock(return_value="Created and verified")

    preview = await plugin.create_rich_doc(
        ctx, "Itinerary", [{"type": "paragraph", "text": "Hello"}]
    )
    assert preview.pending_action is action
    plugin.commit.assert_not_awaited()

    await service.repo.configure_fields(ctx.chat.key, confirm_writes=False, expected_revision=0)
    immediate = await plugin.create_rich_doc(
        ctx, "Itinerary", [{"type": "paragraph", "text": "Hello"}]
    )
    assert immediate.pending_action is None
    assert immediate.content == "Created and verified"
    plugin.commit.assert_awaited_once_with(action, ctx)


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


async def test_append_doc_blocks_refused_first_batch_reports_nothing_written(
    google_service, request_driven_rich_doc_api
):
    from chatcore_google.client import GoogleError

    service, client, ctx, _ = google_service
    api = request_driven_rich_doc_api
    url = f"https://docs.google.com/document/d/{api.document_id}/edit"
    ctx.turn.human_urls = frozenset({url})
    api.fail_batch = "A"
    api.fail_error = GoogleError("Google HTTP 400: stale revision")
    api.wire(client)
    plugin = GooglePlugin(service.settings, service.access, service=service)
    result = await plugin.append_doc_blocks(ctx, url, [{"type": "paragraph", "text": "Append"}])
    response = await plugin.commit(result.pending_action, ctx)
    assert "nothing was written" in response and url in response
    assert "appended" not in response.casefold().replace("the append", "")
    assert not api.batch_b and not api.batch_c
