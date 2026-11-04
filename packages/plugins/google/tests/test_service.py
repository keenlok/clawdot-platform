from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from chatcore.access import AccessPolicy
from chatcore.plugin import CommandContext
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext, TurnState
from chatcore_google.client import GoogleError
from chatcore_google.repository import GoogleRepository
from chatcore_google.service import GoogleService
from chatcore_google.settings import GoogleSettings

URL = "https://docs.google.com/spreadsheets/d/file/edit#gid=7"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def created_doc(text):
    boundary = 1 + len(text.encode("utf-16-le")) // 2
    return {
        "revisionId": "rev-1",
        "tabs": [
            {
                "tabProperties": {"tabId": "created-tab", "title": "Output"},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "startIndex": 1,
                                "endIndex": boundary + 1,
                                "paragraph": {
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": boundary,
                                            "textRun": {"content": text},
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                },
            }
        ],
    }


def setup():
    stores, platform, actor = memory_stores(), FakePlatform(), user("owner")
    access = AccessPolicy([GROUP.key], [actor.ref.key], owners=[actor.ref.key])
    settings = GoogleSettings(owner_email="owner@example.com", oauth_credentials_file="unused")
    client = AsyncMock()
    client.identity.return_value = "owner-id"
    client.metadata.return_value = {
        "id": "file",
        "name": "Trip",
        "mimeType": "application/vnd.google-apps.spreadsheet",
    }
    client.sheet.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": "Day 1"}}]}
    client.values.return_value = {"values": [["Day", "Place"], ["1", "Lunch"]]}
    service = GoogleService(settings, access, client, GoogleRepository())
    ctx = ToolContext(
        GROUP,
        actor,
        False,
        platform,
        stores,
        turn=TurnState(human_urls=frozenset({URL})),
        is_admin=True,
    )
    commit = CommandContext(GROUP, actor, False, True, platform, stores)
    return service, client, ctx, commit


async def test_docx_read_uses_mime_and_preserves_native_doc_path(docx_bytes):
    service, client, ctx, _ = setup()
    client.metadata.return_value = {
        "mimeType": DOCX,
        "name": "Trip.docx",
        "size": str(len(docx_bytes)),
        "capabilities": {"canDownload": True},
    }
    client.media.return_value = docx_bytes
    result = await service.read_doc(ctx, URL)
    assert "Tokyo itinerary" in result and "main body" in result
    client.media.assert_awaited_once_with("file")
    client.document.assert_not_awaited()
    client.metadata.return_value = {
        "mimeType": "application/vnd.google-apps.document",
        "name": "Doc",
    }
    client.document.return_value = {"body": {"content": []}}
    await service.read_doc(ctx, URL)
    client.document.assert_awaited_once_with("file")


@pytest.mark.parametrize(
    "metadata",
    [
        {"mimeType": DOCX, "capabilities": {"canDownload": False}},
        {"mimeType": DOCX},
        {"mimeType": DOCX, "size": "999999999", "capabilities": {"canDownload": True}},
    ],
)
async def test_docx_download_restrictions_and_size_fail_before_media(metadata):
    service, client, ctx, _ = setup()
    client.metadata.return_value = metadata
    with pytest.raises(ValueError):
        await service.read_doc(ctx, URL)
    client.media.assert_not_awaited()


async def test_docx_provenance_optout_and_write_types_remain_closed(docx_bytes):
    service, client, ctx, _ = setup()
    client.metadata.return_value = {"mimeType": DOCX, "capabilities": {"canDownload": True}}
    client.media.return_value = docx_bytes
    ctx.turn.human_urls = frozenset()
    with pytest.raises(ValueError, match="human"):
        await service.read_doc(ctx, URL)
    client.identity.assert_not_awaited()
    ctx.turn.human_urls = frozenset({URL})
    with pytest.raises(ValueError):
        await service.propose(ctx, "append", url=URL, text="no")
    with pytest.raises(ValueError):
        await service.read_sheet(ctx, URL)
    client.media.assert_not_awaited()
    await service.repo.configure(ctx.chat.key, False, 0)
    with pytest.raises(PermissionError):
        await service.read_doc(ctx, URL)
    client.media.assert_not_awaited()


