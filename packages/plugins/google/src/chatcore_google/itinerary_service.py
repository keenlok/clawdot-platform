"""Itinerary snapshots and confirmed new-tab/append-only proposals.

The main agent normalizes source/research data into rows. This service owns deterministic
validation and mutation; model-written instructions never grant permission or skip a click.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from decimal import Decimal

from chatcore_google.itinerary import HEADER, VERSION, normalize_groups, summarize, tracker_rows
from chatcore_google.refs import approved_ref
from chatcore_google.render import literal_rows
from chatcore_google.service import DOC, DOCX, SHEET, select_tab


def fingerprint(snapshot):
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


class ItineraryService:
    def __init__(self, google, *, attachment_reader=None, expenses=None):
        self.google = google
        self.attachment_reader, self.expenses = attachment_reader, expenses

    async def snapshot(self, file_id, *, ref=None, tab=None):
        sheet = await self.google.client.sheet(file_id)
        all_tabs = [item["properties"] for item in sheet.get("sheets", [])]
        if tab is not None or ref is not None and ref.gid is not None:
            all_tabs = [select_tab(sheet, ref, tab)]
        partial = len(all_tabs) > self.google.settings.max_tabs
        result, ignored = [], []
        remaining = self.google.settings.max_rows
        for props in all_tabs[: self.google.settings.max_tabs]:
            if remaining <= 0:
                partial = True
                break
            values = await self.google.tab_values(
                file_id, props["title"], grid=props.get("gridProperties")
            )
            try:
                rows = tracker_rows(values)
            except ValueError:
                ignored.append(props["title"])
                continue
            if len(rows) > remaining or len(values) >= self.google.settings.max_rows + 2:
                partial = True
            result.append(
                {"id": props["sheetId"], "title": props["title"], "rows": rows[:remaining]}
            )
            remaining -= len(rows)
        return {"tabs": result, "ignored": ignored, "partial": partial, "properties": all_tabs}

    async def summary(self, ctx, url, tab=None, *, source_message_ref=None):
        ref, _, _ = await self.google.file(ctx, url, SHEET, source_message_ref=source_message_ref)
        snapshot = await self.snapshot(ref.file_id, ref=ref, tab=tab)
        if not snapshot["tabs"]:
            raise ValueError(
                "No recognized itinerary tabs; expected chatcore-itinerary v1 header/version."
            )
        result = summarize(
            [(value["title"], value["rows"]) for value in snapshot["tabs"]],
            partial=snapshot["partial"],
        )
        result["ignored_non_tracker_tabs"] = snapshot["ignored"]
        result["coverage"] = [value["title"] for value in snapshot["tabs"]]
        trip_id = await self.google.repo.linked_trip(ctx.chat.key, ref.file_id)
        if trip_id and self.expenses:
            ledger = await self.expenses(ctx, trip_id, False)
            result["actual_totals"] = ledger["totals"]
            if result["totals_partial"] or tab is not None or ref.gid is not None:
                result["comparison_note"] = (
                    "Actual totals cover the whole trip; this plan selection/costs "
                    "are incomplete, so no planned-minus-actual comparison is shown."
                )
            else:
                result["planned_minus_actual"] = {
                    code: str(
                        Decimal(result["planned_totals"].get(code, "0"))
                        - Decimal(ledger["totals"].get(code, "0"))
                    )
                    for code in result["planned_totals"].keys() | ledger["totals"].keys()
                }
            result["expenses_trip"] = ledger["trip_name"]
        text = json.dumps(result, ensure_ascii=False)
        if len(text) > self.google.settings.max_chars:
            result["partial"] = True
            result["totals_partial"] = True
            result.pop("planned_minus_actual", None)
            text = json.dumps(result, ensure_ascii=False)[: self.google.settings.max_chars]
            text += "\n[Truncated: partial itinerary coverage and totals]"
        return text

    async def propose_import(
        self, ctx, groups, *, source_url=None, source_message_ref=None, target=None, title=None
    ):
        if bool(source_url) == bool(source_message_ref):
            raise ValueError(
                "Provide exactly one source: a Doc/Sheet/DOCX URL or CSV message reference."
            )
        await self.google.authorize(ctx, write=True)
        if source_url:
            approved_ref(source_url, ctx.turn.human_urls)
        if target:
            approved_ref(target, ctx.turn.human_urls)
        source_id = None
        if source_url:
            ref, metadata, _ = await self.google.file(ctx, source_url, allow_docx=True)
            source_id = ref.file_id
            if metadata["mimeType"] in (DOC, DOCX):
                await self.google.read_doc(ctx, source_url)
            else:
                await self.google.read_sheet(ctx, source_url)
        else:
            if self.attachment_reader is None:
                raise ValueError("CSV attachment reading is unavailable.")
            result = await self.attachment_reader(ctx, source_message_ref)
            if result.is_error:
                raise ValueError(result.content)
        normalized = normalize_groups(groups, self.google.settings)
        return await self._propose(
            ctx, "itinerary_import", normalized, target=target, title=title, source_id=source_id
        )

    async def propose_suggest(self, ctx, url, groups):
        await self.google.authorize(ctx, write=True)
        approved_ref(url, ctx.turn.human_urls)
        normalized = normalize_groups(
            groups,
            self.google.settings,
            suggest=True,
            sources=ctx.turn.seen_urls | set(ctx.turn.human_urls),
        )
        return await self._propose(ctx, "itinerary_suggest", normalized, target=url)

    async def _propose(self, ctx, kind, groups, *, target=None, title=None, source_id=None):
        _, revision = await self.google.repo.settings(ctx.chat.key)
        payload = {
            "owner_email": self.google.settings.owner_email,
            "revision": revision,
            "source_file_id": source_id,
            "groups": groups,
        }
        properties = []
        snapshot = None
        if target:
            ref, metadata, owner = await self.google.file(ctx, target, SHEET)
            payload.update(file_id=ref.file_id, file_name=str(metadata.get("name", "Trip"))[:200])
            sheet = await self.google.client.sheet(ref.file_id)
            properties = [tab["properties"] for tab in sheet.get("sheets", [])]
            if kind == "itinerary_suggest":
                snapshot = await self.snapshot(ref.file_id)
                if snapshot["partial"]:
                    raise ValueError(
                        "The trip exceeds read limits; reduce it before appending suggestions."
                    )
                payload["snapshot"] = fingerprint(snapshot)
        else:
            if not isinstance(title, str) or not title.strip() or len(title) > 200:
                raise ValueError("Provide a trip title when creating its spreadsheet.")
            owner = await self.google.client.identity()
            payload["title"] = title
        payload["owner"] = owner
        used_ids = {props["sheetId"] for props in properties}
        by_name = {props["title"].casefold(): props for props in properties}
        existing_rows = [row for tab in snapshot["tabs"] for row in tab["rows"]] if snapshot else []
        known = {tab["id"] for tab in snapshot["tabs"]} if snapshot else set()
        identities = {
            (str(row[0]).casefold(), str(row[1]).casefold(), str(row[2]).casefold())
            for row in existing_rows
            if len(row) >= 3 and any(str(value).strip() for value in row)
        }
        for group in groups:
            existing = by_name.get(group["title"].casefold())
            if existing:
                if kind == "itinerary_import":
                    raise ValueError(
                        "A destination tab name already exists; choose a new tab name."
                    )
                if existing["sheetId"] not in known:
                    raise ValueError("The destination tab has an unrecognized header/version.")
                group.update(id=existing["sheetId"], new=False, title=existing["title"])
            else:
                new_id = secrets.randbelow(2**31 - 1)
                while new_id in used_ids:
                    new_id = secrets.randbelow(2**31 - 1)
                used_ids.add(new_id)
                group.update(id=new_id, new=True)
            if kind == "itinerary_suggest":
                for row in group["rows"]:
                    key = tuple(value.casefold() for value in row[:3])
                    if key in identities:
                        raise ValueError(
                            "A suggestion repeats an existing/proposed itinerary item."
                        )
                    identities.add(key)
        if len(properties) + sum(group["new"] for group in groups) > self.google.settings.max_tabs:
            raise ValueError("The destination would exceed the tab limit; use fewer tabs.")
        preview = json.dumps(
            [
                {key: value for key, value in group.items() if key in {"title", "rows"}}
                for group in groups
            ],
            ensure_ascii=False,
        )
        summary = (
            f"{kind.replace('_', ' ').title()}\nOwner: {self.google.settings.owner_email}\n"
            f"Trip spreadsheet: {target or title}\n"
            f"New/appended day/city tabs and exact rows:\n{preview}"
        )
        return await self.google.action(ctx, kind, payload, summary)

    async def commit(self, action, ctx):
        payload = await self.google.verify_commit(action, ctx)
        if action.kind not in {"itinerary_import", "itinerary_suggest"}:
            raise ValueError("Unknown itinerary action.")
        groups = payload["groups"]
        if len(groups) > self.google.settings.max_tabs:
            raise ValueError("This batch exceeds the current tab limit; request a fresh preview.")
        self.google.rows([row for group in groups for row in group["rows"]])
        if (
            action.kind == "itinerary_suggest"
            and sum(len(group["rows"]) for group in groups)
            > self.google.settings.suggestion_rows_per_call
        ):
            raise ValueError("This batch exceeds the current suggestion row limit; request again.")
        if payload.get("source_file_id"):
            source = await self.google.client.metadata(payload["source_file_id"])
            self.google.check_type(source, allow_docx=True)
            if source["mimeType"] == DOCX:
                await self.google.read_docx(payload["source_file_id"], source)
        file_id = payload.get("file_id")
        if file_id:
            metadata = await self.google.client.metadata(file_id)
            self.google.check_type(metadata, SHEET)
            props = [
                tab["properties"] for tab in (await self.google.client.sheet(file_id))["sheets"]
            ]
            if len(props) + sum(group["new"] for group in groups) > self.google.settings.max_tabs:
                raise ValueError("The destination would exceed the tab limit; request again.")
            for group in groups:
                if group["new"]:
                    if any(
                        tab["sheetId"] == group["id"]
                        or tab["title"].casefold() == group["title"].casefold()
                        for tab in props
                    ):
                        raise ValueError(
                            "A target tab changed or now exists; request a fresh preview."
                        )
                elif not any(
                    tab["sheetId"] == group["id"] and tab["title"] == group["title"]
                    for tab in props
                ):
                    raise ValueError("The destination tab changed; request a fresh preview.")
            if action.kind == "itinerary_suggest":
                current = await self.snapshot(file_id)
                if fingerprint(current) != payload["snapshot"]:
                    raise ValueError(
                        "The itinerary changed; request a fresh preview before appending."
                    )
        count = (
            sum(len(group["rows"]) for group in groups) if action.kind == "itinerary_suggest" else 0
        )
        await self.google.reserve(action, ctx, suggestion_rows=count)
        is_new_file = not file_id
        if is_new_file:
            created = await self.google.client.request(
                "POST",
                "sheets.googleapis.com",
                "/v4/spreadsheets",
                json={
                    "properties": {"title": payload["title"]},
                    "sheets": [
                        {
                            "properties": {
                                "sheetId": group["id"],
                                "title": group["title"],
                                "gridProperties": {
                                    "columnCount": len(HEADER),
                                    "rowCount": max(100, len(group["rows"]) + 2),
                                },
                            }
                        }
                        for group in groups
                    ],
                },
            )
            file_id = created["spreadsheetId"]
        requests = []
        for group in groups:
            rows = [VERSION, HEADER, *group["rows"]] if group["new"] else group["rows"]
            if group["new"] and not is_new_file:
                requests.append(
                    {
                        "addSheet": {
                            "properties": {
                                "sheetId": group["id"],
                                "title": group["title"],
                                "gridProperties": {
                                    "columnCount": len(HEADER),
                                    "rowCount": max(100, len(group["rows"]) + 2),
                                },
                            }
                        }
                    }
                )
                requests.append(
                    {
                        "updateCells": {
                            "start": {"sheetId": group["id"], "rowIndex": 0, "columnIndex": 0},
                            "rows": literal_rows(rows),
                            "fields": "userEnteredValue",
                        }
                    }
                )
            else:
                requests.append(
                    {
                        "appendCells": {
                            "sheetId": group["id"],
                            "rows": literal_rows(rows),
                            "fields": "userEnteredValue",
                        }
                    }
                )
        await self.google.client.batch(file_id, requests)
        return f"Saved the confirmed itinerary rows: https://docs.google.com/spreadsheets/d/{file_id}/edit"
