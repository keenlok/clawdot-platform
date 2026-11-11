"""Optional outgoing-file capability, independent of the required platform contract."""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from chatcore.events import ChatRef, MessageRef


@runtime_checkable
class FilePlatform(Protocol):
    async def send_file(
        self,
        chat: ChatRef,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        """Send ``data`` as a document; errors contain only the error class name."""
        ...


@runtime_checkable
class VoicePlatform(Protocol):
    """Optional capability for sending a short voice message as a response attachment."""

    async def send_voice(
        self,
        chat: ChatRef,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef: ...


@runtime_checkable
class PhotoPlatform(Protocol):
    """Optional capability for sending one image inline as a response attachment."""

    async def send_photo(
        self,
        chat: ChatRef,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef: ...


@runtime_checkable
class PhotoAlbumPlatform(Protocol):
    """Optional capability for sending 2–10 images as one album."""

    async def send_photos(
        self,
        chat: ChatRef,
        photos: Sequence[tuple[str, bytes]],
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef: ...
