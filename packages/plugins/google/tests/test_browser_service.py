from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from chatcore_google.browser_client import BrowserJob
from chatcore_google.browser_service import (
    BrowserTrialConfig,
    GoogleBrowserEditService,
    _bold_change_matches,
    parse_share_emails,
)
from chatcore_google.edit_snapshot import find_doc_match, flatten_doc_snapshot
from chatcore_google.plugin import GooglePlugin
from chatcore_google.service import DOC, DOCX

URL = "https://docs.google.com/document/d/source-file-123/edit"
EDITOR = "editor@example.invalid"
MEMBER = "member@example.invalid"


def _doc(
    text,
    revision="rev-1",
    *,
    tab_id="tab-source",
    title="Main",
    bold=False,
    bold_span=None,
    style_runs=None,
):
    units = len(text.encode("utf-16-le")) // 2
    if style_runs is not None:
        runs = style_runs
    elif bold_span is None:
        runs = [(text, {} if bold is None else {"bold": bold})]
    else:
        start = text.index(bold_span)
        end = start + len(bold_span)
        runs = [
            (text[:start], {"bold": False}),
            (bold_span, {"bold": True}),
            (text[end:], {"bold": False}),
        ]
    elements = []
    cursor = 1
    for run_text, style in runs:
        if not run_text:
            continue
        run_end = cursor + len(run_text.encode("utf-16-le")) // 2
        elements.append(
            {
                "startIndex": cursor,
                "endIndex": run_end,
                "textRun": {"content": run_text, "textStyle": style},
            }
        )
        cursor = run_end
    elements.append(
        {
            "startIndex": cursor,
            "endIndex": cursor + 1,
            "textRun": {"content": "\n", "textStyle": {}},
        }
    )
    return {
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": tab_id, "title": title},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "startIndex": 1,
                                "endIndex": units + 2,
                                "paragraph": {"elements": elements},
                            }
                        ]
                    }
                },
            }
        ],
    }


def _metadata(file_id="source-file-123", *, mime=DOC, editable=True):
    return {
        "id": file_id,
        "name": "Trip",
        "mimeType": mime,
        "capabilities": {
            "canEdit": editable,
            "canModifyContent": editable,
            "canCopy": True,
            "canDownload": True,
        },
        "isAppAuthorized": True,
    }


def _job(phase, *, job_id, destination_file_id="new-copy-file-123"):
    now = datetime.now(UTC)
    return BrowserJob(job_id, phase, destination_file_id, now, now)


def _config(chat_key, *, editor=EDITOR, members=(MEMBER,), enabled=True):
    return BrowserTrialConfig(
        enabled=enabled,
        allowed_chat_keys=frozenset({chat_key}),
        editor_email=editor,
        share_emails={chat_key: tuple(members)},
    )


def _service(google_service, worker=None, *, config=None):
    base, client, ctx, commit = google_service
    ctx.turn.human_urls = frozenset(
        {
            URL,
            "https://drive.google.com/file/d/source-file-123/view",
        }
    )
    base.settings.copy_qualified = True
    client.identity.return_value = "owner-permission-id"
    client.metadata.return_value = _metadata()
    client.document_for_edit.return_value = _doc("Dinner at 7pm")
    plugin = GooglePlugin(base.settings, base.access, service=base)
    service = GoogleBrowserEditService(
        plugin, worker or AsyncMock(), config or _config(ctx.chat.key)
    )
    return service, client, ctx, commit


def test_share_email_map_is_strict_and_bounded():
    assert parse_share_emails('{"tg:42":["a@example.invalid"]}') == {
        "tg:42": ("a@example.invalid",)
    }
    for invalid in (
        "[]",
        '{"tg:42":"a@example.invalid"}',
        '{"tg:42":["bad address"]}',
        '{"tg:42":["a@example.invalid","A@example.invalid"]}',
        '{"tg:42":[' + ",".join(f'"u{i}@example.invalid"' for i in range(21)) + "]}",
    ):
        with pytest.raises(ValueError):
            parse_share_emails(invalid)


