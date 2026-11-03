from dataclasses import replace

import pytest
from chatcore.access import AccessPolicy
from chatcore.command_prompts import CommandReply
from chatcore.events import Attachment, AttachmentKind, UserRef
from chatcore.plugin import CommandContext
from chatcore.testing import FakePlatform, memory_stores, message
from chatcore.tools.registry import ToolContext
from chatcore_tutor.languages import LANGUAGES, LEVELS
from chatcore_tutor.plugin import TutorPlugin


class AdminPlatform(FakePlatform):
    def __init__(self):
        super().__init__()
        self.admins = set()

    async def is_group_admin(self, chat, user):
        return (chat.key, user.key) in self.admins


class ConfirmRecorder:
    def __init__(self):
        self.action = None

    async def request(self, action, user, *, reply_to=None):
        self.action = action


def setup(*, manager=False, thread_id="7", command_form=None):
    incoming = message("hello", thread_id=thread_id)
    platform = AdminPlatform()
    access = AccessPolicy([], [], owners=[incoming.user.ref.key] if manager else [])
    ctx = CommandContext(
        incoming.chat,
        incoming.user,
        False,
        False,
        platform,
        memory_stores(),
        thread_id=thread_id,
        message=incoming,
        command_form=command_form or (lambda name: f"/{name}"),
    )
    plugin = TutorPlugin(access, platform)
    return plugin, ctx


async def run_tutor(plugin, ctx, args=""):
    return await plugin.commands()[0].handler(ctx, args)


@pytest.mark.asyncio
async def test_group_profiles_are_added_only_for_the_speaker_and_paused_profiles_are_ignored():
    plugin, ctx = setup()
    speaker_a = ctx.user.ref.key
    speaker_b = UserRef(ctx.user.ref.platform, "other").key
    await plugin.memory_repo.upsert(speaker_a, "Japanese", "A2", "English")
    tool_ctx = ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id="8")

    assert "learning Japanese at CEFR A2" in await plugin._skill_context(tool_ctx)
    assert (
        await plugin._skill_context(
            replace(tool_ctx, user=replace(ctx.user, ref=UserRef.parse(speaker_b)))
        )
        is None
    )
    await plugin.memory_repo.set_active(speaker_a, False)
    assert await plugin._skill_context(tool_ctx) is None


@pytest.mark.asyncio
async def test_topic_pin_and_context_are_scoped_to_exact_thread():
    plugin, ctx = setup(manager=True)
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    assert await run_tutor(plugin, ctx, "here japanese A2 english") is None
    action = recorder.action
    assert action.plugin == "tutor" and action.kind == "topic_set"
    assert action.payload == {
        "chat_key": ctx.chat.key,
        "thread_key": "7",
        "target": "Japanese",
        "level": "A2",
        "explain": "English",
        "auto_reply": None,
    }
    assert await plugin.action_handlers()["topic_set"].commit(action, ctx)
    assert await plugin._skill_pin(
        ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id="7")
    )
    context = await plugin._skill_context(
        ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id="7")
    )
    assert context and "Japanese at CEFR A2" in context and "Everything in this topic" in context
    await plugin.memory_repo.upsert(ctx.user.ref.key, "French", "B1", "English")
    context_with_profile = await plugin._skill_context(
        ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id="7")
    )
    assert "learning French at CEFR B1" in context_with_profile
    assert context_with_profile.endswith("Everything in this topic is language practice.")
    assert not await plugin._skill_pin(
        ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id="8")
    )
    assert not await plugin._skill_pin(
        ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id=None)
    )


@pytest.mark.asyncio
async def test_topic_pin_without_default_uses_infer_context_and_group_status_hides_profiles():
    plugin, ctx = setup(manager=True)
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    assert await run_tutor(plugin, ctx, "here") is None
    action = recorder.action
    await plugin.action_handlers()["topic_set"].commit(action, ctx)
    tool_ctx = ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id="7")
    assert await plugin._skill_pin(tool_ctx)
    assert await plugin._skill_context(tool_ctx) == (
        "This topic is for language practice. Infer the language being practised from the "
        "conversation and explain in the group's usual language. Everything in this topic is "
        "language practice."
    )
    await plugin.memory_repo.upsert(ctx.user.ref.key, "Japanese", "A2", "English")
    status = await run_tutor(plugin, ctx)
    assert status.text.startswith("Tutor topic: on, no default language")
    assert "Set your own profile with /tutor in a DM with me." in status.text
    assert "Japanese" not in status.text


