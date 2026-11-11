"""Image generation tool availability; handler-time checks remain authoritative."""

from chatcore.skills import Gate, GateState, ToolGate

from chatcore_image.permissions import may_generate


def build_gates(plugin) -> dict[str, ToolGate]:
    if not plugin.generation_available:
        return {}

    async def scope(chat, is_dm):
        return Gate(GateState.AVAILABLE)

    async def user(ctx):
        if await may_generate(plugin.access, ctx):
            return Gate(GateState.AVAILABLE)
        return Gate(GateState.NOT_ALLOWED)

    return {"generate_image": ToolGate(scope=scope, user=user)}
