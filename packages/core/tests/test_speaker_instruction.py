"""The system prompt says once, in fixed text, that "you" means the latest speaker."""

import json
from pathlib import Path

from chatcore.agent import _SPEAKER_INSTRUCTION, _header_value

SNAPSHOT = Path(__file__).parent / "snapshots" / "agent_requests_off.json"


def test_speaker_instruction_is_fixed_text():
    assert _SPEAKER_INSTRUCTION == (
        "The final message starts with who sent it. That person is who you are replying to; "
        '"you" in your reply means them, not people you addressed earlier. In groups, name '
        'other people instead of calling them "you". Use a nickname or form of address only '
        "for the person it belongs to (match the user key)."
    )


def test_every_snapshot_system_prompt_has_the_speaker_sentence_exactly_once():
    requests = [call for calls in json.loads(SNAPSHOT.read_text()).values() for call in calls]
    assert requests
    for call in requests:
        assert call["system"].count(_SPEAKER_INSTRUCTION) == 1


def test_header_value_escapes_supplementary_format_character_as_valid_json():
    import json

    value = "Member " + chr(0xE0001)

    rendered = _header_value(value)

    assert r"\udb40\udc01" in rendered
    assert json.loads('"' + rendered + '"') == value


def test_header_value_escapes_lone_surrogate_as_valid_json():
    value = "Member \ud83c"

    rendered = _header_value(value)

    rendered.encode("utf-8")
    assert r"\ud83c" in rendered
    assert json.loads('"' + rendered + '"') == value