@pytest.mark.asyncio
async def test_topic_default_without_explanation_uses_group_language():
    plugin, ctx = setup(manager=True)
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    assert await run_tutor(plugin, ctx, "here japanese A2") is None
    await plugin.action_handlers()["topic_set"].commit(recorder.action, ctx)
    context = await plugin._skill_context(
        ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id="7")
    )
    assert context == (
        "This topic is for language practice: Japanese at CEFR A2; explain in the group's usual "
        "language; romanisation: Hepburn romaji. Everything in this topic is language practice."
    )


@pytest.mark.asyncio
async def test_auto_address_is_opt_in_and_scoped_to_the_exact_topic():
    plugin, ctx = setup(manager=True, thread_id="7")
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    assert await run_tutor(plugin, ctx, "here") is None
    await plugin.action_handlers()["topic_set"].commit(recorder.action, ctx)
    assert not (await plugin.repo(ctx.stores).get_topic(ctx.chat.key, "7")).auto_reply

    incoming = message("ordinary group text", thread_id="7")
    assert not await plugin.auto_address(incoming, ctx.stores)
    assert not await plugin.auto_address(replace(incoming, thread_id="8"), ctx.stores)
    assert not await plugin.auto_address(replace(incoming, thread_id=None), ctx.stores)

    assert not await plugin.auto_address(replace(incoming, is_dm=True), ctx.stores)

    assert await run_tutor(plugin, ctx, "here auto") is None
    action = recorder.action
    assert action.kind == "topic_reply"
    assert action.payload == {
        "chat_key": ctx.chat.key,
        "thread_key": "7",
        "auto_reply": True,
    }
    assert action.thread_id == "7"
    assert "every message" in action.summary
    await plugin.action_handlers()["topic_reply"].commit(action, ctx)
    assert await plugin.auto_address(incoming, ctx.stores)
    for kind in (AttachmentKind.PHOTO, AttachmentKind.DOCUMENT):
        media = replace(incoming, text="", attachments=(Attachment("file", kind),))
        assert await plugin.auto_address(media, ctx.stores)
    for kind in (AttachmentKind.AUDIO, AttachmentKind.VIDEO, AttachmentKind.OTHER):
        media = replace(incoming, text="", attachments=(Attachment("file", kind),))
        assert not await plugin.auto_address(media, ctx.stores)
    assert not await plugin.auto_address(replace(incoming, text="", attachments=()), ctx.stores)
    assert not await plugin.auto_address(replace(incoming, thread_id="8"), ctx.stores)
    assert not await plugin.auto_address(replace(incoming, thread_id=None), ctx.stores)


@pytest.mark.asyncio
async def test_quiet_auto_commands_check_permission_and_recheck_marked_topic():
    plugin, ctx = setup()
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    assert (
        await run_tutor(plugin, ctx, "here auto") == "Only a group admin can change tutor topics."
    )
    assert recorder.action is None

    managed, manager_ctx = setup(manager=True)
    manager_ctx.confirm = recorder
    unmarked = await run_tutor(managed, manager_ctx, "here auto")
    assert unmarked is None
    assert recorder.action.kind == "topic_set"
    await managed.action_handlers()["topic_set"].commit(recorder.action, manager_ctx)

    await run_tutor(managed, manager_ctx, "here")
    await managed.action_handlers()["topic_set"].commit(recorder.action, manager_ctx)
    await run_tutor(managed, manager_ctx, "here auto")
    action = recorder.action
    managed.access.owners = frozenset()
    assert (
        "nothing changed"
        in (await managed.action_handlers()["topic_set"].commit(action, manager_ctx)).lower()
    )
    managed.access.owners = frozenset({manager_ctx.user.ref.key})

    await run_tutor(managed, manager_ctx, "here auto")
    action = recorder.action
    await managed.repo(manager_ctx.stores).delete_topic(manager_ctx.chat.key, "7")
    assert "no longer exists" in await managed.action_handlers()["topic_reply"].commit(
        action, manager_ctx
    )
    assert await managed.repo(manager_ctx.stores).get_topic(manager_ctx.chat.key, "7") is None


