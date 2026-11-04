# Google output continuation, verification and access levels

Status: Proposed design; implementation awaits owner review and the default-access choice.

## Goal and observed evidence

Fix follow-up reads/edits of bot-created native Docs, make post-edit verification resilient
to a stale read without weakening its checks, and support confirmed link-editor output Docs.

Production checks found two independent failures. The bot rejected its own new destination
because only a human-posted URL granted source authority. Reposting the URL produced a
confirmed original edit that completed and verified, with owner/app edit access intact.
The initial DOCX-import action had frozen link-viewer access but stopped at content
verification before reaching its sharing step. The owner subsequently changed access manually.
Neither that manual permission change nor the original source may be rewritten by this work.
Private file IDs, text, people and operation records remain outside the public repository.

The generic readback_mismatch record does not retain the native pre-edit snapshot or the
failing comparison. Later read-only checks found the intended replacement and an editable
Doc; this does not prove whether the immediate failure was stale text, style or an unavailable
read. Do not invent a historical root cause or mark an uncertain action verified retroactively.

## Recommended design

### 1. Authorize retained, journaled output Docs

Keep human-link authority for arbitrary external files. Add a second narrow authority source
for a native Doc destination recorded by a confirmed Google action in this same chat/topic.
The trusted record must bind the action, configured Google owner, kind and new destination;
a copied/imported destination must differ from its source. Do not trust URLs merely because
they appeared in a bot message, history excerpt, document content or model output.

A known created destination may qualify even if its later content verification is partial or
uncertain. That permits a new explicitly requested follow-up, never a replay of the old action.
Reserved/unknown destinations, original-source results, browser trials, other chats/topics,
changed owners and arbitrary IDs do not qualify. Retention/purge removes this authority along
with its existing records; a retained human link remains the normal fallback.

Reuse the durable result/action records and pending-action topic binding. Recheck normal
membership, opt-out, writer, owner, metadata and live edit permissions; authorization to a
known file does not bypass a fresh concrete write preview. Preserve legacy result compatibility:
API edits did not consistently set the browser destination_creation_claimed flag, so that flag
alone must not be mistaken for the sole proof of a recorded created destination.

Both native-Doc reading and API edit proposal paths use this resolver. A wrong or unnecessary
source_message_ref does not defeat independently proven output authority. External-file source
references remain strictly human and same-topic. Add the existing source_message_ref option to
API edit tools if needed to avoid the observed read-then-edit unknown-argument dead end, with
its human message checked at proposal time and its source binding frozen/rechecked at commit.
Sheet and browser authority do not expand as a side effect.

Journal all future create_doc outputs, including explicit private choices, using existing
migration 0004 support. Keep old pending actions' access intent and replay protection intact.
No applied migration changes. A new migration is unnecessary unless implementation evidence
shows a missing durable binding; any such need requires an additive reviewed migration.

### 2. Verify reads robustly and report the actual stage

First reproduce the existing failure under deterministic stale-then-current GET responses and
inspect the exact-style failure paths. Use a single verification helper in both edit-success
and uncertain-write-response branches. Retry only document GET/read-back, at most three reads
within a small aggregate deadline; never repeat create/copy/import, batchUpdate or permission
POST calls. Read-only delays are bounded and test-injected, not added to every successful edit.

Every accepted read must still prove exact expected whole-tab text, complete target UTF-16
coverage and unchanged uniform replacement style. Do not strip whitespace, ignore styling,
accept truncated content or equate an existing replacement somewhere with whole-document
verification. A genuine text/style mismatch remains uncertain and blocks automatic sharing.

Record non-content failure categories (text, style, snapshot/read failure, exhausted deadline)
and bounded attempt counts without document bodies, credentials or raw API error responses.
Report that the destination exists when its ID is known; distinguish creation, edit and sharing
outcomes. If verification fails, explicitly say no sharing change was attempted and provide
the known safe URL. Do not claim current ACLs are private without inspecting them, and do not
make the assistant describe a known created destination as an unknown failed creation.

