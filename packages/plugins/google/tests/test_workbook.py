import json
from unittest.mock import AsyncMock

import pytest
from chatcore_google.client import GoogleError
from chatcore_google.workbook import (
    MAX_BATCH_BYTES,
    MAX_BATCH_REQUESTS,
    money_pattern,
    render_workbook,
    write_workbook,
)


def model(*sheets):
    return {"schema": 1, "title": "Expenses — T", "sheets": list(sheets)}


def tab(title="Summary", rows=None, **extra):
    return {"title": title, "rows": rows or [[{"k": "text", "v": "x"}]], **extra}


def render(book, **kwargs):
    return render_workbook(
        book,
        title=kwargs.pop("title", "Final"),
        max_cells=kwargs.pop("max_cells", 200_000),
        max_cell_chars=5000,
        **kwargs,
    )


def cells(plan):
    return [
        cell
        for batch in plan.batches
        for request in batch
        for row in request["updateCells"]["rows"]
        for cell in row["values"]
    ]


def test_typed_values_formats_and_literal_text():
    plan = render(
        model(
            tab(
                rows=[
                    [
                        {"k": "text", "v": '=HYPERLINK("http://evil")', "s": "wrap"},
                        {"k": "text", "v": "-5 taxi"},
                        {"k": "money", "v": "464.00", "exp": 2},
                        {"k": "money", "v": "-1200", "exp": 0},
                        {"k": "rate", "v": "0.1812345678912345678"},
                        {"k": "date", "v": "2026-11-08"},
                        {"k": "datetime", "v": "2026-11-08T18:00:00"},
                        {"k": "number", "v": "46400"},
                        None,
                    ],
                ]
            )
        )
    )
    text, minus, money, yen, rate, day, moment, number, empty = cells(plan)
    assert text["userEnteredValue"] == {"stringValue": '=HYPERLINK("http://evil")'}
    assert text["userEnteredFormat"]["wrapStrategy"] == "WRAP"
    assert minus["userEnteredValue"] == {"stringValue": "-5 taxi"}
    assert money["userEnteredValue"] == {"numberValue": 464.0}
    assert money["userEnteredFormat"]["numberFormat"]["pattern"] == money_pattern(2)
    assert money_pattern(2) == '#,##0.00;[Red](#,##0.00);"–"'
    assert yen["userEnteredFormat"]["numberFormat"]["pattern"] == '#,##0;[Red](#,##0);"–"'
    assert rate["userEnteredValue"]["numberValue"] == pytest.approx(0.18123456789123457)
    assert day["userEnteredValue"] == {"numberValue": 46334.0}
    assert day["userEnteredFormat"]["numberFormat"]["type"] == "DATE"
    assert moment["userEnteredValue"]["numberValue"] == pytest.approx(46334.75)
    assert number["userEnteredValue"] == {"numberValue": 46400.0}
    assert empty == {}
    assert "formulaValue" not in json.dumps(plan.batches)


def test_sum_is_the_only_formula_and_must_point_above():
    rows = [
        [None, {"k": "money", "v": "1", "exp": 2}],
        [None, {"k": "money", "v": "2", "exp": 2}],
        [None, {"k": "sum", "v": [0, 1], "exp": 2, "s": "total"}],
    ]
    plan = render(model(tab(rows=rows)))
    assert cells(plan)[-1]["userEnteredValue"] == {"formulaValue": "=SUM(B1:B2)"}
    for bad in ([0, 2], [1, 0], ["A1", 1], [0]):
        with pytest.raises(ValueError, match="sum rows above"):
            render(model(tab(rows=[[{"k": "sum", "v": bad, "exp": 2}]] * 3)))


@pytest.mark.parametrize(
    "cell",
    [
        {"k": "formula", "v": "=1"},
        {"k": "text", "v": 5},
        {"k": "text", "v": "x", "s": "bogus"},
        {"k": "text", "v": "x", "extra": 1},
        {"k": "money", "v": 12.5, "exp": 2},
        {"k": "money", "v": "NaN", "exp": 2},
        {"k": "money", "v": "1e20", "exp": 2},
        {"k": "money", "v": "1", "exp": 9},
        {"k": "date", "v": "15/11/2026"},
        {"k": "datetime", "v": "2026-11-08T10:00:00+08:00"},
        {"k": "text", "v": "x" * 5001},
    ],
)
def test_invalid_cells_are_rejected_before_any_write(cell):
    with pytest.raises(ValueError):
        render(model(tab(rows=[[cell]])))


