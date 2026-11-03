"""Shared, size-bounded image download and format validation for chat attachments."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Literal

from chatcore.events import Attachment, AttachmentKind, ChatRef
from chatcore.llm.types import ImagePart
from chatcore.platform import ChatPlatform, PlatformError

log = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SUPPORTED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_TOO_LARGE_CODES = frozenset({"filetoolarge", "file_too_large"})


class ImageUnavailable(Exception):
    def __init__(self, reason: Literal["download_failed", "too_large", "unsupported_format"]):
        self.reason = reason
        super().__init__(reason)


def error_label(exc: BaseException) -> str:
    label = type(exc).__name__
    if (
        isinstance(exc, PlatformError)
        and exc.args
        and isinstance(exc.args[0], str)
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", exc.args[0])
    ):
        return f"{label}({exc.args[0]})"
    return label


def is_image(attachment: Attachment) -> bool:
    return attachment.kind is AttachmentKind.PHOTO or (attachment.mime_type or "").startswith(
        "image/"
    )


def sniff_image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def download_images(
    platform: ChatPlatform,
    attachments: Iterable[Attachment],
    *,
    chat: ChatRef,
    limit: int,
    max_bytes: int = MAX_IMAGE_BYTES,
    cache: dict[str, bytes] | None = None,
) -> list[ImagePart]:
    output: list[ImagePart] = []
    for attachment in attachments:
        if len(output) >= limit:
            break
        if not is_image(attachment):
            continue
        try:
            output.append(
                await download_image(
                    platform, attachment, chat=chat, max_bytes=max_bytes, cache=cache
                )
            )
        except Exception as exc:
            cause = exc.__cause__ if isinstance(exc, ImageUnavailable) else None
            log.warning(
                "attachment download failed chat=%s error=%s",
                chat.key,
                error_label(cause or exc),
            )
    return output


async def download_image(
    platform: ChatPlatform,
    attachment: Attachment,
    *,
    chat: ChatRef,
    max_bytes: int = MAX_IMAGE_BYTES,
    cache: dict[str, bytes] | None = None,
) -> ImagePart:
    if attachment.size_bytes is not None and attachment.size_bytes > max_bytes:
        raise ImageUnavailable("too_large")
    data = cache.get(attachment.id) if cache is not None else None
    if data is None:
        try:
            data = await platform.download(attachment, chat=chat, max_bytes=max_bytes)
        except PlatformError as exc:
            # Adapters report size-limit refusals as FileTooLarge/file_too_large.
            too_large = bool(exc.args) and str(exc.args[0]).casefold() in _TOO_LARGE_CODES
            raise ImageUnavailable("too_large" if too_large else "download_failed") from exc
        except Exception as exc:
            raise ImageUnavailable("download_failed") from exc
        if cache is not None:
            cache[attachment.id] = data
    if len(data) > max_bytes:
        raise ImageUnavailable("too_large")
    media_type = sniff_image_type(data)
    if media_type not in SUPPORTED_IMAGE_TYPES:
        raise ImageUnavailable("unsupported_format")
    return ImagePart(data, media_type)
