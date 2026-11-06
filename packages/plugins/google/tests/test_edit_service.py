import io
import zipfile
from dataclasses import replace

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
                    "namedStyles": {
                        "styles": [
                            {
                                "namedStyleType": "NORMAL_TEXT",
                                "textStyle": {
                                    "bold": True,
                                    "italic": False,
                                    "underline": False,
                                    "strikethrough": False,
                                    "smallCaps": False,
                                    "backgroundColor": {},
                                    "baselineOffset": "NONE",
                                    "weightedFontFamily": {"fontFamily": "Arial"},
                                    "fontSize": {"magnitude": 11, "unit": "PT"},
                                    "foregroundColor": {
                                        "color": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}
                                    },
                                },
                            }
                        ]
                    },
                    "body": {
                        "content": [
                            {
                                "startIndex": 1,
                                "endIndex": units + 2,
                                "paragraph": {
                                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
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
                                    ],
                                },
                            }
                        ]
                    },
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


async def test_doc_commit_conflicts_when_named_style_changes_without_text_or_raw_change(
    google_service,
):
    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    client.metadata.return_value = metadata()
    original = doc("Dinner at 7pm")
    changed = doc("Dinner at 7pm", "rev-1")
    changed["tabs"][0]["documentTab"]["namedStyles"]["styles"][0]["textStyle"][
        "weightedFontFamily"
    ] = {"fontFamily": "Courier New"}
    client.document_for_edit.side_effect = [original, changed]
    edits = GoogleEditService(base)
    action = await edits.propose_doc(
        ctx, url(), "Dinner at 7pm", "Dinner at 8pm", destination="copy"
    )

    result = await edits.commit(action, ctx)

    journal = await base.repo.edit_result(ctx.chat.key, action.id)
    assert "Conflict" in result
    assert journal["phase"] == "conflict"
    client.copy_file.assert_not_awaited()
    client.edit_document.assert_not_awaited()


async def test_link_viewer_choice_applies_only_to_frozen_new_doc_copies(google_service):
    base, client, ctx, _ = google_service
    client.metadata.return_value = metadata()
    client.document_for_edit.return_value = doc("Dinner at 7pm")
    edits = GoogleEditService(base)
    with pytest.raises(ValueError, match="Original Docs stay unchanged"):
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


