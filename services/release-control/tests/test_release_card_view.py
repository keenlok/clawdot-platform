import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from chatcore_release_control.release_card_view import (
    CardIssueError,
    CardPresentation,
    CheckLine,
    FileStat,
    computed_flags,
    find_contradictions,
    group_key,
    parse_pr_summary,
    redact_patch,
    render_details,
    render_flags,
    touches,
)

sys.path.insert(0, str(Path(__file__).parent))
pytest_plugins = ("test_ledger",)


def test_parse_pr_summary_and_plain_fallback():
    assert parse_pr_summary("## Summary\n- Adds export\n## Risks\n- Large files") == (
        ["Adds export"],
        ["Large files"],
    )
    assert parse_pr_summary("One line\nTwo line")[0] == ["One line", "Two line"]


def test_grouping_flags_and_contradictions_are_computed_from_paths():
    paths = ["packages/core/migrations/001.sql", "apps/bot/Dockerfile"]
    assert group_key(paths[0]) == "packages/core"
    flags = computed_flags(paths)
    assert flags["migrations"] == flags["deploy_ci"] == 1
    assert find_contradictions(["No migrations", "No deployment files"], flags)
    assert "⚠️ Migrations" in render_flags(flags)


def test_touches_aggregates_git_stats():
    groups = touches([FileStat("packages/core/a.py", 2, 1), FileStat("packages/core/b.py", 3, 0)])
    assert groups[0]["key"] == "packages/core"
    assert (groups[0]["added"], groups[0]["deleted"], groups[0]["files"]) == (5, 1, 2)


def test_presentation_validates_path_set_and_checks_and_removes_patch_from_storage():
    binding = SimpleNamespace(pr_number=42, review_bundle={"deployed_to_head": [{"path": "a.py"}]})
    value = {
        "pr_url": "https://github.com/acme/repo/pull/42",
        "file_stats": [{"path": "a.py", "added": 0, "deleted": 0}],
        "checks": [{"name": "tests", "conclusion": "success", "detail": "2 passed"}],
        "patch_state": "included",
        "patch": "diff",
        "redacted_files": 0,
    }
    parsed = CardPresentation.from_json(value, binding)
    assert parsed.checks == (CheckLine("tests", "success", "2 passed"),)
    assert "patch" not in parsed.to_json()
    with pytest.raises(CardIssueError):
        CardPresentation.from_json({**value, "extra": True}, binding)
    with pytest.raises(CardIssueError):
        CardPresentation.from_json({**value, "file_stats": []}, binding)


def test_patch_redaction_fails_closed_and_only_displays_allowed_paths():
    from chatcore_release_control.publication import ScanVerdict

    class Scanner:
        def __call__(self, item):
            if b"secret" in item.content:
                return ScanVerdict.FINDING
            return ScanVerdict.CLEAN

    raw = b"diff --git a/ok.txt b/ok.txt\n+clean\ndiff --git a/private.txt b/private.txt\n+secret\n"
    result, count = redact_patch(raw, Scanner(), frozenset({"ok.txt"}))
    assert count == 1 and "+clean" in result and "b/[redacted section]" in result


