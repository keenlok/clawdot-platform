from datetime import UTC, datetime, timedelta

import pytest
from chatcore.access import AccessPolicy
from chatcore.administration.plugin import AdministrationPlugin
from chatcore.administration.store import AdminChange, MemoryAdministrationStore
from chatcore.events import ChatRef, User, UserRef
from chatcore.testing import FakePlatform, memory_stores
from chatcore.tools.registry import ToolContext

OWNER = UserRef("tg", "1")
FRIEND = UserRef("tg", "2")
OWNERS = frozenset({OWNER.key})


async def test_dm_grant_and_revoke_are_revisioned_and_audited():
    store = MemoryAdministrationStore()
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    snapshot = await store.apply(
        actor=OWNER,
        owners=OWNERS,
        expected_revision=0,
        change=AdminChange("dm_grant", ChatRef("tg", "*"), FRIEND, expiry),
    )
    assert snapshot.revision == 1
    assert snapshot.dm_grants[FRIEND.key].expires_at is not None
    assert (await store.audit())[0].detail == expiry
    same = await store.apply(
        actor=OWNER,
        owners=OWNERS,
        expected_revision=1,
        change=AdminChange("dm_grant", ChatRef("tg", "*"), FRIEND, expiry),
    )
    assert same.revision == 1 and len(await store.audit()) == 1
    removed = await store.apply(
        actor=OWNER,
        owners=OWNERS,
        expected_revision=1,
        change=AdminChange("dm_revoke", ChatRef("tg", "*"), FRIEND),
    )
    assert removed.revision == 2 and FRIEND.key not in removed.dm_grants
    no_op = await store.apply(
        actor=OWNER,
        owners=OWNERS,
        expected_revision=2,
        change=AdminChange("dm_revoke", ChatRef("tg", "*"), FRIEND),
    )
    assert no_op.revision == 2 and len(await store.audit()) == 2


@pytest.mark.parametrize("batch", [False, True])
async def test_memory_dm_audit_failure_leaves_grants_revision_and_audit_unchanged(
    batch, monkeypatch
):
    store = MemoryAdministrationStore()
    baseline = await store.snapshot()

    def fail_audit_append(entries, entry):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(store, "_append_audit", fail_audit_append)
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    with pytest.raises(RuntimeError, match="injected audit failure"):
        if batch:
            await store.apply_inviter_batch(
                actor=OWNER,
                owners=OWNERS,
                expected_revision=0,
                operation="dm_grant",
                users=(FRIEND, UserRef("tg", "3")),
                value=expiry,
            )
        else:
            await store.apply(
                actor=OWNER,
                owners=OWNERS,
                expected_revision=0,
                change=AdminChange("dm_grant", ChatRef("tg", "*"), FRIEND, expiry),
            )
    assert await store.snapshot() == baseline
    assert await store.audit() == ()


