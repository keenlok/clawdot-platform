"""Confirmed trip-file associations and narrowly scoped saved-source reads."""

import json

from chatcore.events import ChatRef

from chatcore_google.refs import parse_google_ref
from chatcore_google.service import DOC, DOCX, SHEET

READABLE = frozenset({DOC, DOCX, SHEET, "application/pdf", "text/csv"})


def display(value):
    return " ".join(str(value).split())[:200]


def canonical(ref):
    base = "https://drive.google.com/file/d/" + ref.file_id + "/view"
    if ref.kind == "document":
        base = "https://docs.google.com/document/d/" + ref.file_id + "/edit"
        if ref.doc_tab:
            base += "?tab=" + ref.doc_tab
    elif ref.kind == "spreadsheets":
        base = "https://docs.google.com/spreadsheets/d/" + ref.file_id + "/edit"
        if ref.gid is not None:
            base += "#gid=" + str(ref.gid)
    return base


class TripFilesService:
    def __init__(self, google, resolver):
        self.google, self.resolver = google, resolver

    async def trip(self, ctx, trip):
        await self.google.authorize(ctx)
        if self.resolver is None:
            raise ValueError("Trip files are unavailable in this deployment.")
        if not isinstance(trip, str) or not trip.strip():
            raise ValueError("Name an explicit existing trip; use list_trips if unsure.")
        value = await self.resolver(ctx, trip)
        scope = ChatRef.parse(value["chat_key"])
        if scope.platform != ctx.chat.platform or (not ctx.is_dm and scope != ctx.chat):
            raise PermissionError("The resolved trip belongs to another chat scope.")
        if ctx.is_dm and not self.google.access.is_chat_allowed(scope):
            raise PermissionError("The trip group is not allowed.")
        enabled, revision = await self.google.repo.settings(scope.key)
        if not enabled:
            raise PermissionError("Google tools are off in this trip group.")
        return dict(value, group_revision=revision)

    async def propose_attach(self, ctx, url, trip, role):
        await self.google.authorize(ctx, write=True)
        if role not in {"itinerary", "reference"}:
            raise ValueError("Choose itinerary or reference.")
        resolved = await self.trip(ctx, trip)
        ref, metadata, owner = await self.google.source_file(ctx, url)
        repo = self.google.repo
        current = await repo.trip_file(resolved["chat_key"], ref.file_id)
        if current and current["trip_id"] != resolved["trip_id"]:
            raise ValueError("This file belongs to another trip; remove that association first.")
        rows = await repo.trip_files(resolved["chat_key"], resolved["trip_id"])
        if current is None and len(rows) >= self.google.settings.max_rows:
            raise ValueError("This trip has reached its attached-file limit.")
        data = {
            "url": canonical(ref),
            "name": display(metadata.get("name", "File")),
            "mime": metadata.get("mimeType", "unknown"),
            "role": role,
            "owner": owner,
            "requester": ctx.user.ref.key,
        }
        if current and all(current[key] == data[key] for key in ("url", "role", "owner", "mime")):
            raise ValueError("This file is already attached with that role.")
        previous = next(
            (r for r in rows if r["role"] == "itinerary" and r["file_id"] != ref.file_id), None
        )
        note = (
            "\nContent reading is not supported for this format; save as a ref"
            "erence to open manually."
            if data["mime"] not in READABLE
            else "\nContent has not been read by this attachment action."
        )
        if previous and role == "itinerary":
            note += "\nPrevious itinerary becomes a reference: " + display(previous["name"])
        _, revision = await repo.settings(ctx.chat.key)
        return await self.google.action(
            ctx,
            "trip_attach",
            {
                "owner_email": self.google.settings.owner_email,
                "owner": owner,
                "revision": revision,
                "trip_id": resolved["trip_id"],
                "trip_chat": resolved["chat_key"],
                "group_revision": resolved["group_revision"],
                "file_id": ref.file_id,
                "data": data,
                "expected": repo.file_state(rows),
            },
            f"Attach {data['name']} to {display(resolved['trip_name'])} as {role}?\n{data['url']}"
            + note,
        )

    async def list(self, ctx, trip):
        resolved = await self.trip(ctx, trip)
        rows = await self.google.repo.trip_files(resolved["chat_key"], resolved["trip_id"])
        result = {"trip": resolved["trip_name"], "files": [], "partial": False}
        for row in rows:
            result["files"].append({k: row[k] for k in ("file_id", "name", "url", "mime", "role")})
            result["files"][-1]["readable"] = row["mime"] in READABLE and row["owner"] is not None
            if len(json.dumps(result, ensure_ascii=False)) > self.google.settings.max_chars:
                result["files"].pop()
                result["partial"] = True
                break
        return json.dumps(result, ensure_ascii=False)

    async def read(self, ctx, trip, file_id=None):
        resolved = await self.trip(ctx, trip)
        rows = await self.google.repo.trip_files(resolved["chat_key"], resolved["trip_id"])
        row = (
            next((r for r in rows if r["file_id"] == file_id), None)
            if file_id
            else next((r for r in rows if r["role"] == "itinerary"), None)
        )
        if row is None:
            raise ValueError(
                "No matching attached file or primary itinerary; list trip files and choose one."
            )
        if not row["owner"] or row["owner"] != await self.google.client.identity():
            raise ValueError("Saved file owner identity needs a fresh confirmed attachment.")
        ref = parse_google_ref(row["url"])
        if ref.file_id != row["file_id"]:
            raise ValueError("Saved file identity changed; attach it again.")
        metadata = await self.google.client.metadata(row["file_id"])
        self.google.check_metadata(row["file_id"], metadata)
        if metadata.get("mimeType") != row["mime"]:
            raise ValueError("Saved file type changed; attach it again.")
        # Recheck the association after network access before using its saved authority.
        latest = await self.google.repo.trip_file(resolved["chat_key"], row["file_id"])
        if latest != row:
            raise ValueError("Trip file association changed; request a fresh read.")
        content = await self.google.read_content(ref, metadata)
        refreshed = await self.trip(ctx, trip)
        if row["owner"] != await self.google.client.identity():
            raise ValueError("Saved file owner identity changed; attach it again.")
        latest = await self.google.repo.trip_file(resolved["chat_key"], row["file_id"])
        if latest != row or refreshed != resolved:
            raise ValueError("Trip file association or settings changed; request a fresh read.")
        return content

    async def propose_remove(self, ctx, trip, file_id):
        await self.google.authorize(ctx, write=True)
        resolved = await self.trip(ctx, trip)
        rows = await self.google.repo.trip_files(resolved["chat_key"], resolved["trip_id"])
        row = next((r for r in rows if r["file_id"] == file_id), None)
        if row is None:
            raise ValueError("This file is not attached to that trip.")
        _, revision = await self.google.repo.settings(ctx.chat.key)
        return await self.google.action(
            ctx,
            "trip_remove",
            {
                "owner_email": self.google.settings.owner_email,
                "owner": await self.google.client.identity(),
                "revision": revision,
                "trip_id": resolved["trip_id"],
                "trip_chat": resolved["chat_key"],
                "group_revision": resolved["group_revision"],
                "file_id": file_id,
                "expected": self.google.repo.file_state(rows),
            },
            f"Remove {display(row['name'])} from {display(resolved['trip_name'])}?\n"
            "Only its trip association is removed; the Drive file remains.",
        )

    async def commit(self, action, ctx):
        payload = await self.google.verify_commit(action, ctx)
        if action.kind not in {"trip_attach", "trip_remove"}:
            raise ValueError("Unknown trip file action.")
        resolved = await self.trip(ctx, payload["trip_id"])
        if (
            resolved["chat_key"] != payload["trip_chat"]
            or resolved["trip_id"] != payload["trip_id"]
            or resolved["group_revision"] != payload["group_revision"]
        ):
            raise ValueError("Trip group/scope/settings changed; request again.")
        data = payload.get("data")
        if data is not None:
            metadata = await self.google.client.metadata(payload["file_id"])
            self.google.check_metadata(payload["file_id"], metadata)
            if metadata.get("mimeType", "unknown") != data["mime"]:
                raise ValueError("File type changed; request again.")
        await self.google.reserve(action, ctx)
        await self.google.repo.apply_file(
            payload["trip_chat"],
            payload["trip_id"],
            payload["file_id"],
            data,
            payload["expected"],
            revision=payload["group_revision"],
        )
        return (
            "File attached to the confirmed trip."
            if data is not None
            else "File removed from the trip; the Drive file was kept."
        )
