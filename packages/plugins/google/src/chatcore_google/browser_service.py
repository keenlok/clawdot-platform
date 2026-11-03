"""Copy-only Google Docs browser trial broker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from chatcore_google.browser_client import BrowserClientError
from chatcore_google.docx import docx_text
from chatcore_google.edit_models import DOC, DOCX
from chatcore_google.edit_snapshot import doc_style_for_range, find_doc_match, utf16_length

_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")
_FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_TERMINAL_PHASES = frozenset(
    {"verified", "needs_login", "blocked", "failed_before_edit", "uncertain"}
)
_ACTIVE_PHASES = frozenset({"accepted", "running", "awaiting_verification"})
_DOCX_TRUNCATION_MARKER = "[Truncated: partial DOCX text]"
_MISSING_BOLD = object()


def parse_chat_keys(value: str | frozenset[str] | set[str] | tuple[str, ...]) -> frozenset[str]:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (frozenset, set, tuple)):
        values = value
    else:
        raise ValueError("Browser trial chat allowlist is invalid.")
    result = frozenset(item.strip() for item in values if isinstance(item, str) and item.strip())
    if len(result) != sum(bool(item.strip()) for item in values if isinstance(item, str)):
        raise ValueError("Browser trial chat allowlist contains duplicate entries.")
    if any(len(item) > 128 or any(ord(char) < 0x21 for char in item) for item in result):
        raise ValueError("Browser trial chat key is invalid.")
    return result


def parse_share_emails(value: str | dict) -> dict[str, tuple[str, ...]]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("Browser trial share map must be a JSON object.") from None
    if not isinstance(parsed, dict) or len(parsed) > 100:
        raise ValueError("Browser trial share map must be a bounded JSON object.")
    output = {}
    for key, addresses in parsed.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 128
            or any(ord(char) < 0x21 for char in key)
            or not isinstance(addresses, (list, tuple))
            or len(addresses) > 20
        ):
            raise ValueError("Browser trial share map entry is invalid.")
        members = []
        seen = set()
        for address in addresses:
            if not isinstance(address, str) or len(address) > 254 or not _EMAIL.fullmatch(address):
                raise ValueError("Browser trial member address is invalid.")
            folded = address.casefold()
            if folded in seen:
                raise ValueError("Browser trial share map has duplicate members.")
            seen.add(folded)
            members.append(address)
        output[key] = tuple(members)
    return output


@dataclass(frozen=True)
class BrowserTrialConfig:
    enabled: bool
    allowed_chat_keys: frozenset[str]
    editor_email: str
    share_emails: dict[str, tuple[str, ...]]

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Browser trial enabled setting must be boolean.")
        object.__setattr__(self, "allowed_chat_keys", parse_chat_keys(self.allowed_chat_keys))
        if not isinstance(self.editor_email, str) or len(self.editor_email) > 254:
            raise ValueError("Browser editor account is invalid.")
        editor = self.editor_email.strip()
        if self.enabled and (not editor or not _EMAIL.fullmatch(editor)):
            raise ValueError("A dedicated browser editor account is required.")
        object.__setattr__(self, "editor_email", editor)
        object.__setattr__(self, "share_emails", parse_share_emails(self.share_emails))


def _copy_title(name: object, requested: object = None) -> str:
    if requested is not None:
        if not isinstance(requested, str) or not requested.strip() or len(requested) > 200:
            raise ValueError("Copy title must contain 1–200 characters.")
        return requested
    base = str(name or "Google Doc")[:190].rstrip()
    return base + " — edited"


def _tabs(document: dict) -> list[dict]:
    output: list[dict] = []

    def visit(tab):
        output.append(tab.get("tabProperties") or {})
        for child in tab.get("childTabs", ()):
            visit(child)

    for tab in document.get("tabs", ()):
        visit(tab)
    if not output and document.get("body"):
        output.append({"tabId": "", "title": ""})
    return output


def _op(value: object) -> tuple[dict, str, str | None]:
    if not isinstance(value, dict):
        raise ValueError("Choose one supported browser editing operation.")
    kind = value.get("kind")
    if kind == "replace_text":
        if set(value) != {"kind", "find", "replacement"}:
            raise ValueError("Text replacement accepts only find and replacement.")
        find, replacement = value["find"], value["replacement"]
        if (
            not isinstance(find, str)
            or not find
            or len(find) > 2000
            or "\n" in find
            or "\r" in find
            or not isinstance(replacement, str)
            or len(replacement) > 2000
            or "\n" in replacement
            or "\r" in replacement
        ):
            raise ValueError("Text replacement must be bounded within one paragraph.")
        return dict(value), find, replacement
    if kind == "bold_text":
        if set(value) != {"kind", "text", "bold"}:
            raise ValueError("Bold formatting accepts only text and bold.")
        text, bold = value["text"], value["bold"]
        if (
            not isinstance(text, str)
            or not text
            or len(text) > 2000
            or "\n" in text
            or "\r" in text
            or type(bold) is not bool
        ):
            raise ValueError("Bold formatting needs one bounded text span and a boolean state.")
        return dict(value), text, None
    raise ValueError("Only exact text replacement and bold formatting are supported.")


def _worker_field(job, name, default=None):
    if isinstance(job, dict):
        return job.get(name, default)
    return getattr(job, name, default)


def _style_spans(snapshot):
    """Return fully covered selected-tab UTF-16 style spans, independent of run splitting."""
    output = []
    if not snapshot.complete:
        raise ValueError("The selected tab style coverage is incomplete.")
    for paragraph in snapshot.paragraphs:
        cursor = 0
        previous_end = None
        for run in paragraph.runs:
            if run.get("structural") or run.get("suggestions"):
                raise ValueError("The selected tab contains unsupported style coverage.")
            text = run.get("text")
            start, end, style = run.get("start_index"), run.get("end_index"), run.get("style")
            if (
                not isinstance(text, str)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or not isinstance(style, dict)
                or end <= start
                or end - start != utf16_length(text)
                or paragraph.text[cursor : cursor + len(text)] != text
                or (previous_end is not None and start != previous_end)
            ):
                raise ValueError("The selected tab does not have complete style coverage.")
            output.append((start, end, dict(style)))
            cursor += len(text)
            previous_end = end
        if cursor != len(paragraph.text):
            raise ValueError("The selected tab does not have complete style coverage.")
    return output


def _bold_change_matches(before, after, start, end, expected_bold):
    if len(before.paragraphs) != len(after.paragraphs):
        return False
    for left, right in zip(before.paragraphs, after.paragraphs, strict=True):
        if (
            left.text != right.text
            or left.start_index != right.start_index
            or left.end_index != right.end_index
        ):
            return False
    try:
        before_spans, after_spans = _style_spans(before), _style_spans(after)
    except ValueError:
        return False
    boundaries = {point for span in (*before_spans, *after_spans) for point in span[:2]}
    boundaries.update((start, end))
    ordered = sorted(boundaries)
    before_index = after_index = 0
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left == right:
            continue
        while before_index < len(before_spans) and before_spans[before_index][1] <= left:
            before_index += 1
        while after_index < len(after_spans) and after_spans[after_index][1] <= left:
            after_index += 1
        if (
            before_index >= len(before_spans)
            or after_index >= len(after_spans)
            or before_spans[before_index][0] > left
            or before_spans[before_index][1] < right
            or after_spans[after_index][0] > left
            or after_spans[after_index][1] < right
        ):
            return False
        before_style = before_spans[before_index][2]
        after_style = after_spans[after_index][2]
        before_bold = before_style.get("bold", _MISSING_BOLD)
        after_bold = after_style.get("bold", _MISSING_BOLD)
        if before_bold is not _MISSING_BOLD and type(before_bold) is not bool:
            return False
        if after_bold is not _MISSING_BOLD and type(after_bold) is not bool:
            return False
        before_other = {key: value for key, value in before_style.items() if key != "bold"}
        after_other = {key: value for key, value in after_style.items() if key != "bold"}
        if before_other != after_other:
            return False
        if start <= left and right <= end:
            # Omitted TextStyle fields inherit from named styles; only an explicit
            # API value establishes the requested state.
            if after_bold is not expected_bold:
                return False
        elif before_bold != after_bold:
            return False
    return True


class GoogleBrowserEditService:
    """Confirmed copy, fixed sharing, worker dispatch and API-side result verification."""

    def __init__(self, google_plugin, worker, config: BrowserTrialConfig, *, sleep=asyncio.sleep):
        self.plugin = google_plugin
        self.edits = google_plugin.edits
        self.google = google_plugin.service
        self.client = self.google.client
        self.worker = worker
        self.config = config
        self.sleep = sleep
        owner = self.google.settings.owner_email.casefold()
        if config.editor_email and config.editor_email.casefold() == owner:
            raise ValueError("The browser editor account must be different from the Google owner.")
        if any(
            member.casefold() == owner
            for addresses in config.share_emails.values()
            for member in addresses
        ):
            raise ValueError("The Google owner cannot be a configured browser trial member.")

    def _grantees(self, chat_key: str) -> list[str]:
        members = self.config.share_emails.get(chat_key, ())
        if len(members) > 20:
            raise ValueError("At most 20 chat members can receive trial-copy access.")
        output = [self.config.editor_email]
        seen = {self.config.editor_email.casefold()}
        for member in members:
            if member.casefold() == self.google.settings.owner_email.casefold():
                raise ValueError("The Google owner cannot be a browser trial recipient.")
            if member.casefold() not in seen:
                seen.add(member.casefold())
                output.append(member)
        return output

    def _check_chat(self, ctx) -> list[str]:
        if not self.config.enabled:
            raise PermissionError("Google browser editing is disabled.")
        if ctx.chat.key not in self.config.allowed_chat_keys:
            raise PermissionError("Google browser editing is not enabled in this chat.")
        if not self.google.settings.copy_qualified:
            raise PermissionError("Google copy qualification is required for the browser trial.")
        return self._grantees(ctx.chat.key)

    async def propose_doc(self, ctx, url, *, operation, tab_id=None, copy_title=None):
        grantees = self._check_chat(ctx)
        operation, find, replacement = _op(operation)
        changed = replacement if replacement is not None else find
        if max(len(find), len(changed)) > self.google.settings.max_cell_chars:
            raise ValueError("The browser edit exceeds the configured character limit.")
        ref, metadata, owner = await self.edits.resolve_source(
            ctx, url, expected=DOC, allow_docx=True
        )
        mime = metadata.get("mimeType")
        if mime not in {DOC, DOCX}:
            raise ValueError("The browser trial supports Google Docs and Drive DOCX only.")
        if ref.doc_tab and tab_id and ref.doc_tab != tab_id:
            raise ValueError("The tab selected in the URL disagrees with the supplied tab ID.")
        destination = self.edits.destination_decision(metadata, "auto" if mime == DOCX else "copy")
        if destination.mode not in {"copy", "docx_import"} or destination.mime_type != DOC:
            raise ValueError("Browser trial destinations must be new native Google Doc copies.")
        title = _copy_title(metadata.get("name"), copy_title)
        source = {
            "file_id": ref.file_id,
            "mime": mime,
            "content_revision": metadata.get("headRevisionId"),
        }
        if mime == DOCX:
            if metadata.get("capabilities", {}).get("canDownload") is not True:
                raise ValueError("DOCX downloading is not permitted.")
            data = await self.client.media(ref.file_id)
            if len(data) > self.google.settings.max_bytes:
                raise ValueError("DOCX exceeds the configured download size limit.")
            before_text = docx_text(data, max_chars=self.google.settings.max_chars)
            if _DOCX_TRUNCATION_MARKER in before_text:
                raise ValueError("DOCX text is incomplete for the browser trial.")
            if before_text.count(find) != 1:
                raise ValueError(
                    "The requested text must occur exactly once in the DOCX main body."
                )
            snapshot = None
            source["sha256"] = hashlib.sha256(data).hexdigest()
            tab_title = "Imported first tab"
            precondition = {
                "tab_id": None,
                "tab_title": tab_title,
                "before_text": before_text,
            }
        else:
            snapshot = await self.edits.read_doc_snapshot(ref.file_id, tab_id or ref.doc_tab)
            match = find_doc_match(snapshot, find)
            if not snapshot.complete:
                raise ValueError("Pending suggestions make the browser snapshot incomplete.")
            before_text = snapshot.text
            tab_title = snapshot.title
            precondition = {
                "document_revision": snapshot.revision_id,
                "tab_id": snapshot.tab_id,
                "tab_title": tab_title,
                "start_index": match.start_index,
                "end_index": match.end_index,
                "uniform_style": match.uniform_style,
                "before_text": before_text,
                "coverage_complete": snapshot.complete,
            }
        _, revision = await self.google.repo.settings(ctx.chat.key)
        payload = {
            "owner_email": self.google.settings.owner_email,
            "owner": owner,
            "revision": revision,
            "source": source,
            "destination": {"mode": destination.mode, "mime": DOC, "title": title},
            "precondition": precondition,
            "snapshot": {"text": before_text, "tab_title": tab_title},
            "edit": {
                "find": find,
                "replacement": replacement if replacement is not None else find,
            },
            "browser": {"operation": operation, "grantees": grantees},
        }
        detail = (
            f"Replace {find!r} with {replacement!r}"
            if replacement is not None
            else f"Set bold={operation['bold']} for {find!r}"
        )
        summary = (
            f"Google Docs browser trial: {metadata.get('name', 'Document')}\n"
            f"Source: {url}\nDestination: a new Google Doc titled {title!r}\n"
            f"Tab: {tab_title or 'Main body'}\nEdit: {detail}\n"
            f"Writer access after confirmation: {', '.join(grantees)}\n"
            "Only this new copy is edited. The bot verifies the saved result "
            "through the Google API."
        )
        return await self.google.action(ctx, "browser_edit_doc", payload, summary)

    async def _verify_source_precondition(self, action, payload):
        source = payload["source"]
        pre = payload["precondition"]
        find = payload["edit"]["find"]
        if source["mime"] == DOCX:
            data = await self.client.media(source["file_id"])
            if len(data) > self.google.settings.max_bytes or hashlib.sha256(data).hexdigest() != (
                source.get("sha256")
            ):
                return False
            return True
        snapshot = await self.edits.read_doc_snapshot(source["file_id"], pre["tab_id"])
        if (
            not snapshot.complete
            or snapshot.revision_id != pre["document_revision"]
            or snapshot.text != pre["before_text"]
        ):
            return False
        try:
            match = find_doc_match(snapshot, find)
        except ValueError:
            return False
        return (
            match.start_index == pre["start_index"]
            and match.end_index == pre["end_index"]
            and match.uniform_style == pre["uniform_style"]
        )

    async def _phase(self, action, ctx, phase, *, stage=None, category=None):
        result = await self.edits.update_action_result(
            action,
            ctx,
            phase=phase,
            stage=stage,
            category=category,
        )
        return result

    @staticmethod
    def _destination_url(file_id: str) -> str:
        if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
            raise ValueError("Google returned an invalid destination ID.")
        return f"https://docs.google.com/document/d/{file_id}/edit"

    async def _copy_snapshot(self, destination_id: str, payload: dict):
        raw = await self.client.document_for_edit(destination_id)
        tabs = _tabs(raw)
        pre = payload["precondition"]
        if pre.get("tab_id") is None:
            if len(tabs) != 1:
                raise ValueError("Imported DOCX did not produce one unambiguous Google Docs tab.")
            tab = tabs[0]
        else:
            matches = [tab for tab in tabs if tab.get("title") == pre.get("tab_title")]
            if len(matches) != 1:
                raise ValueError("The new Doc copy tab could not be resolved uniquely.")
            tab = matches[0]
        tab_id = tab.get("tabId")
        if not isinstance(tab_id, str) or not tab_id:
            raise ValueError("Google did not return a stable copied tab ID.")
        snapshot = await self.edits.read_doc_snapshot(destination_id, tab_id)
        if not snapshot.complete:
            raise ValueError("The copied Doc snapshot is incomplete.")
        if pre.get("tab_id") is not None and snapshot.text != pre.get("before_text"):
            raise ValueError("The copied Doc content differs from the confirmed preview.")
        # DOCX import can render tables and paragraph boundaries differently from the
        # extractor. Require complete native coverage and the exact unique target instead.
        match = find_doc_match(snapshot, payload["edit"]["find"])
        return snapshot, match

    async def _grant_copy(self, action, ctx, destination_id: str, recipients: list[str]):
        missing = []
        for address in recipients:
            await self.google.verify_commit(action, ctx)
            try:
                await self.client.grant_writer(destination_id, address)
            except Exception:  # noqa: S112 - grant response may contain PII; read-back decides.
                # A pre-existing permission may make create fail; the read-back below decides.
                continue
        await self.google.verify_commit(action, ctx)
        permission_response = await self.client.list_permissions(destination_id)
        permissions = (
            permission_response.get("permissions")
            if isinstance(permission_response, dict)
            else None
        )
        if not isinstance(permissions, list):
            return False, recipients[0], recipients[1:], "permission_readback"
        editor = recipients[0]
        editor_ok = any(
            isinstance(item, dict)
            and item.get("type") == "user"
            and item.get("role") == "writer"
            and isinstance(item.get("emailAddress"), str)
            and item["emailAddress"].casefold() == editor.casefold()
            for item in permissions
        )
        if not editor_ok:
            return False, editor, recipients[1:], "editor_permission_missing"
        for member in recipients[1:]:
            if not any(
                isinstance(item, dict)
                and item.get("type") == "user"
                and item.get("role") == "writer"
                and isinstance(item.get("emailAddress"), str)
                and item["emailAddress"].casefold() == member.casefold()
                for item in permissions
            ):
                missing.append(member)
        return True, editor, missing, "member_permission_missing" if missing else None

    def _worker_request(self, action, payload, copy_snapshot, match):
        operation = payload["browser"]["operation"]
        now = datetime.now(UTC)
        expires = action.expires_at.astimezone(UTC)
        if expires <= now:
            raise ValueError("The browser editing confirmation expired before dispatch.")
        expires = min(expires, now.replace(microsecond=0) + timedelta(seconds=90))
        return {
            "job_id": action.id,
            "expires_at": expires.isoformat(),
            "editor_email": self.config.editor_email,
            "destination_file_id": copy_snapshot.file_id,
            "destination_tab_id": copy_snapshot.tab_id,
            "expected_revision": copy_snapshot.revision_id,
            "operation": operation,
        }

    async def _job_after_post(self, action, request):
        try:
            return await self.worker.create_job(request)
        except BrowserClientError as exc:
            if not exc.uncertain:
                raise
            # A POST timeout can follow acceptance. Query once; never resend the operation.
            try:
                return await self.worker.job(action.id)
            except Exception:
                # Failure to establish acceptance does not make an ambiguous POST safe to retry.
                raise BrowserClientError("worker_acceptance_unknown", uncertain=True) from None

    async def _verify_result(self, action, payload, copy_snapshot, match):
        browser = payload["browser"]
        operation = browser["operation"]
        tab = await self.edits.read_doc_snapshot(copy_snapshot.file_id, copy_snapshot.tab_id)
        if (
            not tab.complete
            or tab.tab_id != copy_snapshot.tab_id
            or tab.title != copy_snapshot.title
        ):
            return False
        before = (
            copy_snapshot.text if payload["source"]["mime"] == DOCX else payload["snapshot"]["text"]
        )
        if operation["kind"] == "replace_text":
            find = operation["find"]
            replacement = operation["replacement"]
            if before.count(find) != 1:
                return False
            expected = before.replace(find, replacement, 1)
            if tab.text != expected:
                return False
            if replacement:
                end = match.start_index + utf16_length(replacement)
                try:
                    style = doc_style_for_range(tab, match.start_index, end)
                except ValueError:
                    return False
                if style != match.uniform_style:
                    return False
            return True
        if tab.text != before:
            return False
        return _bold_change_matches(
            copy_snapshot,
            tab,
            match.start_index,
            match.end_index,
            operation["bold"],
        )

    async def _authorization_after_worker(self, action, ctx, destination_id):
        try:
            await self.google.verify_commit(action, ctx)
            return True
        except Exception:
            try:
                await self.worker.cancel(action.id)
            except Exception:  # noqa: S110 - cancellation is best effort and response is private.
                pass
            await self._phase(
                action, ctx, "uncertain", stage="authorization", category="permission_changed"
            )
            return False

    async def _await_worker(self, action, ctx, payload, destination_id, copy_snapshot, match):
        try:
            fresh_snapshot = await self.edits.read_doc_snapshot(
                destination_id, copy_snapshot.tab_id
            )
            fresh_match = find_doc_match(fresh_snapshot, payload["edit"]["find"])
            if (
                not fresh_snapshot.complete
                or fresh_snapshot.revision_id != copy_snapshot.revision_id
                or fresh_snapshot.text != copy_snapshot.text
                or fresh_snapshot.title != copy_snapshot.title
                or fresh_match.start_index != match.start_index
                or fresh_match.end_index != match.end_index
                or fresh_match.uniform_style != match.uniform_style
            ):
                raise ValueError("The copied Doc changed after sharing.")
        except Exception:
            await self._phase(
                action, ctx, "partial", stage="destination_pre_dispatch", category="copy_changed"
            )
            return self._result_text(
                destination_id,
                "Partial: the shared copy changed before dispatch; no browser edit was sent.",
            )
        copy_snapshot, match = fresh_snapshot, fresh_match
        try:
            request = self._worker_request(action, payload, copy_snapshot, match)
        except ValueError:
            await self._phase(action, ctx, "partial", stage="browser_dispatch", category="expired")
            return self._result_text(
                destination_id,
                "Partial: the confirmation expired before browser dispatch; the copy is unchanged.",
            )
        try:
            if self._check_chat(ctx) != payload["browser"]["grantees"]:
                raise PermissionError("trial configuration changed")
            await self.google.verify_commit(action, ctx)
        except Exception:
            await self._phase(
                action, ctx, "partial", stage="authorization", category="dispatch_blocked"
            )
            return self._result_text(
                destination_id,
                "Partial: authorization or trial settings changed after sharing; "
                "no browser edit was sent.",
            )
        await self._phase(action, ctx, "applying")
        try:
            await self.google.verify_commit(action, ctx)
        except Exception:
            await self._phase(
                action, ctx, "partial", stage="authorization", category="dispatch_blocked"
            )
            return self._result_text(
                destination_id,
                "Partial: authorization changed before dispatch; no browser edit was sent.",
            )
        try:
            job = await self._job_after_post(action, request)
        except BrowserClientError as exc:
            phase = "uncertain" if exc.uncertain else "partial"
            category = "response_unknown" if exc.uncertain else "worker_rejected"
            await self._phase(action, ctx, phase, stage="browser_dispatch", category=category)
            state = (
                "Uncertain: worker acceptance could not be established."
                if exc.uncertain
                else ("Partial: the worker rejected the edit before accepting it.")
            )
            return self._result_text(destination_id, state + " Inspect the copy.")
        except Exception:
            await self._phase(
                action, ctx, "uncertain", stage="browser_dispatch", category="response_unknown"
            )
            return self._result_text(
                destination_id,
                "Uncertain: worker acceptance could not be established. Inspect the copy.",
            )
        phase = _worker_field(job, "phase")
        deadline = min(action.expires_at, datetime.now(UTC) + timedelta(seconds=90))
        while phase in {"accepted", "running"} and datetime.now(UTC) < deadline:
            try:
                await self.google.verify_commit(action, ctx)
            except (ValueError, PermissionError):
                try:
                    await self.worker.cancel(action.id)
                    job = await self.worker.job(action.id)
                    phase = _worker_field(job, "phase")
                except Exception:
                    phase = "uncertain"
                break
            await self.sleep(min(0.25, max(0.0, (deadline - datetime.now(UTC)).total_seconds())))
            try:
                job = await self.worker.job(action.id)
                phase = _worker_field(job, "phase")
            except Exception:
                phase = "uncertain"
                break
        if phase in {"accepted", "running"}:
            try:
                job = await self.worker.cancel(action.id)
                phase = _worker_field(job, "phase")
            except Exception:
                phase = "uncertain"
        if phase == "awaiting_verification":
            if not await self._authorization_after_worker(action, ctx, destination_id):
                return self._result_text(
                    destination_id,
                    "Uncertain: authorization changed after browser editing; inspect the copy.",
                )
            try:
                verified = await self._verify_result(action, payload, copy_snapshot, match)
            except Exception:
                verified = False
            ack = "verified" if verified else "uncertain"
            if not await self._authorization_after_worker(action, ctx, destination_id):
                return self._result_text(
                    destination_id,
                    "Uncertain: authorization changed during verification; inspect the copy.",
                )
            try:
                await self.worker.verify(action.id, ack)
            except Exception:
                if verified:
                    # API read-back is authoritative even if the acknowledgement response is lost.
                    if not await self._authorization_after_worker(action, ctx, destination_id):
                        return self._result_text(
                            destination_id,
                            "Uncertain: authorization changed after verification; "
                            "inspect the copy.",
                        )
                    await self._phase(action, ctx, "verified")
                    return self._result_text(destination_id, "Verified the saved Google Doc copy.")
                await self._phase(
                    action, ctx, "uncertain", stage="verification", category="readback_mismatch"
                )
                return self._result_text(
                    destination_id, "Uncertain: the saved copy did not match the requested edit."
                )
            if verified:
                if not await self._authorization_after_worker(action, ctx, destination_id):
                    return self._result_text(
                        destination_id,
                        "Uncertain: authorization changed after verification; inspect the copy.",
                    )
                await self._phase(action, ctx, "verified")
                suffix = ""
                return self._result_text(
                    destination_id, "Verified the saved Google Doc copy." + suffix
                )
            await self._phase(
                action, ctx, "uncertain", stage="verification", category="readback_mismatch"
            )
            return self._result_text(
                destination_id, "Uncertain: the saved copy did not match the requested edit."
            )
        if phase == "verified":
            # A worker's previous success claim is still checked by the broker API here.
            if not await self._authorization_after_worker(action, ctx, destination_id):
                return self._result_text(
                    destination_id,
                    "Uncertain: authorization changed after browser editing; inspect the copy.",
                )
            try:
                verified = await self._verify_result(action, payload, copy_snapshot, match)
            except Exception:
                verified = False
            if verified:
                if not await self._authorization_after_worker(action, ctx, destination_id):
                    return self._result_text(
                        destination_id,
                        "Uncertain: authorization changed after verification; inspect the copy.",
                    )
                await self._phase(action, ctx, "verified")
                return self._result_text(destination_id, "Verified the saved Google Doc copy.")
            await self._phase(
                action, ctx, "uncertain", stage="verification", category="readback_mismatch"
            )
            return self._result_text(
                destination_id, "Uncertain: the saved copy did not match the requested edit."
            )
        if phase in {"uncertain", "running", "awaiting_verification"}:
            await self._phase(action, ctx, "uncertain", stage="browser", category=str(phase))
            return self._result_text(
                destination_id, "Uncertain: the browser may have edited the copy. Inspect it."
            )
        category = (
            phase
            if isinstance(phase, str) and re.fullmatch(r"[a-z_]{1,64}", phase)
            else "worker_failed"
        )
        await self._phase(action, ctx, "partial", stage="browser", category=category)
        return self._result_text(
            destination_id,
            f"The browser trial stopped ({category}); the copy is available to inspect.",
        )

    @staticmethod
    def _result_text(destination_id, message):
        return f"{message}\nCopy: {GoogleBrowserEditService._destination_url(destination_id)}"

    async def commit(self, action, ctx):
        if action.kind != "browser_edit_doc":
            raise ValueError("Unknown Google browser editing action.")
        payload = await self.google.verify_commit(action, ctx)
        recipients = self._check_chat(ctx)
        if payload.get("browser", {}).get("grantees") != recipients:
            raise ValueError("Browser trial recipients changed; request a fresh preview.")
        if payload.get("browser", {}).get("operation") is None:
            raise ValueError("Browser editing operation is missing.")
        if not await self._verify_source_precondition(action, payload):
            await self.edits.reserve_confirmed_action(action, ctx)
            await self._phase(action, ctx, "conflict", stage="source", category="content_changed")
            return (
                "Conflict: the source changed after preview. Request a fresh browser edit preview."
            )
        await self.google.verify_commit(action, ctx)
        await self.edits.reserve_confirmed_action(action, ctx)
        source_bytes = None
        if payload["source"]["mime"] == DOCX:
            source_bytes = await self.client.media(payload["source"]["file_id"])
            if hashlib.sha256(source_bytes).hexdigest() != payload["source"].get("sha256"):
                await self._phase(
                    action, ctx, "conflict", stage="source", category="content_changed"
                )
                return (
                    "Conflict: the DOCX changed after preview. "
                    "Request a fresh browser edit preview."
                )
        try:
            await self.google.verify_commit(action, ctx)
            created = await self.edits.create_reserved_destination(
                action, ctx, source_bytes=source_bytes
            )
        except Exception:
            result = await self.google.repo.edit_result(ctx.chat.key, action.id)
            phase = result.get("phase") if result else None
            if phase == "uncertain":
                return (
                    "Uncertain: Google may have created a Doc copy; inspect Drive before retrying."
                )
            return (
                "The browser trial could not create its destination; "
                "no browser edit was dispatched."
            )
        destination_id = created.get("id") if isinstance(created, dict) else None
        if (
            not isinstance(destination_id, str)
            or not _FILE_ID.fullmatch(destination_id)
            or destination_id == payload["source"]["file_id"]
        ):
            await self._phase(
                action, ctx, "partial", stage="destination", category="invalid_destination"
            )
            if isinstance(destination_id, str) and _FILE_ID.fullmatch(destination_id):
                return self._result_text(
                    destination_id, "Partial: the created destination is invalid for editing."
                )
            return "Partial: Google returned an invalid destination ID; inspect Drive."
        try:
            await self.google.verify_commit(action, ctx)
            metadata = await self.client.metadata(destination_id)
            self.google.check_metadata(destination_id, metadata)
            if (
                metadata.get("mimeType") != DOC
                or metadata.get("capabilities", {}).get("canEdit") is not True
                or metadata.get("capabilities", {}).get("canModifyContent") is not True
                or any(
                    item.get("readOnly") is True
                    for item in metadata.get("contentRestrictions", ())
                    if isinstance(item, dict)
                )
            ):
                raise ValueError("The new Doc copy is not an editable native Google Doc.")
            copy_snapshot, match = await self._copy_snapshot(destination_id, payload)
        except Exception:
            await self._phase(
                action, ctx, "partial", stage="destination_validation", category="copy_invalid"
            )
            return self._result_text(
                destination_id,
                "Partial: the new copy could not be validated; it was not shared or edited.",
            )
        try:
            share_ok, editor, missing, share_error = await self._grant_copy(
                action, ctx, destination_id, recipients
            )
        except Exception:
            await self._phase(action, ctx, "partial", stage="sharing", category="share_unknown")
            return self._result_text(
                destination_id,
                "Partial: sharing could not be verified; the new copy is available to inspect.",
            )
        if not share_ok:
            await self._phase(action, ctx, "partial", stage="sharing", category=share_error)
            return self._result_text(
                destination_id,
                "Partial: the editor account could not be verified as a writer; "
                "the copy remains available.",
            )
        await self.edits.update_action_result(
            action, ctx, phase="shared", destination_id=destination_id
        )
        # The payload is already frozen in the pending action store; the worker request uses
        # only the resolved destination values carried in this stack frame.
        request_payload = dict(payload)
        request_payload["browser"] = dict(payload["browser"])
        request_payload["browser"].update(
            {
                "destination_tab_id": copy_snapshot.tab_id,
                "start_index": match.start_index,
                "end_index": match.end_index,
                "destination_revision": copy_snapshot.revision_id,
            }
        )
        result = await self._await_worker(
            action, ctx, request_payload, destination_id, copy_snapshot, match
        )
        if missing:
            result += "\nMissing member edit access: " + ", ".join(missing)
        return result

    async def cleanup_scope(self, chat_key: str, requester: str | None = None):
        """Best-effort worker metadata cleanup; does not remove Drive shares or files."""
        try:
            rows = await self.google.repo.browser_edit_results(chat_key, requester)
        except Exception:
            return
        for row in rows:
            job_id = row.get("action_id")
            if not isinstance(job_id, str):
                continue
            try:
                phase = _worker_field(await self.worker.job(job_id), "phase")
            except Exception:  # noqa: S112 - cleanup is best effort for rows without a worker job.
                # A reserved copy may not have reached the worker; nothing to purge remotely.
                continue
            if phase in _ACTIVE_PHASES:
                try:
                    phase = _worker_field(await self.worker.cancel(job_id), "phase")
                except Exception:
                    try:
                        phase = _worker_field(await self.worker.job(job_id), "phase")
                    except Exception:  # noqa: S112 - cleanup is best effort per job.
                        continue  # noqa: S112 - best-effort cleanup must not block other jobs.
            if phase in _TERMINAL_PHASES:
                try:
                    await self.worker.delete(job_id)
                except Exception:  # noqa: S112 - cleanup is best effort per job.
                    continue  # noqa: S112 - best-effort cleanup must not block other jobs.
