"""Narrow, journal-bound link-viewer grants for confirmed new native Docs."""

from __future__ import annotations

import re
from collections import Counter

from chatcore_google.client import GoogleError
from chatcore_google.edit_models import DOC

ACCESS_LEVELS = frozenset({"private", "viewer", "editor"})


def resolve_link_access(settings, link_access=None, link_viewer=None):
    """Normalize new-Doc access while preserving callers that still pass link_viewer."""
    if link_access is not None and link_viewer is not None:
        raise ValueError("Use link_access or link_viewer, not both.")
    if link_access is not None:
        if not isinstance(link_access, str) or link_access not in ACCESS_LEVELS:
            raise ValueError("Choose private, viewer or editor.")
        return link_access
    if link_viewer is not None:
        if type(link_viewer) is not bool:
            raise ValueError("link_viewer must be a boolean.")
        return "viewer" if link_viewer else "private"
    configured = settings.new_docs_link_access
    if configured is not None:
        if configured not in ACCESS_LEVELS:
            raise ValueError("Choose private, viewer or editor for the new-Doc default.")
        return configured
    return "viewer" if settings.new_docs_link_viewer else "private"


def payload_link_access(payload):
    """Decode new canonical payloads and old boolean-only previews without promotion."""
    if "link_access" in payload:
        access = payload["link_access"]
        if not isinstance(access, str) or access not in ACCESS_LEVELS:
            raise ValueError("The confirmed Doc access choice is invalid.")
        if "link_viewer" in payload:
            legacy = payload["link_viewer"]
            if type(legacy) is not bool:
                raise ValueError("The confirmed legacy link_viewer choice is invalid.")
            if access != ("viewer" if legacy else "private"):
                raise ValueError("The confirmed Doc access choices conflict.")
        return access
    legacy = payload.get("link_viewer")
    if legacy is None:
        return "private"
    if type(legacy) is not bool:
        raise ValueError("The confirmed legacy link_viewer choice is invalid.")
    return "viewer" if legacy else "private"


def web_url(file_id: str) -> str:
    return f"https://docs.google.com/document/d/{file_id}/edit"


def write_outcome_uncertain(error):
    """Treat malformed/transport failures after a write as unknown; explicit 4xx is refusal."""
    message = str(error).casefold()
    return not (
        isinstance(error, GoogleError)
        and (
            re.search(r"http 4\d\d", message)
            or "authorization failed" in message
            or "reauthorize" in message
            or "credentials are unavailable" in message
            or "credential file" in message
            or "cannot open that file" in message
            or "rate limit" in message
        )
    )


async def record_result(google, action, ctx, **changes):
    """Return a journal error instead of hiding a remote mutation behind an exception."""
    try:
        return await google.repo.update_edit_result(ctx.chat.key, action.id, **changes)
    except Exception as exc:
        return exc


def _permission_key(entry):
    return tuple(
        entry.get(field)
        for field in ("id", "type", "role", "emailAddress", "domain", "allowFileDiscovery")
    )


def _public_acl_is_safe(entries, requested_role="reader"):
    anyone = [entry for entry in entries if entry.get("type") == "anyone"]
    return (
        len(anyone) == 1
        and all(
            entry.get("role") == requested_role and entry.get("allowFileDiscovery") is False
            for entry in anyone
        )
        and not any(
            entry.get("type") == "domain" and entry.get("role") != "reader" for entry in entries
        )
    )


