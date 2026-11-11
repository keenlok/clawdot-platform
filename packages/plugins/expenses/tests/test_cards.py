from dataclasses import replace

import pytest
from chatcore.cards import Card
from chatcore.events import ChatRef
from chatcore.skills import SkillSet
from chatcore.testing import FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_expenses.cards import _settlement_block, balance_card, trip_summary_card
from chatcore_expenses.plugin import EXPENSES_INSTRUCTIONS, TRIPS_INSTRUCTIONS, ExpensesPlugin
from chatcore_expenses.repository import MemoryLedgerRepo


def test_balance_card_preserves_names_and_negative_minus():
    card = balance_card(
        None,
        {"SGD": "S$12.00 SGD", "JPY": "¥300 JPY"},
        {"SGD": {"tg:a": 1200, "tg:b": -1200}, "JPY": {"tg:a": -300, "tg:b": 300}},
        {"tg:a": "陈明", "tg:b": "Alex"},
        expenses=[
            {
                "currency": "SGD",
                "amount": 1200,
                "paid_by": "tg:a",
                "shares": [{"user_key": "tg:b", "amount": 1200}],
            },
            {
                "currency": "JPY",
                "amount": 300,
                "paid_by": "tg:b",
                "shares": [{"user_key": "tg:a", "amount": 300}],
            },
        ],
    )
    tables = [block for block in card.blocks if block.type == "table"]
    sgd = next(table for table in tables if table.columns[-1].header == "Net (SGD)")
    assert [column.header for column in sgd.columns] == ["Member", "Status", "Net (SGD)"]
    assert [column.align for column in sgd.columns] == ["start", "start", "end"]
    assert sgd.rows == [["陈明", "is owed", "+12.00"], ["Alex", "owes", "−12.00"]]
    assert any("−" in cell for table in tables for row in table.rows for cell in row)


def test_trip_summary_adds_category_or_day_breakdown():
    card = trip_summary_card(
        {"name": "Trip"},
        {"SGD": "S$1.00 SGD"},
        {"SGD": {"a": 100, "b": -100}},
        {"a": "Sam"},
        expenses=[
            {
                "currency": "SGD",
                "amount": 100,
                "paid_by": "a",
                "shares": [{"user_key": "b", "amount": 100}],
                "spent_on": "2026-01-01",
                "category": "Food",
            }
        ],
    )
    assert card.blocks[-1].type == "table"
    assert card.blocks[-1].rows == [["Food (SGD)", "1.00"]]


def test_balance_card_uses_ledger_figures_and_emits_fx_or_settled_callout():
    missing = {"id": "e1"}
    card = balance_card(
        {"name": "Japan"},
        {"SGD": "S$12.00 SGD"},
        {"SGD": {"a": 1200, "b": -1200, "c": 0}},
        {"a": "Alex", "b": "Alexa", "c": "Sam", "d": "Omitted"},
        expenses=[{"currency": "SGD", "amount": 1200, "paid_by": "a", "shares": []}],
        unconverted=[missing],
    )
    table = next(block for block in card.blocks if block.type == "table")
    assert table.rows == [
        ["Alex", "is owed", "+12.00"],
        ["Alexa", "owes", "−12.00"],
        ["Everyone else", "settled", "0.00"],
    ]
    warning = next(block for block in card.blocks if block.type == "callout")
    assert warning.tone == "warning"
    assert warning.text == "1 expense(s) have no FX rate yet; balances exclude them."
    settled = balance_card(None, {}, {"SGD": {"a": 0, "b": 0}}, {"a": "Alex", "b": "Alexa"})
    callout = next(block for block in settled.blocks if block.type == "callout")
    assert (callout.tone, callout.text) == ("good", "All balances are settled.")


def test_settlement_transfers_use_bullets_at_12_and_table_after_12():
    names = {f"u{i}": f"Member {i}" for i in range(82)}
    twelve = [(f"u{i}", f"u{i + 12}", 123450) for i in range(12)]
    bullets = _settlement_block(twelve, names, "SGD")
    assert bullets["type"] == "bullets" and len(bullets["items"]) == 12

    thirteen = [*twelve, ("u24", "u25", 420000000)]
    table = _settlement_block(thirteen, names, "JPY")
    assert table["type"] == "table"
    assert table["columns"] == [
        {"header": "From"},
        {"header": "To"},
        {"header": "Amount (JPY)", "align": "end"},
    ]
    assert len(table["rows"]) == 13
    assert table["rows"][-1] == ["Member 24", "Member 25", "420,000,000"]


