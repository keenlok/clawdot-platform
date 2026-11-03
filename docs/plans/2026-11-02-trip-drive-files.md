# Trip Drive files and itinerary sources

Status: Live, limited via PR #56. Owner-approved merge and automatic deployment
completed; deployed read/confirmation smoke checks passed.
Date: 2026-11-02 (Asia/Singapore).
Planner: Codex, with Codex self-review. The owner's computer has both CLIs, but
`claude auth status` reports signed out; AGENTS.md's available-model fallback applies.

## Problem and requested result

A user should be able to read a human-shared Drive DOCX and associate it with an existing
planned trip. An empty model completion must follow the agent's bounded retry path, including
when it occurs after lazy skill loading. Scripted-provider tests cover empty normal and
token-limited completions without retaining production messages or itinerary contents.

`itinerary_link_trip` originally supported native spreadsheets only. The reusable feature
adds Docs, DOCX, Sheets and arbitrary Drive references, with PDF/CSV readers where supported.
User-facing terminology is **trip** throughout.

Synthetic example: "Set this document as the itinerary for Example trip" resolves an
existing test trip, previews the local association and requests confirmation. Later reads
resolve the saved source without requiring conversion or another pasted link. Actual travel
names, dates, source files and incident evidence stay in private deployment records.

## Approaches and choice

1. **Store Drive file associations on trips, with format-specific readers
   (recommended).** Preserve the original file, support multiple references and a
   primary itinerary, and keep spreadsheet tracking as an optional workflow.
2. Import every itinerary into a Sheet before linking it. Reuses today's tracker,
   but forces a conversion the owner did not request and cannot preserve all
   document structure.
3. Move the trip domain into a new shared plugin immediately. Could simplify a
   broader future trip platform, but adds an unnecessary migration and refactor to
   this request. Existing trip records already provide the identity needed here.

Choose approach 1. Keep the current trip records and existing plugin boundaries.

## User-visible scope

| File | Attach to trip | Read contents in this change |
| --- | --- | --- |
| Native Google Docs | Yes | Existing bounded Docs reader |
| Native Google Sheets | Yes | Existing bounded Sheet reader; tracker summaries retain their current rules |
| Drive DOCX | Yes | Existing bounded paragraph/table reader |
| Drive PDF | Yes | Bounded selectable-text extraction, with page coverage |
| Drive CSV | Yes | Bounded UTF-8/UTF-8-BOM parsing, with row/column coverage |
| Other ordinary Drive files, including Slides, images, XLSX, audio and archives | Yes | Reference metadata/link only; explicitly report unsupported content reading |

A trip may have one primary itinerary and multiple supporting files. "Set this
as the itinerary" selects the primary role. "Attach this booking/document" selects
reference. If the request does not distinguish an itinerary from a reference, ask
one focused question. A supported file can be saved even when content extraction
fails: the preview must disclose that it was attached but not successfully read.
Unsupported formats can also be primary itineraries, with the same clear reading
limitation. Never claim their contents were checked.

PDF images/scans without a text layer need OCR, which is outside this change.
Encrypted PDFs are attachable references but are not decrypted or read. CSV
encoding errors are reported without guessing another encoding. CSV formulas are
text and are never evaluated. Attaching or reading creates no expenses.

This change does not edit DOCX/PDF/CSV or arbitrary Drive files. Existing confirmed
native Docs/Sheets writes remain available under their current contracts. Import
into a tracker is still an explicit, separate operation. Folders and Drive
shortcuts are rejected with a request for the actual target file link; there is no
recursive listing, shortcut traversal, or implicit Drive search.

## Data and compatibility

Add `packages/plugins/google/src/chatcore_google/migrations/0002_trip_files.sql`.
Never edit the applied `0001_google.sql` migration.

The new `google_trip_files` table stores chat key, existing trip ID, Drive file ID,
canonical HTTPS URL, metadata name/MIME snapshot, role (`itinerary` or
`reference`), owner permission ID, requester and created/updated timestamps.
Metadata snapshots are untrusted display data; content is not persisted.

Preserve the existing one-file-to-one-trip association within a chat: primary key
`(chat_key, file_id)`, an index on `(chat_key, trip_id)`, and a partial unique index
on `(chat_key, trip_id)` for role `itinerary`. A file may be used in another chat
only through that chat's independent confirmed association and authorization.
Reject moving a file between trips in this release; the user can explicitly
remove the old association and attach it to the other trip.

Backfill existing `google_itinerary_links` rows as **references** without inventing
a primary itinerary or changing trip IDs. An absent primary triggers an explicit
choice. Legacy rows have no pinned owner identity; listing them remains possible,
but reading through saved authority requires a fresh metadata/owner check and a
confirmed association update. Existing link-based Sheet reads continue as before.

