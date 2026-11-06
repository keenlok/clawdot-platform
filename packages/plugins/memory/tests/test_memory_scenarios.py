from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from chatcore.conversation import Conversation
from chatcore.embed import FakeEmbedder
from chatcore.events import ChatRef, Mention, User, UserRef
from chatcore.llm.types import Completion, Message, Usage
from chatcore.plugin import CommandContext
from chatcore.store.base import SearchHit, Stores
from chatcore.store.memory import MemoryCache, MemoryCoreRepo
from chatcore.testing import FakePlatform, ScriptedProvider, message
from chatcore.threads import TurnOrigin, current_conversation, current_turn_origin
from chatcore.tools.registry import ToolContext
from chatcore_memory.plugin import MemoryPlugin


class ScenarioSearch:
    supports_knn = True

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.chats: dict[str, str] = {}
        self.delete_fails = False

    async def index(self, _index, doc_id, doc, *, chat):
        self.docs[doc_id] = dict(doc)
        self.chats[doc_id] = chat.key

    async def search(self, _index, *, chat, text=None, vector=None, filters=None, size=10):
        found = [
            (key, value)
            for key, value in self.docs.items()
            if self.chats[key] == chat.key
            and (not text or text.casefold() in value["text"].casefold())
        ]
        return [SearchHit(key, 1.0, value) for key, value in found[:size]]

    async def delete(self, _index, *, chat, query=None, refresh_first=False):
        if self.delete_fails:
            self.delete_fails = False
            raise ConnectionError("scenario search delete failure")
        term = (query or {}).get("term", {})
        doomed = [
            key
            for key, value in self.docs.items()
            if self.chats[key] == chat.key and all(value.get(k) == v for k, v in term.items())
        ]
        for key in doomed:
            del self.docs[key]
            del self.chats[key]
        return len(doomed)


def load_scenarios() -> list[dict[str, Any]]:
    fixture = Path(__file__).parent / "fixtures/memory_scenarios.jsonl"
    return [json.loads(line) for line in fixture.read_text().splitlines() if line.strip()]


