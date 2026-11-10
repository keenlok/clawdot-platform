"""Audit actual plugin tools at the model argument boundary, with scoped fake stores."""

from __future__ import annotations

import copy
import inspect
import json

import pytest
from chatcore.access import AccessPolicy
from chatcore.conversation import ConversationPlugin
from chatcore.embed import FakeEmbedder
from chatcore.events import Attachment, AttachmentKind, ChatRef
from chatcore.privacy import PrivacyPlugin
from chatcore.testing import FakePlatform, memory_stores, message, user
from chatcore.tools.registry import ToolContext, ToolRegistry
from chatcore_browse.plugin import BrowsePlugin
from chatcore_browse.settings import BrowseSettings
from chatcore_expenses.plugin import ExpensesPlugin
from chatcore_expenses.repository import MemoryLedgerRepo
from chatcore_extensions.authority import ExtensionAuthority
from chatcore_extensions.plugin import ExtensionsPlugin
from chatcore_extensions.repository import MemoryExtensionRepo
from chatcore_extensions.service import ExtensionService
from chatcore_jobs.plugin import JobsPlugin
from chatcore_jobs.settings import JobsSettings
from chatcore_memory import MemoryPlugin
from chatcore_polls.plugin import PollsPlugin
from chatcore_polls.settings import PollsSettings
from chatcore_search import SearchPlugin
from chatcore_stocks import StocksPlugin
from chatcore_web import WebPlugin, WebSettings

FORBIDDEN = {
    "source",
    "created_by",
    "requested_by",
    "requester",
    "user",
    "user_key",
    "chat",
    "chat_key",
    "chat_id",
    "is_admin",
    "is_dm",
    "pending",
    "confirm",
    "skip_confirm",
}


async def _admin(_chat, _user):
    return False


def plugins():
    extension_repo = MemoryExtensionRepo()
    extension_service = ExtensionService(extension_repo, object(), object(), enabled=lambda: True)
    extension = ExtensionsPlugin(
        extension_repo,
        extension_service,
        ExtensionAuthority(lambda _chat: True, _admin),
        enabled=lambda: True,
    )
    selected = [
        ConversationPlugin(),
        StocksPlugin(),
        WebPlugin(WebSettings(TAVILY_API_KEY="fake-tavily-secret", _env_file=None)),
        SearchPlugin(FakeEmbedder()),
        MemoryPlugin(),
        ExpensesPlugin(ledger=MemoryLedgerRepo()),
        extension,
    ]
    return [*selected, PrivacyPlugin(selected, provider_label="test")]


def remaining_write_tools():
    platform = FakePlatform()
    polls = PollsPlugin(PollsSettings(), AccessPolicy(["fake:a"], []), platform)
    jobs = JobsPlugin(
        JobsSettings(), AccessPolicy(["fake:a"], []), platform, sender=lambda *_args: None
    )
    return [*polls.tools(), *jobs.tools()]


TOOLS = [t for p in plugins() for t in p.tools()]


@pytest.fixture
async def audit():
    stores = memory_stores()
    selected = plugins()
    registry = ToolRegistry(t for p in selected for t in p.tools())
    ctx = ToolContext(ChatRef("fake", "a"), user(), False, FakePlatform(), stores)
    await stores.repo.upsert_member(ctx.chat, ctx.user)
    return selected, registry, ctx


def snapshot(selected, ctx):
    memory = next(p for p in selected if p.name == "memory")
    ledger = next(p for p in selected if p.name == "expenses").ledger
    return copy.deepcopy((vars(ctx.stores.repo), vars(ledger), vars(memory._repo(ctx.stores))))


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_schema_and_handler_signature(tool):
    properties = set(tool.parameters.get("properties", {}))
    assert not properties & FORBIDDEN
    signature = inspect.signature(tool.handler)
    assert not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
    assert set(signature.parameters) - {"self", "ctx"} <= properties


@pytest.mark.parametrize("name", [t.name for t in TOOLS])
@pytest.mark.parametrize(
    "extra", [{"is_admin": True}, {"chat_key": "tg:-999"}, {"created_by": "tg:1"}]
)
async def test_injected_arguments_rejected_without_writes(audit, name, extra):
    selected, registry, ctx = audit
    before = snapshot(selected, ctx)
    result = await registry.call(name, extra, ctx)
    assert result.is_error
    assert result.content == "unknown argument(s): " + ", ".join(extra)
    assert snapshot(selected, ctx) == before
    assert result.pending_action is None


