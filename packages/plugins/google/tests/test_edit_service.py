import io
import zipfile

import pytest
from chatcore_google.client import GoogleError
from chatcore_google.edit_service import GoogleEditService
from chatcore_google.service import DOC, SHEET


def doc(text, revision="rev-1", *, tab_id="tab-1", tab_title="Main"):
    units = len(text.encode("utf-16-le")) // 2
    return {
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": tab_id, "title": tab_title},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "startIndex": 1,
                                "endIndex": units + 2,
                                "paragraph": {
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": units + 1,
                                            "textRun": {
                                                "content": text,
                                                "textStyle": {"bold": True},
                                            },
                                        },
                                        {
                                            "startIndex": units + 1,
                                            "endIndex": units + 2,
                                            "textRun": {"content": "\n", "textStyle": {}},
                                        },
                                    ]
                                },
                            }
                        ]
                    }
                },
            }
        ],
    }


def metadata(file_id="file", mime=DOC, *, editable=True):
    return {
        "id": file_id,
        "name": "Trip",
        "mimeType": mime,
        "capabilities": {
            "canEdit": editable,
            "canModifyContent": editable,
            "canCopy": True,
            "canDownload": True,
        },
        "isAppAuthorized": True,
    }


def url(file_id="file"):
    return f"https://docs.google.com/document/d/{file_id}/edit"


def styled_table_docx(original):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            if name == "word/document.xml":
                data = (
                    b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    b"<w:body><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Tokyo itinerary</w:t></w:r></w:p>"
                    b"<w:tbl><w:tblPr/><w:tblGrid/><w:tr><w:tc><w:tcPr/><w:p><w:r>"
                    b"<w:t>Planning notes</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body>"
                    b"</w:document>"
                )
            else:
                data = source.read(name)
            target.writestr(name, data)
        target.writestr(
            "word/styles.xml",
            b'<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b'<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
            b"</w:style></w:styles>",
        )
    return output.getvalue()


async def test_doc_proposal_is_frozen_and_performs_no_remote_write(google_service):
    base, client, ctx, _ = google_service
    client.metadata.return_value = metadata()
    client.document_for_edit.return_value = doc("Dinner at 7pm")
    edits = GoogleEditService(base)
    action = await edits.propose_doc(ctx, url(), "Dinner at 7pm", "Dinner at 8pm")
    assert action.kind == "edit_doc"
    assert action.payload["destination"]["mode"] == "original"
    assert action.payload["precondition"]["start_index"] == 1
    assert action.payload["precondition"]["end_index"] == 14
    assert "Dinner at 7pm" in action.summary and "Dinner at 8pm" in action.summary
    client.edit_document.assert_not_awaited()
    client.copy_file.assert_not_awaited()
    client.grant_writer.assert_not_awaited()


async def test_link_viewer_choice_applies_only_to_frozen_new_doc_copies(google_service):
    base, client, ctx, _ = google_service
    client.metadata.return_value = metadata()
    client.document_for_edit.return_value = doc("Dinner at 7pm")
    edits = GoogleEditService(base)
    with pytest.raises(ValueError, match="Original Docs stay private"):
        await edits.propose_doc(ctx, url(), "Dinner at 7pm", "Dinner at 8pm", link_viewer=True)

    base.settings.copy_qualified = True
    base.settings.new_docs_link_viewer = True
    copied = await edits.propose_doc(
        ctx,
        url(),
        "Dinner at 7pm",
        "Dinner at 8pm",
        destination="copy",
    )
    private = await edits.propose_doc(
        ctx,
        url(),
        "Dinner at 7pm",
        "Dinner at 8pm",
        destination="copy",
        link_viewer=False,
    )
    base.settings.new_docs_link_viewer = False

    assert copied.payload["link_viewer"] is True
    assert private.payload["link_viewer"] is False
    assert "anyone with the link can view" in copied.summary.casefold()


