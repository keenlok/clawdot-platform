"""Public trip identity bridge: no financial reads or writes and no implicit default."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from chatcore.events import ChatRef, User, UserRef
from chatcore.store.base import Stores
from chatcore.store.memory import MemoryCache, MemoryCoreRepo
from chatcore.testing import FakePlatform
from chatcore.tools.registry import ToolContext
from chatcore_expenses import ExpensesPlugin
from chatcore_expenses.repository import MemoryLedgerRepo


@pytest.fixture
async def scope():
    actor = User(UserRef("fake", "alice"), "Alice", "alice")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    ctx = ToolContext(ChatRef("fake", "group"), actor, False, FakePlatform(), stores)
    await stores.repo.upsert_member(ctx.chat, actor)
    return ctx, MemoryLedgerRepo()


async def start(ledger, ctx, name, status="planned"):
    await ledger.commit(
        ctx.chat,
        "start_trip",
        {
            "name": name,
            "countries": ["CN"],
            "currencies": ["CNY"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
            "status": status,
        },
        actor=ctx.user.ref,
    )
    return await ledger.trip(ctx.chat, name)


@pytest.mark.parametrize("status", ["planned", "active", "closed"])
async def test_public_resolver_returns_existing_identity_without_ledger_export(scope, status):
    ctx, ledger = scope
    trip = await start(ledger, ctx, "Sample & Example", "planned" if status == "closed" else status)
    if status == "closed":
        await ledger.commit(
            ctx.chat,
            "end_trip",
            {"trip_id": trip["id"], "expected_status": "planned"},
            actor=ctx.user.ref,
        )
    before = await ledger.trips(ctx.chat)
    ledger.expenses = AsyncMock(
        side_effect=AssertionError("Trip resolution must not read expenses")
    )
    plugin = ExpensesPlugin(ledger=ledger)

    result = await plugin.resolve_trip(ctx, "Sample &amp; Example")

    assert result == {
        "chat_key": ctx.chat.key,
        "trip_id": str(trip["id"]),
        "trip_name": "Sample & Example",
        "status": status,
    }
    assert await plugin.resolve_trip(ctx, str(trip["id"])) == result
    assert await ledger.trips(ctx.chat) == before
    ledger.expenses.assert_not_awaited()


@pytest.mark.parametrize("choice", [None, "", "  ", 123])
async def test_public_resolver_requires_explicit_trip_even_with_active_default(scope, choice):
    ctx, ledger = scope
    await start(ledger, ctx, "Default", "active")
    with pytest.raises(ValueError, match="trip|string"):
        await ExpensesPlugin(ledger=ledger).resolve_trip(ctx, choice)


async def test_public_resolver_uses_existing_ambiguity_rules(scope):
    ctx, ledger = scope
    await start(ledger, ctx, "China spring")
    await start(ledger, ctx, "China winter")
    with pytest.raises(ValueError, match="several trips"):
        await ExpensesPlugin(ledger=ledger).resolve_trip(ctx, "China")


async def test_public_resolver_does_not_resolve_another_groups_trip_id(scope):
    ctx, ledger = scope
    other = replace(ctx, chat=ChatRef("fake", "other"))
    other_trip = await start(ledger, other, "China")
    with pytest.raises(ValueError, match="not found"):
        await ExpensesPlugin(ledger=ledger).resolve_trip(ctx, str(other_trip["id"]))


async def test_public_resolver_dm_uses_only_resolved_group_and_requires_membership(scope):
    ctx, ledger = scope
    trip = await start(ledger, ctx, "China")
    dm = replace(ctx, chat=ChatRef("fake", "alice"), is_dm=True)
    resolver = AsyncMock(return_value=ctx.chat)
    plugin = ExpensesPlugin(ledger=ledger, group_for_dm=resolver)
    assert (await plugin.resolve_trip(dm, "China"))["trip_id"] == str(trip["id"])
    resolver.assert_awaited_once_with(dm)

    await ctx.stores.repo.mark_member_left(ctx.chat, ctx.user.ref)
    with pytest.raises(PermissionError, match="member"):
        await plugin.resolve_trip(dm, "China")


async def test_public_resolver_rejects_unresolved_dm_and_nonmember_group(scope):
    ctx, ledger = scope
    plugin = ExpensesPlugin(ledger=ledger)
    dm = replace(ctx, chat=ChatRef("fake", "alice"), is_dm=True)
    with pytest.raises(ValueError, match="group"):
        await plugin.resolve_trip(dm, "China")
    outsider = replace(ctx, user=User(UserRef("fake", "outsider"), "Outsider"))
    with pytest.raises(PermissionError, match="member"):
        await plugin.resolve_trip(outsider, "China")
