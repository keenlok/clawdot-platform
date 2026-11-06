"""Confirmed, bounded Google Docs and Sheets edits."""

from __future__ import annotations

import hashlib
import json
import logging
import re

from chatcore_google.client import GoogleError
from chatcore_google.destinations import copy_or_import
from chatcore_google.doc_verification import verify_doc_readback
from chatcore_google.docx import docx_text
from chatcore_google.edit_models import DOC, DOCX, SHEET, EditDestination, choose_destination
from chatcore_google.edit_snapshot import (
    _occurrences,
    build_sheet_edit,
    find_doc_match,
    flatten_doc_snapshot,
    sheet_range_shape,
    validate_sheet_values,
)
from chatcore_google.link_sharing import (
    payload_link_access,
    publish_doc_link_access,
    record_result,
    resolve_link_access,
)
from chatcore_google.refs import approved_ref
from chatcore_google.service import select_tab


def _tabs(document):
    tabs = []

    def visit(tab):
        tabs.append(tab.get("tabProperties") or {})
        for child in tab.get("childTabs", ()):
            visit(child)

    for tab in document.get("tabs", ()):
        visit(tab)
    if not tabs and document.get("body"):
        tabs.append({"tabId": "", "title": ""})
    return tabs


def _web_url(file_id, mime):
    kind = "spreadsheets" if mime == SHEET else "document"
    return f"https://docs.google.com/{kind}/d/{file_id}/edit"


def _copy_title(name, requested):
    if requested is not None:
        if not isinstance(requested, str) or not requested.strip() or len(requested) > 200:
            raise ValueError("Copy title must contain 1–200 characters.")
        return requested
    return str(name or "Google file")[:190].rstrip() + " — edited"


def _access_summary(access, mode):
    if mode == "original":
        return "original file permissions stay unchanged."
    return {
        "private": "private; existing access settings are unchanged.",
        "viewer": "anyone with the link can view; search discovery is off.",
        "editor": "anyone with the link can edit; search discovery is off.",
    }[access]


_logger = logging.getLogger("chatcore_google")


def _read_only(metadata):
    return any(
        item.get("readOnly") is True
        for item in metadata.get("contentRestrictions", ())
        if isinstance(item, dict)
    )


def _uncertain_remote_error(error):
    if not isinstance(error, GoogleError):
        return False
    message = str(error)
    if (
        "HTTP 4" in message
        or "authorization failed" in message
        or "Cannot open that file" in message
    ):
        return False
    if "HTTP 5" in message or "may have completed" in message:
        return True
    # Invalid/oversized 2xx responses and transport failures can follow a completed write.
    return True


