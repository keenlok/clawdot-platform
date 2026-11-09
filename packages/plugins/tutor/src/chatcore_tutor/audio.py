"""Shared audio bounds and nonlinguistic input checks for tutor speech."""

from chatcore.tts import valid_mp3, valid_speech_text

MAX_AUDIO_BYTES = 2 * 1024 * 1024


__all__ = ["MAX_AUDIO_BYTES", "valid_mp3", "valid_tutor_text"]


def valid_tutor_text(value: object, *, max_chars: int = 120) -> bool:
    return valid_speech_text(value, max_chars=max_chars)
