import copy
from unittest.mock import AsyncMock

import pytest
from chatcore.access import AccessPolicy
from chatcore.plugin import CommandContext
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext, TurnState
from chatcore_google.edit_snapshot import utf16_length
from chatcore_google.repository import GoogleRepository
from chatcore_google.service import GoogleService
from chatcore_google.settings import GoogleSettings


def _rich_doc_document(
    content, *, title="Rich Doc", document_id="rich-doc-id", revision="rich-revision-1"
):
    return {
        "documentId": document_id,
        "title": title,
        "revisionId": revision,
        "suggestionsViewMode": "SUGGESTIONS_INLINE",
        "tabs": [
            {
                "tabProperties": {"tabId": "rich-tab", "title": title},
                "documentTab": {"body": {"content": content}},
            }
        ],
        "namedStyles": {"styles": [{"namedStyleType": "NORMAL_TEXT", "textStyle": {}}]},
        "documentStyle": {},
    }


@pytest.fixture
def rich_doc_response():
    def build(plan, *, table_padding=False, empty_tables=False, title="Rich Doc"):
        content = [{"startIndex": 0, "endIndex": 1, "sectionBreak": {"sectionStyle": {}}}]
        index = 1

        def paragraph(text, named_style="NORMAL_TEXT", *, border=None, bullet=None):
            nonlocal index
            value = text + "\n"
            start = index
            end = start + utf16_length(value)
            content.append(
                {
                    "startIndex": start,
                    "endIndex": end,
                    "paragraph": {
                        "elements": [
                            {
                                "startIndex": start,
                                "endIndex": end,
                                "textRun": {"content": value, "textStyle": {}},
                            }
                        ],
                        "paragraphStyle": {
                            "namedStyleType": named_style,
                            **({"borderBottom": border} if border else {}),
                        },
                        **({"bullet": bullet} if bullet else {}),
                    },
                }
            )
            index = end

        for item in plan.expected:
            if item[0] == "paragraph":
                paragraph(
                    item[1],
                    item[2],
                    border={"width": {"magnitude": 1, "unit": "PT"}} if item[3] else None,
                    bullet={"listId": "list-1"} if item[4] else None,
                )
                continue
            if table_padding:
                paragraph("")
            table_start = index
            cell_index = table_start + 1
            table_rows = []
            for row in item[1]:
                cells = []
                for value in row:
                    value = "" if empty_tables else (value or "")
                    cell_text = value + "\n"
                    cell_end = cell_index + utf16_length(cell_text)
                    cells.append(
                        {
                            "startIndex": cell_index,
                            "endIndex": cell_end + 1,
                            "content": [
                                {
                                    "startIndex": cell_index,
                                    "endIndex": cell_end,
                                    "paragraph": {
                                        "elements": [
                                            {
                                                "startIndex": cell_index,
                                                "endIndex": cell_end,
                                                "textRun": {"content": cell_text, "textStyle": {}},
                                            }
                                        ],
                                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                    },
                                }
                            ],
                        }
                    )
                    cell_index = cell_end + 1
                table_rows.append({"tableCells": cells})
            table_end = cell_index
            content.append(
                {
                    "startIndex": table_start,
                    "endIndex": table_end,
                    "table": {"tableRows": table_rows, "tableStyle": {}},
                }
            )
            index = table_end
            if table_padding:
                paragraph("")

        return _rich_doc_document(content, title=title)

    return build


