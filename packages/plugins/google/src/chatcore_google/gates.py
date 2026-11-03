"""Default-on Google scope gates and separate writer/manager permissions."""

from chatcore.skills import Gate, GateState, ToolGate


def build_gates(plugin):
    async def configured(chat, is_dm):
        if not plugin.settings.configured:
            return Gate(GateState.UNCONFIGURED)
        return Gate(GateState.AVAILABLE)

    async def scope(chat, is_dm):
        if not plugin.settings.configured:
            return Gate(GateState.UNCONFIGURED)
        if not (await plugin.service.repo.settings(chat.key))[0]:
            return Gate(
                GateState.OFF, "Google tools are off here; ask a manager to use {cmd:google} on."
            )
        return Gate(GateState.AVAILABLE)

    async def browser_scope(chat, is_dm):
        if plugin.browser_edits is None:
            return Gate(GateState.UNCONFIGURED)
        if chat.key not in plugin.browser_config.allowed_chat_keys:
            return Gate(GateState.NOT_ALLOWED)
        return await scope(chat, is_dm)

    async def check(ctx, *, write=False, configure=False):
        try:
            await plugin.service.authorize(ctx, write=write, configure=configure)
            return Gate(GateState.AVAILABLE)
        except (PermissionError, ValueError):
            return Gate(GateState.NOT_ALLOWED)

    async def read(ctx):
        return await check(ctx)

    async def write(ctx):
        return await check(ctx, write=True)

    async def configure(ctx):
        return await check(ctx, configure=True)

    async def browser_write(ctx):
        if plugin.browser_edits is None or ctx.chat.key not in (
            plugin.browser_config.allowed_chat_keys if plugin.browser_config else ()
        ):
            return Gate(GateState.NOT_ALLOWED)
        return await check(ctx, write=True)

    return {
        tool.name: ToolGate(
            scope=(
                configured
                if tool.name == "configure_google"
                else browser_scope
                if tool.name == "google_browser_edit_doc"
                else scope
            ),
            user=(
                configure
                if tool.name == "configure_google"
                else browser_write
                if tool.name == "google_browser_edit_doc"
                else write
                if tool.writes
                else read
            ),
        )
        for tool in plugin.tools()
    }
