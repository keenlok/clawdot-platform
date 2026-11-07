"""Confirmed saved-file authority and compatibility; no real Drive or Telegram calls."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from chatcore.tools.registry import TurnState
from chatcore_google.service import DOC, DOCX, SHEET
from chatcore_google.trip_files import TripFilesService

URL = "https://drive.google.com/file/d/file/view"


@pytest.fixture
def trip_files(google_service):
    google, client, ctx, commit = google_service
    resolver = AsyncMock(
        return_value={
            "chat_key": ctx.chat.key,
            "trip_id": "existing-trip",
            "trip_name": "Sample & Example trip",
            "status": "planned",
        }
    )
    client.metadata.return_value = {
        "id": "file",
        "name": "Itinerary.docx",
        "mimeType": DOCX,
        "capabilities": {"canDownload": True},
    }
    return TripFilesService(google, resolver), resolver, client, ctx, commit


async def test_attach_preserves_trip_and_reads_later_without_posted_link(trip_files, docx_bytes):
    files, resolver, client, ctx, commit = trip_files
    action = await files.propose_attach(ctx, URL, "Sample & Example trip", "itinerary")
    assert "Sample & Example trip" in action.summary
    assert await files.google.repo.trip_files(ctx.chat.key, "existing-trip") == []
    client.media.assert_not_awaited()
    assert "attached" in (await files.commit(action, commit)).lower()
    client.media.return_value = docx_bytes
    later = replace(ctx, turn=TurnState())
    text = await files.read(later, "Sample & Example trip")
    assert "Tokyo itinerary" in text
    assert resolver.await_args.args[1] == "Sample & Example trip"
    assert (await files.google.repo.trip_files(ctx.chat.key, "existing-trip"))[0][
        "trip_id"
    ] == "existing-trip"
    client.request.assert_not_awaited()


async def test_any_binary_attaches_without_download_and_replacement_keeps_reference(trip_files):
    files, _, client, ctx, commit = trip_files
    client.metadata.return_value["mimeType"] = "image/png"
    a = await files.propose_attach(ctx, URL, "China", "itinerary")
    assert "not supported" in a.summary.lower()
    await files.commit(a, commit)
    assert "not supported" in (await files.read(replace(ctx, turn=TurnState()), "China")).lower()
    client.media.assert_not_awaited()
    other = replace(
        ctx, turn=TurnState(human_urls=frozenset({"https://drive.google.com/file/d/other/view"}))
    )
    client.metadata.return_value = {"id": "other", "name": "Other", "mimeType": DOC}
    a = await files.propose_attach(
        other, "https://drive.google.com/file/d/other/view", "China", "itinerary"
    )
    assert "reference" in a.summary.lower()
    await files.commit(a, commit)
    rows = await files.google.repo.trip_files(ctx.chat.key, "existing-trip")
    assert {r["file_id"]: r["role"] for r in rows} == {"file": "reference", "other": "itinerary"}


async def test_stale_confirmation_and_cross_trip_move_are_rejected(trip_files):
    files, resolver, _, ctx, commit = trip_files
    a = await files.propose_attach(ctx, URL, "China", "itinerary")
    stale = await files.propose_attach(ctx, URL, "China", "reference")
    await files.commit(a, commit)
    with pytest.raises(ValueError, match="changed|fresh"):
        await files.commit(stale, commit)
    resolver.return_value = {
        "chat_key": ctx.chat.key,
        "trip_id": "another-trip",
        "trip_name": "Other",
    }
    with pytest.raises(ValueError, match="another trip|other trip"):
        await files.propose_attach(ctx, URL, "Other", "reference")


async def test_remove_revokes_saved_authority_and_sheet_comparison_link(trip_files):
    files, _, client, ctx, commit = trip_files
    client.metadata.return_value["mimeType"] = SHEET
    await files.commit(await files.propose_attach(ctx, URL, "China", "itinerary"), commit)
    assert await files.google.repo.linked_trip(ctx.chat.key, "file") == "existing-trip"
    remove = await files.propose_remove(ctx, "China", "file")
    assert await files.google.repo.linked_trip(ctx.chat.key, "file") == "existing-trip"
    await files.commit(remove, commit)
    assert await files.google.repo.linked_trip(ctx.chat.key, "file") is None
    with pytest.raises(ValueError, match="itinerary|attached"):
        await files.read(replace(ctx, turn=TurnState()), "China", "file")
    client.request.assert_not_awaited()


@pytest.mark.parametrize("change", ["owner", "mime", "trashed", "scope", "off"])
async def test_confirm_rechecks_access_owner_type_scope_and_optout(trip_files, change):
    files, resolver, client, ctx, commit = trip_files
    action = await files.propose_attach(ctx, URL, "China", "itinerary")
    if change == "owner":
        client.identity.return_value = "new-owner"
    elif change == "mime":
        client.metadata.return_value["mimeType"] = SHEET
    elif change == "trashed":
        client.metadata.return_value["trashed"] = True
    elif change == "scope":
        resolver.return_value["chat_key"] = "fake:other"
    else:
        await files.google.repo.configure(ctx.chat.key, False, 0)
    with pytest.raises((ValueError, PermissionError)):
        await files.commit(action, commit)
    assert await files.google.repo.trip_files(ctx.chat.key, "existing-trip") == []


async def test_saved_read_rechecks_owner_and_cross_chat_cannot_inherit(trip_files):
    files, resolver, client, ctx, commit = trip_files
    await files.commit(await files.propose_attach(ctx, URL, "China", "itinerary"), commit)
    client.identity.return_value = "another-owner"
    with pytest.raises(ValueError, match="owner|identity"):
        await files.read(replace(ctx, turn=TurnState()), "China")
    client.media.assert_not_awaited()
    resolver.return_value["chat_key"] = "fake:other"
    with pytest.raises((ValueError, PermissionError), match="scope|group|chat"):
        await files.list(ctx, "China")


async def test_trip_edit_uses_saved_primary_and_freezes_tab_without_accepting_a_url(
    trip_files,
):
    from chatcore_google.edit_service import GoogleEditService
    from chatcore_google.service import DOC

    files, _, client, ctx, _ = trip_files
    files.google.edits = GoogleEditService(files.google)
    files.google.trip_files = files
    repo = files.google.repo
    client.metadata.return_value = {
        "id": "file",
        "name": "Working itinerary",
        "mimeType": DOC,
        "capabilities": {"canEdit": True, "canModifyContent": True},
    }
    await repo.apply_file(
        ctx.chat.key,
        "existing-trip",
        "file",
        {
            "url": "https://docs.google.com/document/d/file/edit?tab=tab-1",
            "name": "Working itinerary",
            "mime": DOC,
            "role": "itinerary",
            "owner": "owner-id",
            "requester": ctx.user.ref.key,
        },
        repo.file_state([]),
    )
    client.document_for_edit.return_value = {
        "revisionId": "rev-1",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Guangzhou"},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "startIndex": 1,
                                "endIndex": 19,
                                "paragraph": {
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": 18,
                                            "textRun": {"content": "Add this place\n"},
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
    action = await files.propose_edit(ctx, "Sample & Example trip", "Add this place", "Add bar")
    assert action.payload["source"]["file_id"] == "file"
    assert action.payload["source"]["url"].endswith("?tab=tab-1")
    assert action.payload["destination"]["mode"] == "original"
    assert action.payload["trip_source"]["row"]["role"] == "itinerary"
    client.media.assert_not_awaited()


async def test_stale_trip_reference_requires_a_current_human_url(trip_files):
    from dataclasses import replace

    files, _, _, ctx, _ = trip_files
    repo = files.google.repo
    rows = []
    for file_id, role in (("old", "reference"), ("current", "itinerary")):
        data = {
            "url": f"https://docs.google.com/document/d/{file_id}/edit",
            "name": file_id,
            "mime": DOC,
            "role": role,
            "owner": "owner-id",
            "requester": ctx.user.ref.key,
        }
        await repo.apply_file(
            ctx.chat.key,
            "existing-trip",
            file_id,
            data,
            repo.file_state(rows),
        )
        rows = await repo.trip_files(ctx.chat.key, "existing-trip")
    with pytest.raises(ValueError, match="current itinerary"):
        await files.guard_doc_target(ctx, "https://docs.google.com/document/d/old/edit")
    explicit = replace(
        ctx,
        turn=replace(
            ctx.turn,
            human_urls=frozenset(
                {"https://example.com/trip", "https://docs.google.com/document/d/old/edit"}
            ),
        ),
    )
    proof = await files.guard_doc_target(explicit, "https://docs.google.com/document/d/old/edit")
    assert proof["current_named"] is True


async def test_attachment_historical_source_is_revalidated_at_confirmation(trip_files):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from chatcore.testing import message, user
    from chatcore_google.service import DOC

    files, _, client, ctx, commit = trip_files
    incoming = replace(
        message("Use this itinerary: " + URL, sender=user("another-member")),
        chat=ctx.chat,
        message_id="source-message",
    )
    await ctx.stores.repo.log_message(incoming)
    client.metadata.return_value = {
        "id": "file",
        "name": "Itinerary",
        "mimeType": DOC,
    }
    action = await files.propose_attach(
        ctx,
        URL,
        "Sample & Example trip",
        "itinerary",
        source_message_ref="source-message",
    )
    assert action.payload["source_message_ref"] == "source-message"
    ctx.stores.repo.get_message = AsyncMock(return_value=None)
    with pytest.raises(ValueError, match="existing human message"):
        await files.commit(action, commit)
    assert await files.google.repo.trip_files(ctx.chat.key, "existing-trip") == []


async def test_trip_edit_confirm_updates_the_saved_native_doc_once(trip_files):
    from dataclasses import replace

    from chatcore.testing import user
    from chatcore_google.edit_service import GoogleEditService
    from chatcore_google.service import DOC

    files, _, client, ctx, commit = trip_files
    writer = user("authorized-writer")
    files.google.settings.write_user_ids = writer.ref.key
    ctx = replace(ctx, user=writer, is_admin=False)
    commit = replace(commit, user=writer, is_admin=False)
    files.google.edits = GoogleEditService(files.google)
    files.google.trip_files = files
    repo = files.google.repo
    client.metadata.return_value = {
        "id": "file",
        "name": "Working itinerary",
        "mimeType": DOC,
        "capabilities": {"canEdit": True, "canModifyContent": True},
    }
    await repo.apply_file(
        ctx.chat.key,
        "existing-trip",
        "file",
        {
            "url": "https://docs.google.com/document/d/file/edit?tab=tab-1",
            "name": "Working itinerary",
            "mime": DOC,
            "role": "itinerary",
            "owner": "owner-id",
            "requester": ctx.user.ref.key,
        },
        repo.file_state([]),
    )

    def document(text, revision):
        return {
            "revisionId": revision,
            "tabs": [
                {
                    "tabProperties": {"tabId": "tab-1", "title": "Guangzhou"},
                    "documentTab": {
                        "namedStyles": {
                            "styles": [
                                {
                                    "namedStyleType": "NORMAL_TEXT",
                                    "textStyle": {
                                        "bold": False,
                                        "italic": False,
                                        "underline": False,
                                        "strikethrough": False,
                                        "smallCaps": False,
                                        "backgroundColor": {},
                                        "foregroundColor": {
                                            "color": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}
                                        },
                                        "baselineOffset": "NONE",
                                        "fontSize": {"magnitude": 11, "unit": "PT"},
                                        "weightedFontFamily": {"fontFamily": "Arial"},
                                    },
                                }
                            ]
                        },
                        "body": {
                            "content": [
                                {
                                    "startIndex": 1,
                                    "endIndex": len(text) + 2,
                                    "paragraph": {
                                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                        "elements": [
                                            {
                                                "startIndex": 1,
                                                "endIndex": len(text) + 1,
                                                "textRun": {"content": text, "textStyle": {}},
                                            },
                                            {
                                                "startIndex": len(text) + 1,
                                                "endIndex": len(text) + 2,
                                                "textRun": {"content": "\n", "textStyle": {}},
                                            },
                                        ],
                                    },
                                }
                            ]
                        },
                    },
                }
            ],
        }

    client.document_for_edit.side_effect = [
        document("Hotel confirmed; add this place", "rev-1"),
        document("Hotel confirmed; add this place", "rev-1"),
        document("Hotel confirmed; add bar", "rev-2"),
    ]
    action = await files.propose_edit(ctx, "Sample & Example trip", "add this place", "add bar")
    result = await files.google.edits.commit(action, commit)
    assert "verified" in result.lower()
    assert client.edit_document.await_count == 1
    assert client.edit_document.await_args.args[0] == "file"
    client.copy_file.assert_not_awaited()
    client.media.assert_not_awaited()
    with pytest.raises(ValueError, match="already handled|unresolved"):
        await files.google.edits.commit(action, commit)
    assert client.edit_document.await_count == 1


async def test_trip_edit_auto_conversion_is_previewed_as_new_current_primary(
    trip_files, docx_bytes
):
    from chatcore_google.edit_service import GoogleEditService
    from chatcore_google.service import DOCX

    files, _, client, ctx, _ = trip_files
    files.google.edits = GoogleEditService(files.google)
    files.google.trip_files = files
    files.google.settings.copy_qualified = True
    repo = files.google.repo
    client.metadata.return_value = {
        "id": "file",
        "name": "Old itinerary.docx",
        "mimeType": DOCX,
        "capabilities": {"canDownload": True, "canCopy": True},
        "size": len(docx_bytes),
    }
    client.media.return_value = docx_bytes
    await repo.apply_file(
        ctx.chat.key,
        "existing-trip",
        "file",
        {
            "url": "https://drive.google.com/file/d/file/view",
            "name": "Old itinerary.docx",
            "mime": DOCX,
            "role": "itinerary",
            "owner": "owner-id",
            "requester": ctx.user.ref.key,
        },
        repo.file_state([]),
    )
    action = await files.propose_edit(
        ctx, "Sample & Example trip", "Tokyo itinerary", "Tokyo plans"
    )
    assert action.payload["destination"]["mode"] == "docx_import"
    assert action.payload["trip_source"]["make_current"] is True
    assert "advance to this copy only after verified edit" in action.summary
    client.copy_file.assert_not_awaited()
    client.media.assert_awaited_once_with("file")


async def test_trip_edit_association_change_after_preview_stops_before_content_read(trip_files):
    from chatcore_google.edit_service import GoogleEditService
    from chatcore_google.service import DOC

    files, _, client, ctx, commit = trip_files
    files.google.edits = GoogleEditService(files.google)
    files.google.trip_files = files
    repo = files.google.repo
    client.metadata.return_value = {
        "id": "file",
        "name": "Current itinerary",
        "mimeType": DOC,
        "capabilities": {"canEdit": True, "canModifyContent": True},
    }
    await repo.apply_file(
        ctx.chat.key,
        "existing-trip",
        "file",
        {
            "url": "https://docs.google.com/document/d/file/edit",
            "name": "Current itinerary",
            "mime": DOC,
            "role": "itinerary",
            "owner": "owner-id",
            "requester": ctx.user.ref.key,
        },
        repo.file_state([]),
    )
    client.document_for_edit.return_value = {
        "revisionId": "rev-1",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Main"},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "startIndex": 1,
                                "endIndex": 16,
                                "paragraph": {
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": 15,
                                            "textRun": {"content": "Add this place\n"},
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
    action = await files.propose_edit(ctx, "Sample & Example trip", "Add this place", "Add bar")
    rows = await repo.trip_files(ctx.chat.key, "existing-trip")
    await repo.apply_file(
        ctx.chat.key,
        "existing-trip",
        "other",
        {
            "url": "https://docs.google.com/document/d/other/edit",
            "name": "New reference",
            "mime": DOC,
            "role": "reference",
            "owner": "owner-id",
            "requester": ctx.user.ref.key,
        },
        repo.file_state(rows),
    )
    client.document_for_edit.reset_mock()
    client.edit_document.reset_mock()
    with pytest.raises(ValueError, match="associations changed"):
        await files.google.edits.commit(action, commit)
    client.document_for_edit.assert_not_awaited()
    client.edit_document.assert_not_awaited()


async def test_trip_edit_final_identity_recheck_catches_primary_change(trip_files):
    from chatcore_google.service import DOC

    files, _, client, ctx, _ = trip_files
    repo = files.google.repo
    client.metadata.return_value = {
        "id": "file",
        "name": "Current itinerary",
        "mimeType": DOC,
        "capabilities": {"canEdit": True, "canModifyContent": True},
    }
    await repo.apply_file(
        ctx.chat.key,
        "existing-trip",
        "file",
        {
            "url": "https://docs.google.com/document/d/file/edit",
            "name": "Current itinerary",
            "mime": DOC,
            "role": "itinerary",
            "owner": "owner-id",
            "requester": ctx.user.ref.key,
        },
        repo.file_state([]),
    )
    proof = {
        "chat_key": ctx.chat.key,
        "trip_id": "existing-trip",
        "trip_name": "Sample & Example trip",
        "group_revision": 0,
        "actor_revision": 0,
        "owner_email": "owner@example.com",
        "file_id": "file",
        "row": await repo.trip_file(ctx.chat.key, "file"),
        "expected": repo.file_state(await repo.trip_files(ctx.chat.key, "existing-trip")),
        "make_current": False,
    }
    identity_calls = 0

    async def identity_with_concurrent_primary_change():
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 2:
            rows = await repo.trip_files(ctx.chat.key, "existing-trip")
            await repo.apply_file(
                ctx.chat.key,
                "existing-trip",
                "new-primary",
                {
                    "url": "https://docs.google.com/document/d/new-primary/edit",
                    "name": "New primary",
                    "mime": DOC,
                    "role": "itinerary",
                    "owner": "owner-id",
                    "requester": ctx.user.ref.key,
                },
                repo.file_state(rows),
            )
        return "owner-id"

    client.identity.side_effect = identity_with_concurrent_primary_change
    with pytest.raises(ValueError, match="association or settings changed"):
        await files.verify_edit_source(ctx, proof)
    assert await repo.trip_file(ctx.chat.key, "new-primary") is not None


async def test_verified_copy_advances_trip_primary_and_next_edit_uses_it(trip_files):
    from chatcore_google.edit_service import GoogleEditService
    from chatcore_google.service import DOC

    files, _, client, ctx, commit = trip_files
    files.google.edits = GoogleEditService(files.google)
    files.google.trip_files = files
    files.google.settings.copy_qualified = True
    repo = files.google.repo
    source_meta = {
        "id": "file",
        "name": "Current itinerary",
        "mimeType": DOC,
        "isAppAuthorized": True,
        "capabilities": {
            "canEdit": True,
            "canModifyContent": True,
            "canCopy": True,
        },
    }
    copy_meta = {
        **source_meta,
        "id": "copy-id",
        "name": "Current itinerary — edited",
    }

    async def metadata(file_id):
        return copy_meta if file_id == "copy-id" else source_meta

    client.metadata.side_effect = metadata
    await repo.apply_file(
        ctx.chat.key,
        "existing-trip",
        "file",
        {
            "url": "https://docs.google.com/document/d/file/edit?tab=tab-1",
            "name": "Current itinerary",
            "mime": DOC,
            "role": "itinerary",
            "owner": "owner-id",
            "requester": ctx.user.ref.key,
        },
        repo.file_state([]),
    )
    old_text = "Hotel confirmed; add this place"
    new_text = "Hotel confirmed; add bar"

    def document(text, revision):
        return {
            "revisionId": revision,
            "tabs": [
                {
                    "tabProperties": {"tabId": "tab-1", "title": "Guangzhou"},
                    "documentTab": {
                        "namedStyles": {
                            "styles": [
                                {
                                    "namedStyleType": "NORMAL_TEXT",
                                    "textStyle": {
                                        "bold": False,
                                        "italic": False,
                                        "underline": False,
                                        "strikethrough": False,
                                        "smallCaps": False,
                                        "backgroundColor": {},
                                        "foregroundColor": {
                                            "color": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}
                                        },
                                        "baselineOffset": "NONE",
                                        "fontSize": {"magnitude": 11, "unit": "PT"},
                                        "weightedFontFamily": {"fontFamily": "Arial"},
                                    },
                                }
                            ]
                        },
                        "body": {
                            "content": [
                                {
                                    "startIndex": 1,
                                    "endIndex": len(text) + 2,
                                    "paragraph": {
                                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                        "elements": [
                                            {
                                                "startIndex": 1,
                                                "endIndex": len(text) + 1,
                                                "textRun": {"content": text, "textStyle": {}},
                                            },
                                            {
                                                "startIndex": len(text) + 1,
                                                "endIndex": len(text) + 2,
                                                "textRun": {"content": "\n", "textStyle": {}},
                                            },
                                        ],
                                    },
                                }
                            ]
                        },
                    },
                }
            ],
        }

    client.document_for_edit.side_effect = [
        document(old_text, "rev-1"),
        document(old_text, "rev-1"),
        document(old_text, "copy-rev-1"),
        document(new_text, "copy-rev-2"),
        document(new_text, "copy-rev-2"),
    ]
    client.copy_file.return_value = {"id": "copy-id"}
    action = await files.propose_edit(
        ctx,
        "Sample & Example trip",
        "add this place",
        "add bar",
        destination="copy",
        make_current=True,
    )
    assert "advance to this copy" in action.summary
    assert "https://docs.google.com/document/d/file/edit?tab=tab-1" in action.summary
    result = await files.google.edits.commit(action, commit)
    assert "verified" in result.lower()
    rows = await repo.trip_files(ctx.chat.key, "existing-trip")
    assert {row["file_id"]: row["role"] for row in rows} == {
        "file": "reference",
        "copy-id": "itinerary",
    }
    assert next(row for row in rows if row["file_id"] == "copy-id")["url"].endswith("?tab=tab-1")
    next_action = await files.propose_edit(
        ctx, "Sample & Example trip", "add bar", "add restaurant"
    )
    assert next_action.payload["source"]["file_id"] == "copy-id"
    assert client.copy_file.await_count == 1


@pytest.mark.parametrize(
    "mime", ["application/vnd.google-apps.folder", "application/vnd.google-apps.shortcut"]
)
async def test_folder_and_shortcut_are_not_implicit_authority(trip_files, mime):
    files, _, client, ctx, _ = trip_files
    client.metadata.return_value["mimeType"] = mime
    with pytest.raises(ValueError, match="file|shortcut|folder"):
        await files.propose_attach(ctx, URL, "China", "reference")
    client.media.assert_not_awaited()
