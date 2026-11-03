import pytest
from chatcore_google_browser_editor.docs_ui import DocsUiAdapter
from chatcore_google_browser_editor.models import BoldTextOperation, ReplaceTextOperation
from chatcore_google_browser_editor.worker import WorkerFault


class FakeLocator:
    def __init__(self, page, role, name):
        self.page, self.role, self.name = page, role, name

    async def count(self):
        if self.role == "account":
            return int(self.page.account_label is not None)
        if self.role == "editable":
            return int(self.page.editable)
        if self.role == "dialog":
            return int(self.page.dialog_open)
        if self.role == "dialogs":
            return int(self.page.dialog_open)
        if self.role == "matches":
            return self.page.match_count
        if self.role == "search":
            return int(self.page.search_open)
        if self.role == "selection_status":
            return int(self.page.selected_text is not None)
        if self.role == "editor":
            return 1
        return 1

    def get_by_role(self, role, name=None, **_kwargs):
        return self.page.get_by_role(role, name)

    async def get_attribute(self, name):
        if self.role == "account" and name == "aria-label":
            return self.page.account_label
        if self.role == "bold" and name == "aria-pressed":
            return (
                "true"
                if self.page.bold_by_text.get(self.page.selected_text, self.page.bold)
                else "false"
            )
        if self.role == "focused" and name in {"aria-label", "role"}:
            return self.page.focused_attributes.get(name)
        if self.role in {"dialog", "dialogs"} and name == "aria-label" and self.page.dialog_open:
            return "Find and replace"
        return None

    async def fill(self, value):
        self.page.search_text = value
        if self.role == "search":
            self.page.selected_text = value if self.page.selection_available else None

    async def click(self):
        if self.role == "replace":
            self.page.replaced = True
        elif self.role == "bold":
            text = self.page.selected_text
            self.page.bold_by_text[text] = not self.page.bold_by_text.get(text, self.page.bold)
        elif self.role == "done":
            self.page.dialog_open = False
        elif self.role == "checkbox":
            self.page.checkboxes[self.name] = not self.page.checkboxes[self.name]

    async def press(self, key):
        if key == "Escape":
            self.page.dialog_open = False
            self.page.search_open = False
            self.page.focused_attributes = {"aria-label": "Document editor", "role": "textbox"}

    async def is_enabled(self):
        return self.page.focus_ok

    async def is_checked(self):
        return self.page.checkboxes[self.name]

    async def is_visible(self):
        if self.role == "search":
            return self.page.search_open
        if self.role in {"editor", "focused"}:
            return True
        if self.role == "dialog":
            return self.page.dialog_open
        return True

    async def wait_for(self, **_kwargs):
        return None

    async def all_inner_texts(self):
        if self.role == "selection_status":
            return [f"Selected text: {self.page.selected_text}"] if self.page.selected_text else []
        return [f"1 of {self.page.match_count}"]

    async def all(self):
        if self.role == "checkbox":
            return [self]
        return (
            [FakeLocator(self.page, "dialog", "Find and replace")] if self.page.dialog_open else []
        )


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        if key == "Control+h":
            self.page.dialog_open = True
        elif key == "Control+f":
            self.page.search_open = True
        elif key == "Escape":
            self.page.dialog_open = False
            self.page.search_open = False
            self.page.focused_attributes = {"aria-label": "Document editor", "role": "textbox"}


class FakePage:
    def __init__(
        self, *, count=1, account="Google Account: editor@example.com", bold=False, focus=True
    ):
        self.url = (
            "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit?tab=t.0"
        )
        self.match_count = count
        self.account_label = account
        self.editable = True
        self.bold = bold
        self.focus_ok = focus
        self.selection_available = True
        self.selected_text = None
        self.bold_by_text = {}
        self.focused_attributes = {"aria-label": "Document editor", "role": "textbox"}
        self.dialog_open = False
        self.search_open = False
        self.search_text = ""
        self.replaced = False
        self.checkboxes = {
            "Search all tabs": False,
            "Search in current tab": True,
            "Match case": False,
            "Match using regular expressions": False,
        }
        self.keyboard = FakeKeyboard(self)

    def get_by_role(self, role, name=None, **_kwargs):
        mapping = {
            ("button", "Google Account"): "account",
            ("button", "Editing"): "editable",
            ("dialog", "Find and replace"): "dialog",
            ("textbox", "Find"): "find",
            ("textbox", "Replace with"): "replace_text",
            ("button", "Replace"): "replace",
            ("textbox", "Find in document"): "search",
            ("textbox", "Document editor"): "editor",
            ("button", "Bold"): "bold",
            ("status", "saved"): "saved",
            ("status", "Current selection"): "selection_status",
            ("checkbox", "Search all tabs"): "checkbox",
            ("checkbox", "Search in current tab"): "checkbox",
            ("checkbox", "Match case"): "checkbox",
            ("checkbox", "Match using regular expressions"): "checkbox",
        }
        if role == "button" and getattr(name, "pattern", None) == "Google Account":
            mapped = "account"
        else:
            mapped = mapping.get(
                (role, name),
                "done"
                if name == "Done"
                else "dialogs"
                if role == "dialog" and name is None
                else role,
            )
        return FakeLocator(self, mapped, name)

    def locator(self, selector):
        assert selector == ":focus"
        return FakeLocator(self, "focused", selector)

    def get_by_text(self, text, exact=False):
        return FakeLocator(self, "matches", text)


