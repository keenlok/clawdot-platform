"""Small, validated contracts shared by Google API and browser edit backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True)
class EditDestination:
    mode: str
    mime_type: str
    file_id: str | None = None
    title: str | None = None
    web_url: str | None = None

    def __post_init__(self):
        if self.mode not in {"original", "copy", "docx_import"}:
            raise ValueError("Unknown Google edit destination mode.")
        if self.mime_type not in {DOC, SHEET}:
            raise ValueError("Google edit destinations must be a native Doc or Sheet.")


@dataclass(frozen=True)
class EditResult:
    """Durable action outcome; safe to hand to a later browser worker."""

    phase: str
    source_id: str
    destination_id: str | None = None
    destination_url: str | None = None
    coverage: dict[str, Any] = field(default_factory=dict)
    failed_stage: str | None = None
    error_category: str | None = None

    def __post_init__(self):
        if self.phase not in {
            "reserved",
            "destination_created",
            "shared",
            "applying",
            "verified",
            "conflict",
            "partial",
            "uncertain",
        }:
            raise ValueError("Unknown Google edit result phase.")
        if not self.source_id:
            raise ValueError("An edit result requires a source ID.")
        if (
            self.phase in {"destination_created", "shared", "applying", "partial"}
            and not self.destination_id
        ):
            raise ValueError("An unresolved edit result must retain its destination ID.")


@dataclass(frozen=True)
class DocMatch:
    text: str
    start_index: int
    end_index: int
    tab_id: str
    uniform_style: dict[str, Any]
    effective_style: dict[str, Any] = field(default_factory=dict)
    style_resolved: bool = False
    named_style_type: str | None = None
    in_table: bool = False
    style_context_supported: bool = True


@dataclass(frozen=True)
class DocParagraph:
    text: str
    start_index: int
    end_index: int
    runs: tuple[dict[str, Any], ...]
    named_style_type: str | None = None
    in_table: bool = False
    style_context_supported: bool = True


@dataclass(frozen=True)
class DocSnapshot:
    file_id: str | None
    revision_id: str
    tab_id: str
    title: str
    text: str
    paragraphs: tuple[DocParagraph, ...]
    complete: bool = True
    named_styles: tuple[dict[str, Any], ...] = ()
    style_context_supported: bool = True


def choose_destination(mime_type, metadata, *, requested="auto", copy_enabled=False):
    """Resolve a safe destination without attempting a remote mutation."""
    caps = metadata.get("capabilities") or {}
    restrictions = metadata.get("contentRestrictions") or []
    read_only = any(item.get("readOnly") is True for item in restrictions if isinstance(item, dict))
    if mime_type == DOCX:
        if caps.get("canDownload") is not True or caps.get("canCopy") is not True:
            raise ValueError(
                "DOCX download or copy is not permitted; no native copy can be prepared."
            )
        if not copy_enabled:
            raise ValueError("DOCX conversion is not enabled until owner-grant qualification.")
        return EditDestination(mode="docx_import", mime_type=DOC)
    if mime_type not in {DOC, SHEET}:
        raise ValueError("Only native Google Docs, Sheets and permitted DOCX files can be edited.")
    if requested not in {"auto", "copy"}:
        raise ValueError("Choose auto or copy as the destination.")
    editable = (
        caps.get("canEdit") is True and caps.get("canModifyContent") is True and not read_only
    )
    if requested == "auto" and editable:
        return EditDestination(mode="original", mime_type=mime_type)
    if not copy_enabled:
        raise ValueError("copy fallback is not enabled until owner-grant qualification.")
    if caps.get("canCopy") is not True:
        raise ValueError("Google says copying is not permitted for this source.")
    return EditDestination(mode="copy", mime_type=mime_type)
