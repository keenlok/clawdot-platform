"""Trusted invites expand group access without expanding the inviter's other roles."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from chatcore.access import AccessPolicy
from chatcore.administration.store import AdminChange, MemoryAdministrationStore, global_scope
from chatcore.events import ChatRef, UserRef
from chatcore.testing import FakePlatform, memory_stores

OWNER, FRIEND, STRANGER = (UserRef("tg", str(i)) for i in (1, 2, 3))
GROUP = ChatRef("tg", "-1001")
OWNERS = frozenset({OWNER.key})


async def change(store, operation, group=GROUP, user=None, actor=OWNER, value=None):
    return await store.apply(
        actor=actor,
        owners=OWNERS,
        expected_revision=(await store.snapshot()).revision,
        change=AdminChange(operation, group, user, value),
    )


async def admit(store, inviter=FRIEND, group=GROUP, event_key="1:10", bootstrap=frozenset()):
    return await store.admit_group(
        group=group,
        inviter=inviter,
        owners=OWNERS,
        bootstrap_chats=bootstrap,
        event_key=event_key,
    )


async def test_trusted_invite_persists_once_without_other_privileges():
    store = MemoryAdministrationStore()
    await change(store, "trust", global_scope("tg"), FRIEND)
    before = await store.snapshot()
    assert before.trusted_inviters == frozenset({FRIEND.key})
    results = await asyncio.gather(admit(store), admit(store))
    assert sorted(results) == ["admitted", "duplicate"]
    snapshot = await store.snapshot()
    assert snapshot.revision == before.revision + 1
    assert snapshot.overrides[GROUP.key]
    assert snapshot.admissions[GROUP.key].inviter == FRIEND.key
    assert snapshot.admissions[GROUP.key].admitted_at.tzinfo is not None
    assert snapshot.assignments == snapshot.moderators == frozenset()
    assert len(await store.audit()) == 2
    assert (await store.audit())[0].operation == "admit_group"
    with pytest.raises(TypeError):
        snapshot.admissions[GROUP.key] = None
    policy = AccessPolicy([], [], owners=OWNERS, store=store)
    await policy.refresh()
    assert policy.is_chat_allowed(GROUP)
    assert not policy.is_admin(FRIEND)
    assert not await policy.can_dm(FRIEND, FakePlatform(name="tg"), memory_stores().cache)
    await change(store, "untrust", global_scope("tg"), FRIEND)
    assert await admit(store, group=ChatRef("tg", "-1002")) == "denied"
    assert await admit(store, event_key="1:11") == "allowed"
    assert (await store.snapshot()).overrides[GROUP.key]


@pytest.mark.parametrize(
    "inviter", [None, STRANGER, UserRef("other", "2"), UserRef("tg", "*"), UserRef("tg", "02")]
)
async def test_unknown_group_rejects_missing_untrusted_or_invalid_inviter(inviter):
    store = MemoryAdministrationStore()
    await change(store, "trust", global_scope("tg"), FRIEND)
    before = await store.snapshot()
    assert await admit(store, inviter) == "denied"
    assert await store.snapshot() == before


async def test_owner_can_invite_and_existing_allow_needs_no_trust():
    store = MemoryAdministrationStore()
    assert await admit(store, OWNER) == "admitted"
    assert await admit(store, STRANGER, event_key="1:11") == "allowed"
    other = ChatRef("tg", "-1002")
    assert await admit(store, None, group=other, bootstrap=frozenset({other.key})) == "allowed"


async def test_block_overrides_bootstrap_and_delegated_enable_and_unblock_keeps_disabled():
    store = MemoryAdministrationStore()
    await change(store, "grant", user=FRIEND)
    await change(store, "trust", global_scope("tg"), FRIEND)
    await change(store, "blockgroup")
    policy = AccessPolicy([GROUP.key], [], owners=OWNERS, store=store)
    await policy.refresh()
    assert not policy.is_chat_allowed(GROUP)
    assert await admit(store, OWNER, bootstrap=frozenset({GROUP.key})) == "denied"
    for actor in (OWNER, FRIEND):
        with pytest.raises(PermissionError, match="blocked"):
            await change(store, "enable", actor=actor)
    for operation in ("blockgroup", "unblockgroup"):
        with pytest.raises(PermissionError):
            await change(store, operation, actor=FRIEND)
    await change(store, "unblockgroup")
    assert GROUP.key not in (await store.snapshot()).blocked_groups
    assert await admit(store, OWNER) == "denied"
    await change(store, "enable", actor=FRIEND)
    assert await admit(store, event_key="1:12") == "allowed"


@pytest.mark.parametrize("operation", ["trust", "untrust"])
async def test_trust_owner_only_and_validated(operation):
    store = MemoryAdministrationStore()
    with pytest.raises(PermissionError):
        await change(store, operation, global_scope("tg"), STRANGER, actor=FRIEND)
    for target in (UserRef("tg", "-1"), UserRef("tg", "*"), UserRef("other", "2")):
        with pytest.raises(ValueError):
            await change(store, operation, global_scope("tg"), target)
    with pytest.raises(ValueError):
        await change(store, operation, GROUP, FRIEND)


@pytest.mark.parametrize(
    "operation,scope",
    [
        ("block", GROUP),
        ("pause", GROUP),
        ("block", global_scope("tg")),
        ("pause", global_scope("tg")),
    ],
)
async def test_active_restriction_prevents_new_admission(operation, scope):
    store = MemoryAdministrationStore()
    await change(store, "trust", global_scope("tg"), FRIEND)
    await change(store, operation, scope, FRIEND, value=60 if operation == "pause" else None)
    assert await admit(store) == "denied"
    if operation == "pause":
        key = (FRIEND.key, scope.key)
        store._restrictions[key] = replace(
            store._restrictions[key], until=datetime.now(UTC) - timedelta(seconds=1)
        )
        assert await admit(store) == "admitted"


@pytest.mark.parametrize(
    "group",
    [ChatRef("tg", "1"), ChatRef("tg", "*"), ChatRef("tg", "-01"), ChatRef("other", "-1001")],
)
async def test_automatic_admission_is_telegram_group_only(group):
    store = MemoryAdministrationStore()
    assert await admit(store, OWNER, group=group) == "denied"
    assert (await store.snapshot()).revision == 0


async def test_deliberate_disable_is_not_undone_by_owner_invitation():
    store = MemoryAdministrationStore()
    await change(store, "disable")
    assert await admit(store, OWNER) == "denied"
    assert (await store.snapshot()).revision == 1


async def test_effective_group_set_denies_block_even_with_conflicting_enable():
    store = MemoryAdministrationStore()
    await change(store, "blockgroup")
    # A block is authoritative even if older/external bootstrap or persisted allow
    # state conflicts; all membership/worker consumers use this same view.
    policy = AccessPolicy([GROUP.key], [], owners=OWNERS)
    policy._snapshot = replace(await store.snapshot(), overrides={GROUP.key: True})
    platform = FakePlatform(name="tg")
    platform.members[GROUP.key] = {FRIEND.key}
    assert GROUP.key not in policy.allowed_chats
    assert not await policy.member_groups(FRIEND, platform, memory_stores().cache)


async def test_admission_membership_candidates_are_shared_and_block_removes_cached_dm_access():
    store = MemoryAdministrationStore()
    await admit(store, OWNER)
    policy = AccessPolicy([], [], owners=OWNERS, store=store)
    platform, cache = FakePlatform(name="tg"), memory_stores().cache
    platform.members[GROUP.key] = {FRIEND.key}
    await policy.refresh()
    assert await policy.member_groups(FRIEND, platform, cache) == [GROUP]
    assert await policy.can_dm(FRIEND, platform, cache)
    await change(store, "blockgroup")
    await policy.refresh()
    assert not await policy.member_groups(FRIEND, platform, cache)
    assert not await policy.can_dm(FRIEND, platform, cache)
