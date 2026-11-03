"""Skill-layer gates for reminders, website watches and their settings.

Kept apart from ordinary tool code so release policy can review gate changes
separately (docs/plans/2026-10-31-repo-layout.md, D6/D7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chatcore.skills import Gate, GateState, ToolGate

from chatcore_jobs.models import JobKind, Scope
from chatcore_jobs.permissions import is_group_manager, make_actor, make_scope
from chatcore_jobs.service import DISABLED_HINTS

if TYPE_CHECKING:
    from chatcore_jobs.plugin import JobsPlugin


def build_gates(plugin: JobsPlugin) -> dict[str, ToolGate]:
    async def enabled(chat, is_dm, key):
        stores = getattr(plugin, "_stores", None)
        if stores is None or stores.db is None:
            raise RuntimeError("jobs settings store is unavailable")
        config = await plugin.service.repo(stores).settings(Scope(chat.key, is_dm))
        is_enabled = config["reminders_enabled" if key == "reminder" else "watches_enabled"]
        if is_enabled:
            return Gate(GateState.AVAILABLE)
        if is_dm:
            return Gate(GateState.OFF)
        return Gate(
            GateState.OFF,
            DISABLED_HINTS[JobKind.REMINDER if key == "reminder" else JobKind.WATCH],
        )

    async def reminders(chat, is_dm):
        return await enabled(chat, is_dm, "reminder")

    async def watches(chat, is_dm):
        return await enabled(chat, is_dm, "watch")

    async def configure_scope(chat, is_dm):
        if is_dm:
            return Gate(GateState.NOT_ALLOWED)
        return Gate(GateState.AVAILABLE)

    async def configure_user(ctx):
        scope = make_scope(ctx.chat, is_dm=False, thread_id=ctx.thread_id)
        actor = make_actor(ctx.user.ref)
        if await is_group_manager(plugin.access, plugin.platform, scope, actor):
            return Gate(GateState.AVAILABLE)
        return Gate(GateState.NOT_ALLOWED)

    gates = {
        "create_reminder": ToolGate(scope=reminders),
        "restore_reminder": ToolGate(scope=reminders),
        "create_website_watch": ToolGate(scope=watches),
        "restore_website_watch": ToolGate(scope=watches),
        "configure_jobs": ToolGate(scope=configure_scope, user=configure_user),
    }
    if plugin.rendered_fetcher is not None:

        async def variants(chat, is_dm):
            if plugin.rendered_allowed is None or plugin.rendered_fetcher is None:
                return ("Rendered watches are off in this chat; plain watches are available.",)
            allowed = await plugin.rendered_allowed(Scope(chat.key, is_dm))
            return (
                ()
                if allowed
                else ("Rendered watches are off in this chat; plain watches are available.",)
            )

        gates["create_website_watch"] = ToolGate(scope=watches, variant_hints=variants)
    return gates
