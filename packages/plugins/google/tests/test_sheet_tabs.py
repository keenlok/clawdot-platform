import copy

from chatcore_google.repository import GoogleRepository
from chatcore_google.settings import GoogleSettings
from chatcore_google.sheet_tabs import tab_requests
from chatcore_google.workbook import render_workbook
from chatcore_google.workbook_builder import compile_workbook


def sample_plan():
    model = compile_workbook(
        [
            {
                "name": "New tab",
                "columns": [{"header": "Value", "type": "text"}],
                "rows": [["literal"]],
            }
        ],
        GoogleSettings(),
        "Unused title",
    )
    return render_workbook(model, title="Unused title", max_cells=200_000, max_cell_chars=5000)


def test_tab_requests_adds_and_rewrites_without_mutating_plan():
    plan = sample_plan()
    original = copy.deepcopy(plan)
    batches = tab_requests(plan, sheet_id=934, index=3)
    assert batches[0] == [
        {
            "addSheet": {
                "properties": {
                    "sheetId": 934,
                    "title": "New tab",
                    "gridProperties": batches[0][0]["addSheet"]["properties"]["gridProperties"],
                    "index": 3,
                }
            }
        }
    ]
    requests = [request for batch in batches for request in batch]
    assert not any("updateSpreadsheetProperties" in request for request in requests)
    assert all(nested != 1 for request in requests for nested in _sheet_ids(request))
    assert any(934 in list(_sheet_ids(request)) for request in requests[1:])
    assert plan == original


def _sheet_ids(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "sheetId":
                yield item
            else:
                yield from _sheet_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _sheet_ids(item)


async def test_output_trashed_marker_is_scoped_and_idempotent():
    repo = GoogleRepository()
    await repo.record_output("chat", "file", "sheet", "requester", "topic", "owner", "action")
    assert await repo.mark_output_trashed("other-chat", "file", "action") is False
    assert await repo.mark_output_trashed("chat", "file", "wrong-action") is False
    assert await repo.mark_output_trashed("chat", "file", "action") is True
    row = await repo.output_any("chat", "file")
    assert row["trashed_at"] is not None
    assert await repo.output("chat", "topic", "file") is None
    assert await repo.mark_output_trashed("chat", "file", "action") is False