async def test_doc_confirmation_uses_frozen_range_revision_and_verifies_readback(google_service):
    base, client, ctx, _ = google_service
    client.metadata.return_value = metadata()
    client.document_for_edit.side_effect = [
        doc("Dinner at 7pm", "rev-1"),
        doc("Dinner at 7pm", "rev-1"),
        doc("Dinner at 8pm", "rev-2"),
    ]
    edits = GoogleEditService(base)
    action = await edits.propose_doc(ctx, url(), "Dinner at 7pm", "Dinner at 8pm")
    result = await edits.commit(action, ctx)
    request, revision = client.edit_document.await_args.args[1:]
    assert revision == "rev-1"
    assert request[0]["deleteContentRange"]["range"] == {
        "startIndex": 1,
        "endIndex": 14,
        "tabId": "tab-1",
    }
    assert request[1]["insertText"]["location"] == {"index": 1, "tabId": "tab-1"}
    assert request[1]["insertText"]["text"] == "Dinner at 8pm"
    assert any("updateTextStyle" in item for item in request)
    assert "verified" in result.lower()
    journal = await base.repo.edit_result(ctx.chat.key, action.id)
    assert journal["phase"] == "verified"
    client.grant_writer.assert_not_awaited()


async def test_doc_confirmation_stale_revision_stops_before_mutation(google_service):
    base, client, ctx, _ = google_service
    client.metadata.return_value = metadata()
    client.document_for_edit.side_effect = [
        doc("Dinner at 7pm", "rev-1"),
        doc("Dinner at 6pm", "rev-2"),
    ]
    edits = GoogleEditService(base)
    action = await edits.propose_doc(ctx, url(), "Dinner at 7pm", "Dinner at 8pm")
    result = await edits.commit(action, ctx)
    assert "conflict" in result.lower() and "fresh preview" in result.lower()
    client.edit_document.assert_not_awaited()
    assert (await base.repo.edit_result(ctx.chat.key, action.id))["phase"] == "conflict"


