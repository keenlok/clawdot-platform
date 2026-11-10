"""The system prompt says once, in fixed text, that "you" means the latest speaker."""

import json
from pathlib import Path

from chatcore.agent import _SPEAKER_INSTRUCTION

SNAPSHOT = Path(__file__).parent / "snapshots" / "agent_requests_off.json"


def test_speaker_instruction_is_fixed_text():
    assert _SPEAKER_INSTRUCTION == (
        'The speaker of the latest user message is the person you are replying to; "you" in '
        "your reply means them, not people you addressed earlier. In groups, name other people "
        'instead of calling them "you".'
    )


def test_every_snapshot_system_prompt_has_the_speaker_sentence_exactly_once():
    requests = [call for calls in json.loads(SNAPSHOT.read_text()).values() for call in calls]
    assert requests
    for call in requests:
        assert call["system"].count(_SPEAKER_INSTRUCTION) == 1
