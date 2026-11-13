# Replying to generated photos for further edits

Status: Implemented for v0.14.0; owner-approved merge and downstream rollout pending.

Planning: Codex. Claude CLI planning was attempted but its OAuth session expired and could not be refreshed. This uses the available-model fallback in AGENTS.md. Execution and self-review: Codex in the main chat, following the owner-invoked implement-project workflow without delegation. Owner explicitly authorized merge and deployment.

## Problem and scope

A generated photo is uploaded, but its returned message reference is discarded. Its platform attachment reference and originating requester are not recorded. Core only supplies the current message's photos and the immediately resolved human reply's photos to editing tools. Consequently a reply to the generated photo has no edit reference. Reading an older photo with `read_attachment` lets the model see it but does not register it as an authorized edit input.

Fix explicit replies to generated photos, including successive edits and replies after a process restart. Edit the actual replied-to generated photo, rather than silently substituting the original upload. Preserve current own-upload checks in groups, quotas, moderation instructions and chat/topic boundaries. Exclude delivery retries and changes to group ownership policy. Existing photos sent before this change are not guaranteed recoverable; ask the user to upload those again when no trusted reference exists.

## Alternatives and recommendation

1. **Recommended: persist outgoing attachment receipts with requester attribution.** Reuses the message repository and its retention model. Requires an additive core API and migration, but supports durable exact-photo selection without retaining image bytes.
2. Follow conversation mappings back to the original upload. Smaller, but edits the wrong version when the user replied to a generated result.
3. Register every `read_attachment` result for editing. Does not recover unrecorded outputs and widens editing authority to arbitrary history. Do not use this approach.

## Design

Add an optional `PhotoReceiptPlatform` capability in `packages/core/src/chatcore/files.py`. Its `send_photo_with_receipt` method takes the existing send-photo arguments and returns a typed receipt containing the `MessageRef` and downloadable `Attachment`. Keep the existing `PhotoPlatform.send_photo` return type and callers unchanged. The Telegram adapter constructs the receipt from the actual successful `send_photo` response, selecting its downloadable photo with the same selection helper used for incoming photos. Never issue a second send to obtain a receipt.

Extend `CoreRepo.log_bot_message` with optional attachments and an attachment-requester key. Add a nullable `attachment_requester_key` column to messages in a new additive migration, and the corresponding optional `StoredMessage` field. Only trusted outgoing receipt logging sets this attribution; incoming bot flags or captions cannot establish it. Update both memory and PostgreSQL repositories, serialization and cleanup. `/forgetme` must remove requester-attributed output references, including privacy-erasure paths; normal age/chat purges must continue to remove them. Persist platform file identifiers and metadata only, never image bytes or prompts.

When image editing is enabled and the platform supports receipts, `ImagePlugin` uses the receipt capability and records the delivered photo as a bot message attributed to the requesting user. If persistence fails after successful sending, report that the image was sent but follow-up editing may require uploading it again; do not regenerate or resend. Where receipt support is absent, retain existing delivery behavior without advertising durable output editing. Document this limitation for bridge/document delivery.

In `agent.py`, resolve an exact replied-to stored photo before applying conversation-origin remapping. A bot photo is eligible only when it has trusted requester attribution. Use that attribution for `attachment_owners`, and keep ownership enforcement in the image handler. Apply the same source selection to native vision and editing references so the model sees the photo the editor receives. Keep current-message photos first, followed by the explicit reply's photos. Do not add photos from ambient history or arbitrary reply-chain hops. Preserve existing conversation remapping for text-only bot replies. Validate chat and topic before supplying any attachment; no implicit fallback to another photo when the exact output is unavailable.

## Implementation sequence

1. Add failing regression cases to `packages/plugins/image/tests/test_edit_turn.py`: generate a photo, reply to its returned message and edit; reply to that second output and edit again; assert the image client's reference bytes match each generated version rather than the original upload. Test another member's group request is rejected without calling the image API, and current-message/reply numbering remains stable.
2. Add receipt adapter and fake-platform contract tests in `packages/adapters/telegram/tests/test_telegram.py`, `packages/core/src/chatcore/testing.py` and `tests/test_image_delivery_contract.py`. Assert one platform upload, real returned file identifier, and unchanged old send-photo behavior. Implement the optional capability and Telegram receipt conversion.
3. Add repository round-trip, restart/re-instantiation, retention and privacy-erasure tests in the existing core memory/PostgreSQL test modules. Implement the additive migration and repository fields/signatures. Update the exact migration-list test; do not edit applied migrations.
4. Implement outgoing receipt logging in `packages/plugins/image/src/chatcore_image/plugin.py`, then exact-reply selection in `packages/core/src/chatcore/agent.py`. Add core agent guards for missing attribution, cross-chat/topic references and unchanged text-reply mappings. Run each regression first against the old behavior and retain the expected failing evidence.
5. Add unavailable/deleted-file and post-send repository-failure cases: errors identify the missing reference or inability to retain it; no extra generation, unexpected original-photo fallback or repeated delivery occurs. Test editing disabled and non-receipt platforms retain existing behavior.
6. Update `ARCHITECTURE.md`, `CHANGELOG.md` under Unreleased, image capability documentation and this index entry. Explain the persisted metadata, privacy cleanup, additive migration and capability compatibility. Call out protected core/adapter files and attribution handling in the PR.

