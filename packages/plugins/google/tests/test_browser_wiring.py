from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from chatcore.skills import GateState
from chatcore_google.plugin import GOOGLE_INSTRUCTIONS, GooglePlugin


def test_browser_trial_is_absent_by_default(google_service):
    base, _, _, _ = google_service
    plugin = GooglePlugin(base.settings, base.access, service=base)

    assert plugin.browser_client is None
    assert plugin.browser_edits is None
    assert "google_browser_edit_doc" not in {tool.name for tool in plugin.tools()}
    assert "google_browser_edit_doc" not in plugin.skills()[0].tools
    assert len(GOOGLE_INSTRUCTIONS) <= 800


async def test_browser_trial_wiring_is_explicit_and_chat_gated(google_service, tmp_path):
    base, _, ctx, _ = google_service
    token_file = tmp_path / "worker-token"
    token_file.write_text("T" * 48, encoding="ascii")
    token_file.chmod(0o600)
    base.settings.copy_qualified = True
    base.settings.browser_enabled = True
    base.settings.browser_url = "http://google-browser-editor:8080"
    base.settings.browser_token_file = str(token_file)
    base.settings.browser_allowed_chat_keys = ctx.chat.key
    base.settings.browser_editor_email = "editor@example.invalid"
    base.settings.browser_share_emails = json.dumps({ctx.chat.key: ["member@example.invalid"]})

    plugin = GooglePlugin(base.settings, base.access, service=base)
    try:
        tool = next(tool for tool in plugin.tools() if tool.name == "google_browser_edit_doc")
        assert tool.writes
        assert tool.parameters["additionalProperties"] is False
        assert "google_browser_edit_doc" in plugin.skills()[0].tools
        action = SimpleNamespace(summary="preview")
        plugin.browser_edits.propose_doc = AsyncMock(return_value=action)
        operation = {"kind": "bold_text", "text": "Dinner", "bold": True}
        result = await tool.handler(
            ctx,
            url="https://docs.google.com/document/d/source-file-123/edit",
            operation=operation,
        )
        assert result.pending_action is action
        plugin.browser_edits.propose_doc.assert_awaited_once_with(
            ctx,
            "https://docs.google.com/document/d/source-file-123/edit",
            operation=operation,
            tab_id=None,
            copy_title=None,
        )
        gates = plugin.tool_gates()
        assert (await gates[tool.name].scope(ctx.chat, False)).state == GateState.AVAILABLE
        assert (
            await gates[tool.name].scope(SimpleNamespace(key="fake:other-chat"), False)
        ).state == GateState.NOT_ALLOWED
    finally:
        await plugin.aclose()


def test_browser_trial_needs_copy_qualification_before_worker_client(google_service):
    base, _, ctx, _ = google_service
    base.settings.browser_enabled = True
    base.settings.browser_allowed_chat_keys = ctx.chat.key
    base.settings.browser_editor_email = "editor@example.invalid"
    base.settings.browser_url = "http://google-browser-editor:8080"
    base.settings.browser_token_file = "/missing/worker-token"

    plugin = GooglePlugin(base.settings, base.access, service=base)

    assert plugin.browser_client is None
    assert "google_browser_edit_doc" not in {tool.name for tool in plugin.tools()}


async def test_settings_disable_and_privacy_purges_clean_worker_metadata(google_service, tmp_path):
    base, _, ctx, _ = google_service
    token_file = tmp_path / "worker-token"
    token_file.write_text("T" * 48, encoding="ascii")
    token_file.chmod(0o600)
    base.settings.copy_qualified = True
    base.settings.browser_enabled = True
    base.settings.browser_url = "http://google-browser-editor:8080"
    base.settings.browser_token_file = str(token_file)
    base.settings.browser_allowed_chat_keys = ctx.chat.key
    base.settings.browser_editor_email = "editor@example.invalid"
    base.settings.browser_share_emails = "{}"
    plugin = GooglePlugin(base.settings, base.access, service=base)
    cleanup = AsyncMock()
    plugin.browser_edits.cleanup_scope = cleanup
    try:
        action = await base.propose_configure(ctx, False)
        await plugin.commit(action, ctx)
        cleanup.assert_awaited_once_with(ctx.chat.key)
        cleanup.reset_mock()

        await plugin.purge_user(ctx.chat, ctx.user.ref, ctx.stores)
        cleanup.assert_awaited_once_with(ctx.chat.key, ctx.user.ref.key)
        cleanup.reset_mock()

        await plugin.purge_chat(ctx.chat, ctx.stores)
        cleanup.assert_awaited_once_with(ctx.chat.key)
    finally:
        await plugin.aclose()