def test_tab_names_layout_and_filters_are_validated():
    with pytest.raises(ValueError, match="unique"):
        render(model(tab("Ana"), tab("ana")))
    with pytest.raises(ValueError, match="unique"):
        render(model(tab("bad\ttab")))
    with pytest.raises(ValueError, match="Unsupported"):
        render({"schema": 2, "sheets": [tab()]})
    rows = [[{"k": "text", "v": "h"}]] * 4
    two_filters = [
        {"header": 0, "first": 1, "last": 1, "columns": 1, "filter": True},
        {"header": 2, "first": 3, "last": 3, "columns": 1, "filter": True},
    ]
    with pytest.raises(ValueError, match="one filter"):
        render(model(tab(rows=rows, tables=two_filters)))
    with pytest.raises(ValueError, match="out of range"):
        render(model(tab(rows=rows, tables=[{"header": 0, "last": 9, "columns": 1}])))


def test_create_body_finish_requests_and_incomplete_title():
    rows = [
        [{"k": "text", "v": "Header", "s": "header"}, {"k": "text", "v": "B"}],
        [{"k": "text", "v": "x"}, {"k": "money", "v": "1", "exp": 2}],
    ]
    plan = render(
        model(
            tab(
                "Summary",
                rows=rows,
                widths=[160, 90],
                frozen_rows=1,
                tables=[{"header": 0, "first": 1, "last": 1, "columns": 2, "filter": True}],
            ),
            tab("Ana"),
        ),
        title="Trip export",
    )
    assert plan.create["properties"]["title"] == "Trip export (incomplete)"
    summary, ana = plan.create["sheets"]
    assert summary["properties"]["gridProperties"] == {
        "rowCount": 2,
        "columnCount": 2,
        "frozenRowCount": 1,
    }
    assert summary["properties"]["sheetId"] != ana["properties"]["sheetId"]
    kinds = [next(iter(request)) for request in plan.finish]
    assert kinds == [
        "addBanding",
        "setBasicFilter",
        "updateDimensionProperties",
        "updateDimensionProperties",
        "updateSpreadsheetProperties",
    ]
    assert plan.finish[-1]["updateSpreadsheetProperties"]["properties"] == {"title": "Trip export"}
    assert plan.cells == 4 + 1


def test_large_workbook_is_batched_within_bounds_and_cell_cap_is_enforced():
    row = [{"k": "text", "v": "description " * 8, "s": "wrap"}] + [
        {"k": "money", "v": "12345.67", "exp": 2} for _ in range(29)
    ]
    rows = [row for _ in range(5000)]
    plan = render(model(tab("Transactions", rows=rows)), max_cells=200_000)
    assert len(plan.batches) > 1
    written = 0
    for batch in plan.batches:
        assert len(batch) <= MAX_BATCH_REQUESTS
        assert len(json.dumps(batch, ensure_ascii=False, separators=(",", ":"))) <= (
            MAX_BATCH_BYTES + 2 + len(batch)
        )
        for request in batch:
            assert request["updateCells"]["start"]["rowIndex"] == written
            written += len(request["updateCells"]["rows"])
    assert written == 5000  # nothing truncated
    with pytest.raises(ValueError, match="GOOGLE_EXPORT_MAX_CELLS"):
        render(model(tab("Transactions", rows=rows)), max_cells=100_000)


async def test_write_reports_partial_failure_with_link_and_never_retries():
    plan = render(model(tab("Summary"), tab("Ana")))
    client = AsyncMock()
    client.request.return_value = {"spreadsheetId": "new-file"}
    client.batch.side_effect = [None, GoogleError("boom")]
    url, failure = await write_workbook(client, plan)
    assert url == "https://docs.google.com/spreadsheets/d/new-file/edit"
    assert failure == "step 2 of 2 failed"
    assert client.request.await_count == 1 and client.batch.await_count == 2


async def test_write_success_renames_last():
    plan = render(model(tab()))
    client = AsyncMock()
    client.request.return_value = {"spreadsheetId": "f"}
    assert await write_workbook(client, plan) == (
        "https://docs.google.com/spreadsheets/d/f/edit",
        None,
    )
    last = client.batch.await_args_list[-1].args[1]
    assert last[-1]["updateSpreadsheetProperties"]["properties"]["title"] == "Final"


async def test_write_without_spreadsheet_id_fails_before_batches():
    client = AsyncMock()
    client.request.return_value = {}
    with pytest.raises(ValueError):
        await write_workbook(client, render(model(tab())))
    client.batch.assert_not_awaited()
