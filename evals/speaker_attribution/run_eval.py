"""Capture speaker-attribution requests from the current code and score live model replies.

The capture path uses APIs available on ``main`` so this file can be copied to a main checkout
for a before-change baseline. The summary prompt is discovered with ``getattr`` because main
does not yet define ``SUMMARY_SYSTEM_PROMPT``; there the captured chat system prompt is used,
matching the compaction call on that revision.

Run with the bot's model configuration in the environment:
  LLM_API_KEY=... LLM_MODEL=... uv run python evals/speaker_attribution/run_eval.py --runs 20

Only aggregate rates are printed. Prompts and model replies are never written or printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from dataclasses import replace
from datetime import UTC, datetime

import chatcore.conversation as conversation_module
from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin, ConversationStore
from chatcore.events import ChatRef, MessageRef, User, UserRef
from chatcore.limits import Limits
from chatcore.llm.factory import LLMSettings, build_provider
from chatcore.llm.types import Completion, Message
from chatcore.plugin import BasePlugin
from chatcore.store.base import StoredMessage
from chatcore.testing import FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.threads import ThreadSettings, current_conversation

GROUP = ChatRef("tg", "-1001111111111")
MEMBER_A = User(UserRef("tg", "111111111"), "Member A", "member_a")
MEMBER_B = User(UserRef("tg", "222222222"), "Member B", "member_b")
MEMBER_C = User(UserRef("tg", "333333333"), "Member C", "member_c")
BOT_USER = UserRef("tg", "999999999")
BOT_NAME = "clawdot"


class _NicknameMemoryPlugin(BasePlugin):
    """Return a memory-plugin-style attribution without loading or ranking real memories."""

    name = "eval_memory"

    async def before_turn(self, _message, _text, _stores):
        return (
            "<untrusted-memory>#2 [about Member A (tg:111111111)] "
            'Member A prefers to be addressed as "nickname-A".</untrusted-memory>'
        )


def _row(
    message_id: str,
    user: UserRef,
    display_name: str,
    text: str,
    *,
    at: datetime,
    thread_id: str | None = "topic",
    is_bot: bool = False,
    reply_to_id: str | None = None,
    username: str | None = None,
) -> StoredMessage:
    return StoredMessage(
        GROUP,
        message_id,
        user,
        display_name,
        text,
        at,
        thread_id=thread_id,
        is_bot=is_bot,
        reply_to_id=reply_to_id,
        username=username,
    )


def _capture_app(*, scripted: ScriptedProvider, store: ConversationStore):
    stores = memory_stores()
    responder = AgentResponder(
        scripted,
        system_prompt="Use the chat's normal safety rules and answer in the user's language.",
        settings=AgentSettings(recent_context_messages=24, ambient_total_chars=6000),
        limits=Limits(stores.cache),
    )
    app = BotApp(
        FakePlatform(name="tg"),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(store), _NicknameMemoryPlugin()],
        bot_name=BOT_NAME,
        responder=responder,
        group_mentions_only=True,
        conversation_threads=ThreadSettings(),
    )
    return stores, responder, app


async def _capture_ambient_request() -> dict:
    scripted = ScriptedProvider([Completion(Message.assistant("Fixture response."), "end_turn")])
    store = ConversationStore()
    stores, responder, app = _capture_app(scripted=scripted, store=store)
    now = datetime.now(UTC)
    rows: list[StoredMessage] = []
    exchanges = (
        ("trip-question", "Any trip news?"),
        ("audio-request", "Give audio"),
        ("food-question", "Any food ideas?"),
        ("follow-up", "One more question"),
    )
    for index, (message_id, body) in enumerate(exchanges):
        at = now.replace(microsecond=0)
        rows.append(
            _row(
                message_id,
                MEMBER_A.ref,
                MEMBER_A.display_name,
                body,
                at=at,
                username=MEMBER_A.username,
            )
        )
        rows.append(
            _row(
                f"bot-{index}",
                BOT_USER,
                BOT_NAME,
                "今日新聞簡報與旅程建議。" * 120,
                at=at,
                is_bot=True,
                reply_to_id=message_id,
            )
        )
    stores.repo._messages[GROUP.key].extend(rows)
    await stores.repo.upsert_member(GROUP, MEMBER_A)
    await stores.repo.upsert_member(GROUP, MEMBER_B)
    incoming = message(
        "@clawdot What should we eat?", sender=MEMBER_B, mention=True, chat=GROUP, thread_id="topic"
    )
    await responder.respond(incoming, "What should we eat?", app)
    return scripted.calls[0]


async def _capture_reply_request() -> dict:
    scripted = ScriptedProvider([Completion(Message.assistant("Fixture response."), "end_turn")])
    store = ConversationStore()
    stores, _, app = _capture_app(scripted=scripted, store=store)
    now = datetime.now(UTC)
    conversation = await store.create_conversation(GROUP, "topic", MEMBER_C.ref, at=now)
    expense_message = _row(
        "expense-question",
        MEMBER_C.ref,
        MEMBER_C.display_name,
        "Please confirm the shared expense.",
        at=now,
        username=MEMBER_C.username,
    )
    bot_reply = _row(
        "expense-confirmation",
        BOT_USER,
        BOT_NAME,
        "Member C paid the shared expense.",
        at=now,
        is_bot=True,
        reply_to_id=expense_message.message_id,
    )
    earlier_question = _row(
        "older-question-c",
        MEMBER_C.ref,
        MEMBER_C.display_name,
        "Any news for the trip?",
        at=now,
        username=MEMBER_C.username,
    )
    earlier_answer = _row(
        "older-answer-c",
        BOT_USER,
        BOT_NAME,
        "今日の旅行ニュースと旅程の要約です。" * 120,
        at=now,
        is_bot=True,
        reply_to_id=earlier_question.message_id,
    )
    stores.repo._messages[GROUP.key].extend(
        [earlier_question, earlier_answer, expense_message, bot_reply]
    )
    await stores.repo.upsert_member(GROUP, MEMBER_B)
    await stores.repo.upsert_member(GROUP, MEMBER_C)
    await store.map_messages(
        GROUP,
        conversation.id,
        [expense_message.message_id],
        primary_message_id=expense_message.message_id,
        is_bot=False,
        user=MEMBER_C.ref,
    )
    await store.map_messages(
        GROUP,
        conversation.id,
        [bot_reply.message_id],
        primary_message_id=bot_reply.message_id,
        is_bot=True,
        user=None,
    )
    current = message(
        "Please fix the split.",
        sender=MEMBER_B,
        chat=GROUP,
        thread_id="topic",
        reply_to_bot=True,
    )
    current = replace(current, reply_to=MessageRef(GROUP, bot_reply.message_id, "topic"))
    await app.handle(current)
    return scripted.calls[0]


async def _capture_summary_request() -> dict:
    scripted = ScriptedProvider(
        [Completion(Message.assistant("Fixture chat response."), "end_turn")]
    )
    store = ConversationStore()
    stores, responder, app = _capture_app(scripted=scripted, store=store)
    now = datetime.now(UTC)
    conversation = await store.create_conversation(GROUP, "topic", MEMBER_A.ref, at=now)
    rows = [
        _row(
            "summary-ambient-a",
            MEMBER_A.ref,
            MEMBER_A.display_name,
            "Please add the shared meal.",
            at=now,
            username=MEMBER_A.username,
        ),
        _row(
            "summary-ambient-bot",
            BOT_USER,
            BOT_NAME,
            "今日の旅程と共有の食事について確認しました。" * 100,
            at=now,
            is_bot=True,
            reply_to_id="summary-ambient-a",
        ),
    ]
    stores.repo._messages[GROUP.key].extend(rows)
    await stores.repo.upsert_member(GROUP, MEMBER_A)
    await stores.repo.upsert_member(GROUP, MEMBER_B)
    for role, speaker, message_id, body in (
        ("user", MEMBER_A, "summary-a", "Please add the shared meal."),
        ("assistant", None, "summary-answer-a", "I added it for Member A."),
        ("user", MEMBER_B, "summary-b", "Change the split to three people."),
        ("assistant", None, "summary-answer-b", "I can make that change for Member B."),
    ):
        content: dict = {"text": body}
        if speaker is not None:
            content.update(
                {
                    "raw_text": body,
                    "speaker": {
                        "user_key": speaker.ref.key,
                        "display_name": speaker.display_name,
                        "username": speaker.username,
                    },
                }
            )
        await store.add(
            GROUP,
            "topic",
            role,
            content,
            message_id,
            conversation_id=conversation.id,
        )
    current = message(
        "Please make the split three people.",
        sender=MEMBER_B,
        chat=GROUP,
        thread_id="topic",
    )
    token = current_conversation.set(conversation)
    try:
        await responder.respond(current, current.text, app)
    finally:
        current_conversation.reset(token)
    chat_system = scripted.calls[0]["system"]
    summary_script = ScriptedProvider(
        [Completion(Message.assistant("Fixture summary."), "end_turn")]
    )
    summary_system = getattr(conversation_module, "SUMMARY_SYSTEM_PROMPT", None) or chat_system
    await store.compact_conversation(
        GROUP,
        conversation,
        summary_script,
        summary_system,
        "scripted-model",
        force=True,
        keep=0,
    )
    return summary_script.calls[0]


async def capture_requests() -> dict[str, dict]:
    ambient = await _capture_ambient_request()
    reply = await _capture_reply_request()
    summary = await _capture_summary_request()
    return {
        "ambient-only-a": ambient,
        "reply-to-other-answer": reply,
        "summary": summary,
    }


def score(case: str, text: str) -> bool:
    """Return true when a completion shows wrong-member attribution or a bad summary."""
    folded = text.casefold()
    if "nickname-a" in folded:
        return True
    if case == "summary":
        return bool(re.search(r"\byou\b|\bthe user\b", folded)) or any(
            name.casefold() not in folded for name in ("Member A", "Member B")
        )
    wrong_name = "member a" if case == "ambient-only-a" else "member c"
    address = re.match(
        rf"^(?:short answer,\s*)?(?:(?:hi|hello|hey)\s+)?{re.escape(wrong_name)}\b",
        folded[:80].strip(),
    )
    return address is not None


async def run(runs: int, model: str) -> None:
    settings = LLMSettings(
        LLM_PROVIDER=os.environ.get("LLM_PROVIDER", "openai_compat"),
        LLM_BASE_URL=os.environ.get("LLM_BASE_URL"),
        LLM_MODEL=model,
        LLM_API_KEY=os.environ.get("LLM_API_KEY"),
    )
    provider = build_provider(settings)
    requests = await capture_requests()
    for case, captured in requests.items():
        failures = 0
        errors = 0
        for index in range(runs):
            try:
                completion = await provider.complete(
                    captured["messages"],
                    system=captured["system"],
                    tools=(),
                    max_tokens=1024,
                    session_key=f"speaker-attribution:{case}:{index}",
                )
                failures += score(case, completion.message.text)
            except Exception:  # noqa: BLE001 - report only an aggregate provider error count
                errors += 1
        print(f"{case}: wrong={failures}/{runs} rate={failures / runs:.0%} errors={errors}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", ""))
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    asyncio.run(run(args.runs, args.model))


if __name__ == "__main__":
    main()
