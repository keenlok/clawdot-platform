from collections import defaultdict

import pytest
from chatcore_google.settings import GoogleSettings
from chatcore_google.workbook import render_workbook
from chatcore_google.workbook_builder import compile_workbook, preview_workbook


def full_tabs():
    return [
        {
            "name": "Compare",
            "heading": "Hotel shortlist",
            "note": "Prices per night",
            "columns": [
                {"header": "Hotel", "type": "text"},
                {"header": "Price", "type": "money", "decimals": 0, "total": True},
                {"header": "Rating", "type": "number"},
                {"header": "Breakfast", "type": "bool"},
                {"header": "Check-in", "type": "date"},
                {"header": "Share", "type": "percent", "total": True},
                {"header": "When", "type": "datetime"},
                {"header": "Link", "type": "url"},
            ],
            "rows": [
                [
                    '=IMPORTXML("x")',
                    21000,
                    4.5,
                    True,
                    "2027-03-02",
                    0.25,
                    "2027-03-02T10:00:00",
                    "https://example.com",
                ]
            ],
        },
        {"name": "Empty", "columns": [{"header": "Text", "type": "text"}], "rows": []},
        {
            "name": "Other",
            "columns": [
                {"header": "Text", "type": "text"},
                {"header": "Score", "type": "number"},
            ],
            "rows": [["-5", -5], ["+65", 65], ["@here", 1]],
        },
    ]


def test_builder_compiles_typed_tabs_totals_and_literal_text():
    model = compile_workbook(full_tabs(), GoogleSettings(), "Comparison")
    plan = render_workbook(model, title="Comparison", max_cells=200_000, max_cell_chars=5000)
    assert len(model["sheets"]) == 3
    first = model["sheets"][0]
    assert first["frozen_rows"] == 4
    assert first["rows"][0][0]["s"] == "title"
    assert first["rows"][3][0]["s"] == "header"
    assert len(first["tables"]) == 1 and first["tables"][0]["filter"] is True
    assert first["rows"][-1][1] == {"k": "sum", "v": [4, 4], "s": "total", "as": "money", "exp": 0}
    assert first["rows"][-1][5]["as"] == "percent"
    rendered = defaultdict(dict)
    for batch in plan.batches:
        for request in batch:
            update = request.get("updateCells")
            if update is None:
                continue
            sheet_id = update["start"]["sheetId"]
            start_row = update["start"]["rowIndex"]
            for row_offset, row in enumerate(update["rows"]):
                for column, cell in enumerate(row["values"]):
                    rendered[sheet_id][(start_row + row_offset, column)] = cell

    def value(row, column):
        return rendered[1][(row, column)]["userEnteredValue"]

    text_cell = rendered[1][(4, 0)]
    assert value(4, 0) == {"stringValue": '=IMPORTXML("x")'}
    assert value(4, 1) == {"numberValue": 21000.0}
    assert rendered[1][(4, 1)]["userEnteredFormat"]["numberFormat"] == {
        "type": "NUMBER",
        "pattern": '#,##0;[Red](#,##0);"–"',
    }
    assert value(4, 2) == {"numberValue": 4.5}
    assert rendered[1][(4, 2)]["userEnteredFormat"]["numberFormat"] == {
        "type": "NUMBER",
        "pattern": "#,##0.########",
    }
    assert value(4, 3) == {"boolValue": True}
    assert rendered[1][(4, 3)]["dataValidation"] == {"condition": {"type": "BOOLEAN"}}
    assert value(4, 4) == {"numberValue": 46448.0}
    assert rendered[1][(4, 4)]["userEnteredFormat"]["numberFormat"] == {
        "type": "DATE",
        "pattern": "dd mmm yyyy",
    }
    assert value(4, 5) == {"numberValue": 0.25}
    assert rendered[1][(4, 5)]["userEnteredFormat"]["numberFormat"] == {
        "type": "PERCENT",
        "pattern": "0.0%",
    }
    assert value(4, 6) == {"numberValue": pytest.approx(46448 + 10 / 24)}
    assert rendered[1][(4, 6)]["userEnteredFormat"]["numberFormat"] == {
        "type": "DATE_TIME",
        "pattern": "dd mmm yyyy hh:mm",
    }
    assert value(4, 7) == {"stringValue": "https://example.com"}
    assert rendered[1][(4, 7)]["textFormatRuns"] == [
        {"startIndex": 0, "format": {"link": {"uri": "https://example.com"}}}
    ]
    assert "formulaValue" not in text_cell["userEnteredValue"]
    assert rendered[1][(3, 0)]["userEnteredFormat"]["backgroundColor"]
    assert len(model["sheets"][1]["rows"]) == 2
    assert rendered[3][(1, 0)]["userEnteredValue"] == {"stringValue": "-5"}
    assert rendered[3][(2, 0)]["userEnteredValue"] == {"stringValue": "+65"}
    assert rendered[3][(2, 1)]["userEnteredValue"] == {"numberValue": 65.0}

    for sheet_id, frozen_rows, header_row, last_row, columns in (
        (1, 4, 3, 4, 8),
        (2, 1, 0, 0, 1),
        (3, 1, 0, 3, 2),
    ):
        grid = plan.create["sheets"][sheet_id - 1]["properties"]["gridProperties"]
        assert grid["frozenRowCount"] == frozen_rows
        header_format = rendered[sheet_id][(header_row, 0)]["userEnteredFormat"]
        assert header_format["textFormat"]["bold"] is True
        assert header_format["textFormat"]["foregroundColor"] == {
            "red": 1,
            "green": 1,
            "blue": 1,
        }
        assert header_format["wrapStrategy"] == "WRAP"
        assert header_format["horizontalAlignment"] == "CENTER"
        banding = [
            request["addBanding"]["bandedRange"]["range"]
            for request in plan.finish
            if "addBanding" in request
            and request["addBanding"]["bandedRange"]["range"]["sheetId"] == sheet_id
        ]
        filters = [
            request["setBasicFilter"]["filter"]["range"]
            for request in plan.finish
            if "setBasicFilter" in request
            and request["setBasicFilter"]["filter"]["range"]["sheetId"] == sheet_id
        ]
        expected_range = {
            "sheetId": sheet_id,
            "startRowIndex": header_row,
            "endRowIndex": last_row + 1,
            "startColumnIndex": 0,
            "endColumnIndex": columns,
        }
        assert banding == [expected_range]
        assert filters == [expected_range]

    formulas = [
        cell["userEnteredValue"]["formulaValue"]
        for sheet in rendered.values()
        for cell in sheet.values()
        if "formulaValue" in cell.get("userEnteredValue", {})
    ]
    assert formulas == ["=SUM(B5:B5)", "=SUM(F5:F5)"]
    assert model["sheets"][0]["widths"][0] == 129