async def test_read_checks_provenance_before_authentication_and_returns_capped_data():
    service, client, ctx, _ = setup()
    ctx.turn.human_urls = frozenset()
    ctx.turn.seen_urls.add(URL)
    with pytest.raises(ValueError, match="human"):
        await service.read_sheet(ctx, URL)
    client.identity.assert_not_awaited()
    ctx.turn.human_urls = frozenset({URL})
    result = await service.read_sheet(ctx, URL)
    assert "Lunch" in result and "Day 1" in result
    assert client.values.await_args.args[1].startswith("'Day 1'!")


async def test_read_uses_retained_same_topic_output_binding_without_human_repost():
    service, client, ctx, _ = setup()
    doc_url = "https://docs.google.com/document/d/output-id/edit"
    ctx.turn.human_urls = frozenset()
    client.metadata.return_value = {
        "id": "output-id",
        "name": "Output",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canModifyContent": True},
    }
    client.document.return_value = created_doc("Existing\n")
    await service.repo.reserve_edit(
        ctx.chat.key,
        "output-action",
        ctx.user.ref.key,
        0,
        10,
        0,
        kind="edit_doc",
        source_id="source-id",
        output_binding={
            "owner_id": "owner-id",
            "owner_email": "owner@example.com",
            "thread_id": None,
            "mode": "copy",
        },
    )
    await service.repo.update_edit_result(
        ctx.chat.key, "output-action", phase="destination_created", destination_id="output-id"
    )

    result = await service.read_doc(ctx, doc_url)
    generic_result = await service.read_file(ctx, doc_url)

    assert "Existing" in result
    assert "Existing" in generic_result
    assert client.document.await_count == 2


@pytest.mark.parametrize(
    ("chat", "thread", "kind", "binding"),
    [
        ("fake:other", None, "edit_doc", True),
        (GROUP.key, "other-topic", "edit_doc", True),
        (GROUP.key, None, "browser_edit_doc", False),
    ],
)
async def test_output_read_authority_does_not_cross_chat_topic_or_browser(
    chat, thread, kind, binding
):
    service, client, ctx, _ = setup()
    doc_url = "https://docs.google.com/document/d/output-id/edit"
    ctx.turn.human_urls = frozenset()
    ctx.turn.seen_urls.add(doc_url)
    ctx.thread_id = "current-topic"
    await service.repo.reserve_edit(
        chat,
        "output-action",
        ctx.user.ref.key,
        0,
        10,
        0,
        kind=kind,
        source_id="source-id",
        output_binding=(
            {
                "owner_id": "owner-id",
                "owner_email": "owner@example.com",
                "thread_id": thread,
                "mode": "copy",
            }
            if binding
            else None
        ),
    )
    await service.repo.update_edit_result(
        chat, "output-action", phase="destination_created", destination_id="output-id"
    )

    with pytest.raises(ValueError, match="human"):
        await service.read_doc(ctx, doc_url)
    client.metadata.assert_not_awaited()


async def test_output_read_rejects_a_changed_google_owner_before_content_read():
    service, client, ctx, _ = setup()
    doc_url = "https://docs.google.com/document/d/output-id/edit"
    ctx.turn.human_urls = frozenset()
    client.metadata.return_value = {
        "id": "output-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "new-owner-id"}],
        "capabilities": {"canEdit": True, "canModifyContent": True},
    }
    await service.repo.reserve_edit(
        ctx.chat.key,
        "output-action",
        ctx.user.ref.key,
        0,
        10,
        0,
        kind="edit_doc",
        source_id="source-id",
        output_binding={
            "owner_id": "old-owner-id",
            "owner_email": "owner@example.com",
            "thread_id": None,
            "mode": "copy",
        },
    )
    await service.repo.update_edit_result(
        ctx.chat.key, "output-action", phase="destination_created", destination_id="output-id"
    )

    with pytest.raises(ValueError, match="retained output belongs to a different Google owner"):
        await service.read_doc(ctx, doc_url)
    client.document.assert_not_awaited()


