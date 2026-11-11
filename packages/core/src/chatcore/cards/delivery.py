"""Choose the platform's available photo or file delivery capability."""

from collections.abc import Sequence

from chatcore.events import ChatRef
from chatcore.files import FilePlatform, PhotoAlbumPlatform, PhotoPlatform

from .errors import CardError


def can_deliver_cards(platform) -> bool:
    return isinstance(platform, (PhotoPlatform, FilePlatform))


async def send_card_pages(
    platform, chat: ChatRef, pages: Sequence[bytes], *, caption: str | None, thread_id: str | None
) -> None:
    if not pages or not can_deliver_cards(platform):
        raise CardError("unavailable")
    if len(pages) == 1:
        if isinstance(platform, PhotoPlatform):
            await platform.send_photo(
                chat, "card.png", pages[0], caption=caption, thread_id=thread_id
            )
        else:
            await platform.send_file(
                chat, "card.png", pages[0], caption=caption, thread_id=thread_id
            )
    elif isinstance(platform, PhotoAlbumPlatform):
        await platform.send_photos(
            chat,
            [(f"card-{i + 1}.png", page) for i, page in enumerate(pages)],
            caption=caption,
            thread_id=thread_id,
        )
    elif isinstance(platform, PhotoPlatform):
        for i, page in enumerate(pages):
            await platform.send_photo(
                chat,
                f"card-{i + 1}.png",
                page,
                caption=caption if i == 0 else None,
                thread_id=thread_id,
            )
    elif isinstance(platform, FilePlatform):
        for i, page in enumerate(pages):
            await platform.send_file(
                chat,
                f"card-{i + 1}.png",
                page,
                caption=caption if i == 0 else None,
                thread_id=thread_id,
            )
    else:
        raise CardError("unavailable")