Keep `google_itinerary_links` for existing planned-versus-actual comparisons.
Maintain the old and new association atomically when adding/removing a Sheet
association through either the new tools or legacy `itinerary_link_trip`.
Preserve its Sheet-only public contract for compatibility. New source attachment
tools accept the broader formats. All legacy Sheet comparisons retain original
currency handling and partial-coverage suppression.

Replacing a primary itinerary demotes the previous primary to reference in the
same transaction; the confirmation names both files. Removing an association
does not delete a Drive file or alter the trip. Confirmation binds the expected
association state so concurrent replacement/removal cannot silently overwrite a
newer decision. Repeated identical attachments are reported as already attached.

Existing trip IDs, names, members, dates, statuses, defaults, expenses, shares,
balances and settlements are untouched. Planned and closed trips can hold files;
financial mutation rules remain unchanged.

## Tools, boundaries and authority

Keep Drive metadata, associations and readers in the Google plugin. Introduce a
small app-injected read-only trip resolver exported from the expenses package's
top level. It returns only the resolved chat/trip identity and name, checks the
same membership and DM group rules as trip tools, and never builds a ledger CSV.
Google does not import expenses internals; app wiring injects the resolver.

Proposed tools:

- `trip_attach_file(trip, url, role)`: explicit trip plus human-posted Drive URL;
  metadata-only preview and requester confirmation; no remote file write.
- `trip_list_files(trip)`: resolve the trip and list its primary/reference files,
  format support, and bounded metadata. It does not download file contents.
- `trip_read_file(trip, file_id=None)`: read the primary itinerary by default, or
  an explicitly selected saved association. Read one file per call, returning
  coverage/limitations and untrusted content with `persist_content=False`.
- `trip_remove_file(trip, file_id)`: requester-confirmed removal of local metadata
  only, including the compatibility Sheet link where applicable.
- `google_read_file(url)`: human-posted-link reader that dispatches by verified
  MIME to Docs, Sheets, DOCX, PDF or CSV. Other types return metadata and the clear
  unsupported-reader status without downloading them.

Every handler rechecks configured Google owner, access policy, chat opt-out,
restrictions, and trip membership/scope. Attach/remove use the current Google
writer policy and confirmation requirement. Commit re-resolves the exact trip,
owner, file identity/type/access and expected association state. Owner change,
removed access, deleted/trashed file or conflicting state fails safely.

**Permission change:** saved trip associations become a narrow authority for
later reads of that exact file in that exact authorized trip/chat. Confirmed
attachment is the provenance; database filenames, file content and model text
never grant access to additional files. The saved-reader path must not weaken
`approved_ref` globally or copy every stored link into the turn's human URLs.
Recheck owner and Drive access on every read. No file-content link is followed.
Removing the association revokes saved-reader authority immediately. URLs from
old pending actions, summaries or memories do not independently authorize reads.
Call out `gates.py`/`permissions.py` changes and this authority extension in the PR.

Use the already granted `drive.readonly` scope and existing fixed-host requests;
no new OAuth grant is intended. Metadata adds `trashed` for all attachment types.
Canonical links are constructed from verified IDs, rather than trusting arbitrary
`webViewLink` destinations. Recognize native Slides URLs for metadata attachment
only; retain strict hosts, HTTPS and ID validation. Preserve current handling of
Docs tabs and Sheets gid selectors when reading.

## PDF and CSV readers

Add focused `file_readers.py` and `pdf_worker.py` modules under `chatcore_google`.
Declare `pypdf>=6.21.4,<7` directly in the Google plugin and update the lockfile;
the workspace currently already locks 6.21.4 through other plugins, but implicit
transitive availability is not a package dependency.

Drive download byte limit remains `GoogleSettings.max_bytes` (default 2 MB), and
text stays within `max_chars`. Check `canDownload` and metadata size before binary
reads and enforce the streamed byte cap independently. Do not fetch unknown MIME
types based only on filename extensions.

PDF parsing runs in a short-lived Python subprocess with bytes on stdin and a
bounded result on stdout, no OAuth material in arguments or results, a five-second
wall deadline, 512 MiB Linux address-space ceiling, and ten-second CPU ceiling.
Pass a minimal allowlisted environment; do not inherit bot credentials, Google
profile locations, provider keys or database access into the parsing child.
Enforce stdout limits while reading and always kill/reap on cancellation/timeout.
Parse at most 50 pages, stop at the text cap, and report pages read versus total
and truncation. Reject encrypted/malformed documents with a safe error; report
no-text/scanned pages and mixed text coverage. Extract plain text only, without
running JavaScript, opening embedded files or following links. Tests cover the
Linux resource path and safe timeout behavior on macOS without assuming that an
async timeout can interrupt synchronous parsing.

