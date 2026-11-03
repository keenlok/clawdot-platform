import csv
import io
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from zoneinfo import ZoneInfo

import pytest
from chatcore.events import ChatRef, User, UserRef
from chatcore.testing import FakePlatform, memory_stores
from chatcore.tools.registry import ToolContext
from chatcore_expenses.balances import convert_balances, ledger_balances
from chatcore_expenses.currency import format_amount
from chatcore_expenses.export import _major, build_export, export_caption
from chatcore_expenses.plugin import ExpensesPlugin
from chatcore_expenses.repository import MemoryLedgerRepo


async def _active_trip(ledger, chat):
    active = await ledger.trips(chat, statuses=("active",))
    return active[0] if len(active) == 1 else None


@pytest.fixture
async def context():
    stores = memory_stores()
    alice = User(UserRef("fake", "alice"), "Alice", "alice")
    bob = User(UserRef("fake", "bob"), "Bob", "bob")
    carol = User(UserRef("fake", "carol"), "Carol", "carol")
    chat = ChatRef("fake", "group")
    await stores.repo.upsert_member(chat, alice)
    await stores.repo.upsert_member(chat, bob)
    await stores.repo.upsert_member(chat, carol)
    return ToolContext(chat, alice, False, FakePlatform(), stores)


async def _start_trip(ledger, chat, actor, name="Japan", mode="convert", currencies=None):
    await ledger.commit(
        chat,
        "start_trip",
        {
            "name": name,
            "countries": ["JP"],
            "currencies": currencies or ["JPY"],
            "settle_mode": mode,
            "settle_currency": "SGD",
        },
        actor=actor,
    )
    return await _active_trip(ledger, chat)


async def _add_expense(
    ledger,
    chat,
    actor,
    description,
    amount,
    *,
    currency="JPY",
    paid_by="fake:alice",
    shares=None,
    trip_id=None,
    rate=None,
    spent_on=date(2026, 10, 1),
):
    payload = {
        "description": description,
        "amount": amount,
        "currency": currency,
        "paid_by": paid_by,
        "created_by": actor.key,
        "spent_on": spent_on.isoformat(),
        "trip_id": trip_id,
        "shares": shares if shares is not None else {"fake:bob": amount},
        "fx_rate_to_settle": rate,
    }
    await ledger.commit(chat, "add_expense", payload, actor=actor)
    return (await ledger.expenses(chat, trip_id=trip_id, general_only=trip_id is None, limit=None))[
        0
    ]


def _read(data):
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def _rows_by(rows, record):
    return [row for row in rows if row["record"] == record]


async def test_public_sheet_snapshot_is_scoped_and_preserves_original_currency_totals(context):
    ledger = MemoryLedgerRepo()
    trip = await _start_trip(ledger, context.chat, context.user.ref)
    await _add_expense(
        ledger, context.chat, context.user.ref, "Lunch", 1000, trip_id=trip["id"], rate="0.008"
    )
    plugin = ExpensesPlugin(ledger=ledger)
    result = await plugin.sheet_export_data(context, trip=str(trip["id"]))
    assert result["trip_id"] == str(trip["id"]) and result["chat_key"] == context.chat.key
    assert result["totals"] == {"JPY": "1000"}
    assert _read(result["csv"])[0]["description"] == "Lunch"
    other = ToolContext(
        ChatRef("fake", "other"), context.user, False, context.platform, context.stores
    )
    with pytest.raises(ValueError):
        await plugin.sheet_export_data(other, trip=str(trip["id"]))


def test_major_formats_currency_exponents_and_signs():
    assert _major(1250, "SGD") == "12.50"
    assert _major(3000, "JPY") == "3000"
    assert _major(-417, "SGD") == "-4.17"


