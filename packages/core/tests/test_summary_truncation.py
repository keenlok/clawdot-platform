import logging

from chatcore.conversation import SUMMARY_MAX_TOKENS, ConversationStore
from chatcore.llm.types import Completion, Message, Usage
from chatcore.testing import GROUP, ScriptedProvider, user


def done(text, reason="end_turn", usage=None):
    return Completion(Message.assistant(text), reason, usage or Usage())


async def test_compaction_defaults_to_1024_tokens_and_honors_explicit_limit():
    store = ConversationStore()
    provider = ScriptedProvider([done("summary"), done("another"), done("third"), done("fourth")])
    await store.add(GROUP, None, "user", {"text": "one"})
    await store.compact(GROUP, None, provider, "rules", "fake", force=True, keep=0)
    assert provider.calls[0]["max_tokens"] == SUMMARY_MAX_TOKENS == 1024
    await store.add(GROUP, None, "user", {"text": "two"})
    await store.compact(GROUP, None, provider, "rules", "fake", force=True, keep=0, max_tokens=33)
    assert provider.calls[1]["max_tokens"] == 33
    conversation = await store.create_conversation(GROUP, None, user().ref)
    await store.add(GROUP, None, "user", {"text": "one"}, conversation_id=conversation.id)
    await store.compact_conversation(
        GROUP, conversation, provider, "rules", "fake", force=True, keep=0
    )
    assert provider.calls[2]["max_tokens"] == 1024
    second_conversation = await store.create_conversation(GROUP, None, user().ref)
    await store.add(GROUP, None, "user", {"text": "two"}, conversation_id=second_conversation.id)
    await store.compact_conversation(
        GROUP, second_conversation, provider, "rules", "fake", force=True, keep=0, max_tokens=77
    )
    assert provider.calls[3]["max_tokens"] == 77


async def test_truncated_summary_retries_and_saves_clean_shorter_result(caplog):
    store = ConversationStore()
    await store.add(GROUP, None, "user", {"text": "one"})
    provider = ScriptedProvider(
        [done("partial", "max_tokens", Usage(2, 3)), done("complete", usage=Usage(5, 7))]
    )
    caplog.set_level(logging.INFO)
    result = await store.compact(GROUP, None, provider, "rules", "fake", force=True, keep=0)
    assert (await store.summary(GROUP, None))[0] == "complete"
    assert result.usage == Usage(7, 10)
    assert result.truncated
    assert not result.dropped
    assert "shorter summary" in provider.calls[1]["messages"][0].text
    assert "truncated=True" in caplog.text


async def test_repeated_truncation_keeps_prior_summary_and_covers_until(caplog):
    store = ConversationStore()
    await store.add(GROUP, None, "user", {"text": "one"})
    await store.save_summary(GROUP, None, "previous", 0)
    provider = ScriptedProvider([done("cut", "max_tokens"), done("still cut", "max_tokens")])
    caplog.set_level(logging.WARNING)
    result = await store.compact(GROUP, None, provider, "rules", "fake", force=True, keep=0)
    assert await store.summary(GROUP, None) == ("previous", 0)
    assert result.truncated and result.turns_summarized == 0
    assert result.dropped
    assert "conversation summary dropped" in caplog.text


async def test_empty_summary_retries_and_sanitizes_markup_before_save():
    store = ConversationStore()
    await store.add(GROUP, None, "user", {"text": "one"})
    provider = ScriptedProvider(
        [done("<｜DSML｜ calls>call</｜DSML｜ calls>"), done("<tool_call>bad</tool_call>")]
    )
    result = await store.compact(GROUP, None, provider, "rules", "fake", force=True, keep=0)
    assert result.truncated and result.turns_summarized == 0
    assert await store.summary(GROUP, None) is None


async def test_successful_summary_with_markup_is_stored_without_markup():
    store = ConversationStore()
    await store.add(GROUP, None, "user", {"text": "one"})
    provider = ScriptedProvider([done("Useful summary <tool_call>hidden</tool_call>")])
    result = await store.compact(GROUP, None, provider, "rules", "fake", force=True, keep=0)
    assert not result.truncated
    assert await store.summary(GROUP, None) == ("Useful summary", 1)


async def test_whitespace_only_summary_retries_and_saves_retry_text():
    store = ConversationStore()
    await store.add(GROUP, None, "user", {"text": "one"})
    provider = ScriptedProvider([done("   \n  "), done("retry summary")])
    result = await store.compact(GROUP, None, provider, "rules", "fake", force=True, keep=0)
    assert result.truncated and not result.dropped
    assert await store.summary(GROUP, None) == ("retry summary", 1)


async def test_whitespace_retry_is_dropped_with_empty_reason(caplog):
    store = ConversationStore()
    await store.add(GROUP, None, "user", {"text": "one"})
    await store.save_summary(GROUP, None, "prior", 0)
    caplog.set_level(logging.WARNING)
    provider = ScriptedProvider([done("first", "max_tokens"), done("   ")])
    result = await store.compact(GROUP, None, provider, "rules", "fake", force=True, keep=0)
    assert result.dropped and result.turns_summarized == 0
    assert await store.summary(GROUP, None) == ("prior", 0)
    assert "reason=empty" in caplog.text


async def test_threaded_summary_retries_whitespace_and_saves_retry_text():
    store = ConversationStore()
    conversation = await store.create_conversation(GROUP, None, user().ref)
    await store.add(GROUP, None, "user", {"text": "one"}, conversation_id=conversation.id)
    provider = ScriptedProvider([done("   "), done("thread summary", usage=Usage(2, 3))])
    result = await store.compact_conversation(
        GROUP, conversation, provider, "rules", "fake", force=True, keep=0
    )
    assert result.truncated and not result.dropped
    assert await store.conversation_summary(GROUP, conversation.id) == ("thread summary", 1)
    assert result.usage == Usage(2, 3)


async def test_threaded_summary_drop_keeps_previous_summary_and_coverage():
    store = ConversationStore()
    conversation = await store.create_conversation(GROUP, None, user().ref)
    await store.save_conversation_summary(
        GROUP, conversation.id, "prior thread summary", 0, generation=conversation.reset_generation
    )
    await store.add(GROUP, None, "user", {"text": "one"}, conversation_id=conversation.id)
    provider = ScriptedProvider([done("cut", "max_tokens"), done("still cut", "max_tokens")])
    result = await store.compact_conversation(
        GROUP, conversation, provider, "rules", "fake", force=True, keep=0
    )
    assert result.truncated and result.dropped and result.turns_summarized == 0
    assert await store.conversation_summary(GROUP, conversation.id) == ("prior thread summary", 0)


async def test_threaded_whitespace_retry_is_dropped_without_advancing_coverage(caplog):
    store = ConversationStore()
    conversation = await store.create_conversation(GROUP, None, user().ref)
    await store.save_conversation_summary(
        GROUP, conversation.id, "prior thread summary", 0, generation=conversation.reset_generation
    )
    await store.add(GROUP, None, "user", {"text": "one"}, conversation_id=conversation.id)
    caplog.set_level(logging.WARNING)
    provider = ScriptedProvider([done("first", "max_tokens"), done(" \n ")])
    result = await store.compact_conversation(
        GROUP, conversation, provider, "rules", "fake", force=True, keep=0
    )
    assert result.dropped and result.turns_summarized == 0
    assert await store.conversation_summary(GROUP, conversation.id) == ("prior thread summary", 0)
    assert "reason=empty" in caplog.text
