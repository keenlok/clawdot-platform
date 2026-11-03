# Document follow-up retrieval implementation plan

Status: implemented and verified in [PR #60](https://github.com/keenlok/clawdot-platform-history/pull/60),
awaiting review/merge.

Date: 2026-11-02. Original base: `0a5cd47`; integrated base: `1e990a5`. The owner requested
planning and implementation after the
production D4 investigation. Codex plans, implements and self-reviews in this session; the
local Claude CLI's last final-review attempt failed because its OAuth session expired.

## Problem and design

The document was read successfully and summarized through D8. A later turn lost the live-only
document tool output, received a 2,000-character JSON history excerpt ending mid-D5, and could
not reread the Google file because its link was outside the current human message/reply chain.
The model confused incomplete history with incomplete document coverage.

Use the original logged human message as the durable, retention-controlled reference. Add an
optional `source_message_ref` to Google Doc/Sheet reads and itinerary summary. The handler
must load that message from the current chat, require the exact current topic (including the
untopiced case), reject bot messages and missing/deleted records, and match the requested
Google file ID against URLs actually present in the human message. Recheck current Google
authorization before resolving a historical reference and recheck owner identity/file metadata
on every remote read. Do not add historical URLs to `TurnState.human_urls` or permit this
argument on write tools. Existing current-message/reply-chain reads remain compatible.

Add exact-message paging to `read_chat_history`: optional `message_id` and nonnegative `offset`
read only the current chat/topic. Search mode keeps its current group/DM membership rules.
Exact pages are live-only untrusted records capped at 2,000 serialized characters, with explicit
total text length and a `next_offset` measured in original text characters. Bot messages still
require `include_bot_answers=true`. Search snippets with a stored source message expose the
same continuation metadata; index-only excerpts identify their incomplete coverage. Raw tool
bodies remain omitted from persistent conversation history. No new table, cache or dependency.

Compared with retaining whole document bodies or broadly trusting historical URLs, this keeps
reads bounded and makes the original human message the checkable authority. Shared-group
members may follow up on the same human-shared file; other chats/topics and bot/search/document
links do not acquire authority. Purging the original message removes the historical read path.

## Acceptance criteria

- A1: a later user turn can reread a human-shared DOCX through D8 without reposting its link;
  native Docs, Sheets and itinerary summary use the same explicit source-reference mechanism.
- A2: bot-authored, missing/deleted, mismatched-file, other-chat and other-topic sources fail
  before any Google API call; current opt-outs/access revocations still deny a reread.
- A3: historical reads do not authorize writes; writes still require current human provenance,
  writer permission and confirmation. Existing document output remains live-only/untrusted.
- A4: paged history reconstructs long Unicode text without gaps/loops; invalid offsets and
  out-of-scope messages fail; bot inclusion remains explicit and direct reads need no ES index.
- A5: tool guidance distinguishes history excerpt limits from source document coverage and
  explains how to recover the original human source. Existing routing and request snapshots pass.

## Ordered implementation and verification

1. Add failing tests in Google service/plugin tests for `read_doc(ctx, url,
   source_message_ref="human-source")`, scope failures, revoked access and no write escalation.
   Add a real agent sequence in Google plugin tests: human posts link, document read succeeds,
   another group member follows up without a link, and a new read returns the tail of the file.
2. Add `chatcore_google.refs` source-message validation and route the read-only service/plugin
   signatures and schemas through it. Keep write method signatures and `approved_ref` strict.
3. Add failing pagination tests in `packages/plugins/search/tests/test_history_pages.py`.
   Implement bounded record/page rendering in `chatcore_search/history.py`, then extend the
   existing history handler/schema with exact-message lookup and continuation metadata.
4. Update Google behavior guidance, operator docs, the architecture's Google provenance note,
   routing-set follow-up examples and this plan's index row. Call out this read-authorization
   change explicitly in the PR; no gate files or applied migrations should change.
5. Run targeted Google/search/agent tests and verify the red/green failures. Then run locked
   sync, workspace Ruff lint, full Ruff formatting check (record existing baseline failures),
   changed-file formatting, routing/snapshot/boundary/index checks and the required workspace
   pytest suite against fresh local Postgres.
6. Reread the complete diff against A1-A5 and security boundaries. Record review findings and
   dispositions, fix any defects, rerun affected checks, and prepare a reviewable PR.

## Results and review

- Baseline targeted Google service/plugin and search tests: 56 passed.
- Implemented A1-A5 with explicit source-message verification and exact-message history pages.
  No new storage schema, dependency or gate-module change. Historical read authorization is
  intentionally expanded to verified retained human posts; write provenance remains unchanged.
- Regression evidence: 21 initial cases failed before the APIs existed. The complete real
  agent follow-up test passes; disabling historical source handling makes that same test fail
  with the original "Post the file link" error. Raw document text stays live-only.
- Pre-integration focused suite: **228 passed, 5 deselected**.
- Pre-integration full suite: **3594 passed, 10 skipped, 22 deselected**,
  with 14 existing Telegram deprecation warnings. The Mac's IPC ID limit initially blocked
  `initdb`; bounded cleanup of verified unattached, dead-creator, user-owned 56-byte orphan
  segments allowed the clean-database runs. Browser and real sandbox tests remain excluded.
- Locked sync, lockfile validation, workspace Ruff lint and all nine changed Python files'
  formatting pass. Whole-repository formatting reports 18 existing documentation files, none
  changed by this implementation.
- Codex self-review: **PASS**. Reviewed cross-chat/topic/author/file/message-ID checks, current
  opt-out/access enforcement, lack of write escalation, byte-versus-character paging, literal
  truncation markers, raw body retention and source-message privacy deletion. No open findings.
- Claude final review is unavailable: the local CLI reports `loggedIn: false`. Delivery must
  identify this fallback rather than imply an independent Claude review.

### Integration update

PR #56 merged while this fix was being verified. Its `source_file` helper now owns remote
metadata validation and its `google_read_file` dispatch covers Drive PDF/CSV as well as
Docs/Sheets/DOCX. Integrate the historical-source option at that helper, preserve all metadata
checks and confirmed trip-file authority, and expose the option on the generic reader too.
Keep the upstream trip attachment/write paths unchanged. Renumber this fix's routing examples
to 261-262 after upstream's new rows. Rerun full verification against the combined tree.

Also integrate PR #58's preview availability/guidance so explicit trip attachment requests
continue to show Confirm/Cancel immediately. The corpus now contains 262 labelled examples;
increase its size guard to 280 while preserving its schema, privacy and distribution checks.

Final integrated focused suite: **298 passed, 11 deselected**. Final full required suite against
fresh local Postgres: **3688 passed, 10 skipped, 22 deselected**, with the same 14 Telegram
deprecation warnings. All ten changed Python files pass formatting, workspace lint passes,
and the original request snapshots remain unchanged. Both real agent reader variants recover
D8; disabling historical source resolution reproduces the original provenance error for both.
Final self-review against A1-A5 and the integrated trip flows: **PASS**, no open findings.