def test_export_writes_convert_expense_and_share_rows():
    trip = {
        "id": "trip-id",
        "name": "Japan",
        "settle_mode": "convert",
        "settle_currency": "SGD",
    }
    expense = {
        "id": "expense-id",
        "spent_on": date(2026, 10, 1),
        "created_at": datetime(2026, 10, 1, tzinfo=UTC),
        "description": "Lunch",
        "paid_by": "fake:alice",
        "currency": "JPY",
        "amount": 1000,
        "fx_rate_to_settle": Decimal("0.00891230"),
        "shares": [{"user_key": "fake:bob", "amount": 1000}],
    }
    exported = build_export(
        trip,
        [expense],
        [],
        {"fake:alice": "Alice", "fake:bob": "Bob"},
        today=date(2026, 10, 31),
        timezone=UTC,
    )

    assert exported.filename == "expenses-japan-2026-10-31.csv"
    assert exported.expense_count == 1
    assert exported.settlement_count == 0
    assert exported.data.startswith(b"\xef\xbb\xbf")
    assert b"0.0089123" in exported.data
    assert b"8.9123" in exported.data
    assert b"fake:" not in exported.data
    assert export_caption(trip, 1, 0) == (
        "Japan (active): 1 expenses, 0 settlements. Deleted expenses are not included."
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("=SUM(A1)", "'=SUM(A1)"), ("+cmd", "'+cmd"), ("-abc", "'-abc"), ("@x", "'@x")],
)
def test_text_cells_escape_formula_prefixes(value, expected):
    from chatcore_expenses.export import _text

    assert _text(value) == expected


