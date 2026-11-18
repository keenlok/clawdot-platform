import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from chatcore_google.rich_doc import (
    _body_content,
    compile_doc,
    fill_tables,
    preview_doc,
    verify_rich_doc,
)
from chatcore_google.settings import GoogleSettings


def test_compile_doc_builds_styles_marks_lists_and_table_placeholders():
    blocks = [
        {"type": "title", "text": "Kyoto"},
        {"type": "heading", "level": 1, "text": "Day 1"},
        {
            "type": "paragraph",
            "spans": [
                {"text": "Meet "},
                {
                    "text": "station",
                    "bold": True,
                    "italic": True,
                    "underline": True,
                    "code": True,
                    "link": "https://maps.example",
                },
            ],
        },
        {"type": "bullets", "items": ["A", "B"]},
        {"type": "numbered", "items": ["C"]},
        {"type": "divider"},
        {"type": "table", "header": ["Time", "Place"], "rows": [["09:00", "Inari"]]},
    ]
    plan = compile_doc(blocks, GoogleSettings())
    assert plan.body_text == "Kyoto\nDay 1\nMeet station\nA\nB\nC\n\n"
    assert plan.batch_a[0] == {"insertText": {"location": {"index": 1}, "text": plan.body_text}}
    assert any("updateTextStyle" in request for request in plan.batch_a)
    marked = [
        request["updateTextStyle"] for request in plan.batch_a if "updateTextStyle" in request
    ]
    assert marked[0]["range"] == {"startIndex": 18, "endIndex": 25}
    bullets = [
        request["createParagraphBullets"]
        for request in plan.batch_a
        if "createParagraphBullets" in request
    ]
    assert bullets == [
        {"range": {"startIndex": 26, "endIndex": 30}, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"},
        {
            "range": {"startIndex": 30, "endIndex": 32},
            "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN",
        },
    ]
    divider = [
        request["updateParagraphStyle"]
        for request in plan.batch_a
        if "updateParagraphStyle" in request
        and request["updateParagraphStyle"]["fields"] == "borderBottom"
    ]
    assert divider[0]["range"] == {"startIndex": 32, "endIndex": 33}
    assert divider[0] == {
        "range": {"startIndex": 32, "endIndex": 33},
        "paragraphStyle": {
            "borderBottom": {
                "color": {"color": {"rgbColor": {"red": 0.6, "green": 0.6, "blue": 0.6}}},
                "width": {"magnitude": 1, "unit": "PT"},
                "padding": {"magnitude": 1, "unit": "PT"},
                "dashStyle": "SOLID",
            }
        },
        "fields": "borderBottom",
    }
    assert plan.tables[0].placeholder_index == 33
    assert preview_doc(blocks).startswith("Kyoto\nDay 1\nMeet station")


def test_compile_doc_all_mark_field_order_is_stable():
    plan = compile_doc(
        [
            {
                "type": "paragraph",
                "spans": [
                    {
                        "text": "marked",
                        "bold": True,
                        "italic": True,
                        "underline": True,
                        "code": True,
                        "link": "https://maps.example",
                    }
                ],
            }
        ],
        GoogleSettings(),
    )
    request = next(
        request["updateTextStyle"] for request in plan.batch_a if "updateTextStyle" in request
    )
    assert request["fields"] == "bold,italic,underline,weightedFontFamily,link"


def test_compile_doc_create_path_matches_prechange_golden():
    golden = json.loads(
        (Path(__file__).parent / "fixtures" / "compile_doc_create_golden.json").read_text()
    )
    plan = compile_doc(golden["blocks"], GoogleSettings())
    actual = {
        "blocks": golden["blocks"],
        "body_text": plan.body_text,
        "batch_a": plan.batch_a,
        "batch_b": plan.batch_b,
        "tables": [
            {"placeholder_index": t.placeholder_index, "header": t.header, "rows": t.rows}
            for t in plan.tables
        ],
        "expected": plan.expected,
        "bullet_texts": plan.bullet_texts,
        "numbered_texts": plan.numbered_texts,
        "expected_marks": plan.expected_marks,
    }
    for request in actual["batch_a"]:
        operation = next(iter(request.values()))
        if isinstance(operation.get("fields"), str) and "," in operation["fields"]:
            operation["fields"] = ",".join(sorted(operation["fields"].split(",")))
    for request in golden["batch_a"]:
        operation = next(iter(request.values()))
        if isinstance(operation.get("fields"), str) and "," in operation["fields"]:
            operation["fields"] = ",".join(sorted(operation["fields"].split(",")))
    assert json.loads(json.dumps(actual)) == golden


def test_compile_doc_offsets_newline_tabs_and_utf16_indices():
    blocks = [
        {"type": "paragraph", "spans": [{"text": "😀"}, {"text": " linked", "bold": True}]},
        {"type": "table", "header": ["A"], "rows": [["𠀋"]]},
        {"type": "table", "header": ["B"], "rows": [["two"]]},
    ]
    plan = compile_doc(
        blocks, GoogleSettings(), base_index=12, leading_newline=True, tab_id="tab-2"
    )
    assert plan.body_text.startswith("\n😀 linked")
    assert plan.batch_a[0]["insertText"]["location"] == {"index": 12, "tabId": "tab-2"}
    assert [table.placeholder_index for table in plan.tables] == [23, 24]
    for request in (*plan.batch_a, *plan.batch_b):
        operation = next(iter(request.values()))
        for key in ("location", "range"):
            if key in operation:
                assert operation[key]["tabId"] == "tab-2"


def test_doc_body_selection_requires_tab_id_for_multiple_tabs():
    first = [{"startIndex": 1, "endIndex": 2, "paragraph": {"elements": []}}]
    second = [{"startIndex": 1, "endIndex": 2, "paragraph": {"elements": []}}]
    document = {
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-a"},
                "documentTab": {"body": {"content": first}},
            },
            {
                "tabProperties": {"tabId": "tab-b"},
                "documentTab": {"body": {"content": second}},
            },
        ]
    }
    assert _body_content(document) is None
    assert _body_content(document, tab_id="tab-b") is second


