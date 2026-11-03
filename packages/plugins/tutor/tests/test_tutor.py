import pytest
from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.testing import FakePlatform, memory_stores, message
from chatcore.tools.registry import ToolContext
from chatcore_tutor.languages import LANGUAGES, LEVELS, lookup_language, parse_level
from chatcore_tutor.plugin import LANGUAGE_TUTOR_INSTRUCTIONS, TutorPlugin, parse_tutor_args
from chatcore_tutor.repository import MemoryTutorRepo, _validated_profile
from chatcore_tutor.settings import TutorSettings


def make_plugin():
    return TutorPlugin(AccessPolicy([], []), FakePlatform())


def test_language_aliases_and_levels():
    assert lookup_language("JA").name == "Japanese"
    assert lookup_language("日本語").name == "Japanese"
    assert lookup_language("bahasa melayu").name == "Malay"
    assert parse_level("b2") == "B2"
    assert parse_level("bad") is None
    assert LEVELS == ("A1", "A2", "B1", "B2", "C1", "C2")


@pytest.mark.parametrize(
    ("args", "target", "level", "explain"),
    [
        ("Japanese A2", "Japanese", "A2", ()),
        ("A2 日本語 English", "Japanese", "A2", ("English",)),
        ("bahasa melayu b1", "Malay", "B1", ()),
        ("ja english c1", "Japanese", "C1", ("English",)),
        ("cantonese A1 english and chinese", "Cantonese", "A1", ("English", "Mandarin Chinese")),
        ("cantonese A1 english,chinese", "Cantonese", "A1", ("English", "Mandarin Chinese")),
        ("cantonese A1 english/chinese", "Cantonese", "A1", ("English", "Mandarin Chinese")),
        ("cantonese A1 english+chinese", "Cantonese", "A1", ("English", "Mandarin Chinese")),
        ("cantonese A1 english & chinese", "Cantonese", "A1", ("English", "Mandarin Chinese")),
        ("cantonese A1 english / chinese", "Cantonese", "A1", ("English", "Mandarin Chinese")),
        (
            "japanese A2 mandarin chinese and english",
            "Japanese",
            "A2",
            ("Mandarin Chinese", "English"),
        ),
    ],
)
def test_parse_tutor_arguments(args, target, level, explain):
    parsed = parse_tutor_args(args)
    assert (parsed.target, parsed.level, parsed.explain) == (target, level, explain)


@pytest.mark.parametrize(
    "args", ["Japanese", "Klingon A2", "English A1 English", "ja A2 junk", "ja A2 " + "x" * 200]
)
def test_parse_tutor_arguments_rejects_invalid_or_oversized_input(args):
    with pytest.raises(ValueError):
        parse_tutor_args(args)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ("Klingon A2", 'Unknown language "klingon"'),
        ("Cantonese A1 English Cantonese", "Explanation languages must differ"),
        ("Cantonese A1 Cantonese English", "Explanation languages must differ"),
        ("Cantonese A1 English English", "English"),
        ("Cantonese A1 English Chinese French", "At most two explanation languages"),
        ("Cantonese B8 English", "B8"),
    ],
)
def test_parser_errors_name_problem_and_token(args, message):
    with pytest.raises(ValueError, match=message):
        parse_tutor_args(args)


def test_trailing_topic_modifier_is_not_a_profile_parser_language():
    with pytest.raises(ValueError, match="auto"):
        parse_tutor_args("Cantonese A1 English auto")


def test_plugin_declares_valid_skill_and_short_instructions():
    plugin = make_plugin()
    app = BotApp(
        FakePlatform(),
        stores=memory_stores(),
        access=AccessPolicy([], []),
        plugins=[plugin],
        bot_name="testbot",
    )
    skill = app.skills.get("language_tutor")
    assert skill is not None
    assert len(skill.instructions) <= 800
    assert skill.context and skill.pin


def test_tutor_instructions_limit_scope_and_stay_under_limit():
    assert len(LANGUAGE_TUTOR_INSTRUCTIONS) <= 800
    assert LANGUAGE_TUTOR_INSTRUCTIONS.startswith(
        "Apply only when the person practises a language, asks about one, or requests "
        "a translation."
    )
    assert "A passing language mention (food, class, person, place) is not practice" in (
        LANGUAGE_TUTOR_INSTRUCTIONS
    )
    assert "ignore this section and handle the request normally" in LANGUAGE_TUTOR_INSTRUCTIONS


