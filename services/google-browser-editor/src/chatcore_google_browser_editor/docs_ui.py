"""Narrow accessible-control adapter for the two Docs qualification operations."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .models import BoldTextOperation, Operation, ReplaceTextOperation
from .worker import WorkerFault

_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_MATCHES = re.compile(r"^\s*(\d+)\s+of\s+(\d+)\s*$")
_SAVED = re.compile(r"(?:Saved to Drive|All changes saved)", re.IGNORECASE)


class DocsUiAdapter:
    def __init__(
        self, page: Any, expected_email: str, tab_id: str | None = None, *, timeout_ms: int = 10_000
    ) -> None:
        self.page = page
        self.expected_email = expected_email
        self.tab_id = tab_id
        self.timeout_ms = timeout_ms

    async def check_identity(self) -> None:
        account = self.page.get_by_role("button", name=re.compile("Google Account", re.I))
        if await account.count() != 1:
            raise WorkerFault("needs_login", phase="needs_login")
        label = await account.get_attribute("aria-label")
        emails = _EMAIL.findall(label or "")
        normalized = {email.casefold() for email in emails}
        if len(normalized) != 1 or self.expected_email.casefold() not in normalized:
            raise WorkerFault("wrong_editor_account", phase="needs_login")

    def check_selected_tab_url(self) -> None:
        if self.tab_id is None:
            return
        query = parse_qs(urlsplit(self.page.url).query, keep_blank_values=True)
        if query.get("tab") != [self.tab_id]:
            raise WorkerFault("wrong_destination_tab", phase="blocked")

    async def check_editable(self) -> None:
        mode = self.page.get_by_role("button", name="Editing")
        if await mode.count() != 1:
            raise WorkerFault("destination_not_editable", phase="blocked")

    async def _match_counts(self) -> tuple[int, int]:
        statuses = await self.page.get_by_role("status").all_inner_texts()
        for text in statuses:
            match = _MATCHES.fullmatch(text)
            if match:
                return int(match.group(1)), int(match.group(2))
        raise WorkerFault("match_count_unavailable", phase="blocked")

    async def _require_unique_match(self) -> None:
        current, total = await self._match_counts()
        if current != 1 or total != 1:
            raise WorkerFault("match_not_unique", phase="blocked")

    async def _require_bold_selection(self, text: str) -> None:
        """Require an accessible exact selection and focused editor before formatting."""
        try:
            editor = self.page.get_by_role("textbox", name="Document editor", exact=True)
            focused = self.page.locator(":focus")
            selection = self.page.get_by_role("status", name="Current selection", exact=True)
            if await editor.count() != 1:
                raise WorkerFault("selection_unavailable", phase="failed_before_edit")
            if not await editor.is_visible():
                raise WorkerFault("selection_unavailable", phase="failed_before_edit")
            if await focused.count() != 1:
                raise WorkerFault("selection_unavailable", phase="failed_before_edit")
            if (
                await focused.get_attribute("role") != "textbox"
                or await focused.get_attribute("aria-label") != "Document editor"
            ):
                raise WorkerFault("selection_unavailable", phase="failed_before_edit")
            if await self._match_counts() != (1, 1):
                raise WorkerFault("selection_unavailable", phase="failed_before_edit")
            if await selection.count() != 1:
                raise WorkerFault("selection_unavailable", phase="failed_before_edit")
            selected = await selection.all_inner_texts()
            if selected != [f"Selected text: {text}"]:
                raise WorkerFault("selection_unavailable", phase="failed_before_edit")
        except WorkerFault:
            raise
        except Exception as exc:
            raise WorkerFault("selection_unavailable", phase="failed_before_edit") from exc

    async def _require_no_dialog(self) -> None:
        dialogs = self.page.get_by_role("dialog")
        for dialog in await dialogs.all():
            if await dialog.is_visible():
                raise WorkerFault("unexpected_dialog", phase="blocked")

    async def _require_only_dialog(self, expected: str) -> None:
        dialogs = self.page.get_by_role("dialog")
        visible_count = sum([await dialog.is_visible() for dialog in await dialogs.all()])
        expected_dialog = self.page.get_by_role("dialog", name=expected, exact=True)
        if (
            visible_count != 1
            or await expected_dialog.count() != 1
            or not await expected_dialog.is_visible()
        ):
            raise WorkerFault("unexpected_dialog", phase="blocked")

    async def check_dialog_state(self, expected: str | None = None) -> None:
        if expected is None:
            await self._require_no_dialog()
        else:
            await self._require_only_dialog(expected)

    async def _set_checkbox(self, container: Any, name: str, desired: bool) -> bool:
        choices = await container.get_by_role("checkbox", name=name, exact=True).all()
        visible = [choice for choice in choices if await choice.is_visible()]
        if not visible:
            return False
        if len(visible) != 1:
            raise WorkerFault("search_control_ambiguous", phase="blocked")
        checkbox = visible[0]
        current = await checkbox.is_checked()
        if current != desired:
            await checkbox.click()
        if await checkbox.is_checked() != desired:
            raise WorkerFault("search_scope_unavailable", phase="blocked")
        return True

    async def _require_current_tab_scope(self, container: Any) -> None:
        if self.tab_id is None:
            raise WorkerFault("destination_tab_unverified", phase="blocked")
        self.check_selected_tab_url()
        if await self._set_checkbox(container, "Search all tabs", False):
            return
        if await self._set_checkbox(container, "Search in current tab", True):
            return
        raise WorkerFault("current_tab_scope_unavailable", phase="blocked")

    async def _call_before_mutation(self, callback: Callable[[], Awaitable[None]]) -> None:
        await callback()

    async def apply(
        self,
        operation: Operation,
        before_mutation: Callable[[], Awaitable[None]],
    ) -> None:
        self.check_selected_tab_url()
        await self.check_identity()
        await self.check_editable()
        if isinstance(operation, ReplaceTextOperation):
            await self._replace(operation, before_mutation)
        elif isinstance(operation, BoldTextOperation):
            await self._bold(operation, before_mutation)
        else:
            raise WorkerFault("unsupported_operation", phase="blocked")

    async def _replace(
        self, operation: ReplaceTextOperation, before_mutation: Callable[[], Awaitable[None]]
    ) -> None:
        await self.page.keyboard.press("Control+h")
        dialog = self.page.get_by_role("dialog", name="Find and replace")
        await dialog.wait_for(state="visible", timeout=self.timeout_ms)
        if await dialog.count() != 1:
            raise WorkerFault("find_replace_ui_unavailable", phase="blocked")
        await self._require_only_dialog("Find and replace")
        await self._require_current_tab_scope(dialog)
        if not await self._set_checkbox(dialog, "Match case", True):
            raise WorkerFault("case_sensitive_search_unavailable", phase="blocked")
        if not await self._set_checkbox(dialog, "Match using regular expressions", False):
            raise WorkerFault("literal_search_unavailable", phase="blocked")
        await dialog.get_by_role("textbox", name="Find").fill(operation.find)
        await self._require_unique_match()
        await dialog.get_by_role("textbox", name="Replace with").fill(operation.replacement)
        replace = dialog.get_by_role("button", name="Replace", exact=True)
        if await replace.count() != 1 or not await replace.is_enabled():
            raise WorkerFault("replace_control_unavailable", phase="blocked")
        await self._call_before_mutation(before_mutation)
        await replace.click()
        done = dialog.get_by_role("button", name="Done")
        if await done.count() == 1:
            await done.click()
        await self._require_no_dialog()

    async def _search(self, text: str, *, allow_zero: bool = False) -> tuple[int, int]:
        await self.page.keyboard.press("Control+f")
        search = self.page.get_by_role("textbox", name="Find in document")
        await search.wait_for(state="visible", timeout=self.timeout_ms)
        await self._require_current_tab_scope(self.page)
        if not await self._set_checkbox(self.page, "Match case", True):
            raise WorkerFault("case_sensitive_search_unavailable", phase="blocked")
        if not await self._set_checkbox(self.page, "Match using regular expressions", False):
            raise WorkerFault("literal_search_unavailable", phase="blocked")
        await search.fill(text)
        current, total = await self._match_counts()
        await self.page.keyboard.press("Escape")
        if await search.is_visible():
            raise WorkerFault("search_focus_lost", phase="blocked")
        await self._require_no_dialog()
        if not allow_zero and (current != 1 or total != 1):
            raise WorkerFault("match_not_unique", phase="blocked")
        return current, total

    async def _bold(
        self, operation: BoldTextOperation, before_mutation: Callable[[], Awaitable[None]]
    ) -> None:
        await self._search(operation.text)
        await self._require_bold_selection(operation.text)
        button = self.page.get_by_role("button", name="Bold", exact=True)
        if await button.count() != 1:
            raise WorkerFault("bold_control_unavailable", phase="blocked")
        pressed = await button.get_attribute("aria-pressed")
        if pressed not in {"true", "false"} or not await button.is_enabled():
            raise WorkerFault("bold_focus_unavailable", phase="blocked")
        await self._call_before_mutation(before_mutation)
        await self._require_bold_selection(operation.text)
        pressed = await button.get_attribute("aria-pressed")
        if pressed not in {"true", "false"} or not await button.is_enabled():
            raise WorkerFault("bold_focus_unavailable", phase="blocked")
        if (pressed == "true") != operation.bold:
            await button.click()

    async def wait_saved(self) -> None:
        status = self.page.get_by_role("status").filter(has_text=_SAVED)
        await status.wait_for(state="visible", timeout=self.timeout_ms)

    async def verify_after_reopen(self, operation: Operation) -> None:
        self.check_selected_tab_url()
        await self.check_identity()
        await self.check_editable()
        if isinstance(operation, ReplaceTextOperation):
            if operation.replacement:
                await self._search(operation.replacement)
            else:
                current, total = await self._search(operation.find, allow_zero=True)
                if total != 0:
                    raise WorkerFault("reopen_did_not_show_edit", phase="blocked")
        else:
            await self._search(operation.text)
            button = self.page.get_by_role("button", name="Bold", exact=True)
            pressed = await button.get_attribute("aria-pressed")
            if pressed != ("true" if operation.bold else "false"):
                raise WorkerFault("reopen_did_not_show_edit", phase="blocked")
