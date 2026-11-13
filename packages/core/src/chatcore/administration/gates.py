"""Keep owner-only invitation tools out of other people's model requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chatcore.administration.permissions import is_owner_dm
from chatcore.skills import Gate, GateState, ToolGate

if TYPE_CHECKING:
    from chatcore.administration.plugin import AdministrationPlugin


def build_gates(plugin: AdministrationPlugin) -> dict[str, ToolGate]:
    async def scope(chat, is_dm):
        return Gate(GateState.AVAILABLE if is_dm else GateState.NOT_ALLOWED)

    async def owner(ctx):
        return Gate(
            GateState.AVAILABLE if is_owner_dm(plugin.access, ctx) else GateState.NOT_ALLOWED
        )

    return {"manage_trusted_inviters": ToolGate(scope=scope, user=owner)}
