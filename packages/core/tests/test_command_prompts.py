from __future__ import annotations

from dataclasses import dataclass, replace

import chatcore.store.memory as memory_module
from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.command_prompts import CommandPromptStore, CommandReply
from chatcore.events import MessageRef
from chatcore.model_choice import build_model_switch
from chatcore.model_plugin import ModelPlugin
from chatcore.plugin import BasePlugin, Command
from chatcore.store.memory import MemoryCache
from chatcore.testing import GROUP, FakePlatform, memory_stores, message, user


class AskPlugin(BasePlugin):
    """``/echoname`` asks for a name and echoes it; ``/secret`` is admin-only."""

    name = "ask"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def commands(self):
        async def echoname(ctx, args):
            self.calls.append((ctx.user.ref.id, args))
            if not args:
                return CommandReply("What name?", awaits_reply=True, placeholder="A name")
            return f"Hello {args}"

        async def secret(ctx, args):
            self.calls.append((ctx.user.ref.id, "secret:" + args))
            return CommandReply("Secret what?", awaits_reply=True) if not args else "done"

        async def plain(ctx, args):
            return "plain text"

        return [
            Command("echoname", "Echo a name", echoname),
            Command("secret", "Admin thing", secret, admin_only=True),
            Command("plain", "Plain reply", plain),
        ]


def _app(*, platform=None, admins=(), group_mentions_only=False, extra_plugins=()):
    platform = platform or FakePlatform()
    stores = memory_stores()
    plugin = AskPlugin()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], list(admins)),
        plugins=[plugin, *extra_plugins],
        bot_name="bot",
        group_mentions_only=group_mentions_only,
    )
    return app, platform, plugin


def _reply(text, to: MessageRef, *, sender=None, mention=False):
    return replace(
        message(text, sender=sender or user("alice"), mention=mention),
        reply_to=to,
        reply_to_bot=True,
    )


async def _say(app, platform, event):
    before = len(platform.sent)
    await app.handle(event)
    return platform.sent[before:]


async def test_reply_to_a_prompt_is_the_command_input_and_runs_as_the_replier():
    app, platform, plugin = _app()
    [prompt] = await _say(app, platform, message("/echoname"))
    assert prompt.text == "What name?"

    [answer] = await _say(app, platform, _reply("Wei Ling", prompt.ref, sender=user("bob")))
    assert answer.text == "Hello Wei Ling"
    assert answer.reply_to is not None
    assert plugin.calls == [("alice", ""), ("bob", "Wei Ling")]


async def test_a_prompt_is_used_once_then_replies_go_to_the_responder():
    app, platform, plugin = _app()
    [prompt] = await _say(app, platform, message("/echoname"))
    await _say(app, platform, _reply("first", prompt.ref))
    [second] = await _say(app, platform, _reply("second", prompt.ref))
    assert second.text == "echo: second"
    assert plugin.calls[-1] == ("alice", "first")


async def test_cancel_closes_the_prompt():
    app, platform, plugin = _app()
    [prompt] = await _say(app, platform, message("/echoname"))
    [reply] = await _say(app, platform, _reply("Cancel", prompt.ref))
    assert reply.text == "Cancelled."
    assert plugin.calls == [("alice", "")]
    [later] = await _say(app, platform, _reply("Wei", prompt.ref))
    assert later.text == "echo: Wei"


async def test_a_slash_command_reply_runs_as_that_command_and_keeps_the_prompt():
    app, platform, plugin = _app()
    [prompt] = await _say(app, platform, message("/echoname"))
    [plain] = await _say(app, platform, _reply("/plain", prompt.ref))
    assert plain.text == "plain text"
    [answer] = await _say(app, platform, _reply("Wei", prompt.ref))
    assert answer.text == "Hello Wei"


async def test_prompts_expire(monkeypatch):
    app, platform, _plugin = _app()
    now = [1000.0]
    monkeypatch.setattr(memory_module.time, "monotonic", lambda: now[0])
    [prompt] = await _say(app, platform, message("/echoname"))
    now[0] += 601
    [reply] = await _say(app, platform, _reply("Wei", prompt.ref))
    assert reply.text == "echo: Wei"


async def test_plain_string_commands_open_no_prompt():
    app, platform, _plugin = _app()
    [plain] = await _say(app, platform, message("/plain"))
    [reply] = await _say(app, platform, _reply("again", plain.ref))
    assert reply.text == "echo: again"