def test_table_placeholders_use_utf16_offsets_between_tables():
    blocks = [
        {"type": "paragraph", "text": "🙂 𠀋"},
        {"type": "table", "header": ["A"], "rows": []},
        {"type": "paragraph", "text": "after 😀"},
        {"type": "table", "header": ["B"], "rows": []},
    ]
    plan = compile_doc(blocks, GoogleSettings())
    table_requests = [r["insertTable"] for r in plan.batch_b]
    assert [r["location"]["index"] for r in table_requests] == sorted(
        [table.placeholder_index for table in plan.tables], reverse=True
    )
    assert [table.placeholder_index for table in plan.tables] == [7, 17]


def test_compile_doc_errors_name_block_location():
    with pytest.raises(ValueError, match=r"Block 1 \(paragraph\)"):
        compile_doc([{"type": "paragraph", "text": "bad\ntext"}], GoogleSettings())


@pytest.mark.parametrize("value", ["bad\x01text", "bad\u2028text"])
def test_compile_doc_rejects_control_characters(value):
    with pytest.raises(ValueError, match=r"Block 1 \(paragraph\)"):
        compile_doc([{"type": "paragraph", "text": value}], GoogleSettings())


def test_verify_rich_doc_accepts_matching_paragraph_and_table(rich_doc_response):
    blocks = [
        {"type": "heading", "level": 2, "text": "Plan"},
        {"type": "table", "header": ["A"], "rows": [["B"]]},
    ]
    plan = compile_doc(blocks, GoogleSettings())
    doc = rich_doc_response(plan, table_padding=True)
    assert verify_rich_doc(doc, plan) == (True, [])
    changed_cell = copy.deepcopy(doc)
    table = next(
        item["table"]
        for item in changed_cell["tabs"][0]["documentTab"]["body"]["content"]
        if "table" in item
    )
    table["tableRows"][1]["tableCells"][0]["content"][0]["paragraph"]["elements"][0]["textRun"][
        "content"
    ] = "changed\n"
    assert verify_rich_doc(changed_cell, plan) == (False, [])
    missing = copy.deepcopy(doc)
    missing["tabs"][0]["documentTab"]["body"]["content"].pop(1)
    assert verify_rich_doc(missing, plan) == (False, [])
    extra = copy.deepcopy(doc)
    extra["tabs"][0]["documentTab"]["body"]["content"].insert(
        -1,
        {"paragraph": {"elements": [{"textRun": {"content": "extra\n"}}]}},
    )
    assert verify_rich_doc(extra, plan) == (False, [])
    doc["tabs"][0]["documentTab"]["body"]["content"][1]["paragraph"]["paragraphStyle"][
        "namedStyleType"
    ] = "NORMAL_TEXT"
    assert verify_rich_doc(doc, plan) == (True, ["heading style"])