### 3. Freeze private/viewer/editor access in the preview

Add one explicit new-output access enum: private, viewer or editor. New native Docs from
create_doc, copies and DOCX imports can select it. The application default is chosen below;
any explicit per-document choice overrides it. Previews state anyone-with-link view/edit
clearly, with search discovery disabled. An editable native format does not by itself mean
public writer access; model guidance distinguishes those requests.

Keep existing link_viewer callers compatible: true means viewer, false means private. Old
pending payloads freeze their old boolean choice; absent access fields remain private.
A later default flip can never promote a previously approved private/viewer preview to editor.
Reject conflicting old/new tool arguments. The public framework remains private by default.

The fixed sharing helper can grant only anyone/reader or anyone/writer with
allowFileDiscovery=false. There is no general recipient, arbitrary permission body or URL
endpoint. Grant only after content verification to the confirmed, owned, app-authorized new
Doc. Recheck local authorization before POST, durably journal the single attempt, read back
the exact intended role and discovery setting, and verify all previous ACL entries retained.

A matching preexisting link grant is a zero-mutation success. An unexpected different or
broader public grant stops safely; do not silently downgrade, upgrade or rewrite a manual
permission change. Original-document edits never change ACLs. An explicit sharing choice
for an original requests a separate-copy preview instead. Existing manual editor grants,
source files, Sheets and browser trials keep their policies.

### Default-access alternatives (owner choice)

1. Recommended: retain link-viewer by default; grant link-editor when explicitly requested.
   This preserves today's default and supports collaborative copies on request.
2. Select link-editor by default for future bot-created output Docs. Anyone obtaining a link
   can modify the file; every new-output preview discloses that choice. Private/viewer choices
   remain available. This applies only to future outputs, not existing files or old previews.

Both alternatives include the two fixes and the three per-document access choices. Keep the
current manually edited output and its ACL intact. No email recipients or notifications are added.

## Validation and delivery

Use synthetic/local fixtures first; no extra live Google file or permission mutation is
needed or authorized by this design. Tests cover same-topic retained output success; cross-chat,
wrong owner, fabricated/bot-history source denial; known uncertain destination follow-up;
private create journaling; human source references; stale read then exact success; permanent
text/style/read failures; one mutation only; viewer/editor/private defaults and overrides;
legacy payloads after flips; unexpected ACLs and exact ACL delta; original/Sheet/browser isolation;
concurrency/replay and PostgreSQL retention/constraints. Preserve snapshot/catalog/routing tests,
800-character plugin instructions, package boundaries and existing migration checksums.

Implementation maps to the Google plugin's refs/repository/service/edit_service/client/settings,
link-sharing helpers and corresponding tests. Keep domain checks out of generic core. Update
capability docs, packaged explainer and generated wiki, architecture decision, changelog and
plan index. Treat trusted-output authority and writer access as explicit permission changes in
the PR even if no gates.py or permissions.py edit is required. No new third-party dependency.

Execute in the existing isolated worktree, with a new branch from current public main.
Claude CLI is installed but its fresh authentication probe reports loggedIn=false; use the
AGENTS.md unavailable-model fallback: Codex plans, Luna executes, Sol independently reviews.
The owner reviews this design before implementation. A detailed execution plan follows approval.

Run locked sync, whole-tree lint, changed-file formatting, full non-browser/non-sandbox tests
with disposable Postgres, Gitleaks, scaffold/container and installed-wheel checks. Release a
reviewed compatible minor version, then pin that exact immutable tag and commit downstream.
Fetch latest private main and preserve approved Mandarin/Japanese tutor flags, current secrets,
renderer assets and all unrelated configuration. Check all migration hashes against the release.
Run downstream full checks and anonymous image builds, independent maintenance review, PR/main CI,
then the installed health-gated auto-deploy. Verify effective access default, exact installed
provenance, migration state, healthy services and rollback artifacts. Attach runtime receipts to
PRs without a documentation-only follow-up deployment. Do not replay the earlier uncertain edit
or modify the existing output manually as a qualification shortcut.
