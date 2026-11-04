"""Read-only follow-ups resolve original human posts, never model-written URLs."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from chatcore.events import ChatRef
from chatcore.testing import message, user
from chatcore.tools.registry import ToolRegistry
from chatcore_google.itinerary import HEADER, VERSION
from chatcore_google.itinerary_service import ItineraryService
from chatcore_google.plugin import GooglePlugin

URL = "https://docs.google.com/document/d/file/edit"


async def share(ctx, text=URL, *, thread_id=None):
    incoming = replace(
        message(text, sender=user("another-member"), thread_id=thread_id),
        chat=ctx.chat,
        message_id="human-source",
    )
    await ctx.stores.repo.log_message(incoming)
    return await ctx.stores.repo.get_message(ctx.chat, incoming.message_id)


@pytest.mark.parametrize("reader", ["read_doc", "read_file"])
async def test_followup_docx_read_uses_logged_human_source_and_rechecks_remote_file(
    google_service, docx_bytes, reader
):
    service, client, ctx, _ = google_service
    await share(ctx, "Earlier itinerary: " + "long introduction " * 160 + URL)
    client.metadata.return_value = {
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "capabilities": {"canDownload": True},
    }
    client.media.return_value = docx_bytes
    read = getattr(service, reader)
    await read(ctx, URL)
    ctx.turn.human_urls = frozenset()
    ctx.turn.seen_urls.clear()
    result = await read(ctx, URL, source_message_ref="human-source")
    assert "Tokyo itinerary" in result
    assert client.identity.await_count == 2
    assert client.metadata.await_count == 2
    assert client.media.await_count == 2
    assert ctx.turn.human_urls == frozenset()


@pytest.mark.parametrize(
    "kind", ["missing", "bot", "chat", "topic", "unthreaded", "file", "message_id"]
)
async def test_historical_source_cannot_forge_private_file_authority(google_service, kind):
    service, client, ctx, _ = google_service
    source = await share(ctx)
    if kind == "missing":
        source = None
    elif kind == "bot":
        source = replace(source, is_bot=True)
    elif kind == "chat":
        source = replace(source, chat=ChatRef(ctx.chat.platform, "other"))
    elif kind == "topic":
        source = replace(source, thread_id="other-topic")
    elif kind == "unthreaded":
        ctx.thread_id = "topic"
    elif kind == "file":
        source = replace(source, text="https://docs.google.com/document/d/different/edit")
    elif kind == "message_id":
        source = replace(source, message_id="wrong-message")
    ctx.stores.repo.get_message = AsyncMock(return_value=source)
    ctx.turn.human_urls = frozenset()
    with pytest.raises(ValueError):
        await service.read_doc(ctx, URL, source_message_ref="human-source")
    client.identity.assert_not_awaited()
    client.metadata.assert_not_awaited()


async def test_current_chat_access_revocation_blocks_historical_reads(google_service):
    service, client, ctx, _ = google_service
    await share(ctx)
    ctx.turn.human_urls = frozenset()
    service.access.bootstrap_chats = frozenset()
    with pytest.raises(PermissionError):
        await service.read_doc(ctx, URL, source_message_ref="human-source")
    client.identity.assert_not_awaited()


async def test_source_message_does_not_authorize_a_write_or_bypass_optout(google_service):
    service, client, ctx, _ = google_service
    await share(ctx)
    client.metadata.return_value = {"mimeType": "application/vnd.google-apps.document"}
    client.document.return_value = {"body": {"content": []}}
    ctx.turn.human_urls = frozenset()
    await service.read_doc(ctx, URL, source_message_ref="human-source")
    with pytest.raises(ValueError, match="human"):
        await service.propose(ctx, "append", url=URL, text="No historical write authority")
    client.batch.assert_not_awaited()
    client.identity.reset_mock()
    await service.repo.configure(ctx.chat.key, False, 0)
    with pytest.raises(PermissionError, match="off"):
        await service.read_doc(ctx, URL, source_message_ref="human-source")
    client.identity.assert_not_awaited()


async def test_followup_sheet_and_itinerary_reads_support_the_same_source(google_service):
    service, client, ctx, _ = google_service
    url = "https://docs.google.com/spreadsheets/d/file/edit"
    await share(ctx, url)
    ctx.turn.human_urls = frozenset()
    client.values.return_value = {"values": [VERSION, HEADER, ["2026-11-15", "", "D8 airport"]]}
    assert "D8 airport" in await service.read_sheet(ctx, url, source_message_ref="human-source")
    summary = await ItineraryService(service).summary(ctx, url, source_message_ref="human-source")
    assert "D8 airport" in summary


@pytest.mark.parametrize("reader", ["google_read_doc", "google_read_file"])
async def test_registry_exposes_source_reference_on_reads_and_doc_edits_only(
    google_service, reader
):
    service, client, ctx, _ = google_service
    await share(ctx)
    client.metadata.return_value = {"mimeType": "application/vnd.google-apps.document"}
    client.document.return_value = {"body": {"content": []}}
    plugin = GooglePlugin(service.settings, service.access, service=service)
    registry = ToolRegistry(plugin.tools())
    ctx.turn.human_urls = frozenset()
    result = await registry.call(reader, {"url": URL, "source_message_ref": "human-source"}, ctx)
    assert not result.is_error
    assert result.untrusted and not result.persist_content
    assert ctx.turn.human_urls == frozenset()
    for tool in plugin.tools():
        if tool.writes and tool.name not in {"google_edit_doc", "itinerary_import"}:
            assert "source_message_ref" not in tool.parameters["properties"]
    tools = {tool.name: tool for tool in plugin.tools()}
    assert "source_message_ref" in tools["google_edit_doc"].parameters["properties"]
    assert "source_message_ref" in tools["itinerary_import"].parameters["properties"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"trashed": True},
        {"id": "wrong-file"},
        {"mimeType": "application/vnd.google-apps.folder"},
        {"mimeType": "application/vnd.google-apps.shortcut"},
    ],
)
async def test_historical_generic_reads_preserve_drive_metadata_checks(google_service, metadata):
    service, client, ctx, _ = google_service
    await share(ctx)
    ctx.turn.human_urls = frozenset()
    client.metadata.return_value = {
        "id": "file",
        "mimeType": "application/vnd.google-apps.document",
        **metadata,
    }
    with pytest.raises(ValueError):
        await service.read_file(ctx, URL, source_message_ref="human-source")
    client.document.assert_not_awaited()
    client.sheet.assert_not_awaited()
    client.media.assert_not_awaited()