@pytest.mark.asyncio
async def test_command_dm_profile_settings_and_group_topic_permission():
    plugin = make_plugin()
    command = plugin.commands()[0]
    group = message("hello")
    stores = memory_stores()
    group_ctx = plugin_command_context(group, stores, is_dm=False)
    assert await command.handler(group_ctx, "Japanese A2") == (
        "Only a group admin can change tutor topics."
    )
    assert await plugin.memory_repo.get(group.user.ref.key) is None

    dm = message("hello", dm=True)
    ctx = plugin_command_context(dm, stores, is_dm=True)
    saved = await command.handler(ctx, "Japanese A2")
    assert saved == "Japanese, level A2, explanations in English, on"
    status = await command.handler(ctx, "")
    assert status.text == (
        "Japanese, level A2, explanations in English, on\n"
        "Use /tutor <language> <level> [<explanation languages>]; /tutor off pauses."
    )
    assert "paused" in await command.handler(ctx, "off")
    assert not (await plugin.memory_repo.get(dm.user.ref.key)).active
    assert "on" in await command.handler(ctx, "on")
    assert "cleared" in await command.handler(ctx, "clear")
    assert await plugin.memory_repo.get(dm.user.ref.key) is None


@pytest.mark.asyncio
async def test_command_update_keeps_explanation_language_and_invalid_input_writes_nothing():
    plugin = make_plugin()
    incoming = message("hello", dm=True)
    ctx = plugin_command_context(incoming, memory_stores(), is_dm=True)
    command = plugin.commands()[0]
    await command.handler(ctx, "Japanese A2 Korean")
    updated = await command.handler(ctx, "French b1")
    assert updated == "French, level B1, explanations in Korean, on"
    await command.handler(ctx, "Japanese A2 english and chinese")
    bilingual_update = await command.handler(ctx, "French B1")
    assert bilingual_update == (
        "French, level B1, explanations in English and Mandarin Chinese, on"
    )
    assert (await plugin.memory_repo.get(incoming.user.ref.key)).explain == (
        "English",
        "Mandarin Chinese",
    )
    before = await plugin.memory_repo.get(incoming.user.ref.key)
    invalid = await command.handler(ctx, "French B8")
    assert invalid.awaits_reply and "B8" in invalid.text and "Supported languages" in invalid.text
    assert await plugin.memory_repo.get(incoming.user.ref.key) == before


@pytest.mark.asyncio
async def test_dm_english_target_requires_an_explanation_when_no_distinct_language_remains():
    plugin = make_plugin()
    incoming = message("hello", dm=True)
    ctx = plugin_command_context(incoming, memory_stores(), is_dm=True)

    response = await plugin.commands()[0].handler(ctx, "English A1")

    assert response.awaits_reply
    assert "Give an explanation language" in response.text
    assert "Reply to this message with a corrected value." in response.text
    assert await plugin.memory_repo.get(incoming.user.ref.key) is None


@pytest.mark.asyncio
async def test_dm_target_change_drops_matching_inherited_explanation_and_keeps_the_rest():
    plugin = make_plugin()
    incoming = message("hello", dm=True)
    ctx = plugin_command_context(incoming, memory_stores(), is_dm=True)
    command = plugin.commands()[0].handler
    await command(ctx, "Japanese A2 English and Chinese")

    updated = await command(ctx, "English A1")

    assert updated == "English, level A1, explanations in Mandarin Chinese, on"
    assert (await plugin.memory_repo.get(incoming.user.ref.key)).explain == ("Mandarin Chinese",)


@pytest.mark.asyncio
async def test_dm_target_change_falls_back_to_english_when_all_inherited_explanations_match():
    plugin = make_plugin()
    incoming = message("hello", dm=True)
    ctx = plugin_command_context(incoming, memory_stores(), is_dm=True)
    command = plugin.commands()[0].handler
    await command(ctx, "Japanese A2 French")

    updated = await command(ctx, "French A1")

    assert updated == "French, level A1, explanations in English, on"


@pytest.mark.asyncio
async def test_dm_profile_command_turns_repository_value_errors_into_reply_prompts(monkeypatch):
    plugin = make_plugin()
    incoming = message("hello", dm=True)
    ctx = plugin_command_context(incoming, memory_stores(), is_dm=True)

    async def fail_upsert(*args, **kwargs):
        raise ValueError("Invalid tutor profile values")

    monkeypatch.setattr(plugin.memory_repo, "upsert", fail_upsert)
    response = await plugin.commands()[0].handler(ctx, "Japanese A1 English")

    assert response.awaits_reply
    assert "Invalid tutor profile values" in response.text


