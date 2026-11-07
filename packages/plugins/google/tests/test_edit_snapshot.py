import pytest
from chatcore_google.edit_snapshot import (
    build_sheet_edit,
    doc_style_for_range,
    find_doc_match,
    flatten_doc_snapshot,
    resolve_effective_doc_style,
)


def doc(text, *, tab_id="tab-1"):
    length = len(text.encode("utf-16-le")) // 2
    return {
        "revisionId": "rev-1",
        "tabs": [
            {
                "tabProperties": {"tabId": tab_id, "title": "Main"},
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
                                    "foregroundColor": {
                                        "color": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}
                                    },
                                    "baselineOffset": "NONE",
                                    "fontSize": {"magnitude": 11, "unit": "PT"},
                                    "weightedFontFamily": {"fontFamily": "Arial"},
                                },
                            }
                        ]
                    },
                    "body": {
                        "content": [
                            {
                                "startIndex": 1,
                                "endIndex": length + 2,
                                "paragraph": {
                                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": length + 1,
                                            "textRun": {
                                                "content": text,
                                                "textStyle": {"bold": True},
                                            },
                                        },
                                        {
                                            "startIndex": length + 1,
                                            "endIndex": length + 2,
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


def test_doc_snapshot_keeps_tab_revision_exact_text_and_style_spans():
    snapshot = flatten_doc_snapshot(doc("Hi 😀").copy(), tab_id="tab-1", max_chars=100)
    assert snapshot.text == "Hi 😀\n"
    match = find_doc_match(snapshot, "😀")
    assert (match.start_index, match.end_index) == (4, 6)
    assert match.uniform_style == {"bold": True}
    assert match.style_resolved is True
    assert match.effective_style["fontSize"] == 11.0


def test_effective_style_resolution_fails_closed_for_unknown_defaults_and_bad_shapes():
    styles = doc("target")["tabs"][0]["documentTab"]["namedStyles"]["styles"]
    incomplete = [{"namedStyleType": "NORMAL_TEXT", "textStyle": {"bold": True}}]
    assert resolve_effective_doc_style({"bold": True}, incomplete, "NORMAL_TEXT")[1] is False
    malformed = [
        {
            "namedStyleType": "NORMAL_TEXT",
            "textStyle": {**styles[0]["textStyle"], "italic": "false"},
        }
    ]
    assert resolve_effective_doc_style({"bold": True}, malformed, "NORMAL_TEXT")[1] is False
    assert resolve_effective_doc_style({"bold": True}, styles, None)[1] is False


def test_heading_style_inherits_normal_text_and_unspecified_baseline_offset():
    normal = doc("target")["tabs"][0]["documentTab"]["namedStyles"]["styles"][0]["textStyle"]
    named_styles = [
        {"namedStyleType": "NORMAL_TEXT", "textStyle": normal},
        {
            "namedStyleType": "HEADING_1",
            "textStyle": {
                "bold": True,
                "baselineOffset": "BASELINE_OFFSET_UNSPECIFIED",
                "link": {"url": "https://ignored.example"},
            },
        },
    ]
    effective, resolved = resolve_effective_doc_style({}, named_styles, "HEADING_1")
    assert resolved is True
    assert effective["italic"] is False
    assert effective["baselineOffset"] == "NONE"
    assert effective["link"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("fontSize", {"magnitude": True, "unit": "PT"}),
        ("weightedFontFamily", {"fontFamily": "Arial", "weight": True}),
        (
            "foregroundColor",
            {"color": {"rgbColor": {"red": True, "green": 0, "blue": 0}}},
        ),
    ],
)
def test_style_resolver_rejects_boolean_numeric_shapes(field, value):
    styles = doc("target")["tabs"][0]["documentTab"]["namedStyles"]["styles"]
    styles = [
        {
            **styles[0],
            "textStyle": {**styles[0]["textStyle"], field: value},
        }
    ]
    assert resolve_effective_doc_style({}, styles, "NORMAL_TEXT")[1] is False


def test_snapshot_uses_selected_tab_named_styles_and_paragraph_style_identity():
    value = doc("target")
    second = doc("other", tab_id="tab-2")["tabs"][0]
    second["tabProperties"]["title"] = "Other"
    second["documentTab"]["namedStyles"]["styles"][0]["textStyle"]["weightedFontFamily"] = {
        "fontFamily": "Courier New"
    }
    value["tabs"].append(second)
    snapshot = flatten_doc_snapshot(value, tab_id="tab-2", max_chars=100)
    match = find_doc_match(snapshot, "other")
    assert match.named_style_type == "NORMAL_TEXT"
    assert match.effective_style["weightedFontFamily"]["fontFamily"] == "Courier New"


def test_snapshot_does_not_borrow_first_tab_global_style_for_later_tab():
    value = doc("first")
    second = doc("target", tab_id="tab-2")["tabs"][0]
    second["tabProperties"]["title"] = "Other"
    second["documentTab"].pop("namedStyles")
    value["namedStyles"] = value["tabs"][0]["documentTab"]["namedStyles"]
    value["tabs"].append(second)

    match = find_doc_match(flatten_doc_snapshot(value, tab_id="tab-2", max_chars=100), "target")

    assert match.style_resolved is False


@pytest.mark.parametrize("suggestion_field", ["paragraph", "named_styles"])
def test_pending_paragraph_or_named_style_changes_leave_style_unresolved(suggestion_field):
    value = doc("target")
    tab = value["tabs"][0]["documentTab"]
    if suggestion_field == "paragraph":
        tab["body"]["content"][0]["paragraph"]["suggestedParagraphStyleChanges"] = {
            "suggestion-1": {"paragraphStyle": {"namedStyleType": "HEADING_1"}}
        }
    else:
        tab["suggestedNamedStylesChanges"] = {"suggestion-1": {"namedStyles": {}}}

    match = find_doc_match(flatten_doc_snapshot(value, tab_id="tab-1", max_chars=100), "target")

    assert match.style_resolved is False


def test_global_named_style_suggestions_in_single_tab_leave_style_unresolved():
    value = doc("target")
    tab = value["tabs"][0]["documentTab"]
    value["namedStyles"] = tab.pop("namedStyles")
    value["suggestedNamedStylesChanges"] = {"suggestion-1": {"namedStyles": {}}}

    match = find_doc_match(flatten_doc_snapshot(value, tab_id="tab-1", max_chars=100), "target")

    assert match.style_resolved is False


def test_doc_match_rejects_duplicate_absent_split_style_and_suggested_span():
    snapshot = flatten_doc_snapshot(doc("target target"), tab_id="tab-1", max_chars=100)
    with pytest.raises(ValueError, match="exactly one"):
        find_doc_match(snapshot, "target")
    with pytest.raises(ValueError, match="not found"):
        find_doc_match(snapshot, "missing")

    split = doc("target")
    runs = split["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"]
    runs[0]["textRun"]["textStyle"] = {"bold": True}
    runs[0]["textRun"]["content"] = "tar"
    runs[0]["endIndex"] = 4
    runs.insert(
        1,
        {
            "startIndex": 4,
            "endIndex": 7,
            "textRun": {"content": "get", "textStyle": {"italic": True}},
        },
    )
    runs[2]["startIndex"] = 7
    runs[2]["endIndex"] = 8
    mixed = flatten_doc_snapshot(split, tab_id="tab-1", max_chars=100)
    with pytest.raises(ValueError, match="mixed styling"):
        find_doc_match(mixed, "target")

    suggested = doc("target")
    run = suggested["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0][
        "textRun"
    ]
    run["suggestedInsertionIds"] = ["suggestion-1"]
    with pytest.raises(ValueError, match="suggestion"):
        find_doc_match(flatten_doc_snapshot(suggested, tab_id="tab-1", max_chars=100), "target")


def test_snapshot_requires_explicit_tab_when_multiple_tabs_and_rejects_structural_match():
    value = doc("same")
    value["tabs"].append(
        {
            "tabProperties": {"tabId": "tab-2", "title": "Other"},
            "documentTab": value["tabs"][0]["documentTab"],
        }
    )
    with pytest.raises(ValueError, match="explicit tab"):
        flatten_doc_snapshot(value, tab_id=None, max_chars=100)
    structural = doc("before\nafter")
    snapshot = flatten_doc_snapshot(structural, tab_id="tab-1", max_chars=100)
    with pytest.raises(ValueError, match="paragraph boundary"):
        find_doc_match(snapshot, "before\nafter")


def test_snapshot_finds_table_cell_text_and_maps_indices_from_text_runs():
    value = doc("outside")
    table = {
        "startIndex": 8,
        "endIndex": 22,
        "table": {
            "tableRows": [
                {
                    "tableCells": [
                        {
                            "content": [
                                {
                                    "startIndex": 10,
                                    "endIndex": 17,
                                    "paragraph": {
                                        "elements": [
                                            {
                                                "startIndex": 10,
                                                "endIndex": 16,
                                                "textRun": {
                                                    "content": "inside",
                                                    "textStyle": {"bold": False},
                                                },
                                            },
                                            {
                                                "startIndex": 16,
                                                "endIndex": 17,
                                                "textRun": {"content": "\n", "textStyle": {}},
                                            },
                                        ]
                                    },
                                },
                            ]
                        }
                    ]
                }
            ]
        },
    }
    value["tabs"][0]["documentTab"]["body"]["content"].append(table)
    snapshot = flatten_doc_snapshot(value, tab_id="tab-1", max_chars=100)
    match = find_doc_match(snapshot, "inside")
    assert (match.start_index, match.end_index) == (10, 16)
    assert match.uniform_style == {"bold": False}
    assert match.style_resolved is False


def test_table_row_suggestion_is_propagated_into_its_cell_text():
    value = doc("outside")
    table = {
        "startIndex": 8,
        "endIndex": 22,
        "table": {
            "tableRows": [
                {
                    "startIndex": 9,
                    "endIndex": 18,
                    "suggestedInsertionIds": ["pending-row"],
                    "tableCells": [
                        {
                            "content": [
                                {
                                    "startIndex": 10,
                                    "endIndex": 17,
                                    "paragraph": {
                                        "elements": [
                                            {
                                                "startIndex": 10,
                                                "endIndex": 16,
                                                "textRun": {"content": "inside", "textStyle": {}},
                                            },
                                            {
                                                "startIndex": 16,
                                                "endIndex": 17,
                                                "textRun": {"content": "\n", "textStyle": {}},
                                            },
                                        ]
                                    },
                                }
                            ]
                        }
                    ],
                }
            ]
        },
    }
    value["tabs"][0]["documentTab"]["body"]["content"].append(table)
    snapshot = flatten_doc_snapshot(value, tab_id="tab-1", max_chars=100)
    assert snapshot.complete is False
    with pytest.raises(ValueError, match="suggestion"):
        find_doc_match(snapshot, "inside")


def test_snapshot_refuses_suggestions_before_match_and_missing_revision_or_tab_id():
    value = doc("target")
    value["revisionId"] = ""
    with pytest.raises(ValueError, match="revision"):
        flatten_doc_snapshot(value, tab_id="tab-1", max_chars=100)
    value["revisionId"] = "rev-1"
    value["tabs"][0]["tabProperties"]["tabId"] = ""
    with pytest.raises(ValueError, match="tab ID"):
        flatten_doc_snapshot(value, tab_id="", max_chars=100)
    value["tabs"][0]["tabProperties"]["tabId"] = "tab-1"
    run = value["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0][
        "textRun"
    ]
    run["suggestedInsertionIds"] = ["suggestion-1"]
    with pytest.raises(ValueError, match="suggestion"):
        find_doc_match(flatten_doc_snapshot(value, tab_id="tab-1", max_chars=100), "target")


def test_style_readback_finds_adjacent_run_and_requires_full_uniform_coverage():
    value = doc("before target")
    runs = value["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"]
    runs[0]["textRun"]["content"] = "before "
    runs[0]["endIndex"] = 8
    runs[0]["textRun"]["textStyle"] = {"italic": True, "bold": True}
    runs.insert(
        1,
        {
            "startIndex": 8,
            "endIndex": 14,
            "textRun": {"content": "target", "textStyle": {}},
        },
    )
    runs[2]["startIndex"] = 14
    runs[2]["endIndex"] = 15
    snapshot = flatten_doc_snapshot(value, tab_id="tab-1", max_chars=100)
    target = find_doc_match(snapshot, "target")
    assert target.uniform_style == {}
    assert doc_style_for_range(snapshot, target.start_index, target.end_index) == {}
    with pytest.raises(ValueError, match="complete styling coverage"):
        doc_style_for_range(snapshot, target.start_index, target.end_index + 2)


def sheet_grid(*, formula=False, merge=None, protection=None, spilled=False):
    cells = [
        {"userEnteredValue": {"numberValue": 7}, "effectiveValue": {"numberValue": 7}},
        {"userEnteredValue": {"stringValue": "old"}, "effectiveValue": {"stringValue": "old"}},
    ]
    if formula:
        cells[0]["userEnteredValue"] = {"formulaValue": "=1+6"}
    if spilled:
        cells[0].pop("userEnteredValue")
    sheet = {
        "properties": {
            "sheetId": 42,
            "title": "Day 1",
            "gridProperties": {"rowCount": 100, "columnCount": 10},
        },
        "merges": [merge] if merge else [],
        "protectedRanges": [{"range": protection}] if protection else [],
        "data": [{"startRow": 4, "startColumn": 6, "rowData": [{"values": cells}]}],
    }
    return {"sheets": [sheet]}


def test_sheet_edit_keeps_exact_typed_preconditions_and_literal_prefixes():
    snapshot = build_sheet_edit(
        sheet_grid(),
        sheet_id=42,
        tab="Day 1",
        cell_range="G5:H5",
        values=[["-2 pax", "+65 8123"]],
        max_cells=100,
        max_cell_chars=100,
    )
    assert snapshot.before == (({"numberValue": 7}, {"stringValue": "old"}),)
    assert snapshot.after == (({"stringValue": "-2 pax"}, {"stringValue": "+65 8123"}),)
    assert snapshot.grid_range == {
        "sheetId": 42,
        "startRowIndex": 4,
        "endRowIndex": 5,
        "startColumnIndex": 6,
        "endColumnIndex": 8,
    }
    assert snapshot.type_changes == ((True, False),)


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"formula": True}, "formula"),
        ({"spilled": True}, "spill"),
        (
            {
                "merge": {
                    "sheetId": 42,
                    "startRowIndex": 4,
                    "endRowIndex": 5,
                    "startColumnIndex": 6,
                    "endColumnIndex": 8,
                }
            },
            "merged",
        ),
        (
            {
                "protection": {
                    "sheetId": 42,
                    "startRowIndex": 4,
                    "endRowIndex": 5,
                    "startColumnIndex": 6,
                    "endColumnIndex": 7,
                }
            },
            "protected",
        ),
    ],
)
def test_sheet_edit_refuses_unsupported_existing_target_cells(kwargs, message):
    with pytest.raises(ValueError, match=message):
        build_sheet_edit(
            sheet_grid(**kwargs),
            sheet_id=42,
            tab="Day 1",
            cell_range="G5:H5",
            values=[["literal", None]],
            max_cells=100,
            max_cell_chars=100,
        )


