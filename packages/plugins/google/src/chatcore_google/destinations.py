"""Confirmed destination creation shared by the API editor and optional broker."""

from chatcore_google.edit_models import EditDestination


async def copy_or_import(
    client, destination: EditDestination, *, source_id, title, confirmed, source_bytes=None
):
    if not confirmed:
        raise ValueError("A requester confirmation is required before creating a destination.")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("Destination title must contain 1–200 characters.")
    if destination.mode == "copy":
        return await client.copy_file(source_id, title)
    if destination.mode == "docx_import":
        if not isinstance(source_bytes, bytes) or not source_bytes:
            raise ValueError("DOCX source bytes are required for native Google Doc conversion.")
        # Return the raw ID-bearing result. The caller journals any ID before validating MIME.
        return await client.import_docx(source_id, title, source_bytes)
    raise ValueError("Original destinations do not create a new file.")