def test_render_details_splits_large_path_sets_and_appends_tail():
    paths = [{"path": f"src/{index:04}.py", "change_type": "modified"} for index in range(300)]
    binding = {
        "pr_number": 42,
        "target": "prod",
        "base_sha": "b" * 40,
        "head_sha": "h" * 40,
        "head_tree": "t" * 40,
        "policy_digest": "p" * 64,
        "deployed_identity": {
            "git_sha": "d" * 40,
            "image_digest": "sha256:" + "e" * 64,
            "config_generation": 2,
            "config_digest": "c" * 64,
            "target_sequence": 4,
        },
        "review_bundle": {
            "deployed_tree": "d" * 40,
            "base_tree": "b" * 40,
            "head_tree": "h" * 40,
            "check_evidence_digest": "x" * 64,
            "deployed_to_base": paths,
            "base_to_head": paths,
            "deployed_to_head": paths,
        },
    }
    messages = render_details(binding)
    assert len(messages) <= 5
    assert all(len(text.encode("utf-16-le")) // 2 <= 4096 for text in messages)
    assert "more paths (full list in View diff)" in messages[-1]


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_PG_DSN"), reason="requires TEST_PG_DSN")
async def test_release_card_view_store_round_trip_and_immutability(ledger_pool):
    from pathlib import Path

    from chatcore_release_control.ledger import ReleaseLedger
    from chatcore_release_control.release_card_view import ReleaseCardViewStore
    from test_ledger import create_card

    async with ledger_pool.acquire() as connection:
        bridge_schema = Path(__file__).parents[1] / "src/chatcore_release_control/bridge_schema.sql"
        await connection.execute(bridge_schema.read_text())
    ledger = ReleaseLedger(ledger_pool)
    card = await create_card(ledger, owner_user_id=404, chat_id=404, message_id=7777)
    store = ReleaseCardViewStore(ledger_pool)
    await store.put(
        card.approval_id, {"pr_url": "https://github.com/acme/repo/pull/37"}, b"patch", "included"
    )
    row = await store.get(card.approval_id)
    assert row["patch"] == b"patch" and row["patch_state"] == "included"
    with pytest.raises(Exception, match="immutable release card view"):
        await ledger_pool.execute(
            "UPDATE release_control.release_card_view "
            "SET patch_state='unavailable' WHERE approval_id=$1",
            card.approval_id,
        )
    with pytest.raises(Exception, match="immutable release card view"):
        await ledger_pool.execute(
            "DELETE FROM release_control.release_card_view WHERE approval_id=$1", card.approval_id
        )


def test_redaction_splits_only_at_line_start_diff_headers():
    from chatcore_release_control.publication import ScanVerdict

    patch = (
        b"diff --git a/a.py b/a.py\n+x = 'diff --git a/x b/x'\n+secret\n"
        b"diff --git a/b.py b/b.py\n+ok\n"
    )
    seen = []

    def scanner(item):
        seen.append(item.content)
        return ScanVerdict.FINDING if b"secret" in item.content else ScanVerdict.CLEAN

    text, count = redact_patch(patch, scanner, frozenset({"a.py", "b.py"}))
    assert len(seen) == 2 and b"diff --git a/x b/x" in seen[0] and b"secret" in seen[0]
    assert count == 1 and "+secret" not in text and "+ok" in text


@pytest.mark.parametrize("patch", [123, [], {}, True, "diff"])
def test_non_included_patch_state_requires_null_patch(patch):
    binding = SimpleNamespace(pr_number=42, review_bundle={"deployed_to_head": [{"path": "a.py"}]})
    value = {
        "pr_url": "https://github.com/acme/repo/pull/42",
        "file_stats": [{"path": "a.py", "added": 1, "deleted": 0}],
        "patch_state": "unavailable",
        "patch": patch,
    }
    with pytest.raises(CardIssueError):
        CardPresentation.from_json(value, binding)


def test_release_control_flag_covers_current_and_pre_move_paths():
    flags = computed_flags(
        ["services/release-control/src/x.py", "packages/plugin-release-control/src/x.py"]
    )
    assert flags["release_control"] == 2
    assert computed_flags(["services/browser-renderer/x.py"])["release_control"] == 0


def test_release_control_claim_checked_against_its_own_flag():
    flags = computed_flags(["services/release-control/x.py"])
    assert flags["release_control"] == 1 and flags["deploy_ci"] == 0
    [(index, warning)] = find_contradictions(["No release-control changes"], flags)
    assert index == 0 and "release-control files changed" in warning


def test_render_details_fills_fifth_message_before_truncating():
    def item(i):
        return {"path": f"p/{i}.py"}

    review = {
        "deployed_tree": "9" * 40,
        "base_tree": "8" * 40,
        "check_evidence_digest": "2" * 64,
        "deployed_to_base": [item(i) for i in range(250)],
        "base_to_head": [item(i) for i in range(250)],
        "deployed_to_head": [item(i) for i in range(250)],
    }
    binding = {
        "pr_number": 42,
        "target": "production",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "head_tree": "c" * 40,
        "policy_digest": "1" * 64,
        "deployed_identity": {
            "git_sha": "d" * 40,
            "image_digest": "sha256:" + "e" * 64,
            "config_generation": 4,
            "config_digest": "f" * 64,
            "target_sequence": 8,
        },
        "review_bundle": review,
    }
    messages = render_details(binding)
    assert all(len(m.encode("utf-16-le")) // 2 <= 4096 for m in messages)
    assert len(messages) <= 5 and "more paths" not in messages[-1]
    review.update(deployed_to_head=[item(i) for i in range(2000)])
    messages = render_details(binding)
    assert len(messages) == 5
    assert all(len(m.encode("utf-16-le")) // 2 <= 4096 for m in messages)
    assert len(messages[-1].encode("utf-16-le")) // 2 > 3500
    assert messages[-1].endswith("more paths (full list in View diff)")
