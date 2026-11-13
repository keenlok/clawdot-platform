"""Narrow authority for natural-language invitation management."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chatcore.access import AccessPolicy
    from chatcore.plugin import CommandContext
    from chatcore.tools.registry import ToolContext


def is_owner_dm(access: AccessPolicy, ctx: ToolContext | CommandContext) -> bool:
    return (
        ctx.is_dm
        and ctx.chat.key == ctx.user.ref.key
        and ctx.chat.platform == ctx.platform.name
        and ctx.user.ref.platform == ctx.platform.name
        and access.is_owner(ctx.user.ref)
    )
