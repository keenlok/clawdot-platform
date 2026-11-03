import pytest
from chatcore_google.edit_models import EditDestination, EditResult, choose_destination

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_destination_policy_uses_editable_native_original_and_gates_copy_fallback():
    editable = {"capabilities": {"canEdit": True, "canModifyContent": True}}
    assert choose_destination(DOC, editable, requested="auto") == EditDestination(
        mode="original", mime_type=DOC
    )
    with pytest.raises(ValueError, match="copy.*not enabled"):
        choose_destination(
            SHEET,
            {"capabilities": {"canEdit": False, "canCopy": True}},
            requested="auto",
            copy_enabled=False,
        )


def test_original_requires_both_positive_edit_capabilities():
    with pytest.raises(ValueError, match="copy fallback"):
        choose_destination(
            DOC,
            {"capabilities": {"canEdit": True, "canCopy": False}},
            requested="auto",
        )


def test_docx_always_resolves_to_new_native_doc_and_never_original():
    result = choose_destination(
        DOCX,
        {"capabilities": {"canEdit": True, "canDownload": True, "canCopy": True}},
        requested="auto",
        copy_enabled=True,
    )
    assert result == EditDestination(mode="docx_import", mime_type=DOC)


def test_read_only_restriction_routes_to_permitted_copy_without_removing_restriction():
    assert choose_destination(
        DOC,
        {
            "capabilities": {"canEdit": True, "canModifyContent": True, "canCopy": True},
            "contentRestrictions": [{"readOnly": True}],
        },
        requested="auto",
        copy_enabled=True,
    ) == EditDestination(mode="copy", mime_type=DOC)
    with pytest.raises(ValueError, match="copying is not permitted"):
        choose_destination(
            DOC,
            {"capabilities": {"canEdit": False, "canCopy": False}},
            requested="copy",
            copy_enabled=True,
        )


def test_edit_result_allows_uncertain_copy_with_unknown_destination_id():
    result = EditResult(phase="uncertain", source_id="source", failed_stage="copy")
    assert result.destination_id is None


def test_edit_result_is_browser_usable_and_requires_destination_for_unresolved_write():
    result = EditResult(
        phase="partial",
        source_id="source",
        destination_id="destination",
        destination_url="https://docs.google.com/document/d/destination/edit",
        coverage={"tab_id": "tab-1", "start_index": 4, "end_index": 9},
        failed_stage="browser_edit",
    )
    assert result.destination_id == "destination"
    assert result.phase == "partial"
    with pytest.raises(ValueError, match="destination"):
        EditResult(phase="partial", source_id="source")