@pytest.mark.parametrize(
    "column_index,value,header",
    [
        (0, 12, "Hotel"),
        (1, "21000", "Price"),
        (2, "4.5", "Rating"),
        (4, "March 2", "Check-in"),
        (5, "25%", "Share"),
        (6, "2027-03-02T10:00:00+08:00", "When"),
        (3, 1, "Breakfast"),
        (7, "javascript:alert(1)", "Link"),
    ],
)
def test_each_column_type_rejects_wrong_value_with_location(column_index, value, header):
    tabs = full_tabs()
    tabs[0]["rows"][0][column_index] = value
    with pytest.raises(ValueError, match=f'Tab "Compare", row 1, column "{header}"'):
        compile_workbook(tabs, GoogleSettings(), "Comparison")


@pytest.mark.parametrize("with_heading_note", [False, True])
def test_empty_tab_grid_has_an_unfrozen_row_and_table_stops_at_header(with_heading_note):
    tab = {"name": "Blank", "columns": [{"header": "Only", "type": "text"}], "rows": []}
    if with_heading_note:
        tab.update(heading="Heading", note="Note")
    model = compile_workbook([tab], GoogleSettings(), "Empty")
    plan = render_workbook(model, title="Empty", max_cells=200_000, max_cell_chars=5000)
    grid = plan.create["sheets"][0]["properties"]["gridProperties"]
    assert grid["rowCount"] > grid["frozenRowCount"]
    table = next(
        request["addBanding"]["bandedRange"]["range"]
        for request in plan.finish
        if "addBanding" in request
    )
    assert table["endRowIndex"] == grid["frozenRowCount"]
    assert model["sheets"][0]["rows"][-1] == []


