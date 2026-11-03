# Google Docs Headless Browser Editing Trial Plan

> **For agentic workers:** Use the executing-plans skill after owner approval, in an isolated
> `codex/` branch/worktree. Implement the qualification trial first; production promotion is
> a separate decision. Do not implement this plan simply because its document exists.

**Status:** Planned delivery; owner authorized implementation on 2026-11-03. The isolated
worker/broker delivery depends on the API shared primitives and separate egress extraction.
Browser editing remains disabled and unqualified; local evidence and release dependencies
are recorded in the [execution handover](../handovers/2026-11-03-google-editing.md).
**Baseline:** `main` at `0a5cd47` on 2026-11-02.
**PR integration base:** `1e990a5`, including trip Drive associations, their activation and PDF/CSV readers.
**Goal:** Determine whether authenticated Playwright/headless Chrome can reliably edit a
new native Google Doc copy, verify the saved result and recover safely from interrupted jobs.
**Architecture:** Add a dedicated `services/google-browser-editor` worker, called by the
Google plugin through a private authenticated protocol. Reuse API destination, pending-action
and result handling; keep the public read-only renderer stateless and unchanged in behavior.
The worker signs in as a **fresh, dedicated editor Google account**, never the owner's account
(§3a). The broker shares each trial copy with that account and the chat's configured members
as editors.
**Tech stack:** Python 3.12+, Playwright async API, pinned Chromium new-headless build or Chrome
channel, aiohttp, SQLite job journal, existing Google REST client for broker-side verification.
Playwright/aiohttp become dependencies of the new service; SQLite is standard library.
**Authorship:** Codex drafted and self-reviewed. The installed Claude planning CLI was tried
but its expired OAuth session could not refresh. No Claude review is claimed. A separate
Codex reviewer also examined this draft during PR preparation; its API-plan finding was
corrected in the shared dependency before publication.

**Repository split reconciliation (2026-11-03):** Historical hashes and PR 59 above refer to
`clawdot-platform-history`. Implementation uses fresh public platform history. The worker,
broker and generic tests remain in platform; `apps/sb_bot` paths below translate to
`clawdot-deployments/bots/sb_bot`, and private operator files move to
`clawdot-deployments/shared/google-browser`. Bot tests run against installed candidate
wheels in deployments. Deployment pins, manifests, secrets and images remain private.
Do not run old combined-root commands as though both applications lived in platform.

## 1. Scope, alternatives and dependency

The owner requested a browser plan alongside
[API editing](2026-11-02-google-api-editing.md). API edits remain the default. A browser backend
is useful only if a bounded UI operation is reliable and offers a concrete reason to maintain it.

Three approaches were considered: API-only delivery, a bounded optional browser worker, and
a general-purpose browser agent. This plan chooses the bounded worker for qualification;
it does not expose an unrestricted signed-in browser to the model.

**Dependency:** Implement and review the API plan's destination policy, confirmed copy/import,
result journal and structured Doc snapshot first. The trial does not require all Sheets-editing
work, but those shared primitives must exist. Use the same confirmed-action authority for copy
creation and subsequent browser edits; do not ask a human to repost an intermediate copy link
within that one action. A later unrelated edit still requires human-posted provenance;
existing trip-bound read authority remains unchanged. Producing a copy does not implicitly
replace a trip's primary itinerary or add a supporting-file association.

Trial operations:

1. Case-sensitive literal replacement with exactly one supported match in one native Doc tab.
2. Bold formatting of one exact main-body text span, verified through Docs text-style data.

Both operations are supported by the API; they are deliberately simple qualification probes.
Only add a production browser operation later if it has a useful UI-specific requirement.
No Sheets UI editing, arbitrary text insertion, table restructuring, commenting, UI sharing
dialogs, upload dialogs, Drive enumeration or general navigation in this trial. The only
sharing is the broker's fixed API step in §2, applied to the trial copy it just created.
This is browser-trial-specific; it does not change the API plan's no-sharing first delivery.

