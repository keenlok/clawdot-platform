from dataclasses import replace

import pytest
from chatcore.access import AccessPolicy
from chatcore.confirm import ConfirmService
from chatcore.events import ChatRef, MessageRef, User, UserRef
from chatcore.pending import PendingAction
from chatcore.platform import PlatformError
from chatcore.store.base import Stores
from chatcore.store.memory import MemoryCache, MemoryCoreRepo
from chatcore.testing import FakePlatform
from chatcore.tools.registry import ToolContext
from chatcore_expenses import ExpensesPlugin
from chatcore_expenses.repository import MemoryLedgerRepo


class _CardClient:
    def __init__(self):
        self.cards = []

    async def render(self, card, *, theme="light", footer=None):
        self.cards.append(card)
        return [b"png"]

    async def aclose(self):
        pass


@pytest.fixture
def dm_context():
    actor = User(UserRef("fake", "alice"), "Alice", "alice")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    return ToolContext(ChatRef("fake", "alice"), actor, True, FakePlatform(), stores)


def test_private_ledger_mode_is_validated():
    with pytest.raises(ValueError):
        ExpensesPlugin(dm_ledger="other")


async def test_private_dm_starts_personal_trip_in_dm_ledger(dm_context):
    ledger = MemoryLedgerRepo()
    plugin = ExpensesPlugin(ledger=ledger, dm_ledger="private")
    result = await next(t for t in plugin.tools() if t.name == "start_trip").handler(
        dm_context,
        name="Tokyo",
        countries=["JP"],
        settle_mode="convert",
    )
    assert result.pending_action is not None
    assert result.pending_action.chat == dm_context.chat
    assert result.pending_action.payload["members"] == [dm_context.user.ref.key]
    await plugin.action_handlers()["start_trip"].commit(result.pending_action, dm_context)
    assert (await ledger.trips(dm_context.chat))[0]["name"] == "Tokyo"


async def test_private_dm_group_ledger_cards_exports_and_delivery_scope(dm_context):
    group = ChatRef("fake", "crew")
    make_member(dm_context, group)
    ledger = MemoryLedgerRepo()
    trip = await seed_trip(ledger, group, dm_context.user.ref, "Crew Japan")
    await seed_live_expense(
        ledger, group, dm_context.user.ref, description="Group dinner", trip_id=trip["id"]
    )
    cards = _CardClient()
    plugin = private_plugin(ledger, [group], {group: "Crew"})
    plugin.cards = cards
    tools = {item.name: item for item in plugin.tools()}

    summary = await tools["send_trip_summary_card"].handler(
        dm_context, trip=trip["name"], group="Crew"
    )
    assert not summary.is_error
    assert [photo.chat for photo in dm_context.platform.sent_photos] == [dm_context.chat]
    assert "Crew Japan" in cards.cards[-1].title
    assert not any(photo.chat == group for photo in dm_context.platform.sent_photos)

    dm_context.turn.tool_attempts.clear()
    balance = await tools["send_balance_card"].handler(dm_context, group="Crew")
    assert not balance.is_error
    assert dm_context.platform.sent_photos[-1].chat == dm_context.chat
    assert "Crew Japan" in cards.cards[-1].title
    assert not any(photo.chat == group for photo in dm_context.platform.sent_photos)

    dm_context.turn.tool_attempts.clear()
    unique_trip = await tools["send_trip_summary_card"].handler(dm_context, trip=trip["name"])
    assert not unique_trip.is_error
    assert dm_context.platform.sent_photos[-1].chat == dm_context.chat
    assert "Crew Japan" in cards.cards[-1].title

    exported = await tools["export_expenses"].handler(dm_context, group="Crew", trip=trip["name"])
    assert not exported.is_error
    assert [file.chat for file in dm_context.platform.sent_files] == [dm_context.chat]
    assert b"Group dinner" in dm_context.platform.sent_files[0].data
    assert not any(file.chat == group for file in dm_context.platform.sent_files)


