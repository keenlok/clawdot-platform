import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from chatcore.events import ChatRef, User, UserRef
from chatcore.testing import FakePlatform, memory_stores
from chatcore.tools.registry import ToolContext
from chatcore_expenses.balances import convert_balances, ledger_balances
from chatcore_expenses.plugin import ExpensesPlugin
from chatcore_expenses.repository import MemoryLedgerRepo
from chatcore_expenses.workbook import FIXED_TABS, build_workbook, tab_names

ZONE = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 11, 15, 11, 9, tzinfo=UTC)
BASE = datetime(2026, 11, 8, 2, 0, tzinfo=UTC)
CONVERT = {
    "id": "trip-1",
    "name": "Coast",
    "status": "active",
    "settle_mode": "convert",
    "settle_currency": "SGD",
    "start_date": date(2026, 11, 8),
    "end_date": date(2026, 11, 15),
}
NAMES = {"fake:a": "Ana", "fake:b": "Ben", "fake:c": "Cy"}


def expense(
    id,
    amount,
    *,
    currency="HKD",
    paid_by="fake:a",
    shares=None,
    rate="0.16352",
    spent_on=date(2026, 11, 8),
    hour=0,
    description=None,
):
    if shares is None:
        per, rem = divmod(amount, 3)
        shares = {k: per + (1 if i < rem else 0) for i, k in enumerate(NAMES)}
    return {
        "id": id,
        "spent_on": spent_on,
        "created_at": BASE + timedelta(hours=hour),
        "description": description or f"Item {id}",
        "paid_by": paid_by,
        "currency": currency,
        "amount": amount,
        "shares": [{"user_key": k, "amount": v} for k, v in shares.items()],
        "fx_rate_to_settle": rate,
    }


def build(trip, expenses, settlements=(), names=NAMES):
    return build_workbook(trip, expenses, list(settlements), names, now=NOW, timezone=ZONE)


def sheet(book, title):
    return next(s for s in book.model["sheets"] if s["title"] == title)


def values(row):
    return [None if cell is None else cell["v"] for cell in row]


def header_index(tab, first):
    return next(
        i
        for i, row in enumerate(tab["rows"])
        if row and row[0] and row[0]["v"] == first and row[0].get("s") == "header"
    )


def table_rows(tab, first_header):
    """Data rows of the first table whose header starts with ``first_header``."""
    start = header_index(tab, first_header)
    table = next(t for t in tab["tables"] if t["header"] == start)
    return tab["rows"][table["first"] : table["last"] + 1]


def summary_rows(book):
    return {
        row[0]["v"]: row
        for row in table_rows(sheet(book, "Summary"), "Person")
        if row[0]["v"] != "Total"
    }


def test_full_export_has_all_tabs_sorted_transactions_and_json_model():
    expenses = [
        expense("e3", 900, spent_on=date(2026, 11, 9), hour=1),
        expense("e2", 600, spent_on=date(2026, 11, 8), hour=5),
        expense("e1", 300, spent_on=date(2026, 11, 8), hour=5),
        expense("e0", 1200, currency="JPY", rate="0.0089", spent_on=date(2026, 11, 8), hour=1),
    ]
    settlements = [
        {
            "id": "s1",
            "created_at": BASE + timedelta(days=1),
            "from_user": "fake:b",
            "to_user": "fake:a",
            "currency": "SGD",
            "amount": 500,
        }
    ]
    book = build(CONVERT, expenses, settlements)
    assert [s["title"] for s in book.model["sheets"]] == [
        "Summary",
        "Transactions",
        "Repayments",
        "Ana",
        "Ben",
        "Cy",
        "Raw ledger",
    ]
    json.dumps(book.model)  # plain JSON contract
    tx = sheet(book, "Transactions")
    rows = table_rows(tx, "Date")
    assert [row[1]["v"] for row in rows] == ["Item e0", "Item e1", "Item e2", "Item e3"]
    assert tx["frozen_rows"] == header_index(tx, "Date") + 1
    header = values(tx["rows"][header_index(tx, "Date")])
    assert header[:8] == [
        "Date",
        "Description",
        "Paid by",
        "Currency",
        "Amount (original)",
        "FX rate to SGD",
        "Amount SGD",
        "FX status",
    ]
    assert header[8:10] == ["Ana share (original)", "Ana share SGD"]
    flat = json.dumps(tx["rows"])
    assert "trip-1" not in flat and '"e0"' not in flat  # no internal IDs in readable view
    first = rows[0]
    assert first[0] == {"k": "date", "v": "2026-11-08"}
    assert first[4] == {"k": "money", "v": "1200", "exp": 0}
    assert first[5] == {"k": "rate", "v": "0.0089"}
    assert first[6]["k"] == "money" and Decimal(first[6]["v"]) == Decimal("10.68")
    raw = sheet(book, "Raw ledger")
    raw_flat = json.dumps(raw["rows"])
    assert all(f'"e{i}"' in raw_flat for i in range(4)) and '"s1"' in raw_flat
    assert "fake:a" in raw_flat
    assert any(row and row[0] and row[0]["v"] == "repayment" for row in raw["rows"])