async def test_proposal_is_copy_only_and_never_calls_copy_share_or_worker(google_service):
    service, client, ctx, _ = _service(google_service)
    action = await service.propose_doc(
        ctx,
        URL,
        operation={"kind": "replace_text", "find": "Dinner at 7pm", "replacement": "Dinner at 8pm"},
    )
    assert action.kind == "browser_edit_doc"
    assert action.payload["destination"]["mode"] == "copy"
    assert action.payload["browser"]["operation"] == {
        "kind": "replace_text",
        "find": "Dinner at 7pm",
        "replacement": "Dinner at 8pm",
    }
    assert action.payload["browser"]["grantees"] == [EDITOR, MEMBER]
    assert "new Google Doc" in action.summary
    assert EDITOR in action.summary and MEMBER in action.summary
    client.copy_file.assert_not_awaited()
    client.grant_writer.assert_not_awaited()
    client.edit_document.assert_not_awaited()


async def test_proposal_refuses_outside_trial_chat_and_owner_as_editor(google_service):
    service, _, ctx, _ = _service(
        google_service,
        config=_config("other-chat"),
    )
    with pytest.raises(PermissionError):
        await service.propose_doc(
            ctx,
            URL,
            operation={"kind": "bold_text", "text": "Dinner at 7pm", "bold": True},
        )
    base, _, ctx, _ = google_service
    with pytest.raises(ValueError, match="editor account"):
        plugin = GooglePlugin(base.settings, base.access, service=base)
        GoogleBrowserEditService(
            plugin,
            AsyncMock(),
            _config(ctx.chat.key, editor="owner@example.com"),
        )


async def test_docx_proposal_freezes_hash_and_targets_new_native_doc(google_service, docx_bytes):
    from hashlib import sha256

    service, client, ctx, _ = _service(google_service)
    client.metadata.return_value = _metadata(mime=DOCX)
    client.media.return_value = docx_bytes
    action = await service.propose_doc(
        ctx,
        "https://drive.google.com/file/d/source-file-123/view",
        operation={"kind": "replace_text", "find": "Tokyo itinerary", "replacement": "Tokyo plan"},
    )
    assert action.kind == "browser_edit_doc"
    assert action.payload["source"]["sha256"] == sha256(docx_bytes).hexdigest()
    assert action.payload["destination"]["mode"] == "docx_import"
    assert action.payload["destination"]["mime"] == DOC
    assert action.payload["precondition"]["tab_id"] is None
    client.import_docx.assert_not_awaited()


async def test_docx_import_edit_uses_complete_native_tab_and_leaves_source_untouched(
    google_service, docx_bytes
):
    from hashlib import sha256

    worker = AsyncMock()
    service, client, ctx, commit = _service(google_service, worker)
    source_meta = _metadata(mime=DOCX)
    source_meta["isAppAuthorized"] = False
    client.metadata.return_value = source_meta
    client.media.return_value = docx_bytes
    action = await service.propose_doc(
        ctx,
        "https://drive.google.com/file/d/source-file-123/view",
        operation={
            "kind": "replace_text",
            "find": "Tokyo itinerary",
            "replacement": "Tokyo plan",
        },
    )
    assert action.payload["source"]["sha256"] == sha256(docx_bytes).hexdigest()
    destination_meta = _metadata("new-copy-file-123")
    client.metadata.side_effect = lambda file_id: (
        source_meta if file_id == "source-file-123" else destination_meta
    )
    client.document_for_edit.side_effect = [
        _doc("Converted: Tokyo itinerary", "copy-rev", tab_id="copy-tab"),
        _doc("Converted: Tokyo itinerary", "copy-rev", tab_id="copy-tab"),
        _doc("Converted: Tokyo itinerary", "copy-rev", tab_id="copy-tab"),
        _doc("Converted: Tokyo plan", "after-rev", tab_id="copy-tab"),
    ]
    client.import_docx.return_value = {"id": "new-copy-file-123", "mimeType": DOC}
    client.grant_writer.return_value = {
        "id": "p-editor",
        "type": "user",
        "role": "writer",
        "emailAddress": EDITOR,
    }
    client.list_permissions.return_value = {
        "permissions": [
            {"type": "user", "role": "writer", "emailAddress": EDITOR},
            {"type": "user", "role": "writer", "emailAddress": MEMBER},
        ]
    }
    worker.create_job.return_value = _job("accepted", job_id=action.id)
    worker.job.return_value = _job("awaiting_verification", job_id=action.id)
    worker.verify.return_value = _job("verified", job_id=action.id)

    result = await service.commit(action, commit)

    assert "verified" in result.casefold()
    client.import_docx.assert_awaited_once()
    client.copy_file.assert_not_awaited()
    client.edit_document.assert_not_awaited()
    assert all(call.args[0] == "new-copy-file-123" for call in client.grant_writer.await_args_list)
    assert worker.create_job.await_args.args[0]["destination_tab_id"] == "copy-tab"


