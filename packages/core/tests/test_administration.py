import asyncio

import pytest
from chatcore.access import AccessPolicy
from chatcore.administration.store import (
    AdminChange,
    MemoryAdministrationStore,
    StaleChangeError,
)
from chatcore.events import ChatRef, UserRef
from chatcore.store.base import ChatSettings
from chatcore.store.memory import MemoryCache, MemoryCoreRepo
from chatcore.testing import FakePlatform, memory_stores


@pytest.mark.asyncio
async def test_delegate_cannot_enable_unassigned_group():
    owner, delegate = UserRef("tg", "1"), UserRef("tg", "2")
    group = ChatRef("tg", "-1001")
    other = ChatRef("tg", "-1002")
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("grant", group, user=delegate),
    )
    before = await store.snapshot()
    with pytest.raises(PermissionError):
        await store.apply(
            actor=delegate,
            owners=frozenset({owner.key}),
            expected_revision=before.revision,
            change=AdminChange("enable", other),
        )
    assert await store.snapshot() == before


@pytest.mark.asyncio
async def test_successful_writes_are_atomic_and_snapshots_are_immutable():
    owner, delegate = UserRef("tg", "1"), UserRef("tg", "2")
    group = ChatRef("tg", "-1001")
    store = MemoryAdministrationStore()
    first = await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("grant", group, user=delegate),
    )
    with pytest.raises(TypeError):
        first.overrides[group.key] = False
    await store.apply(
        actor=delegate,
        owners=frozenset({owner.key}),
        expected_revision=1,
        change=AdminChange("enable", group),
    )
    snapshot = await store.snapshot()
    assert snapshot.revision == 2
    assert snapshot.overrides[group.key] is True
    assert [row.revision for row in await store.audit()] == [2, 1]


@pytest.mark.asyncio
async def test_legacy_admin_cannot_grant_and_revoke_invalidates_old_revision():
    owner, delegate, target = (UserRef("tg", str(i)) for i in (1, 2, 3))
    group = ChatRef("tg", "-1001")
    store = MemoryAdministrationStore()
    with pytest.raises(PermissionError):
        await store.apply(
            actor=delegate,
            owners=frozenset({owner.key}),
            expected_revision=0,
            change=AdminChange("grant", group, user=target),
        )
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("grant", group, user=delegate),
    )
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=1,
        change=AdminChange("revoke", group, user=delegate),
    )
    with pytest.raises(PermissionError):
        await store.apply(
            actor=delegate,
            owners=frozenset({owner.key}),
            expected_revision=2,
            change=AdminChange("enable", group),
        )
    with pytest.raises(StaleChangeError):
        await store.apply(
            actor=owner,
            owners=frozenset({owner.key}),
            expected_revision=1,
            change=AdminChange("enable", group),
        )
    snap = await store.snapshot()
    assert snap.revision == 2
    assert snap.assignments == frozenset()
    assert len(await store.audit()) == 2


@pytest.mark.asyncio
async def test_only_one_concurrent_write_at_same_revision_succeeds():
    owner = UserRef("tg", "1")
    groups = [ChatRef("tg", "-1001"), ChatRef("tg", "-1002")]
    store = MemoryAdministrationStore()
    results = await asyncio.gather(
        *(
            store.apply(
                actor=owner,
                owners=frozenset({owner.key}),
                expected_revision=0,
                change=AdminChange("enable", group),
            )
            for group in groups
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, StaleChangeError) for result in results) == 1
    assert (await store.snapshot()).revision == 1
    assert len(await store.audit()) == 1