async def test_output_binding_revocation_during_doc_get_stops_the_read():
    service, client, ctx, _ = setup()
    doc_url = "https://docs.google.com/document/d/output-id/edit"
    ctx.turn.human_urls = frozenset()
    client.metadata.return_value = {
        "id": "output-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canModifyContent": True},
    }
    await service.repo.reserve_edit(
        ctx.chat.key,
        "output-action",
        ctx.user.ref.key,
        0,
        10,
        0,
        kind="edit_doc",
        source_id="source-id",
        output_binding={
            "owner_id": "owner-id",
            "owner_email": "owner@example.com",
            "thread_id": None,
            "mode": "copy",
        },
    )
    await service.repo.update_edit_result(
        ctx.chat.key, "output-action", phase="destination_created", destination_id="output-id"
    )

    async def purge_during_get(_file_id):
        await service.repo.purge(ctx.chat.key)
        return created_doc("Existing\n")

    client.document.side_effect = purge_during_get
    with pytest.raises(ValueError, match="binding was removed or changed"):
        await service.read_doc(ctx, doc_url)
    client.document.assert_awaited_once_with("output-id")


async def test_output_binding_revocation_during_metadata_recheck_stops_the_read():
    service, client, ctx, _ = setup()
    doc_url = "https://docs.google.com/document/d/output-id/edit"
    ctx.turn.human_urls = frozenset()
    output_metadata = {
        "id": "output-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canModifyContent": True},
    }
    await service.repo.reserve_edit(
        ctx.chat.key,
        "output-action",
        ctx.user.ref.key,
        0,
        10,
        0,
        kind="edit_doc",
        source_id="source-id",
        output_binding={
            "owner_id": "owner-id",
            "owner_email": "owner@example.com",
            "thread_id": None,
            "mode": "copy",
        },
    )
    await service.repo.update_edit_result(
        ctx.chat.key, "output-action", phase="destination_created", destination_id="output-id"
    )
    metadata_calls = 0

    async def metadata_with_purge(_file_id):
        nonlocal metadata_calls
        metadata_calls += 1
        if metadata_calls == 2:
            await service.repo.purge(ctx.chat.key)
        return output_metadata

    client.metadata.side_effect = metadata_with_purge
    client.document.return_value = created_doc("Existing\n")

    with pytest.raises(ValueError, match="binding was removed or changed during the read"):
        await service.read_doc(ctx, doc_url)
    assert metadata_calls == 2
    client.document.assert_awaited_once_with("output-id")


async def test_append_does_nothing_before_confirmation_and_rechecks_optout():
    service, client, ctx, commit = setup()
    action = await service.propose(ctx, "append", url=URL, rows=[["=BAD()", "Lunch"]])
    client.batch.assert_not_awaited()
    await service.repo.configure(GROUP.key, False, 0)
    with pytest.raises((ValueError, PermissionError), match="off|changed"):
        await service.commit(action, commit)
    client.batch.assert_not_awaited()


async def test_create_doc_freezes_application_default_and_explicit_private_choice():
    service, _, ctx, _ = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    public = await service.propose(ctx, "create_doc", title="Public", text="Text")
    private = await service.propose(
        ctx, "create_doc", title="Private", text="Text", link_viewer=False
    )

    assert public.payload["link_viewer"] is True
    assert private.payload["link_viewer"] is False
    assert "anyone with the link can view" in public.summary.casefold()
    assert "private" in private.summary.casefold()


@pytest.mark.parametrize(
    ("configured", "explicit", "expected"),
    [
        ("private", None, "private"),
        ("viewer", None, "viewer"),
        ("editor", None, "editor"),
        ("editor", "private", "private"),
        ("private", "viewer", "viewer"),
        ("viewer", "editor", "editor"),
    ],
)
async def test_create_doc_freezes_canonical_link_access(configured, explicit, expected):
    service, _, ctx, _ = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_access=configured,
    )

    action = await service.propose(
        ctx, "create_doc", title="Output", text="Text", link_access=explicit
    )

    assert action.payload["link_access"] == expected


@pytest.mark.parametrize("invalid", ["anyone", "writer", "", 1, True])
async def test_create_doc_rejects_invalid_or_ambiguous_access(invalid):
    service, _, ctx, _ = setup()
    with pytest.raises(ValueError, match="link_access|private, viewer or editor"):
        await service.propose(
            ctx,
            "create_doc",
            title="Output",
            text="Text",
            link_access=invalid,
            link_viewer=False,
        )