async def publish_doc_link_access(google, action, ctx, payload, destination_id):
    """Apply the frozen fixed link role to this confirmed new native Doc only."""
    repo, client = google.repo, google.client
    result = await repo.edit_result(ctx.chat.key, action.id)
    access = payload_link_access(payload)
    if access not in {"viewer", "editor"}:
        raise ValueError("The confirmed Doc has no link-access grant to publish.")
    requested_role = "reader" if access == "viewer" else "writer"
    expected_kind = action.kind
    expected_source = None
    if action.kind == "edit_doc":
        destination = payload.get("destination") or {}
        if destination.get("mode") not in {"copy", "docx_import"}:
            raise ValueError("Only a new Doc destination can receive link access.")
        expected_source = (payload.get("source") or {}).get("file_id")
    elif action.kind != "create_doc":
        raise ValueError("Only a confirmed new Doc action can receive link access.")
    if (
        result is None
        or result.get("kind") != expected_kind
        or result.get("source_id") != expected_source
        or result.get("destination_id") != destination_id
        or result.get("phase") not in {"destination_created", "applying"}
        or result.get("failed_stage") == "sharing"
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", str(destination_id))
    ):
        raise ValueError("The confirmed destination is not bound to this sharing action.")

    try:
        identity = await client.identity()
        if identity != payload.get("owner"):
            raise ValueError("The configured Google owner changed before sharing.")
        metadata = await client.metadata(destination_id)
        owners = metadata.get("owners") or []
        if (
            metadata.get("id") != destination_id
            or metadata.get("trashed")
            or metadata.get("mimeType") != DOC
            or metadata.get("isAppAuthorized") is not True
            or metadata.get("ownedByMe") is not True
            or not any(
                str(owner.get("emailAddress", "")).casefold()
                == str(google.settings.owner_email).casefold()
                and owner.get("permissionId") == identity
                for owner in owners
            )
            or metadata.get("capabilities", {}).get("canEdit") is not True
            or metadata.get("capabilities", {}).get("canShare") is not True
        ):
            raise ValueError("The new Doc is not an owned, app-authorized editable destination.")

        permissions = await client.list_permissions(destination_id)
        if permissions.get("nextPageToken"):
            raise ValueError("The destination permissions could not be fully checked.")
        entries = permissions.get("permissions")
        if not isinstance(entries, list):
            raise ValueError("The destination permissions could not be checked.")
        existing = [entry for entry in entries if entry.get("type") == "anyone"]
        if (existing and not _public_acl_is_safe(entries, requested_role)) or any(
            entry.get("type") == "domain" and entry.get("role") != "reader" for entry in entries
        ):
            raise ValueError(
                "The destination already has broader public access; sharing was refused."
            )
        old_acl = Counter(map(_permission_key, entries))
    except Exception:
        await record_result(
            google,
            action,
            ctx,
            phase="partial",
            destination_id=destination_id,
            failed_stage="sharing_preflight",
            error_category="ownership_or_acl_check",
        )
        return False, (
            f"Partial: the verified Doc is at {web_url(destination_id)}; ownership or existing "
            "access could not be safely verified, so no link-access grant was made."
        )

    try:
        await google.verify_commit(action, ctx)
    except Exception:
        await record_result(
            google,
            action,
            ctx,
            phase="partial",
            destination_id=destination_id,
            failed_stage="sharing_authorization",
            error_category="authorization_changed",
        )
        return False, (
            f"Partial: the verified Doc is at {web_url(destination_id)}; chat authorization "
            "changed before sharing, so no link-access grant was made."
        )

    stage_result = await record_result(
        google,
        action,
        ctx,
        phase=result["phase"],
        destination_id=destination_id,
        failed_stage="sharing",
    )
    if isinstance(stage_result, Exception):
        return False, (
            f"Partial: the verified Doc is at {web_url(destination_id)}; the sharing attempt "
            "could not be durably recorded, so no grant was made."
        )
    if not existing:
        try:
            if payload.get("trip_source") and getattr(google, "trip_files", None):
                await google.trip_files.verify_edit_source(ctx, payload["trip_source"])
            if access == "viewer":
                await client.grant_link_viewer(destination_id)
            else:
                await client.grant_link_editor(destination_id)
        except Exception as exc:
            uncertain = write_outcome_uncertain(exc)
            journal_result = await record_result(
                google,
                action,
                ctx,
                phase="uncertain" if uncertain else "partial",
                destination_id=destination_id,
                failed_stage="sharing_grant",
                error_category="remote_unknown" if uncertain else "refused",
            )
            if isinstance(journal_result, Exception):
                uncertain = True
            return False, (
                f"Uncertain: the Doc is at {web_url(destination_id)}; inspect access "
                "before retrying."
                if uncertain
                else f"Partial: the Doc is at {web_url(destination_id)}; Google refused "
                "link access."
            )
    try:
        permissions = await client.list_permissions(destination_id)
        entries = permissions.get("permissions")
        post_acl = (
            Counter(map(_permission_key, entries)) if isinstance(entries, list) else Counter()
        )
        added = post_acl - old_acl
        removed = old_acl - post_acl
        expected_delta = (
            sum(added.values()) == 1
            and sum(removed.values()) == 0
            and all(
                key[1] == "anyone" and key[2] == requested_role and key[5] is False for key in added
            )
            if not existing
            else not added and not removed
        )
        verified = (
            not permissions.get("nextPageToken")
            and isinstance(entries, list)
            and _public_acl_is_safe(entries, requested_role)
            and expected_delta
        )
    except Exception:
        verified = False
    if not verified:
        await record_result(
            google,
            action,
            ctx,
            phase="uncertain",
            destination_id=destination_id,
            failed_stage="sharing_readback",
            error_category="readback_mismatch",
        )
        return False, (
            f"Uncertain: the Doc is at {web_url(destination_id)}; its link access "
            "could not be verified. Inspect it before retrying."
        )
    shared_result = await record_result(
        google,
        action,
        ctx,
        phase="shared",
        destination_id=destination_id,
        failed_stage=None,
        error_category=None,
    )
    if isinstance(shared_result, Exception):
        return False, (
            f"Uncertain: the Doc is at {web_url(destination_id)}; access read-back passed but "
            "the verified share result could not be journaled. Inspect access before retrying."
        )
    return True, ""


async def publish_doc_link_viewer(google, action, ctx, payload, destination_id):
    """Backward-compatible entry point for existing viewer payloads."""
    return await publish_doc_link_access(google, action, ctx, payload, destination_id)