def test_41_settlements_fail_instead_of_omitting_transfers():
    from chatcore.cards import CardError

    transfers = [(f"d{i}", f"c{i}", i + 1) for i in range(41)]
    names = {user: user for pair in transfers for user in pair[:2]}
    with pytest.raises(CardError) as error:
        _settlement_block(transfers, names, "SGD")
    assert error.value.code == "invalid"


async def test_41_transfers_return_text_fallback_instead_of_a_partial_card():
    chat = ChatRef("fake", "many-settlements")
    stores, ledger = memory_stores(), MemoryLedgerRepo()
    actor = user("requester")
    await stores.repo.upsert_member(chat, actor)
    for index in range(41):
        creditor, debtor = user(f"creditor{index:02}"), user(f"debtor{index:02}")
        await stores.repo.upsert_member(chat, creditor)
        await stores.repo.upsert_member(chat, debtor)
        await ledger.commit(
            chat,
            "add_expense",
            {
                "description": f"expense {index}",
                "amount": 100,
                "currency": "SGD",
                "paid_by": creditor.ref.key,
                "created_by": creditor.ref.key,
                "spent_on": "2026-11-01",
                "trip_id": None,
                "shares": {debtor.ref.key: 100},
                "fx_rate_to_settle": None,
            },
            actor=actor.ref,
        )
    client = _CardClient()
    plugin = ExpensesPlugin(ledger=ledger, cards=client)
    ctx = ToolContext(chat, actor, False, FakePlatform("fake"), stores, turn=TurnState())
    result = await next(
        tool for tool in plugin.tools() if tool.name == "send_balance_card"
    ).handler(ctx, general=True)
    assert result.content.startswith(
        "Too many entries for a card; here are the balances as text.\n"
    )
    assert result.content.count(" pays ") == 41
    assert client.calls == 0


def test_table_amounts_keep_currency_exponent_and_group_digits():
    net = balance_card(None, {}, {"USD": {"a": 123450, "b": -123450}}, {"a": "Alex", "b": "Sam"})
    usd = next(block for block in net.blocks if block.type == "table")
    assert [row[2] for row in usd.rows] == ["+1,234.50", "−1,234.50"]
    jpy = balance_card(None, {}, {"JPY": {"a": 4200000, "b": -4200000}}, {"a": "Alex", "b": "Sam"})
    jpy_table = next(block for block in jpy.blocks if block.type == "table")
    assert [row[2] for row in jpy_table.rows] == ["+4,200,000", "−4,200,000"]

    category = trip_summary_card(
        {"name": "Trip"},
        {},
        {},
        {},
        expenses=[
            {"category": "Stay", "currency": "USD", "amount": 420000000, "spent_on": "2026-01-01"}
        ],
    )
    assert category.blocks[-1].rows == [["Stay (USD)", "4,200,000.00"]]
    daily = trip_summary_card(
        {"name": "Trip"},
        {},
        {},
        {},
        expenses=[
            {"category": None, "currency": "JPY", "amount": 4200000, "spent_on": "2026-01-01"}
        ],
    )
    assert daily.blocks[-1].rows == [["2026-01-01 (JPY)", "4,200,000"]]


def test_uncategorized_expenses_are_in_the_category_breakdown():
    expenses = [
        {"currency": "SGD", "amount": 100, "spent_on": "2026-01-01", "category": "Food"},
        {"currency": "SGD", "amount": 200, "spent_on": "2026-01-02", "category": None},
    ]
    card = trip_summary_card({"name": "Trip"}, {}, {}, {}, expenses=expenses)
    assert card.blocks[-1].rows == [["Uncategorized (SGD)", "2.00"], ["Food (SGD)", "1.00"]]