@pytest.mark.asyncio
async def test_profile_two_explanations_round_trip_and_invalid_joined_storage_is_missing():
    repo = MemoryTutorRepo()
    profile = await repo.upsert("tg:1", "Cantonese", "A1", ("English", "Mandarin Chinese"))
    assert profile.explain == ("English", "Mandarin Chinese")
    assert await repo.get("tg:1") == profile
    assert (
        max(
            len(f"{left.name},{right.name}")
            for left in LANGUAGES
            for right in LANGUAGES
            if left.name != right.name
        )
        <= 40
    )

    from datetime import UTC, datetime

    row = {
        "user_key": "tg:2",
        "target": "Cantonese",
        "level": "A1",
        "explain": "English,Klingon",
        "active": True,
        "updated_at": datetime.now(UTC),
    }
    assert _validated_profile(row) is None


@pytest.mark.asyncio
async def test_two_explanations_are_displayed_and_used_in_profile_context():
    plugin = make_plugin()
    incoming = message("hi", dm=True)
    await plugin.memory_repo.upsert(
        incoming.user.ref.key, "Cantonese", "A1", ("English", "Mandarin Chinese")
    )
    ctx = ToolContext(incoming.chat, incoming.user, True, FakePlatform(), memory_stores())
    assert await plugin._skill_context(ctx) == (
        "Tutor profile (set by this person with /tutor): learning Cantonese at CEFR A1; "
        "explain in English, adding Mandarin Chinese where it helps; romanisation: Jyutping. "
        "Follow it even when their message is in Cantonese."
    )
    assert (await plugin.tools()[0].handler(ctx)).content == (
        "Cantonese, level A1, explanations in English and Mandarin Chinese, on"
    )


def plugin_command_context(incoming, stores, *, is_dm):
    from chatcore.plugin import CommandContext

    return CommandContext(
        incoming.chat,
        incoming.user,
        is_dm,
        False,
        FakePlatform(),
        stores,
        command_form=lambda name: f"/{name}",
    )


@pytest.mark.asyncio
async def test_profile_is_private_to_requesting_user_and_hooks_are_dm_only():
    plugin = make_plugin()
    repo = plugin.memory_repo
    incoming = message("hi", dm=True)
    await repo.upsert(incoming.user.ref.key, "Japanese", "A2", "English")
    ctx = ToolContext(incoming.chat, incoming.user, True, FakePlatform(), memory_stores())
    assert await plugin._skill_pin(ctx) is True
    assert await plugin._skill_context(ctx) == (
        "Tutor profile (set by this person with /tutor): learning Japanese at CEFR A2; "
        "explain in English; romanisation: Hepburn romaji. Follow it even when their message "
        "is in Japanese."
    )
    assert len(await plugin._skill_context(ctx)) <= 400
    # A separate request uses its own UserRef key; no profile is copied into the context.
    from dataclasses import replace

    other_ctx = ToolContext(
        ctx.chat,
        replace(ctx.user, ref=replace(ctx.user.ref, id="2")),
        True,
        ctx.platform,
        ctx.stores,
    )
    assert await plugin._skill_pin(other_ctx) is False
    assert await plugin._skill_context(other_ctx) is None
    await repo.set_active(incoming.user.ref.key, False)
    assert await plugin._skill_pin(ctx) is False
    assert await plugin._skill_context(ctx) is None
    group_ctx = ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores)
    assert await plugin._skill_pin(group_ctx) is False
    assert await plugin._skill_context(group_ctx) is None


@pytest.mark.asyncio
async def test_active_profile_context_is_available_in_a_group_without_a_command_echo():
    plugin = make_plugin()
    incoming = message("hi")
    profile = await plugin.memory_repo.upsert(incoming.user.ref.key, "Japanese", "A2", "English")
    assert profile.active
    group_ctx = ToolContext(
        incoming.chat,
        incoming.user,
        False,
        FakePlatform(),
        memory_stores(),
        command_form=lambda name: f"/{name}@bot",
    )

    assert "learning Japanese at CEFR A2" in await plugin._skill_context(group_ctx)
    assert await plugin._skill_pin(group_ctx) is False
    result = await plugin.tools()[0].handler(group_ctx)
    assert result.content == (
        "Japanese, level A2, explanations in English, on\n"
        "Tutor topic: off\n"
        "To make this group or topic a tutor topic, a group admin sends /tutor@bot here "
        "<language> <level> [<explanation languages>] (or /tutor@bot here for no default); "
        "/tutor@bot here auto makes it reply to every message. You can't change this with a tool; "
        "tell the person the command."
    )


@pytest.mark.asyncio
async def test_context_stays_bounded_for_every_language_pair():
    plugin = make_plugin()
    incoming = message("hi", dm=True)
    ctx = ToolContext(incoming.chat, incoming.user, True, FakePlatform(), memory_stores())
    for language in LANGUAGES:
        explain = "Japanese" if language.name == "English" else "English"
        await plugin.memory_repo.upsert(incoming.user.ref.key, language.name, "C2", explain)
        value = await plugin._skill_context(ctx)
        assert value is not None and len(value) <= 400