async def test_commit_shares_only_new_copy_then_verifies_complete_tab_before_ack(
    google_service,
):
    worker = AsyncMock()
    service, client, ctx, commit = _service(google_service, worker)
    action = await service.propose_doc(
        ctx,
        URL,
        operation={"kind": "replace_text", "find": "Dinner at 7pm", "replacement": "Dinner at 8pm"},
    )
    destination_metadata = _metadata("new-copy-file-123")
    client.metadata.side_effect = lambda file_id: (
        destination_metadata if file_id == "new-copy-file-123" else _metadata()
    )
    # Commit rechecks, API guard, copy validation, pre-dispatch check and API read-back.
    client.document_for_edit.side_effect = [
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 8pm", "after-rev", tab_id="copy-tab"),
    ]
    client.copy_file.return_value = {
        "id": "new-copy-file-123",
        "mimeType": DOC,
        "name": "Trip — edited",
    }
    client.grant_writer.return_value = {
        "id": "p-editor",
        "type": "user",
        "role": "writer",
        "emailAddress": EDITOR,
    }
    client.list_permissions.return_value = {
        "permissions": [
            {"type": "user", "role": "writer", "emailAddress": EDITOR},
            {"type": "user", "role": "writer", "emailAddress": MEMBER},
        ]
    }
    worker.create_job.return_value = _job("accepted", job_id=action.id)
    worker.job.side_effect = [
        _job("awaiting_verification", job_id=action.id),
    ]
    worker.verify.return_value = _job("verified", job_id=action.id)

    result = await service.commit(action, commit)

    assert "verified" in result.casefold()
    client.copy_file.assert_awaited_once_with(
        "source-file-123", action.payload["destination"]["title"]
    )
    grants = [call.args for call in client.grant_writer.await_args_list]
    assert grants == [("new-copy-file-123", EDITOR), ("new-copy-file-123", MEMBER)]
    assert all(call.args[0] != "source-file-123" for call in client.grant_writer.await_args_list)
    request = worker.create_job.await_args.args[0]
    assert request["destination_file_id"] == "new-copy-file-123"
    assert request["destination_tab_id"] == "copy-tab"
    assert request["operation"] == {
        "kind": "replace_text",
        "find": "Dinner at 7pm",
        "replacement": "Dinner at 8pm",
    }
    worker.verify.assert_awaited_once_with(action.id, "verified")
    client.edit_document.assert_not_awaited()
    client.document_for_edit.assert_awaited()