@pytest.mark.parametrize("name", [t.name for t in TOOLS if t.parameters.get("required")])
async def test_missing_required_arguments_are_errors(audit, name):
    _, registry, ctx = audit
    result = await registry.call(name, {}, ctx)
    assert result.is_error
    required = sorted(registry.get(name).parameters["required"])
    assert result.content == "missing required argument(s): " + ", ".join(required)


async def seed_expense(registry, ctx):
    tool = registry.get("add_expense")
    result = await tool.handler(
        ctx, description="private lunch", amount="10", paid_by="me", split_with=["me"]
    )
    # Commit through the same plugin instance as the registered handler.
    plugin = tool.handler.__self__
    await plugin.action_handlers()["add_expense"].commit(result.pending_action, ctx)
    return (await plugin.ledger.expenses(ctx.chat))[0]["id"]


@pytest.mark.parametrize(
    "name",
    [
        "delete_expense",
        "update_expense",
        "restore_expense",
        "undo_expense_change",
        "update_trip_members",
        "read_attachment",
        "forget",
        "list_expenses",
        "get_balances",
        "member_expenses",
        "export_expenses",
        "list_trips",
        "get_trip",
        "add_expense",
        "settle_up",
        "set_fx_rate",
        "update_trip",
        "end_trip",
        "activate_trip",
        "reopen_trip",
        "set_default_trip",
        "read_chat_history",
    ],
)
async def test_cross_chat_identifiers(audit, name):
    selected, registry, ctx = audit
    expense_id = await seed_expense(registry, ctx)
    remembered = await registry.call("remember", {"fact": "private hiking arrangement"}, ctx)
    assert not remembered.is_error
    memory = next(p for p in selected if p.name == "memory")
    from chatcore.plugin import CommandContext

    await memory.action_handlers()["remember"].commit(
        remembered.pending_action,
        CommandContext(ctx.chat, ctx.user, ctx.is_dm, False, ctx.platform, ctx.stores),
    )
    memory_id = (await memory._repo(ctx.stores).list(ctx.chat))[0]["id"]
    msg = message(
        "private group A history",
        sender=user("only_a"),
        chat=ctx.chat,
        attachments=[
            Attachment(
                "private-file",
                AttachmentKind.DOCUMENT,
                filename="private.txt",
                mime_type="text/plain",
            )
        ],
    )
    await ctx.stores.repo.log_message(msg)
    await ctx.stores.repo.upsert_member(ctx.chat, msg.user)
    expense_plugin = next(p for p in selected if p.name == "expenses")
    await expense_plugin.ledger.commit(
        ctx.chat,
        "start_trip",
        {
            "name": "private A trip",
            "countries": ["SG"],
            "currencies": ["SGD"],
            "settle_mode": "per_currency",
            "settle_currency": "SGD",
        },
        actor=ctx.user.ref,
    )
    trip_id = (await expense_plugin.ledger.trips(ctx.chat, statuses=("active",)))[0]["id"]
    await expense_plugin.ledger.commit(
        ctx.chat,
        "start_trip",
        {
            "name": "private planned trip",
            "countries": ["JP"],
            "currencies": ["JPY"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
            "status": "planned",
        },
        actor=ctx.user.ref,
    )
    planned_id = (await expense_plugin.ledger.trip(ctx.chat, "private planned trip"))["id"]
    await expense_plugin.ledger.commit(
        ctx.chat,
        "start_trip",
        {
            "name": "private closed trip",
            "countries": ["JP"],
            "currencies": ["JPY"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
        },
        actor=ctx.user.ref,
    )
    closed = await expense_plugin.ledger.trip(ctx.chat, "private closed trip")
    await expense_plugin.ledger.commit(
        ctx.chat,
        "end_trip",
        {"trip_id": closed["id"], "expected_status": "active"},
        actor=ctx.user.ref,
    )
    closed_id = closed["id"]
    args = {
        "delete_expense": {"expense_id": expense_id},
        "update_expense": {"expense_id": expense_id, "amount": "11"},
        "restore_expense": {"expense_id": expense_id},
        "undo_expense_change": {},
        "update_trip_members": {"trip": trip_id, "add": ["me"]},
        "read_attachment": {"message_id": msg.message_id},
        "forget": {"memory_id": memory_id},
        "list_expenses": {"trip": trip_id},
        "get_balances": {"trip": trip_id},
        "member_expenses": {"member": "me", "trip": trip_id},
        "export_expenses": {"trip": trip_id},
        "list_trips": {},
        "get_trip": {"trip": trip_id},
        "add_expense": {
            "description": "coffee",
            "amount": "10",
            "paid_by": "me",
            "split_with": ["me"],
            "trip": trip_id,
        },
        "settle_up": {
            "from_user": "me",
            "to_user": "only_a",
            "amount": "10",
            "trip": trip_id,
        },
        "set_fx_rate": {"currency": "JPY", "rate": "0.01", "trip": trip_id},
        "update_trip": {"trip": trip_id, "add_currencies": ["HKD"]},
        "end_trip": {"trip": trip_id},
        "activate_trip": {"trip": planned_id},
        "reopen_trip": {"trip": closed_id},
        "set_default_trip": {"trip": trip_id},
        "read_chat_history": {"query": "private", "from_user": "only_a"},
    }[name]
    other = ChatRef("fake", "b")
    await ctx.stores.repo.find_members(other)
    await ctx.stores.repo.get_message(other, "missing")
    before = snapshot(selected, ctx)
    ctx.chat = other
    result = await registry.call(name, args, ctx)
    assert result.pending_action is None
    assert all(
        secret not in result.content
        for secret in [
            "private lunch",
            "private hiking arrangement",
            "private group A history",
            "private A trip",
        ]
    )
    assert result.is_error or "no" in result.content.lower()
    assert snapshot(selected, ctx) == before
    assert not ctx.platform.files


WRITES = {
    "add_expense": {"description": "coffee", "amount": "10", "paid_by": "me", "split_with": ["me"]},
    "delete_expense": {},
    "update_expense": {"expense_id": "", "amount": "11"},
    "restore_expense": {"expense_id": ""},
    "undo_expense_change": {},
    "update_trip_members": {"add": ["me"]},
    "settle_up": {"from_user": "me", "to_user": "bob", "amount": "10"},
    "set_fx_rate": {"currency": "JPY", "rate": "0.01"},
    "start_trip": {"name": "Holiday", "countries": ["SG"], "settle_mode": "per_currency"},
    "update_trip": {"add_currencies": ["HKD"]},
    "end_trip": {},
    "activate_trip": {"trip": "Planned"},
    "reopen_trip": {"trip": "Closed"},
    "set_default_trip": {"trip": "Japan"},
    "remember": {"fact": "We enjoy hiking"},
    "request_extension": {
        "description": "Convert Celsius to Fahrenheit",
        "examples": [{"arguments": {"c": 0}, "expected": 32}],
    },
    "forget": {},
    "request_context_reset": {},
}


def assert_json_round_trip(payload):
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.parametrize("name", WRITES)
async def test_writes_require_confirmation(audit, name):
    selected, registry, ctx = audit
    assert {t.name for t in TOOLS if t.writes} <= WRITES.keys()
    await ctx.stores.repo.upsert_member(ctx.chat, user("bob"))
    expense_id = await seed_expense(registry, ctx)
    seed = await registry.call("remember", {"fact": "Old durable arrangement"}, ctx)
    memory = next(p for p in selected if p.name == "memory")
    from chatcore.plugin import CommandContext

    await memory.action_handlers()["remember"].commit(
        seed.pending_action,
        CommandContext(ctx.chat, ctx.user, ctx.is_dm, False, ctx.platform, ctx.stores),
    )
    memory_id = (await memory._repo(ctx.stores).list(ctx.chat))[0]["id"]
    ledger = next(p for p in selected if p.name == "expenses").ledger
    if name in {"end_trip", "set_fx_rate", "update_trip", "set_default_trip"}:
        await ledger.commit(
            ctx.chat,
            "start_trip",
            {
                "name": "Japan",
                "countries": ["JP"],
                "currencies": ["JPY"],
                "settle_mode": "convert",
                "settle_currency": "SGD",
            },
            actor=ctx.user.ref,
        )
    if name == "update_trip_members":
        await ledger.commit(
            ctx.chat,
            "start_trip",
            {
                "name": "Japan",
                "countries": ["JP"],
                "currencies": ["JPY"],
                "settle_mode": "convert",
                "settle_currency": "SGD",
            },
            actor=ctx.user.ref,
        )
    if name == "activate_trip":
        await ledger.commit(
            ctx.chat,
            "start_trip",
            {
                "name": "Planned",
                "countries": ["JP"],
                "currencies": ["JPY"],
                "settle_mode": "convert",
                "settle_currency": "SGD",
                "status": "planned",
            },
            actor=ctx.user.ref,
        )
    if name == "reopen_trip":
        await ledger.commit(
            ctx.chat,
            "start_trip",
            {
                "name": "Closed",
                "countries": ["JP"],
                "currencies": ["JPY"],
                "settle_mode": "convert",
                "settle_currency": "SGD",
                "status": "active",
            },
            actor=ctx.user.ref,
        )
        closed = await ledger.trip(ctx.chat, "Closed")
        await ledger.commit(
            ctx.chat,
            "end_trip",
            {"trip_id": closed["id"], "expected_status": "active"},
            actor=ctx.user.ref,
        )
    arguments = {**WRITES[name]}
    if name == "delete_expense":
        arguments["expense_id"] = expense_id
    if name == "update_expense":
        arguments["expense_id"] = expense_id
    if name == "restore_expense":
        await ledger.commit(
            ctx.chat, "delete_expense", {"expense_id": expense_id}, actor=ctx.user.ref
        )
        arguments["expense_id"] = expense_id
    if name == "forget":
        arguments["memory_id"] = memory_id
    before = snapshot(selected, ctx)
    result = await registry.call(name, arguments, ctx)
    assert not result.is_error, result.content
    action = result.pending_action
    assert action is not None
    assert action.summary and action.chat == ctx.chat and action.requester == ctx.user.ref
    plugin = next(p for p in selected if p.name == action.plugin)
    assert action.kind in plugin.action_handlers()
    if registry.get(name).direct:
        assert action is not None
    assert_json_round_trip(action.payload)
    assert snapshot(selected, ctx) == before


@pytest.mark.parametrize("internal", ["source", "created_by"])
async def test_memory_internal_arguments_cannot_bypass_confirmation(audit, internal):
    selected, registry, ctx = audit
    ctx.turn.read_untrusted = True
    before = snapshot(selected, ctx)
    result = await registry.call("remember", {"fact": "Injected fact", internal: "auto"}, ctx)
    assert result.is_error
    assert result.content == f"unknown argument(s): {internal}"
    assert snapshot(selected, ctx) == before


def test_only_approved_tools_are_direct():
    browse = BrowsePlugin(
        BrowseSettings(render_url="http://renderer.test"),
        AccessPolicy(["fake:a"], []),
        FakePlatform(),
        client=object(),
    )
    all_tools = [*TOOLS, *remaining_write_tools(), *browse.tools()]
    direct = {tool.name for tool in all_tools if tool.direct}
    expected_direct = {
        "add_expense",
        "update_expense",
        "delete_expense",
        "restore_expense",
        "undo_expense_change",
        "update_trip_members",
        "update_trip",
        "create_poll",
        "create_reminder",
        "cancel_reminder",
        "restore_reminder",
        "create_website_watch",
        "cancel_website_watch",
        "restore_website_watch",
        "remember",
        "forget",
    }
    confirmed = {
        "settle_up",
        "start_trip",
        "end_trip",
        "activate_trip",
        "reopen_trip",
        "set_default_trip",
        "set_fx_rate",
        "configure_rendered_fetch",
        "request_extension",
        "configure_jobs",
    }
    names = {tool.name for tool in all_tools}
    assert expected_direct <= names
    assert confirmed <= names
    assert direct == expected_direct
    assert not any(tool.direct for tool in all_tools if tool.name in confirmed)
    assert all(tool.writes for tool in all_tools if tool.direct)
