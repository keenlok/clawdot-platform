# Google API Editing Implementation Plan

> **For agentic workers:** Use the executing-plans skill to implement this plan in order,
> after owner approval. Use an isolated `codex/` branch/worktree at execution time.
> Checkboxes describe future implementation; this document does not authorize execution.

**Status:** Shipped inactive in platform [PR #5](https://github.com/keenlok/clawdot-platform/pull/5)
under the owner's 2026-11-03 implementation and merge authorization. Local implementation,
review and testing are recorded in the [execution handover](../handovers/2026-11-03-google-editing.md).
Downstream release-pin integration, copy qualification and deployment remain separate.
**Baseline:** `main` at `0a5cd47` on 2026-11-02.
**PR integration base:** `1e990a5`, including trip Drive associations, their activation and PDF/CSV readers.
The existing `0002_trip_files.sql` migration is preserved; this plan uses `0003_edit_results.sql`.
**Goal:** Add confirmed, bounded edits to native Google Docs and Sheets, with an edited copy
when original-file editing is unavailable. A DOCX edit always creates a native Google Doc.
**Architecture:** Extend `chatcore_google` with deterministic edit, destination and verification
services. Reuse its owner OAuth client, human-link provenance, writer checks, pending actions
and durable reservations. Browser editing is a separate, optional delivery.
**Tech stack:** Python 3.12+, existing uv workspace, HTTPX, Google Drive/Docs/Sheets REST APIs,
existing Postgres repositories, pytest and `httpx.MockTransport`. No new dependency proposed.
**Authorship:** Codex drafted and self-reviewed. The required Claude planning pass was attempted
with the installed CLI, which failed: "OAuth session expired and could not be refreshed."
No Claude review is claimed; this uses AGENTS.md's unavailable-model fallback. A separate
Codex review during PR preparation identified the replacement-scope correction recorded below.

**Repository split reconciliation (2026-11-03):** The baseline and integration hashes above
belong to `clawdot-platform-history`. The implementation starts from fresh public platform
`f918fa9`, not those historical commits or PR 59. Platform owns the Google plugin, migration,
generic tests and reusable services. References below to `apps/sb_bot` translate to
`clawdot-deployments/bots/sb_bot`; bot tests run there against installed candidate wheels.
Commands spanning both old roots must be run separately in the two active checkouts.
The downstream production pin remains v0.1.1 until a reviewed release contains this work.
Current records stay in `docs/plans` and `docs/handovers`; immutable archives are unchanged.

## 1. Decisions and delivery boundaries

The owner requested both an API editing plan and a separate
[Playwright plan](2026-11-02-google-browser-editing.md), before implementation.

- Native Docs/Sheets: edit the original when effective write access exists; otherwise prepare
  an edited copy if copying and app authorization permit it.
- DOCX: download the original package and convert it into a **new native Google Doc**, then edit
  that Doc. Never update a DOCX, export the result back to Word, or reconstruct it from the
  main-body text extractor. This applies even when the owner can edit the original DOCX.
- A preview states original versus copy, source, destination type/title, selected tab/range,
  before/after values and exact changes. Copy creation is itself a confirmed write.
- API editing is usable without Playwright. The browser plan consumes the shared destination,
  confirmation and result primitives defined here; there is no automatic browser fallback.
- Existing create/append, itinerary import/suggest and expense exports retain their behavior.
- Preserve existing trip-file associations and their scoped read authority. Creating an edited
  copy does not automatically replace a trip's primary itinerary or attach a new reference.
- This first delivery edits text and literal cell values. It does not add structural document
  editing, sheet row deletion/reordering, formula authoring, sharing, Drive search, Slides,
  comments, bookings or a model-accessible arbitrary `batchUpdate` endpoint.

Split into reviewable changes: existing-workflow verification; shared destination/result
handling and native Doc edits; Sheets edits; live qualification and operator documentation.
Keep DOCX conversion and native-copy handling in the shared destination change.

## 2. Destination and permission policy

| Input | Effective original edit access | Copy/download access | Destination |
| --- | --- | --- | --- |
| Native Doc/Sheet | Available | Any | Original, unless the requester explicitly chooses a copy |
| Native Doc/Sheet | Unavailable | Copy is permitted and authorized | New file of the same native type |
| Native Doc/Sheet | Unavailable | Copy is prohibited or unavailable to the app | No mutation; explain the missing access |
| Drive DOCX | Any | Download/conversion permitted | New native Google Doc, always |
| Drive DOCX | Any | Download/copy restricted | No mutation; do not bypass the restriction |
| Unknown type, inaccessible source or ambiguous tab | Any | Any | No mutation; request a supported target |

Extend metadata reads to include `version`, `headRevisionId` (binary files),
`isAppAuthorized`, `copyRequiresWriterPermission`, content restrictions, and
`capabilities(canEdit,canModifyContent,canCopy,canDownload)`.
Account capabilities do not prove an OAuth grant allows a particular API operation.

Two grant families apply, and they differ:

- **Original Docs/Sheets edits** use the installed full `documents` and `spreadsheets` scopes
  (`oauth.py`). These already authorize Docs/Sheets API writes to *any* native file the owner
  can edit, with no per-file Drive grant. For originals, "effective edit access" is therefore
  the owner's `capabilities.canEdit`/`canModifyContent` plus no content restriction. Do not
  route an editable original to the copy path or request extra authorization for it.
- **Drive copy/upload** uses `drive.file`, which is per-file: it covers files the app created
  or was granted, not every file the owner can see. `files.copy` of an externally created
  source is the case qualified below.

Never infer authorization by attempting a mutation during proposal.

Before enabling native-copy fallback, qualify `files.copy` with the installed owner's actual
grant against an app-created file, an externally created editable file and a viewer-only file
whose owner permits copying. Record the results without private IDs. If a case needs additional
per-file authorization, report `needs_file_authorization`; do not silently request full `drive`
scope or claim all fallback cases work. A Picker/Open-with grant flow would be a separately
reviewed authorization extension. A 403 caused by quota or revocation is not automatically
classified as a read-only source.

Copies go to the configured owner's My Drive root, with a deterministic previewed title and
no source-parent or sharing-permission replication. Request `ignoreDefaultVisibility=true`
where supported; domain policies can still affect effective visibility. Do not promise that
every created file is private or automatically accessible to chat members.

Recheck permissions at confirmation. If the preview selected the original and access is lost,
stop and offer a fresh copy preview; never silently switch the confirmed destination.

## 3. Public tool contracts

These tools return pending actions, never execute immediately:

```json
{"tool":"google_edit_doc","arguments":{
  "url":"<human-posted source URL>","find":"Dinner at 7pm",
  "replacement":"Dinner at 8pm","tab_id":null,"destination":"auto","copy_title":null
}}
```

```json
{"tool":"google_edit_sheet","arguments":{
  "url":"<human-posted spreadsheet URL>","tab":"Day 1","range":"G5:H5",
  "values":[["Confirmed","Reservation received"]],"destination":"auto","copy_title":null
}}
```

`destination` is `auto` or `copy`; there is no override that writes to a DOCX.
`copy_title`, when supplied, is 1–200 characters; otherwise derive `<source name> — edited`.
The handler reads before-content itself rather than trusting model-supplied old values.
All fields receive descriptions in `schemas.py`; reject unknown fields.

Doc edits are case-sensitive literal replacements within one explicitly resolved native tab,
with exactly one supported match. A selected match must stay within one main-body paragraph
or table-cell paragraph. Multiple tabs require a tab ID; never default a replacement to all
tabs. The first conversion of DOCX uses its single imported main-body tab and reports this
mapping. Ambiguous, truncated or unsupported structural matches do not produce a write preview.
Use a bounded structured snapshot, not the existing flattened `doc_text()` output, to validate
the match, derive its exact UTF-16 start/end indices and retain the API's document revision.
Exclude headers and footers. Read the snapshot with `suggestionsViewMode=SUGGESTIONS_INLINE`
(explicitly, for both proposal and pre-commit reads): batchUpdate indices address the
document with suggestions inline, so a suggestion-free preview mode would yield indices that
drift past any pending suggestion. Never derive write indices from
`PREVIEW_WITHOUT_SUGGESTIONS` or `PREVIEW_SUGGESTIONS_ACCEPTED`. Refuse a match whose span
contains or touches suggested insertions/deletions (`suggestedInsertionIds`,
`suggestedDeletionIds`, suggested style changes), and refuse when the selected tab has
pending suggestions before the span unless the inline index derivation is covered by a test.
Refuse ranges containing structural markers, inline objects, paragraph-ending newlines or
mixed text styling in this first release; this keeps replacement formatting deterministic
while preserving unrelated content.

Sheets edits select a stable numeric `sheetId`, a bounded rectangular A1 range and an equally
sized rectangular literal grid. Limit edits to 100 cells per action as well as existing
row/column/character caps. Blank clearing must be explicitly represented in the proposed grid.
Refuse merged/spilled/protected target cells and formula replacement in this first release.
Use typed `userEnteredValue` snapshots; formatted strings alone cannot distinguish a number,
formula and literal text. Reuse literal-value validation, but **not** `render.neutralize()`:
`updateCells` with `ExtendedValue.stringValue` is never parsed as a formula, and a prepended
apostrophe would be stored literally, so `-2 pax` or `+65 …` would show a visible `'` and
mismatch the preview and read-back. Write the previewed literal unchanged as `stringValue`;
the preview, frozen payload and verification compare that exact string. Never use
`USER_ENTERED` value input for edits. Changing a numeric/boolean cell to text is a type
change shown in the preview.
For itinerary requests, resolve the exact visible row/range and preview it; ambiguous duplicate
items require user disambiguation. Do not add hidden row IDs or silently migrate tracker schemas.

## 4. Pending action and state contracts

Use `plugin="google"`, `kind="edit_doc"` or `"edit_sheet"`. Preserve existing owner/config
revision, chat, requester and topic checks. Freeze a JSON-serializable payload containing:

```json
{
  "owner_email":"configured owner", "owner":"verified permission ID", "revision":0,
  "source":{"file_id":"source-id","mime":"native MIME or DOCX","content_revision":"opaque"},
  "destination":{"mode":"original","mime":"native MIME","title":null},
  "precondition":{"document_revision":"opaque revision","tab_id":"tab-id",
    "start_index":100,"end_index":113},
  "edit":{"find":"Dinner at 7pm","replacement":"Dinner at 8pm"}
}
```

For Sheets, preconditions contain sheet ID/title, grid properties, target-range typed values
and relevant merge/protection information. Freeze content-specific preconditions, not Drive
`version`: that counter also increases on renames, sharing changes and comments, which would
turn harmless metadata changes into conflicts. Native Docs use the Docs `revisionId` (and
the selected span). Sheets use the typed target-range values plus sheet properties. DOCX uses
the package SHA-256 and `headRevisionId`, not raw bytes; redownload at confirmation and
compare the SHA-256 before uploading. A native copy freezes the same content preconditions
for the source. Drive `version` may be shown for diagnostics only. Internal reads
of a newly created destination are authorized only for this confirmed action; this does not
add the new URL to general human-link provenance. Later edit requests still require a human
link; this does not revoke existing trip-bound authority for reading attached sources.

Add `migrations/0003_edit_results.sql`, leaving both existing applied migrations unchanged. Create
`google_edit_results` keyed by `(chat_key,action_id)`, with a foreign key to `google_actions`
using `ON DELETE CASCADE`. Store kind, phase, source ID, nullable destination ID, timestamps
and a bounded error category; never store raw DOCX bytes, OAuth data or remote error bodies.
Keep preview text in the existing pending-action store with its existing privacy treatment.

Retention: the existing `purge_older_than` deletes `google_actions` rows, which would cascade
away the destination IDs of `partial`/`uncertain` copies and leave orphaned files untraceable.
Change the age-based purge so it skips actions whose result row is `destination_created`,
`applying`, `partial` or `uncertain` until a separate, longer bound
(`GOOGLE_EDIT_UNRESOLVED_RETENTION_DAYS`, default 90). Privacy purge (`purge(chat, user)`)
still deletes them immediately; privacy wins over traceability. Because a retained
reservation is what blocks replay, skipping these rows also keeps their replay protection.
Document both bounds in the operator guide and test the age purge with each phase.

Add a transactional `reserve_edit` repository operation: perform the current settings/replay
checks, insert the existing Google action reservation and its initial result row together.
Phases are `reserved`, `destination_created`, `shared`, `applying`, `verified`, `conflict`, `partial`,
and `uncertain`. A reservation permanently prevents re-executing that action while retained;
phase updates never authorize replay. Persist destination IDs immediately after creation.
Privacy purge/retention must cascade result rows and their source/destination metadata.
`shared` is the browser broker's completed permission-read-back boundary. Retain it and
`reserved` crash windows with other unresolved results. A durable one-time destination
creation claim prevents concurrent helper callers or a restart from creating another copy;
it is committed before the remote request and is never reset by a phase change.

## 5. Execution and conflict handling

1. Proposal: validate provenance, permissions, MIME, destination policy and bounded source;
   resolve the exact edit; freeze payload and full before/after preview. No remote mutation.
2. Confirmation: use existing core confirmation, then recheck requester/chat/topic, expiry,
   current writer access, owner identity, opt-out revision, limits, source and destination policy.
3. Compare source preconditions. A changed source needs a new preview. Do not regenerate
   replacement text, move a range or substitute a different tab during commit.
4. Reserve durably before the first copy, upload or content mutation.
5. For native copies, call fixed-endpoint Drive `files.copy`, then persist the destination ID,
   check its actual MIME/content/access and resolve the destination's tab IDs. IDs in a copy
   must not be assumed to match source IDs; resolve by validated structure and exact content.
6. For DOCX, use bounded multipart `POST /upload/drive/v3/files?uploadType=multipart` with
   metadata MIME `application/vnd.google-apps.document` and the original DOCX media bytes.
   This is a new-file import, not an update. Validate `canDownload`, copy restrictions and
   source bytes before upload. Existing ZIP/XML validation remains in force. No PATCH support
   or binary overwrite endpoint is added to `GoogleHttp`.
7. Read the new native Doc, validate the exact supported match and capture its fresh revision.
   If conversion changed the match or introduced ambiguity, leave the copy unedited and report
   its link as a partial result. Never guess a replacement after the original preview.
8. Docs: compile one atomic batch containing `deleteContentRange` for the exact validated
   main-body UTF-16 range and `insertText` at its start, with explicit `tabId` on each request
   and `writeControl.requiredRevisionId`. An empty replacement omits insertion. Do not issue
   tab-wide `replaceAllText`: the same phrase could exist in an excluded header or footer.
   Validate inherited insertion style against the supported uniform-style span and explicitly
   restore that span's style when needed within the same batch.
9. Sheets: re-read typed before-values and tab/range protections immediately before a bounded
   `updateCells` request with field mask `userEnteredValue`. Preserve unrelated formatting,
   validation, notes and cells. Do not expose raw requests to the model.
10. Read back the exact target, verify expected text/cell types and values, then mark verified.

Example Docs request compiled by the handler:

```json
{"writeControl":{"requiredRevisionId":"captured revision"},"requests":[
  {"deleteContentRange":{"range":{"startIndex":100,"endIndex":113,"tabId":"resolved-tab-id"}}},
  {"insertText":{"location":{"index":100,"tabId":"resolved-tab-id"},"text":"Dinner at 8pm"}}
]}
```

**Concurrency limit:** Docs revision checks reject stale writes server-side. Sheets has no
equivalent `requiredRevisionId` in this API. Its before-value check can detect earlier edits
but cannot prevent a collaborator edit between that read and the write. Read-back verification
does not remove that race. State this limitation in the Sheet preview and operator guide;
allow requesters to choose a copy. Do not advertise atomic compare-and-swap or implement
automatic overwrite/rollback of a collaborator's later changes.

Return explicit results: `verified` with destination URL and coverage; `conflict` with a fresh
preview instruction; `partial` with any known created-file URL and failed stage; `uncertain`
when remote application cannot be established. A definite pre-mutation refusal is not an
uncertain write. Never report success solely because HTTP returned 200 or retry an unknown
copy, conversion or edit automatically. No automatic file cleanup or deletion.

## 6. Files and ownership

Paths below are repository-relative for the engineer executing the plan.

| File | Responsibility |
| --- | --- |
| `packages/plugins/google/src/chatcore_google/edit_models.py` (new) | Strict edit/precondition/destination/result contracts and bounds |
| `packages/plugins/google/src/chatcore_google/edit_snapshot.py` (new) | Structured bounded Doc/typed Sheet snapshots and literal match validation |
| `packages/plugins/google/src/chatcore_google/destinations.py` (new) | Permission-aware original/copy policy, native copy and DOCX import |
| `packages/plugins/google/src/chatcore_google/edit_service.py` (new) | Proposal/commit sequencing, verification and outcome classification |
| `packages/plugins/google/src/chatcore_google/client.py` | Explicit metadata, typed grid reads, copy/import and revision-checked edit methods |
| `packages/plugins/google/src/chatcore_google/repository.py` | Atomic edit reservations, result transitions and scoped privacy cleanup |
| `packages/plugins/google/src/chatcore_google/migrations/0003_edit_results.sql` (new) | Additive result journal |
| `packages/plugins/google/src/chatcore_google/plugin.py`, `schemas.py`, `gates.py` | Tools, descriptions, skill membership, action dispatch and writer gates |
| `packages/plugins/google/src/chatcore_google/settings.py` | Edit-cell limit and unresolved-result retention; no new broad OAuth scope |
| `packages/plugins/google/tests/test_edit_models.py`, `test_edit_snapshot.py`, `test_destinations.py`, `test_edit_service.py` (new) | Pure-policy, snapshot, destination and confirmation tests |
| Existing Google `test_client.py`, `test_plugin.py`, `test_pg_google.py` | REST, tool catalog, durable concurrency/replay/privacy checks |
| `apps/sb_bot/tests/test_google.py`, `tests/data/routing_set.jsonl` under the same app test directory | Real confirmation workflow and route examples |
| `docs/google-workspace.md`, `docs/plans/README.md`, `ARCHITECTURE.md` | Operator limits, delivery status and accepted architecture decisions |

`service.py` supplies existing authorization/action helpers; avoid growing it into another edit
implementation. Keep plugin `__init__.py` exports-only; no app-to-plugin submodule imports.
Any `gates.py`/permission change, new migration or public export is called out for maintenance
review. Update request snapshots only when the actual advertised tool catalog changes.

## 7. Ordered implementation tasks

### Task 1: Establish the existing write baseline

- [ ] Sync with `uv sync --locked --all-packages` and inspect current confirmation callers.
- [ ] Run `uv run pytest -q packages/plugins/google/tests apps/sb_bot/tests/test_google.py`.
  Expect existing tests to pass; integration skips require an explicit explanation.
- [ ] Extend `apps/sb_bot/tests/test_google.py` with a real ConfirmService create/append round
  trip: no mutation before confirmation, cancel/wrong-user/wrong-topic do nothing, confirming
  twice calls the mock once, and changed permissions/opt-out invalidate the pending action.
- [ ] Observe the new assertions pass for existing behavior; fix a discovered bug separately
  with a failing regression before changing its implementation. Do not duplicate coverage.
- [ ] Record the baseline and commit any necessary regression fixes independently.

### Task 2: Define snapshots and destination decisions

- [ ] Write tests in `test_edit_models.py`, `test_edit_snapshot.py`, `test_destinations.py` for
  every row in §2, DOCX with `canEdit=true` still converting, malformed ranges, duplicate text,
  multiple tabs, split text runs, formulas, merges and typed before-values.
- [ ] Run those three test files and observe failure because the new modules do not exist.
- [ ] Implement the contracts and deterministic policy from §§2–4; no network mutation here.
- [ ] Add fixed client method tests for metadata fields, typed grid reads and revision IDs.
  Implement only those reads. Re-run the same tests to green and commit.

### Task 3: Add reservation results, native copy and DOCX import

- [ ] Add failing repository tests for same-action concurrent reservation, settings revision,
  partial-copy ID retention, restart replay refusal and chat/user/retention cleanup, including
  age purge skipping unresolved phases until their longer bound and privacy purge removing them.
- [ ] Add the additive migration and transactional journal operations from §4. Confirm with
  a disposable Postgres schema; never modify the shared development migration history.
- [ ] Add MockTransport tests for the exact copy and multipart import paths, MIME/byte bounds,
  no copy/upload before confirmation, unchanged source and no permissions/delete calls.
- [ ] Implement `destinations.py` and explicit client helpers. New file IDs are action-local.
- [ ] Run the command below, then commit. Record integration prerequisites if unavailable.

```sh
uv run pytest -q packages/plugins/google/tests/test_destinations.py packages/plugins/google/tests/test_client.py packages/plugins/google/tests/test_pg_google.py
```

### Task 4: Implement native Doc edits and converted DOCX edits

- [ ] Write failing `test_edit_service.py` cases for frozen preview, one exact match, explicit
  tabs, native-copy ID remapping, changed revision, revoked access, DOCX byte change, conversion
  mismatch leaving an unedited copy, timeout after copy, timeout after edit and failed read-back.
- [ ] Include identical text in the main body and a header/footer, plus emoji before the body
  match. Assert compiled UTF-16 ranges edit only the previewed body span. Cover uniform-style
  restoration and refusal of structural or mixed-style spans.
- [ ] Add a pending suggested insertion before the match and one inside it: assert the
  snapshot request uses `SUGGESTIONS_INLINE`, indices account for the earlier suggestion (or
  the edit is refused), and a span touching a suggestion is refused. Add a rename/comment
  between preview and confirm and assert it is not a conflict.
- [ ] Implement §5 through the Doc path; return a PendingAction during proposal and reuse core
  confirmation during commit. Journal before every possible first mutation.
- [ ] Register `google_edit_doc` and both edit action kinds only when configured, add writer
  gates and bounded plugin-owned skill guidance. Do not switch API failures to browser jobs.
- [ ] Run the command below and require all relevant cases to pass.
- [ ] Inspect the compiled REST request and verify the wrong revision never edits; commit.

```sh
uv run pytest -q packages/plugins/google/tests/test_edit_service.py packages/plugins/google/tests/test_plugin.py apps/sb_bot/tests/test_google.py
```

### Task 5: Implement bounded Sheets edits

- [ ] Add failing tests for rectangular shape, 100-cell cap, stable sheet ID/title, wrong `gid`,
  type changes, stale before-values, blank clearing, formula/protection/merge refusal, preserved
  formatting and read-back mismatch. Include literals starting with `-`, `+`, `=` and `@`:
  the request carries the exact string as `stringValue` with no apostrophe and read-back
  verifies it. Include a fake collaborator race and assert that the
  result does not claim compare-and-swap protection.
- [ ] Implement typed snapshots, literal `updateCells` compilation and §5 verification. Add
  `google_edit_sheet`; expose exact range/old/new values and the concurrency limitation.
- [ ] Update Google and itinerary routing cases, skill catalog and actual request snapshots.
  Keep current tracker schemas; edits do not reorder or delete itinerary rows.
- [ ] Run Google/app tests plus `apps/sb_bot/tests/test_routing_set.py`,
  `test_skill_routing_set.py`, `test_skills_catalog.py`, `test_skill_instruction_ownership.py`
  from that test directory and `tests/test_package_boundaries.py`. Commit after green.

### Task 6: Review, qualification and handoff

- [ ] Self-review every destination branch, state transition, permission gate and privacy path.
- [ ] Document the new tools, scopes, copy restrictions, fidelity boundaries, partial results
  and Sheets race in the operator guide; update accepted architecture and the plans index.
- [ ] Run the repository checks in §8 and record all failures with their cause and scope.
- [ ] Open a reviewable PR with executor/reviewer roles, dependency statement, migration and
  gate changes. Do not merge, deploy, rotate credentials or perform live writes under this plan.
- [ ] After separate live-test authorization, use non-sensitive samples in one test chat:
  native create/append, editable original, viewer copy, restricted copy, DOCX conversion, source
  change after preview, lost access and uncertain outcome inspection. Confirm source DOCX
  checksum is unchanged and newly returned links open with expected permissions.
- [ ] Mark capability cases enabled only after their actual owner-grant checks succeed.

## 8. Checks and acceptance

Run each as its own command:

```sh
uv sync --locked --all-packages
uv run ruff check packages services apps tests
uv run ruff format --check .
uv run pytest -q -m "not browser and not sandbox_integration"
git diff --check
```

Expected: lint, changed-file formatting, diff and tests pass. Record existing unrelated
formatting failures and unavailable integration services; never claim skipped tests ran.
Use a disposable Postgres database for migration/reservation tests. No real user document,
cookie, file ID, OAuth profile or private content enters fixtures/logs.

- [ ] Native original and copy destinations follow §2 and remain frozen after confirmation.
- [ ] DOCX always yields a native Google Doc copy; no request writes to the DOCX source.
- [ ] No copy, conversion or content change happens before requester confirmation.
- [ ] Docs reject stale revisions; Sheets behavior accurately describes the residual race.
- [ ] Every uncertain/partial outcome reports its known destination and cannot replay.
- [ ] Existing Google/CSV/expense/itinerary behavior and privacy checks remain covered.

## 9. References

- [Drive capabilities and app authorization](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
- [Drive OAuth scope boundaries](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Native file copy](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/copy)
- [DOCX upload and native conversion](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
- [Docs revision checks](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate)
- [Docs tab selection](https://developers.google.com/workspace/docs/api/how-tos/tabs)
- [Sheets batch and collaboration guarantees](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate)
- Existing [Google design](2026-11-01-google-workspace.md) and [operator guide](../google-workspace.md).

## 10. PR preparation review

A separate Codex reviewer identified that tab-wide `replaceAllText` could change a matching
header/footer outside the validated main-body span. The plan now requires an exact UTF-16
range replacement with a revision check and body/header regression coverage. PR integration
also reserves migration number `0003`, preserving the trip-file migration on the latest base.

A later Claude review of PR #59 led to these corrections: snapshot indices use
`SUGGESTIONS_INLINE` and refuse spans touching suggestions; Sheets edits do not apply
formula-prefix neutralization to `stringValue`; preconditions use content revisions rather
than Drive `version`; unresolved results outlive the normal action retention; and §2 now
states that the installed `documents`/`spreadsheets` scopes already authorize original edits.