def test_realistic_docs_get_readback_verifies_with_section_break_and_inline_mode(rich_doc_response):
    blocks = [{"type": "heading", "level": 2, "text": "Plan"}]
    plan = compile_doc(blocks, GoogleSettings())
    document = rich_doc_response(plan)
    assert document["documentId"]
    assert document["suggestionsViewMode"] == "SUGGESTIONS_INLINE"
    content = document["tabs"][0]["documentTab"]["body"]["content"]
    assert "sectionBreak" in content[0]
    assert verify_rich_doc(document, plan) == (True, [])


def test_verify_rich_doc_rejects_non_text_paragraph_elements_and_bad_cells(rich_doc_response):
    plan = compile_doc(
        [
            {"type": "paragraph", "text": "safe"},
            {"type": "table", "header": ["A"], "rows": [["B"]]},
        ],
        GoogleSettings(),
    )
    document = rich_doc_response(plan)
    content = document["tabs"][0]["documentTab"]["body"]["content"]
    paragraph = next(item["paragraph"] for item in content if "paragraph" in item)
    paragraph["elements"].append({"inlineObjectElement": {"inlineObjectId": "obj"}})
    assert verify_rich_doc(document, plan) == (False, [])

    document = rich_doc_response(plan)
    table = next(
        item["table"]
        for item in document["tabs"][0]["documentTab"]["body"]["content"]
        if "table" in item
    )
    table["tableRows"][1]["tableCells"][0]["content"].append(
        {"paragraph": {"elements": [{"textRun": {"content": "extra\n"}}]}}
    )
    assert verify_rich_doc(document, plan) == (False, [])

    document = rich_doc_response(plan)
    table = next(
        item["table"]
        for item in document["tabs"][0]["documentTab"]["body"]["content"]
        if "table" in item
    )
    table["tableRows"][0]["tableCells"].append(table["tableRows"][0]["tableCells"][0])
    assert verify_rich_doc(document, plan) == (False, [])


@pytest.mark.parametrize(
    "blocks",
    [
        [{"type": "table", "header": ["A"], "rows": []}, {"type": "divider"}],
        [{"type": "divider"}, {"type": "table", "header": ["A"], "rows": []}],
        [{"type": "table", "header": ["A"], "rows": []}],
        [
            {"type": "table", "header": ["A"], "rows": []},
            {"type": "table", "header": ["B"], "rows": []},
        ],
    ],
)
def test_padding_is_only_one_unstyled_empty_paragraph_around_each_table(blocks, rich_doc_response):
    plan = compile_doc(blocks, GoogleSettings())
    document = rich_doc_response(plan, table_padding=True)
    assert verify_rich_doc(document, plan) == (True, [])
    content = document["tabs"][0]["documentTab"]["body"]["content"]
    planned_divider = next(
        (
            item["paragraph"]
            for item in content
            if "paragraph" in item
            and item["paragraph"].get("paragraphStyle", {}).get("borderBottom")
        ),
        None,
    )
    if planned_divider is not None:
        planned_divider["paragraphStyle"].pop("borderBottom")
        assert verify_rich_doc(document, plan) == (True, ["divider border"])