@pytest.mark.asyncio
async def test_remarking_preserves_auto_reply_and_status_reports_mode():
    plugin, ctx = setup(manager=True)
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    await run_tutor(plugin, ctx, "here")
    await plugin.action_handlers()["topic_set"].commit(recorder.action, ctx)
    await run_tutor(plugin, ctx, "here auto")
    await plugin.action_handlers()["topic_reply"].commit(recorder.action, ctx)

    await run_tutor(plugin, ctx, "here japanese A2 english")
    await plugin.action_handlers()["topic_set"].commit(recorder.action, ctx)
    topic = await plugin.repo(ctx.stores).get_topic(ctx.chat.key, "7")
    assert topic.auto_reply
    assert "replies to every message" in (await run_tutor(plugin, ctx)).text

    await run_tutor(plugin, ctx, "here quiet")
    assert recorder.action.kind == "topic_reply"
    assert recorder.action.payload["auto_reply"] is False
    await plugin.action_handlers()["topic_reply"].commit(recorder.action, ctx)
    assert "replies only when tagged" in (await run_tutor(plugin, ctx)).text


@pytest.mark.asyncio
async def test_modifier_only_commands_preserve_marked_topic_defaults():
    plugin, ctx = setup(manager=True)
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    await run_tutor(plugin, ctx, "here cantonese A1 english and chinese")
    await plugin.action_handlers()["topic_set"].commit(recorder.action, ctx)
    original = await plugin.repo(ctx.stores).get_topic(ctx.chat.key, "7")
    assert original is not None

    await run_tutor(plugin, ctx, "here auto")
    assert recorder.action.kind == "topic_reply"
    await plugin.action_handlers()["topic_reply"].commit(recorder.action, ctx)
    current = await plugin.repo(ctx.stores).get_topic(ctx.chat.key, "7")
    assert (current.target, current.level, current.explain, current.auto_reply) == (
        original.target,
        original.level,
        original.explain,
        True,
    )

    await run_tutor(plugin, ctx, "auto")
    assert recorder.action.kind == "topic_reply"
    await plugin.action_handlers()["topic_reply"].commit(recorder.action, ctx)
    current = await plugin.repo(ctx.stores).get_topic(ctx.chat.key, "7")
    assert (current.target, current.level, current.explain, current.auto_reply) == (
        original.target,
        original.level,
        original.explain,
        True,
    )

    await run_tutor(plugin, ctx, "quiet")
    assert recorder.action.kind == "topic_reply"
    await plugin.action_handlers()["topic_reply"].commit(recorder.action, ctx)
    current = await plugin.repo(ctx.stores).get_topic(ctx.chat.key, "7")
    assert (current.target, current.level, current.explain, current.auto_reply) == (
        original.target,
        original.level,
        original.explain,
        False,
    )

    await run_tutor(plugin, ctx, "here quiet")
    assert recorder.action.kind == "topic_reply"
    await plugin.action_handlers()["topic_reply"].commit(recorder.action, ctx)
    current = await plugin.repo(ctx.stores).get_topic(ctx.chat.key, "7")
    assert (current.target, current.level, current.explain, current.auto_reply) == (
        original.target,
        original.level,
        original.explain,
        False,
    )


