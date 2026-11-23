"""Bounded ElevenLabs Scribe speech-to-text request helpers."""

from __future__ import annotations

import json

import httpx

from chatcore.tts import ELEVENLABS_ORIGIN


class STTError(Exception):
    def __init__(self, status_code: int | None = None):
        self.status_code = status_code
        super().__init__("speech transcription failed")


async def transcribe(
    client: httpx.AsyncClient,
    audio: bytes,
    *,
    key: str,
    model: str = "scribe_v2",
    keyterms: list[str] | None = None,
) -> tuple[str, str | None]:
    try:
        data = {"model_id": model, "tag_audio_events": "false"}
        if keyterms:
            data["keyterms"] = json.dumps(keyterms, ensure_ascii=False)
        response = await client.post(
            f"{ELEVENLABS_ORIGIN}/v1/speech-to-text",
            headers={"xi-api-key": key},
            data=data,
            files={"file": ("voice.ogg", audio, "audio/ogg")},
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise STTError(response.status_code)
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise STTError(response.status_code)
        lang = payload.get("language_code")
        return payload["text"], lang if isinstance(lang, str) else None
    except STTError:
        raise
    except Exception:
        raise STTError() from None