async def test_fill_tables_uses_readback_indices_backwards_and_bolds_header(rich_doc_response):
    blocks = [{"type": "table", "header": ["A", "B"], "rows": [["x", "y"]]}]
    plan = compile_doc(blocks, GoogleSettings())
    client = AsyncMock()
    document = rich_doc_response(plan, empty_tables=True)
    document["revisionId"] = "rev"
    client.document_for_edit.return_value = document
    actual_starts = [
        cell["content"][0]["paragraph"]["elements"][0]["startIndex"]
        for row in document["tabs"][0]["documentTab"]["body"]["content"][1]["table"]["tableRows"]
        for cell in row["tableCells"]
    ]
    await fill_tables(client, "doc", plan)
    requests = client.edit_document.await_args.args[1]
    assert [r["insertText"]["location"]["index"] for r in requests if "insertText" in r] == [
        *sorted(actual_starts, reverse=True),
    ]
    assert [r["updateTextStyle"]["textStyle"] for r in requests if "updateTextStyle" in r] == [
        {"bold": True},
        {"bold": True},
    ]
    assert client.edit_document.await_args.args == ("doc", requests, "rev")


@pytest.mark.parametrize(
    "malformation,fragment", [("missing", "number of tables"), ("shape", "table shape")]
)
async def test_fill_tables_refuses_unexpected_readback_shape(
    malformation, fragment, rich_doc_response
):
    plan = compile_doc([{"type": "table", "header": ["A"], "rows": []}], GoogleSettings())
    document = rich_doc_response(plan, empty_tables=True)
    content = document["tabs"][0]["documentTab"]["body"]["content"]
    if malformation == "missing":
        content[:] = [content[0]]
    else:
        content[1]["table"]["tableRows"].clear()
    client = AsyncMock()
    client.document_for_edit.return_value = document
    with pytest.raises(ValueError, match=fragment):
        await fill_tables(client, "doc", plan)
    client.edit_document.assert_not_awaited()


@pytest.mark.parametrize(
    "blocks,fragment",
    [
        ([{"type": "paragraph", "text": "x", "extra": 1}], "Block 1"),
        ([{"type": "paragraph", "text": "x", "spans": [{"text": "y"}]}], "Block 1"),
        ([{"type": "heading", "level": 4, "text": "x"}], "Block 1"),
        (
            [{"type": "paragraph", "spans": [{"text": "x", "link": "javascript:alert(1)"}]}],
            "Block 1",
        ),
        ([{"type": "table", "header": ["A", "B"], "rows": [["x"]]}], "row 1"),
    ],
)
def test_compile_doc_rejects_invalid_shapes_with_location(blocks, fragment):
    with pytest.raises(ValueError, match=fragment):
        compile_doc(blocks, GoogleSettings())


@pytest.mark.parametrize(
    "url", ["javascript:bad", "data:text/plain,x", "ftp://host/x", "https:///missing"]
)
def test_compile_doc_rejects_non_http_or_hostless_links(url):
    with pytest.raises(ValueError, match=r"Block 1 \(paragraph\), span 1"):
        compile_doc(
            [{"type": "paragraph", "spans": [{"text": "link", "link": url}]}],
            GoogleSettings(),
        )


