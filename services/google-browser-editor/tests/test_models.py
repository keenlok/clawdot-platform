import json
from datetime import UTC, datetime, timedelta

import pytest
from chatcore_google_browser_editor.models import (
    BoldTextOperation,
    CreateJobRequest,
    ReplaceTextOperation,
    editor_url,
    validate_expiry,
)
from pydantic import ValidationError


def valid_payload(**changes):
    payload = {
        "job_id": "a1b2c3d4e5f6478899aabbccddeeff00",
        "expires_at": datetime.now(UTC) + timedelta(seconds=30),
        "editor_email": "editor@example.com",
        "destination_file_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "destination_tab_id": "t.0",
        "expected_revision": "revision-opaque-01",
        "operation": {"kind": "replace_text", "find": "before", "replacement": "after"},
    }
    payload.update(changes)
    return payload


def test_request_accepts_only_bounded_supported_operation() -> None:
    request = CreateJobRequest.model_validate(valid_payload())
    assert isinstance(request.operation, ReplaceTextOperation)
    assert request.destination_file_id.startswith("1Ab")


def test_request_accepts_iso_timestamp_in_json_body() -> None:
    payload = valid_payload()
    payload["expires_at"] = payload["expires_at"].isoformat()
    assert CreateJobRequest.model_validate_json(json.dumps(payload)).job_id


def test_request_rejects_unknown_top_level_and_operation_fields() -> None:
    with pytest.raises(ValidationError):
        CreateJobRequest.model_validate(valid_payload(unrestricted_url="https://example.com"))
    payload = valid_payload()
    payload["operation"]["script"] = "alert(1)"
    with pytest.raises(ValidationError):
        CreateJobRequest.model_validate(payload)


def test_request_rejects_invalid_ids_and_oversized_literals() -> None:
    with pytest.raises(ValidationError):
        CreateJobRequest.model_validate(valid_payload(destination_file_id="../../etc/passwd"))
    payload = valid_payload()
    payload["operation"]["find"] = "x" * 2001
    with pytest.raises(ValidationError):
        CreateJobRequest.model_validate(payload)


def test_bold_operation_is_strict_and_bounded() -> None:
    op = BoldTextOperation.model_validate({"kind": "bold_text", "text": "A title", "bold": True})
    assert op.bold is True
    with pytest.raises(ValidationError):
        BoldTextOperation.model_validate(
            {"kind": "bold_text", "text": "x", "bold": True, "click": True}
        )


def test_replacement_can_be_empty_and_revision_is_opaque_printable_text() -> None:
    payload = valid_payload()
    payload["operation"]["replacement"] = ""
    payload["expected_revision"] = "opaque rev/with punctuation=="
    request = CreateJobRequest.model_validate(payload)
    assert request.operation.replacement == ""


def test_expiry_must_be_aware_and_within_90_seconds() -> None:
    now = datetime(2026, 11, 3, tzinfo=UTC)
    assert validate_expiry(now + timedelta(seconds=90), now) == now + timedelta(seconds=90)
    with pytest.raises(ValueError):
        validate_expiry(now - timedelta(seconds=1), now)
    with pytest.raises(ValueError):
        validate_expiry(now + timedelta(seconds=91), now)
    with pytest.raises(ValueError):
        validate_expiry(datetime(2026, 11, 3))


def test_editor_url_is_fixed_and_uses_only_validated_identifiers() -> None:
    assert editor_url("1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "t.0") == (
        "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit?tab=t.0"
    )
    with pytest.raises(ValueError):
        editor_url("example.com", "t.0")