@pytest.mark.parametrize("invalid", ["anyone", "writer", "", 1, True])
async def test_create_doc_rejects_invalid_access_level(invalid):
    service, _, ctx, _ = setup()
    with pytest.raises(ValueError, match="private, viewer or editor"):
        await service.propose(ctx, "create_doc", title="Output", text="Text", link_access=invalid)


@pytest.mark.parametrize("invalid", [0, 1, "true"])
async def test_create_doc_rejects_non_boolean_legacy_choice(invalid):
    service, _, ctx, _ = setup()
    with pytest.raises(ValueError, match="link_viewer must be a boolean"):
        await service.propose(ctx, "create_doc", title="Output", text="Text", link_viewer=invalid)


async def test_link_access_payload_compatibility_never_promotes_old_previews():
    from chatcore_google.link_sharing import payload_link_access

    assert payload_link_access({"link_viewer": True}) == "viewer"
    assert payload_link_access({"link_viewer": False}) == "private"
    assert payload_link_access({}) == "private"
    assert payload_link_access({"link_access": "editor"}) == "editor"
    with pytest.raises(ValueError, match="choices conflict"):
        payload_link_access({"link_access": "editor", "link_viewer": False})


async def test_legacy_viewer_helper_does_not_promote_missing_or_private_payload():
    service, client, ctx, _ = setup()
    from chatcore_google.link_sharing import publish_doc_link_viewer

    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")
    await service.repo.reserve_edit(
        ctx.chat.key,
        action.id,
        ctx.user.ref.key,
        0,
        10,
        0,
        kind="create_doc",
        source_id=None,
        output_binding={
            "owner_id": action.payload["owner"],
            "owner_email": action.payload["owner_email"],
            "thread_id": action.thread_id,
            "mode": "create",
        },
    )
    await service.repo.update_edit_result(
        ctx.chat.key, action.id, phase="destination_created", destination_id="new-doc"
    )
    await service.repo.update_edit_result(ctx.chat.key, action.id, phase="applying")
    variants = []
    missing = dict(action.payload)
    missing.pop("link_access")
    missing.pop("link_viewer")
    variants.append(missing)
    private = dict(missing, link_viewer=False)
    variants.append(private)
    for payload in variants:
        with pytest.raises(ValueError, match="no link-access grant"):
            await publish_doc_link_viewer(
                service, replace(action, payload=payload), ctx, payload, "new-doc"
            )
    client.grant_link_viewer.assert_not_awaited()


async def test_create_doc_editor_grant_is_frozen_and_verified():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_access="viewer",
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "name": "Output",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    owner_permission = {
        "id": "owner-permission",
        "type": "user",
        "role": "owner",
        "emailAddress": "owner@example.com",
    }
    client.list_permissions.side_effect = [
        {"permissions": [owner_permission]},
        {
            "permissions": [
                owner_permission,
                {
                    "id": "link-permission",
                    "type": "anyone",
                    "role": "writer",
                    "allowFileDiscovery": False,
                },
            ]
        },
    ]
    action = await service.propose(
        ctx, "create_doc", title="Output", text="Hello", link_access="editor"
    )
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_access="private",
    )

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert action.payload["link_access"] == "editor"
    assert "anyone with the link can edit" in result.casefold()
    assert journal["phase"] == "verified" and journal["destination_id"] == "created-id"
    assert journal["output_owner_id"] == "owner-id"
    client.grant_link_editor.assert_awaited_once_with("created-id")


async def test_create_doc_editor_refuses_existing_viewer_acl_without_rewriting():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com", oauth_credentials_file="unused"
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    client.list_permissions.return_value = {
        "permissions": [{"type": "anyone", "role": "reader", "allowFileDiscovery": False}]
    }
    action = await service.propose(
        ctx, "create_doc", title="Output", text="Hello", link_access="editor"
    )

    result = await service.commit(action, commit)

    assert "Partial" in result
    client.grant_link_editor.assert_not_awaited()


async def test_link_viewer_create_journals_known_id_verifies_text_then_shares():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "name": "Output",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    owner_permission = {
        "id": "owner-permission",
        "type": "user",
        "role": "owner",
        "emailAddress": "owner@example.com",
    }
    client.list_permissions.side_effect = [
        {"permissions": [owner_permission]},
        {
            "permissions": [
                owner_permission,
                {"type": "anyone", "role": "reader", "allowFileDiscovery": False},
            ]
        },
    ]
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert "anyone with the link can view" in result.casefold()
    assert journal["phase"] == "verified" and journal["destination_id"] == "created-id"
    client.grant_link_viewer.assert_awaited_once_with("created-id")