def test_summary_and_person_tabs_match_canonical_balances_and_rounding():
    expenses = [expense("r", 3, rate="0.333")]
    book = build(CONVERT, expenses)
    groups, _ = ledger_balances(CONVERT, expenses, [])
    rows = summary_rows(book)
    for key, label in NAMES.items():
        net = Decimal(rows[label][6]["v"])
        assert net == Decimal(groups["SGD"][key]) / 100
    assert sum(Decimal(row[6]["v"]) for row in rows.values()) == 0
    # The one-cent group adjustment is visible, not hidden in the totals.
    adjustments = {label: Decimal(row[5]["v"]) for label, row in rows.items()}
    assert sorted(adjustments.values()) == [Decimal("-0.01"), 0, 0]
    net, _ = convert_balances(expenses, [], "SGD")
    ana = table_rows(sheet(book, "Ana"), "Paid upfront SGD")[0]
    assert Decimal(ana[5]["v"]) == Decimal(net["fake:a"]) / 100
    assert ana[4] == rows["Ana"][5]
    # Exact settlement equivalents keep full precision (not pre-rounded).
    assert Decimal(rows["Ana"][1]["v"]) == Decimal("0.00999")
    total = next(r for r in table_rows(sheet(book, "Summary"), "Person") if r[0]["v"] == "Total")
    assert total[6] == {
        "k": "sum",
        "v": [total_first(book), total_first(book) + 2],
        "exp": 2,
        "s": "total",
    }


def total_first(book):
    return header_index(sheet(book, "Summary"), "Person") + 1


def test_owed_and_owe_labels():
    book = build(CONVERT, [expense("x", 900, paid_by="fake:a")])
    rows = summary_rows(book)
    assert rows["Ana"][7] == {"k": "text", "v": "Owed to you", "s": "pos"}
    assert rows["Ben"][7] == {"k": "text", "v": "You owe", "s": "neg"}


def test_missing_fx_is_flagged_and_excluded_without_understating_originals():
    expenses = [expense("ok", 900), expense("no", 600, currency="CNY", rate=None)]
    book = build(CONVERT, expenses)
    summary = sheet(book, "Summary")
    warnings = [
        row[0]["v"] for row in summary["rows"] if row and row[0] and row[0].get("s") == "warn"
    ]
    assert warnings and "1 expense has no stored FX rate to SGD (CNY 6.00)" in warnings[0]
    header = values(summary["rows"][header_index(summary, "Person")])
    paid_cny = header.index("Paid CNY (original)")
    assert Decimal(summary_rows(book)["Ana"][paid_cny]["v"]) == Decimal("6.00")
    tx_rows = table_rows(sheet(book, "Transactions"), "Date")
    missing = next(row for row in tx_rows if row[1]["v"] == "Item no")
    assert missing[5] is None and missing[6] is None
    assert missing[7] == {"k": "text", "v": "Missing FX rate", "s": "warn"}
    total = sheet(book, "Transactions")["rows"][-1]
    assert total[1]["v"] == "Total SGD (excludes 1 without FX rate)"
    assert "1 expense has no stored FX rate" in book.preview
    groups, unconverted = ledger_balances(CONVERT, expenses, [])
    assert [e["id"] for e in unconverted] == ["no"]
    assert Decimal(summary_rows(book)["Ana"][6]["v"]) == Decimal(groups["SGD"]["fake:a"]) / 100


