"""Bounded Google reads and exact, confirmed, append-only remote mutations."""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import replace

from chatcore.files import FilePlatform
from chatcore.pending import PendingAction

from chatcore_google.doc_verification import verify_doc_readback
from chatcore_google.docx import docx_text
from chatcore_google.link_sharing import (
    payload_link_access,
    publish_doc_link_access,
    record_result,
    resolve_link_access,
    web_url,
    write_outcome_uncertain,
)
from chatcore_google.permissions import authorize
from chatcore_google.refs import approved_ref, approved_source_ref, parse_google_ref
from chatcore_google.render import doc_text, literal_rows

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_logger = logging.getLogger("chatcore_google")


def column_name(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def column_number(value):
    result = 0
    for char in value:
        result = result * 26 + ord(char) - 64
    return result


def _same_output_binding(left, right):
    fields = (
        "chat_key",
        "action_id",
        "kind",
        "source_id",
        "destination_id",
        "output_owner_id",
        "output_owner_email",
        "output_thread_id",
        "output_mode",
    )
    return (
        left is not None
        and right is not None
        and all(left.get(field) == right.get(field) for field in fields)
    )


def select_tab(sheet, ref, tab=None):
    tabs = [value["properties"] for value in sheet.get("sheets", [])]
    by_gid = next((value for value in tabs if value["sheetId"] == ref.gid), None)
    by_name = next((value for value in tabs if value["title"] == tab), None) if tab else None
    if ref.gid is not None and by_gid is None or tab is not None and by_name is None:
        raise ValueError("That spreadsheet tab no longer exists.")
    if by_gid and by_name and by_gid["sheetId"] != by_name["sheetId"]:
        raise ValueError("The tab name and URL gid select different tabs.")
    selected = by_gid or by_name or (tabs[0] if tabs else None)
    if selected is None:
        raise ValueError("This spreadsheet has no readable tabs.")
    return selected


class GoogleService:
    def __init__(self, settings, access, client, repo):
        self.settings, self.access, self.client, self.repo = settings, access, client, repo

    async def authorize(self, ctx, *, write=False, configure=False):
        await authorize(
            self.settings, self.access, self.repo, ctx, write=write, configure=configure
        )

    def rows(self, rows, *, grid_columns=None):
        if not isinstance(rows, list) or not rows or len(rows) > self.settings.max_rows:
            raise ValueError("Provide a nonempty batch within the row limit.")
        for row in rows:
            limit = (
                min(self.settings.max_columns, grid_columns)
                if grid_columns is not None
                else self.settings.max_columns
            )
            if not isinstance(row, list) or not row or len(row) > limit:
                raise ValueError("Each row must be within the column limit.")
            if any(
                isinstance(cell, str) and len(cell) > self.settings.max_cell_chars for cell in row
            ):
                raise ValueError("A cell exceeds the character limit.")
        literal_rows(rows)
        if len(json.dumps(rows, ensure_ascii=False)) > self.settings.max_chars:
            raise ValueError("The row batch exceeds the character limit.")
        return copy.deepcopy(rows)

    async def source_file(self, ctx, url, *, source_message_ref=None):
        if source_message_ref is None:
            ref = approved_ref(url, ctx.turn.human_urls)
            await self.authorize(ctx)
        else:
            await self.authorize(ctx)
            ref = await approved_source_ref(url, ctx, source_message_ref)
        owner = await self.client.identity()
        metadata = await self.client.metadata(ref.file_id)
        self.check_metadata(ref.file_id, metadata)
        return ref, metadata, owner

    async def doc_source(self, ctx, url, *, source_message_ref=None, write=False):
        """Resolve a human-posted Doc or a retained same-chat bot-created Doc output."""
        ref = parse_google_ref(url)
        await self.authorize(ctx, write=write)
        output = await self.repo.output_destination(ctx.chat.key, ctx.thread_id, ref.file_id)
        if output is not None:
            try:
                owner = await self.client.identity()
                if (
                    not isinstance(output.get("output_owner_id"), str)
                    or not isinstance(output.get("output_owner_email"), str)
                    or output["output_owner_id"] != owner
                    or output["output_owner_email"].casefold()
                    != self.settings.owner_email.casefold()
                ):
                    raise ValueError("The retained output belongs to a different Google owner.")
                metadata = await self.client.metadata(ref.file_id)
                self._check_output_doc(ref.file_id, metadata, owner, write=write)
                latest = await self.repo.output_destination(
                    ctx.chat.key, ctx.thread_id, ref.file_id
                )
                if not _same_output_binding(latest, output):
                    raise ValueError("The retained output binding changed during the read.")
                return (
                    ref,
                    metadata,
                    owner,
                    {
                        "kind": "output",
                        "action_id": output["action_id"],
                        "file_id": ref.file_id,
                        "owner_id": output["output_owner_id"],
                        "owner_email": output["output_owner_email"],
                        "thread_id": output["output_thread_id"],
                        "mode": output["output_mode"],
                    },
                )
            except (ValueError, PermissionError) as output_error:
                if source_message_ref is None:
                    try:
                        approved_ref(url, ctx.turn.human_urls)
                    except ValueError:
                        raise output_error from None
        if source_message_ref is None:
            ref = approved_ref(url, ctx.turn.human_urls)
            proof = None
        else:
            ref = await approved_source_ref(url, ctx, source_message_ref)
            proof = {"kind": "human_message", "message_id": source_message_ref}
        owner = await self.client.identity()
        metadata = await self.client.metadata(ref.file_id)
        self.check_metadata(ref.file_id, metadata)
        self.check_type(metadata, DOC, allow_docx=True)
        return ref, metadata, owner, proof

    def _check_output_doc(self, file_id, metadata, owner, *, write=False):
        self.check_metadata(file_id, metadata)
        owners = metadata.get("owners") or []
        if (
            not isinstance(owners, list)
            or metadata.get("id", file_id) != file_id
            or metadata.get("mimeType") != DOC
            or metadata.get("isAppAuthorized") is not True
            or metadata.get("ownedByMe") is not True
            or not any(
                str(item.get("emailAddress", "")).casefold() == self.settings.owner_email.casefold()
                and item.get("permissionId") == owner
                for item in owners
                if isinstance(item, dict)
            )
        ):
            raise ValueError(
                "The retained output is not an app-authorized Doc owned by this account."
            )
        caps = metadata.get("capabilities") or {}
        if write and (caps.get("canEdit") is not True or caps.get("canModifyContent") is not True):
            raise ValueError("The retained output is no longer editable by the configured owner.")

    async def recheck_doc_source(self, ctx, file_id, proof, *, write=False):
        """Recheck frozen output or retained-message provenance after remote Doc reads."""
        if not proof:
            return
        if proof.get("kind") == "human_message":
            await approved_source_ref(
                f"https://docs.google.com/document/d/{file_id}/edit",
                ctx,
                proof.get("message_id"),
            )
            return
        if proof.get("kind") != "output":
            raise ValueError("The frozen Doc source authority is invalid.")
        latest = await self.repo.output_destination(ctx.chat.key, ctx.thread_id, file_id)
        if (
            latest is None
            or latest["action_id"] != proof.get("action_id")
            or latest["output_owner_id"] != proof.get("owner_id")
            or latest["output_owner_email"] != proof.get("owner_email")
            or latest["output_thread_id"] != proof.get("thread_id")
            or latest["output_mode"] != proof.get("mode")
        ):
            raise ValueError("The retained output binding was removed or changed.")
        if proof.get("owner_email", "").casefold() != self.settings.owner_email.casefold():
            raise ValueError("Google owner configuration changed; request a fresh Doc preview.")
        owner = await self.client.identity()
        if owner != proof.get("owner_id"):
            raise ValueError("Google owner identity changed; request a fresh Doc preview.")
        metadata = await self.client.metadata(file_id)
        self._check_output_doc(file_id, metadata, owner, write=write)
        latest_after_io = await self.repo.output_destination(ctx.chat.key, ctx.thread_id, file_id)
        if not _same_output_binding(latest_after_io, latest):
            raise ValueError("The retained output binding was removed or changed during the read.")

    @staticmethod
    def check_metadata(file_id, metadata):
        if metadata.get("id", file_id) != file_id or metadata.get("trashed"):
            raise ValueError("This file changed or is in the trash; post an accessible file link.")
        if metadata.get("mimeType") in {
            "application/vnd.google-apps.folder",
            "application/vnd.google-apps.shortcut",
        }:
            raise ValueError("Post the actual file link; folders and shortcuts are not supported.")

    async def file(self, ctx, url, expected=None, *, allow_docx=False, source_message_ref=None):
        ref, metadata, owner = await self.source_file(
            ctx, url, source_message_ref=source_message_ref
        )
        self.check_type(metadata, expected, allow_docx=allow_docx)
        return ref, metadata, owner

    async def read_file(self, ctx, url, *, source_message_ref=None):
        ref = parse_google_ref(url)
        if ref.kind in {"document", "drive_file"} and await self.repo.output_destination(
            ctx.chat.key, ctx.thread_id, ref.file_id
        ):
            ref, metadata, _, proof = await self.doc_source(
                ctx, url, source_message_ref=source_message_ref
            )
            content = await self.read_content(ref, metadata)
            await self.recheck_doc_source(ctx, ref.file_id, proof)
            return content
        ref, metadata, _ = await self.source_file(ctx, url, source_message_ref=source_message_ref)
        return await self.read_content(ref, metadata)

    async def read_content(self, ref, metadata):
        """Internal dispatch: caller must verify human-link or saved-trip authority first."""
        mime = metadata.get("mimeType")
        if mime in (DOC, DOCX):
            return await self.read_document(ref, metadata)
        if mime == SHEET:
            return await self.read_spreadsheet(ref, metadata)
        if mime not in ("application/pdf", "text/csv"):
            return (
                str(metadata.get("name", "File"))[:300]
                + "\nContent reading is not supported for this format; metadata only. "
                "Content was not read."
            )
        if metadata.get("capabilities", {}).get("canDownload") is not True:
            raise ValueError("File download is not permitted; check the owner's download access.")
        size = metadata.get("size")
        if size is not None:
            try:
                size = int(size)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("File size metadata is invalid.") from None
            if size < 0 or size > self.settings.max_bytes:
                raise ValueError("File exceeds the download size limit.")
        from chatcore_google.file_readers import read_csv, read_pdf

        data = await self.client.media(ref.file_id)
        if len(data) > self.settings.max_bytes:
            raise ValueError("File exceeds the download size limit.")
        text = (
            await read_pdf(data, max_chars=self.settings.max_chars)
            if mime == "application/pdf"
            else read_csv(data, self.settings)
        )
        return str(metadata.get("name", "File"))[:300] + "\n" + text

    @staticmethod
    def check_type(metadata, expected=None, *, allow_docx=False):
        mime = metadata.get("mimeType", "unknown")
        if mime == "text/csv":
            raise ValueError("Please attach the CSV in chat; Drive CSV downloads are not enabled.")
        if mime == DOCX and allow_docx and expected in (None, DOC):
            return
        if mime not in (DOC, SHEET):
            raise ValueError(
                "This operation requires a native Google Doc/Sheet. Drive DOCX is read-only; "
                "Word writes, Excel and other formats are not supported."
            )
        if expected is not None and mime != expected:
            raise ValueError("The file type does not match this tool; use the Doc or Sheet reader.")

    async def read_doc(self, ctx, url, *, source_message_ref=None):
        ref, metadata, _, proof = await self.doc_source(
            ctx, url, source_message_ref=source_message_ref
        )
        result = await self.read_document(ref, metadata)
        await self.recheck_doc_source(ctx, ref.file_id, proof)
        return result

    async def read_document(self, ref, metadata):
        if metadata["mimeType"] == DOCX:
            text = await self.read_docx(ref.file_id, metadata)
            return str(metadata.get("name", "Document"))[:300] + "\n" + text
        document = await self.client.document(ref.file_id)
        return (
            str(metadata.get("name", "Document"))[:300]
            + "\n"
            + doc_text(document, max_chars=self.settings.max_chars, tab_id=ref.doc_tab)
        )

    async def read_docx(self, file_id, metadata):
        """Internal content read; caller must authorize the chat/owner and source first."""
        self.check_type(metadata, DOC, allow_docx=True)
        if metadata.get("capabilities", {}).get("canDownload") is not True:
            raise ValueError("DOCX download is not permitted; check the owner's download access.")
        size = metadata.get("size")
        if size is not None:
            try:
                size = int(size)
            except (ValueError, TypeError, OverflowError):
                raise ValueError("DOCX file size metadata is invalid.") from None
            if size < 0 or size > self.settings.max_bytes:
                raise ValueError("DOCX file exceeds the download size limit.")
        return docx_text(await self.client.media(file_id), max_chars=self.settings.max_chars)

    async def tab_values(self, file_id, title, cell_range=None, *, grid=None):
        grid = grid or {}
        columns = min(self.settings.max_columns, grid.get("columnCount", self.settings.max_columns))
        rows_limit = min(
            self.settings.max_rows + 2, grid.get("rowCount", self.settings.max_rows + 2)
        )
        if cell_range is None:
            cell_range = f"A1:{column_name(columns)}{rows_limit}"
        match = re.fullmatch(
            r"([A-Z]{1,3})([1-9][0-9]*)(?::([A-Z]{1,3})([1-9][0-9]*))?", cell_range
        )
        if not match:
            raise ValueError("Use a bounded A1 cell range, e.g. A1:H100, without a tab prefix.")
        c1, r1, c2, r2 = match.groups()
        width = column_number(c2 or c1) - column_number(c1) + 1
        height = int(r2 or r1) - int(r1) + 1
        if (
            not 0 < width <= self.settings.max_columns
            or not 0 < height <= self.settings.max_rows + 2
        ):
            raise ValueError("That range exceeds the configured row/column limit.")
        if column_number(c2 or c1) > grid.get("columnCount", 18278) or int(r2 or r1) > grid.get(
            "rowCount", 10_000_000
        ):
            raise ValueError("That range exceeds the target tab's row/column grid.")
        quoted = "'" + title.replace("'", "''") + "'!" + cell_range
        data = await self.client.values(file_id, quoted)
        rows = data.get("values", [])
        if not isinstance(rows, list):
            raise ValueError("Google returned invalid spreadsheet values.")
        return rows[: self.settings.max_rows + 2]

    async def read_sheet(self, ctx, url, cell_range=None, tab=None, *, source_message_ref=None):
        ref, metadata, _ = await self.file(ctx, url, SHEET, source_message_ref=source_message_ref)
        return await self.read_spreadsheet(ref, metadata, cell_range=cell_range, tab=tab)

    async def read_spreadsheet(self, ref, metadata, *, cell_range=None, tab=None):
        sheet = await self.client.sheet(ref.file_id)
        selected = select_tab(sheet, ref, tab)
        values = await self.tab_values(
            ref.file_id, selected["title"], cell_range, grid=selected.get("gridProperties")
        )
        text = json.dumps(values, ensure_ascii=False)
        truncated = len(text) > self.settings.max_chars or len(values) >= self.settings.max_rows + 2
        return (
            str(metadata.get("name", "Sheet"))[:300]
            + " — "
            + selected["title"][:100]
            + "\n"
            + text[: self.settings.max_chars]
            + ("\n[Truncated: partial range coverage]" if truncated else "")
        )

    async def action(self, ctx, kind, payload, summary):
        action = PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin="google",
            kind=kind,
            payload=copy.deepcopy(payload),
            summary=summary,
            thread_id=ctx.thread_id,
        )
        if len(summary) > 3500:
            if not isinstance(ctx.platform, FilePlatform):
                raise ValueError(
                    "The preview is too large for this platform; request a smaller batch."
                )
            filename = "google-write-preview-" + action.id + ".txt"
            await ctx.platform.send_file(
                ctx.chat,
                filename,
                summary.encode("utf-8"),
                caption="Full preview. Review before confirming; no Google change has been made.",
                thread_id=ctx.thread_id,
            )
            action = replace(
                action,
                summary=summary[:1600]
                + "\n\n[Excerpt]\n"
                + f"Full batch attached as {filename}. Confirm writes the entire attached batch.",
            )
        return action

    async def propose_configure(self, ctx, enabled):
        await self.authorize(ctx, configure=True)
        if type(enabled) is not bool:
            raise ValueError("Specify on or off.")
        _, revision = await self.repo.settings(ctx.chat.key)
        return await self.action(
            ctx,
            "configure",
            {"enabled": enabled, "revision": revision, "owner_email": self.settings.owner_email},
            f"Turn Google tools {'on' if enabled else 'off'} in this chat? "
            "This applies to Google and itinerary reads/writes.",
        )

    async def propose(
        self,
        ctx,
        kind,
        *,
        url=None,
        title=None,
        text=None,
        rows=None,
        tab=None,
        link_viewer=None,
        link_access=None,
    ):
        await self.authorize(ctx, write=True)
        if kind not in ("append", "create_doc", "create_sheet"):
            raise ValueError("Unknown Google write operation.")
        if kind != "create_doc" and (link_access is not None or link_viewer is not None):
            raise ValueError("Link access choices apply only to a new Google Doc.")
        payload = {"owner_email": self.settings.owner_email}
        grid_columns = None
        if kind == "append":
            ref, metadata, owner = await self.file(ctx, url)
            payload.update(file_id=ref.file_id, mime=metadata["mimeType"], doc_tab=ref.doc_tab)
            name = str(metadata.get("name", "File"))[:300]
            if metadata["mimeType"] == SHEET:
                selected = select_tab(await self.client.sheet(ref.file_id), ref, tab)
                payload.update(sheet_id=selected["sheetId"], tab_title=selected["title"])
                grid_columns = selected.get("gridProperties", {}).get("columnCount")
                name += " / " + selected["title"][:100]
            elif rows is not None:
                raise ValueError("Append text to a Doc and rows to a Sheet.")
            elif tab is not None:
                raise ValueError(
                    "For a Doc tab, use its tab URL; spreadsheet tab names are not valid."
                )
        else:
            owner = await self.client.identity()
            if not isinstance(title, str) or not title.strip() or len(title) > 200:
                raise ValueError("Provide a title of 1–200 characters.")
            payload["title"] = title
            payload["mime"] = DOC if kind == "create_doc" else SHEET
            name = title
            if kind == "create_doc":
                access = resolve_link_access(self.settings, link_access, link_viewer)
                payload["link_access"] = access
                if access != "editor":
                    payload["link_viewer"] = access == "viewer"
        if payload["mime"] == SHEET:
            if text is not None:
                raise ValueError("Provide rows for a Sheet.")
            payload["rows"] = self.rows(rows, grid_columns=grid_columns)
            preview = json.dumps(payload["rows"], ensure_ascii=False)
        else:
            if not isinstance(text, str) or not text or len(text) > self.settings.max_chars:
                raise ValueError("Provide nonempty text within the character limit.")
            payload["text"] = text
            preview = text
        _, revision = await self.repo.settings(ctx.chat.key)
        payload.update(owner=owner, revision=revision)
        # Long previews are attached in full by action(); the pending payload stays frozen.
        link_access = payload_link_access(payload) if kind == "create_doc" else None
        access_text = {
            "private": "private; existing access settings are unchanged",
            "viewer": "anyone with the link can view; search discovery is off",
            "editor": "anyone with the link can edit; search discovery is off",
        }
        link_line = f"Access: {access_text[link_access]}.\n" if kind == "create_doc" else ""
        target = (
            f"new Doc ({link_access} link access)"
            if kind == "create_doc"
            else "new private file"
            if kind == "create_sheet"
            else url
        )
        summary = (
            f"{kind.replace('_', ' ').title()}: {name}\n"
            + link_line
            + f"Owner: {self.settings.owner_email}\nTarget: {target}\n\n{preview}"
        )
        return await self.action(ctx, kind, payload, summary)

    async def verify_commit(self, action, ctx):
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
        await self.authorize(
            ctx, write=action.kind != "configure", configure=action.kind == "configure"
        )
        if (await self.repo.settings(ctx.chat.key))[1] != payload["revision"]:
            raise ValueError("Google settings changed; request again.")
        if action.kind != "configure" and await self.client.identity() != payload["owner"]:
            raise ValueError("Google owner identity changed; request again.")
        return payload

    async def reserve(self, action, ctx, *, suggestion_rows=0):
        await self.repo.reserve(
            ctx.chat.key,
            action.id,
            ctx.user.ref.key,
            suggestion_rows,
            self.settings.suggestion_rows_per_day,
            action.payload["revision"],
        )

    async def commit(self, action, ctx):
        payload = await self.verify_commit(action, ctx)
        if action.kind == "configure":
            await self.repo.configure(ctx.chat.key, payload["enabled"], payload["revision"])
            return "Google tools are " + ("on" if payload["enabled"] else "off") + " in this chat."
        if action.kind == "append":
            metadata = await self.client.metadata(payload["file_id"])
            self.check_type(metadata, payload["mime"])
            if payload["mime"] == SHEET:
                tabs = (await self.client.sheet(payload["file_id"])).get("sheets", [])
                selected = next(
                    (
                        tab["properties"]
                        for tab in tabs
                        if tab["properties"]["sheetId"] == payload["sheet_id"]
                        and tab["properties"]["title"] == payload["tab_title"]
                    ),
                    None,
                )
                if selected is None:
                    raise ValueError("The target tab changed; request again.")
                rows = self.rows(
                    payload["rows"],
                    grid_columns=selected.get("gridProperties", {}).get("columnCount"),
                )
                await self.reserve(action, ctx)
                await self.client.batch(
                    payload["file_id"],
                    [
                        {
                            "appendCells": {
                                "sheetId": payload["sheet_id"],
                                "rows": literal_rows(rows),
                                "fields": "userEnteredValue",
                            }
                        }
                    ],
                )
            else:
                if not 0 < len(payload["text"]) <= self.settings.max_chars:
                    raise ValueError(
                        "Document text exceeds the current character limit; request again."
                    )
                await self.reserve(action, ctx)
                await self.append_doc(payload["file_id"], payload["text"], payload.get("doc_tab"))
            return "Content appended to the confirmed file."
        if action.kind not in ("create_doc", "create_sheet"):
            raise ValueError("Unknown Google action.")
        if action.kind == "create_sheet":
            rows = self.rows(payload["rows"])
        elif not 0 < len(payload["text"]) <= self.settings.max_chars:
            raise ValueError("Document text exceeds the current character limit; request again.")
        if action.kind == "create_doc":
            return await self._create_output_doc(action, ctx, payload)
        await self.reserve(action, ctx)
        result = await self.client.request(
            "POST",
            "sheets.googleapis.com",
            "/v4/spreadsheets",
            json={
                "properties": {"title": payload["title"]},
                "sheets": [
                    {
                        "properties": {
                            "title": "Sheet1",
                            "gridProperties": {
                                "columnCount": max(map(len, rows)),
                                "rowCount": max(100, len(rows)),
                            },
                        }
                    }
                ],
            },
        )
        file_id = result["spreadsheetId"]
        sheet_id = result["sheets"][0]["properties"]["sheetId"]
        await self.client.batch(
            file_id,
            [
                {
                    "appendCells": {
                        "sheetId": sheet_id,
                        "rows": literal_rows(rows),
                        "fields": "userEnteredValue",
                    }
                }
            ],
        )
        return f"Created https://docs.google.com/spreadsheets/d/{file_id}/edit (sharing unchanged)."

    async def _create_output_doc(self, action, ctx, payload):
        """Create, journal, verify and optionally share a confirmed native Doc output."""
        await self.repo.reserve_edit(
            ctx.chat.key,
            action.id,
            ctx.user.ref.key,
            0,
            self.settings.suggestion_rows_per_day,
            payload["revision"],
            kind="create_doc",
            source_id=None,
            output_binding={
                "owner_id": payload["owner"],
                "owner_email": payload["owner_email"],
                "thread_id": action.thread_id,
                "mode": "create",
            },
        )
        await self.repo.claim_destination_creation(
            ctx.chat.key,
            action.id,
            kind="create_doc",
            source_id=None,
            revision=payload["revision"],
        )
        try:
            result = await self.client.request(
                "POST", "docs.googleapis.com", "/v1/documents", json={"title": payload["title"]}
            )
        except Exception as exc:
            uncertain = write_outcome_uncertain(exc)
            await record_result(
                self,
                action,
                ctx,
                phase="uncertain" if uncertain else "conflict",
                failed_stage="destination",
                error_category="remote_unknown" if uncertain else "refused",
            )
            return (
                "Uncertain: Google may have created the Doc without returning its ID; "
                "inspect Drive before retrying."
                if uncertain
                else "Google refused to create the Doc; no content was written."
            )
        destination_id = result.get("documentId") if isinstance(result, dict) else None
        if not isinstance(destination_id, str) or not 1 <= len(destination_id) <= 200:
            await record_result(
                self,
                action,
                ctx,
                phase="uncertain",
                failed_stage="destination",
                error_category="missing_id",
            )
            return (
                "Uncertain: Google may have created the Doc without returning a valid ID; "
                "inspect Drive."
            )
        safe_url = (
            web_url(destination_id)
            if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", destination_id)
            else None
        )
        result_record = await record_result(
            self,
            action,
            ctx,
            phase="destination_created",
            destination_id=destination_id,
        )
        if isinstance(result_record, Exception):
            location = f"{safe_url}" if safe_url else "the returned destination ID"
            return (
                f"Partial: Google created {location}, but its ID could not be durably journaled; "
                "no content or sharing change was attempted. Inspect Drive before retrying."
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", destination_id):
            await record_result(
                self,
                action,
                ctx,
                phase="partial",
                destination_id=destination_id,
                failed_stage="destination_validation",
                error_category="invalid_remote_id",
            )
            return (
                "Partial: Google returned a destination ID that cannot be opened safely; "
                "inspect Drive."
            )
        url = web_url(destination_id)
        try:
            metadata = await self.client.metadata(destination_id)
            self.check_metadata(destination_id, metadata)
            if metadata.get("mimeType") != DOC:
                raise ValueError("Google created an unexpected destination type.")
        except Exception:
            await record_result(
                self,
                action,
                ctx,
                phase="partial",
                destination_id=destination_id,
                failed_stage="destination_validation",
                error_category="inaccessible_or_wrong_type",
            )
            return (
                f"Partial: the Doc is at {url}; its destination could not be validated, "
                "so no content was written."
            )
        applying = await record_result(
            self,
            action,
            ctx,
            phase="applying",
            destination_id=destination_id,
            failed_stage="content_attempt",
        )
        if isinstance(applying, Exception):
            return (
                f"Partial: the Doc is at {url}; the content attempt could not be durably recorded, "
                "so no content was written."
            )
        append_error = None
        try:
            await self.append_doc(destination_id, payload["text"])
        except Exception as exc:
            append_error = exc
        verification = await verify_doc_readback(
            self.client,
            destination_id,
            payload["text"] + "\n",
            max_chars=self.settings.max_chars + 1,
            require_complete=True,
        )
        if not verification.verified:
            _logger.info(
                "Google Doc read-back verification failed category=%s attempts=%d",
                verification.category,
                verification.attempts,
            )
            uncertain = append_error is None or write_outcome_uncertain(append_error)
            await record_result(
                self,
                action,
                ctx,
                phase="uncertain" if uncertain else "partial",
                destination_id=destination_id,
                failed_stage="content_verification",
                error_category=verification.category,
            )
            return (
                f"{'Uncertain' if uncertain else 'Partial'}: the Doc is at {url}; content "
                f"verification failed ({verification.category}), so no link-access change "
                "was attempted. Inspect before retrying."
            )

        access = payload_link_access(payload)
        if access in {"viewer", "editor"}:
            try:
                shared, message = await publish_doc_link_access(
                    self, action, ctx, payload, destination_id
                )
            except Exception:
                await record_result(
                    self,
                    action,
                    ctx,
                    phase="uncertain",
                    destination_id=destination_id,
                    failed_stage="sharing_result",
                    error_category="journal_or_preflight_failure",
                )
                return (
                    f"Uncertain: the Doc is at {url}; its link-access result could not be "
                    "safely finalized. Inspect access before retrying."
                )
            if not shared:
                return message
        finalized = await record_result(
            self,
            action,
            ctx,
            phase="verified",
            destination_id=destination_id,
        )
        if isinstance(finalized, Exception):
            return (
                f"Uncertain: the Doc is at {url}; its content and link-viewer access were "
                "read-back verified, but final journal completion failed. Inspect before retrying."
            )
        suffix = {
            "private": "sharing unchanged",
            "viewer": "anyone with the link can view",
            "editor": "anyone with the link can edit",
        }[access]
        return f"Created and verified {url}; {suffix}."

    async def append_doc(self, file_id, text, tab_id=None):
        location = {"tabId": tab_id} if tab_id else {}
        return await self.client.request(
            "POST",
            "docs.googleapis.com",
            f"/v1/documents/{file_id}:batchUpdate",
            json={"requests": [{"insertText": {"endOfSegmentLocation": location, "text": text}}]},
        )