def test_compile_doc_enforces_block_item_table_and_text_limits():
    settings = GoogleSettings().model_copy(update={"max_chars": 1000})
    too_many = [({"type": "paragraph", "text": "x"}) for _ in range(201)]
    with pytest.raises(ValueError, match="1–200"):
        compile_doc(too_many, settings)
    with pytest.raises(ValueError, match="at most 10 tables"):
        compile_doc([{"type": "table", "header": ["A"], "rows": []}] * 11, settings)
    with pytest.raises(ValueError, match="1–100 items"):
        compile_doc([{"type": "bullets", "items": ["x"] * 101}], settings)
    with pytest.raises(ValueError, match="1–100 formatted spans"):
        compile_doc([{"type": "paragraph", "spans": [{"text": "x"}] * 101}], settings)
    with pytest.raises(ValueError, match="at most 50 rows"):
        compile_doc([{"type": "table", "header": ["A"], "rows": [["x"]] * 51}], settings)
    with pytest.raises(ValueError, match="1–8 columns"):
        compile_doc([{"type": "table", "header": ["x"] * 9, "rows": []}], settings)
    with pytest.raises(ValueError, match="GOOGLE_MAX_CHARS"):
        compile_doc([{"type": "paragraph", "text": "x" * 1001}], settings)
    cell_settings = GoogleSettings().model_copy(update={"max_cell_chars": 100})
    with pytest.raises(ValueError, match=r'Block 1 \(table\), row 1, column "A"'):
        compile_doc([{"type": "table", "header": ["A"], "rows": [["x" * 101]]}], cell_settings)
    output_settings = GoogleSettings().model_copy(update={"output_max_chars": 5000})
    with pytest.raises(ValueError, match="GOOGLE_OUTPUT_MAX_CHARS"):
        compile_doc([{"type": "paragraph", "text": "x" * 6000}], output_settings)


async def _render_with_fake(api, plan):
    """Apply a plan's batches through the request-driven Docs fake and return its read-back."""
    client = api.wire(AsyncMock())
    path = f"/v1/documents/{api.document_id}:batchUpdate"
    await api.request("POST", "docs.googleapis.com", path, json={"requests": list(plan.batch_a)})
    if plan.batch_b:
        await api.request(
            "POST", "docs.googleapis.com", path, json={"requests": list(plan.batch_b)}
        )
    await fill_tables(client, api.document_id, plan)
    return api._document()


def _body(document):
    return document["tabs"][0]["documentTab"]["body"]["content"]


def _paragraph(document, text):
    return next(
        item["paragraph"]
        for item in _body(document)
        if "paragraph" in item
        and "".join(e["textRun"]["content"] for e in item["paragraph"]["elements"]) == text + "\n"
    )


MARKS_AFTER_TABLES = [
    {"type": "table", "header": ["H1"], "rows": [["a"]]},
    {"type": "table", "header": ["H2"], "rows": [["b"]]},
    {"type": "paragraph", "text": "One"},
    {"type": "paragraph", "spans": [{"text": "Two", "bold": True}]},
    {"type": "paragraph", "spans": [{"text": "Map", "link": "https://maps.example"}]},
]


async def test_marks_after_multiple_tables_verify_against_the_right_paragraph(
    request_driven_rich_doc_api,
):
    plan = compile_doc(MARKS_AFTER_TABLES, GoogleSettings())
    document = await _render_with_fake(request_driven_rich_doc_api, plan)
    assert verify_rich_doc(document, plan) == (True, [])
    moved = copy.deepcopy(document)
    for element in _paragraph(moved, "Two")["elements"]:
        element["textRun"].pop("textStyle", None)
    for element in _paragraph(moved, "One")["elements"]:
        element["textRun"]["textStyle"] = {"bold": True}
    assert verify_rich_doc(moved, plan) == (True, ["bold/italic/underline"])
    unlinked = copy.deepcopy(document)
    for element in _paragraph(unlinked, "Map")["elements"]:
        element["textRun"].pop("textStyle", None)
    assert verify_rich_doc(unlinked, plan) == (True, ["links"])


async def test_non_bmp_text_and_multiple_tables_round_trip_through_docs_fake(
    request_driven_rich_doc_api,
):
    blocks = [
        {"type": "heading", "level": 1, "text": "Day 1 🗾"},
        {"type": "table", "header": ["Time 🕘", "Place"], "rows": [["09:00", "𠀋 shrine"]]},
        {"type": "paragraph", "spans": [{"text": "Then 🍜 "}, {"text": "lunch", "bold": True}]},
        {"type": "table", "header": ["Item"], "rows": [["🎫 pass"], [None]]},
        {"type": "bullets", "items": ["𠀋 one", "two"]},
    ]
    plan = compile_doc(blocks, GoogleSettings())
    document = await _render_with_fake(request_driven_rich_doc_api, plan)
    assert verify_rich_doc(document, plan) == (True, [])
    tables = [item["table"] for item in _body(document) if "table" in item]
    assert [
        [
            [cell["content"][0]["paragraph"]["elements"][0]["textRun"]["content"] for cell in r]
            for r in [row["tableCells"] for row in table["tableRows"]]
        ]
        for table in tables
    ] == [
        [["Time 🕘\n", "Place\n"], ["09:00\n", "𠀋 shrine\n"]],
        [["Item\n"], ["🎫 pass\n"], ["\n"]],
    ]