async def test_private_dm_model_cannot_choose_delivery_chat(dm_context):
    group = ChatRef("fake", "crew")
    attacker = ChatRef("fake", "attacker")
    make_member(dm_context, group)
    ledger = MemoryLedgerRepo()
    await seed_live_expense(ledger, group, dm_context.user.ref, description="Group dinner")
    plugin = private_plugin(ledger, [group], {group: "Crew"})
    plugin.cards = _CardClient()
    tools = {item.name: item for item in plugin.tools()}

    card = await tools["send_balance_card"].handler(
        dm_context, group="Crew", deliver_to=(attacker, "malicious-thread")
    )
    assert not card.is_error
    assert dm_context.platform.sent_photos[-1].chat == dm_context.chat
    assert dm_context.platform.sent_photos[-1].thread_id == dm_context.thread_id

    exported = await tools["export_expenses"].handler(
        dm_context, group="Crew", deliver_to=(attacker, "malicious-thread")
    )
    assert not exported.is_error
    assert dm_context.platform.sent_files[-1].chat == dm_context.chat
    assert dm_context.platform.sent_files[-1].thread_id == dm_context.thread_id


async def test_private_dm_nonmember_group_cannot_deliver_cards_or_exports(dm_context):
    group = ChatRef("fake", "hidden")
    ledger = MemoryLedgerRepo()
    cards = _CardClient()
    plugin = private_plugin(ledger, [group], {group: "Hidden"})
    plugin.cards = cards
    tools = {item.name: item for item in plugin.tools()}

    for name in ("send_balance_card", "export_expenses"):
        result = await tools[name].handler(dm_context, group="Hidden")
        assert result.is_error
    assert not dm_context.platform.sent_photos and not dm_context.platform.sent_files
    assert not cards.cards


async def test_private_plugin_group_chat_delivers_card_and_export_to_group(dm_context):
    group = ChatRef("fake", "crew")
    group_context = replace(dm_context, chat=group, is_dm=False)
    ledger = MemoryLedgerRepo()
    await seed_live_expense(ledger, group, dm_context.user.ref, description="Group dinner")
    plugin = private_plugin(ledger, [], {})
    plugin.cards = _CardClient()
    tools = {item.name: item for item in plugin.tools()}

    card = await tools["send_balance_card"].handler(group_context, general=True)
    assert not card.is_error
    exported = await tools["export_expenses"].handler(group_context, general=True)
    assert not exported.is_error
    assert group_context.platform.sent_photos[-1].chat == group
    assert group_context.platform.sent_files[-1].chat == group


async def test_private_group_name_uses_current_membership_and_title(dm_context):
    group = ChatRef("fake", "crew")
    make_member(dm_context, group)
    ledger = MemoryLedgerRepo()
    plugin = ExpensesPlugin(
        ledger=ledger,
        dm_ledger="private",
        member_groups=lambda user: _groups(group),
        group_title=lambda chat: _title(chat, "Japan Crew"),
    )
    result = await next(t for t in plugin.tools() if t.name == "start_trip").handler(
        dm_context,
        name="Tokyo",
        countries=["JP"],
        settle_mode="convert",
        group=" japan crew ",
    )
    assert result.pending_action is not None
    assert result.pending_action.chat == group


async def test_private_trip_refuses_other_members(dm_context):
    plugin = ExpensesPlugin(ledger=MemoryLedgerRepo(), dm_ledger="private")
    result = await next(t for t in plugin.tools() if t.name == "start_trip").handler(
        dm_context,
        name="Tokyo",
        countries=["JP"],
        settle_mode="convert",
        members=["Bob"],
    )
    assert result.is_error
    assert "Private trips only track your own expenses" in result.content


async def test_private_expense_defaults_to_requester_only(dm_context):
    plugin = ExpensesPlugin(ledger=MemoryLedgerRepo(), dm_ledger="private")
    result = await next(t for t in plugin.tools() if t.name == "add_expense").handler(
        dm_context,
        description="Coffee",
        amount="5",
        paid_by="me",
    )
    assert result.pending_action is not None
    assert result.pending_action.chat == dm_context.chat
    assert result.pending_action.payload["shares"] == {dm_context.user.ref.key: 500}


@pytest.mark.parametrize("payer", ["me", "Alice", "@alice"])
async def test_private_trip_default_split_resolves_requester(dm_context, payer):
    ledger = MemoryLedgerRepo()
    plugin = ExpensesPlugin(ledger=ledger, dm_ledger="private")
    trip = await seed_trip(ledger, dm_context.chat, dm_context.user.ref, "Tokyo")
    result = await tool(plugin, "add_expense").handler(
        dm_context, description="Coffee", amount="5", paid_by=payer, trip=trip["name"]
    )
    assert result.pending_action is not None
    assert result.pending_action.payload["paid_by"] == dm_context.user.ref.key
    # The seeded trip is in JPY, which has no minor unit.
    assert result.pending_action.payload["shares"] == {dm_context.user.ref.key: 5}


