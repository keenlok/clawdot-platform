"""Small bounded FFmpeg boundary for locally slowing generated MP3 audio."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time

from chatcore_tutor.audio import MAX_AUDIO_BYTES, valid_mp3

log = logging.getLogger("chatcore_tutor")


class SlowAudioError(Exception):
    """Safe processor error with no subprocess output attached."""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def _cleanup(process, tasks: list[asyncio.Task]) -> OSError | None:
    kill_error = None
    for task in tasks:
        if not task.done():
            task.cancel()
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            kill_error = exc
    await asyncio.gather(*tasks, return_exceptions=True)
    await process.wait()
    return kill_error


async def _await_cleanup(process, tasks: list[asyncio.Task]) -> tuple[bool, OSError | None]:
    """Finish child cleanup despite repeat cancellation without replacing the task error."""
    cleanup = asyncio.create_task(_cleanup(process, tasks))
    current = asyncio.current_task()
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
            if current is not None:
                current.uncancel()
    return cancelled, cleanup.result()


async def slow_mp3(data: bytes) -> bytes:
    deadline = time.monotonic() + 10
    if not isinstance(data, bytes) or len(data) > MAX_AUDIO_BYTES:
        raise SlowAudioError("invalid_audio")
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise SlowAudioError("unavailable")
    if not valid_mp3(data):
        raise SlowAudioError("invalid_audio")
    try:
        process = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-protocol_whitelist",
                "pipe",
                "-f",
                "mp3",
                "-i",
                "pipe:0",
                "-map",
                "0:a:0",
                "-vn",
                "-af",
                "atempo=0.75",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "128k",
                "-f",
                "mp3",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            ),
            timeout=max(0, deadline - time.monotonic()),
        )
    except asyncio.CancelledError:
        raise
    except (OSError, TimeoutError):
        raise SlowAudioError("processor") from None

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        repeated_cancel, kill_error = await _await_cleanup(process, [])
        if repeated_cancel:
            raise asyncio.CancelledError from None
        if kill_error is not None:
            log.warning("tutor ffmpeg cleanup failed error=%s", type(kill_error).__name__)
        raise SlowAudioError("processor_timeout")

    output = bytearray()

    async def feed() -> None:
        assert process.stdin is not None
        process.stdin.write(data)
        await process.stdin.drain()
        process.stdin.close()

    async def collect() -> None:
        assert process.stdout is not None
        while chunk := await process.stdout.read(65536):
            if len(output) + len(chunk) > MAX_AUDIO_BYTES:
                raise SlowAudioError("output_limit")
            output.extend(chunk)

    tasks = [
        asyncio.create_task(feed()),
        asyncio.create_task(collect()),
        asyncio.create_task(process.wait()),
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=remaining)
    except asyncio.CancelledError:
        _cancelled, kill_error = await _await_cleanup(process, tasks)
        if kill_error is not None:
            log.warning("tutor ffmpeg cleanup failed error=%s", type(kill_error).__name__)
        raise
    except (TimeoutError, SlowAudioError) as exc:
        repeated_cancel, kill_error = await _await_cleanup(process, tasks)
        if repeated_cancel:
            raise asyncio.CancelledError from None
        if kill_error is not None:
            log.warning("tutor ffmpeg cleanup failed error=%s", type(kill_error).__name__)
        if isinstance(exc, TimeoutError):
            raise SlowAudioError("processor_timeout") from None
        raise
    except Exception:
        repeated_cancel, kill_error = await _await_cleanup(process, tasks)
        if repeated_cancel:
            raise asyncio.CancelledError from None
        if kill_error is not None:
            log.warning("tutor ffmpeg cleanup failed error=%s", type(kill_error).__name__)
        raise SlowAudioError("processor") from None
    if process.returncode != 0 or not valid_mp3(output):
        repeated_cancel, kill_error = await _await_cleanup(process, tasks)
        if repeated_cancel:
            raise asyncio.CancelledError from None
        if kill_error is not None:
            log.warning("tutor ffmpeg cleanup failed error=%s", type(kill_error).__name__)
        raise SlowAudioError("processor")
    return bytes(output)