async def test_sheet_preview_keeps_literal_prefixes_and_commit_updates_only_values(google_service):
    base, client, ctx, _ = google_service
    client.metadata.return_value = metadata(mime=SHEET)
    client.sheet.return_value = {
        "sheets": [
            {
                "properties": {
                    "sheetId": 42,
                    "title": "Day 1",
                    "gridProperties": {"rowCount": 100, "columnCount": 10},
                }
            }
        ]
    }
    client.grid_for_edit.side_effect = [
        {
            "sheets": [
                {
                    "properties": client.sheet.return_value["sheets"][0]["properties"],
                    "data": [
                        {
                            "startRow": 4,
                            "startColumn": 6,
                            "rowData": [
                                {
                                    "values": [
                                        {"userEnteredValue": {"numberValue": 7}},
                                        {"userEnteredValue": {"stringValue": "old"}},
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        {
            "sheets": [
                {
                    "properties": client.sheet.return_value["sheets"][0]["properties"],
                    "data": [
                        {
                            "startRow": 4,
                            "startColumn": 6,
                            "rowData": [
                                {
                                    "values": [
                                        {"userEnteredValue": {"numberValue": 7}},
                                        {"userEnteredValue": {"stringValue": "old"}},
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        {
            "sheets": [
                {
                    "properties": client.sheet.return_value["sheets"][0]["properties"],
                    "data": [
                        {
                            "startRow": 4,
                            "startColumn": 6,
                            "rowData": [
                                {
                                    "values": [
                                        {"userEnteredValue": {"numberValue": 7}},
                                        {"userEnteredValue": {"stringValue": "old"}},
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        {
            "sheets": [
                {
                    "properties": client.sheet.return_value["sheets"][0]["properties"],
                    "data": [
                        {
                            "startRow": 4,
                            "startColumn": 6,
                            "rowData": [
                                {
                                    "values": [
                                        {"userEnteredValue": {"stringValue": "-2 pax"}},
                                        {"userEnteredValue": {"stringValue": "+65 8123"}},
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    ]
    edits = GoogleEditService(base)
    action = await edits.propose_sheet(
        ctx,
        "https://docs.google.com/spreadsheets/d/file/edit",
        "Day 1",
        "G5:H5",
        [["-2 pax", "+65 8123"]],
    )
    assert "no revision compare-and-swap" in action.summary.lower()
    result = await edits.commit(action, ctx)
    payload = client.edit_cells.await_args.args[1]
    update = payload["requests"][0]["updateCells"]
    assert update["rows"][0]["values"][0]["userEnteredValue"] == {"stringValue": "-2 pax"}
    assert update["rows"][0]["values"][1]["userEnteredValue"] == {"stringValue": "+65 8123"}
    assert update["fields"] == "userEnteredValue"
    assert "compare-and-swap" not in result.lower()
    assert "verified" in result.lower()


async def test_copy_fallback_is_refused_until_owner_grant_qualification(google_service):
    base, client, ctx, _ = google_service
    client.metadata.return_value = metadata(editable=False)
    edits = GoogleEditService(base)
    with pytest.raises(ValueError, match="copy fallback.*qualification"):
        await edits.propose_doc(ctx, url(), "Dinner at 7pm", "Dinner at 8pm")
    client.copy_file.assert_not_awaited()


async def test_qualified_native_copy_remaps_tab_id_and_journals_destination_before_edit(
    google_service,
):
    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    client.metadata.side_effect = [metadata(), metadata(), metadata("copy-id")]
    client.copy_file.return_value = {"id": "copy-id", "mimeType": DOC}
    client.document_for_edit.side_effect = [
        doc("Dinner at 7pm", tab_id="source-tab"),
        doc("Dinner at 7pm", tab_id="source-tab"),
        doc("Dinner at 7pm", tab_id="copy-tab"),
        doc("Dinner at 8pm", revision="rev-2", tab_id="copy-tab"),
    ]
    action = await GoogleEditService(base).propose_doc(
        ctx,
        url(),
        "Dinner at 7pm",
        "Dinner at 8pm",
        destination="copy",
    )
    result = await GoogleEditService(base).commit(action, ctx)
    assert "verified" in result.lower()
    assert client.copy_file.await_args.args == ("file", "Trip — edited")
    requests = client.edit_document.await_args.args[1]
    assert requests[0]["deleteContentRange"]["range"]["tabId"] == "copy-tab"
    journal = await base.repo.edit_result(ctx.chat.key, action.id)
    assert journal["phase"] == "verified" and journal["destination_id"] == "copy-id"


@pytest.mark.parametrize("write_error", [False, True])
async def test_link_viewer_copy_is_shared_after_both_verified_edit_paths(
    google_service, write_error
):
    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    copied_meta = metadata("copy-id")
    copied_meta["ownedByMe"] = True
    copied_meta["owners"] = [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}]
    copied_meta["capabilities"]["canShare"] = True
    client.metadata.side_effect = [metadata(), metadata(), copied_meta, copied_meta]
    client.copy_file.return_value = {"id": "copy-id", "mimeType": DOC}
    client.document_for_edit.side_effect = [
        doc("Dinner at 7pm", tab_id="source-tab"),
        doc("Dinner at 7pm", tab_id="source-tab"),
        doc("Dinner at 7pm", tab_id="copy-tab"),
        doc("Dinner at 8pm", tab_id="copy-tab"),
    ]
    if write_error:
        client.edit_document.side_effect = GoogleError(
            "Google request failed; a write may have completed."
        )
    client.list_permissions.side_effect = [
        {"permissions": []},
        {"permissions": [{"type": "anyone", "role": "reader", "allowFileDiscovery": False}]},
    ]
    action = await GoogleEditService(base).propose_doc(
        ctx,
        url(),
        "Dinner at 7pm",
        "Dinner at 8pm",
        destination="copy",
        link_viewer=True,
    )

    result = await GoogleEditService(base).commit(action, ctx)

    journal = await base.repo.edit_result(ctx.chat.key, action.id)
    assert "anyone with the link can view" in result.casefold()
    assert journal["phase"] == "verified" and journal["destination_id"] == "copy-id"
    client.grant_link_viewer.assert_awaited_once_with("copy-id")


async def test_wrong_copy_mime_is_recorded_partial_with_known_destination(google_service):
    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    client.metadata.side_effect = [metadata(), metadata(), metadata("copy-id", SHEET)]
    client.copy_file.return_value = {"id": "copy-id", "mimeType": SHEET}
    client.document_for_edit.side_effect = [doc("Dinner at 7pm"), doc("Dinner at 7pm")]
    action = await GoogleEditService(base).propose_doc(
        ctx,
        url(),
        "Dinner at 7pm",
        "Dinner at 8pm",
        destination="copy",
    )
    result = await GoogleEditService(base).commit(action, ctx)
    assert "partial" in result.lower() and "copy-id" in result
    journal = await base.repo.edit_result(ctx.chat.key, action.id)
    assert journal["phase"] == "partial" and journal["destination_id"] == "copy-id"
    client.edit_document.assert_not_awaited()


async def test_unknown_copy_response_is_uncertain_and_cannot_be_replayed(google_service):
    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    client.metadata.side_effect = [metadata(), metadata()]
    client.copy_file.side_effect = GoogleError("Google request failed; a write may have completed.")
    client.document_for_edit.side_effect = [doc("Dinner at 7pm"), doc("Dinner at 7pm")]
    edits = GoogleEditService(base)
    action = await edits.propose_doc(
        ctx, url(), "Dinner at 7pm", "Dinner at 8pm", destination="copy"
    )
    result = await edits.commit(action, ctx)
    assert "uncertain" in result.lower()
    with pytest.raises(ValueError, match="handled|uncertain"):
        await edits.commit(action, ctx)
    journal = await base.repo.edit_result(ctx.chat.key, action.id)
    assert journal["phase"] == "uncertain" and journal["destination_id"] is None


async def test_docx_converts_to_native_copy_without_source_app_file_grant(
    google_service, docx_bytes
):
    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    source_meta = metadata(
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    source_meta["isAppAuthorized"] = False
    client.metadata.side_effect = [source_meta, source_meta, metadata("native-id")]
    source_package = styled_table_docx(docx_bytes)
    client.media.return_value = source_package
    client.import_docx.return_value = {"id": "native-id", "mimeType": DOC}
    client.document_for_edit.side_effect = [
        doc("Tokyo itinerary", tab_title="Imported first tab"),
        doc("Tokyo plans", revision="rev-2", tab_title="Imported first tab"),
    ]
    edits = GoogleEditService(base)
    action = await edits.propose_doc(
        ctx,
        url(),
        "Tokyo itinerary",
        "Tokyo plans",
    )
    assert action.payload["destination"]["mode"] == "docx_import"
    result = await edits.commit(action, ctx)
    assert "verified" in result.lower()
    assert client.import_docx.await_args.args[0] == "file"
    assert client.import_docx.await_args.args[2] == source_package
    requests = client.edit_document.await_args.args[1]
    assert len(requests) == 3
    assert requests[0]["deleteContentRange"]["range"] == {
        "startIndex": 1,
        "endIndex": 16,
        "tabId": "tab-1",
    }
    assert requests[1]["insertText"]["location"] == {"index": 1, "tabId": "tab-1"}
    assert requests[1]["insertText"]["text"] == "Tokyo plans"


async def test_reserved_destination_claim_prevents_concurrent_duplicate_copies(google_service):
    import asyncio
    from dataclasses import replace

    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    client.metadata.return_value = metadata()
    client.document_for_edit.return_value = doc("Dinner at 7pm")

    async def copy(*args):
        await asyncio.sleep(0.01)
        return {"id": "copy-id", "mimeType": DOC}

    client.copy_file.side_effect = copy
    edits = GoogleEditService(base)
    action = await edits.propose_doc(
        ctx, url(), "Dinner at 7pm", "Dinner at 8pm", destination="copy"
    )
    action = replace(action, kind="browser_edit_doc")
    await edits.reserve_confirmed_action(action, ctx)

    results = await asyncio.gather(
        edits.create_reserved_destination(action, ctx),
        edits.create_reserved_destination(action, ctx),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    client.copy_file.assert_awaited_once()
    journal = await base.repo.edit_result(ctx.chat.key, action.id)
    assert journal["kind"] == "browser_edit_doc" and journal["destination_id"] == "copy-id"


async def test_reserved_destination_rechecks_settings_revision_before_copy(google_service):
    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    client.metadata.return_value = metadata()
    client.document_for_edit.return_value = doc("Dinner at 7pm")
    edits = GoogleEditService(base)
    action = await edits.propose_doc(
        ctx, url(), "Dinner at 7pm", "Dinner at 8pm", destination="copy"
    )
    await edits.reserve_confirmed_action(action, ctx)
    await base.repo.configure(ctx.chat.key, False, action.payload["revision"])
    await base.repo.configure(ctx.chat.key, True, action.payload["revision"] + 1)

    with pytest.raises(ValueError, match="settings"):
        await edits.create_reserved_destination(action, ctx)
    client.copy_file.assert_not_awaited()


async def test_reserved_destination_rechecks_owner_configuration_before_copy(google_service):
    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    client.metadata.return_value = metadata()
    client.document_for_edit.return_value = doc("Dinner at 7pm")
    edits = GoogleEditService(base)
    action = await edits.propose_doc(
        ctx, url(), "Dinner at 7pm", "Dinner at 8pm", destination="copy"
    )
    await edits.reserve_confirmed_action(action, ctx)
    base.settings.owner_email = "changed@example.com"

    with pytest.raises(ValueError, match="owner configuration"):
        await edits.create_reserved_destination(action, ctx)
    client.copy_file.assert_not_awaited()


async def test_reserved_destination_rechecks_frozen_document_content(google_service):
    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    client.metadata.return_value = metadata()
    client.document_for_edit.side_effect = [
        doc("Dinner at 7pm", "rev-1"),
        doc("Dinner at 6pm", "rev-2"),
    ]
    edits = GoogleEditService(base)
    action = await edits.propose_doc(
        ctx, url(), "Dinner at 7pm", "Dinner at 8pm", destination="copy"
    )
    await edits.reserve_confirmed_action(action, ctx)

    with pytest.raises(ValueError, match="content changed"):
        await edits.create_reserved_destination(action, ctx)
    client.copy_file.assert_not_awaited()


async def test_reserved_docx_destination_rejects_unfrozen_source_bytes(google_service, docx_bytes):
    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    source_meta = metadata(
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    client.metadata.return_value = source_meta
    client.media.return_value = docx_bytes
    client.document_for_edit.return_value = doc("Tokyo itinerary")
    edits = GoogleEditService(base)
    action = await edits.propose_doc(ctx, url(), "Tokyo itinerary", "Tokyo plans")
    await edits.reserve_confirmed_action(action, ctx)

    with pytest.raises(ValueError, match="source bytes do not match"):
        await edits.create_reserved_destination(action, ctx, source_bytes=b"other package")
    client.import_docx.assert_not_awaited()


async def test_copied_sheet_resolves_destination_tab_by_title_and_new_numeric_id(google_service):
    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    source_props = {
        "sheetId": 7,
        "title": "Tokyo",
        "gridProperties": {"rowCount": 50, "columnCount": 10},
    }
    copied_props = {**source_props, "sheetId": 99}
    client.metadata.side_effect = [
        metadata(mime=SHEET),
        metadata(mime=SHEET),
        metadata("copy-id", SHEET),
    ]
    client.sheet.side_effect = [
        {"sheets": [{"properties": source_props}]},
        {"sheets": [{"properties": source_props}]},
        {"sheets": [{"properties": copied_props}]},
        {"sheets": [{"properties": copied_props}]},
        {"sheets": [{"properties": copied_props}]},
        {"sheets": [{"properties": copied_props}]},
    ]
    client.copy_file.return_value = {"id": "copy-id", "mimeType": SHEET}

    def grid(sheet_id, value):
        return {
            "sheets": [
                {
                    "properties": copied_props if sheet_id == 99 else source_props,
                    "data": [
                        {
                            "startRow": 0,
                            "startColumn": 0,
                            "rowData": [{"values": [{"userEnteredValue": {"stringValue": value}}]}],
                        }
                    ],
                }
            ]
        }

    client.grid_for_edit.side_effect = [
        grid(7, "old"),
        grid(7, "old"),
        grid(99, "old"),
        grid(99, "old"),
        grid(99, "new"),
    ]
    edits = GoogleEditService(base)
    action = await edits.propose_sheet(
        ctx,
        "https://docs.google.com/spreadsheets/d/file/edit",
        "Tokyo",
        "A1",
        [["new"]],
        destination="copy",
    )

    result = await edits.commit(action, ctx)

    update = client.edit_cells.await_args.args[1]["requests"][0]["updateCells"]
    assert update["range"]["sheetId"] == 99
    assert "verified" in result.lower()
