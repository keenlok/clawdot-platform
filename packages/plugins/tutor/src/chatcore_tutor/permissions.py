"""Live authorization checks for tutor operations."""

import logging

from chatcore.access import AccessPolicy
from chatcore.events import ChatRef, UserRef
from chatcore.files import VoicePlatform
from chatcore.group_access import GroupAdminPlatform
from chatcore.platform import ChatPlatform
from chatcore.tools.registry import ToolContext

log = logging.getLogger(__name__)


async def is_group_manager(
    access: AccessPolicy,
    platform: ChatPlatform,
    chat: ChatRef,
    user: UserRef,
) -> bool:
    if user.platform != chat.platform or platform.name != chat.platform:
        return False
    refreshed = True
    try:
        await access.refresh()
    except Exception as exc:
        refreshed = False
        log.warning(
            "tutor manager access refresh failed chat=%s user=%s error=%s",
            chat.key,
            user.key,
            type(exc).__name__,
        )
    if refreshed and access.can_setup(user, chat):
        return True
    if not isinstance(platform, GroupAdminPlatform):
        return False
    try:
        return bool(await platform.is_group_admin(chat, user))
    except Exception as exc:
        log.warning(
            "tutor manager check failed chat=%s user=%s error=%s",
            chat.key,
            user.key,
            type(exc).__name__,
        )
        return False


async def may_pronounce(access: AccessPolicy, ctx: ToolContext) -> bool:
    """Refresh access and fail closed for unsupported namespaces, restrictions or DM access."""
    if (
        ctx.chat.platform != ctx.user.ref.platform
        or ctx.chat.platform != ctx.platform.name
        or not isinstance(ctx.platform, VoicePlatform)
    ):
        return False
    try:
        await access.refresh()
        if access.restriction_for(ctx.user.ref, ctx.chat, is_dm=ctx.is_dm) is not None:
            return False
        if ctx.is_dm:
            return await access.can_dm(ctx.user.ref, ctx.platform, ctx.stores.cache)
        return access.is_chat_allowed(ctx.chat)
    except Exception as exc:
        log.warning("tutor pronunciation access check failed error=%s", type(exc).__name__)
        return False