async def test_bold_edit_verifies_full_tab_and_utf16_style_range(google_service):
    worker = AsyncMock()
    service, client, ctx, commit = _service(google_service, worker)
    text = "A😀 target Z"
    client.document_for_edit.return_value = _doc(text)
    action = await service.propose_doc(
        ctx,
        URL,
        operation={"kind": "bold_text", "text": "target", "bold": True},
    )
    client.metadata.side_effect = lambda file_id: _metadata(file_id)
    client.document_for_edit.side_effect = [
        _doc(text),
        _doc(text),
        _doc(text, "copy-rev", tab_id="copy-tab"),
        _doc(text, "copy-rev", tab_id="copy-tab"),
        _doc(text, "copy-rev", tab_id="copy-tab"),
        _doc(text, "after-rev", tab_id="copy-tab", bold_span="target"),
    ]
    client.copy_file.return_value = {"id": "new-copy-file-123"}
    client.grant_writer.return_value = {"id": "p", "type": "user", "role": "writer"}
    client.list_permissions.return_value = {
        "permissions": [
            {"type": "user", "role": "writer", "emailAddress": EDITOR},
            {"type": "user", "role": "writer", "emailAddress": MEMBER},
        ]
    }
    worker.create_job.return_value = _job("accepted", job_id=action.id)
    worker.job.return_value = _job("awaiting_verification", job_id=action.id)
    worker.verify.return_value = _job("verified", job_id=action.id)

    result = await service.commit(action, commit)

    assert "verified" in result.casefold()
    request = worker.create_job.await_args.args[0]
    assert request["operation"] == {"kind": "bold_text", "text": "target", "bold": True}
    assert request["destination_tab_id"] == "copy-tab"
    client.edit_document.assert_not_awaited()


async def test_bold_change_outside_requested_span_is_uncertain(google_service):
    worker = AsyncMock()
    service, client, ctx, commit = _service(google_service, worker)
    text = "A😀 target Z"
    client.document_for_edit.return_value = _doc(text)
    action = await service.propose_doc(
        ctx,
        URL,
        operation={"kind": "bold_text", "text": "target", "bold": True},
    )
    client.metadata.side_effect = lambda file_id: _metadata(file_id)
    client.document_for_edit.side_effect = [
        _doc(text),
        _doc(text),
        _doc(text, "copy-rev", tab_id="copy-tab"),
        _doc(text, "copy-rev", tab_id="copy-tab"),
        _doc(text, "copy-rev", tab_id="copy-tab"),
        _doc(text, "after-rev", tab_id="copy-tab", bold=True),
    ]
    client.copy_file.return_value = {"id": "new-copy-file-123"}
    client.grant_writer.return_value = {"id": "p", "type": "user", "role": "writer"}
    client.list_permissions.return_value = {
        "permissions": [
            {"type": "user", "role": "writer", "emailAddress": EDITOR},
            {"type": "user", "role": "writer", "emailAddress": MEMBER},
        ]
    }
    worker.create_job.return_value = _job("accepted", job_id=action.id)
    worker.job.return_value = _job("awaiting_verification", job_id=action.id)
    worker.verify.return_value = _job("uncertain", job_id=action.id)

    result = await service.commit(action, commit)

    assert "uncertain" in result.casefold()
    worker.verify.assert_awaited_once_with(action.id, "uncertain")


def test_bold_verification_fails_closed_for_inherited_target_and_outside_defaults():
    text = "A😀 target Z"
    before_doc = _doc(text, bold=None)
    before = flatten_doc_snapshot(before_doc, tab_id="tab-source", max_chars=1000)
    match = find_doc_match(before, "target")

    inherited_noop = flatten_doc_snapshot(
        _doc(text, "after", bold=None), tab_id="tab-source", max_chars=1000
    )
    assert not _bold_change_matches(
        before, inherited_noop, match.start_index, match.end_index, False
    )

    outside_changed = flatten_doc_snapshot(
        _doc(
            text,
            "after",
            style_runs=[
                ("A😀 ", {"bold": False}),
                ("target", {"bold": False}),
                (" Z", {}),
            ],
        ),
        tab_id="tab-source",
        max_chars=1000,
    )
    assert not _bold_change_matches(
        before, outside_changed, match.start_index, match.end_index, False
    )