CSV uses stdlib `csv` on UTF-8/UTF-8-BOM bytes, comma delimiter, `newline=''`, and
the existing row/column/cell/text limits. Preserve quoted commas/newlines and
empty cells. Stop with explicit coverage when row/output limits are reached;
reject oversized cells, invalid bytes and malformed quotes with actionable errors.
Use a bounded parsed-table result, not executable HTML. Native Docs/Sheets and
DOCX reuse their existing parsers and limitations.

## Conversation behavior and agent recovery

Extend the itinerary app composite with trip lookup, attach/list/read/remove and
the generic reader, within its existing step budget. Keep plugin-owned behavior
instructions under 800 characters; app-specific cross-plugin behavior stays in
`apps/sb_bot/src/sb_bot/skills.py`. Update routing examples for both the original
failed phrase and later reads by trip name. No prompt or response says "saved"
before confirmation commits.

Reading is not required before an attachment preview. If the user also asks for
a summary, read the supported source and attach it with one confirmation, showing
any read limitation. Do not activate the planned trip, change its default status,
create another trip or import rows unless explicitly requested.

In `packages/core/src/chatcore/agent.py`, handle an ordinary empty response even
when there was no stripped tool markup and the turn was not forced final. Allow
one bounded continuation with currently loaded tools so an unfinished workflow
can produce its actual preview/tool call. If it remains empty, use the existing
bounded no-tools synthesis/fallback behavior. Keep deadline, global tool budget,
meta-call cap, usage accounting, confirmation early-exit and direct-write echo
rules. Retry does not rerun already executed tools or commit pending writes.
Record safe finish reason/empty-result diagnostics without logging provider raw
responses, document bodies or hidden reasoning.

## Delivery tasks and evidence required

1. Add a failing core regression: tool read, `use_skill`, ordinary empty completion,
   then continuation to a confirmation-producing tool. Also cover token-limited
   emptiness, repeated emptiness, no remaining time/budget, successful direct-write
   silence, and exactly-once execution/accounting. Apply the narrow recovery fix.
2. Add the read-only trip resolver with exact group/DM membership tests and public
   re-export. Add the additive migration, repository memory/Postgres paths,
   backfill, role uniqueness, compatibility synchronization, stale-confirmation
   and rollback tests. Verify prior ledger/trip values are identical afterward.
3. Implement metadata-only attachment previews, list/read/remove and explicit
   primary replacement. Cover authorization at both gate and handler, commit-time
   access/owner/type changes, saved-source authority, cross-chat denial, old
   unconfirmed links, unknown MIME references and folders/shortcuts rejection.
4. Add bounded PDF/CSV readers and generic dispatch. Use small synthetic documents
   without owner data; exercise malformed/encrypted/scanned/mixed PDFs, process
   timeout/output limits, CSV BOM/quotes/encoding/limits and unchanged native/DOCX
   readers. Verify unsupported references never request media.
5. Wire the app resolver and composite; update tool descriptions, tool audit,
   routing dataset, skill catalog and request snapshots where expected. Add an
   end-to-end FakePlatform/scripted-provider scenario mirroring the owner's exact
   DOCX -> existing planned trip request: one preview, no early write, one confirm,
   same trip ID, then successful later read without reposting the URL. Run the
   same scenario for native Docs, Sheets, PDF, CSV and an unsupported reference.
6. Update the operator Google guide, this plan, prior itinerary plan and plan index
   to distinguish built, merged, deployed and live-verified status. Update
   ARCHITECTURE.md with the saved-source authority decision, keeping section
   numbers stable. State dependency and permission changes, executor/reviewer
   roles, and unavailable checks in the PR. Merge/deploy is a separate action.

Expected implementation paths: Google `plugin.py`, `gates.py`, `permissions.py`,
`service.py`, `client.py`, `refs.py`, `repository.py`, new `trip_files.py`,
`file_readers.py`, `pdf_worker.py`, migration and tests; expenses `plugin.py` and
public `__init__.py`; app `app.py`, `skills.py` and routing fixtures; core
`agent.py` and agent skill/budget tests; Google `pyproject.toml` and `uv.lock`.
No applied migration or unrelated plugin refactor is required.

Required final checks from AGENTS.md:

```sh
uv sync --locked --all-packages
uv run ruff check packages services apps tests
uv run ruff format --check .
uv run pytest -q -m "not browser and not sandbox_integration"
```

Run focused failing/passing tests per task first. Record existing formatting or
local Postgres/shared-memory failures separately; do not edit unrelated files or
applied migrations to hide them. CI with Postgres must validate migrations and
compatibility. Before claiming the user workflow works, run a post-deployment
read-only check and a real chat confirmation scenario with owner authorization;
a direct parser/API check alone does not prove routing or chaining works.

## References and design review