@pytest.mark.asyncio
async def test_convert_export_balances_match_ledger_and_balance_tool(context):
    ledger = MemoryLedgerRepo()
    trip = await _start_trip(ledger, context.chat, context.user.ref)
    await _add_expense(
        ledger,
        context.chat,
        context.user.ref,
        "first",
        1,
        trip_id=trip["id"],
        rate="0.0089123",
        shares={"fake:carol": 1},
    )
    await _add_expense(
        ledger,
        context.chat,
        UserRef("fake", "bob"),
        "second",
        1,
        trip_id=trip["id"],
        rate="0.006",
        paid_by="fake:bob",
        shares={"fake:carol": 1},
        spent_on=date(2026, 10, 2),
    )
    await _add_expense(
        ledger,
        context.chat,
        context.user.ref,
        "missing",
        500,
        trip_id=trip["id"],
        rate=None,
        spent_on=date(2026, 10, 3),
    )
    await ledger.commit(
        context.chat,
        "settle_up",
        {
            "from_user": "fake:bob",
            "to_user": "fake:alice",
            "amount": 100,
            "currency": "SGD",
            "trip_id": trip["id"],
        },
        actor=context.user.ref,
    )
    expenses = await ledger.expenses(context.chat, trip_id=trip["id"], limit=None)
    settlements = await ledger.settlements(context.chat, trip_id=trip["id"])
    names = {"fake:alice": "Alice", "fake:bob": "Bob", "fake:carol": "Carol"}
    exported = build_export(
        trip, expenses, settlements, names, today=date(2026, 10, 31), timezone=UTC
    )
    rows = _read(exported.data)
    expected, _ = ledger_balances(trip, expenses, settlements)
    actual = {row["person"]: int(Decimal(row["amount"]) * 100) for row in _rows_by(rows, "balance")}
    assert actual == {names[key]: amount for key, amount in expected["SGD"].items()}

    plugin = ExpensesPlugin(ledger=ledger)
    balance = await next(t for t in plugin.tools() if t.name == "get_balances").handler(context)
    for key, amount in expected["SGD"].items():
        if amount:
            amount_text = f"{abs(amount) / 100:.2f}"
            line = f"- {names[key]} {'is owed' if amount > 0 else 'owes'} S${amount_text} SGD"
            assert line in balance.content

    expense_rows = _rows_by(rows, "expense")
    shares = _rows_by(rows, "share")
    key_for_name = {name: key for key, name in names.items()}
    exact = {key: Decimal(0) for key in names}
    for row in expense_rows:
        if row["amount_in_settle"]:
            exact[key_for_name[row["person"]]] += Decimal(row["amount_in_settle"]) * 100
    for row in shares:
        if row["amount_in_settle"]:
            exact[key_for_name[row["person"]]] -= Decimal(row["amount_in_settle"]) * 100
    file_settlements = _rows_by(rows, "settlement")
    for row in file_settlements:
        minor = Decimal(row["amount"]) * 100
        exact[key_for_name[row["person"]]] += minor
        exact[key_for_name[row["counterparty"]]] -= minor
    independently_rounded = {
        key: int(value.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
        for key, value in exact.items()
    }
    assert sum(independently_rounded.values()) != 0
    assert {
        name: actual[name] - independently_rounded[key]
        for key, name in names.items()
        if actual[name] != independently_rounded[key]
    } == {"Carol": -1}

    recomputed, unconverted = convert_balances(
        [
            {
                "paid_by": next(key for key, name in names.items() if name == row["person"]),
                "amount": int(
                    Decimal(row["amount"]) * (10 ** (0 if row["currency"] == "JPY" else 2))
                ),
                "currency": row["currency"],
                "fx_rate_to_settle": row["fx_rate"] or None,
                "shares": [
                    {
                        "user_key": next(
                            key for key, name in names.items() if name == share["person"]
                        ),
                        "amount": int(
                            Decimal(share["amount"])
                            * (10 ** (0 if share["currency"] == "JPY" else 2))
                        ),
                    }
                    for share in shares
                    if share["id"] == row["id"]
                ],
            }
            for row in expense_rows
        ],
        [
            {
                "from_user": key_for_name[row["person"]],
                "to_user": key_for_name[row["counterparty"]],
                "amount": int(Decimal(row["amount"]) * 100),
                "currency": row["currency"],
            }
            for row in file_settlements
        ],
        "SGD",
    )
    assert recomputed == expected["SGD"]
    assert unconverted


@pytest.mark.asyncio
async def test_export_tool_scopes_active_trip_general_and_dm_destination(context):
    ledger = MemoryLedgerRepo()
    trip_a = await _start_trip(ledger, context.chat, context.user.ref, "A")
    trip_a_expense = await _add_expense(
        ledger, context.chat, context.user.ref, "trip A", 1000, trip_id=trip_a["id"], rate="1"
    )
    await ledger.commit(
        context.chat,
        "settle_up",
        {
            "from_user": "fake:bob",
            "to_user": "fake:alice",
            "amount": 2000,
            "currency": "SGD",
            "trip_id": trip_a["id"],
        },
        actor=context.user.ref,
    )
    trip_a_settlement = (await ledger.settlements(context.chat, trip_id=trip_a["id"]))[0]
    await ledger.commit(
        context.chat,
        "end_trip",
        {"trip_id": trip_a["id"], "expected_status": "active", "summary": ""},
        actor=context.user.ref,
    )
    trip_b = await _start_trip(ledger, context.chat, context.user.ref, "B")
    trip_b_expense = await _add_expense(
        ledger, context.chat, context.user.ref, "trip B", 1200, trip_id=trip_b["id"], rate="1"
    )
    await ledger.commit(
        context.chat,
        "settle_up",
        {
            "from_user": "fake:bob",
            "to_user": "fake:alice",
            "amount": 3000,
            "currency": "SGD",
            "trip_id": trip_b["id"],
        },
        actor=context.user.ref,
    )
    trip_b_settlement = (await ledger.settlements(context.chat, trip_id=trip_b["id"]))[0]
    general_expense = await _add_expense(
        ledger, context.chat, context.user.ref, "general item", 700, currency="SGD"
    )
    await ledger.commit(
        context.chat,
        "settle_up",
        {
            "from_user": "fake:bob",
            "to_user": "fake:alice",
            "amount": 4000,
            "currency": "SGD",
            "trip_id": None,
        },
        actor=context.user.ref,
    )
    general_settlement = (await ledger.settlements(context.chat, general_only=True))[0]

    other_chat = ChatRef("fake", "another-group")
    other_trip = await _start_trip(ledger, other_chat, context.user.ref, "other chat trip")
    other_trip_expense = await _add_expense(
        ledger,
        other_chat,
        context.user.ref,
        "foreign trip",
        800,
        trip_id=other_trip["id"],
        rate="1",
    )
    await ledger.commit(
        other_chat,
        "settle_up",
        {
            "from_user": "fake:bob",
            "to_user": "fake:alice",
            "amount": 8000,
            "currency": "SGD",
            "trip_id": other_trip["id"],
        },
        actor=context.user.ref,
    )
    other_trip_settlement = (await ledger.settlements(other_chat, trip_id=other_trip["id"]))[0]
    other_general_expense = await _add_expense(
        ledger, other_chat, context.user.ref, "foreign general", 900, currency="SGD"
    )
    await ledger.commit(
        other_chat,
        "settle_up",
        {
            "from_user": "fake:bob",
            "to_user": "fake:alice",
            "amount": 9000,
            "currency": "SGD",
            "trip_id": None,
        },
        actor=context.user.ref,
    )
    other_general_settlement = (await ledger.settlements(other_chat, general_only=True))[0]
    trip_a_ids = {trip_a_expense["id"], trip_a_settlement["id"]}
    trip_b_ids = {trip_b_expense["id"], trip_b_settlement["id"]}
    general_ids = {general_expense["id"], general_settlement["id"]}
    other_chat_ids = {
        other_trip_expense["id"],
        other_trip_settlement["id"],
        other_general_expense["id"],
        other_general_settlement["id"],
    }
    all_scope_ids = trip_a_ids | trip_b_ids | general_ids | other_chat_ids
    plugin = ExpensesPlugin(ledger=ledger)
    tool = next(t for t in plugin.tools() if t.name == "export_expenses")

    default = await tool.handler(context)
    assert default.content == (
        "Sent expenses-b-"
        f"{datetime.now(ZoneInfo('Asia/Singapore')).date().isoformat()}.csv: "
        "1 expenses, 1 settlements."
    )
    assert context.platform.sent_files[-1].caption == (
        "B (active): 1 expenses, 1 settlements. Deleted expenses are not included."
    )
    default_rows = _read(context.platform.sent_files[-1].data)
    assert {row["description"] for row in _rows_by(default_rows, "expense")} == {"trip B"}
    assert {row["amount"] for row in _rows_by(default_rows, "settlement")} == {"30.00"}
    assert all(row["trip_id"] == trip_b["id"] for row in default_rows)
    default_ids = {row["id"] for row in default_rows} - {""}
    assert default_ids <= trip_b_ids
    assert not default_ids & (all_scope_ids - trip_b_ids)

    selected_a = await tool.handler(context, trip="A")
    assert selected_a.content.startswith("Sent expenses-a-")
    a_rows = _read(context.platform.sent_files[-1].data)
    assert {row["description"] for row in _rows_by(a_rows, "expense")} == {"trip A"}
    assert {row["amount"] for row in _rows_by(a_rows, "settlement")} == {"20.00"}
    assert all(row["trip_id"] == trip_a["id"] for row in a_rows)
    a_ids = {row["id"] for row in a_rows} - {""}
    assert a_ids <= trip_a_ids
    assert not a_ids & (all_scope_ids - trip_a_ids)

    assert (await tool.handler(context, trip="other chat trip")).content.startswith(
        'Trip "other chat trip" not found.'
    )

    await tool.handler(context, general=True)
    general_rows = _read(context.platform.sent_files[-1].data)
    assert {row["description"] for row in _rows_by(general_rows, "expense")} == {"general item"}
    assert {row["amount"] for row in _rows_by(general_rows, "settlement")} == {"40.00"}
    assert all(
        row["trip_id"] == "" and row["trip_name"] == "General ledger" for row in general_rows
    )
    exported_general_ids = {row["id"] for row in general_rows} - {""}
    assert exported_general_ids <= general_ids
    assert not exported_general_ids & (all_scope_ids - general_ids)

    group = context.chat
    dm = ChatRef("fake", context.user.ref.id)
    context.chat = dm
    context.is_dm = True
    dm_platform = FakePlatform()
    context.platform = dm_platform
    plugin.group_for_dm = lambda _ctx: _resolved_group(group)
    sent = await next(t for t in plugin.tools() if t.name == "export_expenses").handler(
        context, trip="A"
    )
    assert sent.content.startswith("Sent expenses-a-")
    assert dm_platform.sent_files[0].chat == dm


async def _resolved_group(chat):
    return chat


@pytest.mark.asyncio
async def test_export_validation_and_platform_errors(context):
    plugin = ExpensesPlugin(ledger=MemoryLedgerRepo())
    tool = next(t for t in plugin.tools() if t.name == "export_expenses")
    assert (await tool.handler(context, trip="", general=False)).is_error
    assert (await tool.handler(context, trip=7)).is_error
    assert (await tool.handler(context, general="true")).is_error
    assert (await tool.handler(context, trip="A", general=True)).content == (
        "Choose a trip or the general ledger, not both."
    )
    assert (await tool.handler(context, trip="missing")).content.startswith(
        'Trip "missing" not found.'
    )

    class NoFilePlatform:
        capabilities = FakePlatform().capabilities

    context.platform = NoFilePlatform()
    assert (await tool.handler(context)).content == "This chat platform can't send files."


@pytest.mark.asyncio
async def test_per_currency_and_general_balances_are_exported(context):
    ledger = MemoryLedgerRepo()
    trip = await _start_trip(
        ledger,
        context.chat,
        context.user.ref,
        "Mixed",
        mode="per_currency",
        currencies=["JPY", "KRW"],
    )
    await _add_expense(ledger, context.chat, context.user.ref, "yen", 3000, trip_id=trip["id"])
    await _add_expense(
        ledger,
        context.chat,
        context.user.ref,
        "won",
        5000,
        currency="KRW",
        trip_id=trip["id"],
        spent_on=date(2026, 10, 2),
    )
    await _add_expense(ledger, context.chat, context.user.ref, "general yen", 2000)
    plugin = ExpensesPlugin(ledger=ledger)
    tool = next(t for t in plugin.tools() if t.name == "export_expenses")
    balance_tool = next(t for t in plugin.tools() if t.name == "get_balances")
    for kwargs, selected, general_only in (({}, trip, False), ({"general": True}, None, True)):
        if selected is None:
            await ledger.commit(
                context.chat,
                "end_trip",
                {"trip_id": trip["id"], "expected_status": "active", "summary": "finished"},
                actor=context.user.ref,
            )
        await tool.handler(context, **kwargs)
        rows = _read(context.platform.sent_files[-1].data)
        expenses = await ledger.expenses(
            context.chat,
            trip_id=selected["id"] if selected else None,
            general_only=general_only,
            limit=None,
        )
        settlements = await ledger.settlements(
            context.chat, trip_id=selected["id"] if selected else None, general_only=general_only
        )
        groups, _ = ledger_balances(selected, expenses, settlements)
        exported_balances = {
            (row["currency"], row["person"]): int(
                Decimal(row["amount"]) * (10 ** (0 if row["currency"] in {"JPY", "KRW"} else 2))
            )
            for row in _rows_by(rows, "balance")
        }
        expected = {
            (currency, {"fake:alice": "Alice", "fake:bob": "Bob"}[key]): amount
            for currency, values in groups.items()
            for key, amount in values.items()
        }
        assert exported_balances == expected
        balance_result = await balance_tool.handler(
            context, **({"trip": selected["name"]} if selected else {})
        )
        for row in _rows_by(rows, "balance"):
            minor = int(
                Decimal(row["amount"]) * (10 ** (0 if row["currency"] in {"JPY", "KRW"} else 2))
            )
            if minor:
                line = (
                    f"- {row['person']} {'is owed' if minor > 0 else 'owes'} "
                    f"{format_amount(abs(minor), row['currency'])}"
                )
                assert line in balance_result.content
        recomputed = {}
        for row in _rows_by(rows, "expense"):
            unit = 10 ** (0 if row["currency"] in {"JPY", "KRW"} else 2)
            key = (row["currency"], row["person"])
            recomputed[key] = recomputed.get(key, 0) + int(Decimal(row["amount"]) * unit)
        for row in _rows_by(rows, "share"):
            unit = 10 ** (0 if row["currency"] in {"JPY", "KRW"} else 2)
            key = (row["currency"], row["person"])
            recomputed[key] = recomputed.get(key, 0) - int(Decimal(row["amount"]) * unit)
        for row in _rows_by(rows, "settlement"):
            unit = 10 ** (0 if row["currency"] in {"JPY", "KRW"} else 2)
            source = (row["currency"], row["person"])
            target = (row["currency"], row["counterparty"])
            amount = int(Decimal(row["amount"]) * unit)
            recomputed[source] = recomputed.get(source, 0) + amount
            recomputed[target] = recomputed.get(target, 0) - amount
        assert recomputed == exported_balances
        if selected is None:
            assert {row["currency"] for row in _rows_by(rows, "expense")} == {"JPY"}
        else:
            assert {row["currency"] for row in _rows_by(rows, "expense")} == {"JPY", "KRW"}


@pytest.mark.asyncio
async def test_export_escapes_text_and_uses_former_member_labels(context):
    ledger = MemoryLedgerRepo()
    trip = await _start_trip(ledger, context.chat, context.user.ref)
    names = ["=Alice", "+Alice", "-Alice", "@Alice", "\tAlice", "\rAlice"]
    for index, name in enumerate(names):
        await context.stores.repo.upsert_member(
            context.chat, User(UserRef("fake", f"unsafe{index}"), name, f"unsafe{index}")
        )
    await _add_expense(
        ledger,
        context.chat,
        context.user.ref,
        '=SUM(A1), "quoted"\nsecond line',
        500,
        paid_by="fake:unsafe0",
        shares={"fake:former": 500},
        trip_id=trip["id"],
        rate="1",
    )
    for index, name in enumerate(names):
        await _add_expense(
            ledger,
            context.chat,
            context.user.ref,
            f"{name} expense",
            100,
            paid_by=f"fake:unsafe{index}",
            shares={"fake:bob": 100},
            trip_id=trip["id"],
            rate="1",
            spent_on=date(2026, 10, 2 + index),
        )
    await ledger.commit(
        context.chat,
        "settle_up",
        {
            "from_user": "fake:former",
            "to_user": "fake:unsafe1",
            "amount": 25,
            "currency": "SGD",
            "trip_id": trip["id"],
        },
        actor=context.user.ref,
    )
    expenses = await ledger.expenses(context.chat, trip_id=trip["id"], limit=None)
    settlements = await ledger.settlements(context.chat, trip_id=trip["id"])
    members = await context.stores.repo.find_members(context.chat)
    exported = build_export(
        trip,
        expenses,
        settlements,
        {m.ref.key: m.display_name for m in members},
        today=date(2026, 10, 31),
        timezone=UTC,
    )
    rows = _read(exported.data)
    descriptions = [row["description"] for row in _rows_by(rows, "expense")]
    assert '\'=SUM(A1), "quoted"\nsecond line' in descriptions
    assert all(description.startswith("'") for description in descriptions)
    assert _rows_by(rows, "share")[0]["person"] == "Former member 1"
    assert not any("fake:" in str(value) for row in rows for value in row.values())
    escaped_names = {"'=Alice", "'+Alice", "'-Alice", "'@Alice", "'\tAlice", "'\rAlice"}
    assert escaped_names <= {row["person"] for row in _rows_by(rows, "balance")}
    assert all(not row["amount"].startswith("'") for row in _rows_by(rows, "balance"))


@pytest.mark.asyncio
async def test_soft_deleted_expense_is_absent_and_balance_stays_current(context):
    ledger = MemoryLedgerRepo()
    trip = await _start_trip(ledger, context.chat, context.user.ref)
    live = await _add_expense(
        ledger, context.chat, context.user.ref, "live", 1000, trip_id=trip["id"], rate="1"
    )
    deleted = await _add_expense(
        ledger,
        context.chat,
        context.user.ref,
        "deleted secret",
        700,
        trip_id=trip["id"],
        rate="1",
        spent_on=date(2026, 10, 2),
    )
    await ledger.commit(
        context.chat,
        "delete_expense",
        {"expense_id": deleted["id"]},
        actor=context.user.ref,
    )
    plugin = ExpensesPlugin(ledger=ledger)
    await next(t for t in plugin.tools() if t.name == "export_expenses").handler(context)
    rows = _read(context.platform.sent_files[-1].data)
    assert {row["id"] for row in _rows_by(rows, "expense")} == {live["id"]}
    assert all("deleted secret" not in str(value) for row in rows for value in row.values())
    expenses = await ledger.expenses(context.chat, trip_id=trip["id"], limit=None)
    expected, _ = ledger_balances(trip, expenses, [])
    actual = {row["person"]: int(Decimal(row["amount"]) * 100) for row in _rows_by(rows, "balance")}
    assert actual == {
        "Alice": expected["SGD"]["fake:alice"],
        "Bob": expected["SGD"]["fake:bob"],
    }


@pytest.mark.asyncio
async def test_export_command_paths_and_missing_group(context):
    ledger = MemoryLedgerRepo()
    plugin = ExpensesPlugin(ledger=ledger)
    command = next(command for command in plugin.commands() if command.name == "export")

    async def _call(text):
        from chatcore.plugin import CommandContext

        return await command.handler(
            CommandContext(
                context.chat,
                context.user,
                context.is_dm,
                False,
                context.platform,
                context.stores,
                thread_id="17",
            ),
            text,
        )

    assert (await _call(" ")).startswith("Sent expenses-general-")
    assert context.platform.sent_files[-1].thread_id == "17"
    assert (await _call("general")).startswith("Sent expenses-general-")
    closed_trip = await _start_trip(ledger, context.chat, context.user.ref, "Closed Trip")
    await _add_expense(
        ledger,
        context.chat,
        context.user.ref,
        "closed-trip expense",
        1200,
        trip_id=closed_trip["id"],
        rate="1",
    )
    await ledger.commit(
        context.chat,
        "end_trip",
        {"trip_id": closed_trip["id"], "expected_status": "active", "summary": "finished"},
        actor=context.user.ref,
    )
    reply = await _call("Closed Trip")
    assert reply.startswith("Sent expenses-closed-trip-")
    closed_rows = _read(context.platform.sent_files[-1].data)
    assert {row["description"] for row in _rows_by(closed_rows, "expense")} == {
        "closed-trip expense"
    }
    assert (await _call("missing")).startswith('Trip "missing" not found.')
    context.is_dm = True
    plugin.group_for_dm = None
    assert "can’t identify your group" in (await _call("")).lower()