@pytest.mark.asyncio
async def test_no_default_topic_status_reports_quiet_and_auto_modes():
    plugin, ctx = setup(manager=True)
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    status = await run_tutor(plugin, ctx)
    assert isinstance(status, CommandReply)
    assert status.awaits_reply
    assert status.placeholder == "e.g. japanese A2 english"
    assert status.text == (
        "Tutor topic: off\n"
        "To turn it on: /tutor here <language> <level> [<explanation languages>], or "
        "/tutor here for no default language.\n"
        'Or reply to this message with one of those, e.g. "japanese A2 english", "auto" or "off".\n'
        "Set your own profile with /tutor in a DM with me."
    )
    await run_tutor(plugin, ctx, "here")
    await plugin.action_handlers()["topic_set"].commit(recorder.action, ctx)

    status = await run_tutor(plugin, ctx)
    assert isinstance(status, CommandReply)
    assert status.text == (
        "Tutor topic: on, no default language; replies only when tagged\n"
        "Change it with /tutor here <language> <level> [<explanation languages>]; "
        "/tutor here auto replies to every message, /tutor here quiet only when tagged, "
        "/tutor here off turns it off.\n"
        'Or reply to this message with one of those, e.g. "japanese A2 english", "auto" or "off".\n'
        "Set your own profile with /tutor in a DM with me."
    )

    await run_tutor(plugin, ctx, "here auto")
    await plugin.action_handlers()["topic_reply"].commit(recorder.action, ctx)
    status = await run_tutor(plugin, ctx)
    assert isinstance(status, CommandReply)
    assert status.text.startswith(
        "Tutor topic: on, no default language; replies to every message\n"
    )


@pytest.mark.asyncio
async def test_non_manager_group_status_has_plain_admin_guidance_and_mention_form():
    plugin, ctx = setup(command_form=lambda name: f"/{name}@bot")

    assert await run_tutor(plugin, ctx) == (
        "Tutor topic: off\n"
        "A group admin can turn it on with /tutor@bot here <language> <level>.\n"
        "Set your own profile with /tutor@bot in a DM with me."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "kind", "payload"),
    [
        (
            "japanese A2 english",
            "topic_set",
            {"target": "Japanese", "level": "A2", "explain": "English", "auto_reply": None},
        ),
        ("auto", "topic_reply", {"auto_reply": True}),
        ("quiet", "topic_reply", {"auto_reply": False}),
        ("off", "topic_clear", {"target": None, "level": None, "explain": None}),
        ("on", "topic_set", {"target": None, "level": None, "explain": None, "auto_reply": None}),
    ],
)
async def test_group_reply_words_map_to_topic_actions(args, kind, payload):
    plugin, ctx = setup(manager=True)
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    if args in {"auto", "quiet"}:
        await run_tutor(plugin, ctx, "here")
        await plugin.action_handlers()["topic_set"].commit(recorder.action, ctx)

    assert await run_tutor(plugin, ctx, args) is None
    assert recorder.action.kind == kind
    assert {key: recorder.action.payload[key] for key in payload} == payload


@pytest.mark.asyncio
async def test_group_on_reply_to_marked_topic_returns_status_without_action():
    plugin, ctx = setup(manager=True)
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    await run_tutor(plugin, ctx, "here")
    await plugin.action_handlers()["topic_set"].commit(recorder.action, ctx)
    recorder.action = None

    status = await run_tutor(plugin, ctx, "on")

    assert status == "Tutor topic: on, no default language; replies only when tagged"
    assert recorder.action is None


@pytest.mark.asyncio
async def test_invalid_group_reply_prompts_managers_and_gives_plain_usage_to_others():
    plugin, ctx = setup(manager=True)
    usage = await run_tutor(plugin, ctx, "turn it on")
    assert isinstance(usage, CommandReply)
    assert usage.awaits_reply
    assert "japanese A2 english" in usage.text
    assert "auto" in usage.text and "quiet" in usage.text and "off" in usage.text
    assert "Reply to this message with a corrected value." in usage.text

    detailed = await run_tutor(plugin, ctx, "here Klingon A2")
    assert isinstance(detailed, CommandReply) and detailed.awaits_reply
    assert 'Unknown language "klingon"' in detailed.text
    assert "Supported:" in detailed.text

    ordinary, ordinary_ctx = setup()
    plain_usage = await run_tutor(ordinary, ordinary_ctx, "turn it on")
    assert isinstance(plain_usage, str)
    assert "japanese A2 english" in plain_usage
    assert not isinstance(plain_usage, CommandReply)