def scenario_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep deterministic safety measures separate; this is not a quality score."""
    categories: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = categories.setdefault(result["category"], {"pass": 0, "fail": 0})
        bucket[result["baseline"]] += 1
    return {
        "wrong_person_mutations": sum(item["wrong_person_mutations"] for item in results),
        "wrong_person_answers": sum(item["wrong_person_answers"] for item in results),
        "stale_current_answers": sum(item["stale_current_answers"] for item in results),
        "erased_fact_returns": sum(item["erased_fact_returns"] for item in results),
        "erased_text_index_documents": sum(item["erased_text_index_documents"] for item in results),
        "categories": categories,
    }


def _origin_context(turn: dict[str, Any]):
    @contextmanager
    def origin_context():
        origin = TurnOrigin(turn.get("origin", "direct"), turn.get("source_message_id"))
        token = current_turn_origin.set(origin)
        try:
            yield
        finally:
            current_turn_origin.reset(token)

    return origin_context()


@contextmanager
def _conversation_context(
    chat: ChatRef,
    conversation_name: str | None,
    conversations: dict[tuple[str, str], Conversation],
):
    conversation = None
    if conversation_name is not None:
        key = (chat.key, conversation_name)
        conversation = conversations.get(key)
        if conversation is None:
            now = datetime.now(UTC)
            conversation = Conversation(
                id=len(conversations) + 1,
                chat=chat,
                thread_id=None,
                participants=frozenset(),
                reset_generation=0,
                created_at=now,
                last_active_at=now,
            )
            conversations[key] = conversation
    token = current_conversation.set(conversation)
    try:
        yield
    finally:
        current_conversation.reset(token)


def _user(users: dict[str, User], name: str, *, is_bot: bool = False) -> User:
    existing = users.get(name)
    if existing is None:
        existing = User(UserRef("fake", name.lower()), name, name.lower(), is_bot)
        users[name] = existing
    return existing


def _make_mentions(
    specs: list[dict[str, Any]], text: str, users: dict[str, User]
) -> tuple[Mention, ...]:
    mentions = []
    for spec in specs:
        target_name = spec["target"]
        target = _user(users, target_name, is_bot=bool(spec.get("is_bot", False)))
        mention_text = spec.get("text", f"@{target_name}")
        start = spec.get("start", text.find(mention_text))
        if start < 0:
            raise ValueError(f"mention text missing from scripted turn: {mention_text}")
        end = spec.get("end", start + len(mention_text))
        mentions.append(
            Mention(
                start=start,
                end=end,
                kind=spec.get("kind", "text_mention"),
                text=mention_text,
                target=target.ref,
                username=target.username,
                display_name=target.display_name,
                is_bot=target.is_bot,
            )
        )
    return tuple(mentions)


async def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    search = ScenarioSearch()
    search.delete_fails = bool(scenario.get("search_delete_fails", False))
    core_repo = MemoryCoreRepo()
    stores = Stores(repo=core_repo, cache=MemoryCache(), search=search)
    provider = ScriptedProvider(responses=[])
    plugin = MemoryPlugin(embedder=FakeEmbedder(), provider=provider)
    chats: dict[str, ChatRef] = {}
    users: dict[str, User] = {}
    conversations: dict[tuple[str, str], Conversation] = {}
    explicit = scenario.get("explicit", [])
    action_errors: list[str] = []
    mutations: list[dict[str, Any]] = []
    provider_call_turns: list[int] = []
    erased_text = scenario.get("expect", {}).get("erased_absent")
    erased_text_seen = False
    erased_text_seen_before_erasure = False

    async def snapshot_rows() -> dict[int, dict[str, Any]]:
        result = {}
        for chat in chats.values():
            result.update(
                {int(row["id"]): row for row in await plugin._repo(stores).list(chat, limit=500)}
            )
        return result

    async def observe_erased_text() -> bool:
        if not erased_text:
            return False
        repo_rows = await snapshot_rows()
        return any(erased_text in row["text"] for row in repo_rows.values()) or any(
            erased_text in row["text"] for row in search.docs.values()
        )

    def record_mutations(
        before: dict[int, dict[str, Any]], after: dict[int, dict[str, Any]], actor: User
    ):
        for memory_id in before.keys() & after.keys():
            old, new = before[memory_id], after[memory_id]
            if old["text"] != new["text"] and old["created_by"] != actor.ref.key:
                mutations.append(
                    {
                        "id": memory_id,
                        "before": old["text"],
                        "after": new["text"],
                        "actor": actor.ref.key,
                    }
                )

    async def apply_action(action: dict[str, Any]) -> None:
        nonlocal erased_text_seen, erased_text_seen_before_erasure
        chat_name = action.get("chat", "room")
        chat = chats.setdefault(chat_name, ChatRef("fake", chat_name))
        actor = _user(users, action.get("speaker", "Alice"))
        await core_repo.upsert_member(chat, actor)
        tool_ctx = ToolContext(chat, actor, False, FakePlatform(), stores)
        present_before_action = await observe_erased_text()
        erased_text_seen = erased_text_seen or present_before_action
        if action["action"] in {"purge_user", "purge_chat", "forget"}:
            erased_text_seen_before_erasure = (
                erased_text_seen_before_erasure or present_before_action
            )
        before = await snapshot_rows()
        if action["action"] == "remember":
            remember_tool = next(tool for tool in plugin.tools() if tool.name == "remember")
            arguments = {"fact": action["text"], "about": action.get("about")}
            if "replaces" in action:
                arguments["replaces"] = action["replaces"]
            try:
                result = await remember_tool.handler(tool_ctx, **arguments)
            except TypeError:
                action_errors.append("remember rejected the scripted replaces argument")
            else:
                if result.pending_action is None:
                    action_errors.append("remember did not create a pending action")
                else:
                    ctx = CommandContext(chat, actor, False, False, tool_ctx.platform, stores)
                    await plugin.action_handlers()["remember"].commit(result.pending_action, ctx)
        elif action["action"] == "forget":
            forget_tool = next(tool for tool in plugin.tools() if tool.name == "forget")
            result = await forget_tool.handler(tool_ctx, memory_id=action["memory_id"])
            if result.pending_action is None:
                action_errors.append("forget did not create a pending action")
            else:
                ctx = CommandContext(chat, actor, False, False, tool_ctx.platform, stores)
                await plugin.action_handlers()["forget"].commit(result.pending_action, ctx)
        elif action["action"] == "purge_user":
            target_name = action["user"]
            target = _user(users, target_name)
            await core_repo.upsert_member(chat, target)
            await plugin.purge_user(chat, target.ref, stores)
        elif action["action"] == "purge_chat":
            await plugin.purge_chat(chat, stores)
        if action["action"] in {"purge_user", "purge_chat", "forget"}:
            await plugin.process_index_work(stores)
        after = await snapshot_rows()
        record_mutations(before, after, actor)
        erased_text_seen = erased_text_seen or await observe_erased_text()

    for turn_index, turn in enumerate(scenario["turns"]):
        for action in explicit:
            if action.get("before_turn") == turn_index:
                await apply_action(action)
        chat = chats.setdefault(turn["chat"], ChatRef("fake", turn["chat"]))
        speaker = _user(users, turn["speaker"])
        await core_repo.upsert_member(chat, speaker)
        mentions = _make_mentions(turn.get("mentions", []), turn["text"], users)
        for mention in mentions:
            if mention.target is not None:
                await core_repo.upsert_member(
                    chat, _user(users, (mention.display_name or mention.target.id).title())
                )
        msg = replace(message(turn["text"], sender=speaker, chat=chat), mentions=mentions)
        await core_repo.log_message(msg)
        before = await snapshot_rows()
        calls_before = len(provider.calls)
        turn_output = scenario.get("extraction", [])[turn_index : turn_index + 1]
        provider.responses[:] = [
            Completion(Message.assistant(json.dumps(output)), "end_turn", Usage(1, 1))
            for output in turn_output
        ]
        try:
            with (
                _origin_context(turn),
                _conversation_context(chat, turn.get("conversation"), conversations),
            ):
                await plugin.after_turn(msg, turn["text"], stores)
        finally:
            provider.responses.clear()
        if len(provider.calls) > calls_before:
            provider_call_turns.append(turn_index)
        after = await snapshot_rows()
        record_mutations(before, after, speaker)
        erased_text_seen = erased_text_seen or await observe_erased_text()
    for action in explicit:
        if "before_turn" not in action:
            await apply_action(action)

    # Exercise the real memory command handler and its rendered listing path.
    for chat in chats.values():
        actor = next(iter(users.values()), None)
        if actor is None:
            actor = _user(users, "Alice")
        ctx = CommandContext(chat, actor, False, False, FakePlatform(), stores)
        command = next(item for item in plugin.commands() if item.name == "memory")
        await command.handler(ctx, "")

    rows = [
        row for chat in chats.values() for row in await plugin._repo(stores).list(chat, limit=500)
    ]
    contexts: dict[str, str | None] = {}
    for key in ("context_contains", "context_excludes"):
        spec = scenario.get("expect", {}).get(key)
        if not isinstance(spec, dict):
            continue
        chat_name = spec["chat"]
        chat = chats.setdefault(chat_name, ChatRef("fake", chat_name))
        query = spec.get("query", "")
        conversation_name = spec.get("query_conversation")
        msg = message(query, chat=chat)
        with _conversation_context(chat, conversation_name, conversations):
            contexts[chat_name] = await plugin.before_turn(msg, query, stores)

    result: dict[str, Any] = {
        "id": scenario["id"],
        "category": scenario["category"],
        "kind": scenario["kind"],
        "baseline": scenario["baseline"],
        "rows": rows,
        "contexts": contexts,
        "search_docs": list(search.docs.values()),
        "provider_calls": provider.calls,
        "provider_call_turns": provider_call_turns,
        "mutations": mutations,
        "action_errors": action_errors,
        "erased_text_seen": erased_text_seen,
        "erased_text_seen_before_erasure": erased_text_seen_before_erasure,
    }
    result["wrong_person_mutations"] = len(mutations)
    result["wrong_person_answers"] = _wrong_person_answer_count(scenario, rows, contexts, users)
    result["stale_current_answers"] = _stale_current_answer_count(scenario, contexts)
    result["erased_fact_returns"] = _erased_fact_return_count(scenario, rows)
    result["erased_text_index_documents"] = _erased_index_count(scenario, search.docs)
    result["errors"] = _scenario_errors(scenario, result)
    return result


def _scenario_errors(scenario: dict[str, Any], result: dict[str, Any]) -> list[str]:
    expect = scenario.get("expect", {})
    errors = list(result["action_errors"])
    if "rows" in expect:
        actual = [
            {"text": row["text"], "subject": row["subject_user_key"], "creator": row["created_by"]}
            for row in result["rows"]
        ]
        wanted = sorted(
            expect["rows"], key=lambda row: (row["text"], row["subject"] or "", row["creator"])
        )
        actual = sorted(actual, key=lambda row: (row["text"], row["subject"] or "", row["creator"]))
        if actual != wanted:
            errors.append("stored rows differ")
    for expected in expect.get("wording_unchanged", []):
        row = next((row for row in result["rows"] if int(row["id"]) == expected["id"]), None)
        if (
            row is None
            or row["text"] != expected["text"]
            or row["created_by"] != expected["creator"]
        ):
            errors.append("wording_unchanged assertion failed")
    if expect.get("no_wrong_person_mutation") and result["wrong_person_mutations"]:
        errors.append("a different speaker changed an existing row")
    for kind in ("context_contains", "context_excludes"):
        if kind not in expect:
            continue
        spec = expect[kind]
        contains = (
            spec["text"].casefold() in (result["contexts"].get(spec["chat"]) or "").casefold()
        )
        if contains != (kind == "context_contains"):
            errors.append(f"{kind} assertion failed")
    for turn_index in expect.get("no_extraction_call", []):
        if turn_index in result["provider_call_turns"]:
            errors.append(f"extraction ran for turn {turn_index}")
    for turn_index in expect.get("extraction_call", []):
        if turn_index not in result["provider_call_turns"]:
            errors.append(f"extraction did not run for turn {turn_index}")
    for turn_index in expect.get("extraction_marks_untrusted", []):
        try:
            call_index = result["provider_call_turns"].index(turn_index)
        except ValueError:
            errors.append(f"no extraction request recorded for turn {turn_index}")
            continue
        request = result["provider_calls"][call_index]
        request_text = (
            str(request["system"])
            + " "
            + " ".join(str(message.text) for message in request["messages"])
        )
        if "untrusted" not in request_text.casefold():
            errors.append(f"extraction request for turn {turn_index} did not mark body untrusted")
    if "erased_absent" in expect:
        text = expect["erased_absent"]
        if expect.get("never_stored"):
            if result["erased_text_seen"]:
                errors.append("never_stored text appeared in repository or search index")
        elif not result["erased_text_seen_before_erasure"]:
            errors.append("erased text was never present before an erasure action")
        if any(text in row["text"] for row in result["rows"]):
            errors.append("erased fact returned from repository")
        if any(text in row["text"] for row in result["search_docs"]):
            errors.append("erased text remained in search index")
    return errors


def _wrong_person_answer_count(
    scenario: dict[str, Any],
    rows: list[dict[str, Any]],
    contexts: dict[str, str | None],
    users: dict[str, User],
) -> int:
    count = 0
    for spec in (
        scenario.get("expect", {}).get("context_contains"),
        scenario.get("expect", {}).get("context_excludes"),
    ):
        if not isinstance(spec, dict):
            continue
        rendered_lines = (contexts.get(spec["chat"]) or "").splitlines()
        for row in rows:
            line = next(
                (
                    line
                    for line in rendered_lines
                    if re.search(rf"#{int(row['id'])}(?:\s|\[|$)", line)
                ),
                None,
            )
            if line is None:
                continue
            if row.get("subject_user_key"):
                user = users.get(row["subject_user_key"].split(":", 1)[-1].title())
                name = user.display_name if user else "unknown person"
            elif row["source"] == "auto":
                user = users.get(row["created_by"].split(":", 1)[-1].title())
                name = user.display_name if user else "unknown person"
            else:
                user = users.get(row["created_by"].split(":", 1)[-1].title())
                name = user.display_name if user else "unknown person"
            attribution_phrases = (
                f"about {name}",
                f"from {name}",
                f"saved by {name}",
            )
            if not any(phrase.casefold() in line.casefold() for phrase in attribution_phrases):
                count += 1
    return count


def _stale_current_answer_count(scenario: dict[str, Any], contexts: dict[str, str | None]) -> int:
    spec = scenario.get("expect", {}).get("context_excludes")
    if not isinstance(spec, dict):
        return 0
    return int(spec["text"].casefold() in (contexts.get(spec["chat"]) or "").casefold())


def _erased_fact_return_count(scenario: dict[str, Any], rows: list[dict[str, Any]]) -> int:
    text = scenario.get("expect", {}).get("erased_absent")
    return int(bool(text) and any(text in row["text"] for row in rows))


def _erased_index_count(scenario: dict[str, Any], docs: dict[str, dict[str, Any]]) -> int:
    text = scenario.get("expect", {}).get("erased_absent")
    return int(bool(text) and any(text in row["text"] for row in docs.values()))


def test_fixture_has_required_coverage_and_failure_notes():
    scenarios = load_scenarios()
    categories = {
        "similar_wording",
        "ambiguous_subject",
        "explicit_vs_auto",
        "new_conversation",
        "correction",
        "expired_plan",
        "multilingual",
        "injection",
        "bare_tag_reply",
        "erasure",
    }
    dimensions = {"extraction", "cross_session", "knowledge_update", "temporal", "abstention"}
    assert len(scenarios) >= 30
    assert len({item["id"] for item in scenarios}) == len(scenarios)
    assert categories <= {item["category"] for item in scenarios}
    assert dimensions <= {item["dimension"] for item in scenarios}
    assert all(item.get("note") for item in scenarios if item["baseline"] == "fail")
    assert all(item.get("kind") in {"regression", "control"} for item in scenarios)
    assert all(item.get("baseline") in {"pass", "fail"} for item in scenarios)
    for item in scenarios:
        assert {"id", "kind", "category", "dimension", "turns", "expect", "baseline"} <= item.keys()
        assert all({"chat", "speaker", "text"} <= turn.keys() for turn in item["turns"])
        assert all(
            turn.get("origin", "direct") in {"direct", "bare_tag", "synthetic"}
            for turn in item["turns"]
        )
        if "extraction" in item:
            assert len(item["extraction"]) == len(item["turns"])
        assert isinstance(item.get("explicit", []), list)
        assert isinstance(item["expect"], dict)
        if item["baseline"] == "fail":
            assert item.get("target_slice") in {"1", "2a", "3"}
        else:
            assert "target_slice" not in item
    assert sum(len(item["turns"]) > 1 for item in scenarios) >= 10
    assert any(
        item["category"] == "erasure"
        and any(
            action.get("action") == "purge_user"
            and action.get("user") not in {turn["speaker"] for turn in item["turns"]}
            for action in item.get("explicit", [])
        )
        for item in scenarios
    )


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(
            item,
            id=item["id"],
            marks=pytest.mark.xfail(
                strict=True, reason=item.get("note", "baseline scenario failure")
            ),
        )
        if item["baseline"] == "fail"
        else pytest.param(item, id=item["id"])
        for item in load_scenarios()
    ],
)
async def test_scripted_memory_scenario_matches_required_behavior(scenario):
    result = await run_scenario(scenario)
    assert not result["errors"], "; ".join(result["errors"])


def test_scenario_summary_reports_safety_measures_separately():
    results = [
        {
            "category": "erasure",
            "baseline": "fail",
            "wrong_person_mutations": 1,
            "wrong_person_answers": 2,
            "stale_current_answers": 3,
            "erased_fact_returns": 4,
            "erased_text_index_documents": 5,
        },
        {
            "category": "erasure",
            "baseline": "pass",
            "wrong_person_mutations": 0,
            "wrong_person_answers": 0,
            "stale_current_answers": 0,
            "erased_fact_returns": 0,
            "erased_text_index_documents": 0,
        },
    ]
    summary = scenario_summary(results)
    assert summary["wrong_person_mutations"] == 1
    assert summary["wrong_person_answers"] == 2
    assert summary["stale_current_answers"] == 3
    assert summary["erased_fact_returns"] == 4
    assert summary["erased_text_index_documents"] == 5
    assert summary["categories"]["erasure"] == {"pass": 1, "fail": 1}


@pytest.mark.asyncio
async def test_deterministic_suite_summary_keeps_each_measure_separate():
    results = [await run_scenario(item) for item in load_scenarios()]
    summary = scenario_summary(results)
    assert set(summary) == {
        "wrong_person_mutations",
        "wrong_person_answers",
        "stale_current_answers",
        "erased_fact_returns",
        "erased_text_index_documents",
        "categories",
    }
    totals = {item["category"]: 0 for item in load_scenarios()}
    for item in load_scenarios():
        totals[item["category"]] += 1
    for category, count in totals.items():
        assert sum(summary["categories"][category].values()) == count


@pytest.mark.asyncio
async def test_erasure_runner_reports_noop_purge(monkeypatch):
    async def no_op_purge(_self, _chat, _user_ref, _stores):
        return None

    monkeypatch.setattr(MemoryPlugin, "purge_user", no_op_purge)
    erasure = next(item for item in load_scenarios() if item["id"] == "erasure-01")
    result = await run_scenario(erasure)
    assert "erased fact returned from repository" in result["errors"]
