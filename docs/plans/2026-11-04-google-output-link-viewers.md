# Link-viewer access for new output Docs

Status: Live limited once v0.4.0 is published; the framework default remains private. The owner requested both an existing generated output and future output
copies to be viewable by anyone with the link. This explicitly extends the earlier owner-only
API-output policy; it does not authorize sharing source files or enabling browser trials.

## Approved behavior

New native Google Doc outputs may use an explicit, frozen link-viewer setting. The public
framework defaults to private; SBot will select link-viewer by default. Add an optional
`link_viewer` boolean to google_create_doc and google_edit_doc: omitted uses the application
default for new Docs, false preserves a private output, true requests anyone/reader with
allowFileDiscovery=false. The confirmation preview states that access before any create,
copy, conversion, edit or sharing mutation. Missing fields in older pending actions mean
private even after the deployment default changes. The sharing choice is frozen, rather
than recomputed from the default at commit time.

Original Doc edits never change sharing. An explicit link_viewer=true request that would
edit an original must request a separate copy instead of publishing the original. Sheet
creation/copies, appends, trip associations and authenticated browser trials retain their
existing policies. New files are not shared by email and receive no public writer grant.
The source ACL is never copied or changed. Google may impose account/domain restrictions;
a refusal must be reported, not bypassed.

Sharing happens only after the new output's content is verified. The grant is exactly
type=anyone, role=reader, allowFileDiscovery=false, and read-back must verify those fields.
Only the newly created destination ID bound to the confirmed action can be shared; validate
that it is owned/app-authorized and shareable by the configured owner. Existing ACL entries
must remain intact. Unexpected broader ACLs are not silently rewritten.

## Implementation plan

1. Add GoogleSettings.new_docs_link_viewer=false and optional tool arguments. Resolve the
   default at preview time, freeze the boolean in pending payloads, and put the sharing line
   near the beginning of both create/copy previews (including attached long previews).
   Preserve provenance, writer/chat/topic checks and the 800-character skill instruction cap.
2. Add a fixed GoogleClient link-viewer permission endpoint and include allowFileDiscovery in
   permission read-back. It must validate IDs, use the existing no-retry Google transport,
   never accept arbitrary permission bodies/recipients, and never grant writer access.
3. Add an internal sharing helper to validate the confirmed destination, preflight/read back
   its ACL and finalize the grant. Durable result state must identify a sharing attempt before
   the POST. On refusal or uncertainty, retain the destination URL and accurate partial/uncertain
   status; do not claim success or retry a possible mutation. The existing action reservation
   prevents concurrent and repeated commits.
4. Integrate sharing into both successful API Doc edit/read-back branches, only for new
   native copies and DOCX imports. Honor explicit private choices and legacy private payloads.
   Leave original Doc and Sheet writes and browser broker behavior unchanged.
5. For link-viewer google_create_doc outputs, reserve and journal a durable destination before
   content or permission mutation, verify exact created text, then share. Reuse the result
   journal with an additive 0004 migration that admits kind=create_doc with no source_id;
   retain NOT NULL semantics for all existing edit kinds using an explicit check constraint.
   Do not change an applied SQL file. Keep the existing private creation path compatible.
6. Update Google capability documentation/skill guidance and CHANGELOG.md. Add an architecture
   decision that the explicit new-output sharing preview supersedes the earlier owner-only
   output policy, while originals and browser-trial permissions keep their prior boundaries.
7. Test before implementation: fixed reader/non-discovery request and read-back; default and
   explicit private choices; old pending payloads after a default flip; source/original/Sheet/
   browser non-sharing; ownership/ACL refusal; content verification before grant; both edit
   success paths; create/edit destination journaling; sharing failure/timeout/read-back mismatch;
   replay and concurrency; additive SQL constraints and retention. Never test with real files
   except the separately authorized existing-output operation.
8. Run locked sync, lint, changed-file formatting (record existing full-tree exceptions), full
   non-browser/non-sandbox tests with disposable Postgres, routing/catalog/snapshot/boundary/
   migration checks, Gitleaks, scaffold and installed-wheel checks. Obtain independent review.
9. Publish a reviewed release, update downstream exact sources/lock/config/migration manifest,
   enable only the new output-Doc default, run downstream checks and CI, then verify the
   installed health-gated rollout. Preserve concurrent work and current runtime feature flags.

## Scope and release coordination

The base is current platform main, including the independently merged tutor work. This change
must not activate tutor audio, browser editing or any other unrelated feature. Fetch current
downstream main before the final pin so concurrent approved changes are preserved. No new
third-party dependency is needed. The new migration is additive and must remain compatible
with rollback to an older application. Runtime evidence belongs in the private delivery record.

The owner separately authorized updating the identified existing output. That bounded operation
has been completed using the actual installed owner grant: link-viewer read-back passed and
content/previous permissions were unchanged. Its ID and operation journal remain private;
no source or qualification fixture was shared.

Execution roles: Claude CLI is installed but its authentication probe failed with expired
OAuth. Under AGENTS.md's unavailable-model fallback, Codex executes and an independent Codex
reviews; no Claude review is claimed. The requested sharing behavior above is the approved
scope; new-file writes still require the normal concrete in-bot confirmation.

## Validation record

New client/preview regressions failed before implementation. Google and disposable-Postgres
checks passed 299 tests; catalog, wiki, boundaries, plan index, tool audit, request snapshots
and explainers passed 307 checks. The complete platform suite passed 3,714 tests, three
environment skips and 26 browser/sandbox deselections, with 14 Telegram deprecation warnings.
Skips were unavailable Elasticsearch and FFmpeg. Whole-tree lint, 12 changed Python files'
formatting, lock/diff checks and Gitleaks passed. Full-tree formatting retains the same 14
unchanged Markdown exceptions. Installed scaffold wheels passed the outside-checkout smoke;
the scaffold container built and its network-disabled read-only console smoke passed.

Independent review resolved two failure-reporting findings: malformed/oversized successful
POST responses remain uncertain, and journal failures retain known safe destination URLs.
Eighty focused checks and a final 51-test ACL-delta recheck passed. No runtime blocker remains.
Candidate first-party wheels are tested against the private app before release. No additional
Google file was created or shared for these tests; the explicitly authorized existing-output
grant is recorded privately. The published tag, downstream exact pin, additive migration and
health-gated rollout will be recorded on the linked delivery PRs.

PR CI caught the stale generated Google wiki mirror. Regeneration changed only that mirror;
the exact CI generator check then passed. Runtime source and reviewed wheel bytes are unchanged.