@pytest.mark.asyncio
async def test_identity_must_match_dedicated_editor_and_not_owner() -> None:
    ui = DocsUiAdapter(FakePage(account="Google Account: owner@example.com"), "editor@example.com")
    with pytest.raises(WorkerFault) as error:
        await ui.check_identity()
    assert error.value.category == "wrong_editor_account"


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 2])
async def test_replace_requires_exactly_one_match_before_any_mutation(count: int) -> None:
    page = FakePage(count=count)
    ui = DocsUiAdapter(page, "editor@example.com", "t.0")
    calls = []

    async def before_mutation():
        calls.append(True)

    with pytest.raises(WorkerFault):
        await ui.apply(
            ReplaceTextOperation(kind="replace_text", find="before", replacement="after"),
            before_mutation,
        )
    assert calls == []
    assert not page.replaced


@pytest.mark.asyncio
async def test_replace_runs_exactly_one_selected_accessible_action() -> None:
    page = FakePage()
    ui = DocsUiAdapter(page, "editor@example.com", "t.0")
    calls = []

    async def before_mutation():
        calls.append("mutation")

    await ui.apply(
        ReplaceTextOperation(kind="replace_text", find="before", replacement="after"),
        before_mutation,
    )
    assert page.replaced
    assert calls == ["mutation"]


@pytest.mark.asyncio
async def test_bold_reads_current_state_and_toggles_only_when_needed() -> None:
    page = FakePage(bold=False)
    ui = DocsUiAdapter(page, "editor@example.com", "t.0")
    calls = []

    async def before_mutation():
        calls.append("mutation")

    async def again():
        calls.append("again")

    await ui.apply(
        BoldTextOperation(kind="bold_text", text="heading", bold=True),
        before_mutation,
    )
    assert page.bold_by_text["heading"] is True
    assert calls == ["mutation"]

    already_bold = FakePage(bold=True)
    await DocsUiAdapter(already_bold, "editor@example.com", "t.0").apply(
        BoldTextOperation(kind="bold_text", text="heading", bold=True), again
    )
    assert already_bold.bold_by_text.get("heading", already_bold.bold) is True
    assert calls == ["mutation", "again"]


@pytest.mark.asyncio
async def test_bold_focus_loss_fails_before_mutation() -> None:
    page = FakePage(focus=False)
    calls = []

    async def before_mutation():
        calls.append("mutation")

    with pytest.raises(WorkerFault):
        await DocsUiAdapter(page, "editor@example.com", "t.0").apply(
            BoldTextOperation(kind="bold_text", text="heading", bold=True),
            before_mutation,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_bold_rechecks_focus_and_selected_text_after_journal_guard() -> None:
    page = FakePage()
    phases = []

    async def before_mutation():
        phases.append("running")
        page.selected_text = "different text"
        page.focused_attributes = {"aria-label": "Bold", "role": "button"}

    with pytest.raises(WorkerFault) as error:
        await DocsUiAdapter(page, "editor@example.com", "t.0").apply(
            BoldTextOperation(kind="bold_text", text="heading", bold=True),
            before_mutation,
        )
    assert error.value.category == "selection_unavailable"
    assert phases == ["running"]
    assert page.bold_by_text.get("heading", False) is False
    assert page.bold_by_text.get("different text", False) is False


@pytest.mark.asyncio
async def test_bold_requires_accessible_selection_status_before_guard() -> None:
    page = FakePage()
    page.selection_available = False
    calls = []

    async def before_mutation():
        calls.append(True)

    with pytest.raises(WorkerFault) as error:
        await DocsUiAdapter(page, "editor@example.com", "t.0").apply(
            BoldTextOperation(kind="bold_text", text="heading", bold=True),
            before_mutation,
        )
    assert error.value.category == "selection_unavailable"
    assert calls == []


@pytest.mark.asyncio
async def test_ambiguous_account_label_and_wrong_tab_are_refused() -> None:
    label = "Google Account: editor@example.com (owner@example.com)"
    with pytest.raises(WorkerFault):
        await DocsUiAdapter(FakePage(account=label), "editor@example.com", "t.0").check_identity()
    page = FakePage()
    page.url = page.url.replace("tab=t.0", "tab=t.1")
    ui = DocsUiAdapter(page, "editor@example.com", "t.0")
    with pytest.raises(WorkerFault):
        await ui.apply(
            ReplaceTextOperation(kind="replace_text", find="before", replacement="after"),
            lambda: None,
        )
