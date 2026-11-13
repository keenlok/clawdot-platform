# Shared group photo edits

Status: Approved scope on 2026-11-14: the owner selected “Any explicitly replied-to photo in the
same group.” This supersedes the own-upload restriction deliberately preserved by the earlier
[reply handoff fix](2026-11-13-image-reply-handoff.md). The owner’s existing authorization to
implement, merge and deploy this issue remains in effect. Implementation and self-review stay
in the main Codex chat under implement-project, without delegation.

Base: platform `d8a0516` and deployment `25a9389`. Isolated branches are
`codex/shared-photo-edits` in each repository. Existing user work is preserved.

## Accepted behavior

- AC1: any authorized group member can explicitly reply to another member’s stored photo and
  request an edit. The actual replied-to photo is submitted, not an inferred historical upload.
- AC2: the same works for stored bot-generated photos, including successive edits and photos
  with no requester attribution. Missing older output records still require re-uploading.
- AC3: another member’s photo reached only through a text-reply/conversation mapping or arbitrary
  history remains unavailable for editing. Cross-chat and cross-topic references remain rejected.
- AC4: own current uploads, DM behavior, numbering, opt-in edit configuration, moderation,
  per-turn and daily budgets, metadata retention and requester erasure remain compatible.
- AC5: the offered edit instructions explicitly state the new shared-photo rule so prior refusals
  in conversation history do not suggest that only the original requester can edit.
- AC6: publish a reviewed platform release, pin its exact commit in the bot, merge after CI and
  verify the deployed source and health. No paid generation or unsolicited chat message is needed.

## Implementation

Simply removing the owner check would also allow indirect conversation-history photos. Instead,
core records an ephemeral `TurnState.explicit_reply_attachment_ids` set containing only image IDs
from the exact stored reply after validating its chat/topic. Prefer that exact photo over origin
mapping for both human and bot sources. Native vision and the edit tool use the same selected
source. Text-only reply remapping continues to work as before, but gains no shared-edit authority.

In `chatcore_image.plugin`, permit a group edit when the attachment belongs to the requester or
its ID is in that explicit-reply set. Keep call-time checks and fail-closed owner/attachment
alignment. Reject other inferred sources with a direct-reply instruction. Keep attribution for
privacy cleanup and future outputs; it is no longer an ownership barrier for explicit replies.

Update editing-on skill instructions within the 800-character cap and current documentation.
Editing-off text, schema and snapshots remain unchanged. No migration or dependency is needed.
Call this out as a permission-policy change in the PR even though gates.py and permissions.py
need no changes.

## Ordered verification

1. Change agent integration regressions from the superseded refusals to successful edits for
   another member’s upload and generated output. Add direct stored bot-photo coverage and an
   indirect text-reply refusal. Observe the expected failures before changing production code.
2. Add the ephemeral provenance field, scoped exact-reply selection and handler check. Update
   unit refusals to distinguish explicit replies from unproven other-member attachments.
3. Run the image, core vision/context, routing/snapshot and package/plan boundary checks.
   Separately review the entire diff for plan compliance and regression risk; repair findings.
4. Run all AGENTS.md sync/lint/format/test checks. Format only changed files; record inherited
   formatting or local service limitations. Use fake image clients and disposable test services.
5. Release v0.16.0 (v0.15.0 is already on main), preserve all existing migration hashes, and update
   every deployment source/lock identity to the same published commit. Verify dependency and
   encrypted-envelope checks, hosted bot/renderer builds and CI before merging the rollout.
6. Trigger the existing CI-gated poller only after main CI passes. Verify the current release,
   installed core/plugin source hashes, editing enabled state and healthy zero-restart containers.
   Record actual results and rollout receipt in the PRs; do not claim a real-image trial.

Integration record: main advanced to `30a5e3c` (v0.15.1) during verification. Rebased onto
that routing fix; preserved its changes. The first full run passed 4,354 tests and found three
core tests still asserting the superseded blanket bot-photo exclusion. Updated those tests to
assert the approved explicit-reply behavior, untrusted image treatment, ordering and scoped
provenance. The affected core/image rerun passed 82 tests. No production implementation change
was needed for those assertion updates.

## Self-review and verification

Plan compliance: PASS. Regression risk: PASS after reviewing the full diff and existing callers.
Core binds the shared-edit exception to exact reply IDs after chat/topic validation, the handler
still performs access and budget checks, and the model is told the same rule. Attribution and
privacy cleanup remain intact. This is an intentional permission-policy change; gates.py and
permissions.py are unchanged. No dependencies, migrations or environment settings changed.

Red/green: three integration cases failed on the old implementation (another member's upload,
another member's generated output, and an unattributed stored bot photo). After implementation,
93 focused checks passed; the three outdated blanket bot-photo exclusions from the full run were
updated and the affected 82 tests passed. After integrating v0.15.1, 206 image, core vision/hooks,
provenance, routing, snapshot, inviter, package and plan checks passed. Ruff, generated explainer
checking, changed-file formatting, diff checks and gitleaks pass. Global formatting still reports
16 inherited Markdown plan files, which are left untouched.

The final combined full-suite run and hosted CI are release/merge gates; final results and the
actual downstream rollout receipt are recorded on the associated PRs. No independent review or
paid live-image test is claimed.

The combined v0.15.1 run passed 4,359 tests (57 skips, one expected failure), and PR CI passed.
Before merge, v0.15.2 (`703c626`) landed with bounded inviter argument descriptions. Integrated
that maintenance commit without changing the shared-photo implementation, preserving its new
metadata regression. Re-ran relevant checks and required CI on the merged tree.