def test_per_currency_mode_has_no_conversion_and_per_currency_blocks():
    trip = dict(CONVERT, settle_mode="per_currency")
    expenses = [expense("h", 900, rate=None), expense("j", 3000, currency="JPY", rate=None)]
    settlements = [
        {
            "id": "s",
            "created_at": BASE,
            "from_user": "fake:b",
            "to_user": "fake:a",
            "currency": "JPY",
            "amount": 1000,
        }
    ]
    book = build(trip, expenses, settlements)
    summary = sheet(book, "Summary")
    headers = [
        values(row) for row in summary["rows"] if row and row[0] and row[0].get("s") == "header"
    ]
    assert [h[1] for h in headers] == ["Paid upfront HKD", "Paid upfront JPY"]
    tx_header = values(
        sheet(book, "Transactions")["rows"][header_index(sheet(book, "Transactions"), "Date")]
    )
    assert not any("FX" in str(h) or "SGD" in str(h) for h in tx_header)
    groups, unconverted = ledger_balances(trip, expenses, settlements)
    assert not unconverted
    jpy_start = [
        i for i, row in enumerate(summary["rows"]) if row and row[0] and row[0].get("s") == "header"
    ][1]
    jpy_rows = {row[0]["v"]: row for row in summary["rows"][jpy_start + 1 : jpy_start + 4]}
    assert Decimal(jpy_rows["Ben"][5]["v"]) == Decimal(groups["JPY"]["fake:b"])
    assert jpy_rows["Ben"][3]["v"] == "1000"
    assert "Net HKD:" in book.preview and "Net JPY:" in book.preview


def test_general_ledger_without_trip():
    book = build(None, [expense("g", 900, rate=None)])
    assert sheet(book, "Summary")["rows"][0][0]["v"] == "General ledger — expense summary"
    assert book.model["title"] == "Expenses — General ledger"


def test_participants_include_repayment_only_and_former_members_with_unique_tabs():
    names = {"fake:a": "Summary", "fake:b": "Al/ex", "fake:c": "Al/ex", "fake:d": "\t"}
    expenses = [expense("x", 900, shares={"fake:a": 300, "fake:b": 300, "fake:c": 300})]
    settlements = [
        {
            "id": "s",
            "created_at": BASE,
            "from_user": "fake:d",
            "to_user": "fake:z",
            "currency": "SGD",
            "amount": 1,
        }
    ]
    book = build(CONVERT, expenses, settlements, names)
    titles = [s["title"] for s in book.model["sheets"]][3:-1]
    assert titles == ["Al ex", "Al ex (2)", "Former member 1", "Summary (2)", "Unnamed member 1"]
    assert len({t.casefold() for t in titles} | {t.casefold() for t in FIXED_TABS}) == 9
    labels = values(
        sheet(book, "Transactions")["rows"][header_index(sheet(book, "Transactions"), "Date")]
    )
    assert "Al/ex share SGD" in labels and "Al/ex (2) share SGD" in labels


def test_tab_names_are_safe_bounded_and_unique():
    names = tab_names(["Raw ledger", "a" * 90, "a" * 90, "x:y*z?[1]/'q'\\", "​", "TRANSACTIONS"])
    assert names[0] == "Raw ledger (2)"
    assert len(names[1]) == 50 and names[2].endswith("(2)") and len(names[2]) <= 50
    assert names[3] == "x y z 1 q"
    assert names[4] == "Person 5"
    assert names[5] == "TRANSACTIONS (2)"


