"""Fail-closed scoped authorization for reusable jobs."""

from __future__ import annotations

import logging

from chatcore.access import AccessPolicy
from chatcore.events import ChatRef, UserRef
from chatcore.group_access import GroupAdminPlatform
from chatcore.platform import ChatPlatform

from chatcore_jobs.models import Actor, Scope


def make_scope(chat: ChatRef, *, is_dm: bool, thread_id: str | None) -> Scope:
    return Scope(chat.key, is_dm, thread_id)


def make_actor(user: UserRef) -> Actor:
    return Actor(user.key)


async def is_live_group_admin(platform: ChatPlatform, scope: Scope, actor: Actor) -> bool:
    if scope.is_dm:
        return False
    chat = ChatRef.parse(scope.chat_key)
    user = UserRef.parse(actor.user_key)
    if chat.platform != user.platform or getattr(platform, "name", None) != chat.platform:
        return False
    method = getattr(platform, "is_group_admin", None)
    if not callable(method):
        return False
    try:
        return bool(await method(chat, user))
    except Exception:
        return False


async def is_group_manager(
    access: AccessPolicy, platform: ChatPlatform, scope: Scope, actor: Actor
) -> bool:
    """Accept an assigned setup manager or a current native group administrator."""
    if scope.is_dm:
        return False
    refreshed = True
    try:
        await access.refresh()
    except Exception as exc:
        refreshed = False
        logging.getLogger(__name__).warning(
            "job manager access refresh failed: %s", type(exc).__name__
        )
    chat = ChatRef.parse(scope.chat_key)
    user = UserRef.parse(actor.user_key)
    if refreshed and access.can_setup(user, chat):
        return True
    return await is_live_group_admin(platform, scope, actor)


async def can_use_scope(
    scope: Scope,
    actor: Actor,
    *,
    access: AccessPolicy,
    platform: ChatPlatform,
    dm_enabled: bool,
    require_dm_enabled: bool = True,
) -> bool:
    chat = ChatRef.parse(scope.chat_key)
    user = UserRef.parse(actor.user_key)
    if chat.platform != user.platform or getattr(platform, "name", None) != chat.platform:
        return False
    if scope.is_dm:
        if scope.chat_key != actor.user_key or chat.platform != user.platform:
            return False
        if require_dm_enabled and not dm_enabled:
            return False
        if access.is_admin(user):
            return True
        if access.assigned_groups(user):
            return True
        # Deliberately bypass AccessPolicy's one-hour membership cache for jobs.
        for group in access.groups_on(platform.name):
            try:
                if await platform.is_member(group, user):
                    return True
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "DM membership lookup failed: %s", type(exc).__name__
                )
                continue
        return False
    return access.is_chat_allowed(chat)


__all__ = [
    "Actor",
    "GroupAdminPlatform",
    "Scope",
    "can_use_scope",
    "is_live_group_admin",
    "is_group_manager",
    "make_actor",
    "make_scope",
]
