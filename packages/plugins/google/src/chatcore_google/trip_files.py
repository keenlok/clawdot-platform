"""Confirmed trip-file associations and narrowly scoped saved-source reads."""

import json

from chatcore.events import ChatRef

from chatcore_google.refs import approved_source_ref, parse_google_ref
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

    async def propose_attach(self, ctx, url, trip, role, source_message_ref=None):
        await self.google.authorize(ctx, write=True)
        if role not in {"itinerary", "reference"}:
            raise ValueError("Choose itinerary or reference.")
        resolved = await self.trip(ctx, trip)
        ref, metadata, owner = await self.google.source_file(
            ctx, url, source_message_ref=source_message_ref
        )
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
                "source_message_ref": source_message_ref,
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

    async def edit_source(self, ctx, trip):
        """Build a bounded edit proof from the saved primary itinerary."""
        resolved = await self.trip(ctx, trip)
        _, actor_revision = await self.google.repo.settings(ctx.chat.key)
        rows = await self.google.repo.trip_files(resolved["chat_key"], resolved["trip_id"])
        row = next((r for r in rows if r["role"] == "itinerary"), None)
        if row is None:
            raise ValueError("No primary itinerary is saved for this trip; choose one first.")
        proof = {
            "chat_key": resolved["chat_key"],
            "trip_id": resolved["trip_id"],
            "trip_name": resolved["trip_name"],
            "group_revision": resolved["group_revision"],
            "actor_revision": actor_revision,
            "owner_email": self.google.settings.owner_email,
            "file_id": row["file_id"],
            "row": row,
            "expected": self.google.repo.file_state(rows),
            "make_current": False,
        }
        await self.verify_edit_source(ctx, proof)
        return proof

    async def verify_edit_source(self, ctx, proof):
        """Revalidate scope, settings, membership and the exact saved primary."""
        await self.google.authorize(ctx, write=True)
        resolved = await self.trip(ctx, proof["trip_id"])
        if (
            resolved["chat_key"] != proof["chat_key"]
            or resolved["trip_id"] != proof["trip_id"]
            or resolved["group_revision"] != proof["group_revision"]
        ):
            raise ValueError("Trip group/scope/settings changed; request a fresh edit.")
        _, actor_revision = await self.google.repo.settings(ctx.chat.key)
        if actor_revision != proof["actor_revision"]:
            raise ValueError("Google requester settings changed; request a fresh edit.")
        rows = await self.google.repo.trip_files(proof["chat_key"], proof["trip_id"])
        if self.google.repo.file_state(rows) != proof["expected"]:
            raise ValueError("Trip file associations changed; request a fresh edit.")
        row = next((r for r in rows if r["file_id"] == proof["file_id"]), None)
        if row != proof["row"] or row is None or row["role"] != "itinerary":
            raise ValueError("The saved primary itinerary changed; request a fresh edit.")
        if self.google.settings.owner_email != proof["owner_email"]:
            raise ValueError("Google owner settings changed; request a fresh edit.")
        owner = await self.google.client.identity()
        if not row["owner"] or row["owner"] != owner:
            raise ValueError("Saved file owner identity needs a fresh confirmed attachment.")
        ref = parse_google_ref(row["url"])
        if ref.file_id != row["file_id"]:
            raise ValueError("Saved file identity changed; attach it again.")
        metadata = await self.google.client.metadata(row["file_id"])
        self.google.check_metadata(row["file_id"], metadata)
        if metadata.get("mimeType") != row["mime"] or row["mime"] not in {
            "application/vnd.google-apps.document",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }:
            raise ValueError("The saved primary is not an editable Doc source.")
        owner_after = await self.google.client.identity()
        await self.google.authorize(ctx, write=True)
        refreshed = await self.trip(ctx, proof["trip_id"])
        latest = await self.google.repo.trip_file(proof["chat_key"], proof["file_id"])
        rows_after = await self.google.repo.trip_files(proof["chat_key"], proof["trip_id"])
        _, actor_revision = await self.google.repo.settings(ctx.chat.key)
        if (
            latest != row
            or self.google.repo.file_state(rows_after) != proof["expected"]
            or refreshed != resolved
            or actor_revision != proof["actor_revision"]
            or self.google.settings.owner_email != proof["owner_email"]
            or owner_after != owner
        ):
            raise ValueError("Trip file association or settings changed; request a fresh edit.")
        return resolved, row, ref, metadata, owner

    async def guard_doc_target(self, ctx, url):
        """Block an implicitly selected saved reference when a newer primary exists."""
        ref = parse_google_ref(url)
        attached = await self.google.repo.trip_file(ctx.chat.key, ref.file_id)
        current_ids = set()
        for value in getattr(getattr(ctx, "turn", None), "human_urls", ()):
            try:
                current_ids.add(parse_google_ref(value).file_id)
            except (TypeError, ValueError):
                continue
        proof = {
            "file_id": ref.file_id,
            "trip_id": attached["trip_id"] if attached else None,
            "current_named": ref.file_id in current_ids,
        }
        if attached and attached["role"] == "reference":
            rows = await self.google.repo.trip_files(ctx.chat.key, attached["trip_id"])
            if any(row["role"] == "itinerary" for row in rows) and not proof["current_named"]:
                raise ValueError(
                    "This file is a saved trip reference, not its current itinerary. "
                    "Name this older file in your current message to edit it explicitly."
                )
        return proof

    async def verify_doc_target(self, ctx, proof):
        """Recheck stale-reference protection after a confirmed edit proposal."""
        attached = await self.google.repo.trip_file(ctx.chat.key, proof["file_id"])
        if attached is None or attached["role"] != "reference":
            return
        rows = await self.google.repo.trip_files(ctx.chat.key, attached["trip_id"])
        if any(row["role"] == "itinerary" for row in rows) and not proof["current_named"]:
            raise ValueError(
                "This file is a saved trip reference, not its current itinerary. "
                "Name this older file in your current message to edit it explicitly."
            )

    async def propose_edit(
        self,
        ctx,
        trip,
        find,
        replacement,
        *,
        destination="auto",
        copy_title=None,
        link_access=None,
        make_current=None,
    ):
        await self.google.authorize(ctx, write=True)
        if make_current is not None and type(make_current) is not bool:
            raise ValueError("Choose whether a requested copy becomes the current itinerary.")
        proof = await self.edit_source(ctx, trip)
        proof["make_current"] = make_current
        return await self.google.edits.propose_trip_doc(
            ctx,
            proof,
            find,
            replacement,
            destination=destination,
            copy_title=copy_title,
            link_access=link_access,
        )

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
            if payload.get("source_message_ref") is not None:
                await approved_source_ref(data["url"], ctx, payload["source_message_ref"])
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
