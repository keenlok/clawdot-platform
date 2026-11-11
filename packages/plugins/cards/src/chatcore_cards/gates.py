"""Tool availability gates for summary cards."""

from chatcore.skills import Gate, GateState, ToolGate

from chatcore_cards.permissions import may_send_card


def build_gates(plugin) -> dict[str, ToolGate]:
    if not plugin.cards_available:
        return {}

    async def scope(chat, is_dm):
        return Gate(GateState.AVAILABLE)

    async def user(ctx):
        if await may_send_card(plugin.access, ctx):
            return Gate(GateState.AVAILABLE)
        return Gate(GateState.NOT_ALLOWED)

    return {"send_card": ToolGate(scope=scope, user=user)}
