from dataclasses import fields

from chatcore.events import ChatRef
from chatcore.files import VoicePlatform
from chatcore.testing import FakePlatform
from chatcore.tools.registry import TurnState


def test_voice_is_optional_and_fake_records_bytes_caption_and_topic():
    platform = FakePlatform()
    assert isinstance(platform, VoicePlatform)

    async def send():
        await platform.send_voice(
            ChatRef("fake", "group"),
            "pronunciation.mp3",
            b"ID3audio",
            caption="你好\nnei5 hou2\nNormal",
            thread_id="topic-2",
        )

    import asyncio

    asyncio.run(send())
    sent = platform.sent_voices[0]
    assert (sent.filename, sent.data, sent.caption, sent.thread_id) == (
        "pronunciation.mp3",
        b"ID3audio",
        "你好\nnei5 hou2\nNormal",
        "topic-2",
    )


def test_turn_state_attempt_counters_are_real_turn_local_state():
    assert "tool_attempts" in {item.name for item in fields(TurnState)}
    first, second = TurnState(), TurnState()
    first.tool_attempts["pronounce_tutor_text"] = 1
    assert first.tool_attempts == {"pronounce_tutor_text": 1}
    assert second.tool_attempts == {}