@pytest.mark.parametrize("batch", [False, True])
async def test_dm_grant_expiry_rechecked_under_memory_revision_lock(monkeypatch, batch):
    import chatcore.administration.store as store_module

    class Clock(datetime):
        current = datetime.now(UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz else cls.current.replace(tzinfo=None)

    expiry = Clock.current + timedelta(seconds=1)
    expiry_text = expiry.isoformat()
    original_validate = store_module.validate_change

    def validate_then_expire(change):
        original_validate(change)
        Clock.current = expiry + timedelta(seconds=1)

    monkeypatch.setattr(store_module, "datetime", Clock)
    monkeypatch.setattr(store_module, "validate_change", validate_then_expire)
    store = MemoryAdministrationStore()
    with pytest.raises(ValueError, match="future"):
        if batch:
            await store.apply_inviter_batch(
                actor=OWNER,
                owners=OWNERS,
                expected_revision=0,
                operation="dm_grant",
                users=(FRIEND,),
                value=expiry_text,
            )
        else:
            await store.apply(
                actor=OWNER,
                owners=OWNERS,
                expected_revision=0,
                change=AdminChange("dm_grant", ChatRef("tg", "*"), FRIEND, expiry_text),
            )
    assert (await store.snapshot()).revision == 0
    assert not (await store.snapshot()).dm_grants
    assert await store.audit() == ()


async def test_dm_grants_control_only_dm_access_and_expire_at_check_time():
    store = MemoryAdministrationStore()
    policy = AccessPolicy([], [], owners=OWNERS, store=store)
    await policy.refresh()
    cache = memory_stores().cache
    platform = FakePlatform(name="tg")
    assert not await policy.can_dm(FRIEND, platform, cache)
    expiry = datetime.now(UTC) + timedelta(minutes=2)
    await store.apply(
        actor=OWNER,
        owners=OWNERS,
        expected_revision=0,
        change=AdminChange("dm_grant", ChatRef("tg", "*"), FRIEND, expiry.isoformat()),
    )
    await policy.refresh()
    assert policy.has_dm_grant(FRIEND, now=expiry - timedelta(seconds=1))
    assert not policy.has_dm_grant(FRIEND, now=expiry + timedelta(seconds=1))
    assert await policy.can_dm(FRIEND, platform, cache)
    assert await policy.member_groups(FRIEND, platform, cache) == []


async def test_natural_dm_batch_freezes_known_and_explicit_id_then_commits():
    group = ChatRef("tg", "-1001")
    platform = FakePlatform(name="tg")
    platform.members[group.key] = {OWNER.key}
    stores = memory_stores()
    await stores.repo.upsert_member(group, User(FRIEND, "Alex", "alex"))
    store = MemoryAdministrationStore(stores.repo)
    access = AccessPolicy([group.key], [], owners=OWNERS, store=store)
    await access.refresh()
    plugin = AdministrationPlugin(access, store, OWNERS)
    ctx = ToolContext(
        ChatRef("tg", OWNER.id), User(OWNER, "Owner"), True, platform, stores, is_admin=True
    )
    tool = {item.name: item for item in plugin.tools()}["manage_dm_access"]
    result = await tool.handler(ctx, operation="grant", people=["Alex", "tg:9"])
    assert result.pending_action is not None
    assert "Alex" in result.content and "unknown name [tg:9]" in result.content
    assert not (await store.snapshot()).dm_grants
    from chatcore.plugin import CommandContext

    command_ctx = CommandContext(
        chat=ctx.chat,
        user=ctx.user,
        is_dm=True,
        is_admin=True,
        platform=platform,
        stores=stores,
    )
    reply = await plugin.commit(result.pending_action, command_ctx)
    assert "Saved dm_grant" in reply
    assert set((await store.snapshot()).dm_grants) == {FRIEND.key, "tg:9"}


async def test_dm_batch_preview_never_treats_a_directory_name_as_synthetic():
    from chatcore.display_time import format_instant
    from chatcore.display_time_store import display_service

    group = ChatRef("tg", "-1001")
    platform = FakePlatform(name="tg")
    platform.members[group.key] = {OWNER.key}
    stores = memory_stores()
    await stores.repo.upsert_member(group, User(FRIEND, "unknown name [tg:999]", "spoofed"))
    store = MemoryAdministrationStore(stores.repo)
    access = AccessPolicy([group.key], [], owners=OWNERS, store=store)
    await access.refresh()
    plugin = AdministrationPlugin(access, store, OWNERS)
    ctx = ToolContext(
        ChatRef("tg", OWNER.id), User(OWNER, "Owner"), True, platform, stores, is_admin=True
    )
    tool = {item.name: item for item in plugin.tools()}["manage_dm_access"]
    result = await tool.handler(
        ctx, operation="grant", people=["unknown name [tg:999]", "tg:999"], duration="1h"
    )
    assert result.pending_action is not None
    assert "unknown name [tg:999] (@spoofed) [tg:2]" in result.content
    expiry = datetime.fromisoformat(result.pending_action.payload["value"].replace("Z", "+00:00"))
    zone, _ = await display_service(stores).resolve(ctx.chat, ctx.user.ref, is_dm=True)
    assert format_instant(expiry, zone) in result.content
    assert result.pending_action.payload["value"] not in result.content
    assert "only if typed by the owner" in tool.parameters["properties"]["people"]["description"]
    assert "never invented" in tool.parameters["properties"]["people"]["items"]["description"]


@pytest.mark.parametrize("restriction", ["block", "pause"])
async def test_global_restrictions_still_deny_a_dm_grantee(restriction):
    store = MemoryAdministrationStore()
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await store.apply(
        actor=OWNER,
        owners=OWNERS,
        expected_revision=0,
        change=AdminChange("dm_grant", ChatRef("tg", "*"), FRIEND, expiry),
    )
    await store.apply(
        actor=OWNER,
        owners=OWNERS,
        expected_revision=1,
        change=AdminChange(
            restriction,
            ChatRef("tg", "*"),
            FRIEND,
            3600 if restriction == "pause" else None,
        ),
    )
    policy = AccessPolicy([], [], owners=OWNERS, store=store)
    await policy.refresh()
    assert await policy.can_dm(FRIEND, FakePlatform(name="tg"), memory_stores().cache)
    assert policy.restriction_for(FRIEND, ChatRef("tg", FRIEND.id), is_dm=True) is not None


@pytest.mark.parametrize("value", ["bad", "2026-01-01T00:00:00", "2000-01-01T00:00:00Z"])
async def test_dm_grant_expiry_must_be_future_and_aware(value):
    store = MemoryAdministrationStore()
    with pytest.raises(ValueError):
        await store.apply(
            actor=OWNER,
            owners=OWNERS,
            expected_revision=0,
            change=AdminChange("dm_grant", ChatRef("tg", "*"), FRIEND, value),
        )
    assert (await store.snapshot()).revision == 0
