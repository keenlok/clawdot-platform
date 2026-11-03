from unittest.mock import AsyncMock

import pytest
from chatcore.access import AccessPolicy
from chatcore.administration.store import AdminChange, MemoryAdministrationStore
from chatcore.events import ChatRef, UserRef
from chatcore_jobs.models import Actor, Scope
from chatcore_jobs.permissions import can_use_scope, is_live_group_admin


class Platform:
    name = "tg"

    def __init__(self, *, group_admin=False, member=False, failure=False):
        self.is_group_admin = AsyncMock(return_value=group_admin)
        self.is_member = AsyncMock(return_value=member)
        if failure:
            self.is_group_admin.side_effect = RuntimeError("diagnostic")
            self.is_member.side_effect = RuntimeError("diagnostic")


@pytest.mark.asyncio
async def test_global_admin_is_not_group_admin():
    platform = Platform(group_admin=False)
    scope = Scope("tg:-100", False, None)
    assert not await is_live_group_admin(platform, scope, Actor("tg:7"))


@pytest.mark.asyncio
async def test_dm_job_access_includes_current_setup_delegate_assignments():
    group = ChatRef("tg", "-100")
    owner, delegate = UserRef("tg", "1"), UserRef("tg", "7")
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("grant", group, user=delegate),
    )
    access = AccessPolicy([group.key], [], store=store)
    await access.refresh()
    platform = Platform(member=False)
    assert await can_use_scope(
        Scope(delegate.key, True),
        Actor(delegate.key),
        access=access,
        platform=platform,
        dm_enabled=True,
    )
    platform.is_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_admin_lookup_fails_closed_for_dm_and_unsupported_platform():
    assert not await is_live_group_admin(
        Platform(group_admin=True), Scope("tg:7", True), Actor("tg:7")
    )
    assert not await is_live_group_admin(object(), Scope("tg:-100", False), Actor("tg:7"))


@pytest.mark.asyncio
async def test_dm_membership_is_fresh_and_not_group_routed():
    platform = Platform(member=True)
    access = AccessPolicy(["tg:-100"], [])
    scope = Scope("tg:7", True, None)
    assert await can_use_scope(
        scope, Actor("tg:7"), access=access, platform=platform, dm_enabled=True
    )
    platform.is_member.assert_awaited_once_with(ChatRef("tg", "-100"), UserRef("tg", "7"))


@pytest.mark.asyncio
async def test_dm_access_denies_lookup_failure_and_global_disable():
    access = AccessPolicy(["tg:-100"], [])
    scope = Scope("tg:7", True, None)
    assert not await can_use_scope(
        scope, Actor("tg:7"), access=access, platform=Platform(failure=True), dm_enabled=True
    )
    assert not await can_use_scope(
        scope, Actor("tg:7"), access=access, platform=Platform(member=True), dm_enabled=False
    )


@pytest.mark.asyncio
async def test_owner_dm_cancellation_authorization_can_survive_global_disable():
    platform = Platform(member=True)
    scope = Scope("tg:7", True, None)
    access = AccessPolicy(["tg:-100"], [])
    assert await can_use_scope(
        scope,
        Actor("tg:7"),
        access=access,
        platform=platform,
        dm_enabled=False,
        require_dm_enabled=False,
    )
