"""Skill-layer gates for polls and poll settings.

Kept apart from ordinary tool code so release policy can review gate changes
separately (plans/2026-10-31-repo-layout.md, D6/D7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chatcore.skills import Gate, GateState, ToolGate

from chatcore_polls.models import Scope
from chatcore_polls.permissions import is_group_manager
from chatcore_polls.service import POLLS_OFF_HINT

if TYPE_CHECKING:
    from chatcore_polls.plugin import PollsPlugin


def build_gates(plugin: PollsPlugin) -> dict[str, ToolGate]:
    async def group(chat, is_dm):
        if is_dm:
            return Gate(GateState.NOT_ALLOWED, "Polls only work in groups.")
        return Gate(GateState.AVAILABLE)

    async def scope(chat, is_dm):
        if is_dm:
            return Gate(GateState.NOT_ALLOWED, "Polls only work in groups.")
        stores = getattr(plugin, "_stores", None)
        if stores is None or stores.db is None:
            raise RuntimeError("poll settings store is unavailable")

        current = await plugin.service.repo(stores).settings(Scope(chat.key, is_dm))
        if current["polls_enabled"]:
            return Gate(GateState.AVAILABLE)
        return Gate(GateState.OFF, POLLS_OFF_HINT)

    async def manager(ctx):
        target = Scope(ctx.chat.key, ctx.is_dm, ctx.thread_id)
        if await is_group_manager(plugin.access, plugin.platform, target, ctx.user.ref):
            return Gate(GateState.AVAILABLE)
        return Gate(GateState.NOT_ALLOWED)

    return {
        "create_poll": ToolGate(scope=scope),
        "list_polls": ToolGate(scope=group),
        "close_poll": ToolGate(scope=group),
        "configure_polls": ToolGate(scope=group, user=manager),
    }
