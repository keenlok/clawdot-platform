"""Request builders for adding workbook tabs to an existing spreadsheet."""

from __future__ import annotations

import copy


def tab_requests(plan, *, sheet_id, index):
    """Adapt a one-tab workbook plan without changing its source requests."""
    batches = copy.deepcopy(plan.batches)
    finish = copy.deepcopy(plan.finish)
    first = plan.create["sheets"][0]["properties"]
    properties = copy.deepcopy(first)
    properties.update(sheetId=sheet_id, index=index)
    requests = [[{"addSheet": {"properties": properties}}]]

    def rewrite(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "sheetId" and item == 1:
                    value[key] = sheet_id
                else:
                    rewrite(item)
        elif isinstance(value, list):
            for item in value:
                rewrite(item)

    for batch in batches:
        if batch:
            batch_requests = []
            for request in batch:
                if "addSheet" not in request and "updateSpreadsheetProperties" not in request:
                    rewrite(request)
                    batch_requests.append(request)
            if batch_requests:
                requests.append(batch_requests)
    final_requests = []
    for request in finish:
        if "updateSpreadsheetProperties" in request:
            continue
        rewrite(request)
        final_requests.append(request)
    if final_requests:
        requests.append(final_requests)

    def ids(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "sheetId":
                    yield item
                else:
                    yield from ids(item)
        elif isinstance(value, list):
            for item in value:
                yield from ids(item)

    if any(value != sheet_id for batch in requests for request in batch for value in ids(request)):
        raise ValueError("Workbook plan references an unexpected sheet.")
    return requests