def test_sheet_edit_requires_exact_grid_shape_and_cell_cap():
    with pytest.raises(ValueError, match="same dimensions"):
        build_sheet_edit(
            sheet_grid(),
            sheet_id=42,
            tab="Day 1",
            cell_range="G5:H5",
            values=[["one"]],
            max_cells=100,
            max_cell_chars=100,
        )


def test_sheet_edit_applies_existing_row_column_and_total_char_limits():
    with pytest.raises(ValueError, match="column limit"):
        build_sheet_edit(
            sheet_grid(),
            sheet_id=42,
            tab="Day 1",
            cell_range="A1:AG1",
            values=[["x"] * 33],
            max_cells=100,
            max_cell_chars=100,
            max_rows=300,
            max_columns=32,
            max_chars=1000,
        )
    with pytest.raises(ValueError, match="content limit"):
        build_sheet_edit(
            sheet_grid(),
            sheet_id=42,
            tab="Day 1",
            cell_range="G5:H5",
            values=[["123456", "123456"]],
            max_cells=100,
            max_cell_chars=100,
            max_chars=10,
        )


def test_sheet_edit_catches_unbounded_and_named_protection_and_checks_full_coverage():
    whole_sheet = sheet_grid(protection={"sheetId": 42})
    with pytest.raises(ValueError, match="protected"):
        build_sheet_edit(
            whole_sheet,
            sheet_id=42,
            tab="Day 1",
            cell_range="G5:H5",
            values=[["one", "two"]],
            max_cells=100,
            max_cell_chars=100,
        )