class RequestDrivenRichDocApi:
    """Small Docs API fake that derives read-back only from recorded batch requests."""

    def __init__(self, *, initial_nodes=None, tab_id="rich-tab"):
        self.document_id = "rich-created-id"
        self.title = ""
        self.tab_id = tab_id
        self.revision = "rich-revision-1"
        self.nodes = (
            copy.deepcopy(initial_nodes)
            if initial_nodes is not None
            else [
                {
                    "kind": "paragraph",
                    "text": "\n",
                    "style": {"namedStyleType": "NORMAL_TEXT"},
                    "inline": [],
                }
            ]
        )
        self.batch_a = []
        self.batch_b = []
        self.batch_c = []
        self.revision_inputs = []
        self.document_reads = 0
        self.omit_write_control = False
        self.fail_batch = None
        self.fail_error = None
        self.fail_rename = False
        self.rename_attempted = False
        self.name_after_rename_error = "Rich Doc (incomplete)"
        self.raise_metadata_after_rename = False
        self.corrupt_text = False
        self.drop_text_style = set()
        self.permissions = [{"id": "owner-id", "type": "user", "role": "owner"}]
        self.metadata_reads = []

    def wire(self, client):
        client.request.side_effect = self.request
        client.document_for_edit.side_effect = self.document_for_edit
        client.edit_document.side_effect = self.edit_document
        client.rename_file.side_effect = self.rename_file
        client.metadata.side_effect = self.metadata
        client.list_permissions.side_effect = self.list_permissions
        client.grant_link_viewer.side_effect = self.grant_viewer
        return client

    async def request(self, method, host, path, **kwargs):
        if path == "/v1/documents":
            self.title = kwargs["json"]["title"]
            return {"documentId": self.document_id}
        if not path.endswith(":batchUpdate"):
            return {}
        requests = kwargs["json"]["requests"]
        batch = "B" if any("insertTable" in item for item in requests) else "A"
        if batch == "A":
            self.batch_a.append(copy.deepcopy(requests))
        else:
            self.batch_b.append(copy.deepcopy(requests))
        if self.fail_batch == batch:
            raise self.fail_error or RuntimeError(f"test batch {batch} failed")
        if batch == "A":
            self._apply_batch_a(requests)
        else:
            self._apply_batch_b(requests)
        self.revision = f"after-{batch}"
        return self._batch_response()

    def _batch_response(self):
        response = {"documentId": self.document_id, "replies": []}
        if not self.omit_write_control:
            response["writeControl"] = {"requiredRevisionId": self.revision}
        return response

    async def document_for_edit(self, file_id):
        self.document_reads += 1
        return self._document()

    async def edit_document(self, file_id, requests, revision_id):
        self.revision_inputs.append(revision_id)
        if not isinstance(revision_id, str) or revision_id != self.revision:
            raise ValueError("The Docs revision precondition is missing or stale.")
        for request in requests:
            operation = next(iter(request.values()))
            target = operation.get("location") or operation.get("range")
            if target and target.get("tabId", self.tab_id) != self.tab_id:
                raise AssertionError("request targeted a different Docs tab")
        if any("insertTable" in request for request in requests):
            self.batch_b.append(copy.deepcopy(requests))
            if self.fail_batch == "B":
                raise self.fail_error or RuntimeError("test batch B failed")
            self._apply_batch_b(requests)
            self.revision = "after-B"
        elif any("deleteParagraphBullets" in request for request in requests) or any(
            operation.get("fields") == "*" for request in requests for operation in request.values()
        ):
            self.batch_a.append(copy.deepcopy(requests))
            if self.fail_batch == "A":
                raise self.fail_error or RuntimeError("test batch A failed")
            self._apply_batch_a(requests)
            self.revision = "after-A"
        else:
            self.batch_c.append(copy.deepcopy(requests))
            if self.fail_batch == "C":
                raise self.fail_error or RuntimeError("test batch C failed")
            self._apply_batch_c(requests)
            self.revision = "after-C"
        return self._batch_response()

    async def metadata(self, file_id):
        self.metadata_reads.append(file_id)
        if self.raise_metadata_after_rename and self.rename_attempted:
            raise RuntimeError("test metadata unavailable")
        return {
            "id": file_id,
            "name": self.title,
            "mimeType": "application/vnd.google-apps.document",
            "isAppAuthorized": True,
            "ownedByMe": True,
            "owners": [{"emailAddress": "owner@example.com", "permissionId": "owner-id"}],
            "capabilities": {"canEdit": True, "canModifyContent": True, "canShare": True},
            "trashed": False,
        }

    async def rename_file(self, file_id, name):
        self.rename_attempted = True
        if self.fail_rename:
            if self.name_after_rename_error is not None:
                self.title = self.name_after_rename_error
            raise RuntimeError("test rename failed")
        self.title = name
        return {"id": file_id, "name": name}

    async def list_permissions(self, file_id):
        return {"permissions": copy.deepcopy(self.permissions)}

    async def grant_viewer(self, file_id):
        self.permissions.append(
            {"id": "viewer-link", "type": "anyone", "role": "reader", "allowFileDiscovery": False}
        )

    def _paragraphs(self):
        cursor = 1
        for node in self.nodes:
            if node["kind"] == "paragraph":
                end = cursor + utf16_length(node["text"])
                yield node, cursor, end
                cursor = end
            else:
                cursor += self._table_length(node)

    @staticmethod
    def _table_length(node):
        return 1 + sum(utf16_length(cell["text"]) + 1 for row in node["rows"] for cell in row)

    def _table_cells(self):
        cursor = 1
        for node in self.nodes:
            if node["kind"] == "paragraph":
                cursor += utf16_length(node["text"])
                continue
            cell_cursor = cursor + 1
            for row in node["rows"]:
                for cell in row:
                    yield cell, cell_cursor
                    cell_cursor += utf16_length(cell["text"]) + 1
            cursor += self._table_length(node)

    def _apply_batch_a(self, requests):
        insert = next((item["insertText"] for item in requests if "insertText" in item), None)
        if insert:
            location = insert["location"]["index"]
            cursor = 1
            for position, node in enumerate(self.nodes):
                if node["kind"] != "paragraph":
                    cursor += self._table_length(node)
                    continue
                end = cursor + utf16_length(node["text"])
                if cursor <= location <= end:
                    offset = location - cursor
                    prefix_chars, used = [], 0
                    for char in node["text"]:
                        if used == offset:
                            break
                        prefix_chars.append(char)
                        used += utf16_length(char)
                    prefix = "".join(prefix_chars)
                    combined = prefix + insert["text"] + node["text"][len(prefix) :]
                    texts = combined.splitlines(keepends=True)
                    if not texts or not texts[-1].endswith("\n"):
                        texts.append("")
                    fresh = []
                    for text in texts:
                        fresh_node = {
                            "kind": "paragraph",
                            "text": text,
                            "style": copy.deepcopy(node["style"]),
                            "inline": copy.deepcopy(node.get("inline", [])),
                        }
                        if node.get("bullet"):
                            fresh_node["bullet"] = copy.deepcopy(node["bullet"])
                        fresh.append(fresh_node)
                    self.nodes[position : position + 1] = fresh
                    break
                cursor = end
        for item in requests:
            if "updateParagraphStyle" in item:
                request = item["updateParagraphStyle"]
                start, end = request["range"]["startIndex"], request["range"]["endIndex"]
                for node, left, right in self._paragraphs():
                    if left < end and right > start:
                        if request.get("fields") == "*":
                            node["style"] = copy.deepcopy(request["paragraphStyle"])
                        else:
                            node["style"].update(request["paragraphStyle"])
            elif "createParagraphBullets" in item:
                request = item["createParagraphBullets"]
                start, end = request["range"]["startIndex"], request["range"]["endIndex"]
                for node, left, right in self._paragraphs():
                    if left < end and right > start:
                        node["bullet"] = {"listId": "list-1", "preset": request["bulletPreset"]}
            elif "updateTextStyle" in item:
                request = item["updateTextStyle"]
                start, end = request["range"]["startIndex"], request["range"]["endIndex"]
                style = {
                    key: value
                    for key, value in request["textStyle"].items()
                    if key not in self.drop_text_style
                }
                for node, left, _ in self._paragraphs():
                    if left < end and left + utf16_length(node["text"]) > start:
                        if request.get("fields") == "*":
                            node["inline"] = []
                        else:
                            node["inline"].append((start - left, end - left, style))
            elif "deleteParagraphBullets" in item:
                request = item["deleteParagraphBullets"]
                start, end = request["range"]["startIndex"], request["range"]["endIndex"]
                for node, left, right in self._paragraphs():
                    if left < end and right > start:
                        node.pop("bullet", None)

    def _apply_batch_b(self, requests):
        for item in requests:
            request = item["insertTable"]
            location = request["location"]["index"]
            for node, start, end in self._paragraphs():
                if start <= location < end:
                    position = next(
                        i for i, candidate in enumerate(self.nodes) if candidate is node
                    )
                    table = {
                        "kind": "table",
                        "rows": [
                            [{"text": "\n", "style": {}} for _ in range(request["columns"])]
                            for _ in range(request["rows"])
                        ],
                    }
                    before = {
                        "kind": "paragraph",
                        "text": "\n",
                        "style": {"namedStyleType": "NORMAL_TEXT"},
                        "inline": [],
                    }
                    self.nodes[position:position] = [before, table]
                    break

    def _apply_batch_c(self, requests):
        for item in requests:
            if "insertText" in item:
                location = item["insertText"]["location"]["index"]
                value = item["insertText"]["text"]
                for cell, start in self._table_cells():
                    if start == location:
                        cell["text"] = value + "\n"
                        break
                else:
                    raise AssertionError(
                        f"cell insertion index {location} did not come from read-back"
                    )
            elif "updateTextStyle" in item:
                request = item["updateTextStyle"]
                start = request["range"]["startIndex"]
                for cell, cell_start in self._table_cells():
                    if cell_start <= start < cell_start + utf16_length(cell["text"]):
                        cell["style"].update(request["textStyle"])

    @staticmethod
    def _paragraph_element(text, start, end, style, inline=None):
        inline = inline or []
        boundaries = {0, utf16_length(text)}
        for left, right, _ in inline:
            boundaries.update((max(0, left), min(utf16_length(text), right)))
        ordered = sorted(boundaries)
        chars = []
        index = 0
        for char in text:
            size = utf16_length(char)
            chars.append((index, index + size, char))
            index += size
        runs = []
        for left, right in zip(ordered, ordered[1:], strict=False):
            value = "".join(char for a, b, char in chars if a < right and b > left)
            if not value:
                continue
            run_style = {}
            for style_left, style_right, marked in inline:
                if style_left < right and style_right > left:
                    run_style.update(marked)
            run_start = start + left
            run_end = run_start + utf16_length(value)
            text_run = {"content": value}
            if run_style:
                text_run["textStyle"] = run_style
            runs.append({"startIndex": run_start, "endIndex": run_end, "textRun": text_run})
        return {
            "startIndex": start,
            "endIndex": end,
            "paragraph": {"elements": runs, "paragraphStyle": style},
        }

    def _document(self):
        content = [{"startIndex": 0, "endIndex": 1, "sectionBreak": {"sectionStyle": {}}}]
        cursor = 1
        for node in self.nodes:
            if node["kind"] == "paragraph":
                end = cursor + utf16_length(node["text"])
                item = self._paragraph_element(
                    node["text"], cursor, end, copy.deepcopy(node["style"]), node["inline"]
                )
                if node.get("bullet"):
                    item["paragraph"]["bullet"] = copy.deepcopy(node["bullet"])
                content.append(item)
                cursor = end
            else:
                table_start = cursor
                cell_cursor = table_start + 1
                rows = []
                for row in node["rows"]:
                    cells = []
                    for cell in row:
                        end = cell_cursor + utf16_length(cell["text"])
                        para = self._paragraph_element(
                            cell["text"], cell_cursor, end, {"namedStyleType": "NORMAL_TEXT"}
                        )
                        if cell["style"]:
                            for run in para["paragraph"]["elements"]:
                                run["textRun"]["textStyle"] = copy.deepcopy(cell["style"])
                        cells.append(
                            {"startIndex": cell_cursor, "endIndex": end + 1, "content": [para]}
                        )
                        cell_cursor = end + 1
                    rows.append({"tableCells": cells})
                cursor = cell_cursor
                content.append(
                    {
                        "startIndex": table_start,
                        "endIndex": cursor,
                        "table": {"tableRows": rows, "tableStyle": {}},
                    }
                )
        document = _rich_doc_document(
            content,
            title=self.title or "Rich Doc",
            document_id=self.document_id,
            revision=self.revision,
        )
        document["tabs"][0]["tabProperties"]["tabId"] = self.tab_id
        if self.corrupt_text:
            for item in content:
                if "paragraph" in item:
                    run = item["paragraph"]["elements"][0].get("textRun")
                    if run and run.get("content", "\n") != "\n":
                        run["content"] = "Corrupted\n"
                        break
        return document


