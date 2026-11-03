from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from chatcore_release_control.ledger import CardNonce, ReleaseBinding
from chatcore_release_control.policy import DeploymentIdentity
from chatcore_release_control.release_card_view import CardPresentation, CheckLine, FileStat
from chatcore_release_control.telegram_cards import CardIssueError, ReleaseCardIssuer


class FakeLedger:
    def __init__(self) -> None:
        self.calls = []
        self.fail = False

    async def create_card(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("sensitive database diagnostic")
        return object()


class FakeBotApi:
    def __init__(self, response=None, fail=False) -> None:
        self.calls = []
        self.response = response or {"message_id": 41, "chat": {"id": 7}}
        self.fail = fail

    async def call(self, method: str, parameters: Mapping[str, object], *, token: str) -> object:
        self.calls.append((method, dict(parameters)))
        if self.fail:
            raise RuntimeError("token-bearing fake exception")
        return self.response

    async def download_file(self, file_id: str, max_bytes: int, *, token: str) -> bytes:
        raise AssertionError("card issuer must not download files")


def binding(
    release_kind: str | None = "reconciliation", *, same_tree_continuation: bool = False
) -> ReleaseBinding:
    base_sha = "a" * 40
    base_tree = "9" * 40 if same_tree_continuation else "8" * 40
    review = {
        "complete_delta_reviewed": True,
        "deployed_commit": "d" * 40,
        "base_commit": base_sha,
        "head_commit": "b" * 40,
        "deployed_tree": "9" * 40,
        "base_tree": base_tree,
        "head_tree": "c" * 40,
        "policy_digest": "1" * 64,
        "base_is_ancestor_of_head": True,
        "base_is_descendant_of_deployed": same_tree_continuation,
        "same_tree_continuation_recorded": same_tree_continuation,
        "check_evidence_digest": "2" * 64,
        "deployed_to_base": (
            [] if same_tree_continuation else [{"path": "inherited/file.py", "status": "modified"}]
        ),
        "base_to_head": [{"path": "candidate/file.py", "status": "added"}],
        "deployed_to_head": (
            [{"path": "candidate/file.py"}]
            if same_tree_continuation
            else [{"path": "inherited/file.py"}, {"path": "candidate/file.py"}]
        ),
    }
    if release_kind is not None:
        review["release_kind"] = release_kind
    return ReleaseBinding(
        repository_id=101,
        owner_id=202,
        pr_number=37,
        target="production",
        base_sha=base_sha,
        head_sha="b" * 40,
        head_tree="c" * 40,
        deployed_identity=DeploymentIdentity(
            git_sha="d" * 40,
            image_digest="sha256:" + "e" * 64,
            config_generation=4,
            config_digest="f" * 64,
            policy_digest="1" * 64,
            target_sequence=8,
        ),
        policy_digest="1" * 64,
        review_bundle=review,
    )


def presentation(target_binding, **overrides):
    data = {
        "pr_url": f"https://github.com/acme/repo/pull/{target_binding.pr_number}",
        "pr_title": "Add export",
        "what_changes": ("Adds a CSV export", "Members receive a private file"),
        "risks": ("Large ledgers are truncated",),
        "file_stats": tuple(
            FileStat(item["path"], 6, 1)
            for item in target_binding.review_bundle["deployed_to_head"]
        ),
        "checks": (CheckLine("tests", "success", "1131 passed"),),
        "review_findings": 2,
        "review_fixed": 2,
    }
    data.update(overrides)
    return CardPresentation(**data)


def with_delta(candidate, changes):
    return ReleaseBinding(
        repository_id=candidate.repository_id,
        owner_id=candidate.owner_id,
        pr_number=candidate.pr_number,
        target=candidate.target,
        base_sha=candidate.base_sha,
        head_sha=candidate.head_sha,
        head_tree=candidate.head_tree,
        deployed_identity=candidate.deployed_identity,
        policy_digest=candidate.policy_digest,
        review_bundle={**candidate.review_bundle, "deployed_to_head": changes},
    )


@pytest.mark.asyncio
async def test_card_binds_full_d_b_h_and_separate_delta_with_generated_nonce() -> None:
    ledger, bot = FakeLedger(), FakeBotApi()
    protected = []
    issuer = ReleaseCardIssuer(
        ledger=ledger,
        transport=bot,
        token="broker-token",
        current_telegram_owner=lambda: 7,
        protect_message=lambda chat, message: protected.append((chat, message)),
    )
    receipt = await issuer.issue(
        binding=binding(),
        owner_user_id=7,
        summary="untrusted *text*\nwith a newline",
        now=datetime(2026, 10, 31, tzinfo=UTC),
    )
    method, params = bot.calls[0]
    assert method == "sendMessage"
    assert params["chat_id"] == 7
    assert params["link_preview_options"] == {"is_disabled": True}
    assert "parse_mode" not in params and "entities" not in params
    assert "D → B inherited changes:" not in params["text"]
    assert "Trusted check evidence digest: " + "2" * 64 not in params["text"]
    assert "D tree: " + "9" * 40 not in params["text"]
    assert "B tree: " + "8" * 40 not in params["text"]
    assert "H tree: " + "c" * 40 not in params["text"]
    assert "Release request: PR #37" in params["text"]
    assert "Rollback: previous image kept" in params["text"]
    assert "\nwith a newline" not in params["text"]
    keyboard = params["reply_markup"]["inline_keyboard"]
    approve, reject = keyboard[0]
    assert approve["callback_data"].startswith("release:a:")
    assert reject["callback_data"].startswith("release:r:")
    assert len(approve["callback_data"].encode()) <= 64
    assert keyboard[1] == [
        {"text": "Details", "callback_data": "release:i"},
        {"text": "View diff", "callback_data": "release:v"},
    ]
    assert isinstance(ledger.calls[0]["nonce"], CardNonce)
    assert ledger.calls[0]["binding"].to_json() == binding().to_json()
    assert ledger.calls[0]["message_id"] == 41
    assert ledger.calls[0]["owner_user_id"] == ledger.calls[0]["chat_id"] == 7
    assert ledger.calls[0]["binding"].owner_id == 202
    assert protected == [("7", "41")]
    assert receipt is not None


@pytest.mark.parametrize("kind", ["ordinary", "reconciliation", "legacy"])
def test_card_render_snapshots(kind):
    from chatcore_release_control.telegram_cards import _text

    candidate = (
        binding("ordinary", same_tree_continuation=True)
        if kind == "ordinary"
        else binding("reconciliation")
    )
    rendered = _text(
        candidate,
        "Legacy summary" if kind == "legacy" else "",
        None if kind == "legacy" else presentation(candidate),
    )
    if kind == "legacy":
        expected = """Release request: PR #37
⚠️ Reconciliation: includes previously undeployed changes (1 inherited D→B files + 1 PR B→H files)

What changes
- Legacy summary

Touches
- candidate/file.py (1 files)
- inherited/file.py (1 files)
No migrations · no config · no deploy/CI files

Checks: trusted evidence bound (see Details)
Rollback: previous image kept (sha256:eeeeeeeeeeee); no DB change to undo
Approving merges and deploys bbbbbbb → production"""
        assert rendered == expected
    elif kind == "ordinary":
        expected = """Release request: PR #37 "Add export"
https://github.com/acme/repo/pull/37

What changes (from the PR description)
- Adds a CSV export
- Members receive a private file

Touches
- candidate/file.py (+6 / −1)
No migrations · no config · no deploy/CI files

Checks
✅ tests 1131 passed
Review: 2 findings, both fixed

Risk / limits (from the PR description)
- Large ledgers are truncated
Rollback: previous image kept (sha256:eeeeeeeeeeee); no DB change to undo
Approving merges and deploys bbbbbbb → production"""
        assert rendered == expected
    else:
        expected = """Release request: PR #37 "Add export"
https://github.com/acme/repo/pull/37
⚠️ Reconciliation: includes previously undeployed changes (1 inherited D→B files + 1 PR B→H files)

What changes (from the PR description)
- Adds a CSV export
- Members receive a private file

Touches
- candidate/file.py (+6 / −1)
- inherited/file.py (+6 / −1)
No migrations · no config · no deploy/CI files

Checks
✅ tests 1131 passed
Review: 2 findings, both fixed

Risk / limits (from the PR description)
- Large ledgers are truncated
Rollback: previous image kept (sha256:eeeeeeeeeeee); no DB change to undo
Approving merges and deploys bbbbbbb → production"""
        assert rendered == expected


def test_card_many_files_caps_untrusted_text_and_replaces_urls():
    from chatcore_release_control.telegram_cards import _text

    candidate = binding("ordinary", same_tree_continuation=True)
    paths = [{"path": f"packages/p{i}/file.py"} for i in range(40)]
    candidate = with_delta(candidate, paths)
    stats = tuple(FileStat(item["path"], 1, 1) for item in paths)
    view = presentation(
        candidate,
        pr_title="X" * 500,
        what_changes=("Read https://example.com now",),
        risks=(),
        file_stats=stats,
    )
    result = _text(candidate, "", view)
    assert len(result.encode("utf-16-le")) // 2 <= 4096
    assert "and " in result and "more files (full list in View diff / Details)" in result
    assert "[link removed]" in result and "https://example.com" not in result


def test_contradicted_migration_claim_is_replaced_and_rollback_warns():
    from chatcore_release_control.telegram_cards import _text

    candidate = binding("ordinary", same_tree_continuation=True)
    paths = [{"path": "packages/core/migrations/0010_x.sql"}]
    candidate = with_delta(candidate, paths)
    view = presentation(
        candidate,
        what_changes=("No migrations",),
        risks=("No database changes",),
        file_stats=(FileStat(paths[0]["path"], 1, 0),),
    )
    result = _text(candidate, "", view)
    assert 'PR text says "no migrations"' in result
    assert "- No migrations" not in result
    assert "⚠️ Migrations (1 files)" in result
    assert "image rollback won't undo it" in result


@pytest.mark.asyncio
async def test_uncertain_send_never_creates_card_or_protection_and_has_fixed_error() -> None:
    ledger, bot = FakeLedger(), FakeBotApi(fail=True)
    protected = []
    issuer = ReleaseCardIssuer(
        ledger=ledger,
        transport=bot,
        token="broker-token",
        current_telegram_owner=lambda: 7,
        protect_message=lambda chat, message: protected.append((chat, message)),
    )
    with pytest.raises(CardIssueError) as error:
        await issuer.issue(binding=binding(), owner_user_id=7, summary="safe")
    assert error.value.code == "send_uncertain"
    assert "token-bearing" not in str(error.value)
    assert ledger.calls == []
    assert protected == []


@pytest.mark.asyncio
async def test_ledger_failure_leaves_confirmed_message_protected_but_not_returned() -> None:
    ledger, bot = FakeLedger(), FakeBotApi()
    ledger.fail = True
    protected = []
    issuer = ReleaseCardIssuer(
        ledger=ledger,
        transport=bot,
        token="broker-token",
        current_telegram_owner=lambda: 7,
        protect_message=lambda chat, message: protected.append((chat, message)),
    )
    with pytest.raises(CardIssueError) as error:
        await issuer.issue(binding=binding(), owner_user_id=7, summary="safe")
    assert error.value.code == "ledger_unavailable"
    assert protected == [("7", "41")]


@pytest.mark.asyncio
async def test_card_rejects_mismatched_review_bundle_before_sending() -> None:
    ledger, bot = FakeLedger(), FakeBotApi()
    issuer = ReleaseCardIssuer(
        ledger=ledger,
        transport=bot,
        token="broker-token",
        current_telegram_owner=lambda: 7,
        protect_message=lambda chat, message: None,
    )
    candidate = binding()
    bad = ReleaseBinding(
        repository_id=candidate.repository_id,
        owner_id=candidate.owner_id,
        pr_number=candidate.pr_number,
        target=candidate.target,
        base_sha=candidate.base_sha,
        head_sha=candidate.head_sha,
        head_tree=candidate.head_tree,
        deployed_identity=candidate.deployed_identity,
        policy_digest=candidate.policy_digest,
        review_bundle={**candidate.review_bundle, "base_commit": "f" * 40},
    )
    with pytest.raises(CardIssueError) as error:
        await issuer.issue(binding=bad, owner_user_id=202, summary="safe")
    assert error.value.code == "invalid_card"
    assert not bot.calls and not ledger.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("digest", [None, "not-a-digest", "A" * 64, "2" * 63])
async def test_card_rejects_missing_or_malformed_trusted_check_digest_before_sending(
    digest,
) -> None:
    ledger, bot = FakeLedger(), FakeBotApi()
    issuer = ReleaseCardIssuer(
        ledger=ledger,
        transport=bot,
        token="broker-token",
        current_telegram_owner=lambda: 7,
        protect_message=lambda chat, message: None,
    )
    candidate = binding()
    bundle = candidate.review_bundle
    if digest is None:
        bundle.pop("check_evidence_digest")
    else:
        bundle["check_evidence_digest"] = digest
    invalid = ReleaseBinding(
        repository_id=candidate.repository_id,
        owner_id=candidate.owner_id,
        pr_number=candidate.pr_number,
        target=candidate.target,
        base_sha=candidate.base_sha,
        head_sha=candidate.head_sha,
        head_tree=candidate.head_tree,
        deployed_identity=candidate.deployed_identity,
        policy_digest=candidate.policy_digest,
        review_bundle=bundle,
    )
    with pytest.raises(CardIssueError) as error:
        await issuer.issue(binding=invalid, owner_user_id=7, summary="safe")
    assert error.value.code == "invalid_card"
    assert bot.calls == [] and ledger.calls == []


@pytest.mark.asyncio
async def test_wrong_telegram_actor_is_denied_without_conflating_github_owner() -> None:
    ledger, bot = FakeLedger(), FakeBotApi()
    issuer = ReleaseCardIssuer(
        ledger=ledger,
        transport=bot,
        token="broker-token",
        current_telegram_owner=lambda: 7,
        protect_message=lambda chat, message: None,
    )
    # GitHub repository owner is 202; the separately configured Telegram owner is 7.
    candidate = binding()
    assert candidate.owner_id == 202
    with pytest.raises(CardIssueError) as error:
        await issuer.issue(binding=candidate, owner_user_id=202, summary="safe")
    assert error.value.code == "invalid_card"
    assert not bot.calls and not ledger.calls


@pytest.mark.asyncio
async def test_card_kind_is_explicit_and_ordinary_unknown_base_is_not_sent() -> None:
    bot = FakeBotApi()
    issuer = ReleaseCardIssuer(
        ledger=FakeLedger(),
        transport=bot,
        token="broker-token",
        current_telegram_owner=lambda: 7,
        protect_message=lambda _chat, _message: None,
    )
    await issuer.issue(
        binding=binding("reconciliation"),
        owner_user_id=7,
        summary="inherited main content",
    )
    text = bot.calls[0][1]["text"]
    assert text.startswith("Release request: PR #37")
    assert "⚠️ Reconciliation: includes previously undeployed changes" in text
    assert "previously undeployed changes (1 inherited D→B files + 1 PR B→H files)" in text
    assert "D → B inherited changes:" not in text and "B → H PR changes:" not in text
    assert "Checks: trusted evidence bound (see Details)" in text

    continuation_bot = FakeBotApi()
    continuation_issuer = ReleaseCardIssuer(
        ledger=FakeLedger(),
        transport=continuation_bot,
        token="broker-token",
        current_telegram_owner=lambda: 7,
        protect_message=lambda _chat, _message: None,
    )
    await continuation_issuer.issue(
        binding=binding("ordinary", same_tree_continuation=True),
        owner_user_id=7,
        summary="recorded same-tree continuation",
    )
    continuation_text = continuation_bot.calls[0][1]["text"]
    assert continuation_text.startswith("Release request: PR #37")
    assert "Reconciliation:" not in continuation_text

    legacy_bot = FakeBotApi()
    legacy_issuer = ReleaseCardIssuer(
        ledger=FakeLedger(),
        transport=legacy_bot,
        token="broker-token",
        current_telegram_owner=lambda: 7,
        protect_message=lambda _chat, _message: None,
    )
    await legacy_issuer.issue(
        binding=binding(None, same_tree_continuation=True),
        owner_user_id=7,
        summary="legacy ordinary card kind",
    )
    assert legacy_bot.calls[0][1]["text"].startswith("Release request: PR #37")

    invalid_bot = FakeBotApi()
    invalid_issuer = ReleaseCardIssuer(
        ledger=FakeLedger(),
        transport=invalid_bot,
        token="broker-token",
        current_telegram_owner=lambda: 7,
        protect_message=lambda _chat, _message: None,
    )
    with pytest.raises(CardIssueError, match="invalid_card"):
        await invalid_issuer.issue(
            binding=binding("maintenance_bypass"),
            owner_user_id=7,
            summary="must not send",
        )
    assert invalid_bot.calls == []

    ordinary_bot = FakeBotApi()
    ordinary_issuer = ReleaseCardIssuer(
        ledger=FakeLedger(),
        transport=ordinary_bot,
        token="broker-token",
        current_telegram_owner=lambda: 7,
        protect_message=lambda _chat, _message: None,
    )
    with pytest.raises(CardIssueError, match="invalid_card"):
        await ordinary_issuer.issue(
            binding=binding(None),
            owner_user_id=7,
            summary="B is not D and has no recorded continuation",
        )
    assert ordinary_bot.calls == []


def test_paths_are_single_line_cleaned_in_the_card():
    from chatcore_release_control.telegram_cards import _text

    candidate = binding("ordinary", same_tree_continuation=True)
    evil = "packages/x/a Approved by owner‮.py"
    candidate = with_delta(candidate, [{"path": evil}])
    result = _text(candidate, "", presentation(candidate, file_stats=(FileStat(evil, 1, 0),)))
    assert " " not in result and "‮" not in result
    assert "\nApproved by owner" not in result


def test_touches_keep_changed_line_order():
    from chatcore_release_control.telegram_cards import _text

    candidate = binding("ordinary", same_tree_continuation=True)
    paths = [{"path": "packages/aaa/x.py"}, {"path": "packages/zzz/y.py"}]
    candidate = with_delta(candidate, paths)
    stats = (FileStat("packages/aaa/x.py", 1, 0), FileStat("packages/zzz/y.py", 1000, 0))
    result = _text(candidate, "", presentation(candidate, file_stats=stats))
    assert result.index("packages/zzz/y.py") < result.index("packages/aaa/x.py")


def test_partial_bullet_truncation_is_marked(monkeypatch):
    from chatcore_release_control import telegram_cards
    from chatcore_release_control.telegram_cards import _text

    candidate = binding("ordinary", same_tree_continuation=True)
    paths = [{"path": "packages/p/x.py"}]
    candidate = with_delta(candidate, paths)
    view = presentation(
        candidate,
        what_changes=tuple("😀" * 160 for _ in range(5)),
        risks=tuple("😀" * 160 for _ in range(4)),
        file_stats=tuple(FileStat(item["path"], 1, 1) for item in paths),
    )
    monkeypatch.setattr(telegram_cards, "MAX_CARD_TEXT_UTF16", 100_000)
    full = len(_text(candidate, "", view).encode("utf-16-le")) // 2
    # Room for everything except roughly one risk bullet (each is ~322 UTF-16 units).
    monkeypatch.setattr(telegram_cards, "MAX_CARD_TEXT_UTF16", full - 200)
    result = _text(candidate, "", view)
    assert len(result.encode("utf-16-le")) // 2 <= full - 200
    risk_section = result.split("Risk / limits (from the PR description)\n", 1)[1]
    shown = risk_section.split("\nRollback:")[0].splitlines()
    assert shown[-1] == "- …" and 1 <= len(shown) - 1 < 4
