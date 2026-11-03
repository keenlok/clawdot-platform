"""Optional outgoing-file capability, independent of the required platform contract."""

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
