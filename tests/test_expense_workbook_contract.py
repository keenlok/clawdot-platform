"""The expenses workbook model renders through the Google workbook renderer unchanged."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from chatcore_expenses.balances import ledger_balances
from chatcore_expenses.workbook import build_workbook
from chatcore_google.workbook import render_workbook

BASE = datetime(2026, 11, 8, 2, tzinfo=UTC)
PEOPLE = ["fake:a", "fake:b", "fake:c", "fake:d"]


def ledger(count):
    trip = {
        "id": "t",
        "name": "Contract",
        "status": "active",
        "settle_mode": "convert",
        "settle_currency": "SGD",
        "start_date": date(2026, 11, 8),
        "end_date": None,
    }
    rates = {"HKD": "0.16352", "JPY": "0.0089", "SGD": "1", "CNY": None}
    expenses = []
    for i in range(count):
        currency = ["HKD", "JPY", "SGD", "CNY"][i % 4]
        amount = 1000 + i * 7
        per, rem = divmod(amount, 3)
        expenses.append(
            {
                "id": f"e{i}",
                "spent_on": date(2026, 11, 8) + timedelta(days=i % 6),
                "created_at": BASE + timedelta(minutes=i),
                "description": f"=cmd|' /C calc'!A{i}",
                "paid_by": PEOPLE[i % 4],
                "currency": currency,
                "amount": amount,
                "shares": [
                    {"user_key": k, "amount": per + (1 if j < rem else 0)}
                    for j, k in enumerate(PEOPLE[:3])
                ],
                "fx_rate_to_settle": rates[currency],
            }
        )
    settlements = [
        {
            "id": "s",
            "created_at": BASE,
            "from_user": "fake:b",
            "to_user": "fake:a",
            "currency": "SGD",
            "amount": 1234,
        }
    ]
    return trip, expenses, settlements


def test_real_builder_output_renders_typed_and_complete():
    trip, expenses, settlements = ledger(400)  # far beyond the generic 300-row create limit
    book = build_workbook(
        trip,
        expenses,
        settlements,
        {"fake:a": "Ana", "fake:b": "Ben"},
        now=BASE,
        timezone=ZoneInfo("Asia/Singapore"),
    )
    plan = render_workbook(
        book.model, title=book.model["title"], max_cells=200_000, max_cell_chars=5000
    )
    titles = [s["properties"]["title"] for s in plan.create["sheets"]]
    assert titles[:3] == ["Summary", "Transactions", "Repayments"] and titles[-1] == "Raw ledger"
    blob = json.dumps(plan.batches)
    formulas = [
        cell["userEnteredValue"]["formulaValue"]
        for batch in plan.batches
        for request in batch
        for row in request["updateCells"]["rows"]
        for cell in row["values"]
        if "formulaValue" in cell.get("userEnteredValue", {})
    ]
    assert formulas and all(f.startswith("=SUM(") for f in formulas)
    assert '"stringValue": "=cmd|\' /C calc\'!A0"' in blob
    groups, unconverted = ledger_balances(trip, expenses, settlements)
    assert len(unconverted) == 100
    transactions = next(s for s in book.model["sheets"] if s["title"] == "Transactions")
    assert sum(1 for row in transactions["rows"] if row and row[0] and row[0]["k"] == "date") == 400
    summary = next(s for s in book.model["sheets"] if s["title"] == "Summary")
    nets = [
        Decimal(row[6]["v"])
        for row in summary["rows"]
        if len(row) > 7 and row[6] and row[6]["k"] == "money"
    ]
    assert len(nets) == 4 and sum(nets) == 0
    assert sorted(nets) == sorted(Decimal(groups["SGD"].get(k, 0)) / 100 for k in PEOPLE)
