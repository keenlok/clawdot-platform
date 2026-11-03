"""End-to-end through the console adapter: the same path a real chat app takes."""

import io
from collections.abc import AsyncIterator, Callable

from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.pending import PendingAction
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.testing import memory_stores
from chatcore_console import ConsolePlatform

Line = str | Callable[[], str]


async def lazy(*items: Line) -> AsyncIterator[str]:
    """Lines may be callables, evaluated only when reached (e.g. to use an ID created earlier)."""
    for item in items:
        yield item() if callable(item) else item


class Ledger(BasePlugin):
    name = "ledger"

    def __init__(self) -> None:
        self.saved: list[dict] = []
        self.last_action: str = ""

    def commands(self) -> list[Command]:
        async def add(ctx: CommandContext, args: str) -> None:
            action = PendingAction.create(
                chat=ctx.chat,
                requester=ctx.user.ref,
                plugin=self.name,
                kind="add",
                payload={"what": args},
                summary=f"Add {args}?",
            )
            assert ctx.confirm is not None
            await ctx.confirm.request(action, ctx.user)
            self.last_action = action.id

        return [Command("add", "add a thing", add)]

    def action_handlers(self):
        saved = self.saved

        class Commit:
            async def commit(self, action, ctx):
                saved.append(action.payload)
                return "saved"

        return {"add": Commit()}


async def run(*script: Line, plugins=()) -> str:
    out = io.StringIO()
    platform = ConsolePlatform(bot_name="sbbot", lines=lazy(*script), out=out)
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=AccessPolicy(["console:group"], []),
        plugins=list(plugins),
        bot_name="sbbot",
    )
    await app.run()
    return out.getvalue()


async def test_mention_reply_and_dm():
    output = await run("hello everyone", "@sbbot ping", "::reply", "and again", "::dm", "secret?")
    assert "[group] sbbot: echo: ping" in output
    assert "[group] sbbot: echo: and again" in output
    assert "[dm] sbbot: echo: secret?" in output
    assert "hello everyone" not in output


async def test_reply_control_selects_nth_recent_bot_message():
    out = io.StringIO()
    platform = ConsolePlatform(
        bot_name="sbbot",
        lines=lazy("@sbbot first", "@sbbot second", "::reply 2", "@sbbot continue"),
        out=out,
    )

    class Capture:
        def __init__(self):
            self.reply_targets = []
            self.sent_refs = []

        async def respond(self, message, text, app):
            self.reply_targets.append(message.reply_to)
            self.sent_refs.append(
                await app.send(message.chat, f"answer: {text}", reply_to=message.ref)
            )

        async def consider(self, message, app):
            return None

    responder = Capture()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=AccessPolicy(["console:group"], []),
        responder=responder,
        bot_name="sbbot",
        group_mentions_only=True,
    )
    await app.run()

    assert responder.reply_targets[-1] == responder.sent_refs[0]
    assert "answer: continue" in out.getvalue()


async def test_other_chats_are_left():
    output = await run("::chat strangers", "@sbbot hi")
    assert "(sbbot left the chat)" in output
    assert "echo" not in output


async def test_chat_switch_rejects_sentinel_and_namespaced_values():
    output = await run("::chat *", "::chat", "::chat tg:group", "@sbbot hello")
    assert "[group] sbbot: echo: hello" in output
    assert output.count("unknown control") == 3


async def test_confirm_flow_only_for_requester():
    ledger = Ledger()
    output = await run(
        "/add dinner",
        "::as bob",
        lambda: f"::press {ledger.last_action} yes",  # bob isn't the requester: ignored
        "::as me",
        lambda: f"::press {ledger.last_action} yes",
        lambda: f"::press {ledger.last_action} yes",  # second press: already used
        plugins=[ledger],
    )
    assert "Add dinner?" in output and "[Confirm] [Cancel]" in output
    assert ledger.saved == [{"what": "dinner"}]
    assert output.count("sbbot: saved") == 1
    assert "already handled" in output
