from __future__ import annotations

from datetime import UTC, datetime, timedelta

from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.model_choice import build_model_switch
from chatcore.model_plugin import ModelPlugin
from chatcore.testing import GROUP, FakePlatform, memory_stores, message, user
from chatcore.tools.registry import ToolContext


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _app(
    *,
    who="anyone",
    platform=None,
    admins=(),
    group_mentions_only=False,
    choices_raw="glm-5,kimi:text,x:train,y:text:train",
    image_fallback=None,
):
    platform = platform or FakePlatform()
    stores = memory_stores()
    clock = Clock()
    config = build_model_switch(
        provider_kind="openai_compat",
        default_model="deepseek-flash",
        choices_raw=choices_raw,
        cache=stores.cache,
        duration_minutes=30,
        max_minutes=60,
        cooldown_s=300,
        who=who,
        clock=clock,
    )
    access = AccessPolicy([GROUP.key], list(admins))
    plugin = ModelPlugin(config, access, bot_name="bot", clock=clock, image_fallback=image_fallback)
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[plugin],
        bot_name="bot",
        group_mentions_only=group_mentions_only,
    )
    return app, platform, config, clock


async def _say(app, platform, text, sender=None, **kwargs):
    before = len(platform.sent)
    await app.handle(message(text, sender=sender or user("alice"), **kwargs))
    return "\n".join(item.text for item in platform.sent[before:])


async def test_show_lists_default_and_choices():
    app, platform, _config, _clock = _app()
    reply = await _say(app, platform, "/model")
    assert "Model here: deepseek-flash (default)." in reply
    assert (
        "Choices: glm-5, kimi (text only), x (trains on chats), y (text only, trains on chats)."
        in reply
    )
    assert "/model@bot <name> switches this chat for 30 minutes" in reply


async def test_anyone_can_switch_and_the_reply_names_who_and_until():
    app, platform, config, clock = _app()
    reply = await _say(app, platform, "/model GLM-5", sender=user("bob"))
    assert reply.startswith("Switched this chat to glm-5 for 30 minutes (set by Bob")
    assert "/model@bot default switches back now." in reply
    override = await config.store.get(GROUP)
    assert override is not None and override.model == "glm-5"
    assert override.until == clock.now + timedelta(minutes=30)

    shown = await _say(app, platform, "/model")
    assert "Model here: glm-5, switched by Bob until" in shown


async def test_text_only_choice_says_images_are_not_read():
    app, platform, _config, _clock = _app()
    reply = await _say(app, platform, "/model kimi")
    assert "Images aren't read while it's on." in reply


async def test_text_only_choice_names_description_fallback():
    app, platform, _config, _clock = _app(image_fallback="vision-model")
    reply = await _say(app, platform, "/model kimi")
    assert "Images are described by vision-model while it's on." in reply

    plugin = next(plugin for plugin in app.plugins if plugin.name == "model")
    ctx = ToolContext(
        chat=GROUP,
        user=user("alice"),
        is_dm=False,
        platform=platform,
        stores=app.stores,
    )
    info = await plugin._get_model_info(ctx)
    assert "Images are described by vision-model while it's on." in info.content


async def test_text_only_model_info_without_fallback_keeps_original_exact_text():
    app, platform, _config, _clock = _app()
    await _say(app, platform, "/model kimi")
    plugin = next(plugin for plugin in app.plugins if plugin.name == "model")
    ctx = ToolContext(
        chat=GROUP,
        user=user("alice"),
        is_dm=False,
        platform=platform,
        stores=app.stores,
    )
    info = await plugin._get_model_info(ctx)
    assert info.content == (
        "Current model: kimi (text only), a temporary switch until 2026-11-01 12:30:00 UTC "
        "(UTC+00:00). Default: deepseek-flash. Choices marked (trains on chats) are run by "
        "providers that use prompts and replies to train their models; the current model isn't "
        "marked that way. Choices: glm-5, kimi (text only), x (trains on chats), y (text only, "
        "trains on chats). You can't change the model yourself. To switch, the person sends "
        "/model <name>; /model default switches back."
    )


async def test_training_model_switch_warns_and_keeps_switch_back_at_the_end():
    app, platform, _config, _clock = _app()
    reply = await _say(app, platform, "/model x")
    notice = (
        "Heads up: while this switch is on, x's provider uses the prompts and replies in this "
        "chat to train its models, so don't share anything private."
    )
    assert notice in reply
    assert reply.endswith("/model@bot default switches back now.")


async def test_text_only_training_switch_shows_both_notices():
    app, platform, _config, _clock = _app()
    reply = await _say(app, platform, "/model y")
    assert "Images aren't read while it's on." in reply
    assert "y's provider uses the prompts and replies in this chat to train its models" in reply
    assert reply.endswith("/model@bot default switches back now.")


async def test_non_training_switch_reply_stays_byte_identical():
    app, platform, _config, _clock = _app()
    reply = await _say(app, platform, "/model glm-5")
    assert reply == (
        "Switched this chat to glm-5 for 30 minutes (set by Alice, reverts "
        "2026-11-01 12:30:00 UTC (UTC+00:00)). "
        "/model@bot default switches back now."
    )


async def test_already_on_training_model_warns_and_says_how_to_switch_back():
    app, platform, config, _clock = _app()
    await config.store.switch(GROUP, "x", set_by="Alice")
    reply = await _say(app, platform, "/model x")
    assert "already on x (trains on chats) until" in reply
    assert "x's provider uses the prompts and replies in this chat to train its models" in reply
    assert reply.endswith("/model@bot default switches back now.")