async def test_editor_share_failure_keeps_known_copy_and_never_dispatches(
    google_service,
):
    worker = AsyncMock()
    service, client, ctx, commit = _service(google_service, worker)
    action = await service.propose_doc(
        ctx,
        URL,
        operation={"kind": "bold_text", "text": "Dinner at 7pm", "bold": True},
    )
    client.metadata.side_effect = lambda file_id: _metadata(file_id)
    client.document_for_edit.side_effect = [
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
    ]
    client.copy_file.return_value = {"id": "new-copy-file-123"}
    client.grant_writer.side_effect = RuntimeError("sensitive Google response body")

    result = await service.commit(action, commit)

    assert "new-copy-file-123" in result
    assert "sensitive Google response body" not in result
    worker.create_job.assert_not_awaited()


async def test_member_share_failure_is_reported_but_editor_writer_grant_is_required(
    google_service,
):
    worker = AsyncMock()
    service, client, ctx, commit = _service(google_service, worker)
    action = await service.propose_doc(
        ctx,
        URL,
        operation={"kind": "replace_text", "find": "Dinner at 7pm", "replacement": "Dinner at 8pm"},
    )
    client.metadata.side_effect = lambda file_id: _metadata(file_id)
    client.document_for_edit.side_effect = [
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 8pm", "after-rev", tab_id="copy-tab"),
    ]
    client.copy_file.return_value = {"id": "new-copy-file-123"}
    client.grant_writer.side_effect = [
        {"id": "p-editor", "type": "user", "role": "writer", "emailAddress": EDITOR},
        RuntimeError("member grant denied"),
    ]
    client.list_permissions.return_value = {
        "permissions": [{"type": "user", "role": "writer", "emailAddress": EDITOR}]
    }
    worker.create_job.return_value = _job("accepted", job_id=action.id)
    worker.job.return_value = _job("awaiting_verification", job_id=action.id)
    worker.verify.return_value = _job("verified", job_id=action.id)

    result = await service.commit(action, commit)

    assert "member" in result.casefold()
    assert "new-copy-file-123" in result
    assert worker.create_job.await_count == 1


async def test_worker_success_with_api_readback_mismatch_is_acknowledged_uncertain(
    google_service,
):
    worker = AsyncMock()
    service, client, ctx, commit = _service(google_service, worker)
    action = await service.propose_doc(
        ctx,
        URL,
        operation={"kind": "bold_text", "text": "Dinner at 7pm", "bold": True},
    )
    client.metadata.side_effect = lambda file_id: _metadata(file_id)
    client.document_for_edit.side_effect = [
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 7pm", "after-rev", tab_id="copy-tab"),
    ]
    client.copy_file.return_value = {"id": "new-copy-file-123"}
    client.grant_writer.return_value = {"id": "p", "type": "user", "role": "writer"}
    client.list_permissions.return_value = {
        "permissions": [{"type": "user", "role": "writer", "emailAddress": EDITOR}]
    }
    worker.create_job.return_value = _job("accepted", job_id=action.id)
    worker.job.return_value = _job("awaiting_verification", job_id=action.id)
    worker.verify.return_value = _job("uncertain", job_id=action.id)

    result = await service.commit(action, commit)

    assert "uncertain" in result.casefold()
    worker.verify.assert_awaited_once_with(action.id, "uncertain")