@pytest.fixture
def request_driven_rich_doc_api():
    return RequestDrivenRichDocApi()


@pytest.fixture
def docx_bytes():
    import io
    import zipfile

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>',
        )
        package.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Tokyo itinerary</w:t></w:r></w:p></w:body></w:document>",
        )
    return output.getvalue()


@pytest.fixture
def google_service():
    actor, stores, platform = user("owner"), memory_stores(), FakePlatform()
    access = AccessPolicy([GROUP.key], [actor.ref.key], owners=[actor.ref.key])
    config = GoogleSettings(owner_email="owner@example.com", oauth_credentials_file="unused")
    client = AsyncMock()
    client.identity.return_value = "owner-id"
    client.metadata.return_value = {
        "id": "file",
        "name": "Trip",
        "mimeType": "application/vnd.google-apps.spreadsheet",
    }
    client.sheet.return_value = {
        "sheets": [
            {"properties": {"sheetId": 7, "title": "Tokyo"}},
            {"properties": {"sheetId": 8, "title": "Kyoto"}},
        ]
    }
    service = GoogleService(config, access, client, GoogleRepository())
    ctx = ToolContext(
        GROUP,
        actor,
        False,
        platform,
        stores,
        turn=TurnState(human_urls=frozenset({"https://docs.google.com/spreadsheets/d/file/edit"})),
        is_admin=True,
    )
    command = CommandContext(GROUP, actor, False, True, platform, stores)
    return service, client, ctx, command