@pytest.mark.asyncio
async def test_oversized_group_args_prompt_managers_but_not_non_managers():
    args = "x" * 201
    manager_plugin, manager_ctx = setup(manager=True)
    manager_result = await run_tutor(manager_plugin, manager_ctx, args)
    assert isinstance(manager_result, CommandReply)
    assert manager_result.awaits_reply

    plugin, ctx = setup()
    result = await run_tutor(plugin, ctx, args)
    assert isinstance(result, str)
    assert not isinstance(result, CommandReply)
    assert "reply" in result


@pytest.mark.asyncio
async def test_non_manager_reply_is_refused_without_pending_action():
    plugin, ctx = setup()
    recorder = ConfirmRecorder()
    ctx.confirm = recorder

    assert await run_tutor(plugin, ctx, "japanese A2 english") == (
        "Only a group admin can change tutor topics."
    )
    assert recorder.action is None


@pytest.mark.asyncio
async def test_group_profile_tool_reports_requester_and_topic_without_other_profiles():
    plugin, ctx = setup(manager=True, command_form=lambda name: f"/{name}@bot")
    await plugin.memory_repo.upsert(ctx.user.ref.key, "Japanese", "A2", "English")
    await plugin.memory_repo.upsert("other-user", "French", "B1", "English")
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    await run_tutor(plugin, ctx, "here japanese A2 english")
    await plugin.action_handlers()["topic_set"].commit(recorder.action, ctx)
    tool_ctx = ToolContext(
        ctx.chat,
        ctx.user,
        False,
        ctx.platform,
        ctx.stores,
        thread_id="7",
        command_form=lambda name: f"/{name}@bot",
    )

    result = await plugin.tools()[0].handler(tool_ctx)

    assert result.content == (
        "Japanese, level A2, explanations in English, on\n"
        "Tutor topic: on, default Japanese A2, explanations in English; replies only when tagged\n"
        "To make this group or topic a tutor topic, a group admin sends /tutor@bot here "
        "<language> <level> [<explanation languages>] (or /tutor@bot here for no default); "
        "/tutor@bot here auto makes it reply to every message. You can't change this with a tool; "
        "tell the person the command."
    )
    assert "French" not in result.content


@pytest.mark.asyncio
async def test_group_profile_tool_without_profile_includes_status_and_command_instruction():
    plugin, ctx = setup(command_form=lambda name: f"/{name}@bot")
    result = await plugin.tools()[0].handler(
        ToolContext(
            ctx.chat,
            ctx.user,
            False,
            ctx.platform,
            ctx.stores,
            thread_id="7",
            command_form=lambda name: f"/{name}@bot",
        )
    )
    assert result.content == (
        "No tutor profile. Set one in a DM with /tutor@bot.\n"
        "Tutor topic: off\n"
        "To make this group or topic a tutor topic, a group admin sends /tutor@bot here "
        "<language> <level> [<explanation languages>] (or /tutor@bot here for no default); "
        "/tutor@bot here auto makes it reply to every message. You can't change this with a tool; "
        "tell the person the command."
    )


@pytest.mark.asyncio
async def test_dm_tutor_profile_setting_still_saves_personal_profile():
    plugin, ctx = setup(manager=True)
    dm_message = message("hello", dm=True)
    dm_ctx = CommandContext(
        dm_message.chat,
        dm_message.user,
        True,
        False,
        ctx.platform,
        ctx.stores,
        message=dm_message,
    )

    assert await run_tutor(plugin, dm_ctx, "japanese A2 english") == (
        "Japanese, level A2, explanations in English, on"
    )
    assert await plugin.memory_repo.get(dm_message.user.ref.key) is not None