@pytest.mark.parametrize(
    "field",
    [
        "suggestedInsertionIds",
        "suggestedDeletionIds",
        "suggestedTextStyleChanges",
        "suggestedParagraphStyleChanges",
    ],
)
async def test_pending_suggestions_fail_verification(field, request_driven_rich_doc_api):
    plan = compile_doc([{"type": "paragraph", "text": "Body"}], GoogleSettings())
    document = await _render_with_fake(request_driven_rich_doc_api, plan)
    assert document["suggestionsViewMode"] == "SUGGESTIONS_INLINE"
    assert verify_rich_doc(document, plan) == (True, [])
    run = _paragraph(document, "Body")["elements"][0]
    if field == "suggestedParagraphStyleChanges":
        _paragraph(document, "Body")[field] = {"s1": {"paragraphStyle": {}}}
    elif field == "suggestedTextStyleChanges":
        run["textRun"][field] = {"s1": {"textStyle": {"bold": True}}}
    else:
        run["textRun"][field] = ["s1"]
    assert verify_rich_doc(document, plan) == (False, [])


@pytest.mark.parametrize("extra", ["child_tab", "positioned_object", "inline_objects", "two_tabs"])
async def test_unrequested_document_content_fails_verification(extra, request_driven_rich_doc_api):
    plan = compile_doc([{"type": "paragraph", "text": "Body"}], GoogleSettings())
    document = await _render_with_fake(request_driven_rich_doc_api, plan)
    tab = document["tabs"][0]
    if extra == "child_tab":
        tab["childTabs"] = [
            {
                "tabProperties": {"tabId": "child"},
                "documentTab": {"body": {"content": [{"paragraph": {"elements": []}}]}},
            }
        ]
    elif extra == "positioned_object":
        _paragraph(document, "Body")["positionedObjectIds"] = ["kix.image"]
        tab["documentTab"]["positionedObjects"] = {"kix.image": {"objectId": "kix.image"}}
    elif extra == "inline_objects":
        tab["documentTab"]["inlineObjects"] = {"kix.inline": {"objectId": "kix.inline"}}
    else:
        document["tabs"].append(copy.deepcopy(tab))
    assert verify_rich_doc(document, plan) == (False, [])


@pytest.mark.parametrize(
    "style",
    [
        {"borderTop": {"width": {"magnitude": 3, "unit": "PT"}}},
        {"borderLeft": {"width": {"magnitude": 1, "unit": "PT"}}},
        {"shading": {"backgroundColor": {"color": {"rgbColor": {"red": 1}}}}},
    ],
)
async def test_styled_empty_paragraph_is_not_table_padding(style, request_driven_rich_doc_api):
    plan = compile_doc(
        [{"type": "table", "header": ["A"], "rows": [["x"]]}, {"type": "paragraph", "text": "End"}],
        GoogleSettings(),
    )
    document = await _render_with_fake(request_driven_rich_doc_api, plan)
    assert verify_rich_doc(document, plan) == (True, [])
    padding = next(item for item in _body(document) if "paragraph" in item)
    padding["paragraph"]["paragraphStyle"].update(style)
    assert verify_rich_doc(document, plan) == (False, [])


@pytest.mark.parametrize("link", ["https://@", "https://user@", "https://:443", "http://[::1"])
def test_links_without_a_host_are_rejected(link):
    with pytest.raises(ValueError, match=r"Block 1 \(paragraph\), span 1"):
        compile_doc(
            [{"type": "paragraph", "spans": [{"text": "x", "link": link}]}], GoogleSettings()
        )