def test_text_cells_are_literal_and_numbers_stay_numbers():
    book = build(CONVERT, [expense("x", 900, description='=IMPORTXML("http://x","//a")')])
    row = table_rows(sheet(book, "Transactions"), "Date")[0]
    assert row[1] == {"k": "text", "v": '=IMPORTXML("http://x","//a")', "s": "wrap"}
    assert row[4]["k"] == "money"
    kinds = {cell["k"] for tab in book.model["sheets"] for r in tab["rows"] for cell in r if cell}
    assert kinds <= {"text", "number", "rate", "money", "date", "datetime", "sum"}


def test_large_ledger_is_not_truncated():
    expenses = [expense(f"e{i:04d}", 100 + i, hour=i) for i in range(1200)]
    book = build(CONVERT, expenses)
    assert len(table_rows(sheet(book, "Transactions"), "Date")) == 1200
    assert book.expense_count == 1200


def test_too_many_participants_is_refused():
    shares = {f"fake:{i}": 1 for i in range(61)}
    with pytest.raises(ValueError, match="at most 60"):
        build(CONVERT, [expense("x", 61, shares=shares)], names={})


@pytest.fixture
async def context():
    stores = memory_stores()
    chat = ChatRef("fake", "group")
    for key, name in (("alice", "Alice"), ("bob", "Bob")):
        await stores.repo.upsert_member(chat, User(UserRef("fake", key), name, key))
    alice = User(UserRef("fake", "alice"), "Alice", "alice")
    return ToolContext(chat, alice, False, FakePlatform(), stores)


async def test_plugin_workbook_uses_one_snapshot_excludes_deleted_and_never_writes(context):
    ledger = MemoryLedgerRepo()
    actor = context.user.ref
    await ledger.commit(
        context.chat,
        "start_trip",
        {
            "name": "Japan",
            "countries": ["JP"],
            "currencies": ["JPY"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=actor,
    )
    trip = (await ledger.trips(context.chat))[0]
    for description in ("Kept", "Gone-item"):
        await ledger.commit(
            context.chat,
            "add_expense",
            {
                "description": description,
                "amount": 1000,
                "currency": "JPY",
                "paid_by": "fake:alice",
                "created_by": actor.key,
                "spent_on": "2026-11-08",
                "trip_id": trip["id"],
                "shares": {"fake:bob": 1000},
                "fx_rate_to_settle": "0.0089",
            },
            actor=actor,
        )
    deleted = next(e for e in ledger.expense_rows.values() if e["description"] == "Gone-item")
    deleted["deleted_at"] = NOW
    calls = []
    original = ledger.export_snapshot

    async def tracked(*args, **kwargs):
        calls.append(kwargs)
        return await original(*args, **kwargs)

    ledger.export_snapshot = tracked
    ledger.commit = None  # any write attempt would fail loudly
    plugin = ExpensesPlugin(ledger=ledger)
    result = await plugin.sheet_export_workbook(context, trip="Japan")
    assert calls == [{"trip_id": str(trip["id"]), "general_only": False}]
    assert result["expense_count"] == 1 and result["trip_id"] == str(trip["id"])
    assert result["chat_key"] == context.chat.key
    assert "Gone-item" not in json.dumps(result["workbook"])
    assert result["snapshot"] and "Japan" in result["preview"]
    other = ToolContext(
        ChatRef("fake", "other"), context.user, False, context.platform, context.stores
    )
    with pytest.raises(ValueError):
        await plugin.sheet_export_workbook(other, trip=str(trip["id"]))
    with pytest.raises(ValueError):
        await plugin.sheet_export_workbook(context, trip="Japan", general=True)


async def test_memory_snapshot_rejects_trip_from_another_chat():
    ledger = MemoryLedgerRepo()
    actor = UserRef("fake", "alice")
    chat = ChatRef("fake", "group")
    await ledger.commit(
        chat,
        "start_trip",
        {
            "name": "Japan",
            "countries": ["JP"],
            "currencies": ["JPY"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=actor,
    )
    trip = (await ledger.trips(chat))[0]
    with pytest.raises(ValueError, match="no longer exists"):
        await ledger.export_snapshot(ChatRef("fake", "x"), trip_id=trip["id"])
