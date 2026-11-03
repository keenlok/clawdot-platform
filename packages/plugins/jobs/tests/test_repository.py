from datetime import UTC, datetime
from uuid import uuid4

import pytest
from chatcore_jobs.repository import _job


def stored_row(payload):
    return {
        "id": uuid4(),
        "chat_key": "tg:-100",
        "is_dm": False,
        "owner_key": "tg:7",
        "thread_id": None,
        "kind": "watch",
        "status": "active",
        "payload": payload,
        "created_at": datetime(2026, 10, 31, tzinfo=UTC),
        "next_run_at": datetime(2026, 10, 31, tzinfo=UTC),
        "occurrence": 0,
        "revision": 0,
        "last_delivery_state": None,
        "snapshot": None,
        "snapshot_hash": None,
        "failure_count": 0,
        "error_episode": False,
        "terminal_at": None,
    }


def test_old_watch_payload_without_mode_remains_plain():
    parsed = _job(stored_row({"url": "https://example.com", "interval_seconds": 300}))
    assert parsed.payload.get("mode", "plain") == "plain"


@pytest.mark.parametrize("mode", ["other", None, 1])
def test_watch_payload_rejects_unknown_mode(mode):
    with pytest.raises(ValueError, match="invalid watch payload"):
        _job(stored_row({"url": "https://example.com", "interval_seconds": 3600, "mode": mode}))


def test_rendered_watch_payload_requires_https():
    with pytest.raises(ValueError, match="invalid watch payload"):
        _job(
            stored_row({"url": "http://example.com", "interval_seconds": 3600, "mode": "rendered"})
        )