def test_sheet_edit_allows_last_grid_column():
    value = {
        "sheets": [
            {
                "properties": {
                    "sheetId": 42,
                    "title": "Day 1",
                    "gridProperties": {"rowCount": 5, "columnCount": 10},
                },
                "data": [{"startRow": 4, "startColumn": 9, "rowData": [{"values": [{}]}]}],
            }
        ]
    }
    snapshot = build_sheet_edit(
        value,
        sheet_id=42,
        tab="Day 1",
        cell_range="J5",
        values=[["last cell"]],
        max_cells=100,
        max_cell_chars=100,
    )
    assert snapshot.grid_range["endColumnIndex"] == 10
    named = sheet_grid()
    named["namedRanges"] = [
        {
            "namedRangeId": "named-1",
            "range": {
                "sheetId": 42,
                "startRowIndex": 4,
                "endRowIndex": 5,
                "startColumnIndex": 6,
                "endColumnIndex": 7,
            },
        }
    ]
    named["sheets"][0]["protectedRanges"] = [{"namedRangeId": "named-1"}]
    with pytest.raises(ValueError, match="protected"):
        build_sheet_edit(
            named,
            sheet_id=42,
            tab="Day 1",
            cell_range="G5:H5",
            values=[["one", "two"]],
            max_cells=100,
            max_cell_chars=100,
        )
    incomplete = sheet_grid()
    incomplete["sheets"][0]["data"][0]["rowData"][0]["values"].pop()
    with pytest.raises(ValueError, match="complete typed coverage"):
        build_sheet_edit(
            incomplete,
            sheet_id=42,
            tab="Day 1",
            cell_range="G5:H5",
            values=[["one", "two"]],
            max_cells=100,
            max_cell_chars=100,
        )
    with pytest.raises(ValueError, match="cell limit"):
        build_sheet_edit(
            sheet_grid(),
            sheet_id=42,
            tab="Day 1",
            cell_range="G5:H5",
            values=[["one", "two"]],
            max_cells=1,
            max_cell_chars=100,
        )
