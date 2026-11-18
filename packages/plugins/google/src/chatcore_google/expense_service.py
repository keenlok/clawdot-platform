"""Expense exports and explicit trip links through an app-injected read-only provider."""

import csv
import io
from dataclasses import replace

from chatcore.events import ChatRef

from chatcore_google.refs import parse_google_ref
from chatcore_google.service import SHEET
from chatcore_google.workbook import render_workbook, write_workbook


class ExpenseService:
    def __init__(self, google, provider=None, workbook_provider=None):
        self.google, self.provider = google, provider
        self.workbook_provider = workbook_provider

    @property
    def available(self):
        return self.provider is not None or self.workbook_provider is not None

    async def workbook(self, ctx, trip=None, general=False, title=None):
        """Read one ledger snapshot through the app provider and render it, without writing."""
        book = await self.workbook_provider(ctx, trip, general)
        final = title or book["workbook"].get("title") or "Expenses"
        plan = render_workbook(
            book["workbook"],
            title=str(final)[:200],
            max_cells=self.google.settings.export_max_cells,
            max_cell_chars=self.google.settings.max_cell_chars,
        )
        return book, plan

    async def ledger(self, ctx, trip=None, general=False):
        if self.provider is None:
            raise ValueError("Expense export/linking is unavailable in this deployment.")
        return await self.provider(ctx, trip, general)

    async def propose_export(self, ctx, *, title=None, trip=None, general=False):
        if self.workbook_provider is not None:
            return await self.propose_workbook(ctx, title=title, trip=trip, general=general)
        await self.google.authorize(ctx, write=True)
        ledger = await self.ledger(ctx, trip, general)
        if len(ledger["csv"]) > self.google.settings.max_bytes:
            raise ValueError("The expense export exceeds the size limit; export a smaller trip.")
        rows = list(csv.reader(io.StringIO(ledger["csv"].decode("utf-8-sig"))))
        action = await self.google.propose(
            ctx,
            "create_sheet",
            title=title or ("Expenses — " + ledger["trip_name"][:150]),
            rows=rows,
        )
        action.payload.update(ledger_chat=ledger["chat_key"], ledger_trip=ledger["trip_id"])
        return replace(action, kind="expenses_export")

    async def propose_workbook(self, ctx, *, title=None, trip=None, general=False):
        await self.google.authorize(ctx, write=True)
        if title is not None and (
            not isinstance(title, str) or not title.strip() or len(title) > 200
        ):
            raise ValueError("Provide a title of 1–200 characters.")
        book, plan = await self.workbook(ctx, trip, general, title)
        owner = await self.google.client.identity()
        _, revision = await self.google.repo.settings(ctx.chat.key)
        payload = {
            "owner_email": self.google.settings.owner_email,
            "owner": owner,
            "revision": revision,
            "title": title,
            "workbook": 1,
            "ledger_chat": book["chat_key"],
            "ledger_trip": book["trip_id"],
        }
        summary = (
            f"Create formatted expense workbook: {plan.title}\n"
            f"Owner: {self.google.settings.owner_email}\n"
            "Target: new private Google Sheet (sharing unchanged)\n\n"
            f"{book['preview']}\n\n"
            "The ledger is read again when you confirm, so changes made before then are "
            "included. The file is a snapshot and will not update afterwards."
        )
        action = await self.google.action(ctx, "expenses_export", payload, summary)
        return action

    async def commit_workbook(self, action, ctx, payload):
        book, plan = await self.workbook(
            ctx, payload["ledger_trip"], payload["ledger_trip"] is None, payload.get("title")
        )
        if book["chat_key"] != payload["ledger_chat"] or book["trip_id"] != payload["ledger_trip"]:
            raise ValueError("Expense group/scope changed; request a fresh export.")
        await self.google.reserve(action, ctx)
        url, failure = await write_workbook(self.google.client, plan)
        file_id = parse_google_ref(url).file_id
        await self.google.repo.record_output(
            ctx.chat.key,
            file_id,
            SHEET,
            ctx.user.ref.key,
            action.thread_id,
            payload["owner"],
            action.id,
        )
        if failure:
            return (
                f"Created {url}, but the workbook is incomplete ({failure}). Its title ends "
                "with “(incomplete)”. Nothing was retried; check or delete it, then request a "
                "new export."
            )
        return (
            f"Created {url}: {book['expense_count']} expenses and {book['settlement_count']} "
            f"recorded repayments, snapshot {book.get('snapshot', 'at confirmation')}. "
            "Private to the Google owner (sharing unchanged). It is a snapshot and does not "
            "update when the ledger changes."
        )

    async def propose_link(self, ctx, url, trip):
        await self.google.authorize(ctx, write=True)
        ref, metadata, owner = await self.google.file(ctx, url, SHEET)
        ledger = await self.ledger(ctx, trip, False)
        if not ledger["trip_id"]:
            raise ValueError("Link an explicit trip, not the general ledger.")
        scope = ChatRef.parse(ledger["chat_key"])
        if scope.platform != ctx.chat.platform or (not ctx.is_dm and scope != ctx.chat):
            raise PermissionError("The trip belongs to another chat scope.")
        if ctx.is_dm and not self.google.access.is_chat_allowed(scope):
            raise PermissionError("The trip group is not allowed.")
        enabled, group_revision = await self.google.repo.settings(scope.key)
        if not enabled:
            raise PermissionError("Google tools are off in this trip group.")
        rows = await self.google.repo.trip_files(scope.key, ledger["trip_id"])
        current = await self.google.repo.trip_file(scope.key, ref.file_id)
        if current and current["trip_id"] != ledger["trip_id"]:
            raise ValueError("This file belongs to another trip; remove that association first.")
        _, revision = await self.google.repo.settings(ctx.chat.key)
        return await self.google.action(
            ctx,
            "itinerary_link",
            {
                "file_id": ref.file_id,
                "owner": owner,
                "owner_email": self.google.settings.owner_email,
                "revision": revision,
                "ledger_chat": ledger["chat_key"],
                "ledger_trip": ledger["trip_id"],
                "expected_files": self.google.repo.file_state(rows),
                "group_revision": group_revision,
                "data": {
                    "url": "https://docs.google.com/spreadsheets/d/" + ref.file_id + "/edit",
                    "name": str(metadata.get("name", "Trip"))[:200],
                    "mime": SHEET,
                    "role": current["role"] if current else "reference",
                    "owner": owner,
                    "requester": ctx.user.ref.key,
                },
            },
            f"Link {str(metadata.get('name', 'Trip'))[:200]} to trip "
            f"{ledger['trip_name'][:200]}?\nTarget: {url}\nAll day/city tabs use this trip link.",
        )

    async def commit(self, action, ctx):
        payload = await self.google.verify_commit(action, ctx)
        if action.kind not in {"expenses_export", "itinerary_link"}:
            raise ValueError("Unknown expense export action.")
        if action.kind == "expenses_export" and payload.get("workbook"):
            if self.workbook_provider is None:
                raise ValueError("Workbook export is unavailable in this deployment.")
            return await self.commit_workbook(action, ctx, payload)
        current = await self.ledger(ctx, payload["ledger_trip"], payload["ledger_trip"] is None)
        if (
            current["chat_key"] != payload["ledger_chat"]
            or current["trip_id"] != payload["ledger_trip"]
        ):
            raise ValueError("Expense group/scope changed; request a fresh export or link.")
        if action.kind == "expenses_export":
            return await self.google.commit(replace(action, kind="create_sheet"), ctx)
        if "expected_files" not in payload or "group_revision" not in payload:
            raise ValueError("This older link confirmation needs a fresh request.")
        scope = ChatRef.parse(payload["ledger_chat"])
        if not ctx.is_dm and scope != ctx.chat:
            raise PermissionError("Trip chat scope changed; request again.")
        if ctx.is_dm and not self.google.access.is_chat_allowed(scope):
            raise PermissionError("The trip group is not allowed.")
        metadata = await self.google.client.metadata(payload["file_id"])
        self.google.check_metadata(payload["file_id"], metadata)
        self.google.check_type(metadata, SHEET)
        await self.google.reserve(action, ctx)
        await self.google.repo.apply_file(
            payload["ledger_chat"],
            payload["ledger_trip"],
            payload["file_id"],
            payload["data"],
            payload["expected_files"],
            revision=payload["group_revision"],
        )
        return "Linked this spreadsheet to the confirmed trip (all day/city tabs)."
