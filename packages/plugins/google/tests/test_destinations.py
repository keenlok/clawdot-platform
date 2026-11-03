import pytest
from chatcore_google.destinations import copy_or_import
from chatcore_google.edit_models import EditDestination


async def test_native_copy_is_fixed_to_owner_my_drive_and_is_never_attempted_before_commit():
    class Client:
        def __init__(self):
            self.calls = []

        async def copy_file(self, file_id, title):
            self.calls.append((file_id, title))
            return {"id": "copy-id", "mimeType": "application/vnd.google-apps.document"}

    client = Client()
    with pytest.raises(ValueError, match="confirmation"):
        await copy_or_import(
            client,
            EditDestination(mode="copy", mime_type="application/vnd.google-apps.document"),
            source_id="source-id",
            title="Edited copy",
            confirmed=False,
        )
    assert client.calls == []
    result = await copy_or_import(
        client,
        EditDestination(mode="copy", mime_type="application/vnd.google-apps.document"),
        source_id="source-id",
        title="Edited copy",
        confirmed=True,
    )
    assert client.calls == [("source-id", "Edited copy")]
    assert result["id"] == "copy-id"


async def test_docx_import_preserves_original_bytes_and_targets_native_doc_mime():
    class Client:
        async def import_docx(self, file_id, title, source_bytes):
            assert (file_id, title, source_bytes) == ("source", "Native copy", b"PK\x03\x04")
            return {"id": "native-id", "mimeType": "application/vnd.google-apps.document"}

    result = await copy_or_import(
        Client(),
        EditDestination(mode="docx_import", mime_type="application/vnd.google-apps.document"),
        source_id="source",
        title="Native copy",
        confirmed=True,
        source_bytes=b"PK\x03\x04",
    )
    assert result["mimeType"] == "application/vnd.google-apps.document"
