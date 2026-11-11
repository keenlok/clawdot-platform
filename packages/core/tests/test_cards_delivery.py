import pytest
from chatcore.cards.delivery import can_deliver_cards, send_card_pages
from chatcore.cards.errors import CardError
from chatcore.events import ChatRef
from chatcore.testing import FakePlatform


@pytest.mark.asyncio
async def test_fake_platform_album_delivery():
    platform = FakePlatform()
    chat = ChatRef("fake", "group")
    assert can_deliver_cards(platform)
    await send_card_pages(platform, chat, [b"a", b"b"], caption="c", thread_id="t")
    assert platform.sent_albums[0][2:] == ("c", "t")


@pytest.mark.asyncio
async def test_platform_without_card_capability_fails():
    with pytest.raises(CardError):
        await send_card_pages(object(), ChatRef("fake", "x"), [b"x"], caption=None, thread_id=None)


class PhotoOnly:
    async def send_photo(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return None

    def __init__(self):
        self.calls = []


class FileOnly:
    async def send_file(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return None

    def __init__(self):
        self.calls = []


class AlbumPhoto(PhotoOnly):
    async def send_photos(self, *args, **kwargs):
        self.album_call = (args, kwargs)
        return None


@pytest.mark.asyncio
async def test_delivery_all_single_and_multi_page_branches():
    chat = ChatRef("fake", "group")
    photo = PhotoOnly()
    await send_card_pages(photo, chat, [b"one"], caption="cap", thread_id="t")
    assert photo.calls[0][0][1:3] == ("card.png", b"one")

    album = AlbumPhoto()
    await send_card_pages(album, chat, [b"a", b"b"], caption="cap", thread_id="t")
    assert album.album_call[0][1] == [("card-1.png", b"a"), ("card-2.png", b"b")]

    fallback_photo = PhotoOnly()
    await send_card_pages(fallback_photo, chat, [b"a", b"b"], caption="cap", thread_id="t")
    assert [call[1]["caption"] for call in fallback_photo.calls] == ["cap", None]

    file = FileOnly()
    await send_card_pages(file, chat, [b"one"], caption="cap", thread_id="t")
    await send_card_pages(file, chat, [b"a", b"b"], caption="cap", thread_id="t")
    assert [call[0][1] for call in file.calls] == ["card.png", "card-1.png", "card-2.png"]


@pytest.mark.asyncio
async def test_photo_failure_never_falls_back_to_document():
    class FailingPhoto(FakePlatform):
        async def send_photo(self, *args, **kwargs):
            raise RuntimeError("photo failed")

    platform = FailingPhoto()
    with pytest.raises(RuntimeError, match="photo failed"):
        await send_card_pages(
            platform, ChatRef("fake", "group"), [b"png"], caption="cap", thread_id=None
        )
    assert not platform.sent_files
