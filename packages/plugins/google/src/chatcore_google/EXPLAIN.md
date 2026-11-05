+++
topic = "google"
title = "Google files and bounded edits"
summary = "Read authorized files and confirm bounded API edits with preconditions and verification."
aliases = ["google_docs", "google_sheets", "drive", "google_workspace"]
+++

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
New native Doc outputs can be private, link viewers, or link editors when the confirmed preview
freezes that choice. The framework default is private; an explicit tool choice overrides it for
that action. Original Docs retain their permissions; Sheets and browser trials use their existing
policies.

When the trip resolver is wired, `trip_edit_doc(trip, find, replacement)` resolves only the saved
primary itinerary and accepts no source URL. Native Docs are edited in place after requester
confirmation. Auto conversion/fallback copies become primary only after verified read-back; an
explicit `destination=copy` stays standalone unless `make_current=true`. The preview freezes and
names the association decision. `trip_attach_file` may use `source_message_ref` to identify a
retained human message containing the selected file link. Generic Doc edits block an implicitly
selected saved reference when another primary exists; an older file can be edited when the current
request itself names it. Historical source-message provenance alone never bypasses this check.

## Storage
Original file contents and saved changes live in Google services, not chat memory. The plugin stores
trip-file associations and edit-result journal records in its configured repository. Frozen action
payloads and pending confirmations use application storage. Result records can include destination
IDs and partial or uncertain outcomes, so a later check can inspect the known destination instead
of blindly creating another copy. This is not a backup of every Google file or full revision history.

## Safety
Writes need requester confirmation of their preview. Handlers recheck source provenance, access,
destinations and preconditions. Docs use revision-aware UTF-16 targets; bounded read-back checks
the whole text and relevant target style/coverage before any requested output sharing. Link access
applies only to a new Doc output: viewer uses anyone/reader and editor uses anyone/writer, both
with search discovery disabled. Existing source permissions stay unchanged. Browser copy access is
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
for partial or uncertain results, inspect the journaled destination before retrying. Compare the
actual document/range with the intended change. A successful creation is not evidence that source
formatting was preserved unless the selected path and read-back support that claim.