async def test_admin_only_prompt_answered_by_a_non_admin_is_silent():
    app, platform, plugin = _app(admins=["fake:alice"])
    [prompt] = await _say(app, platform, message("/secret"))
    assert await _say(app, platform, _reply("x", prompt.ref, sender=user("bob"))) == []
    assert plugin.calls == [("alice", "secret:")]


async def test_mention_only_groups_take_untagged_and_tagged_answers():
    app, platform, _plugin = _app(group_mentions_only=True)
    [prompt] = await _say(app, platform, message("/echoname@bot"))
    [answer] = await _say(app, platform, _reply("Wei", prompt.ref))
    assert answer.text == "Hello Wei"

    [prompt] = await _say(app, platform, message("/echoname@bot"))
    [answer] = await _say(app, platform, _reply("@bot Siti", prompt.ref, mention=True))
    assert answer.text == "Hello Siti"


async def test_reply_prompt_platforms_open_the_reply_box():
    @dataclass
    class PromptPlatform(FakePlatform):
        def __post_init__(self):
            self.prompts: list[tuple[str, str]] = []

        async def send_reply_prompt(
            self, chat, text, *, reply_to=None, thread_id=None, placeholder=""
        ):
            self.prompts.append((text, placeholder))
            return await self.send(chat, text, reply_to=reply_to, thread_id=thread_id)

    platform = PromptPlatform()
    app, _platform, _plugin = _app(platform=platform)
    [prompt] = await _say(app, platform, message("/echoname"))
    await _say(app, platform, message("/plain"))
    assert platform.prompts == [("What name?", "A name")]
    [answer] = await _say(app, platform, _reply("Wei", prompt.ref))
    assert answer.text == "Hello Wei"


async def test_split_prompt_answered_on_any_part_closes_every_part():
    store = CommandPromptStore(MemoryCache())
    await store.open(GROUP, ["m1", "m2", "m1"], "echoname")
    assert await store.take(GROUP, "m2") == "echoname"
    assert await store.take(GROUP, "m1") is None
    assert await store.take(GROUP.__class__(GROUP.platform, "other"), "m1") is None


async def test_model_prompt_switches_on_reply_and_reasks_on_unknown_names():
    stores_platform = FakePlatform()
    probe = memory_stores()
    config = build_model_switch(
        provider_kind="openai_compat",
        default_model="deepseek-flash",
        choices_raw="glm-5,kimi:text",
        cache=probe.cache,
        duration_minutes=30,
        max_minutes=60,
        cooldown_s=0,
        who="anyone",
    )
    app = BotApp(
        stores_platform,
        stores=probe,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ModelPlugin(config, bot_name="bot")],
        bot_name="bot",
    )
    platform = stores_platform
    [shown] = await _say(app, platform, message("/model"))
    assert "Or reply to this message with a model name, or default." in shown.text

    [unknown] = await _say(app, platform, _reply("gpt-9", shown.ref))
    assert "Unknown model." in unknown.text and "Reply to this message" in unknown.text

    [switched] = await _say(app, platform, _reply("GLM-5", unknown.ref, sender=user("bob")))
    assert switched.text.startswith("Switched this chat to glm-5")
    assert (await config.store.get(GROUP)).set_by == "Bob"


async def test_restricted_people_get_the_notice_and_leave_the_prompt_open():
    from datetime import UTC, datetime

    from chatcore.administration.store import Restriction

    app, platform, plugin = _app()
    original = app.access.restriction_for

    def restriction_for(user_ref, chat, *, is_dm, **kwargs):
        if user_ref.id == "bob":
            return Restriction(
                user_ref.key, chat.key, "block", None, "fake:owner", datetime.now(UTC)
            )
        return original(user_ref, chat, is_dm=is_dm, **kwargs)

    app.access.restriction_for = restriction_for
    [prompt] = await _say(app, platform, message("/echoname"))
    [notice] = await _say(app, platform, _reply("cancel", prompt.ref, sender=user("bob")))
    assert notice.text == "You can't use this bot here right now."
    [answer] = await _say(app, platform, _reply("Wei", prompt.ref))
    assert answer.text == "Hello Wei"
    assert plugin.calls == [("alice", ""), ("alice", "Wei")]