def test_expense_skillset_validates_and_instructions_do_not_change_with_cards():
    class CardsClient:
        async def render(self, *args, **kwargs):
            return [b"png"]

        async def aclose(self):
            pass

    plain = ExpensesPlugin()
    with_cards = ExpensesPlugin(cards=CardsClient())
    for plugin in (plain, with_cards):
        SkillSet.build([plugin], ToolRegistry(plugin.tools()))
    assert [skill.instructions for skill in plain.skills()] == [
        skill.instructions for skill in with_cards.skills()
    ]
    assert with_cards.skills()[0].instructions == EXPENSES_INSTRUCTIONS
    assert with_cards.skills()[1].instructions == TRIPS_INSTRUCTIONS
    assert len(EXPENSES_INSTRUCTIONS) <= 800 and len(TRIPS_INSTRUCTIONS) <= 800
    tools = {tool.name: tool for tool in with_cards.tools()}
    assert tools["send_balance_card"].description == (
        "Send balances and who-pays-whom as one card image; figures come from the ledger. "
        "Use when someone asks for a card or picture of balances."
    )
    assert tools["send_trip_summary_card"].description == (
        "Send a trip summary card (balances plus spend by category or day); "
        "figures come from the ledger."
    )


class _CardClient:
    def __init__(self, pages=None):
        self.pages = pages or [b"png"]
        self.cards = []
        self.calls = 0

    async def render(self, card, *, theme="light", footer=None):
        self.cards.append(card)
        self.calls += 1
        return self.pages

    async def aclose(self):
        pass


async def _expense_context(ledger, *, chat=None, platform=None, user_value=None):
    stores = memory_stores()
    chat = chat or ChatRef("fake", "card-group")
    actor = user_value or user("alex")
    await stores.repo.upsert_member(chat, actor)
    return ToolContext(chat, actor, False, platform or FakePlatform(), stores, turn=TurnState())


async def test_card_tools_validate_trip_scope_attempt_once_and_recheck_group_scope():
    ledger = MemoryLedgerRepo()
    ctx = await _expense_context(ledger)
    client = _CardClient()
    plugin = ExpensesPlugin(ledger=ledger, cards=client)
    tools = {tool.name: tool for tool in plugin.tools()}
    assert "send_balance_card" in tools and "send_trip_summary_card" in tools
    first = await tools["send_balance_card"].handler(ctx, trip="missing")
    expected = await plugin._export(ctx, trip="missing")
    assert first.content == expected.content
    conflict = await tools["send_balance_card"].handler(ctx, trip="x", general=True)
    export_conflict = await plugin._export(ctx, trip="x", general=True)
    assert conflict.content == export_conflict.content

    ctx.turn = TurnState()
    sent = await tools["send_balance_card"].handler(ctx, general=True)
    assert not sent.is_error
    again = await tools["send_trip_summary_card"].handler(ctx)
    assert again.is_error and client.calls == 1

    group_a = ChatRef("fake", "linked-a")
    group_b = ChatRef("fake", "linked-b")
    resolver_values = iter([group_a, group_b])

    async def resolver(_ctx):
        return next(resolver_values)

    dm = await _expense_context(ledger, chat=ChatRef("fake", "dm"))
    dm = ToolContext(dm.chat, dm.user, True, dm.platform, dm.stores, turn=TurnState())
    plugin = ExpensesPlugin(ledger=ledger, cards=client, group_for_dm=resolver)
    result = await next(
        tool for tool in plugin.tools() if tool.name == "send_balance_card"
    ).handler(dm, general=True)
    assert result.content == "Card delivery is no longer allowed."
    assert client.calls == 2
    assert not dm.platform.sent_photos and not dm.platform.sent_files

    calls = 0

    async def failed_resolver(_ctx):
        nonlocal calls
        calls += 1
        if calls == 1:
            return group_a
        raise RuntimeError("scope lookup failed")

    dm.turn = TurnState()
    plugin = ExpensesPlugin(ledger=ledger, cards=client, group_for_dm=failed_resolver)
    result = await next(
        tool for tool in plugin.tools() if tool.name == "send_balance_card"
    ).handler(dm, general=True)
    assert result.content == "Card delivery is no longer allowed."
    assert not dm.platform.sent_photos and not dm.platform.sent_files