async def test_post_response_loss_queries_job_without_reposting(google_service):
    worker = AsyncMock()
    service, client, ctx, commit = _service(google_service, worker)
    action = await service.propose_doc(
        ctx,
        URL,
        operation={"kind": "replace_text", "find": "Dinner at 7pm", "replacement": "Dinner at 8pm"},
    )
    client.metadata.side_effect = lambda file_id: _metadata(file_id)
    client.document_for_edit.side_effect = [
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
    ]
    client.copy_file.return_value = {"id": "new-copy-file-123"}
    client.grant_writer.return_value = {"id": "p", "type": "user", "role": "writer"}
    client.list_permissions.return_value = {
        "permissions": [{"type": "user", "role": "writer", "emailAddress": EDITOR}]
    }
    from chatcore_google.browser_client import BrowserClientError

    worker.create_job.side_effect = BrowserClientError("worker_transport_error", uncertain=True)
    worker.job.side_effect = ValueError("not found")

    result = await service.commit(action, commit)

    assert "uncertain" in result.casefold()
    worker.create_job.assert_awaited_once()
    worker.job.assert_awaited_once_with(action.id)


async def test_shared_copy_change_after_grant_blocks_browser_dispatch(google_service):
    worker = AsyncMock()
    service, client, ctx, commit = _service(google_service, worker)
    action = await service.propose_doc(
        ctx,
        URL,
        operation={"kind": "replace_text", "find": "Dinner at 7pm", "replacement": "Dinner at 8pm"},
    )
    client.metadata.side_effect = lambda file_id: _metadata(file_id)
    client.document_for_edit.side_effect = [
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Changed by another writer", "changed-rev", tab_id="copy-tab"),
    ]
    client.copy_file.return_value = {"id": "new-copy-file-123"}
    client.grant_writer.return_value = {"id": "p", "type": "user", "role": "writer"}
    client.list_permissions.return_value = {
        "permissions": [{"type": "user", "role": "writer", "emailAddress": EDITOR}]
    }

    result = await service.commit(action, commit)

    assert "copy changed" in result.casefold()
    assert "new-copy-file-123" in result
    worker.create_job.assert_not_awaited()


async def test_authorization_loss_after_worker_completion_is_uncertain_and_not_acked(
    google_service,
):
    worker = AsyncMock()
    service, client, ctx, commit = _service(google_service, worker)
    action = await service.propose_doc(
        ctx,
        URL,
        operation={"kind": "replace_text", "find": "Dinner at 7pm", "replacement": "Dinner at 8pm"},
    )
    client.metadata.side_effect = lambda file_id: _metadata(file_id)
    client.document_for_edit.side_effect = [
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
        _doc("Dinner at 7pm", "copy-rev", tab_id="copy-tab"),
    ]
    client.copy_file.return_value = {"id": "new-copy-file-123"}
    client.grant_writer.return_value = {"id": "p", "type": "user", "role": "writer"}
    client.list_permissions.return_value = {
        "permissions": [{"type": "user", "role": "writer", "emailAddress": EDITOR}]
    }
    worker.create_job.return_value = _job("accepted", job_id=action.id)

    async def completed(_job_id):
        service.google.verify_commit = AsyncMock(side_effect=PermissionError("opted out"))
        return _job("awaiting_verification", job_id=action.id)

    worker.job.side_effect = completed

    result = await service.commit(action, commit)

    assert "uncertain" in result.casefold()
    assert "new-copy-file-123" in result
    worker.cancel.assert_awaited_once_with(action.id)
    worker.verify.assert_not_awaited()


async def test_privacy_cleanup_uses_worker_state_not_api_journal_phase(google_service):
    worker = AsyncMock()
    service, client, ctx, _ = _service(google_service, worker)
    service.google.repo.browser_edit_results = AsyncMock(
        return_value=[
            {"action_id": "opaque-job-000000000001", "phase": "applying"},
            {"action_id": "opaque-job-000000000002", "phase": "partial"},
        ]
    )
    worker.cancel.return_value = _job("uncertain", job_id="opaque-job-000000000001")
    worker.job.side_effect = [
        _job("running", job_id="opaque-job-000000000001"),
        _job("verified", job_id="opaque-job-000000000002"),
    ]
    await service.cleanup_scope(ctx.chat.key)
    worker.cancel.assert_awaited_once_with("opaque-job-000000000001")
    assert worker.delete.await_count == 2
