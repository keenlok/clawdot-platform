"""Narration tool availability; handler-time checks remain authoritative."""

from chatcore.skills import Gate, GateState, ToolGate

from chatcore_voice.permissions import may_narrate


def build_gates(plugin):
    if not plugin.narration_available:
        return {}

    async def scope(chat, is_dm):
        return Gate(GateState.AVAILABLE)

    async def user(ctx):
        if await may_narrate(plugin.access, ctx):
            return Gate(GateState.AVAILABLE)
        return Gate(GateState.NOT_ALLOWED)

    return {"narrate_text": ToolGate(scope=scope, user=user)}