async def test_show_training_override_includes_notice_line():
    app, platform, config, _clock = _app()
    await config.store.switch(GROUP, "x", set_by="Alice")
    reply = await _say(app, platform, "/model")
    assert "Model here: x (trains on chats), switched by Alice" in reply
    assert "Heads up: while this switch is on, x's provider uses the prompts and replies" in reply


async def test_unknown_name_lists_training_labels():
    app, platform, _config, _clock = _app()
    reply = await _say(app, platform, "/model missing")
    assert "x (trains on chats), y (text only, trains on chats)" in reply


async def test_unknown_name_cooldown_and_default():
    app, platform, config, _clock = _app()
    assert (
        "Unknown model. Choose one of: glm-5, kimi (text only), x (trains on chats), "
        "y (text only, trains on chats), or default."
    ) in await _say(app, platform, "/model gpt-9")
    assert "Use /model@bot, /model@bot <name>" in await _say(app, platform, "/model a b")
    assert "already on the default model" in await _say(app, platform, "/model default")

    await _say(app, platform, "/model glm-5")
    assert "Try again in 5 minutes." in await _say(app, platform, "/model kimi")
    assert "already on glm-5 until" in await _say(app, platform, "/model glm-5")

    assert "back to the default model, deepseek-flash" in await _say(
        app, platform, "/model default"
    )
    assert await config.store.get(GROUP) is None


async def test_naming_the_default_model_switches_back():
    app, platform, config, _clock = _app()
    await _say(app, platform, "/model glm-5")
    assert "back to the default model" in await _say(app, platform, "/model deepseek-flash")
    assert await config.store.get(GROUP) is None


async def test_admins_mode_refuses_members_but_allows_admins_and_show():
    class AdminPlatform(FakePlatform):
        async def is_group_admin(self, chat, user_ref):
            return user_ref.id == "carol"

    app, platform, config, _clock = _app(who="admins", platform=AdminPlatform())
    assert "Only group admins and setup managers" in await _say(app, platform, "/model glm-5")
    assert await config.store.get(GROUP) is None
    assert "Model here: deepseek-flash" in await _say(app, platform, "/model")
    assert "already on the default model" in await _say(app, platform, "/model default")

    assert "Switched this chat to glm-5" in await _say(
        app, platform, "/model glm-5", sender=user("carol")
    )
    assert "back to the default model" in await _say(app, platform, "/model default")


async def test_admins_mode_accepts_bot_admins_in_dms():
    app, platform, config, _clock = _app(who="admins", admins=["fake:alice"])
    # Bot admins pass in any chat, including a DM.
    reply = await _say(app, platform, "/model glm-5", dm=True)
    assert "Switched this chat to glm-5" in reply


async def test_model_command_is_in_help_under_its_own_group():
    app, platform, _config, _clock = _app()
    assert app.command_groups["model"] == "Model"
    help_text = await _say(app, platform, "/help")
    assert "Model:\n/model - Show or switch this chat's model for a while" in help_text


async def test_get_model_info_reports_without_writing():
    app, platform, config, clock = _app()
    tool = app.tools.get("get_model_info")
    assert tool is not None and not tool.writes
    ctx = ToolContext(GROUP, user("alice"), False, platform, app.stores)
    ctx.command_form = lambda name: f"/{name}@bot"
    result = await tool.handler(ctx)
    assert "Current model: deepseek-flash (the default)." in result.content
    assert (
        "Choices marked (trains on chats) are run by providers that use prompts and replies "
        "to train their models; the current model isn't marked that way."
    ) in result.content
    assert "You can't change the model yourself." in result.content
    assert "/model@bot <name>" in result.content
    assert not result.untrusted

    await config.store.switch(GROUP, "kimi", set_by="Alice <script>")
    result = await tool.handler(ctx)
    assert "Current model: kimi (text only), a temporary switch until" in result.content
    assert "the current model isn't marked that way" in result.content
    # The display name is member-written; the tool never returns it.
    assert "Alice" not in result.content
    assert await config.store.get(GROUP) is not None

    await config.store.clear(GROUP)
    clock.now += timedelta(minutes=5)
    await config.store.switch(GROUP, "x", set_by="Alice")
    result = await tool.handler(ctx)
    assert (
        "The provider of the current model uses prompts and replies in this chat to train"
        in result.content
    )
    assert "it isn't safe to share anything private here now" in result.content
    assert "/model@bot default switches back" in result.content
    assert "isn't marked that way" not in result.content


async def test_get_model_info_without_training_choices_is_unchanged():
    app, platform, _config, _clock = _app(choices_raw="glm-5,kimi:text")
    tool = app.tools.get("get_model_info")
    assert tool is not None
    ctx = ToolContext(GROUP, user("alice"), False, platform, app.stores)
    ctx.command_form = lambda name: f"/{name}@bot"
    result = await tool.handler(ctx)
    assert result.content == (
        "Current model: deepseek-flash (the default). Choices: glm-5, kimi (text only). "
        "You can't change the model yourself. To switch, the person sends /model@bot <name>; "
        "/model@bot default switches back."
    )


def test_bot_model_skill_declares_only_the_read_only_tool():
    app, _platform, _config, _clock = _app()
    skill = app.skills.get("bot_model")
    assert skill is not None
    assert skill.tools == ("get_model_info",)
    assert skill.commands == ("model",)
    assert 4 <= len(skill.examples) <= 12
