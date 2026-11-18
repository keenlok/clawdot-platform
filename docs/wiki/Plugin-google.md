<!-- Generated from packaged EXPLAIN.md; do not edit. -->
# Google files and bounded edits

[Canonical source](https://github.com/keenlok/clawdot-platform/blob/main/packages/plugins/google/src/chatcore_google/EXPLAIN.md) · sha256:3ae7fb85f58431fc707a14f52ab14fd0ac8bdc5df82ad7f64178e1449f8142f9

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

An expense export creates one new private spreadsheet after confirmation. When the app supplies a
workbook provider, code builds every tab from one consistent ledger snapshot read at confirmation:
summary balances, transactions, one tab per participant, suggested and recorded repayments, and the
raw records with stable IDs. Numbers and dates are typed cells; ledger text is always literal text.
The file is a snapshot and is never synced. If writing stops part-way, the reply links the file and
its title keeps "(incomplete)"; nothing is retried automatically.

## Storage
Original file contents and saved changes live in Google services, not chat memory. The plugin stores
trip-file associations, edit-result records and a `google_outputs` journal binding created files to
their chat, topic, requester, owner and action. Only an owner-owned, non-trashed file in the same
chat/topic counts as an output here. Separate create reservations, taken atomically at
confirmation, count each non-writer's daily creates. Both are removed by retention purges. Frozen action
payloads and pending confirmations use application storage. Result records can include destination
IDs and partial or uncertain outcomes, so a later check can inspect the known destination instead
of blindly creating another copy. This is not a backup of every Google file or full revision history.

## Safety
Writes normally need requester confirmation of their preview. A manager can turn previews off per
chat with `/google confirm off` or `configure_google`; creates and appends to a requester's own
journaled output then run immediately. Edits to human-shared files, expense exports and
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