- [Drive file metadata](https://developers.google.com/workspace/drive/api/reference/rest/v3/files): MIME, capabilities, trash state and shortcut distinction.
- [Drive downloads/exports](https://developers.google.com/workspace/drive/api/guides/manage-downloads): stored binary versus native Workspace file reads.
- [pypdf text extraction limits](https://pypdf.readthedocs.io/en/stable/user/extract-text.html): scans/OCR, layout and content-stream memory behavior.
- Related delivery: [Drive DOCX](2026-11-02-drive-docx.md) and [itinerary tools](2026-11-01-itinerary-tools.md).

Self-review: scope separates attachment from extraction; prior trip identity and
Sheet comparison behavior are preserved; saved-file authority has an explicit
confirmed origin and revocation path; unsupported formats and scans have honest
read limits; the original blank-response bug has a deterministic regression.
Owner approved implementation and merge in this chat on 2026-11-02; deployment
will use the existing automatic pipeline. No additional merge approval is needed.


## Execution evidence

- Codex executed the approved plan with parallel component work and a separate
  Codex review. Claude CLI authentication was unavailable, as recorded above.
- Empty-response regression demonstrated the failure before the fix. The real
  BotApp regression now covers the original chained DOCX request, lazy skill
  loading, one confirmation, an unchanged existing trip/expense, and a later
  saved-source read without a reposted URL.
- Format lifecycle tests cover Docs, Sheets, DOCX, text PDF, CSV and unsupported
  references through the real registry and confirmation service. Unsupported
  formats never request media. Read results remain untrusted/nonpersistent.
- Actual local Postgres tests cover additive legacy backfill, unchanged trip and
  expense sentinels, primary replacement, concurrency, rollback, legacy Sheet
  synchronization and privacy scoping.
- Independent review identified and regression-tested group opt-out during a DM
  confirmation, removal/opt-out during a download, final-scope-refresh revocation,
  and legacy DM association scoping. Fixes were applied before merge.
- Locked dependency sync and full Ruff lint pass. Changed Python files pass
  formatting; the global format command still reports 18 untouched plan records.
- Full macOS test attempt: 3,495 passed, 71 skipped, 22 deselected; 92 setup errors
  from host shared-memory exhaustion in release-control Postgres fixtures, and
  the existing local applied checksum mismatch for core 0005. Two additional
  failures exposed a routing label and reader notice mismatch, both corrected
  and rerun in the focused verification. Linux CI is required before merge.
- Merge and automatic deployment were expressly authorized by the owner. No
  Telegram messages or remote Google writes were sent during development checks.


## Verified automatic rollout

- [PR #56](https://github.com/keenlok/clawdot-platform-history/pull/56) merged as
  `bbaeef77afe17ec063f250bc7f7bd51e789b079d` after [PR CI](https://github.com/keenlok/clawdot-platform-history/actions/runs/41471949246)
  passed with **3,657 passed, 6 skipped, 22 deselected**. The [merged-main CI](https://github.com/keenlok/clawdot-platform-history/actions/runs/41472623431)
  also passed.
- The existing host timer automatically deployed release `bbaeef77afe1`. Bot and
  browser containers were healthy; the auto-deploy service completed successfully.
  No forced deployment or new Google grant was used.
- Synthetic document fixtures cover bounded DOCX/PDF/CSV extraction, confirmed association
  with an existing test trip, subsequent source resolution and untrusted/nonpersistent output.
  Actual source contents and deployment observations remain private.
- The original real BotApp/router chained-flow regression passed before merge.
  A real user retry and requester click in Telegram is still the final manual
  acceptance step; a real trip has deliberately not been attached by QA.
- Prior DOCX activation documentation from PR #55 was included in #56; GitHub
  recognized that PR as merged too. This record changes documentation only.


## Preview availability refinement (PR #58)

A real DeepSeek dry run with the deployed source and isolated memory stores showed
that the model could load an unrelated Google configuration skill and ask for
permission in prose after reading the DOCX. The handlers worked, but attachment
was not initially offered with the Google read skill. This is a tool-presentation
and instruction refinement within the approved original chained-workflow scope.

When the trip resolver is wired, the Google Workspace skill now includes its own
trip attachment/list/read/remove tools. Both Google and itinerary guidance state
that an explicit request authorizes the preview: call the tool to display the
requester's Confirm/Cancel control, without asking in prose first. Ordinary
Google reads still work when there is no trip resolver. Gates, handlers and
confirmation/authorization rules are unchanged; no new scope or dependency.

The missing-tool regression failed before the change. The updated real-model
metadata was then tested inside an isolated interpreter on the deployed image,
without changing installed modules or production stores: DeepSeek called
`list_trips`, `google_read_doc`, then `trip_attach_file` and produced one itinerary
preview for the correct existing synthetic trip. There were no production writes
or Telegram messages. The app regression still covers lazy skill loading and
empty-response recovery, while asserting that attachments are initially offered.
PR #58 includes this refinement and the rollout record above.
