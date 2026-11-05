"""The source reader must expose trip attachment before a model guesses another skill."""

from unittest.mock import AsyncMock

from chatcore_google.plugin import GooglePlugin


def test_google_read_skill_exposes_attachment_preview_when_resolver_is_wired(google_service):
    service, _, _, _ = google_service
    plugin = GooglePlugin(
        service.settings, service.access, service=service, trip_resolver=AsyncMock()
    )
    skill = next(s for s in plugin.skills() if s.name == "google_workspace")
    assert {
        "trip_attach_file",
        "trip_list_files",
        "trip_read_file",
        "trip_remove_file",
        "trip_edit_doc",
    } <= set(skill.tools)
    assert "prose" in skill.instructions and "preview" in skill.instructions
    tools = {tool.name: tool for tool in plugin.tools()}
    assert tools["trip_edit_doc"].writes
    assert "url" not in tools["trip_edit_doc"].parameters["properties"]
    assert "source_message_ref" in tools["trip_attach_file"].parameters["properties"]


def test_google_reads_stay_available_without_trip_resolver(google_service):
    service, _, _, _ = google_service
    plugin = GooglePlugin(service.settings, service.access, service=service)
    skill = next(s for s in plugin.skills() if s.name == "google_workspace")
    assert "google_read_file" in skill.tools
    assert "trip_attach_file" not in skill.tools
    assert "trip_edit_doc" not in skill.tools
