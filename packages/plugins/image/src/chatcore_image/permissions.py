"""Call-time access checks for image generation."""

from __future__ import annotations

import logging

from chatcore.access import AccessPolicy
from chatcore.files import FilePlatform, PhotoPlatform
from chatcore.tools.registry import ToolContext

log = logging.getLogger("chatcore_image")


async def may_generate(access: AccessPolicy, ctx: ToolContext) -> bool:
    if (
        ctx.chat.platform != ctx.user.ref.platform
        or ctx.chat.platform != ctx.platform.name
        or not isinstance(ctx.platform, (PhotoPlatform, FilePlatform))
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
        log.warning("image access check failed error=%s", type(exc).__name__)
        return False