async def test_edit_proposal_accepts_journaled_output_even_with_wrong_optional_human_ref(
    google_service,
):
    base, client, ctx, _ = google_service
    ctx.turn.human_urls = frozenset()
    base.settings.copy_qualified = True
    client.metadata.return_value = {
        **metadata("output-id"),
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {
            "canEdit": True,
            "canModifyContent": True,
            "canCopy": True,
            "canDownload": True,
        },
    }
    client.document_for_edit.return_value = doc("Dinner at 7pm")
    await base.repo.reserve_edit(
        ctx.chat.key,
        "prior-output",
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
    await base.repo.update_edit_result(
        ctx.chat.key, "prior-output", phase="destination_created", destination_id="output-id"
    )

    action = await GoogleEditService(base).propose_doc(
        ctx,
        url("output-id"),
        "Dinner at 7pm",
        "Dinner at 8pm",
        source_message_ref="not-a-human-message",
    )

    assert action.payload["source"]["file_id"] == "output-id"
    assert action.payload["source"]["authority"]["action_id"] == "prior-output"


async def test_external_doc_source_message_ref_is_frozen_and_rechecked_at_commit(google_service):
    from chatcore.testing import message

    base, client, ctx, _ = google_service
    ctx.turn.human_urls = frozenset()
    client.metadata.return_value = metadata()
    client.document_for_edit.return_value = doc("Dinner at 7pm")
    human = replace(
        message("Please edit " + url(), sender=ctx.user, chat=ctx.chat),
        message_id="human-doc-source",
    )
    await ctx.stores.repo.log_message(human)
    edits = GoogleEditService(base)

    action = await edits.propose_doc(
        ctx,
        url(),
        "Dinner at 7pm",
        "Dinner at 8pm",
        source_message_ref="human-doc-source",
    )

    assert action.payload["source"]["authority"] == {
        "kind": "human_message",
        "message_id": "human-doc-source",
    }
    await ctx.stores.repo.purge_user(ctx.chat, ctx.user.ref)
    with pytest.raises(ValueError, match="source must be an existing human message"):
        await edits.commit(action, ctx)
    client.edit_document.assert_not_awaited()


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


async def test_doc_verification_retries_stale_readback_without_repeating_mutation(
    google_service, monkeypatch
):
    import asyncio

    delays = []

    async def record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    base, client, ctx, _ = google_service
    client.metadata.return_value = metadata()
    client.document_for_edit.side_effect = [
        doc("Dinner at 7pm"),
        doc("Dinner at 7pm"),
        doc("Dinner at 7pm"),
        doc("Dinner at 8pm", "rev-2"),
    ]
    edits = GoogleEditService(base)
    action = await edits.propose_doc(ctx, url(), "Dinner at 7pm", "Dinner at 8pm")

    result = await edits.commit(action, ctx)

    assert "verified" in result.lower()
    assert client.edit_document.await_count == 1
    assert client.document_for_edit.await_count == 4
    assert delays == [0.1]


async def test_doc_verification_persists_text_mismatch_after_bounded_reads(
    google_service, monkeypatch
):
    import asyncio

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    base, client, ctx, _ = google_service
    client.metadata.return_value = metadata()
    client.document_for_edit.side_effect = [
        doc("Dinner at 7pm"),
        doc("Dinner at 7pm"),
        doc("Dinner at 7pm"),
        doc("Dinner at 6pm", "rev-2"),
        doc("Dinner at 6pm", "rev-3"),
    ]
    edits = GoogleEditService(base)
    action = await edits.propose_doc(ctx, url(), "Dinner at 7pm", "Dinner at 8pm")

    result = await edits.commit(action, ctx)

    journal = await base.repo.edit_result(ctx.chat.key, action.id)
    assert "Uncertain" in result and url("file") in result
    assert "no link-access change was attempted" in result.casefold()
    assert journal["phase"] == "uncertain"
    assert journal["error_category"] == "text_mismatch"
    assert client.edit_document.await_count == 1
    assert client.document_for_edit.await_count == 5


async def test_doc_verification_retries_read_error_then_exact_readback(google_service, monkeypatch):
    import asyncio

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    base, client, ctx, _ = google_service
    client.metadata.return_value = metadata()
    client.document_for_edit.side_effect = [
        doc("Dinner at 7pm"),
        doc("Dinner at 7pm"),
        GoogleError("temporary read failure"),
        doc("Dinner at 8pm", "rev-2"),
    ]
    edits = GoogleEditService(base)
    action = await edits.propose_doc(ctx, url(), "Dinner at 7pm", "Dinner at 8pm")

    result = await edits.commit(action, ctx)

    assert "verified" in result.lower()
    assert client.edit_document.await_count == 1
    assert client.document_for_edit.await_count == 4


async def test_doc_verification_ignores_unrelated_inline_image_and_keeps_target_checks(
    google_service,
):
    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    client.metadata.side_effect = [metadata(), metadata(), metadata("copy-id"), metadata("copy-id")]
    client.copy_file.return_value = {"id": "copy-id", "mimeType": DOC}
    client.edit_document.side_effect = GoogleError("synthetic response loss")
    with_image = doc("Dinner at 8pm", "rev-2", tab_id="copy-tab")
    content = with_image["tabs"][0]["documentTab"]["body"]["content"][0]
    content["endIndex"] += 1
    content["paragraph"]["elements"].append(
        {"startIndex": 15, "endIndex": 16, "inlineObjectElement": {"inlineObjectId": "image"}}
    )
    client.document_for_edit.side_effect = [
        doc("Dinner at 7pm", tab_id="source-tab"),
        doc("Dinner at 7pm", tab_id="source-tab"),
        doc("Dinner at 7pm", tab_id="copy-tab"),
        with_image,
    ]
    edits = GoogleEditService(base)
    action = await edits.propose_doc(
        ctx, url(), "Dinner at 7pm", "Dinner at 8pm", destination="copy"
    )

    result = await edits.commit(action, ctx)

    assert "verified" in result.lower()
    assert client.edit_document.await_count == 1


async def test_doc_verification_rejects_style_mismatch_after_bounded_reads(
    google_service, monkeypatch
):
    import asyncio

    async def no_sleep(_delay):
        return None

    base, client, ctx, _ = google_service
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    base.settings.copy_qualified = True
    client.metadata.side_effect = [
        metadata(),
        metadata(),
        metadata("copy-id"),
        metadata("copy-id"),
    ]
    client.copy_file.return_value = {"id": "copy-id", "mimeType": DOC}
    wrong_style = doc("Dinner at 8pm", "rev-2", tab_id="copy-tab")
    wrong_style["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0][
        "textRun"
    ]["textStyle"] = {"italic": True}
    client.document_for_edit.side_effect = [
        doc("Dinner at 7pm", tab_id="source-tab"),
        doc("Dinner at 7pm", tab_id="source-tab"),
        doc("Dinner at 7pm", tab_id="copy-tab"),
        wrong_style,
        wrong_style,
        wrong_style,
    ]
    edits = GoogleEditService(base)
    action = await edits.propose_doc(
        ctx,
        url(),
        "Dinner at 7pm",
        "Dinner at 8pm",
        destination="copy",
        link_access="viewer",
    )

    result = await edits.commit(action, ctx)

    journal = await base.repo.edit_result(ctx.chat.key, action.id)
    assert "Partial" in result and "text was saved and verified" in result
    assert "Formatting preservation could not be verified" in result
    assert "Do not repeat the edit" in result
    assert "Sharing and current-itinerary advancement were not performed" in result
    assert journal["phase"] == "partial"
    assert journal["error_category"] == "formatting_mismatch"
    assert client.edit_document.await_count == 1
    client.grant_link_viewer.assert_not_awaited()


async def test_doc_verification_rejects_missing_target_style_coverage(google_service, monkeypatch):
    import asyncio

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    base, client, ctx, _ = google_service
    client.metadata.return_value = metadata()
    missing_coverage = doc("Dinner at 8pm", "rev-2")
    missing_coverage["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0][
        "startIndex"
    ] = 2
    client.document_for_edit.side_effect = [
        doc("Dinner at 7pm"),
        doc("Dinner at 7pm"),
        missing_coverage,
        missing_coverage,
        missing_coverage,
    ]
    edits = GoogleEditService(base)
    action = await edits.propose_doc(ctx, url(), "Dinner at 7pm", "Dinner at 8pm")

    result = await edits.commit(action, ctx)

    journal = await base.repo.edit_result(ctx.chat.key, action.id)
    assert "Partial" in result and "text was saved and verified" in result
    assert journal["phase"] == "partial"
    assert journal["error_category"] == "formatting_unverified"
    assert client.edit_document.await_count == 1


async def test_table_inheritance_yields_text_verified_formatting_partial(google_service):
    base, client, ctx, _ = google_service
    client.metadata.return_value = metadata()

    def table_doc(text, revision):
        result = doc("unused", revision)
        units = len(text.encode("utf-16-le")) // 2
        result["tabs"][0]["documentTab"]["body"]["content"] = [
            {
                "startIndex": 1,
                "endIndex": units + 7,
                "table": {
                    "tableRows": [
                        {
                            "tableCells": [
                                {
                                    "content": [
                                        {
                                            "startIndex": 2,
                                            "endIndex": units + 3,
                                            "paragraph": {
                                                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                                "elements": [
                                                    {
                                                        "startIndex": 2,
                                                        "endIndex": units + 2,
                                                        "textRun": {
                                                            "content": text,
                                                            "textStyle": {},
                                                        },
                                                    },
                                                    {
                                                        "startIndex": units + 2,
                                                        "endIndex": units + 3,
                                                        "textRun": {
                                                            "content": "\n",
                                                            "textStyle": {},
                                                        },
                                                    },
                                                ],
                                            },
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                },
            }
        ]
        return result

    client.document_for_edit.side_effect = [
        table_doc("Dinner at 7pm", "rev-1"),
        table_doc("Dinner at 7pm", "rev-1"),
        table_doc("Dinner at 8pm", "rev-2"),
        table_doc("Dinner at 8pm", "rev-2"),
        table_doc("Dinner at 8pm", "rev-2"),
    ]
    edits = GoogleEditService(base)
    action = await edits.propose_doc(ctx, url(), "Dinner at 7pm", "Dinner at 8pm")

    result = await edits.commit(action, ctx)

    journal = await base.repo.edit_result(ctx.chat.key, action.id)
    assert "text was saved and verified" in result
    assert "Formatting preservation could not be verified" in result
    assert "Do not repeat the edit" in result
    assert journal["phase"] == "partial"
    assert journal["error_category"] == "formatting_unverified"
    assert client.edit_document.await_count == 1
    assert client.document_for_edit.await_count == 5


async def test_doc_verification_stops_when_read_exceeds_aggregate_deadline():
    from chatcore_google.doc_verification import verify_doc_readback

    current_time = 0.0

    class SlowRead:
        async def document_for_edit(self, _file_id):
            nonlocal current_time
            current_time = 5.1
            return doc("Expected\n")

    result = await verify_doc_readback(
        SlowRead(),
        "file-id",
        "Expected\n",
        max_chars=100,
        clock=lambda: current_time,
    )

    assert result.verified is False
    assert result.category == "readback_deadline"
    assert result.attempts == 1


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


async def test_link_editor_copy_is_shared_only_after_strict_content_verification(google_service):
    base, client, ctx, _ = google_service
    base.settings.copy_qualified = True
    copied_meta = metadata("copy-id")
    copied_meta.update(
        ownedByMe=True,
        isAppAuthorized=True,
        owners=[{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
    )
    copied_meta["capabilities"]["canShare"] = True
    client.metadata.side_effect = [metadata(), metadata(), copied_meta, copied_meta]
    client.copy_file.return_value = {"id": "copy-id", "mimeType": DOC}
    client.document_for_edit.side_effect = [
        doc("Dinner at 7pm", tab_id="source-tab"),
        doc("Dinner at 7pm", tab_id="source-tab"),
        doc("Dinner at 7pm", tab_id="copy-tab"),
        doc("Dinner at 8pm", tab_id="copy-tab"),
    ]
    client.list_permissions.side_effect = [
        {"permissions": []},
        {
            "permissions": [
                {
                    "id": "public",
                    "type": "anyone",
                    "role": "writer",
                    "allowFileDiscovery": False,
                }
            ]
        },
    ]
    action = await GoogleEditService(base).propose_doc(
        ctx,
        url(),
        "Dinner at 7pm",
        "Dinner at 8pm",
        destination="copy",
        link_access="editor",
    )

    result = await GoogleEditService(base).commit(action, ctx)

    journal = await base.repo.edit_result(ctx.chat.key, action.id)
    assert "anyone with the link can edit" in result.casefold()
    assert journal["phase"] == "verified" and journal["destination_id"] == "copy-id"
    client.grant_link_editor.assert_awaited_once_with("copy-id")


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


async def test_doc_verification_accepts_equivalent_normal_text_inheritance(monkeypatch):
    import asyncio

    from chatcore_google.doc_verification import verify_doc_readback
    from chatcore_google.edit_snapshot import find_doc_match, flatten_doc_snapshot

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    expected_style = {
        "bold": True,
        "italic": False,
        "underline": False,
        "strikethrough": False,
        "smallCaps": False,
        "backgroundColor": {},
        "baselineOffset": "NONE",
        "weightedFontFamily": {"fontFamily": "Arial"},
        "fontSize": {"magnitude": 11, "unit": "PT"},
        "foregroundColor": {"color": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}},
    }
    before = doc("Dinner at 7pm")
    before["tabs"][0]["documentTab"]["namedStyles"] = {
        "styles": [{"namedStyleType": "NORMAL_TEXT", "textStyle": expected_style}]
    }
    before["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["paragraphStyle"] = {
        "namedStyleType": "NORMAL_TEXT"
    }
    before["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]["textRun"][
        "textStyle"
    ] = expected_style
    after = doc("Dinner at 8pm", "rev-2")
    after["tabs"][0]["documentTab"]["namedStyles"] = before["tabs"][0]["documentTab"]["namedStyles"]
    after["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["paragraphStyle"] = {
        "namedStyleType": "NORMAL_TEXT"
    }
    after["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]["textRun"][
        "textStyle"
    ] = {}

    class Client:
        async def document_for_edit(self, _file_id):
            return after

    before_snapshot = flatten_doc_snapshot(before, tab_id="tab-1", max_chars=100)
    match = find_doc_match(before_snapshot, "Dinner at 7pm")
    result = await verify_doc_readback(
        Client(),
        "file",
        "Dinner at 8pm\n",
        max_chars=100,
        tab_id="tab-1",
        match=match,
        replacement="Dinner at 8pm",
    )

    assert result.verified is True


@pytest.mark.parametrize(
    "changed_style",
    [
        {"bold": False},
        {"weightedFontFamily": {"fontFamily": "Times New Roman"}},
        {"weightedFontFamily": {"fontFamily": "Arial", "weight": 600}},
        {"fontSize": {"magnitude": 12, "unit": "PT"}},
        {"foregroundColor": {"color": {"rgbColor": {"red": 1, "green": 0, "blue": 0}}}},
        {"link": {"url": "https://example.com"}},
    ],
)
async def test_doc_verification_detects_changed_effective_style(changed_style):
    from chatcore_google.doc_verification import verify_doc_readback
    from chatcore_google.edit_snapshot import find_doc_match, flatten_doc_snapshot

    before = doc("Dinner at 7pm")
    after = doc("Dinner at 8pm", "rev-2")
    after["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]["textRun"][
        "textStyle"
    ] = changed_style
    match = find_doc_match(
        flatten_doc_snapshot(before, tab_id="tab-1", max_chars=100), "Dinner at 7pm"
    )

    class Client:
        async def document_for_edit(self, _file_id):
            return after

    result = await verify_doc_readback(
        Client(),
        "file",
        "Dinner at 8pm\n",
        max_chars=100,
        tab_id="tab-1",
        match=match,
        replacement="Dinner at 8pm",
    )

    assert result.verified is False
    assert result.text_verified is True
    assert result.category == "style_mismatch"


async def test_later_text_mismatch_clears_earlier_exact_text_evidence(monkeypatch):
    import asyncio

    from chatcore_google.doc_verification import verify_doc_readback
    from chatcore_google.edit_snapshot import find_doc_match, flatten_doc_snapshot

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    before = doc("Dinner at 7pm")
    first = doc("Dinner at 8pm", "rev-2")
    first["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]["textRun"][
        "textStyle"
    ] = {"italic": True}
    mismatch = doc("Dinner at 6pm", "rev-3")

    class Client:
        def __init__(self):
            self.results = iter([first, mismatch, mismatch])

        async def document_for_edit(self, _file_id):
            return next(self.results)

    match = find_doc_match(
        flatten_doc_snapshot(before, tab_id="tab-1", max_chars=100), "Dinner at 7pm"
    )
    result = await verify_doc_readback(
        Client(),
        "file",
        "Dinner at 8pm\n",
        max_chars=100,
        tab_id="tab-1",
        match=match,
        replacement="Dinner at 8pm",
    )

    assert result.category == "text_mismatch"
    assert result.text_verified is False
    assert result.formatting_verified is False


async def test_later_read_error_clears_earlier_exact_text_evidence(monkeypatch):
    import asyncio

    from chatcore_google.doc_verification import verify_doc_readback
    from chatcore_google.edit_snapshot import find_doc_match, flatten_doc_snapshot

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    before = doc("Dinner at 7pm")
    exact_text = doc("Dinner at 8pm", "rev-2")
    exact_text["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0][
        "textRun"
    ]["textStyle"] = {"italic": True}

    class Client:
        def __init__(self):
            self.calls = 0

        async def document_for_edit(self, _file_id):
            self.calls += 1
            if self.calls == 1:
                return exact_text
            raise GoogleError("read failed")

    match = find_doc_match(
        flatten_doc_snapshot(before, tab_id="tab-1", max_chars=100), "Dinner at 7pm"
    )
    client = Client()
    result = await verify_doc_readback(
        client,
        "file",
        "Dinner at 8pm\n",
        max_chars=100,
        tab_id="tab-1",
        match=match,
        replacement="Dinner at 8pm",
    )

    assert client.calls == 3
    assert result.category == "read_error"
    assert result.text_verified is False
    assert result.formatting_verified is False


async def test_deadline_after_exact_text_with_style_warning_clears_current_text_claim():
    from chatcore_google.doc_verification import verify_doc_readback
    from chatcore_google.edit_snapshot import find_doc_match, flatten_doc_snapshot

    now = 0.0
    before = doc("Dinner at 7pm")
    after = doc("Dinner at 8pm", "rev-2")
    after["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]["textRun"][
        "textStyle"
    ] = {"italic": True}
    match = find_doc_match(
        flatten_doc_snapshot(before, tab_id="tab-1", max_chars=100), "Dinner at 7pm"
    )

    class Client:
        calls = 0

        async def document_for_edit(self, _file_id):
            self.calls += 1
            return after

    async def expire_deadline(_delay):
        nonlocal now
        now = 6.0

    client = Client()
    result = await verify_doc_readback(
        client,
        "file",
        "Dinner at 8pm\n",
        max_chars=100,
        tab_id="tab-1",
        match=match,
        replacement="Dinner at 8pm",
        clock=lambda: now,
        sleep=expire_deadline,
    )

    assert client.calls == 1
    assert result.category == "readback_deadline"
    assert result.text_verified is False
    assert result.formatting_verified is False