@pytest.mark.asyncio
async def test_topic_mark_can_set_two_explanations_and_auto_mode_in_one_confirmation():
    plugin, ctx = setup(manager=True)
    recorder = ConfirmRecorder()
    ctx.confirm = recorder

    assert await run_tutor(plugin, ctx, "here cantonese A1 english and chinese auto") is None
    action = recorder.action
    assert action.kind == "topic_set"
    assert action.payload["explain"] == "English,Mandarin Chinese"
    assert action.payload["auto_reply"] is True
    assert "explanations in English and Mandarin Chinese" in action.summary
    assert "replies to every message" in action.summary

    await plugin.action_handlers()["topic_set"].commit(action, ctx)
    topic = await plugin.repo(ctx.stores).get_topic(ctx.chat.key, "7")
    assert topic is not None
    assert topic.explain == ("English", "Mandarin Chinese")
    assert topic.auto_reply
    context = await plugin._skill_context(
        ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id="7")
    )
    assert "explain in English, adding Mandarin Chinese where it helps" in context

    status = await run_tutor(plugin, ctx)
    assert "explanations in English and Mandarin Chinese" in status.text


@pytest.mark.asyncio
async def test_status_prompt_reply_starting_here_runs_topic_path_and_dm_errors_prompt():
    plugin, ctx = setup(manager=True)
    ctx.confirm = ConfirmRecorder()
    prompt = await run_tutor(plugin, ctx)
    assert isinstance(prompt, CommandReply) and prompt.awaits_reply
    assert await run_tutor(plugin, ctx, "here auto") is None
    recorder_action = ctx.confirm.action
    assert recorder_action is not None
    assert recorder_action.kind == "topic_set"
    assert recorder_action.payload["auto_reply"] is True

    dm_message = message("hello", dm=True)
    dm_ctx = CommandContext(
        dm_message.chat, dm_message.user, True, False, ctx.platform, ctx.stores, message=dm_message
    )
    error = await run_tutor(plugin, dm_ctx, "klingon A2")
    assert isinstance(error, CommandReply) and error.awaits_reply
    assert 'Unknown language "klingon"' in error.text


@pytest.mark.asyncio
async def test_non_manager_cannot_mark_and_manager_confirmation_rechecks_permission():
    plugin, ctx = setup()
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    assert await run_tutor(plugin, ctx, "here") == "Only a group admin can change tutor topics."
    assert recorder.action is None
    assert await plugin.repo(ctx.stores).get_topic(ctx.chat.key, "7") is None

    managed, manager_ctx = setup(manager=True)
    manager_ctx.confirm = recorder
    await run_tutor(managed, manager_ctx, "here")
    action = recorder.action
    managed.access.owners = frozenset()
    assert (
        "nothing changed"
        in (await managed.action_handlers()["topic_set"].commit(action, manager_ctx)).lower()
    )
    assert await managed.repo(manager_ctx.stores).get_topic(manager_ctx.chat.key, "7") is None


@pytest.mark.asyncio
async def test_native_group_admin_can_request_a_topic_mark():
    plugin, ctx = setup()
    plugin.platform.admins.add((ctx.chat.key, ctx.user.ref.key))
    recorder = ConfirmRecorder()
    ctx.confirm = recorder

    assert await run_tutor(plugin, ctx, "here") is None
    assert recorder.action.kind == "topic_set"


@pytest.mark.asyncio
async def test_bare_group_topic_args_are_handled_and_dm_here_is_ignored():
    plugin, ctx = setup(manager=True)
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    assert await run_tutor(plugin, ctx, "japanese A2") is None
    assert recorder.action.kind == "topic_set"
    assert recorder.action.payload["target"] == "Japanese"
    assert await plugin.memory_repo.get(ctx.user.ref.key) is None
    assert await run_tutor(plugin, ctx, "here") is None
    assert recorder.action.kind == "topic_set"

    dm_message = message("hello", dm=True)
    dm_ctx = CommandContext(
        dm_message.chat,
        dm_message.user,
        True,
        False,
        ctx.platform,
        ctx.stores,
        message=dm_message,
        confirm=ConfirmRecorder(),
    )
    result = await run_tutor(plugin, dm_ctx, "here")
    assert "group topic" in result
    assert await plugin.memory_repo.get(dm_message.user.ref.key) is None

    mention_ctx = replace(ctx, command_form=lambda name: f"/{name}@bot")
    prompt = await run_tutor(plugin, mention_ctx, "here Klingon A2")
    assert isinstance(prompt, CommandReply)
    assert "/tutor@bot" in prompt.text