async def test_link_viewer_create_never_shares_unverified_content():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
    }
    client.document_for_edit.return_value = created_doc("Wrong\n")
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert "text_mismatch" in result and "no link-access change was attempted" in result
    assert journal["phase"] == "uncertain" and journal["destination_id"] == "created-id"
    assert journal["error_category"] == "text_mismatch"
    client.grant_link_viewer.assert_not_awaited()


async def test_link_viewer_create_rejects_matching_text_with_incomplete_snapshot():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    incomplete = created_doc("Hello\n")
    paragraph = incomplete["tabs"][0]["documentTab"]["body"]["content"][0]
    paragraph["endIndex"] += 1
    paragraph["paragraph"]["elements"].append(
        {"startIndex": 7, "endIndex": 8, "inlineObjectElement": {"inlineObjectId": "image"}}
    )
    client.document_for_edit.return_value = incomplete
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert "snapshot_error" in result
    assert journal["phase"] == "uncertain" and journal["error_category"] == "snapshot_error"
    client.grant_link_viewer.assert_not_awaited()


async def test_link_viewer_preflight_refuses_broader_acl_with_known_url():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    client.list_permissions.return_value = {
        "permissions": [{"type": "anyone", "role": "writer", "allowFileDiscovery": True}]
    }
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert "Partial" in result and "created-id" in result
    assert journal["phase"] == "partial" and journal["destination_id"] == "created-id"
    client.grant_link_viewer.assert_not_awaited()


async def test_link_viewer_journal_claim_failure_prevents_grant(monkeypatch):
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    client.list_permissions.return_value = {"permissions": []}
    update = service.repo.update_edit_result

    async def fail_sharing_stage(*args, **kwargs):
        if kwargs.get("failed_stage") == "sharing":
            raise RuntimeError("journal unavailable")
        return await update(*args, **kwargs)

    monkeypatch.setattr(service.repo, "update_edit_result", fail_sharing_stage)
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    assert "Partial" in result and "created-id" in result
    client.grant_link_viewer.assert_not_awaited()


@pytest.mark.parametrize(
    ("grant_error", "expected_result", "expected_phase"),
    [
        (
            GoogleError("Google request failed; a write may have completed."),
            "Uncertain",
            "uncertain",
        ),
        (GoogleError("Google returned an invalid response."), "Uncertain", "uncertain"),
        (GoogleError("Google response exceeds the size limit."), "Uncertain", "uncertain"),
        (GoogleError("Cannot open that file. Check sharing."), "Partial", "partial"),
    ],
)
async def test_link_viewer_post_failure_keeps_destination_and_blocks_replay(
    grant_error, expected_result, expected_phase
):
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    client.list_permissions.return_value = {"permissions": []}
    client.grant_link_viewer.side_effect = grant_error
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert expected_result in result and "created-id" in result
    assert journal["phase"] == expected_phase and journal["destination_id"] == "created-id"
    with pytest.raises(ValueError, match="handled|uncertain"):
        await service.commit(action, commit)


async def test_link_viewer_readback_mismatch_is_uncertain_with_known_url():
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    client.list_permissions.side_effect = [
        {"permissions": []},
        {"permissions": [{"type": "anyone", "role": "reader", "allowFileDiscovery": True}]},
    ]
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert "Uncertain" in result and "created-id" in result
    assert journal["phase"] == "uncertain" and journal["destination_id"] == "created-id"
    client.grant_link_viewer.assert_awaited_once_with("created-id")
    with pytest.raises(ValueError, match="handled|uncertain"):
        await service.commit(action, commit)


async def test_create_doc_stops_when_returned_id_cannot_be_journaled(monkeypatch):
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.return_value = {"documentId": "created-id"}
    update = service.repo.update_edit_result

    async def fail_destination_journal(*args, **kwargs):
        if kwargs.get("phase") == "destination_created":
            raise RuntimeError("journal unavailable")
        return await update(*args, **kwargs)

    monkeypatch.setattr(service.repo, "update_edit_result", fail_destination_journal)
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    assert "Partial" in result and "created-id" in result
    assert client.request.await_count == 1
    client.metadata.assert_not_awaited()
    client.grant_link_viewer.assert_not_awaited()