async def test_private_update_members_requester_is_noop_and_other_is_refused(dm_context):
    ledger = MemoryLedgerRepo()
    trip = await seed_trip(ledger, dm_context.chat, dm_context.user.ref, "Tokyo")
    plugin = ExpensesPlugin(ledger=ledger, dm_ledger="private")
    noop = await tool(plugin, "update_trip_members").handler(
        dm_context, trip=trip["name"], add=["Alice"]
    )
    assert not noop.is_error
    assert "no changes" in noop.content
    refused = await tool(plugin, "update_trip_members").handler(
        dm_context, trip=trip["name"], add=["Bob"]
    )
    assert refused.is_error
    assert "Private trips only track your own expenses" in refused.content


async def test_private_trip_member_resolution_does_not_change_group_or_group_mode(dm_context):
    group = ChatRef("fake", "crew")
    group_ctx = replace(dm_context, chat=group, is_dm=False)
    private = ExpensesPlugin(ledger=MemoryLedgerRepo(), dm_ledger="private")
    assert await private._members(dm_context, dm_context.chat) == [dm_context.user]
    assert await private._members(group_ctx, group) == []
    group_mode = ExpensesPlugin(ledger=MemoryLedgerRepo(), dm_ledger="group")
    assert await group_mode._members(dm_context, dm_context.chat) == []


async def test_default_mode_keeps_group_dm_resolution_and_tool_spec(dm_context):
    group = ChatRef("fake", "crew")

    async def resolver(ctx):
        return group

    plugin = ExpensesPlugin(group_for_dm=resolver)
    assert await plugin._chat(dm_context) == group
    start_trip = next(t for t in plugin.tools() if t.name == "start_trip")
    assert "group" not in start_trip.parameters["properties"]


def tool(plugin, name):
    return next(item for item in plugin.tools() if item.name == name)


def private_plugin(ledger, groups, titles):
    async def member_groups(_user):
        return list(groups)

    async def group_title(chat):
        return titles.get(chat)

    return ExpensesPlugin(
        ledger=ledger,
        dm_ledger="private",
        member_groups=member_groups,
        group_title=group_title,
    )


async def test_unknown_group_title_is_refused(dm_context):
    group = ChatRef("fake", "crew")
    plugin = private_plugin(MemoryLedgerRepo(), [group], {group: "Japan Crew"})
    result = await tool(plugin, "start_trip").handler(
        dm_context,
        name="Tokyo",
        countries=["JP"],
        settle_mode="convert",
        group="Nope",
    )
    assert result.is_error
    assert "couldn't find exactly one of your groups called Nope" in result.content


async def test_duplicate_member_group_titles_are_ambiguous(dm_context):
    one, two = ChatRef("fake", "one"), ChatRef("fake", "two")
    plugin = private_plugin(
        MemoryLedgerRepo(), [one, two], {one: "Travel Crew", two: " travel crew "}
    )
    result = await tool(plugin, "start_trip").handler(
        dm_context,
        name="Tokyo",
        countries=["JP"],
        settle_mode="convert",
        group="Travel Crew",
    )
    assert result.is_error
    assert "couldn't find exactly one of your groups called Travel Crew" in result.content


async def test_known_nonmember_group_title_is_refused(dm_context):
    member_group, hidden_group = ChatRef("fake", "member"), ChatRef("fake", "hidden")
    plugin = private_plugin(
        MemoryLedgerRepo(), [member_group], {member_group: "Friends", hidden_group: "Private Crew"}
    )
    result = await tool(plugin, "start_trip").handler(
        dm_context,
        name="Tokyo",
        countries=["JP"],
        settle_mode="convert",
        group="Private Crew",
    )
    assert result.is_error
    assert "couldn't find exactly one of your groups called Private Crew" in result.content