class GoogleEditService:
    """Proposal and confirmed execution service shared by API and broker callers."""

    def __init__(self, google):
        self.google = google
        self.settings, self.client, self.repo = google.settings, google.client, google.repo

    async def _source(self, ctx, url, *, expected=None, allow_docx=False):
        await self.google.authorize(ctx, write=True)
        ref = approved_ref(url, ctx.turn.human_urls)
        owner = await self.client.identity()
        metadata = await self.client.metadata(ref.file_id)
        self.google.check_metadata(ref.file_id, metadata)
        self.google.check_type(metadata, expected, allow_docx=allow_docx)
        return ref, metadata, owner

    async def resolve_source(self, ctx, url, *, expected=None, allow_docx=False):
        """Reusable owner and human-provenance check for a downstream broker."""
        return await self._source(ctx, url, expected=expected, allow_docx=allow_docx)

    def _destination(self, metadata, requested):
        result = choose_destination(
            metadata.get("mimeType"),
            metadata,
            requested=requested,
            copy_enabled=self.settings.copy_qualified,
        )
        if result.mode == "copy" and metadata.get("isAppAuthorized") is not True:
            raise ValueError(
                "needs_file_authorization: Google has not granted this app per-file copy access."
            )
        return result

    def destination_decision(self, metadata, requested="auto"):
        return self._destination(metadata, requested)

    async def _doc_snapshot(self, file_id, tab_id):
        raw = await self.client.document_for_edit(file_id)
        snapshot = flatten_doc_snapshot(
            raw, tab_id=tab_id, max_chars=self.settings.max_chars, file_id=file_id
        )
        return raw, snapshot

    async def read_doc_snapshot(self, file_id, tab_id=None):
        """Read bounded selected-tab content with SUGGESTIONS_INLINE indices."""
        _, snapshot = await self._doc_snapshot(file_id, tab_id)
        return snapshot

    async def propose_doc(
        self,
        ctx,
        url,
        find,
        replacement,
        tab_id=None,
        destination="auto",
        copy_title=None,
        link_viewer=None,
        link_access=None,
        source_message_ref=None,
        _trip_source=None,
    ):
        if not isinstance(find, str) or not find or "\n" in find or "\r" in find:
            raise ValueError("Find text must be nonempty and stay within one paragraph.")
        if not isinstance(replacement, str) or "\n" in replacement or "\r" in replacement:
            raise ValueError("Replacement text must stay within one paragraph.")
        if max(len(find), len(replacement)) > self.settings.max_cell_chars:
            raise ValueError("The Doc edit exceeds the configured character limit.")
        target_proof = None
        if _trip_source is None:
            trip_files = getattr(self.google, "trip_files", None)
            if trip_files is not None:
                target_proof = await trip_files.guard_doc_target(ctx, url)
            ref, metadata, owner, source_proof = await self.google.doc_source(
                ctx,
                url,
                source_message_ref=source_message_ref,
                write=True,
            )
        else:
            _, row, ref, metadata, owner = await self.google.trip_files.verify_edit_source(
                ctx, _trip_source
            )
            url = row["url"]
            source_proof = {"kind": "trip_file", "trip_id": _trip_source["trip_id"]}
        if ref.doc_tab and tab_id and ref.doc_tab != tab_id:
            raise ValueError("The supplied Docs tab ID disagrees with the tab selected by the URL.")
        mime = metadata["mimeType"]
        chosen = self._destination(metadata, destination)
        if _trip_source is not None and _trip_source.get("make_current") is None:
            _trip_source = dict(
                _trip_source,
                make_current=destination == "auto" and chosen.mode != "original",
            )
        if (
            _trip_source is not None
            and _trip_source.get("make_current")
            and chosen.mode == "original"
        ):
            raise ValueError("make_current applies only when the edit creates a separate copy.")
        explicit_access = link_access is not None or link_viewer is not None
        selected_access = resolve_link_access(self.settings, link_access, link_viewer)
        if chosen.mode == "original" and explicit_access and selected_access != "private":
            raise ValueError(
                "Original Docs stay unchanged; choose destination=copy for link access."
            )
        frozen_access = selected_access if chosen.mode != "original" else "private"
        name = str(metadata.get("name", "Document"))[:300]
        title = _copy_title(name, copy_title) if chosen.mode != "original" else None
        source = {
            "file_id": ref.file_id,
            "mime": mime,
            "content_revision": metadata.get("headRevisionId"),
            "url": url,
        }
        if source_proof is not None:
            source["authority"] = source_proof
        if mime == DOCX:
            if metadata.get("capabilities", {}).get("canDownload") is not True:
                raise ValueError(
                    "DOCX downloading is not permitted; no native Doc can be prepared."
                )
            if not self.settings.copy_qualified:
                raise ValueError("DOCX conversion is disabled until owner-grant qualification.")
            size = metadata.get("size")
            if size is not None and int(size) > self.settings.max_bytes:
                raise ValueError("DOCX exceeds the configured download size limit.")
            data = await self.client.media(ref.file_id)
            if len(data) > self.settings.max_bytes:
                raise ValueError("DOCX exceeds the configured download size limit.")
            text = docx_text(data, max_chars=self.settings.max_chars)
            if _occurrences(text, find) != 1:
                raise ValueError(
                    "The requested text must occur exactly once in the DOCX main body."
                )
            source["sha256"] = hashlib.sha256(data).hexdigest()
            tab_title = "Imported first tab"
            before_text = text
            precondition = {"tab_id": None, "tab_title": tab_title, "before_text": text}
        else:
            _, snapshot = await self._doc_snapshot(ref.file_id, tab_id or ref.doc_tab)
            match = find_doc_match(snapshot, find)
            tab_title, before_text = snapshot.title, snapshot.text
            precondition = {
                "document_revision": snapshot.revision_id,
                "tab_id": snapshot.tab_id,
                "tab_title": tab_title,
                "start_index": match.start_index,
                "end_index": match.end_index,
                "uniform_style": match.uniform_style,
                "effective_style": match.effective_style,
                "style_resolved": match.style_resolved,
                "named_style_type": match.named_style_type,
                "named_styles": list(snapshot.named_styles),
                "in_table": match.in_table,
                "before_text": before_text,
                "coverage_complete": snapshot.complete,
            }
        _, revision = await self.repo.settings(ctx.chat.key)
        if _trip_source is None:
            await self.google.recheck_doc_source(ctx, ref.file_id, source_proof, write=True)
        else:
            await self.google.trip_files.verify_edit_source(ctx, _trip_source)
        payload = {
            "owner_email": self.settings.owner_email,
            "owner": owner,
            "revision": revision,
            "source": source,
            "destination": {"mode": chosen.mode, "mime": chosen.mime_type, "title": title},
            "precondition": precondition,
            "snapshot": {"text": before_text, "tab_title": tab_title},
            "edit": {"find": find, "replacement": replacement},
            "link_access": frozen_access,
        }
        if _trip_source is not None:
            payload["trip_source"] = _trip_source
        if target_proof is not None:
            payload["trip_target"] = target_proof
        if frozen_access != "editor":
            payload["link_viewer"] = frozen_access == "viewer"
        where = (
            "the original file"
            if chosen.mode == "original"
            else f"a new native Google Doc titled {title!r}"
        )
        summary = (
            f"Edit Google Doc: {name}\nSource: {url}\nDestination: {where}\n"
            + f"Access: {_access_summary(frozen_access, chosen.mode)}\n"
            + f"Tab: {tab_title or 'Main body'}\n\n"
            f"Before: {find!r}\nAfter: {replacement!r}\n"
            "Scope: one exact main-body paragraph or table-cell match; "
            "headers and footers are excluded."
        )
        if _trip_source is not None:
            summary += f"\nTrip: {_trip_source['trip_name']}\n"
            if chosen.mode == "original":
                summary += "Current itinerary: edit this saved primary in place."
            elif _trip_source.get("make_current"):
                summary += "Current itinerary: advance to this copy only after verified edit."
            else:
                summary += "Current itinerary: remains the saved source; this copy is standalone."
        return await self.google.action(ctx, "edit_doc", payload, summary)

    async def propose_trip_doc(
        self,
        ctx,
        proof,
        find,
        replacement,
        *,
        destination="auto",
        copy_title=None,
        link_access=None,
    ):
        _, row, _, _, _ = await self.google.trip_files.verify_edit_source(ctx, proof)
        return await self.propose_doc(
            ctx,
            row["url"],
            find,
            replacement,
            destination=destination,
            copy_title=copy_title,
            link_access=link_access,
            _trip_source=proof,
        )

    async def propose_sheet(
        self, ctx, url, tab, cell_range, values, destination="auto", copy_title=None
    ):
        if not isinstance(tab, str) or not tab:
            raise ValueError("Specify the exact Sheet tab name.")
        width, height, _ = sheet_range_shape(
            cell_range,
            max_cells=self.settings.max_edit_cells,
            max_rows=self.settings.max_rows,
            max_columns=self.settings.max_columns,
        )
        validate_sheet_values(
            values,
            width=width,
            height=height,
            max_cells=self.settings.max_edit_cells,
            max_cell_chars=self.settings.max_cell_chars,
            max_chars=self.settings.max_chars,
        )
        ref, metadata, owner = await self._source(ctx, url, expected=SHEET)
        chosen = self._destination(metadata, destination)
        selected = select_tab(await self.client.sheet(ref.file_id), ref, tab)
        quoted = "'" + selected["title"].replace("'", "''") + "'!" + cell_range
        grid = await self.client.grid_for_edit(ref.file_id, selected["sheetId"], quoted)
        snapshot = build_sheet_edit(
            grid,
            sheet_id=selected["sheetId"],
            tab=selected["title"],
            cell_range=cell_range,
            values=values,
            max_cells=self.settings.max_edit_cells,
            max_cell_chars=self.settings.max_cell_chars,
            max_rows=self.settings.max_rows,
            max_columns=self.settings.max_columns,
            max_chars=self.settings.max_chars,
        )
        title = _copy_title(metadata.get("name"), copy_title) if chosen.mode != "original" else None
        _, revision = await self.repo.settings(ctx.chat.key)
        payload = {
            "owner_email": self.settings.owner_email,
            "owner": owner,
            "revision": revision,
            "source": {"file_id": ref.file_id, "mime": SHEET, "content_revision": None},
            "destination": {"mode": chosen.mode, "mime": SHEET, "title": title},
            "precondition": {
                "sheet_id": snapshot.sheet_id,
                "tab_title": snapshot.tab,
                "cell_range": snapshot.cell_range,
                "grid_range": snapshot.grid_range,
                "grid_properties": selected.get("gridProperties"),
                "before": snapshot.before,
            },
            "edit": {"after": snapshot.after, "type_changes": snapshot.type_changes},
        }
        summary = (
            f"Edit Google Sheet: {metadata.get('name', 'Spreadsheet')}\nSource: {url}\n"
            f"Destination: {'the original file' if chosen.mode == 'original' else 'a new Sheet'}\n"
            f"Title: {title!r}\n"
            f"Tab: {snapshot.tab} · Range: {cell_range}\n"
            f"Before: {json.dumps(snapshot.before, ensure_ascii=False)}\n"
            f"After: {json.dumps(snapshot.after, ensure_ascii=False)}\n"
            "Concurrency: Sheets has no revision compare-and-swap; "
            "a collaborator can race after the final read."
        )
        return await self.google.action(ctx, "edit_sheet", payload, summary)

    async def _verify_pending(self, action, ctx):
        if (
            action.chat != ctx.chat
            or action.requester != ctx.user.ref
            or action.is_expired()
            or action.thread_id != ctx.thread_id
        ):
            raise PermissionError("This confirmation belongs to another requester, chat or topic.")
        payload = action.payload
        if payload.get("owner_email") != self.settings.owner_email:
            raise ValueError("Google owner configuration changed; request again.")
        await self.google.authorize(ctx, write=True)
        if (await self.repo.settings(ctx.chat.key))[1] != payload.get("revision"):
            raise ValueError("Google settings changed; request a fresh confirmation.")
        if await self.client.identity() != payload.get("owner"):
            raise ValueError("Google owner identity changed; request a fresh confirmation.")
        if await self.repo.edit_result(ctx.chat.key, action.id):
            raise ValueError(
                "This action was already handled or has an unresolved result; "
                "inspect it before retrying."
            )
        return payload

    async def _current_source(self, payload):
        source, mode = payload["source"], payload["destination"]["mode"]
        metadata = await self.client.metadata(source["file_id"])
        self.google.check_metadata(source["file_id"], metadata)
        if metadata.get("id", source["file_id"]) != source["file_id"]:
            raise ValueError("The source identity changed; request a fresh preview.")
        if metadata.get("mimeType") != source["mime"] or metadata.get("trashed"):
            raise ValueError("The source type or availability changed; request a fresh preview.")
        caps = metadata.get("capabilities") or {}
        if source["mime"] == DOCX:
            if caps.get("canDownload") is not True or caps.get("canCopy") is not True:
                raise ValueError("DOCX download or conversion permission changed; request again.")
            if not self.settings.copy_qualified:
                raise ValueError("DOCX conversion is disabled until owner-grant qualification.")
            if metadata.get("headRevisionId") != source.get("content_revision"):
                raise ValueError(
                    "DOCX revision changed after preview; request a fresh confirmation."
                )
        elif mode == "original":
            if (
                caps.get("canEdit") is not True
                or caps.get("canModifyContent") is not True
                or _read_only(metadata)
            ):
                raise ValueError("Original edit access changed; request a fresh copy preview.")
        else:
            if not self.settings.copy_qualified:
                raise ValueError("Copy fallback is disabled until owner-grant qualification.")
            if caps.get("canCopy") is not True:
                raise ValueError("Google no longer permits copying this file.")
            if metadata.get("isAppAuthorized") is not True:
                raise ValueError(
                    "needs_file_authorization: Google per-file copy access was revoked."
                )
        return metadata

    async def _current_doc(self, payload):
        pre = payload["precondition"]
        raw = await self.client.document_for_edit(payload["source"]["file_id"])
        snapshot = flatten_doc_snapshot(
            raw,
            tab_id=pre.get("tab_id"),
            max_chars=self.settings.max_chars,
            file_id=payload["source"]["file_id"],
        )
        if snapshot.revision_id != pre.get("document_revision") or snapshot.text != pre.get(
            "before_text"
        ):
            return snapshot, False
        try:
            match = find_doc_match(snapshot, payload["edit"]["find"])
        except ValueError:
            return snapshot, False
        valid = (
            match.start_index == pre.get("start_index")
            and match.end_index == pre.get("end_index")
            and match.uniform_style == pre.get("uniform_style")
            and match.effective_style == pre.get("effective_style", match.effective_style)
            and match.style_resolved == pre.get("style_resolved", match.style_resolved)
            and match.named_style_type == pre.get("named_style_type", match.named_style_type)
            and list(snapshot.named_styles) == pre.get("named_styles", list(snapshot.named_styles))
            and match.in_table == pre.get("in_table", match.in_table)
        )
        return snapshot, valid

    def _sheet_width_height(self, pre):
        grid = pre["grid_range"]
        return (
            grid["endColumnIndex"] - grid["startColumnIndex"],
            grid["endRowIndex"] - grid["startRowIndex"],
        )

    def _confirmed_sheet_values(self, payload):
        pre = payload["precondition"]
        width, height = self._sheet_width_height(pre)
        raw = []
        for row in payload["edit"]["after"]:
            values = []
            for cell in row:
                if cell is None:
                    values.append(None)
                    continue
                if not isinstance(cell, dict) or len(cell) != 1:
                    raise ValueError("A confirmed Sheet value is not a supported literal.")
                key, value = next(iter(cell.items()))
                if key == "stringValue" and isinstance(value, str):
                    values.append(value)
                elif key == "boolValue" and type(value) is bool:
                    values.append(value)
                elif key == "numberValue" and isinstance(value, (int, float)):
                    values.append(value)
                else:
                    raise ValueError("A confirmed Sheet value is not a supported literal.")
            raw.append(values)
        return validate_sheet_values(
            raw,
            width=width,
            height=height,
            max_cells=self.settings.max_edit_cells,
            max_cell_chars=self.settings.max_cell_chars,
            max_chars=self.settings.max_chars,
        )

    async def _current_sheet(self, file_id, pre):
        sheet = await self.client.sheet(file_id)
        matching = [
            item.get("properties") or {}
            for item in sheet.get("sheets", ())
            if (item.get("properties") or {}).get("sheetId") == pre["sheet_id"]
        ]
        if len(matching) != 1 or matching[0].get("title") != pre["tab_title"]:
            return None, False
        quoted = "'" + pre["tab_title"].replace("'", "''") + "'!" + pre["cell_range"]
        grid = await self.client.grid_for_edit(file_id, pre["sheet_id"], quoted)
        if matching[0].get("gridProperties") != pre.get("grid_properties"):
            return None, False
        width, height = self._sheet_width_height(pre)
        snapshot = build_sheet_edit(
            grid,
            sheet_id=pre["sheet_id"],
            tab=pre["tab_title"],
            cell_range=pre["cell_range"],
            values=[[None] * width for _ in range(height)],
            max_cells=self.settings.max_edit_cells,
            max_cell_chars=self.settings.max_cell_chars,
            max_rows=self.settings.max_rows,
            max_columns=self.settings.max_columns,
            max_chars=self.settings.max_chars,
        )
        before = tuple(tuple(row) for row in pre["before"])
        return snapshot, snapshot.before == before

    async def _reserve(self, action, ctx):
        payload = action.payload
        destination = payload.get("destination") or {}
        output_binding = None
        if action.kind == "edit_doc" and destination.get("mode") in {"copy", "docx_import"}:
            output_binding = {
                "owner_id": payload["owner"],
                "owner_email": payload["owner_email"],
                "thread_id": action.thread_id,
                "mode": destination["mode"],
            }
        await self.repo.reserve_edit(
            ctx.chat.key,
            action.id,
            ctx.user.ref.key,
            0,
            self.settings.suggestion_rows_per_day,
            payload["revision"],
            kind=action.kind,
            source_id=payload["source"]["file_id"],
            output_binding=output_binding,
        )

    async def reserve_confirmed_action(self, action, ctx):
        """Reserve replay protection and result state before API or browser writes."""
        if action.kind not in {"edit_doc", "edit_sheet", "browser_edit_doc"}:
            raise ValueError("Unknown Google edit reservation kind.")
        payload = await self._verify_pending(action, ctx)
        await self._reserve(action, ctx)
        return payload

    async def _phase(self, action, ctx, phase, *, destination_id=None, stage=None, category=None):
        return await self.repo.update_edit_result(
            ctx.chat.key,
            action.id,
            phase=phase,
            destination_id=destination_id,
            failed_stage=stage,
            error_category=category,
        )

    async def update_action_result(
        self, action, ctx, *, phase, destination_id=None, stage=None, category=None
    ):
        """Shared result transition for the optional browser broker."""
        return await self._phase(
            action, ctx, phase, destination_id=destination_id, stage=stage, category=category
        )

    async def list_browser_results(self, ctx, *, requester=None):
        """List browser-edit result IDs and phases within the current chat."""
        await self.google.authorize(ctx)
        requester_key = requester.key if hasattr(requester, "key") else requester
        return await self.repo.browser_edit_results(ctx.chat.key, requester_key)

    async def _source_bytes(self, payload):
        source = payload["source"]
        data = await self.client.media(source["file_id"])
        if len(data) > self.settings.max_bytes:
            raise ValueError("DOCX exceeds the configured download size limit.")
        if hashlib.sha256(data).hexdigest() != source.get("sha256"):
            raise ValueError("DOCX content changed after preview; request a fresh confirmation.")
        return data

    async def _create_destination(self, payload, source_bytes=None):
        dest = payload["destination"]
        return await copy_or_import(
            self.client,
            EditDestination(mode=dest["mode"], mime_type=dest["mime"]),
            source_id=payload["source"]["file_id"],
            title=dest["title"],
            confirmed=True,
            source_bytes=source_bytes,
        )

    async def create_reserved_destination(self, action, ctx, *, source_bytes=None):
        """Create a confirmed destination and journal its ID before validating it."""
        payload = await self.google.verify_commit(action, ctx)
        result = await self.repo.edit_result(ctx.chat.key, action.id)
        if (
            result is None
            or result["phase"] != "reserved"
            or result["kind"] != action.kind
            or result["source_id"] != action.payload["source"]["file_id"]
        ):
            raise ValueError("The confirmed Google edit reservation is unavailable.")
        source = payload["source"]
        destination = payload["destination"]
        await self._current_source(payload)
        if destination["mode"] not in {"copy", "docx_import"}:
            raise ValueError("This action does not require a new destination.")
        if action.kind == "edit_doc" and source["mime"] != DOCX:
            _, content_valid = await self._current_doc(payload)
            if not content_valid:
                raise ValueError("The selected Doc content changed; request a fresh preview.")
        elif action.kind == "edit_sheet":
            _, content_valid = await self._current_sheet(source["file_id"], payload["precondition"])
            if not content_valid:
                raise ValueError("The selected Sheet content changed; request a fresh preview.")
        elif action.kind == "browser_edit_doc":
            if source["mime"] == DOCX:
                frozen_bytes = await self._source_bytes(payload)
                if (
                    source_bytes is not None
                    and hashlib.sha256(source_bytes).hexdigest()
                    != hashlib.sha256(frozen_bytes).hexdigest()
                ):
                    raise ValueError("DOCX source bytes do not match the confirmed preview.")
                source_bytes = frozen_bytes
            else:
                _, content_valid = await self._current_doc(payload)
                if not content_valid:
                    raise ValueError("The selected Doc content changed; request a fresh preview.")
        elif source["mime"] == DOCX:
            frozen_bytes = await self._source_bytes(payload)
            if (
                source_bytes is not None
                and hashlib.sha256(source_bytes).digest() != hashlib.sha256(frozen_bytes).digest()
            ):
                raise ValueError("DOCX source bytes do not match the confirmed preview.")
            source_bytes = frozen_bytes
        # The DB claim is the final local gate before the non-idempotent Google create call.
        await self.repo.claim_destination_creation(
            ctx.chat.key,
            action.id,
            kind=action.kind,
            source_id=source["file_id"],
            revision=payload["revision"],
        )
        try:
            created = await self._create_destination(payload, source_bytes=source_bytes)
        except Exception as exc:
            uncertain = _uncertain_remote_error(exc)
            await self._phase(
                action,
                ctx,
                "uncertain" if uncertain else "conflict",
                stage="destination",
                category="remote_unknown" if uncertain else "refused",
            )
            raise
        if (
            not isinstance(created, dict)
            or not isinstance(created.get("id"), str)
            or not created["id"]
        ):
            await self._phase(action, ctx, "uncertain", stage="destination", category="missing_id")
            raise GoogleError("Google may have created a destination without returning its ID.")
        if len(created["id"]) > 200:
            await self._phase(
                action, ctx, "uncertain", stage="destination", category="invalid_remote_id"
            )
            raise GoogleError("Google may have created a destination with an invalid ID.")
        await self._phase(action, ctx, "destination_created", destination_id=created["id"])
        return created

    def _doc_requests(self, pre, edit, tab_id):
        start, end = pre["start_index"], pre["end_index"]
        requests = [
            {
                "deleteContentRange": {
                    "range": {
                        "startIndex": start,
                        "endIndex": end,
                        "tabId": tab_id,
                    }
                }
            }
        ]
        replacement = edit["replacement"]
        if replacement:
            end_index = start + len(replacement.encode("utf-16-le")) // 2
            requests.append(
                {
                    "insertText": {
                        "location": {"index": start, "tabId": tab_id},
                        "text": replacement,
                    }
                }
            )
            requests.append(
                {
                    "updateTextStyle": {
                        "range": {"startIndex": start, "endIndex": end_index, "tabId": tab_id},
                        "textStyle": pre["uniform_style"],
                        "fields": "bold,italic,underline,strikethrough,smallCaps,backgroundColor,"
                        "foregroundColor,baselineOffset,fontSize,weightedFontFamily,link",
                    }
                }
            )
        return requests

    async def _verify_doc_after_write(self, file_id, expected_text, match, replacement, tab_id):
        result = await verify_doc_readback(
            self.client,
            file_id,
            expected_text,
            max_chars=self.settings.max_chars,
            tab_id=tab_id,
            match=match,
            replacement=replacement,
        )
        if not result.verified:
            _logger.info(
                "Google Doc read-back verification failed category=%s attempts=%d",
                result.category,
                result.attempts,
            )
        return result

    async def _finish_doc_success(self, action, ctx, payload, destination_id, tab_id=None):
        """Finalize verified edit content, sharing only an explicitly frozen new output."""
        access = payload_link_access(payload)
        if access in {"viewer", "editor"}:
            try:
                if payload.get("trip_source"):
                    await self.google.trip_files.verify_edit_source(ctx, payload["trip_source"])
                shared, message = await publish_doc_link_access(
                    self.google, action, ctx, payload, destination_id
                )
            except Exception:
                await record_result(
                    self.google,
                    action,
                    ctx,
                    phase="uncertain",
                    destination_id=destination_id,
                    failed_stage="sharing_result",
                    error_category="journal_or_preflight_failure",
                )
                return (
                    f"Uncertain: the Doc is at {_web_url(destination_id, DOC)}; its link-access "
                    "sharing result could not be safely finalized. Inspect access before retrying."
                )
            if not shared:
                return message
        final_result = await record_result(
            self.google,
            action,
            ctx,
            phase="verified",
            destination_id=destination_id,
        )
        if isinstance(final_result, Exception):
            return (
                f"Uncertain: the Doc is at {_web_url(destination_id, DOC)}; content and access "
                "were verified, but final journal completion failed. Inspect before retrying."
            )
        trip_source = payload.get("trip_source")
        if trip_source and trip_source.get("make_current"):
            try:
                await self.google.trip_files.verify_edit_source(ctx, trip_source)
                data = {
                    "url": _web_url(destination_id, DOC) + (f"?tab={tab_id}" if tab_id else ""),
                    "name": payload["destination"]["title"],
                    "mime": DOC,
                    "role": "itinerary",
                    "owner": trip_source["row"]["owner"],
                    "requester": ctx.user.ref.key,
                }
                await self.repo.apply_file(
                    trip_source["chat_key"],
                    trip_source["trip_id"],
                    destination_id,
                    data,
                    trip_source["expected"],
                    revision=trip_source["group_revision"],
                )
            except Exception:
                return (
                    f"Verified edit: {_web_url(destination_id, DOC)}. "
                    "The document edit succeeded, but the trip's current itinerary "
                    "association could not be verified or advanced; inspect trip files "
                    "before requesting another change."
                )
        suffix = {
            "private": "",
            "viewer": "; anyone with the link can view",
            "editor": "; anyone with the link can edit",
        }[access]
        return f"Verified: {_web_url(destination_id, DOC)}{suffix}"

    async def _finish_doc_text_only(self, action, ctx, payload, destination_id, verification):
        category = (
            "formatting_mismatch"
            if verification.category == "style_mismatch"
            else "formatting_unverified"
        )
        await self._phase(
            action,
            ctx,
            "partial",
            destination_id=destination_id,
            stage="verification",
            category=category,
        )
        response = (
            f"Partial: text was saved and verified in this Doc at {_web_url(destination_id, DOC)}. "
            "Formatting preservation could not be verified. Do not repeat the edit; inspect "
            "the current Doc before proposing another write."
        )
        if payload["destination"]["mode"] != "original":
            response += " Sharing and current-itinerary advancement were not performed."
        return response

    async def _copy_doc_tab(self, payload, file_id):
        pre = payload["precondition"]
        raw = await self.client.document_for_edit(file_id)
        tabs = _tabs(raw)
        if pre.get("tab_id") is None:
            if len(tabs) != 1:
                raise ValueError("Imported DOCX did not produce one unambiguous native tab.")
            tab = tabs[0]
        else:
            matching = [item for item in tabs if item.get("title") == pre.get("tab_title")]
            if len(matching) != 1:
                raise ValueError("The copied Doc tab could not be resolved uniquely.")
            tab = matching[0]
        snapshot = flatten_doc_snapshot(
            raw, tab_id=tab.get("tabId"), max_chars=self.settings.max_chars, file_id=file_id
        )
        if pre.get("tab_id") is not None and snapshot.text != pre.get("before_text"):
            raise ValueError("The copied Doc content changed after preview.")
        return snapshot, find_doc_match(snapshot, payload["edit"]["find"])

    async def _sheet_after(self, payload, file_id, sheet_id):
        snapshot, valid = await self._current_sheet(file_id, payload["precondition"])
        if snapshot is None:
            return None
        return snapshot.before

    async def commit(self, action, ctx):
        payload = await self._verify_pending(action, ctx)
        if action.kind not in {"edit_doc", "edit_sheet"}:
            raise ValueError("Unknown Google edit action.")
        if action.kind == "edit_doc" and payload_link_access(payload) != "private":
            if payload.get("destination", {}).get("mode") == "original":
                raise ValueError("Original Docs stay unchanged; request a separate copy.")
        confirmed_values = (
            self._confirmed_sheet_values(payload) if action.kind == "edit_sheet" else None
        )
        if payload.get("trip_source"):
            await self.google.trip_files.verify_edit_source(ctx, payload["trip_source"])
        elif payload.get("trip_target") and getattr(self.google, "trip_files", None):
            await self.google.trip_files.verify_doc_target(ctx, payload["trip_target"])
        await self._current_source(payload)
        if payload.get("trip_source"):
            await self.google.trip_files.verify_edit_source(ctx, payload["trip_source"])
        docx_bytes = (
            await self._source_bytes(payload) if payload["source"]["mime"] == DOCX else None
        )
        source_snapshot = None
        if action.kind == "edit_doc" and payload["source"]["mime"] != DOCX:
            source_snapshot, valid = await self._current_doc(payload)
            if not valid:
                await self._reserve(action, ctx)
                await self._phase(
                    action, ctx, "conflict", stage="precondition", category="content_changed"
                )
                return (
                    "Conflict: the selected Doc content or revision changed. "
                    "Request a fresh preview."
                )
        elif action.kind == "edit_sheet":
            source_snapshot, valid = await self._current_sheet(
                payload["source"]["file_id"], payload["precondition"]
            )
            if not valid:
                await self._reserve(action, ctx)
                await self._phase(
                    action, ctx, "conflict", stage="precondition", category="content_changed"
                )
                return "Conflict: the selected cells or tab changed. Request a fresh preview."

        if action.kind == "edit_doc":
            if payload.get("trip_source"):
                await self.google.trip_files.verify_edit_source(ctx, payload["trip_source"])
            else:
                await self.google.recheck_doc_source(
                    ctx,
                    payload["source"]["file_id"],
                    payload["source"].get("authority"),
                    write=True,
                )
                if payload.get("trip_target") and getattr(self.google, "trip_files", None):
                    await self.google.trip_files.verify_doc_target(ctx, payload["trip_target"])

        await self._reserve(action, ctx)
        dest, source = payload["destination"], payload["source"]
        destination_id = source["file_id"] if dest["mode"] == "original" else None
        if dest["mode"] != "original":
            if payload.get("trip_source"):
                await self.google.trip_files.verify_edit_source(ctx, payload["trip_source"])
            elif payload.get("trip_target") and getattr(self.google, "trip_files", None):
                await self.google.trip_files.verify_doc_target(ctx, payload["trip_target"])
            try:
                created = await self._create_destination(payload, source_bytes=docx_bytes)
            except Exception as exc:
                unknown = _uncertain_remote_error(exc)
                phase = "uncertain" if unknown else "conflict"
                await self._phase(
                    action,
                    ctx,
                    phase,
                    stage="destination",
                    category="remote_unknown" if unknown else "refused",
                )
                return (
                    "Uncertain: Google may have created the destination; "
                    "inspect Drive before retrying."
                    if unknown
                    else "The destination could not be created; no content edit was attempted."
                )
            if (
                not isinstance(created, dict)
                or not isinstance(created.get("id"), str)
                or not created["id"]
            ):
                await self._phase(
                    action, ctx, "uncertain", stage="destination", category="missing_id"
                )
                return "Uncertain: Google may have created a destination without returning its ID."
            destination_id = created["id"]
            if len(destination_id) > 200:
                await self._phase(
                    action, ctx, "uncertain", stage="destination", category="invalid_remote_id"
                )
                return "Uncertain: Google may have created a destination with an invalid ID."
            await self._phase(action, ctx, "destination_created", destination_id=destination_id)
            try:
                if (
                    not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", destination_id)
                    or destination_id == source["file_id"]
                ):
                    raise ValueError("Google returned an invalid or unchanged destination ID.")
                created_meta = await self.client.metadata(destination_id)
                self.google.check_metadata(destination_id, created_meta)
                if created_meta.get("mimeType") != dest["mime"]:
                    raise ValueError("Google created an unexpected destination MIME type.")
                if dest["mime"] != DOCX and (
                    created_meta.get("capabilities", {}).get("canEdit") is not True
                    or created_meta.get("capabilities", {}).get("canModifyContent") is not True
                    or _read_only(created_meta)
                ):
                    raise ValueError("The new destination is not editable.")
            except Exception:
                await self._phase(
                    action,
                    ctx,
                    "partial",
                    destination_id=destination_id,
                    stage="destination_validation",
                    category="inaccessible",
                )
                safe_url = (
                    _web_url(destination_id, dest["mime"])
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", destination_id)
                    else "the known destination ID"
                )
                return (
                    f"Partial: destination {safe_url} could not be validated; "
                    "no edit was attempted."
                )

        if action.kind == "edit_doc":
            if dest["mode"] == "original":
                target_snapshot = source_snapshot
                target_match = find_doc_match(target_snapshot, payload["edit"]["find"])
            else:
                try:
                    target_snapshot, target_match = await self._copy_doc_tab(
                        payload, destination_id
                    )
                except (ValueError, GoogleError):
                    await self._phase(
                        action,
                        ctx,
                        "partial",
                        destination_id=destination_id,
                        stage="destination_validation",
                        category="content_mismatch",
                    )
                    return (
                        f"Partial: the unedited copy is at {_web_url(destination_id, DOC)}; "
                        "its content did not match the preview."
                    )
                if source["mime"] != DOCX and (
                    target_snapshot.text != payload["snapshot"]["text"]
                    or target_match.uniform_style != payload["precondition"]["uniform_style"]
                    or target_match.effective_style
                    != payload["precondition"].get("effective_style", target_match.effective_style)
                    or target_match.style_resolved
                    != payload["precondition"].get("style_resolved", target_match.style_resolved)
                    or list(target_snapshot.named_styles)
                    != payload["precondition"].get(
                        "named_styles", list(target_snapshot.named_styles)
                    )
                ):
                    await self._phase(
                        action,
                        ctx,
                        "partial",
                        destination_id=destination_id,
                        stage="destination_validation",
                        category="content_mismatch",
                    )
                    return (
                        f"Partial: the unedited copy is at {_web_url(destination_id, DOC)}; "
                        "its content or style changed."
                    )
            tab_id = target_snapshot.tab_id
            pre = dict(payload["precondition"])
            pre.update(
                start_index=target_match.start_index,
                end_index=target_match.end_index,
                uniform_style=target_match.uniform_style,
            )
            await self._phase(action, ctx, "applying", destination_id=destination_id)
            expected = target_snapshot.text.replace(
                payload["edit"]["find"], payload["edit"]["replacement"], 1
            )
            try:
                if payload.get("trip_source"):
                    await self.google.trip_files.verify_edit_source(ctx, payload["trip_source"])
                elif payload.get("trip_target") and getattr(self.google, "trip_files", None):
                    await self.google.trip_files.verify_doc_target(ctx, payload["trip_target"])
                await self.client.edit_document(
                    destination_id,
                    self._doc_requests(pre, payload["edit"], tab_id),
                    target_snapshot.revision_id,
                )
            except Exception as exc:
                verification = await self._verify_doc_after_write(
                    destination_id,
                    expected,
                    target_match,
                    payload["edit"]["replacement"],
                    tab_id,
                )
                if verification.verified:
                    return await self._finish_doc_success(
                        action, ctx, payload, destination_id, target_snapshot.tab_id
                    )
                if verification.text_verified:
                    return await self._finish_doc_text_only(
                        action, ctx, payload, destination_id, verification
                    )
                stale = "HTTP 400" in str(exc)
                phase = (
                    "partial"
                    if stale and dest["mode"] != "original"
                    else "conflict"
                    if stale
                    else "uncertain"
                )
                await self._phase(
                    action,
                    ctx,
                    phase,
                    destination_id=destination_id,
                    stage="doc_edit" if stale else "verification",
                    category="stale_revision" if stale else verification.category,
                )
                if stale and dest["mode"] != "original":
                    return (
                        f"Partial: the copy remains at {_web_url(destination_id, DOC)}; "
                        "the edit was refused and no link-access change was attempted."
                    )
                return (
                    "Conflict: the Doc revision changed before the edit. Request a fresh preview."
                    if stale
                    else f"Uncertain: the Doc is at {_web_url(destination_id, DOC)}; "
                    f"the edit could not be verified ({verification.category}), so no "
                    "link-access change was attempted. Inspect before retrying."
                )
            verification = await self._verify_doc_after_write(
                destination_id,
                expected,
                target_match,
                payload["edit"]["replacement"],
                tab_id,
            )
            if not verification.verified:
                if verification.text_verified:
                    return await self._finish_doc_text_only(
                        action, ctx, payload, destination_id, verification
                    )
                await self._phase(
                    action,
                    ctx,
                    "uncertain",
                    destination_id=destination_id,
                    stage="verification",
                    category=verification.category,
                )
                return (
                    f"Uncertain: the Doc is at {_web_url(destination_id, DOC)}; content "
                    f"verification failed ({verification.category}), so no link-access change "
                    "was attempted. Inspect before retrying."
                )
            return await self._finish_doc_success(
                action, ctx, payload, destination_id, target_snapshot.tab_id
            )

        pre = payload["precondition"]
        if dest["mode"] == "original":
            target_sheet_id = pre["sheet_id"]
        else:
            try:
                copied = await self.client.sheet(destination_id)
                props = [
                    item.get("properties") or {}
                    for item in copied.get("sheets", ())
                    if (item.get("properties") or {}).get("title") == pre["tab_title"]
                ]
            except Exception:
                await self._phase(
                    action,
                    ctx,
                    "partial",
                    destination_id=destination_id,
                    stage="destination_validation",
                    category="read_failed",
                )
                return (
                    f"Partial: the unedited Sheet copy is at {_web_url(destination_id, SHEET)}; "
                    "its tab could not be read."
                )
            if len(props) != 1:
                await self._phase(
                    action,
                    ctx,
                    "partial",
                    destination_id=destination_id,
                    stage="destination_validation",
                    category="tab_mismatch",
                )
                return (
                    f"Partial: the unedited Sheet copy is at {_web_url(destination_id, SHEET)}; "
                    "its tab could not be resolved."
                )
            target_sheet_id = props[0]["sheetId"]
            try:
                snapshot, _ = await self._current_sheet(
                    destination_id, {**pre, "sheet_id": target_sheet_id}
                )
            except Exception:
                await self._phase(
                    action,
                    ctx,
                    "partial",
                    destination_id=destination_id,
                    stage="destination_validation",
                    category="read_failed",
                )
                return (
                    f"Partial: the unedited Sheet copy is at {_web_url(destination_id, SHEET)}; "
                    "its cells could not be read."
                )
            if snapshot is None or snapshot.before != tuple(tuple(row) for row in pre["before"]):
                await self._phase(
                    action,
                    ctx,
                    "partial",
                    destination_id=destination_id,
                    stage="destination_validation",
                    category="content_mismatch",
                )
                return (
                    f"Partial: the unedited Sheet copy is at {_web_url(destination_id, SHEET)}; "
                    "selected cells changed."
                )
        # Recheck destination values, protections and grid immediately before the write.
        try:
            final_snapshot, final_valid = await self._current_sheet(
                destination_id, {**pre, "sheet_id": target_sheet_id}
            )
        except Exception:
            phase = "partial" if dest["mode"] != "original" else "conflict"
            await self._phase(
                action,
                ctx,
                phase,
                destination_id=destination_id,
                stage="precondition",
                category="read_failed",
            )
            return (
                f"Partial: the unedited Sheet copy is at {_web_url(destination_id, SHEET)}; "
                "cells could not be rechecked."
                if phase == "partial"
                else (
                    "Conflict: selected cells could not be rechecked before the update. "
                    "Request a fresh preview."
                )
            )
        expected_before = tuple(tuple(row) for row in pre["before"])
        if not final_valid or final_snapshot.before != expected_before:
            phase = "partial" if dest["mode"] != "original" else "conflict"
            await self._phase(
                action,
                ctx,
                phase,
                destination_id=destination_id,
                stage="precondition",
                category="content_changed",
            )
            return (
                f"Partial: the unedited Sheet copy is at {_web_url(destination_id, SHEET)}; "
                "cells changed."
                if phase == "partial"
                else (
                    "Conflict: the selected cells changed before the update. "
                    "Request a fresh preview."
                )
            )
        after_values = confirmed_values
        rows = [
            {"values": [{"userEnteredValue": value} if value is not None else {} for value in row]}
            for row in after_values
        ]
        await self._phase(action, ctx, "applying", destination_id=destination_id)
        try:
            await self.client.edit_cells(
                destination_id,
                {
                    "requests": [
                        {
                            "updateCells": {
                                "range": {**pre["grid_range"], "sheetId": target_sheet_id},
                                "rows": rows,
                                "fields": "userEnteredValue",
                            }
                        }
                    ]
                },
            )
        except Exception as exc:
            try:
                actual, _ = await self._current_sheet(
                    destination_id, {**pre, "sheet_id": target_sheet_id}
                )
                verified = actual is not None and actual.before == tuple(
                    tuple(row) for row in payload["edit"]["after"]
                )
            except Exception:
                verified = False
            if verified:
                await self._phase(action, ctx, "verified", destination_id=destination_id)
                return f"Verified: {_web_url(destination_id, SHEET)}"
            unknown = _uncertain_remote_error(exc)
            phase = (
                "uncertain" if unknown else "partial" if dest["mode"] != "original" else "conflict"
            )
            await self._phase(
                action,
                ctx,
                phase,
                destination_id=destination_id,
                stage="sheet_edit",
                category="remote_unknown" if unknown else "write_refused",
            )
            return (
                f"Uncertain: inspect {_web_url(destination_id, SHEET)} before retrying."
                if unknown
                else (
                    f"Partial: the copy remains at {_web_url(destination_id, SHEET)}; "
                    "Google refused the Sheet update."
                )
                if dest["mode"] != "original"
                else "Conflict: Google refused the Sheet update; request a fresh preview."
            )
        try:
            actual, _ = await self._current_sheet(
                destination_id, {**pre, "sheet_id": target_sheet_id}
            )
            verified = actual is not None and actual.before == tuple(
                tuple(row) for row in payload["edit"]["after"]
            )
        except Exception:
            verified = False
        if not verified:
            await self._phase(
                action,
                ctx,
                "uncertain",
                destination_id=destination_id,
                stage="verification",
                category="readback_mismatch",
            )
            return f"Uncertain: inspect {_web_url(destination_id, SHEET)} before retrying."
        await self._phase(action, ctx, "verified", destination_id=destination_id)
        return f"Verified: {_web_url(destination_id, SHEET)}"
