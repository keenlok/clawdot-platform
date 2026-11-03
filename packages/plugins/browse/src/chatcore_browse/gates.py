"""Skill-layer gates for rendered page reading and its settings.

Kept apart from ordinary tool code so release policy can review gate changes
separately (docs/plans/2026-10-31-repo-layout.md, D6/D7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chatcore.skills import Gate, GateState, ToolGate
from chatcore_jobs.models import Scope
from chatcore_jobs.permissions import is_group_manager, make_actor, make_scope

from chatcore_browse.repository import BrowseRepo

if TYPE_CHECKING:
    from chatcore_browse.plugin import BrowsePlugin


def build_gates(plugin: BrowsePlugin) -> dict[str, ToolGate]:
    async def rendered(chat, is_dm):
        stores = getattr(plugin, "_stores", None)
        if stores is None or stores.db is None:
            raise RuntimeError("browse settings store is unavailable")

        current = await BrowseRepo(stores.db, plugin.settings).settings(Scope(chat.key, is_dm))
        if current["rendered_enabled"]:
            return Gate(GateState.AVAILABLE)
        if is_dm:
            return Gate(GateState.OFF)
        return Gate(
            GateState.OFF,
            "Rendered page reading is off here. A group setup manager or admin can send "
            "{cmd:browsetools} on.",
        )

    async def configure_scope(chat, is_dm):
        return Gate(GateState.NOT_ALLOWED) if is_dm else Gate(GateState.AVAILABLE)

    async def configure_user(ctx):
        scope = make_scope(ctx.chat, is_dm=False, thread_id=ctx.thread_id)
        actor = make_actor(ctx.user.ref)
        if await is_group_manager(plugin.service.access, plugin.service.platform, scope, actor):
            return Gate(GateState.AVAILABLE)
        return Gate(GateState.NOT_ALLOWED)

    gates = {}
    if plugin.settings.render_url:
        gates["fetch_rendered_page"] = ToolGate(scope=rendered)
        gates["configure_rendered_fetch"] = ToolGate(scope=configure_scope, user=configure_user)
    return gates