@pytest.mark.parametrize("operation", ["write", "read", "resolve_trip"])
@pytest.mark.parametrize("membership_fails", [False, True], ids=["not-a-member", "unavailable"])
async def test_cached_membership_is_confirmed_uncached_at_call_time(
    dm_context, operation, membership_fails
):
    group = ChatRef("fake", "crew")
    platform = dm_context.platform
    platform.members[group.key] = {dm_context.user.ref.key}
    access = AccessPolicy([group.key], [])

    async def current_groups(user):
        return await access.member_groups(user, platform, dm_context.stores.cache)

    ledger = MemoryLedgerRepo()
    trip = await seed_trip(ledger, group, dm_context.user.ref, "Japan 2026")
    plugin = ExpensesPlugin(
        ledger=ledger,
        dm_ledger="private",
        member_groups=current_groups,
        group_title=lambda chat: _title(chat, "Crew"),
    )
    assert await current_groups(dm_context.user.ref) == [group]
    platform.members[group.key].clear()
    assert await current_groups(dm_context.user.ref) == [group]
    if membership_fails:

        async def fail_membership(_chat, _user):
            raise PlatformError("unavailable")

        platform.is_member = fail_membership

    if operation == "write":
        result = await tool(plugin, "start_trip").handler(
            dm_context,
            name="Tokyo",
            countries=["JP"],
            settle_mode="convert",
            group="Crew",
        )
        assert result.is_error
        assert "membership" in result.content.casefold() or "member" in result.content.casefold()
    elif operation == "read":
        result = await tool(plugin, "list_expenses").handler(dm_context, trip="Japan 2026")
        assert result.is_error
        assert "membership" in result.content.casefold() or "member" in result.content.casefold()
    else:
        with pytest.raises(PermissionError, match="member"):
            await plugin.resolve_trip(dm_context, "Japan 2026")
    assert trip["name"] == "Japan 2026"


async def test_group_argument_is_ignored_outside_dm(dm_context):
    group = ChatRef("fake", "crew")
    group_context = replace(dm_context, chat=group, is_dm=False)
    plugin = private_plugin(MemoryLedgerRepo(), [], {})
    result = await tool(plugin, "start_trip").handler(
        group_context,
        name="Tokyo",
        countries=["JP"],
        settle_mode="convert",
        group="Unknown outside DMs",
    )
    assert result.pending_action is not None
    assert result.pending_action.chat == group
    group_property = tool(plugin, "start_trip").parameters["properties"]["group"]
    assert "only applies in a dm" in group_property["description"].casefold()


