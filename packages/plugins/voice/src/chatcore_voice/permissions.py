"""Call-time access checks for voice narration."""

from __future__ import annotations

import logging

from chatcore.access import AccessPolicy
from chatcore.files import VoicePlatform
from chatcore.tools.registry import ToolContext

log = logging.getLogger("chatcore_voice")


async def may_narrate(access: AccessPolicy, ctx: ToolContext) -> bool:
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
        log.warning("voice narration access check failed error=%s", type(exc).__name__)
        return False


async def may_change_voice_settings(
    access: AccessPolicy, *, chat, user, is_dm: bool, platform, cache, is_admin: bool = False
) -> bool:
    """Authorize per-chat settings with current DM access or group administrator status."""
    if chat.platform != user.ref.platform or chat.platform != platform.name:
        return False
    try:
        await access.refresh()
        if access.restriction_for(user.ref, chat, is_dm=is_dm) is not None:
            return False
        if access.is_admin(user.ref):
            return True
        if is_dm:
            return await access.can_dm(user.ref, platform, cache)
        if not access.is_chat_allowed(chat):
            return False
        if is_admin:
            return True
        checker = getattr(platform, "is_group_admin", None)
        return bool(await checker(chat, user.ref)) if callable(checker) else False
    except Exception as exc:
        log.warning("voice settings access check failed error=%s", type(exc).__name__)
        return False
