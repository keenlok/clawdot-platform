import logging

import pytest
from chatcore.events import Attachment, AttachmentKind
from chatcore.images import (
    MAX_IMAGE_BYTES,
    ImageUnavailable,
    download_image,
    download_images,
    error_label,
    is_image,
    sniff_image_type,
)
from chatcore.testing import GROUP, FakePlatform

JPEG = b"\xff\xd8\xff" + b"x"
PNG = b"\x89PNG\r\n\x1a\n" + b"x"
WEBP = b"RIFF" + (b"\x00" * 4) + b"WEBP" + b"x"


@pytest.mark.parametrize(
    ("data", "expected"),
    [(JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp")],
)
def test_sniff_supported_image(data, expected):
    assert sniff_image_type(data) == expected


@pytest.mark.parametrize("data", [b"", b"GIF89a", b"\x00\x00\x00\x18ftypheic", b"%PDF-1.7", b"bad"])
def test_sniff_rejects_unsupported_data(data):
    assert sniff_image_type(data) is None


def test_is_image_uses_kind_or_image_mime():
    assert is_image(Attachment("1", AttachmentKind.PHOTO))
    assert is_image(Attachment("2", AttachmentKind.DOCUMENT, mime_type="image/png"))
    assert not is_image(Attachment("3", AttachmentKind.DOCUMENT, mime_type="application/pdf"))


async def test_download_images_sniffs_and_filters_before_and_after_download(caplog):
    platform = FakePlatform()
    good_photo = Attachment("photo", AttachmentKind.PHOTO)
    good_document = Attachment("png", AttachmentKind.DOCUMENT, mime_type="image/png")
    fake_pdf = Attachment("pdf", AttachmentKind.DOCUMENT, mime_type="image/jpeg")
    too_declared = Attachment("large", AttachmentKind.PHOTO, size_bytes=MAX_IMAGE_BYTES + 1)
    too_downloaded = Attachment("large-data", AttachmentKind.PHOTO)
    broken = Attachment("broken", AttachmentKind.PHOTO)
    platform.files.update(
        {"photo": JPEG, "png": PNG, "pdf": b"%PDF-file", "large-data": b"x" * (MAX_IMAGE_BYTES + 1)}
    )
    original_download = platform.download
    downloaded = []

    async def download(attachment, **kwargs):
        downloaded.append(attachment.id)
        if attachment.id == "broken":
            raise RuntimeError("secret message")
        if attachment.id == "large-data":
            return b"\xff\xd8\xff" + b"x" * MAX_IMAGE_BYTES
        return await original_download(attachment, **kwargs)

    platform.download = download
    with caplog.at_level(logging.WARNING):
        parts = await download_images(
            platform,
            [good_photo, good_document, fake_pdf, too_declared, too_downloaded, broken],
            chat=GROUP,
            limit=10,
        )
    assert [(part.media_type, part.data) for part in parts] == [
        ("image/jpeg", JPEG),
        ("image/png", PNG),
    ]
    assert "large" not in downloaded
    assert "secret message" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "large-data" in downloaded
    assert all(part.data != b"\xff\xd8\xff" + b"x" * MAX_IMAGE_BYTES for part in parts)


async def test_download_images_obeys_limit():
    platform = FakePlatform(files={"1": JPEG, "2": PNG})
    items = [Attachment("1", AttachmentKind.PHOTO), Attachment("2", AttachmentKind.PHOTO)]
    result = await download_images(platform, items, chat=GROUP, limit=1)
    assert len(result) == 1


async def test_download_image_reuses_turn_cache():
    platform = FakePlatform(files={"photo": JPEG})
    attachment = Attachment("photo", AttachmentKind.PHOTO)
    cache = {}
    first = await download_image(platform, attachment, chat=GROUP, cache=cache)
    platform.files.clear()
    second = await download_image(platform, attachment, chat=GROUP, cache=cache)
    assert first == second
    assert cache == {"photo": JPEG}


def test_error_label_only_includes_safe_platform_error_code():
    from chatcore.platform import PlatformError

    assert error_label(PlatformError("TimedOut")) == "PlatformError(TimedOut)"
    assert error_label(PlatformError("token=secret")) == "PlatformError"


async def test_download_image_distinguishes_size_and_format_errors():
    platform = FakePlatform(files={"bad": b"GIF89a"})
    with pytest.raises(ImageUnavailable) as unsupported:
        await download_image(platform, Attachment("bad", AttachmentKind.PHOTO), chat=GROUP)
    assert unsupported.value.reason == "unsupported_format"
    with pytest.raises(ImageUnavailable) as too_large:
        await download_image(
            platform,
            Attachment("big", AttachmentKind.PHOTO, size_bytes=MAX_IMAGE_BYTES + 1),
            chat=GROUP,
        )
    assert too_large.value.reason == "too_large"


@pytest.mark.parametrize("code", ["FileTooLarge", "file_too_large"])
async def test_download_image_maps_adapter_size_refusal_to_too_large(code):
    from chatcore.platform import PlatformError

    class TooLarge(FakePlatform):
        async def download(self, attachment, *, chat, max_bytes):
            raise PlatformError(code)

    with pytest.raises(ImageUnavailable) as caught:
        await download_image(TooLarge(), Attachment("big", AttachmentKind.PHOTO), chat=GROUP)
    assert caught.value.reason == "too_large"
