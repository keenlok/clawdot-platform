import array
import asyncio
import shutil

import pytest
from chatcore_tutor.slow_audio import SlowAudioError, slow_mp3


async def test_slow_processor_rejects_invalid_mp3_without_calling_shell(monkeypatch):
    monkeypatch.setattr("chatcore_tutor.slow_audio.shutil.which", lambda _: "/usr/bin/ffmpeg")
    with pytest.raises(SlowAudioError):
        await slow_mp3(b"not audio")


@pytest.mark.parametrize("value", [None, "audio", 42, [1, 2], b"x" * (2 * 1024 * 1024 + 1)])
async def test_slow_processor_rejects_non_bytes_and_oversize_before_spawn(monkeypatch, value):
    monkeypatch.setattr("chatcore_tutor.slow_audio.shutil.which", lambda _: "/usr/bin/ffmpeg")

    async def fail_spawn(*args, **kwargs):
        pytest.fail("invalid audio reached subprocess creation")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    with pytest.raises(SlowAudioError):
        await slow_mp3(value)


async def test_spawn_and_processing_share_one_ten_second_deadline(monkeypatch):
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"
    now = [100.0]
    timeouts = []

    class Stream:
        def __init__(self, chunks=()):
            self.chunks = iter(chunks)

        async def read(self, size):
            return next(self.chunks, b"")

        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    class Child:
        stdin = Stream()
        stdout = Stream([audio])
        returncode = 0

        async def wait(self):
            return 0

    child = Child()
    monkeypatch.setattr("chatcore_tutor.slow_audio.shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr("chatcore_tutor.slow_audio.time.monotonic", lambda: now[0])

    async def create(*args, **kwargs):
        now[0] += 6
        return child

    async def record_timeout(awaitable, *, timeout):  # noqa: ASYNC109
        timeouts.append(timeout)
        return await awaitable

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(asyncio, "wait_for", record_timeout)
    assert await slow_mp3(audio) == audio
    assert timeouts == [10, 4]


async def test_slow_processor_checks_binary_before_generation(monkeypatch):
    monkeypatch.setattr("chatcore_tutor.slow_audio.shutil.which", lambda _: None)
    with pytest.raises(SlowAudioError, match="unavailable"):
        await slow_mp3(b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64")


async def test_child_stdin_and_stdout_are_handled_together_and_output_is_bounded(monkeypatch):
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"

    class Stream:
        def __init__(self, chunks=(), on_eof=None):
            self.chunks = iter(chunks)
            self.written = bytearray()
            self.on_eof = on_eof

        async def read(self, size):
            chunk = next(self.chunks, b"")
            if not chunk and self.on_eof:
                self.on_eof()
            return chunk

        def write(self, value):
            self.written.extend(value)

        async def drain(self):
            pass

        def close(self):
            pass

    class Child:
        def __init__(self, output, *, race=False):
            self.done = asyncio.Event()
            self.stdin, self.stdout = Stream(), Stream(output, self.done.set)
            self.returncode = None
            self.killed = False
            self.reaped = False
            self.race = race

        async def wait(self):
            await self.done.wait()
            self.returncode = -9 if self.killed else 0
            self.reaped = True
            return self.returncode

        def kill(self):
            self.killed = True
            self.done.set()
            if self.race:
                raise ProcessLookupError

    monkeypatch.setattr("chatcore_tutor.slow_audio.shutil.which", lambda _: "/usr/bin/ffmpeg")
    child = Child([audio, b""])

    async def create(*args, **kwargs):
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert await slow_mp3(audio) == audio
    assert child.stdin.written == audio and child.reaped

    child = Child([b"x" * (2 * 1024 * 1024 + 1)], race=True)
    with pytest.raises(SlowAudioError, match="output_limit"):
        await slow_mp3(audio)
    assert child.killed and child.reaped


async def test_spawn_error_is_opaque_and_never_escapes(monkeypatch):
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"
    monkeypatch.setattr("chatcore_tutor.slow_audio.shutil.which", lambda _: "/fake/ffmpeg")

    async def fail_spawn(*args, **kwargs):
        raise OSError("raw spawn diagnostic")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    with pytest.raises(SlowAudioError) as error:
        await slow_mp3(audio)
    assert "raw spawn diagnostic" not in str(error.value)


async def test_pipe_read_error_kills_and_reaps_child(monkeypatch):
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"
    done = asyncio.Event()

    class Stream:
        async def read(self, size):
            raise OSError("raw pipe read detail")

        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    class Child:
        def __init__(self):
            self.stdin, self.stdout = Stream(), Stream()
            self.returncode = None
            self.killed = self.reaped = False

        async def wait(self):
            await done.wait()
            self.returncode = -9
            self.reaped = True
            return self.returncode

        def kill(self):
            self.killed = True
            done.set()

    child = Child()
    monkeypatch.setattr("chatcore_tutor.slow_audio.shutil.which", lambda _: "/fake/ffmpeg")

    async def create(*args, **kwargs):
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(SlowAudioError) as error:
        await slow_mp3(audio)
    assert str(error.value) == "processor"
    assert child.killed and child.reaped


async def test_timeout_kills_reaps_and_leaves_no_pipe_tasks(monkeypatch):
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"
    done = asyncio.Event()

    class Stream:
        async def read(self, size):
            await asyncio.Future()

        def write(self, data):
            pass

        async def drain(self):
            await asyncio.Future()

        def close(self):
            pass

    class Child:
        def __init__(self):
            self.stdin, self.stdout = Stream(), Stream()
            self.returncode = None
            self.killed = self.reaped = False

        async def wait(self):
            await done.wait()
            self.reaped = True
            self.returncode = -9 if self.killed else 0
            return self.returncode

        def kill(self):
            self.killed = True
            done.set()

    child = Child()
    monkeypatch.setattr("chatcore_tutor.slow_audio.shutil.which", lambda _: "/fake/ffmpeg")

    async def create(*args, **kwargs):
        return child

    real_wait_for = asyncio.wait_for

    async def quick_timeout(awaitable, timeout):  # noqa: ASYNC109
        return await real_wait_for(awaitable, timeout=0.01)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(asyncio, "wait_for", quick_timeout)
    with pytest.raises(SlowAudioError, match="processor_timeout"):
        await slow_mp3(audio)
    assert child.killed and child.reaped
    pending = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and task.get_coro().__name__ in {"feed", "collect"}
    ]
    assert not pending


async def test_cancellation_during_cleanup_survives_repeat_cancel(monkeypatch):
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"
    done = asyncio.Event()
    cleanup_started = asyncio.Event()

    class Stream:
        async def read(self, size):
            await asyncio.Future()

        def write(self, data):
            pass

        async def drain(self):
            await asyncio.Future()

        def close(self):
            pass

    class Child:
        def __init__(self):
            self.stdin, self.stdout = Stream(), Stream()
            self.returncode = None
            self.killed = self.reaped = False

        async def wait(self):
            await done.wait()
            self.reaped = True
            self.returncode = -9 if self.killed else 0
            return self.returncode

        def kill(self):
            self.killed = True
            cleanup_started.set()
            asyncio.get_running_loop().call_later(0.03, done.set)

    child = Child()
    monkeypatch.setattr("chatcore_tutor.slow_audio.shutil.which", lambda _: "/fake/ffmpeg")

    async def create(*args, **kwargs):
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(slow_mp3(audio))
    await asyncio.sleep(0)
    task.cancel()
    await cleanup_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.killed and child.reaped


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
async def test_real_ffmpeg_synthetic_tone_slowdown_changes_duration_not_pitch():
    async def run(*args):
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == 0
        return output

    source = await run(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=0.4*sin(2*PI*(220+40*sin(2*PI*0.5*t))*t):s=44100:d=2",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "128k",
        "-f",
        "mp3",
        "pipe:1",
    )
    slowed = await slow_mp3(source)
    decode_args = ("ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "mp3", "-i")

    # Decode by writing the synthetic clips to stdin without using a filesystem path.
    async def decode(data):
        process = await asyncio.create_subprocess_exec(
            *decode_args,
            "pipe:0",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "44100",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(process.communicate(data), timeout=10)
        assert process.returncode == 0
        return output

    normal_pcm, slow_pcm = await asyncio.gather(decode(source), decode(slowed))
    normal_duration, slow_duration = len(normal_pcm) / 88200, len(slow_pcm) / 88200
    assert 0.9 * (normal_duration / 0.75) <= slow_duration <= 1.1 * (normal_duration / 0.75)

    def estimate(data):
        samples = array.array("h")
        samples.frombytes(data[int(0.2 * 44100) * 2 : int(0.4 * 44100) * 2])
        crossings = sum(
            left <= 0 < right for left, right in zip(samples, samples[1:], strict=False)
        )
        return crossings / 0.2

    assert abs(estimate(normal_pcm) - estimate(slow_pcm)) < 30
