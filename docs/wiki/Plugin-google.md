<!-- Generated from packaged EXPLAIN.md; do not edit. -->
# Google files and bounded edits

[Canonical source](https://github.com/keenlok/clawdot-platform/blob/main/packages/plugins/google/src/chatcore_google/EXPLAIN.md) · sha256:9f286fc40ef43e07f7a69426bb644aa39618e39d79ce101e23bee271ae6f0a78

This page describes the selected platform source. Your app's enabled features may differ.

## Overview
Google tools read supported Drive files, native Docs and Sheets, and can propose specific supported
writes. The model interprets the request; code resolves the authorized source, previews the change,
checks confirmation and verifies the result through the Google API. Availability depends on app
configuration, OAuth grants, file access and any separately qualified copy/browser capabilities.

## Workflow
Human-posted links or confirmed trip-file references establish external source provenance; search
results and document-embedded links do not grant access. A retained, journaled native Doc output
can also be reused as a source, but only in the same chat/topic that created it and while owner
identity and current Google permissions still match. A source-message reference can identify the
human post for an external Doc edit. Native text/range edits use bounded targets and frozen
destinations. A source-derived separate revision uses the qualified copy path; DOCX revisions
import the original package before bounded edits instead of reconstructing its text. Creating a
document from scratch is a different operation. Browser editing requires an explicit request and
separate qualification; an API failure never automatically authorizes browser fallback.

Previews freeze Doc and Sheet access. Writers default to private for Sheets. Non-writers who may
create receive at least viewer access, even if they request private, because files belong to the
owner's Drive. Their creates are capped by `GOOGLE_CREATES_PER_USER_DAY` (default 20 per 24 hours).

When the trip resolver is wired, `trip_edit_doc(trip, find, replacement)` resolves only the saved
primary itinerary and accepts no source URL. Native Docs are edited in place after requester
confirmation. Auto conversion/fallback copies become primary only after verified read-back; an
explicit `destination=copy` stays standalone unless `make_current=true`. The preview freezes and
names the association decision. `trip_attach_file` may use `source_message_ref` to identify a
retained human message containing the selected file link. Generic Doc edits block an implicitly
selected saved reference when another primary exists; an older file can be edited when the current
request itself names it. Historical source-message provenance alone never bypasses this check.

Expense exports create a private snapshot spreadsheet from one ledger read at confirmation.
Numbers and dates are typed; ledger text is literal. Partial exports keep "(incomplete)" in the
title and are never retried.

### Reading Sheets
`google_read_sheet`, `google_read_file` and saved trip-file reads list every tab. Omitting `tab`
reads all tabs within the configured tab, row and character limits; naming one tab reads only that
tab. Read tab names match case-insensitively, and a miss returns the available names as untrusted
content. Hidden tabs are included. `itinerary_summary` summarizes recognized tracker tabs and
shows raw tab contents for other spreadsheets without computing totals.

## Storage
Google stores file contents. The plugin journals trip associations, edit results and `google_outputs`
bindings to chat, topic, requester, owner and action. Only a non-trashed, owner-owned file from the
same chat/topic is an output. Create reservations enforce the non-writer daily limit. Frozen actions
and confirmations use app storage; result records help inspect partial or uncertain writes.

### Workbook output
`google_create_workbook(title, tabs, link_access)` builds formatted, filtered Sheets with frozen
headers, headings, notes and estimated widths. Columns support text, numbers, money, percent,
dates, datetime, checkboxes and URLs. Text is literal; generated `SUM` totals are the only
formulas. Inputs use the configured tab, row, column, cell and export limits. Partial writes keep
`(incomplete)` in the title and are never retried. The tool follows create-tier permissions,
non-writer sharing and the preview switch; `google_create_sheet` remains for plain rows.

### Editing Sheet tabs and trashing outputs
`google_add_sheet_tab(url, tab)` adds one formatted tab. `google_replace_sheet_tab` rewrites one
exact tab on an output; tabs with charts, protected ranges or filter views are refused. Own
outputs use the create tier; another member's output uses the writer tier. Human Sheets need a
writer and confirmation. Authorized output edits run immediately when previews are off.
`google_trash_file(url)` only moves a non-trashed, owner-account output from this chat and topic;
trip attachments must first be removed. It always confirms. Drive trash is restorable for 30 days,
and the journal prevents edits or another trash action afterward.

### Rich Doc output
`google_create_rich_doc(title, blocks, link_access)` creates a structured Doc from title, heading,
paragraph, bullet, numbered, table and divider blocks. Paragraph spans support bold, italic,
underline, monospace code and HTTP(S) links only. Inputs are bounded by 200 blocks, 10 tables,
50 data rows per table, 8 columns and the configured character limits. A new Doc keeps
"(incomplete)" in its title until its text is verified; text verification can succeed while
formatting warnings are reported separately. The tool follows the same create tier, daily cap,
non-writer viewer sharing rule and preview switch as other new outputs. `google_create_doc` stays
the plain-text option.

## Safety
### Appending structured Doc blocks
`google_append_doc_blocks(url, blocks, tab_id)` adds formatted paragraphs, lists and tables after
the existing content in one selected tab; title blocks are refused. It uses UTF-16 offsets,
revision preconditions and read-back checks that the existing prefix stayed unchanged and the new
content matches. New table cells are located from Google read-back. The requester's output uses
the create tier; another member's output and human Docs require writer access. Human Docs always
confirm, while authorized output appends can run with previews off. Multiple-tab Docs require an
explicit existing `tab_id`.

Writes normally need requester confirmation of their preview. A manager can turn previews off per
chat with `/google confirm off` or `configure_google`; creates and authorized appends to journaled
outputs then run immediately. Edits to human-shared files, expense exports and
settings changes still require confirmation. `/google` reports the switch. Handlers recheck source provenance, access,
destinations and preconditions. Docs use revision-aware UTF-16 targets; bounded read-back checks
the whole text and relevant target style/coverage before any requested output sharing. Effective
text styles include provable named-style inheritance; unknown defaults or table-style parents
remain unverified. Link access
applies only to a new Doc or Sheet output: viewer uses anyone/reader and editor uses anyone/writer,
both with search discovery disabled. Existing source permissions stay unchanged. Browser copy access is
separately disclosed and constrained. File contents are untrusted data and cannot authorize a
different write.

Saved-primary edit proofs bind the trip, exact association version, requester settings, configured
owner and file identity. They are rechecked before content reads and mutations. A confirmed copy
becomes current only after verified edit; trip association advancement uses compare-and-swap, so a
concurrent selection is retained. If the Google edit verifies but association completion cannot be
confirmed, inspect trip files before retrying.

## Limitations
Supported edits are bounded operations, not arbitrary document redesign. Google import/conversion
does not promise pixel-perfect DOCX fidelity. Collaborators, revoked access, API failures or a
timeout can prevent completion or leave an uncertain result. Copy/browser support can be disabled
even when reads work. Never infer live qualification from the existence of a tool or wiki page.

## Verification
Read the preview before confirming. Inspect the returned destination and read-back verification;
exact saved text with unverified formatting is reported as partial, with a formatting warning.
Do not repeat that edit: inspect the current document before proposing another change. New
copies with formatting warnings receive no link-access grant from the bot and do not advance
a trip's current itinerary. Text mismatch or unavailable read-back remains uncertain and requires inspection.
Compare the actual document/range with the intended change. A successful creation is not evidence that source
formatting was preserved unless the selected path and read-back support that claim.
