"""Reproduce the D4 follow-up failure with real routing, history and Google handlers."""

import io
import json
import zipfile
from dataclasses import replace

import pytest
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.embed import FakeEmbedder
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message, ToolCall
from chatcore.skill_turn import SkillSettings
from chatcore.skills import SkillsMode
from chatcore.store.base import SearchHit
from chatcore.testing import GROUP, ScriptedProvider, message, user
from chatcore_google.plugin import GooglePlugin
from chatcore_search import SearchPlugin

URL = "https://docs.google.com/document/d/file/edit"


def completion(text="", *calls):
    return Completion(Message.assistant(text, calls), "tool_use" if calls else "end_turn")


@pytest.mark.parametrize("reader", ["google_read_doc", "google_read_file"])
async def test_another_member_can_recover_full_docx_after_a_clipped_history_answer(
    google_service, docx_bytes, reader
):
    service, client, initial_ctx, _ = google_service
    stores, platform = initial_ctx.stores, initial_ctx.platform
    source_text = "\n".join(f"D{day} destination {day}" for day in range(1, 9))
    # Keep the real DOCX parser on the agent path; fake only Google transport bytes.
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as original, zipfile.ZipFile(output, "w") as copy:
        for part in original.namelist():
            data = original.read(part).replace(b"Tokyo itinerary", source_text.encode())
            copy.writestr(part, data)
    client.media.return_value = output.getvalue()
    client.metadata.return_value = {
        "name": "Itinerary.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "capabilities": {"canDownload": True},
    }
    long_answer = "\n".join(f"D{day} " + "香港行程 " * 100 for day in range(1, 9))
    provider = ScriptedProvider(
        [
            completion("", ToolCall("first-read", reader, {"url": URL})),
            completion(long_answer),
            completion(
                "",
                ToolCall(
                    "find-source",
                    "read_chat_history",
                    {"query": "file", "include_bot_answers": True},
                ),
            ),
            completion("", ToolCall("load-google", "use_skill", {"name": "google_workspace"})),
            completion(
                "",
                ToolCall(
                    "reread",
                    reader,
                    {"url": URL, "source_message_ref": "original-human-post"},
                ),
            ),
            completion("D8 destination 8 is available from the original document."),
        ]
    )

    class HistoryIndex:
        async def search(self, _index, *, chat, **_kwargs):
            rows = await stores.repo.recent_messages(chat, limit=20)
            return [
                SearchHit(
                    "known-history",
                    1,
                    {"message_ids": [row.message_id for row in rows], "text": "file"},
                )
            ]

    # Only the search backend is faked; source expansion still reads the real scoped repo.
    stores.search = HistoryIndex()
    search = SearchPlugin(FakeEmbedder())
    conversation = ConversationPlugin()
    responder = AgentResponder(
        provider,
        system_prompt="rules",
        skills_prompt="skills",
        limits=Limits(stores.cache),
        settings=AgentSettings(
            skills=SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword")
        ),
    )
    app = BotApp(
        platform,
        stores=stores,
        access=service.access,
        plugins=[
            conversation,
            search,
            GooglePlugin(service.settings, service.access, service=service),
        ],
        bot_name="bot",
        responder=responder,
    )
    first = replace(
        message("Read this DOCX " + URL, sender=initial_ctx.user, mention=True),
        message_id="original-human-post",
    )
    await app.handle(first)
    await app.handle(
        message("Any suggestions for the itinerary?", sender=user("bob"), mention=True)
    )

    history = next(
        item for item in provider.calls[3]["messages"] if item.tool_call_id == "find-source"
    )
    assert "original-human-post" in history.text and '"next_offset":' in history.text
    assert "…[truncated]" not in history.text  # The marker is safely JSON-escaped.
    reread = next(item for item in provider.calls[5]["messages"] if item.tool_call_id == "reread")
    assert not reread.is_error and "D8 destination 8" in reread.text
    assert client.media.await_count == 2
    turns = await conversation.store.recent(GROUP, None, limit=50)
    persisted = json.dumps([turn.content for turn in turns])
    assert all(source_text not in turn.content.get("text", "") for turn in turns)
    assert "[Tool result omitted from persistent history.]" in persisted
    assert platform.sent[-1].text == "D8 destination 8 is available from the original document."
