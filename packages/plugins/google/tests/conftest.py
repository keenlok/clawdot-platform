from unittest.mock import AsyncMock

import pytest
from chatcore.access import AccessPolicy
from chatcore.plugin import CommandContext
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext, TurnState
from chatcore_google.repository import GoogleRepository
from chatcore_google.service import GoogleService
from chatcore_google.settings import GoogleSettings


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
