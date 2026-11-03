"""Strict broker-to-worker protocol models."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator

JOB_ID = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
FILE_ID = re.compile(r"[A-Za-z0-9_-]{10,128}\Z")
TAB_ID = re.compile(r"(?:t\.[A-Za-z0-9_-]{1,80}|[0-9]{1,20})\Z")
LiteralText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
ReplacementText = Annotated[str, StringConstraints(max_length=2000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReplaceTextOperation(StrictModel):
    kind: Literal["replace_text"]
    find: LiteralText
    replacement: ReplacementText


class BoldTextOperation(StrictModel):
    kind: Literal["bold_text"]
    text: LiteralText
    bold: StrictBool


Operation = Annotated[ReplaceTextOperation | BoldTextOperation, Field(discriminator="kind")]


class CreateJobRequest(StrictModel):
    job_id: str
    expires_at: datetime
    editor_email: str
    destination_file_id: str
    destination_tab_id: str
    expected_revision: str
    operation: Operation

    @field_validator("job_id")
    @classmethod
    def validate_job_id(cls, value: str) -> str:
        if not JOB_ID.fullmatch(value):
            raise ValueError("invalid job id")
        return value

    @field_validator("destination_file_id")
    @classmethod
    def validate_file_id(cls, value: str) -> str:
        if not FILE_ID.fullmatch(value):
            raise ValueError("invalid destination id")
        return value

    @field_validator("destination_tab_id")
    @classmethod
    def validate_tab_id(cls, value: str) -> str:
        if not TAB_ID.fullmatch(value):
            raise ValueError("invalid tab id")
        return value

    @field_validator("expected_revision")
    @classmethod
    def validate_revision_id(cls, value: str) -> str:
        if (
            not value
            or len(value) > 256
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
        ):
            raise ValueError("invalid expected revision")
        return value

    @field_validator("editor_email")
    @classmethod
    def validate_email_shape(cls, value: str) -> str:
        if len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("invalid editor account")
        return value

    @field_validator("expires_at")
    @classmethod
    def require_aware_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expiry must include a timezone")
        return value.astimezone(UTC)


class VerificationRequest(StrictModel):
    result: Literal["verified", "uncertain"]


def validate_expiry(expires_at: datetime, now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("expiry must include a timezone")
    expiry = expires_at.astimezone(UTC)
    current = current.astimezone(UTC)
    if expiry <= current:
        raise ValueError("job is expired")
    if expiry > current + timedelta(seconds=90):
        raise ValueError("job deadline exceeds 90 seconds")
    return expiry


def editor_url(file_id: str, tab_id: str) -> str:
    if not FILE_ID.fullmatch(file_id) or not TAB_ID.fullmatch(tab_id):
        raise ValueError("invalid destination")
    return f"https://docs.google.com/document/d/{file_id}/edit?tab={tab_id}"
