"""Voice tool availability; handler-time checks remain authoritative."""

import logging

from chatcore.skills import Gate, GateState, ToolGate

from chatcore_voice.permissions import may_change_voice_settings, may_narrate

log = logging.getLogger("chatcore_voice")


def build_gates(plugin):
    gates = {}

    async def scope(chat, is_dm):
        return Gate(GateState.AVAILABLE)

    async def user(ctx):
        if await may_narrate(plugin.access, ctx):
            return Gate(GateState.AVAILABLE)
        return Gate(GateState.NOT_ALLOWED)

    if plugin.narration_available:
        gates["narrate_text"] = ToolGate(scope=scope, user=user)

    async def settings_user(ctx):
        allowed = await may_change_voice_settings(
            plugin.access,
            chat=ctx.chat,
            user=ctx.user,
            is_dm=ctx.is_dm,
            platform=ctx.platform,
            cache=ctx.stores.cache,
            is_admin=ctx.is_admin,
        )
        return Gate(GateState.AVAILABLE if allowed else GateState.NOT_ALLOWED)

    async def settings_scope(chat, is_dm):
        return Gate(GateState.AVAILABLE)

    if plugin.narration_available or plugin.transcription.available:
        gates["voice_settings"] = ToolGate(scope=settings_scope, user=settings_user)
    return gates
