"""Request-shape regressions for group speaker attribution."""

from dataclasses import replace

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import SUMMARY_SYSTEM_PROMPT, ConversationPlugin, ConversationStore
from chatcore.events import UserRef
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message
from chatcore.testing import (
    GROUP,
    FakePlatform,
    ScriptedProvider,
    memory_stores,
    message,
    user,
)
from chatcore.threads import ThreadSettings


class _NicknameMemory:
    name = "test_memory"

    def tools(self):
        return []

    def commands(self):
        return []

    def skills(self):
        return []

    def action_handlers(self):
        return {}

    async def before_turn(self, _message, _text, _stores):
        return (
            "<untrusted-memory>#2 [about Member A (tg:111111111)] "
            'Member A prefers "nickname-A"</untrusted-memory>'
        )


def _setup(settings=None, *, responses=None, conversation_threads=False):
    stores = memory_stores()
    provider = ScriptedProvider(responses or [Completion(Message.assistant("ok"), "end_turn")])
    responder = AgentResponder(
        provider,
        system_prompt="",
        settings=settings or AgentSettings(),
        limits=Limits(stores.cache),
    )
    store = ConversationStore()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(store), _NicknameMemory()],
        bot_name="clawdot",
        responder=responder,
        group_mentions_only=True,
        conversation_threads=ThreadSettings() if conversation_threads else None,
    )
    return stores, responder, app, store


def _assert_body_outside_header_wrapper(final: str, human_text: str) -> None:
    closing = final.find("</untrusted-")
    closing_end = final.find(">", closing) + 1
    assert closing >= 0 and closing_end > closing
    assert final[closing_end:] == "\n" + human_text
    assert final.count("<untrusted-") == 1


async def test_ambient_history_from_member_a_does_not_replace_member_b_final_speaker():
    stores, responder, app, store = _setup(
        AgentSettings(recent_context_messages=16, ambient_total_chars=6000)
    )
    member_a = replace(user("alice"), ref=UserRef("tg", "111111111"), display_name="Member A")
    member_b = replace(
        user("bob"),
        ref=UserRef("tg", "222222222"),
        display_name="Member B",
        username=None,
    )
    await stores.repo.upsert_member(GROUP, member_a)
    await stores.repo.upsert_member(GROUP, member_b)
    for index, (speaker, body) in enumerate(
        (
            (member_a, "Any trip news?"),
            (member_a, "Give audio"),
            (member_a, "Any food ideas?"),
            (member_a, "One more question"),
        )
    ):
        row = message(body, sender=speaker)
        await stores.repo.log_message(row)
        await stores.repo.log_bot_message(
            GROUP,
            f"bot-{index}",
            "今日新聞簡報。" * 50,
            thread_id=None,
            reply_to_id=row.message_id,
        )

    _, turns, *_ = await responder._context(
        message("what should we eat?", sender=member_b),
        "what should we eat?",
        app,
        store,
        "testtag",
    )

    final = turns[-1].text
    assert final.startswith("<untrusted-testtag>Message from Member B")
    assert "Member B (tg:222222222)" in final
    assert "(, tg:222222222)" not in final
    assert "tg:222222222" in final
    _assert_body_outside_header_wrapper(final, "what should we eat?")
    assert "about Member A (tg:111111111)" in "\n".join(turn.text for turn in turns)
    assert "今日新聞簡報。" in "\n".join(turn.text for turn in turns)
    assert "(you)" not in "\n".join(turn.text for turn in turns)


async def test_summary_compaction_uses_fixed_internal_note_prompt():
    from chatcore.conversation import ConversationStore

    store = ConversationStore()
    await store.add(GROUP, None, "user", {"text": "Member A asked a question."})
    provider = ScriptedProvider([Completion(Message.assistant("Member A asked."), "end_turn")])

    await store.compact(
        GROUP, None, provider, "chat persona that must not leak", "fake", force=True, keep=0
    )

    assert provider.calls[0]["system"] == SUMMARY_SYSTEM_PROMPT
    assert "chat persona that must not leak" not in provider.calls[0]["system"]
    assert "third person" in provider.calls[0]["system"]


async def test_reply_to_other_members_answer_marks_handover_and_reply_target():
    member_b = replace(user("bob"), display_name="Member B")
    member_c = replace(user("carol"), display_name="Member C")
    responses = [
        Completion(Message.assistant("Member C paid the expense."), "end_turn"),
        Completion(Message.assistant("I will fix the split."), "end_turn"),
        Completion(Message.assistant("I will update Member C's split."), "end_turn"),
    ]
    stores, responder, app, store = _setup(responses=responses, conversation_threads=True)
    c_request = message(
        "@clawdot confirm the expense", sender=member_c, mention=True, thread_id="topic"
    )

    await app.handle(c_request)
    bot_reply = app.platform.sent[-1].ref
    b_reply = replace(
        message(
            "Please fix the split",
            sender=member_b,
            reply_to_bot=True,
            thread_id="topic",
        ),
        reply_to=bot_reply,
    )
    await app.handle(b_reply)

    b_prompt = responder.provider.calls[1]
    b_text = "\n".join(item.text for item in b_prompt["messages"])
    assert "which answered Member C (fake:carol)" in b_text
    assert '"you" in it means them, not the current speaker.' in b_text
    assert "Member B is joining a conversation started by Member C" in b_text

    c_reply = replace(
        message(
            "Please fix my split",
            sender=member_c,
            reply_to_bot=True,
            thread_id="topic",
        ),
        reply_to=bot_reply,
    )
    await app.handle(c_reply)
    c_text = "\n".join(item.text for item in responder.provider.calls[2]["messages"])
    assert "which answered Member C (fake:carol)" in c_text
    assert '"you" in it means them, not the current speaker.' not in c_text
    assert "is joining a conversation" not in c_text