def test_builder_rejects_too_many_columns_with_tab_name():
    tabs = [
        {
            "name": "Wide",
            "columns": [{"header": f"C{i}", "type": "text"} for i in range(9)],
            "rows": [],
        }
    ]
    with pytest.raises(ValueError, match='Tab "Wide"'):
        compile_workbook(tabs, GoogleSettings(max_columns=8), "Wide")


def test_preview_shows_headers_and_first_rows():
    tabs = full_tabs()
    tabs[0]["rows"] *= 4
    preview = preview_workbook(tabs)
    assert 'Tab "Compare": 4 rows' in preview
    assert "Hotel (text), Price (money)" in preview
    assert "… 1 more rows" in preview


@pytest.mark.parametrize(
    "change,location",
    [
        (lambda t: t[0]["rows"][0].__setitem__(1, "bad"), 'row 1, column "Price"'),
        (lambda t: t[0]["rows"][0].__setitem__(1, True), 'row 1, column "Price"'),
        (lambda t: t[0]["rows"][0].__setitem__(3, 1), 'row 1, column "Breakfast"'),
        (lambda t: t[0]["rows"][0].__setitem__(4, "bad"), 'row 1, column "Check-in"'),
        (lambda t: t[0]["columns"][0].update(total=True), 'Tab "Compare"'),
        (lambda t: t[0]["columns"][0].update(decimals=2), 'Tab "Compare"'),
        (lambda t: t[0].update(extra=True), 'Tab "Compare"'),
        (lambda t: t[0]["rows"][0].pop(), 'Tab "Compare"'),
    ],
)
def test_builder_rejects_invalid_values_with_location(change, location):
    tabs = full_tabs()
    change(tabs)
    with pytest.raises(ValueError, match=location):
        compile_workbook(tabs, GoogleSettings(), "Comparison")


def test_builder_limits_duplicates_empty_totals_and_zero_row_totals():
    tabs = full_tabs()
    tabs[0]["columns"][0]["width"] = 220
    tabs[0]["columns"][0].pop("total", None)
    tabs[0]["rows"] = []
    model = compile_workbook(tabs[:1], GoogleSettings(), "Empty")
    assert len(model["sheets"][0]["rows"]) == 5
    assert model["sheets"][0]["tables"][0]["last"] == model["sheets"][0]["tables"][0]["header"]
    assert model["sheets"][0]["widths"][0] == 220
    with pytest.raises(ValueError, match="unique case-insensitively"):
        compile_workbook([tabs[0], {**tabs[0], "name": "compare"}], GoogleSettings(), "Dup")
    with pytest.raises(ValueError, match="tabs"):
        compile_workbook(full_tabs(), GoogleSettings(max_tabs=1), "Many")
    too_many_rows = full_tabs()[:1]
    too_many_rows[0]["rows"] *= 2
    with pytest.raises(ValueError, match="rows"):
        compile_workbook(too_many_rows, GoogleSettings(max_rows=1), "Many")


def test_builder_rejects_oversize_argument_and_cell():
    tabs = [{"name": "T", "columns": [{"header": "A", "type": "text"}], "rows": [["x" * 6000]]}]
    with pytest.raises(ValueError, match="GOOGLE_OUTPUT_MAX_CHARS"):
        compile_workbook(
            tabs, GoogleSettings(output_max_chars=5000, max_cell_chars=10_000), "Oversize"
        )
    tabs[0]["rows"] = [["x" * 110]]
    with pytest.raises(ValueError, match='row 1, column "A"'):
        compile_workbook(tabs, GoogleSettings(max_cell_chars=100), "Oversize")


def test_google_output_character_limit_uses_environment_setting(monkeypatch):
    monkeypatch.setenv("GOOGLE_OUTPUT_MAX_CHARS", "7000")
    assert GoogleSettings().output_max_chars == 7000


def test_tab_limit_never_exceeds_the_renderer_ceiling():
    settings = GoogleSettings(max_tabs=100, export_max_cells=2_000_000)
    tab = {"columns": [{"header": "A", "type": "text"}], "rows": [["x"]]}
    tabs = [{**tab, "name": f"T{i}"} for i in range(80)]
    model = compile_workbook(tabs, settings, "Many")
    render_workbook(model, title="Many", max_cells=2_000_000, max_cell_chars=5000)
    with pytest.raises(ValueError, match="Provide 1–80 workbook tabs"):
        compile_workbook([*tabs, {**tab, "name": "T80"}], settings, "Many")
