"""Shared audio bounds and nonlinguistic input checks for tutor speech."""

import unicodedata

MAX_AUDIO_BYTES = 2 * 1024 * 1024


def valid_tutor_text(value: object, *, max_chars: int = 120) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= max_chars
        and bool(value.strip())
        and all(
            unicodedata.category(char) != "Cs"
            and (unicodedata.category(char) != "Cc" or char in "\t\n\r")
            for char in value
        )
    )


def valid_mp3(data: bytes) -> bool:
    offset = 0
    if data.startswith(b"ID3"):
        if len(data) < 10:
            return False
        size = data[6:10]
        if any(part & 0x80 for part in size):
            return False
        tag_size = (size[0] << 21) | (size[1] << 14) | (size[2] << 7) | size[3]
        offset = 10 + tag_size + (10 if data[5] & 0x10 else 0)
    return (
        len(data) >= offset + 4
        and data[offset] == 0xFF
        and (data[offset + 1] & 0xE0) == 0xE0
        and (data[offset + 1] & 0x06) != 0
    )
