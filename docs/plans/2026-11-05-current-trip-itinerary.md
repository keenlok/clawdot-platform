# Current trip itinerary targeting

Status: Planned; owner approved on 2026-11-05, implementation in progress.

Owner clarification: subsequent compatible, editable native-Doc revisions update
the same current document. Copying is reserved for conversion, a noneditable
source, or an explicit request for another copy.

## Goal and observed failure

A group member asking to update an existing trip itinerary must start from the
chosen working document, preserving earlier confirmed edits. The bot must not
silently rebuild from an older source found in chat history.

Read-only production inspection on 2026-11-05 established that the latest request
to add a bar to a Guangzhou day selected an earlier DOCX using a historical human
message reference. Its pending preview would import another native copy. An
earlier human message had explicitly selected the revised native output, and an
edit to that output was verified. No trip-file associations were saved. The latest
preview remains unconfirmed; this investigation performed no Google writes.

The existing output journal authorizes eligible new outputs but does not select a
working document. Historical output rows without owner/topic bindings must not be
silently backfilled. Ordinary file copies deliberately do not change associations.

## Approaches and decision

1. **Recommended: durable trip selection and explicit revision advancement.** Reuse
   the saved primary itinerary, add an edit path resolved by trip, and advance it
   only when the confirmation explicitly includes that association change.
2. Improve history-search instructions alone. Smaller, but selection remains
   dependent on retrieval ranking and conversational context.
3. Always choose the newest output in a chat. Rejected: unrelated documents and
   alternative drafts can be newer than the chosen itinerary.

Use approach 1. The primary itinerary is the current document, not an arbitrary
file selected by timestamp. Native editable Docs are edited in place by default.
An explicitly requested copy can become current when the preview says so.

## User-visible behavior

- Resolve a named trip, read its saved primary itinerary, and edit that document
  for requests such as “add this to our Guangzhou day itinerary.” Ask which trip
  when ambiguous; do not use the active expense default for a different trip.
- “Use this as our itinerary” saves an explicit primary-file selection through a
  confirmed attachment. A retained human source message can supply the link.
- If no primary is saved, offer to select the human-chosen revision; do not
  silently select the oldest source or the newest file by date.
- For a requested revised copy, the preview names the trip, exact source URL,
  destination, access role, and whether the new file becomes the current itinerary.
  A standalone copy remains standalone unless that association is confirmed.
- Advance the primary only after the new output is fully verified. Retain the
  previous primary as a reference. Failure, uncertainty or conflicting changes
  leave the current association unchanged and are reported separately from any
  already-created or already-edited document.
- Reject a historical edit target that is a saved previous itinerary/reference
  when another primary is selected, unless the user explicitly identifies that
  older target in the current request/reply. A historical source-message argument
  alone does not authorize replacing the current choice.
- Every edit still requires the existing requester confirmation. Do not replay
  the pending bar edit or old uncertain actions automatically.

## Implementation boundaries

Framework implementation stays in the Google plugin; downstream SBot owns its
composite routing instructions. Keep package exports and architecture boundaries.

1. `trip_files.py`: centralize primary resolution and association proof creation;
   expose `trip_edit_doc` through `plugin.py`. This tool takes an explicit trip and
   bounded replacement, not a model-selected URL. Support the same destination
   and access choices as the existing Doc editor and an explicit make-current
   choice for copies. Saved-source authority applies only to that selected file.
2. `edit_service.py` and `service.py`: accept a validated trip-source proof built
   by the service, freeze the trip/file association version and settings in the
   confirmation payload, and recheck identity, trip scope, association and settings
   before content reads and before mutations. Do not accept a model-supplied proof.
   Preserve exact revision/text/style checks and the existing bounded read-backs.
3. `repository.py`: use the existing chat advisory lock and compare-and-swap
   file-state check for primary advancement. Perform no network calls inside the
   database transaction. Never overwrite a concurrent explicit trip selection.
   Extend durable result data only if needed for independently reportable,
   idempotent association completion; use a new additive migration if required,
   never edit an applied migration.
4. `plugin.py`, `schemas.py` and trip-file handlers: add argument descriptions,
   skill membership and gates for the new tool; handler authorization remains
   mandatory. Add historical source-message support to confirmed Doc attachment
   without authorizing other history-search results or unrelated formats.
5. Add stale-target protection to generic Doc edits using saved trip associations.
   A direct user request naming an older file remains possible; do not silently
   redirect a frozen edit to a different file.
6. Downstream `bots/sb_bot/src/sb_bot/skills.py`: select the trip's saved primary
   before searching history and use `trip_edit_doc` for implicit trip updates.
   Keep plugin-owned instructions within 800 characters; update catalog/routing
   fixtures and request snapshots as required.

Existing link-viewer default/editor-on-request behavior, original-file ACLs,
Sheets behavior, browser-disabled state and unrelated tutor configuration remain
outside this fix. No new third-party dependency is proposed. Saved-trip edit
authority is a permission change and must be described in the PR.

## Regression and verification plan

- Reproduce the reported sequence with synthetic IDs: import source A to B,
  confirm an accommodation edit in B, save B as primary, then have another member
  add an activity. Assert the edit targets B, preserves the accommodation edit,
  and does not download A or create another copy.
- Without a primary, assert a selection prompt instead of an edit from history.
- Confirm a copy from B to C with make-current enabled, then assert the next
  trip edit targets C. An unrelated newer draft D must not change this choice.
- Test standalone copies, rejected/expired confirmations, read-back uncertainty,
  sharing failure, duplicate delivery, post-Google database failure, and an
  association changed between preview and commit. No automatic mutation replay.
- Test generic historical A/B selection after C becomes primary, and a current
  human request explicitly targeting an older file. Preview URLs must remain exact.
- Test wrong chat/topic, wrong owner, revoked settings/access, group membership,
  removed association, unsupported source types and legacy unbound output rows.
- Run memory and PostgreSQL repository cases, tool availability/gates, source
  provenance, follow-up agent, routing/catalog, package boundaries and plan index.
- Run repository-required locked sync, lint, changed-file formatting and the full
  non-browser/non-sandbox-integration suite; report unchanged baseline failures.

## Execution, review and release

Use a failing regression before implementation. Repository model policy assigns
Luna execution and Sol review. Claude CLI is installed but a fresh auth check
reported loggedIn=false; use the documented available-model fallback for planning
and final review, and disclose that in the PR.

After approval, create the detailed execution checklist, implement and review,
publish a tested platform patch release, update the private exact release pin and
SBot composite, and use its ordinary CI-gated deployment. Preserve later changes
already on main, including the Cantonese repair. Verify installed source, default
sharing and browser-disabled configuration without live document mutations.

## Existing trip recovery preview

Private recovery will select the revised native Doc that the owner previously
said “use this” for, as primary for the HK–Shenzhen–Foshan trip. The original DOCX
may be retained as a reference so stale-target protection can identify it. Before
any live association write, present exact trip/file URLs and roles using the normal
confirmation workflow. Do not publish private IDs or document content in public
plans/tests. Selecting the itinerary changes bot database associations only;
content edits and ACL changes require their own previews and confirmations.