@pytest.mark.asyncio
async def test_clear_is_confirmed_and_chat_level_and_topic_marks_do_not_cross_scopes():
    plugin, ctx = setup(manager=True, thread_id=None)
    recorder = ConfirmRecorder()
    ctx.confirm = recorder
    assert await run_tutor(plugin, ctx, "here") is None
    assert recorder.action.payload["thread_key"] == ""
    await plugin.action_handlers()["topic_set"].commit(recorder.action, ctx)
    assert await plugin._skill_pin(
        ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id=None)
    )
    assert not await plugin._skill_pin(
        ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id="7")
    )
    await plugin.purge_chat(ctx.chat, ctx.stores)
    assert await plugin.repo(ctx.stores).get_topic(ctx.chat.key, "") is None

    ctx.thread_id = "7"
    assert await run_tutor(plugin, ctx, "here japanese A2 english") is None
    assert recorder.action.payload["thread_key"] == "7"
    await plugin.action_handlers()["topic_set"].commit(recorder.action, ctx)
    assert await plugin._skill_pin(
        ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id="7")
    )
    assert await run_tutor(plugin, ctx, "here off") is None
    assert recorder.action.kind == "topic_clear"
    assert recorder.action.summary == "Stop tutor mode in this topic"
    assert recorder.action.payload == {
        "chat_key": ctx.chat.key,
        "thread_key": "7",
        "target": None,
        "level": None,
        "explain": None,
    }
    assert "stopped" in await plugin.action_handlers()["topic_clear"].commit(recorder.action, ctx)
    assert not await plugin._skill_pin(
        ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id="7")
    )


@pytest.mark.asyncio
async def test_topic_context_stays_under_400_for_all_supported_language_pairs_and_levels():
    plugin, ctx = setup(manager=True)
    topic_repo = plugin.repo(ctx.stores)
    tool_ctx = ToolContext(ctx.chat, ctx.user, False, ctx.platform, ctx.stores, thread_id="7")
    ordinary_group_ctx = replace(tool_ctx, thread_id="8")
    dm_ctx = ToolContext(ctx.chat, ctx.user, True, ctx.platform, ctx.stores)
    topic_default_ctx = replace(
        tool_ctx,
        user=replace(ctx.user, ref=UserRef(ctx.user.ref.platform, "topic-default-tester")),
    )
    for target in LANGUAGES:
        for first in LANGUAGES:
            if target.name == first.name:
                continue
            for second in LANGUAGES:
                if second.name in {target.name, first.name}:
                    continue
                explanations = (first.name, second.name)
                for level in LEVELS:
                    await topic_repo.upsert_topic(
                        ctx.chat.key,
                        "7",
                        target.name,
                        level,
                        explanations,
                        ctx.user.ref.key,
                    )
                    context = await plugin._skill_context(topic_default_ctx)
                    assert context is not None and context.startswith(
                        "This topic is for language practice:"
                    )
                    assert len(context) <= 400
                    await plugin.memory_repo.upsert(
                        ctx.user.ref.key, target.name, level, explanations
                    )
                    profile_context = await plugin._skill_context(tool_ctx)
                    assert profile_context is not None and profile_context.startswith(
                        "Tutor profile"
                    )
                    assert len(profile_context) <= 400
                    ordinary_context = await plugin._skill_context(ordinary_group_ctx)
                    dm_context = await plugin._skill_context(dm_ctx)
                    assert ordinary_context is not None and len(ordinary_context) <= 400
                    assert dm_context is not None and len(dm_context) <= 400