async def test_real_converted_ledger_card_matches_balance_text_and_warns_for_missing_fx():
    chat = ChatRef("fake", "ledger-card")
    stores, ledger = memory_stores(), MemoryLedgerRepo()
    alex = replace(user("alex"), display_name="Alex")
    alexa = replace(user("alexa"), display_name="Alexa")
    for member in (alex, alexa):
        await stores.repo.upsert_member(chat, member)
    await ledger.commit(
        chat,
        "start_trip",
        {
            "name": "Japan",
            "countries": ["JP"],
            "currencies": ["JPY", "SGD"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
            "members": [alex.ref.key, alexa.ref.key],
        },
        actor=alex.ref,
    )
    trip = await ledger.trip(chat)
    for description, amount, currency, payer, shares, rate in (
        ("rated yen", 1234, "JPY", alex, {alexa.ref.key: 1234}, "0.01"),
        ("singapore dollars", 3000, "SGD", alexa, {alex.ref.key: 3000}, "1"),
        ("missing yen", 500, "JPY", alex, {alexa.ref.key: 500}, None),
    ):
        await ledger.commit(
            chat,
            "add_expense",
            {
                "description": description,
                "amount": amount,
                "currency": currency,
                "paid_by": payer.ref.key,
                "created_by": payer.ref.key,
                "spent_on": "2026-11-01",
                "trip_id": trip["id"],
                "shares": shares,
                "fx_rate_to_settle": rate,
            },
            actor=payer.ref,
        )
    await ledger.commit(
        chat,
        "settle_up",
        {
            "from_user": alexa.ref.key,
            "to_user": alex.ref.key,
            "amount": 100,
            "currency": "SGD",
            "trip_id": trip["id"],
        },
        actor=alex.ref,
    )
    client, platform = _CardClient(), FakePlatform("fake")
    plugin = ExpensesPlugin(ledger=ledger, cards=client)
    ctx = ToolContext(chat, alex, False, platform, stores, turn=TurnState())
    expected = (await plugin._balances(ctx)).content
    result = await next(
        tool for tool in plugin.tools() if tool.name == "send_balance_card"
    ).handler(ctx, trip="Japan")
    assert not result.is_error
    card = client.cards[0]
    table = next(
        block
        for block in card.blocks
        if block.type == "table" and block.columns[-1].header == "Net (SGD)"
    )
    expected_balances = {}
    expected_transfers = []
    import re

    for name, status, amount in re.findall(
        r"- (Alex(?:a)?) (is owed|owes) S\$(\d+\.\d+) SGD", expected
    ):
        expected_balances[name] = (status, amount)
    for source, target, amount in re.findall(
        r"- (Alex(?:a)?) pays (Alex(?:a)?) S\$(\d+\.\d+) SGD", expected
    ):
        expected_transfers.append(f"{source} pays {target} S${amount} SGD")
    from decimal import Decimal

    assert {
        row[0]: (row[1], Decimal(row[2].lstrip("+−").replace(",", ""))) for row in table.rows
    } == {
        name: (status, Decimal(amount.replace(",", "")))
        for name, (status, amount) in expected_balances.items()
    }
    transfers = next(block for block in card.blocks if block.type == "bullets")
    assert list(transfers.items) == expected_transfers
    assert set(row[0] for row in table.rows) == {"Alex", "Alexa"}
    warning = next(block for block in card.blocks if block.type == "callout")
    assert warning.text == "1 expense(s) have no FX rate yet; balances exclude them."
    stats = next(block for block in card.blocks if block.type == "stats")
    assert {tile.label: tile.value for tile in stats.tiles} == {
        "Spend JPY": "1734",
        "Spend SGD": "30",
        "Expenses": "3",
    }


async def test_file_only_expense_platform_uses_send_file():
    class FileOnly:
        name = "fake"

        def __init__(self):
            self.files = []

        async def send_file(self, chat, filename, data, *, caption=None, thread_id=None):
            self.files.append((filename, data, caption))

    chat = ChatRef("fake", "file-card")
    stores, actor, ledger = memory_stores(), user("alex"), MemoryLedgerRepo()
    await stores.repo.upsert_member(chat, actor)
    platform, client = FileOnly(), _CardClient()
    plugin = ExpensesPlugin(ledger=ledger, cards=client)
    ctx = ToolContext(chat, actor, False, platform, stores, turn=TurnState())
    result = await next(
        tool for tool in plugin.tools() if tool.name == "send_balance_card"
    ).handler(ctx, general=True)
    assert not result.is_error
    assert platform.files[0][2] == "📋 Balances · General ledger"


@pytest.mark.parametrize("builder_error", ["card", "model"])
async def test_invalid_card_limit_falls_back_to_normal_balance_text(monkeypatch, builder_error):
    from chatcore.cards import CardError
    from chatcore_expenses import plugin as module

    ledger = MemoryLedgerRepo()
    ctx = await _expense_context(ledger)
    await ledger.commit(
        ctx.chat,
        "add_expense",
        {
            "description": "shared",
            "amount": 1000,
            "currency": "SGD",
            "paid_by": ctx.user.ref.key,
            "created_by": ctx.user.ref.key,
            "spent_on": "2026-11-01",
            "trip_id": None,
            "shares": {"fake:bob": 1000},
            "fx_rate_to_settle": None,
        },
        actor=ctx.user.ref,
    )

    def invalid(*args, **kwargs):
        if builder_error == "card":
            raise CardError("invalid", "too many entries")
        Card.model_validate({"title": "", "blocks": []})

    monkeypatch.setattr(module, "balance_card", invalid)
    plugin = ExpensesPlugin(ledger=ledger, cards=_CardClient())
    expected = await plugin._balance_text(ctx, ledger, ctx.chat, None)
    result = await next(
        tool for tool in plugin.tools() if tool.name == "send_balance_card"
    ).handler(ctx, general=True)
    assert (
        result.content == "Too many entries for a card; here are the balances as text.\n" + expected
    )


async def test_renderer_size_limit_falls_back_to_normal_balance_text():
    from chatcore.cards import CardError

    ledger = MemoryLedgerRepo()
    ctx = await _expense_context(ledger)
    await ledger.commit(
        ctx.chat,
        "add_expense",
        {
            "description": "shared",
            "amount": 1000,
            "currency": "SGD",
            "paid_by": ctx.user.ref.key,
            "created_by": ctx.user.ref.key,
            "spent_on": "2026-11-01",
            "trip_id": None,
            "shares": {"fake:bob": 1000},
            "fx_rate_to_settle": None,
        },
        actor=ctx.user.ref,
    )

    class TooLargeClient(_CardClient):
        async def render(self, card, *, theme="light", footer=None):
            raise CardError("too_large")

    plugin = ExpensesPlugin(ledger=ledger, cards=TooLargeClient())
    expected = await plugin._balance_text(ctx, ledger, ctx.chat, None)
    result = await next(
        tool for tool in plugin.tools() if tool.name == "send_balance_card"
    ).handler(ctx, general=True)
    assert (
        result.content == "Too many entries for a card; here are the balances as text.\n" + expected
    )


async def test_no_card_client_keeps_tools_skills_and_gates_unchanged():
    first, second = ExpensesPlugin(), ExpensesPlugin(cards=None)
    first_tools, second_tools = first.tools(), second.tools()
    assert [
        (tool.name, tool.description, tool.parameters, tool.writes) for tool in first_tools
    ] == [(tool.name, tool.description, tool.parameters, tool.writes) for tool in second_tools]
    assert first.skills() == second.skills()
    assert [
        tool.name for tool in first_tools if tool.name.startswith("send_") and "card" in tool.name
    ] == []
    assert first.tool_gates().keys() == second.tool_gates().keys()


def test_no_card_expenses_baseline_matches_committed_tool_skill_and_gate_names():
    pre_card_tools = [
        "add_expense",
        "update_expense",
        "restore_expense",
        "undo_expense_change",
        "update_trip_members",
        "delete_expense",
        "settle_up",
        "set_fx_rate",
        "start_trip",
        "update_trip",
        "end_trip",
        "list_expenses",
        "get_balances",
        "member_expenses",
        "export_expenses",
        "list_trips",
        "get_trip",
        "activate_trip",
        "reopen_trip",
        "set_default_trip",
    ]
    expense_tools = (
        "add_expense",
        "delete_expense",
        "list_expenses",
        "get_balances",
        "member_expenses",
        "settle_up",
        "export_expenses",
        "read_receipt",
        "get_trip",
        "update_expense",
        "restore_expense",
        "undo_expense_change",
    )
    trip_tools = (
        "start_trip",
        "update_trip",
        "end_trip",
        "get_trip",
        "set_fx_rate",
        "get_balances",
        "list_trips",
        "activate_trip",
        "reopen_trip",
        "set_default_trip",
        "update_trip_members",
    )
    plugin = ExpensesPlugin(cards=None)
    assert [tool.name for tool in plugin.tools()] == pre_card_tools
    skills = {skill.name: skill for skill in plugin.skills()}
    assert skills["expenses"].tools == expense_tools
    assert skills["expenses"].instructions == EXPENSES_INSTRUCTIONS
    assert skills["trips"].tools == trip_tools
    assert skills["trips"].instructions == TRIPS_INSTRUCTIONS
    assert set(plugin.tool_gates()) == set(pre_card_tools)
    assert "send_balance_card" not in pre_card_tools
    assert "send_trip_summary_card" not in pre_card_tools
