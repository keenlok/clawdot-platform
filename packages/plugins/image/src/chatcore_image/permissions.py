"""Call-time access checks for image generation."""

from __future__ import annotations

import logging

from chatcore.access import AccessPolicy
from chatcore.events import Attachment
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


def may_use_reference(ctx: ToolContext, attachment: Attachment, owner: str) -> bool:
    """Whether the requester may edit this photo: any in a DM; in a group their own, or one
    they replied to directly (validated by core)."""
    return (
        ctx.is_dm
        or owner == ctx.user.ref.key
        or attachment.id in ctx.turn.explicit_reply_attachment_ids
    )
