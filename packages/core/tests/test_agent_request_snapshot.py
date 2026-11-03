"""The legacy provider request shape is frozen before skills change the agent."""

import json
import os
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.display_time_plugin import DisplayTimePlugin
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message, ToolCall, Usage
from chatcore.plugin import BasePlugin
from chatcore.skill_turn import SkillSettings
from chatcore.skills import Skill, SkillsMode
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.tools.registry import Tool, ToolResult

SNAPSHOT = Path(__file__).parent / "snapshots" / "agent_requests_off.json"


class SnapshotTools(BasePlugin):
    name = "snapshot_tools"

    def tools(self):
        async def echo(ctx, text: str):
            return ToolResult("echo: " + text)

        async def admin(ctx):
            return ToolResult("admin")

        return [
            Tool(
                "snapshot_echo",
                "Echo a value",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                echo,
            ),
            Tool(
                "snapshot_admin",
                "Admin value",
                {"type": "object", "properties": {}},
                admin,
                admin_only=True,
            ),
        ]


def _message(item):
    return {
        "role": item.role.value,
        "text": item.text,
        "tool_call_id": item.tool_call_id,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in item.tool_calls
        ],
        "content": [
            {"type": type(part).__name__, **({"text": part.text} if hasattr(part, "text") else {})}
            for part in item.content
        ],
    }


def _request(call):
    return {
        "system": call["system"],
        "messages": [_message(item) for item in call["messages"]],
        "tools": [asdict(tool) for tool in call["tools"]],
        "tool_choice": call["tool_choice"],
        "max_tokens": call["max_tokens"],
        "session_key": call["session_key"],
    }


def _completion(text="done", *, calls=(), reason="end_turn", usage=None):
    usage = usage or Usage(12, 4)
    return Completion(Message.assistant(text, tuple(calls)), reason, usage)


@pytest.mark.parametrize("scenario", ["normal", "final", "retry"])
@pytest.mark.parametrize("wired", [False, True])
@pytest.mark.parametrize("skill_mode", ["off", "shadow", "on_other"])
async def test_agent_requests_match_legacy_snapshot(monkeypatch, scenario, wired, skill_mode):
    monkeypatch.setattr("chatcore.agent.secrets.token_hex", lambda _: "fixedtag")
    tool_call = ToolCall("call-1", "snapshot_echo", {"text": "hello"})
    scripted = {
        "normal": [_completion("", calls=[tool_call], reason="tool_use"), _completion("answer")],
        "final": [_completion("", calls=[tool_call], reason="tool_use"), _completion("answer")],
        "retry": [
            _completion("", calls=[tool_call], reason="tool_use"),
            _completion("", reason="end_turn"),
            _completion("retry answer"),
        ],
    }[scenario]
    stores = memory_stores()
    provider = ScriptedProvider(scripted)
    responder = AgentResponder(
        provider,
        system_prompt="snapshot system",
        settings=AgentSettings(
            tool_steps=1 if scenario in {"final", "retry"} else 8,
            skills=SkillSettings(
                mode=SkillsMode.ON if skill_mode == "on_other" else SkillsMode(skill_mode),
                chats=frozenset({"fake:other"}) if skill_mode == "on_other" else frozenset(),
                router="keyword",
            ),
        ),
        limits=Limits(stores.cache),
        clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[DisplayTimePlugin(), ConversationPlugin(), SnapshotTools()],
        responder=responder,
        bot_name="snapshotbot",
        group_mentions_only=True,
        skills=(
            Skill(
                "snapshot_composite",
                "Snapshot test composite",
                ("one", "two", "三", "can do lah"),
                ("snapshot_echo",),
            ),
        )
        if wired
        else (),
        general_tools=("get_current_time",) if wired else (),
    )
    message_id = {"normal": "1", "final": "2", "retry": "3"}[scenario]
    incoming = replace(message("snapshot request", mention=True), message_id=message_id)
    await responder.respond(incoming, incoming.text, app)
    if scenario == "retry":
        assert [call["tool_choice"] for call in provider.calls] == ["auto", "none", "none"]
        assert provider.calls[2]["system"].endswith(
            "Tool calls are impossible in this response. Any tool-call syntax will be discarded. "
            "Reply in plain text for the user."
        )
    fixture_key = scenario
    serialized = {fixture_key: [_request(call) for call in provider.calls]}
    if os.getenv("UPDATE_SNAPSHOT") == "1":
        existing = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}
        existing.update(serialized)
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n")
    expected = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}
    assert serialized[fixture_key] == expected[fixture_key]