@pytest.mark.asyncio
async def test_tool_returns_private_profile_and_group_topic_status():
    plugin = make_plugin()
    incoming = message("hi", dm=True)
    await plugin.memory_repo.upsert(incoming.user.ref.key, "Japanese", "A2", "English")
    tool = plugin.tools()[0]
    ctx = ToolContext(incoming.chat, incoming.user, True, FakePlatform(), memory_stores())
    assert (await tool.handler(ctx)).content == "Japanese, level A2, explanations in English, on"
    group_ctx = ToolContext(
        ctx.chat,
        ctx.user,
        False,
        ctx.platform,
        ctx.stores,
        command_form=lambda name: f"/{name}@bot",
    )
    assert (await tool.handler(group_ctx)).content == (
        "Japanese, level A2, explanations in English, on\n"
        "Tutor topic: off\n"
        "To make this group or topic a tutor topic, a group admin sends /tutor@bot here "
        "<language> <level> [<explanation languages>] (or /tutor@bot here for no default); "
        "/tutor@bot here auto makes it reply to every message. You can't change this with a tool; "
        "tell the person the command."
    )
    assert tool.parameters["properties"] == {}
    assert tool.parameters["additionalProperties"] is False
    empty_ctx = ToolContext(incoming.chat, incoming.user, True, FakePlatform(), memory_stores())
    empty_plugin = make_plugin()
    assert (await empty_plugin.tools()[0].handler(empty_ctx)).content == (
        "No tutor profile. The person can set one with /tutor <language> <level> "
        "[<explanation languages>]."
    )
    empty_group_ctx = ToolContext(
        empty_ctx.chat,
        empty_ctx.user,
        False,
        empty_ctx.platform,
        empty_ctx.stores,
        command_form=lambda name: f"/{name}@bot",
    )
    assert (await empty_plugin.tools()[0].handler(empty_group_ctx)).content == (
        "No tutor profile. Set one in a DM with /tutor@bot.\n"
        "Tutor topic: off\n"
        "To make this group or topic a tutor topic, a group admin sends /tutor@bot here "
        "<language> <level> [<explanation languages>] (or /tutor@bot here for no default); "
        "/tutor@bot here auto makes it reply to every message. You can't change this with a "
        "tool; tell the person the command."
    )


@pytest.mark.asyncio
async def test_tool_and_command_do_not_expose_another_users_profile():
    plugin = make_plugin()
    owner = message("hi", dm=True)
    await plugin.memory_repo.upsert(owner.user.ref.key, "Japanese", "A2", "English")
    from dataclasses import replace

    other_user = replace(owner.user, ref=replace(owner.user.ref, id="other"))
    tool = plugin.tools()[0]
    ctx = ToolContext(owner.chat, other_user, True, FakePlatform(), memory_stores())
    assert "No tutor profile" in (await tool.handler(ctx)).content
    from chatcore.plugin import CommandContext

    command_ctx = CommandContext(
        owner.chat, other_user, True, False, FakePlatform(), memory_stores()
    )
    response = await plugin.commands()[0].handler(command_ctx, "")
    assert response.awaits_reply and "Supported languages" in response.text


@pytest.mark.asyncio
async def test_purge_user_removes_profile_regardless_of_chat():
    plugin = make_plugin()
    incoming = message("hi", dm=True)
    await plugin.memory_repo.upsert(incoming.user.ref.key, "Japanese", "A2", "English")
    await plugin.purge_user(incoming.chat, incoming.user.ref, memory_stores())
    assert await plugin.memory_repo.get(incoming.user.ref.key) is None


def test_settings_are_off_by_default():
    assert TutorSettings().TUTOR_ENABLED is False


def test_tutor_repo_rejects_non_postgres_database():
    with pytest.raises(TypeError, match="requires PgDatabase"):
        from chatcore_tutor.repository import TutorRepo

        TutorRepo(object())


@pytest.mark.asyncio
async def test_memory_repo_upsert_pause_resume_and_delete():
    repo = MemoryTutorRepo()
    assert await repo.get("tg:1") is None
    assert not await repo.set_active("tg:1", True)
    profile = await repo.upsert("tg:1", "Japanese", "A2", "English")
    assert profile.active
    assert await repo.set_active("tg:1", False)
    assert not (await repo.get("tg:1")).active
    assert await repo.set_active("tg:1", True)
    assert (await repo.get("tg:1")).active
    assert await repo.delete("tg:1")
    assert not await repo.delete("tg:1")
