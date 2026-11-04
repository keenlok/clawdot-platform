"""Narrow, journal-bound link-viewer grants for confirmed new native Docs."""

from __future__ import annotations

import re
from collections import Counter

from chatcore_google.client import GoogleError
from chatcore_google.edit_models import DOC


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


def _public_acl_is_safe(entries):
    anyone = [entry for entry in entries if entry.get("type") == "anyone"]
    return (
        len(anyone) == 1
        and all(
            entry.get("role") == "reader" and entry.get("allowFileDiscovery") is False
            for entry in anyone
        )
        and not any(
            entry.get("type") == "domain" and entry.get("role") != "reader" for entry in entries
        )
    )


async def publish_doc_link_viewer(google, action, ctx, payload, destination_id):
    """Share only this confirmed, journaled, owner-authorized output Doc."""
    repo, client = google.repo, google.client
    result = await repo.edit_result(ctx.chat.key, action.id)
    expected_kind = action.kind
    expected_source = None
    if action.kind == "edit_doc":
        destination = payload.get("destination") or {}
        if destination.get("mode") not in {"copy", "docx_import"}:
            raise ValueError("Only a new Doc destination can receive link-viewer access.")
        expected_source = (payload.get("source") or {}).get("file_id")
    elif action.kind != "create_doc":
        raise ValueError("Only a confirmed new Doc action can receive link-viewer access.")
    if (
        payload.get("link_viewer") is not True
        or result is None
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
        if (existing and not _public_acl_is_safe(entries)) or any(
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
            "access could not be safely verified, so no link-viewer grant was made."
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
            "changed before sharing, so no link-viewer grant was made."
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
            await client.grant_link_viewer(destination_id)
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
                "link-viewer access."
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
            and all(key[1] == "anyone" and key[2] == "reader" and key[5] is False for key in added)
            if not existing
            else not added and not removed
        )
        verified = (
            not permissions.get("nextPageToken")
            and isinstance(entries, list)
            and _public_acl_is_safe(entries)
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
            f"Uncertain: the Doc is at {web_url(destination_id)}; its link-viewer access "
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