async def seed_trip(ledger, chat, actor, name, status="active"):
    initial_status = "planned" if status == "closed" else status
    await ledger.commit(
        chat,
        "start_trip",
        {
            "name": name,
            "countries": ["JP"],
            "currencies": ["JPY"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
            "status": initial_status,
            "members": [actor.key],
        },
        actor=actor,
    )
    trip = (await ledger.trips(chat))[0]
    if status == "closed":
        await ledger.commit(
            chat,
            "end_trip",
            {"trip_id": trip["id"], "expected_status": initial_status},
            actor=actor,
        )
    return trip


async def update_trip_tool(plugin, ctx, trip_name, new_name):
    return await tool(plugin, "update_trip").handler(ctx, trip=trip_name, name=new_name)


@pytest.mark.parametrize("status", ["active", "planned"])
async def test_unique_group_trip_name_targets_group_ledger(dm_context, status):
    group = ChatRef("fake", "crew")
    make_member(dm_context, group)
    ledger = MemoryLedgerRepo()
    trip = await seed_trip(ledger, group, dm_context.user.ref, "Japan 2026", status)
    plugin = private_plugin(ledger, [group], {group: "Crew"})
    result = await update_trip_tool(plugin, dm_context, "Japan 2026", "Renamed Japan")
    assert result.pending_action is not None
    assert result.pending_action.chat == group
    assert result.pending_action.payload["trip_id"] == trip["id"]


async def test_private_trip_wins_when_group_trip_has_same_name(dm_context):
    group = ChatRef("fake", "crew")
    ledger = MemoryLedgerRepo()
    group_trip = await seed_trip(ledger, group, dm_context.user.ref, "Japan 2026")
    private_trip = await seed_trip(ledger, dm_context.chat, dm_context.user.ref, "Japan 2026")
    plugin = private_plugin(ledger, [group], {group: "Crew"})
    result = await update_trip_tool(plugin, dm_context, "Japan 2026", "Private Japan")
    assert result.pending_action is not None
    assert result.pending_action.chat == dm_context.chat
    assert result.pending_action.payload["trip_id"] == private_trip["id"]
    assert result.pending_action.payload["trip_id"] != group_trip["id"]


async def test_trip_name_in_two_groups_asks_for_group(dm_context):
    one, two = ChatRef("fake", "one"), ChatRef("fake", "two")
    make_member(dm_context, one)
    make_member(dm_context, two)
    ledger = MemoryLedgerRepo()
    await seed_trip(ledger, one, dm_context.user.ref, "Japan 2026")
    await seed_trip(ledger, two, dm_context.user.ref, "Japan 2026")
    plugin = private_plugin(ledger, [one, two], {one: "One", two: "Two"})
    result = await update_trip_tool(plugin, dm_context, "Japan 2026", "Renamed")
    assert result.is_error
    assert "several groups. Name the group" in result.content


async def test_private_ambiguous_trip_does_not_fall_back_to_group(dm_context):
    group = ChatRef("fake", "crew")
    ledger = MemoryLedgerRepo()
    await seed_trip(ledger, dm_context.chat, dm_context.user.ref, "Japan East")
    await seed_trip(ledger, dm_context.chat, dm_context.user.ref, "Japan West")
    await seed_trip(ledger, group, dm_context.user.ref, "Japan")
    plugin = private_plugin(ledger, [group], {group: "Crew"})
    result = await update_trip_tool(plugin, dm_context, "Japan", "Renamed")
    assert result.is_error
    assert "matches several trips" in result.content


async def test_ambiguous_trip_inside_group_asks_for_clearer_trip(dm_context):
    group = ChatRef("fake", "crew")
    make_member(dm_context, group)
    ledger = MemoryLedgerRepo()
    await seed_trip(ledger, group, dm_context.user.ref, "Japan East")
    await seed_trip(ledger, group, dm_context.user.ref, "Japan West")
    plugin = private_plugin(ledger, [group], {group: "Crew"})
    result = await update_trip_tool(plugin, dm_context, "Japan", "Renamed")
    assert result.is_error
    assert "ambiguous" in result.content.casefold()
    assert "trip" in result.content.casefold() or "group" in result.content.casefold()


@pytest.mark.parametrize("change", ["add_other", "remove_requester"])
async def test_private_trip_membership_changes_are_refused_when_requested(dm_context, change):
    ledger = MemoryLedgerRepo()
    trip = await seed_trip(ledger, dm_context.chat, dm_context.user.ref, "Japan 2026")
    bob = User(UserRef("fake", "bob"), "Bob", "bob")
    await dm_context.stores.repo.upsert_member(dm_context.chat, dm_context.user)
    await dm_context.stores.repo.upsert_member(dm_context.chat, bob)
    plugin = ExpensesPlugin(ledger=ledger, dm_ledger="private")
    kwargs = {"add": ["Bob"]} if change == "add_other" else {"remove": ["me"]}
    result = await tool(plugin, "update_trip_members").handler(
        dm_context, trip=trip["name"], **kwargs
    )
    assert result.is_error
    assert "Private trips only track your own expenses" in result.content


@pytest.mark.parametrize(
    ("kind", "member_change"),
    [
        ("start_trip", "add_other"),
        ("update_trip_members", "add_other"),
        ("update_trip_members", "remove_requester"),
    ],
)
async def test_private_member_payload_is_revalidated_at_commit(dm_context, kind, member_change):
    ledger = MemoryLedgerRepo()
    plugin = ExpensesPlugin(ledger=ledger, dm_ledger="private")
    bob = UserRef("fake", "bob")
    if kind == "start_trip":
        payload = {
            "name": "Forged",
            "countries": ["JP"],
            "currencies": ["JPY"],
            "settle_mode": "convert",
            "settle_currency": "SGD",
            "status": "active",
            "members": [dm_context.user.ref.key, bob.key],
        }
    else:
        trip = await seed_trip(ledger, dm_context.chat, dm_context.user.ref, "Japan 2026")
        payload = {
            "trip_id": trip["id"],
            "members_before": [dm_context.user.ref.key],
            "add": [bob.key] if member_change == "add_other" else [],
            "remove": [dm_context.user.ref.key] if member_change == "remove_requester" else [],
        }
    action = PendingAction.create(
        chat=dm_context.chat,
        requester=dm_context.user.ref,
        plugin=plugin.name,
        kind=kind,
        payload=payload,
        summary="test action",
    )
    result = await plugin.action_handlers()[kind].commit(action, dm_context)
    assert "Not saved: Private trips only track your own expenses" in result
    trips = await ledger.trips(dm_context.chat)
    if kind == "update_trip_members":
        assert len(trips) == 1
        assert [
            row["user_key"]
            for row in await ledger.trip_members(dm_context.chat, payload["trip_id"])
        ] == [dm_context.user.ref.key]
    else:
        assert trips == []


async def seed_expense(ledger, chat, actor, *, payer=None, shares=None, description="expense"):
    payer = payer or actor
    shares = shares or {actor.key: 1000}
    await ledger.commit(
        chat,
        "add_expense",
        {
            "description": description,
            "amount": 1000,
            "currency": "SGD",
            "paid_by": payer.key,
            "created_by": actor.key,
            "spent_on": "2026-11-16",
            "trip_id": None,
            "shares": shares,
            "fx_rate_to_settle": None,
        },
        actor=actor,
    )
    row = (await ledger.expenses(chat))[0]
    await ledger.commit(chat, "delete_expense", {"expense_id": row["id"]}, actor=actor)
    return row


async def seed_live_expense(ledger, chat, actor, *, description="expense", trip_id=None):
    await ledger.commit(
        chat,
        "add_expense",
        {
            "description": description,
            "amount": 1000,
            "currency": "SGD",
            "paid_by": actor.key,
            "created_by": actor.key,
            "spent_on": "2026-11-16",
            "trip_id": trip_id,
            "shares": {actor.key: 1000},
            "fx_rate_to_settle": None,
        },
        actor=actor,
    )


@pytest.mark.parametrize("foreign_field", ["payer", "share"])
async def test_private_restore_refuses_foreign_payer_or_share_at_request(dm_context, foreign_field):
    ledger = MemoryLedgerRepo()
    bob = UserRef("fake", "bob")
    row = await seed_expense(
        ledger,
        dm_context.chat,
        dm_context.user.ref,
        payer=bob if foreign_field == "payer" else None,
        shares=({bob.key: 1000} if foreign_field == "share" else {dm_context.user.ref.key: 1000}),
    )
    plugin = ExpensesPlugin(ledger=ledger, dm_ledger="private")
    result = await tool(plugin, "restore_expense").handler(dm_context, expense_id=row["id"])
    assert result.is_error
    assert "Private trips only track your own expenses" in result.content


@pytest.mark.parametrize("foreign_field", ["payer", "share"])
async def test_private_restore_rechecks_stored_expense_at_commit(dm_context, foreign_field):
    ledger = MemoryLedgerRepo()
    row = await seed_expense(ledger, dm_context.chat, dm_context.user.ref)
    plugin = ExpensesPlugin(ledger=ledger, dm_ledger="private")
    proposal = await tool(plugin, "restore_expense").handler(dm_context, expense_id=row["id"])
    assert proposal.pending_action is not None
    stored = ledger.expense_rows[row["id"]]
    bob = UserRef("fake", "bob")
    if foreign_field == "payer":
        stored["paid_by"] = bob.key
    else:
        stored["shares"] = {bob.key: 1000}
    result = await plugin.action_handlers()["restore_expense"].commit(
        proposal.pending_action, dm_context
    )
    assert "Not saved: Private trips only track your own expenses" in result
    assert stored["deleted_at"] is not None


@pytest.mark.parametrize("foreign_field", ["payer", "share"])
async def test_private_undo_refuses_foreign_snapshot_at_request(dm_context, foreign_field):
    ledger = MemoryLedgerRepo()
    bob = UserRef("fake", "bob")
    await ledger.commit(
        dm_context.chat,
        "add_expense",
        {
            "description": "foreign snapshot",
            "amount": 1000,
            "currency": "SGD",
            "paid_by": bob.key if foreign_field == "payer" else dm_context.user.ref.key,
            "created_by": dm_context.user.ref.key,
            "spent_on": "2026-11-16",
            "trip_id": None,
            "shares": (
                {bob.key: 1000} if foreign_field == "share" else {dm_context.user.ref.key: 1000}
            ),
            "fx_rate_to_settle": None,
        },
        actor=dm_context.user.ref,
    )
    plugin = ExpensesPlugin(ledger=ledger, dm_ledger="private")
    result = await tool(plugin, "undo_expense_change").handler(dm_context)
    assert result.is_error
    assert "Private trips only track your own expenses" in result.content


@pytest.mark.parametrize("foreign_field", ["payer", "share"])
async def test_private_undo_rechecks_snapshot_at_commit(dm_context, foreign_field):
    ledger = MemoryLedgerRepo()
    await seed_expense(ledger, dm_context.chat, dm_context.user.ref)
    plugin = ExpensesPlugin(ledger=ledger, dm_ledger="private")
    proposal = await tool(plugin, "undo_expense_change").handler(dm_context)
    assert proposal.pending_action is not None
    event = next(
        item
        for item in ledger.expense_events
        if item["id"] == proposal.pending_action.payload["event_id"]
    )
    bob = UserRef("fake", "bob")
    if foreign_field == "payer":
        event["before"]["paid_by"] = bob.key
    else:
        event["before"]["shares"] = {bob.key: 1000}
    result = await plugin.action_handlers()["undo_expense_change"].commit(
        proposal.pending_action, dm_context
    )
    assert "Not saved: Private trips only track your own expenses" in result
    assert ledger.expense_rows[event["expense_id"]]["deleted_at"] is not None


async def test_closed_group_trip_does_not_resolve_trip_fallback(dm_context):
    group = ChatRef("fake", "crew")
    ledger = MemoryLedgerRepo()
    await seed_trip(ledger, group, dm_context.user.ref, "Japan 2026", "closed")
    plugin = private_plugin(ledger, [group], {group: "Crew"})
    result = await tool(plugin, "list_expenses").handler(dm_context, trip="Japan 2026")
    assert result.is_error
    assert 'Trip "Japan 2026" not found' in result.content


async def test_private_list_and_balances_read_only_dm_ledger(dm_context):
    group = ChatRef("fake", "crew")
    ledger = MemoryLedgerRepo()
    bob = UserRef("fake", "bob")

    async def seed(chat, description, shares):
        await ledger.commit(
            chat,
            "add_expense",
            {
                "description": description,
                "amount": 1000,
                "currency": "SGD",
                "paid_by": dm_context.user.ref.key,
                "created_by": dm_context.user.ref.key,
                "spent_on": "2026-11-16",
                "trip_id": None,
                "shares": shares,
                "fx_rate_to_settle": None,
            },
            actor=dm_context.user.ref,
        )

    await seed(dm_context.chat, "Private Coffee", {dm_context.user.ref.key: 1000})
    await seed(group, "Group Dinner", {dm_context.user.ref.key: 500, bob.key: 500})
    plugin = ExpensesPlugin(ledger=ledger, dm_ledger="private")
    listing = await tool(plugin, "list_expenses").handler(dm_context)
    balances = await tool(plugin, "get_balances").handler(dm_context)
    assert "Private Coffee" in listing.content
    assert "Group Dinner" not in listing.content
    assert balances.content == "All balances are settled."


async def test_private_confirmation_uses_dm_and_keeps_reply(dm_context):
    platform = dm_context.platform
    plugin = ExpensesPlugin(ledger=MemoryLedgerRepo(), dm_ledger="private")
    result = await tool(plugin, "start_trip").handler(
        dm_context, name="Tokyo", countries=["JP"], settle_mode="convert"
    )
    reply = MessageRef(dm_context.chat, "human-message")
    handlers = {
        f"{plugin.name}.{kind}": handler for kind, handler in plugin.action_handlers().items()
    }
    await ConfirmService(platform, dm_context.stores.repo, handlers).request(
        result.pending_action, dm_context.user, reply_to=reply
    )
    sent = platform.sent[-1]
    assert sent.chat == dm_context.chat
    assert sent.reply_to == reply


async def test_resolve_trip_uses_unique_member_group_trip(dm_context):
    group = ChatRef("fake", "crew")
    make_member(dm_context, group)
    ledger = MemoryLedgerRepo()
    trip = await seed_trip(ledger, group, dm_context.user.ref, "Japan 2026")
    plugin = private_plugin(ledger, [group], {group: "Crew"})
    await dm_context.stores.repo.upsert_member(group, dm_context.user)
    resolved = await plugin.resolve_trip(dm_context, "Japan 2026")
    assert resolved["chat_key"] == group.key
    assert resolved["trip_id"] == trip["id"]


async def _groups(group):
    return [group]


async def _title(chat, value):
    return value


def make_member(ctx, group):
    ctx.platform.members[group.key] = {ctx.user.ref.key}