**Copy-only trial:** Even editable native Docs are copied before browser edits. This limits
the trial to disposable destinations. It does not change the API plan's original-when-editable
policy. Enabling browser edits of native originals requires a later reviewed promotion plan.
DOCX always converts to a new native Google Doc, in the trial and any future browser release.

## 2. Destinations and confirmation

| Source | Trial behavior |
| --- | --- |
| Native Google Doc, editable or viewer-only | Create a native copy if account/app copying is permitted; edit that copy |
| Drive DOCX, editable or viewer-only | Download/import to a new native Google Doc if restrictions permit; edit that Doc |
| Native Sheet | Not supported by this browser trial; use API editing |
| No read/copy/download authority, ambiguous source or unsupported type | No mutation |

The normal preview identifies the backend as a browser trial, source file, **new Google Doc**
destination, copy title, selected tab, exact before/after text or bold span, verification
coverage, and the exact accounts that will receive edit access (the editor account plus the
chat's configured member emails). One requester confirmation covers the specified copy, that
sharing and the bounded edit. No preview
creates a file, navigates a writable editor, types, changes selection through edit shortcuts,
or submits a browser job. Google Docs autosaves: the first edit keystroke is a remote write.

At commit, recheck owner identity, current writer/chat/topic permissions, opt-out revision,
expiry, source version and copy restrictions. Reserve before creating the destination.
Persist its ID immediately; validate copied/imported content and resolve destination tab IDs.
If source conversion makes the exact operation ambiguous, report the unedited copy as partial.

Then, still under the owner's API credentials, share only that new copy with fixed
`permissions.create` requests: `type=user`, `role=writer`, one request per address, for the
configured editor account and the chat's configured member emails, with
`sendNotificationEmail=false` (the link is posted in chat). The copy is app-created, so the
existing `drive.file` grant covers this; no new scope. Never share the source, a folder, a
domain or `anyone`; never grant `owner`/`organizer` roles or transfer ownership. Read back
the copy's permissions and require the editor account's writer role before dispatch. Record
journal phase `shared` after the read-back. If a member grant fails, continue only if the
editor grant succeeded and report the missing members; if the editor grant fails, report the
unedited copy as partial. Sharing is not undone automatically.
Never substitute an original, another account or a different browser operation after preview.

## 3. Service and authentication boundaries

- Add a standalone service in `services/`, not a plugin or extension worker. The Google plugin
  owns user permissions and tools; the service owns browser lifecycle and its job journal.
- Do not add cookies, sessions, interaction endpoints or authenticated modes to the existing
  renderer. Its no-interaction/context-isolation tests must still pass unchanged.
- Give the new worker no OAuth/API profile, chat database credentials, bot token, SSH key,
  generic host mounts or default Chrome profile. It receives one dedicated **editor account**
  browser session (§3a) and a separate private service authentication secret. It never
  receives the owner's web session.
- The operator bootstraps a dedicated profile in a visible browser and completes sign-in/MFA
  for the editor account manually. Export only that dedicated session through a local helper;
  never copy a personal default browser profile. No password is accepted from chat or saved
  by the bot.
- Store browser state outside the repo with mode 600; mount the seed read-only. New isolated
  contexts load it per job. Temporary writable browser data lives in tmpfs. No cross-owner
  account choice or shared multi-user profiles. OAuth consent and web sign-in remain distinct.
- Before editing, inspect the signed-in account through the UI and require an exact match to
  the configured editor account email; the broker separately verified that address holds
  writer permission on this copy. A signed-in owner account is a refusal, not a match.
  Account chooser, login page, missing identity, stale session or MFA challenge returns
  `needs_login` before typing.
- Do not bypass CAPTCHA, MFA or account restrictions. Reauthentication uses the operator's
  local visible browser. Headless sign-in/session reuse is a qualification result, not an
  assumed Google guarantee. Stop the trial if the actual account cannot run reliably.

### 3a. Dedicated editor account

The owner's Google cookies are account-wide: a stolen session would expose the owner's Gmail,
Drive, sharing and account settings regardless of the worker's navigation allowlist. The
worker therefore uses a separate account whose only Drive access is the trial copies shared
with it.

- The operator creates a **fresh consumer Google account** used only by this worker. It is
  not the owner, not a group member's personal account, and not reused for any other service,
  OAuth client or sign-in. Do not grant it Cloud project, Workspace admin or OAuth roles.
- Enable 2-Step Verification with a factor the operator holds (not SMS to a shared number).
  Store recovery details with the operator's other secrets, never in the repository.
- The editor account receives access only through the broker's per-copy writer grants. Do not
  share sources, folders or shared drives with it. Periodically, and in the qualification
  record, check its "Shared with me" contains only trial copies.
- Copies stay owned by the configured owner, in the owner's My Drive; the editor account
  needs no storage quota and holds no OAuth grant. Broker verification keeps using the owner's
  API credentials.
- Compromise of the editor session exposes trial copies (and anything else wrongly shared
  with it), not the owner's account. Response: revoke its sessions, change its password and
  remove its access to the copies from the owner's Drive.
- New consumer accounts may be challenged or restricted by Google when used from a server or
  headless browser. Treat this as a qualification result; do not add evasion.

The worker has an internal network reachable only by the bot, a dedicated public egress
network, no published host port and no access to Postgres/Valkey/host/private addresses.
Reuse the renderer's guarded CONNECT proxy without making one service depend on another. As a
prerequisite PR, move `chatcore_browser_renderer/proxy.py` and the egress-policy and byte-budget
types it needs (`policy.py`, `budget.py` parts) into core as `chatcore.security.egress_proxy`,
next to `net_guard`. Follow AGENTS.md "Moving or splitting code": `git mv`, keep logger names,
re-export old names from the renderer, move test patch targets, and prove no behavior change
with the renderer's existing unit and browser tests unchanged. That move is a protected core
change for maintenance review. Both services then import the proxy from core; do not fork a
weaker network filter. Apply a separate firewall rule set to the editor's egress bridge.

Allow only the fixed Google Docs editor host for top-level navigation and the broker-provided
exact file ID/tab. Reject external links, popups, downloads, permission/share dialogs and
top-level account redirects. Restrict resource hosts to the exact set established during
qualification, checked into worker policy; never allow all `*.google.com` by default. Private
IP blocking remains a network boundary even for allowlisted hosts. Tighten page resources
without breaking actual autosave channels, which must be included in the live trial.

Disable raw page console/network logging, screenshots, video, tracing and DOM dumps by default.
Persist only opaque job ID, payload hash, destination ID, phase, timestamps and bounded error
category. Browser state and private content never enter chat diagnostics or fixture files.

## 4. Tool, broker and worker contracts

The optional `google_browser_edit_doc` tool accepts source URL, selected tab, destination title
and one of these strict operation objects. Its handler derives owner/target/authority itself:

```json
{"operation":{"kind":"replace_text","find":"Dinner at 7pm","replacement":"Dinner at 8pm"}}
```

```json
{"operation":{"kind":"bold_text","text":"Reservation confirmed","bold":true}}
```

Resolve exactly one occurrence within a supported main-body paragraph. Reject unknown keys,
raw selectors, JavaScript, CDP commands, arbitrary URLs and instructions to the browser agent.
The model receives a pending-action preview; it never controls the browser session directly.
This tool uses `writes=True`, the existing Google writer gate and plugin-owned skill guidance.
It is absent when disabled, unconfigured or outside explicitly allowed trial chat keys.

Immediately before dispatch, recheck current chat/writer/opt-out authorization and read the
new copy's current revision and bounded content. A source/copy phase does not exempt the
later browser phase from those checks. `expected_revision` below records this broker snapshot;
it is not a server-enforced revision condition on UI keystrokes.

After confirmation, API copy creation and sharing, the broker submits a fixed private request:

```http
POST /jobs
Authorization: Bearer <separate internal secret>
Content-Type: application/json
```

```json
{
  "job_id":"opaque confirmed action ID", "expires_at":"UTC deadline",
  "editor_email":"configured editor account", "destination_file_id":"new-copy-id",
  "destination_tab_id":"resolved-tab-id", "expected_revision":"broker snapshot revision",
  "operation":{"kind":"replace_text","find":"Dinner at 7pm","replacement":"Dinner at 8pm"}
}
```

Only the authenticated broker may submit jobs. It verifies the destination was created for
this reserved action and is in phase `shared`; caller-supplied ownership booleans are not
accepted. `editor_email` must equal the worker's own configured editor account. The worker builds
the fixed editor URL from a validated ID; it does not accept a navigation URL.

Limits: maximum 16 KiB job request, one operation, 2,000 characters per literal, one active job
per owner, at most one queued job and a 90-second job deadline. Reject busy/expired jobs before
editing. Health is `GET /healthz`; job status is authenticated `GET /jobs/{job_id}`. Return only
structured phase/error metadata, never session data or document text.

Persist a SQLite reservation before acknowledging job acceptance. Same ID and same hash returns
the stored status and never performs a second edit; same ID with another hash returns HTTP 409.
Do not retry POST automatically after network timeout. The broker may query status by ID.
Worker job phases: `accepted`, `running`, `awaiting_verification`, `verified`, `needs_login`,
`blocked`, `failed_before_edit`, `uncertain`. Before the first possible content mutation, commit
`running` durably. A restart marks running jobs uncertain and never resumes their keystrokes.
Accepted jobs whose in-memory operation disappeared are failed-before-edit, not reconstructed
from private persisted content. Queued jobs recheck expiry before starting.

The broker does authoritative verification through its API credentials. After worker completion,
it reads content and style using the API; `POST /jobs/{job_id}/verification` accepts only
`verified` or `uncertain`, authenticated and matching an awaiting-verification job. A successful
worker interaction alone never sets verified. Non-success states are reflected in the API
plan's scoped result journal, with any already-created destination link retained.

Authenticated `POST /jobs/{job_id}/cancel` cancels an accepted/queued job as
`failed_before_edit`. A running job receives a stop signal: stop before its first mutation
if it has not typed, otherwise close at the next safe boundary and classify as `uncertain`.
Check this signal before navigation and before each mutating interaction. Cancellation never
reverts autosaved content. The broker uses this endpoint for revoked/disabled scopes and
privacy cleanup; the status response distinguishes stopped-before-edit from possible writes.

## 5. Browser algorithm and uncertainty

1. Load dedicated session into a fresh isolated context; enforce resource and top-level policy.
2. Open the exact new destination/tab and check editor-account identity and editable state.
3. Confirm the source phrase and target tab still match the structured broker snapshot. UI
   activity cannot enforce an atomic Docs `requiredRevisionId`; copies reduce exposure but
   do not make UI editing race-free. On unexpected UI/content, stop before typing.
4. Resolve the single span using tested Docs UI search/selection/keyboard interactions.
   Keep selectors in one adapter module. Use accessible controls where available; do not use
   page-coordinate clicks or private Docs JavaScript/model internals as the trial's core path.
5. Commit journal `running` before the first edit keystroke. Execute exactly the frozen text
   replacement or bold toggle; never regenerate or broaden a match.
6. Wait for the UI's saved indication, then close/reopen the destination and check it is still
   present. The saved indicator alone is insufficient proof of the requested operation.
7. Return `awaiting_verification`; the broker reads the exact content/style through the API.
   Compare the complete bounded selected-tab text to the frozen snapshot with exactly the
   intended replacement applied, not just the presence of the new phrase. For bold, require
   unchanged text and check `textStyle.bold` on the resolved UTF-16 text range. Verify tab
   identity and refuse a snapshot whose required coverage exceeds configured limits.
8. Mark verified only after matching API read-back. A transient verification failure may retry
   bounded read-only verification; it must never retry editing. Report the destination and
   uncertainty if saved content cannot be established.

Partial text entry, browser crash, selector change after typing, unexpected dialog or expired
session during edit are uncertain outcomes. Keep the copy for inspection. Do not roll it back,
delete it, or start a fresh copy automatically. A later retry requires a newly previewed action.
Existing owner/chat opt-out checks apply before job dispatch; provide a best-effort cancellation
signal for queued jobs, while stating an already-started autosaving mutation cannot be recalled.

## 6. Files and ownership

Paths below are repository-relative. New worker package name is `chatcore-google-browser-editor`,
with module `chatcore_google_browser_editor`. New files require the usual package/layout review.

| File | Responsibility |
| --- | --- |
| `services/google-browser-editor/pyproject.toml` (new) | Service dependencies and entry point |
| `services/google-browser-editor/src/chatcore_google_browser_editor/__init__.py` (new) | Exports only |
| Same module directory: `settings.py`, `models.py`, `journal.py` (new) | Worker config, strict protocol, SQLite replay/crash states |
| Same module directory: `session.py`, `docs_ui.py`, `engine.py` (new) | Dedicated editor-account identity, bounded UI operations, lifecycle/deadlines |
| Same module directory: `server.py`, `main.py`, `bootstrap.py` (new) | Authenticated endpoints, startup, operator-only session seed helper |
| `services/google-browser-editor/tests/test_models.py`, `test_journal.py`, `test_server.py`, `test_engine.py`, `test_docs_ui.py` (new) | Protocol, authentication, state and fake-page behavior |
| `services/google-browser-editor/tests/test_browser_integration.py` (new) | Real local Chromium synthetic editor tests; no Google login in CI |
| `services/google-browser-editor/Dockerfile`, `SECCOMP.md` (new) | Pinned browser image and sandbox policy |
| `packages/plugins/google/src/chatcore_google/browser_client.py`, `browser_service.py` (new) | Private worker client, confirmed API-copy orchestration, fixed copy sharing and API verification |
| Existing Google `client.py` | Fixed `permissions.create`/`permissions.list` methods for an app-created file ID only |
| Existing Google `settings.py`, `plugin.py`, `schemas.py`, `gates.py` | Disabled-by-default trial settings/tool/gates/dispatch |
| `packages/plugins/google/tests/test_browser_client.py`, `test_browser_service.py` (new) | Frozen payloads, copy-only policy, no dispatch before confirmation, result verification |
| `apps/sb_bot/src/sb_bot/app.py`, `apps/sb_bot/tests/test_google.py`, routing fixtures | Optional injection, real confirmation and test-chat exposure |
| `apps/sb_bot/compose.google-browser.yaml` (new) | Private worker network, profile/secret mounts, tmpfs, limits and journal volume |
| `apps/sb_bot/deploy/google-browser/firewall.sh`, `sb-google-browser-firewall.service` (new) | Dedicated persistent egress rules |
| `apps/sb_bot/.env.example`, deploy docs/tests, `uv.lock`, workspace sources | Configuration, dependency/deploy integration and review |
| `docs/google-browser-editing.md` (new), `ARCHITECTURE.md`, `docs/plans/README.md` | Session operations, qualification evidence and delivery state |

Import proxy/network primitives from `chatcore.security.egress_proxy` (§3); the worker does
not depend on `chatcore-browser-renderer`. Do not add a Google cookie/profile mount to that renderer.
The new worker has its own logger name and never imports `sb_bot` or another plugin's gates.
Keep migrations/result cleanup in the Google plugin, not in the worker's SQLite schema.

## 7. Configuration and retention

Google plugin settings: `GOOGLE_BROWSER_ENABLED=false`, `GOOGLE_BROWSER_URL`,
`GOOGLE_BROWSER_TOKEN_FILE`, `GOOGLE_BROWSER_ALLOWED_CHAT_KEYS`, `GOOGLE_BROWSER_EDITOR_EMAIL`,
and `GOOGLE_BROWSER_SHARE_EMAILS` (a JSON map from allowed chat key to at most 20 member
email addresses). Defaults register no browser tool and make no worker connection. URL and
addresses are operator-configured and fixed; never model or chat input. Refuse startup if the
editor email equals `GOOGLE_OWNER_EMAIL`. The bot cannot map chat users to Google addresses,
so members are listed by the operator; a chat with no entry shares with the editor account only.

Worker settings use `GOOGLE_BROWSER_WORKER_`: editor account email, seed-state file, auth-token file,
journal path, browser channel, deadline and resource limits. The worker refuses startup without
explicit editor account and token/session paths; it never prints file contents. Start with pinned
Chromium new-headless mode; test Chrome channel only as a recorded alternate configuration.
Keep version/image digests in the deployment record rather than floating browser downloads.

SQLite journal contains no operation text and expires terminal records' metadata after 24
hours, but keeps a tombstone of `job_id` and payload hash (no content, file IDs or
timestamps beyond a coarse day) for 400 days. A job ID matching a tombstone is refused with
HTTP 409 and never runs, so worker-side replay protection does not depend on the 24-hour
window or on the broker alone. `DELETE /jobs/{job_id}` removes metadata but keeps the
tombstone. A short-lived request stores operation text in memory only. The broker's result rows follow
Google action privacy purge/retention; add authenticated `DELETE /jobs/{job_id}` for local
metadata purge, limited to terminal jobs. This deletes no Google file and removes no sharing.
In-flight jobs stop best-effort at their next safe boundary. User/chat purge does not revoke
the editor account session or its access to earlier copies; the operator revokes/removes
those separately. Document both retention systems.

## 8. Ordered implementation tasks

### Task 1: Build the protocol and durable job skeleton

- [ ] Confirm the API plan's shared prerequisites are merged/reviewed; create an isolated
  worktree and disabled service skeleton. Record dependencies and protected files explicitly.
- [ ] Land the §3 proxy move to `chatcore.security.egress_proxy` as its own behavior-neutral
  PR first; renderer unit and browser tests must pass unchanged.
- [ ] Write failing model/journal tests: unknown keys, malformed IDs, oversized operations,
  missing auth, expiry, same-ID replay, different payload with same ID, queue limits, restart
  in each phase, and retention/purge, including a replayed job ID after its 24-hour metadata
  expiry being refused by the tombstone. No browser or Google account needed.
- [ ] Implement §§4 and 7, then run the command below. Expect all cases green before committing
  the skeleton, including cancellation before and after the first possible mutation.

```sh
uv run pytest -q services/google-browser-editor/tests/test_models.py services/google-browser-editor/tests/test_journal.py services/google-browser-editor/tests/test_server.py
```

### Task 2: Add isolated session and browser lifecycle

- [ ] Write failing fake-browser tests for wrong account (including the owner's), account chooser, login redirect,
  missing session, popup/download refusal, wrong-file navigation, private-IP destinations,
  deadline, cleanup and one active context per owner.
- [ ] Implement the session bootstrap helper and fresh-context lifecycle. Authentication
  bootstrap is interactive and operator-only; CI uses synthetic storage and fake identity UI.
- [ ] Add a local HTTPS synthetic-editor integration fixture with real Chromium. Cover typing,
  simulated autosave, reopen, policy refusal and crash journal transitions without Google.
- [ ] Pass the command below, installing the approved browser build only when needed. Record
  skipped browser coverage honestly.
- [ ] Pass existing `services/browser-renderer/tests/test_engine.py` and
  `test_browser_integration.py` to prove public renderer isolation is unchanged; commit.

```sh
uv run pytest -q services/google-browser-editor/tests/test_engine.py services/google-browser-editor/tests/test_browser_integration.py
```

### Task 3: Implement only the two qualified UI operations

- [ ] Write failing adapter tests for one exact match, zero/multiple matches, tab mismatch,
  toolbar ambiguity, formatting toggle state, focus loss and unexpected dialogs.
- [ ] Implement `docs_ui.py` with one replace operation and one set-bold operation. Check the
  current bold state before toggling; do not blindly invert it. Do not use arbitrary page JS.
- [ ] Write fake tests for save indication followed by missing persisted change and partial
  text entry. Both must stay uncertain until broker verification establishes the result.
- [ ] Run the command below; commit after green.

```sh
uv run pytest -q services/google-browser-editor/tests/test_docs_ui.py services/google-browser-editor/tests/test_engine.py
```

### Task 4: Add Google broker and optional trial tool

- [ ] Write failing `test_browser_service.py` cases: no dispatch before confirmation, copy of
  editable native sources, DOCX conversion regardless of edit rights, denied copy, changed
  owner/opt-out/topic, lost-copy response, duplicate confirmation and expired queued job.
- [ ] Write failing sharing tests: exact `permissions.create` bodies (user/writer, no
  notification), only the editor account and that chat's configured members, never the source
  file, no `anyone`/domain/owner roles, editor grant required before dispatch, member grant
  failure reported, editor grant failure gives a partial result, editor email equal to owner
  refused at startup, and the preview lists every grantee.
- [ ] Add tests where UI returns success but API content/style differs; assert no verified
  result. Test UTF-16 offsets, surrounding-text preservation and unchanged source documents.
- [ ] Implement private worker client, action-bound destination authority and result mapping;
  use shared reservations and copy/import from the API plan. Add verification acknowledgment
  and metadata purge calls; never mount API credentials into the worker.
- [ ] Register the optional writer-gated tool only for configured allowed trial chats. Add
  routing/catalog/request coverage and app injection through public plugin exports.
- [ ] Pass Google browser tests, Google plugin/app confirmation tests and package-boundary,
  routing and skill-instruction checks. Commit with gate changes explicitly identified.

### Task 5: Package and review the inactive deployment

- [ ] Add pinned worker container, network/volume/firewall configuration and operator runbook.
  Test no public port, no renderer cookie mount, no database network, read-only seed and
  mode-600 secrets, non-root sandbox, queue/deadline/resource limits and clean process exit.
- [ ] Verify authorization failures return bounded categories and logs contain no cookies,
  private URLs, text, screenshots or response bodies. No arbitrary URL/JS endpoints exist.
- [ ] Run full repository checks in §10. Open a reviewable inactive PR with executor/reviewer
  roles and dependency, secret-mount, permission and deploy changes. No deployment is implied.

### Task 6: Owner-authorized live qualification

- [ ] After separate live-test authorization, create the fresh editor account (§3a), bootstrap
  its dedicated browser session and verify its exact identity in the UI and its writer grant
  via the owner's API. Confirm it can open no owner file that was not shared with it, and that
  group members received edit access to trial copies. Stop on blocked headless access; do not claim workaround
  support or modify anti-detection fingerprints to bypass Google's restrictions.
- [ ] Run 20 confirmed copy-only trials: 10 exact replacements and 10 bold operations. Include
  ASCII, CJK, emoji, multiple Doc tabs and Docs converted from synthetic DOCX. Use fixtures
  containing no real private data; verify every result by API after editor reopen.
- [ ] Run fault trials separately: login expiry before typing, challenge page, focus loss,
  browser crash after typing, worker restart, response loss, revoked writer rights, changed
  source, duplicate confirmation, wrong owner and wrong tab. All must stop or report the
  exact known partial/uncertain state without repeating an edit or touching the source.
- [ ] Require 20/20 verified normal trials and zero duplicate/source/wrong-target edits, plus
  correct fault classification, on the deployed OS/browser build. A single unexpected mutation
  fails qualification; diagnose it and repeat the complete normal/fault qualification after fixes.
- [ ] Record browser/image versions, supported operations, allowlisted resource hosts, session
  setup/expiry behavior, counts and limitations without content/credentials/private file IDs.
- [ ] Owner review chooses continued copy-only trial, removal, or a separately planned production
  promotion. Do not enable original edits or general browser access as an automatic next step.

## 9. Acceptance and rollback

- [ ] Existing public renderer remains read-only, session-free and usable independently.
- [ ] Disabled/unconfigured browser settings expose no tool and launch no worker request.
- [ ] Every trial destination is a new native Google Doc; DOCX sources are never modified.
- [ ] No job or autosaving mutation starts before the requester's exact preview is confirmed.
- [ ] Durable duplicate/restart protection works across lost responses and worker processes.
- [ ] Browser session identity must match the dedicated editor account, never the owner; login challenges require the operator.
- [ ] Each trial copy is shared as writer only with the editor account and that chat's configured members; no source or folder is shared.
- [ ] "Verified" requires broker API content/style checks after the worker's save/reopen flow.
- [ ] A failed trial leaves a known copy link and bounded outcome, with no blind retry/rollback.

Rollback: disable `GOOGLE_BROWSER_ENABLED`, stop accepting jobs, let a running job reach its
safe boundary/deadline, then stop the worker. If retiring it, revoke/remove its dedicated
browser state, sign the editor account out of all sessions and remove its access to trial
copies (or delete the account). Member access to copies is left for the owner to manage. API Google tools remain available. No automatic Google file deletion or action replay.

## 10. Checks and references

Run each command independently. Browser tests require the pinned installed browser.

```sh
uv sync --locked --all-packages
uv run ruff check packages services apps tests
uv run ruff format --check .
uv run pytest -q -m "not browser and not sandbox_integration"
uv run pytest -q -m browser services/google-browser-editor/tests services/browser-renderer/tests
git diff --check
```

Expected: changed files pass lint/format, unit and integration checks pass where prerequisites
exist, and no shared renderer behavior changes. Record preexisting/unavailable checks rather
than claiming a clean full run. Live Google qualification is separate from all mocked/local tests.

- [Playwright headless Chromium and Chrome support](https://playwright.dev/python/docs/browsers)
- [Playwright authenticated state](https://playwright.dev/python/docs/auth)
- [Dedicated browser profiles and lifecycle](https://playwright.dev/python/docs/api/class-browsertype)
- [Docs revisions and batch updates](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate)
- [Drive native conversion](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
- Existing [renderer boundaries](../rendered-fetch.md) and
  [renderer service plan](browse-renderer-service.md).

## Delivery note — 2026-11-03

Implementation follows the reviewed plan without changing its trial scope. Public API editing
prerequisites are under [PR #5](https://github.com/keenlok/clawdot-platform/pull/5), and the
behavior-preserving guarded-proxy extraction is under
[PR #6](https://github.com/keenlok/clawdot-platform/pull/6). The copy-only broker and dedicated
browser worker have local implementation/test evidence; their browser PR is forthcoming.

Final evidence includes 229 worker/renderer tests and 304 broker/plugin/boundary/index tests.
The frozen public suite passed 3,566 tests (two Elasticsearch skips, 26 deselections). The final
installed candidate passed 401 downstream app tests (seven environment skips), including all
12 real SBot confirmation cases for verified/uncertain outcomes and replay protection. These
checks use synthetic or mocked data; they do not establish successful Google sign-in, live UI
stability, autosave behavior or resource-host allowlist sufficiency.

The bold operation requires the exact unique Find result to be announced as selected text and the
visible document editor to retain focus. If either accessibility signal is unavailable or drifts,
the worker refuses before typing or reports an uncertain outcome. API verification also requires
an explicit target bold value and preserves the presence/value of bold and other style fields
outside that UTF-16 range; inherited target bold values fail closed. The supported live UI has not
been qualified and may refuse this operation. The browser tool and worker remain disabled by
default; no dedicated account was provisioned and no live Google write was performed. Owner
authorization for the 20-trial qualification remains a separate gate.