## Verification and handoff

Run from the isolated branch based on origin/main:

```sh
uv sync --locked --all-packages
uv run pytest -q packages/plugins/image/tests packages/adapters/telegram/tests tests/test_image_delivery_contract.py
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check .
uv run pytest -q -m "not browser and not sandbox_integration"
```

Run PostgreSQL-backed repository/privacy tests against a disposable database; record any unavailable integration checks as unverified. Format only changed files. Confirm request snapshots, skill catalog and routing sets remain unchanged where behavior is unchanged. Use fake image clients and synthetic file bytes; no paid generation or production messaging for automated validation.

Self-review the complete diff in the main chat, following the owner-selected implement-project workflow. Prepare the PR with actual checks and migration/API notes. Platform release and downstream deployment must be reported separately from local implementation. No production changes are part of preparing this plan.

## Execution and self-review record

Base: `27a218ec3ddecc3fc7a682fdf88951932566a1c8`, isolated branch
`codex/image-reply-handoff`. Owner approved implementation, merge and bot deployment, and selected
main-chat implementation/self-review with implement-project. No delegation or independent review
was performed.

Acceptance evidence:

- AC1: successive explicit output replies use the selected generated version, even when a
  conversation mapping points to the original request; agent integration regression passes.
- AC2: current-message/reply numbering, group requester checks, missing attribution and cross-chat
  or topic rejection are covered by agent integration tests. No arbitrary history promotion.
- AC3: PostgreSQL repository re-instantiation preserves references. Requester erasure and retention
  remove them; memory erasure and PostgreSQL privacy integration checks also pass.
- AC4: the adapter's receipt comes from one successful upload. Existing photo delivery tests and
  document fallback tests pass unchanged. Missing references fail without substituting the original,
  and post-send persistence failure does not repeat generation or delivery.
- AC5: additive migration only; no dependency upgrades. Documentation, explainer output and
  changelog describe metadata storage and compatibility. Release version is v0.14.0.
- AC6: downstream immutable pin, merge and production rollout verification are tracked separately
  in the deployment repository's image-reply rollout record.

Separate self-review: plan compliance and regression risk reviewed against the entire diff and
existing callers. Finding R1 (test fixture schema omitted the new column) was repaired by updating
`packages/plugins/memory/tests/test_integration.py`; the integration/release-control rerun passed
726 tests, with one skip. This required one extra existing test fixture beyond the original file
list. The three initial regressions failed on the original missing-output-reference behavior and
passed after implementation. Existing delivery-contract tests were reused rather than modified.

The first full run had 4,154 passing tests, the R1 fixture failure, and 92 local PostgreSQL setup
errors caused by exhausted macOS shared-memory IDs. Twelve stale unattached 56-byte segments from
exited test processes were removed; no live service was stopped or reconfigured. The affected
integration and release-control rerun passed. A final full-suite run is recorded below when it
finishes. Global format checking still reports 16 pre-existing Markdown plan files; all changed
Python files pass formatting. Ruff, generated explainer checking, diff checking, wheel builds for
core/Telegram/image and gitleaks pass. No paid model request or production message was sent.

Integration note: platform v0.13.0 landed during verification. The branch was rebased onto
`f5dd9372dfe0b7e5b6afd3f8f22664b970ded076`; the new output migration is now 0012, preserving
the already released trusted-group migration unchanged. Before integration, the final full suite
passed 4,249 tests (57 skipped, 29 deselected, one expected failure). Combined-tree verification
is recorded below.

Final self-review: PASS for plan compliance and regression risk after the R1 fixture repair and
main integration. Combined-tree focused checks passed 273 tests. All changed Python files pass
formatting; full Ruff, generated explainer and diff checks pass. Full combined-tree and hosted CI
checks remain explicit merge gates; no deployment success is claimed by this record.
