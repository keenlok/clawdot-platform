"""Skill-layer gates for expense and trip tools.

Kept apart from ordinary tool code so release policy can review gate changes
separately (docs/plans/2026-10-31-repo-layout.md, D6/D7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chatcore.skills import Gate, GateState, ToolGate

if TYPE_CHECKING:
    from chatcore_expenses.plugin import ExpensesPlugin


def build_gates(plugin: ExpensesPlugin) -> dict[str, ToolGate]:
    async def linked(ctx):
        if not ctx.is_dm:
            return Gate(GateState.AVAILABLE)
        if await plugin._chat(ctx) is not None:
            return Gate(GateState.AVAILABLE)
        return Gate(
            GateState.NOT_ALLOWED,
            "Expenses work in a group, or in a DM once you're linked to exactly one group.",
        )

    names = {tool.name for tool in plugin.tools()}
    return {name: ToolGate(user=linked) for name in names}
