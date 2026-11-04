"""Pronunciation tool availability; handler-time checks remain authoritative."""

from chatcore.skills import Gate, GateState, ToolGate

from chatcore_tutor.permissions import may_pronounce


def build_gates(plugin):
    if not plugin.pronunciation_available:
        return {}

    async def scope(chat, is_dm):
        return Gate(GateState.AVAILABLE)

    async def user(ctx):
        if await may_pronounce(plugin.access, ctx):
            return Gate(GateState.AVAILABLE)
        return Gate(GateState.NOT_ALLOWED)

    return {"pronounce_tutor_text": ToolGate(scope=scope, user=user)}