async def test_create_doc_final_journal_failure_does_not_claim_success(monkeypatch):
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
        "isAppAuthorized": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
        "capabilities": {"canEdit": True, "canShare": True},
    }
    client.document_for_edit.return_value = created_doc("Hello\n")
    client.list_permissions.side_effect = [
        {"permissions": []},
        {"permissions": [{"type": "anyone", "role": "reader", "allowFileDiscovery": False}]},
    ]
    update = service.repo.update_edit_result

    async def fail_final_journal(*args, **kwargs):
        if kwargs.get("phase") == "verified":
            raise RuntimeError("journal unavailable")
        return await update(*args, **kwargs)

    monkeypatch.setattr(service.repo, "update_edit_result", fail_final_journal)
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    assert "Uncertain" in result and "created-id" in result
    assert "Created and verified" not in result
    client.grant_link_viewer.assert_awaited_once_with("created-id")


async def test_legacy_create_doc_payload_without_choice_stays_private_after_default_flip():
    service, client, ctx, commit = setup()
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")
    legacy_payload = dict(action.payload)
    legacy_payload.pop("link_viewer")
    action = replace(action, payload=legacy_payload)
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = [{"documentId": "created-id"}, {"ok": True}]
    client.metadata.return_value = {
        "id": "created-id",
        "mimeType": "application/vnd.google-apps.document",
    }
    client.document_for_edit.return_value = created_doc("Hello\n")

    result = await service.commit(action, commit)

    assert "sharing unchanged" in result
    client.grant_link_viewer.assert_not_awaited()


@pytest.mark.parametrize(
    "write_error",
    [
        GoogleError("Google returned an invalid response."),
        GoogleError("Google response exceeds the size limit."),
    ],
)
async def test_create_doc_malformed_success_response_is_uncertain(write_error):
    service, client, ctx, commit = setup()
    service.settings = GoogleSettings(
        owner_email="owner@example.com",
        oauth_credentials_file="unused",
        new_docs_link_viewer=True,
    )
    client.request.side_effect = write_error
    action = await service.propose(ctx, "create_doc", title="Output", text="Hello")

    result = await service.commit(action, commit)

    journal = await service.repo.edit_result(ctx.chat.key, action.id)
    assert "Uncertain" in result
    assert journal["phase"] == "uncertain" and journal["destination_id"] is None
    client.grant_link_viewer.assert_not_awaited()


async def test_confirmed_append_uses_server_side_literal_append_and_cannot_replay():
    service, client, ctx, commit = setup()
    action = await service.propose(ctx, "append", url=URL, rows=[["=BAD()", "Lunch"]])
    result = await service.commit(action, commit)
    assert "appended" in result.lower()
    requests = client.batch.await_args.args[1]
    assert requests[0]["appendCells"]["sheetId"] == 7
    assert (
        requests[0]["appendCells"]["rows"][0]["values"][0]["userEnteredValue"]["stringValue"]
        == "'=BAD()"
    )
    with pytest.raises(ValueError, match="handled|uncertain"):
        await service.commit(action, commit)
    assert client.batch.await_count == 1


async def test_pending_owner_change_permission_revocation_and_wrong_chat_do_not_write():
    for change in ("owner", "permission", "chat"):
        service, client, ctx, commit = setup()
        action = await service.propose(ctx, "append", url=URL, rows=[["ok"]])
        if change == "owner":
            client.identity.return_value = "different"
        elif change == "permission":
            service.access.admin_users = frozenset()
            service.access.owners = frozenset()
        else:
            commit = replace(commit, chat=type(GROUP)("fake", "elsewhere"))
        with pytest.raises((ValueError, PermissionError)):
            await service.commit(action, commit)
        client.batch.assert_not_awaited()