@pytest.mark.parametrize(
    "change",
    [
        AdminChange("enable", ChatRef("tg", "42")),
        AdminChange("grant", ChatRef("tg", "-1001"), UserRef("tg", "-42")),
        AdminChange("grant", ChatRef("tg", "-1001"), UserRef("other", "42")),
        AdminChange("enable", ChatRef("tg", "-0")),
        AdminChange("enable", ChatRef("tg", "-１２３")),
        AdminChange("enable", ChatRef("tg", "-01")),
        AdminChange("grant", ChatRef("tg", "-1001"), UserRef("tg", "0")),
        AdminChange("grant", ChatRef("tg", "-1001"), UserRef("tg", "１２３")),
        AdminChange("grant", ChatRef("tg", "-1001"), UserRef("tg", "01")),
        AdminChange("chatmode", ChatRef("tg", "-1001"), value=True),
        AdminChange("enable", ChatRef("tg", "-1001"), value="oops"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_changes_do_not_write_audit_or_revision(change):
    owner = UserRef("tg", "1")
    store = MemoryAdministrationStore()
    with pytest.raises(ValueError):
        await store.apply(
            actor=owner, owners=frozenset({owner.key}), expected_revision=0, change=change
        )
    assert (await store.snapshot()).revision == 0
    assert await store.audit() == ()


@pytest.mark.asyncio
async def test_actor_must_match_platform_even_when_owner_and_zero_audit_limit_is_empty():
    owner = UserRef("fake", "root")
    group = ChatRef("tg", "-1001")
    store = MemoryAdministrationStore()
    with pytest.raises(ValueError, match="same platform"):
        await store.apply(
            actor=owner,
            owners=frozenset({owner.key}),
            expected_revision=0,
            change=AdminChange("enable", group),
        )
    assert await store.audit(limit=0) == ()


@pytest.mark.asyncio
async def test_persisted_disable_overrides_bootstrap_across_policy_rebuild():
    owner = UserRef("tg", "1")
    group = ChatRef("tg", "-1001")
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("disable", group),
    )
    policy = AccessPolicy([group.key], [], owners=[owner.key], store=store)
    await policy.refresh()
    assert not policy.is_chat_allowed(group)
    rebuilt = AccessPolicy([group.key], [], owners=[owner.key], store=store)
    await rebuilt.refresh()
    assert not rebuilt.is_chat_allowed(group)


@pytest.mark.asyncio
async def test_assignment_admits_setup_dm_without_admin_role_or_membership():
    owner, delegate = UserRef("fake", "owner"), UserRef("fake", "delegate")
    group = ChatRef("fake", "group")
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("grant", group, user=delegate),
    )
    delegated = AccessPolicy([group.key], [], owners=[owner.key], store=store)
    await delegated.refresh()
    assert not delegated.is_admin(delegate)
    assert delegated.can_setup(delegate, group)
    assert await delegated.can_dm(delegate, FakePlatform(), memory_stores().cache)


@pytest.mark.asyncio
async def test_positive_telegram_bootstrap_user_is_not_a_group_candidate():
    policy = AccessPolicy(["tg:123", "tg:-1001"], [])
    assert policy.groups_on("tg") == [ChatRef("tg", "-1001")]


@pytest.mark.asyncio
async def test_disabled_group_membership_cache_no_longer_admits_dm():
    member = UserRef("fake", "member")
    group = ChatRef("fake", "group")
    owner = UserRef("fake", "owner")
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("disable", group),
    )
    policy = AccessPolicy([group.key], [], owners=[owner.key], store=store)
    await policy.refresh()
    cache = MemoryCache()
    await cache.set(f"member:{group.key}:{member.key}", b"1", ttl_s=3600)
    assert not await policy.can_dm(member, FakePlatform(), cache)


@pytest.mark.asyncio
async def test_memory_settings_updates_preserve_the_other_field_and_audit_no_text():
    owner = UserRef("fake", "owner")
    group = ChatRef("fake", "group")
    repo = MemoryCoreRepo()
    store = MemoryAdministrationStore(repo)
    await repo.save_chat_settings(ChatSettings(group, persona="Existing persona"), updated_by=owner)
    owners = frozenset({owner.key})
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=0,
        change=AdminChange("chatmode", group, value="on"),
    )
    settings = await repo.get_chat_settings(group)
    assert settings.persona == "Existing persona" and settings.chatmode
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=1,
        change=AdminChange("persona", group, value="Private persona text"),
    )
    settings = await repo.get_chat_settings(group)
    assert settings.persona == "Private persona text" and settings.chatmode
    audit = await store.audit()
    assert all("Private persona text" not in str(entry) for entry in audit)
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=2,
        change=AdminChange("persona_reset", group),
    )
    settings = await repo.get_chat_settings(group)
    assert settings.persona is None and settings.chatmode