async def test_optout_is_chat_scoped_and_can_be_reenabled_by_manager():
    service, client, ctx, commit = setup()
    action = await service.propose_configure(ctx, False)
    await service.commit(action, commit)
    with pytest.raises(PermissionError, match="off"):
        await service.read_sheet(ctx, URL)
    action = await service.propose_configure(ctx, True)
    await service.commit(action, commit)
    assert "Lunch" in await service.read_sheet(ctx, URL)
    assert (await service.repo.settings("fake:other")) == (True, 0)


async def test_ambiguous_tab_range_and_unknown_native_type_are_refused():
    service, client, ctx, _ = setup()
    for cell_range in ["A:A", "A1:ZZZ999999", 'IMPORTRANGE("x")', "Other!A1"]:
        with pytest.raises(ValueError):
            await service.read_sheet(ctx, URL, cell_range=cell_range)
    with pytest.raises(ValueError):
        await service.read_sheet(ctx, URL, tab="Other")
    client.metadata.return_value["mimeType"] = "text/csv"
    with pytest.raises(ValueError, match="attach|CSV"):
        await service.read_sheet(ctx, URL)


async def test_create_is_confirmed_with_frozen_rows_and_no_automatic_sharing():
    service, client, ctx, commit = setup()
    rows = [["Name", "Cost"], ["Lunch", 12]]
    client.request.return_value = {
        "spreadsheetId": "created",
        "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}],
    }
    action = await service.propose(ctx, "create_sheet", title="Trip", rows=rows)
    rows[1][0] = "changed after preview"
    assert "Lunch" in action.summary
    client.request.assert_not_awaited()
    result = await service.commit(action, commit)
    assert "/created" in result
    assert "Lunch" in str(client.batch.await_args)
    assert "permissions" not in str(client.request.await_args)


async def test_large_ledger_preview_is_attached_in_full_before_confirmation():
    service, client, ctx, _ = setup()
    rows = [[str(index), "x" * 100] for index in range(40)]
    action = await service.propose(ctx, "create_sheet", title="Ledger", rows=rows)
    assert len(action.summary) < 3500 and "attached" in action.summary
    assert len(ctx.platform.sent_files) == 1
    assert "39" in ctx.platform.sent_files[0].data.decode()
    assert len(action.payload["rows"]) == 40
    client.request.assert_not_awaited()


async def test_default_read_is_bounded_by_actual_tab_grid_not_just_configured_caps():
    service, client, ctx, _ = setup()
    client.sheet.return_value["sheets"][0]["properties"]["gridProperties"] = {
        "rowCount": 10,
        "columnCount": 26,
    }
    await service.read_sheet(ctx, URL)
    assert client.values.await_args.args[1] == "'Day 1'!A1:Z10"


async def test_append_rejects_rows_wider_than_existing_grid_before_confirmation():
    service, client, ctx, _ = setup()
    client.sheet.return_value["sheets"][0]["properties"]["gridProperties"] = {
        "rowCount": 10,
        "columnCount": 2,
    }
    with pytest.raises(ValueError, match="column|grid"):
        await service.propose(ctx, "append", url=URL, rows=[["one", "two", "three"]])
    client.batch.assert_not_awaited()


async def test_doc_append_refuses_sheet_tab_arguments_instead_of_ignoring_them():
    service, client, ctx, _ = setup()
    client.metadata.return_value["mimeType"] = "application/vnd.google-apps.document"
    with pytest.raises(ValueError, match="tab"):
        await service.propose(ctx, "append", url=URL, text="hello", tab="some tab")


async def test_lowered_limits_reject_a_pending_creation_before_any_remote_write():
    service, client, ctx, commit = setup()
    client.request.return_value = {
        "spreadsheetId": "created",
        "sheets": [{"properties": {"sheetId": 0}}],
    }
    action = await service.propose(ctx, "create_sheet", title="Trip", rows=[["a"], ["b"]])
    service.settings.max_rows = 1
    with pytest.raises(ValueError, match="row"):
        await service.commit(action, commit)
    client.request.assert_not_awaited()


async def test_lowered_text_limit_rejects_pending_doc_append_before_write():
    service, client, ctx, commit = setup()
    client.metadata.return_value["mimeType"] = "application/vnd.google-apps.document"
    action = await service.propose(ctx, "append", url=URL, text="Hello world")
    service.settings.max_chars = 5
    with pytest.raises(ValueError, match="character limit"):
        await service.commit(action, commit)
    client.request.assert_not_awaited()
