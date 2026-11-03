# Google Docs and Sheets with CSV input (`packages/plugins/google`, `chatcore_google`)

Status: **Live for native Docs/Sheets and CSV input.** PR #47 included the revised plans from
#39 and #40 alongside implementation. Owner OAuth was configured and moved to production on
2026-11-02; identity and native Doc/Sheet create/append/read checks passed. Drive DOCX is a
separate [live extension](2026-11-02-drive-docx.md) shipped in PR #54 after explicit expanded consent and deployed verification.
See [execution record](2026-11-02-google-itinerary-implementation.md)
and [operator guide](../docs/google-workspace.md). Decisions and remaining Google setup are in §10.

Read `ARCHITECTURE.md` §3d, §6a, `plans/link-allowlist.md` (URL provenance), `plans/skill-layer.md`,
and these files, which define the patterns this plugin follows:

- `packages/plugins/web/src/chatcore_web/plugin.py` (`seen_urls` check, untrusted-content wrapping,
  `safe_get`)
- `packages/plugins/expenses/src/chatcore_expenses/export.py` and `plugin.py` (the export tool)
- `packages/core/src/chatcore/confirm.py`, `pending.py` (confirm-before-write)
- `packages/core/src/chatcore/security/net_guard.py`, `injection.py`

## Goal

Let people in an allowed chat give the bot a Google Doc or Sheet link and ask about it, and let
the bot write back in a small, confirmed way:

- Read a Doc (text) and a Sheet (a range or tab).
- Read a human-shared CSV attachment as tabular input, using the existing attachment path.
- Create a Doc or Sheet and append to it.
- First real use case: export an expense trip or ledger to a Sheet
  (`plugin-expenses` already builds the rows in `export.py`).

Out of scope for v1: editing or deleting existing content, Slides, Drive browsing or search,
per-chat-user Google accounts, comments, sharing changes, Word writes, Excel extraction and authenticated
Drive CSV downloads. CSV attachments do not need Google credentials.

## 1. Authentication: one owner-authorized account

Owner decision (2026-11-02): act on the owner's behalf first. New Docs and Sheets belong to
the owner's Google account. Service accounts cannot own files; sharing a folder with one
does not provide storage quota. See [Google's ownership guidance](https://developers.google.com/workspace/drive/api/guides/about-shareddrives).

- Default setup: one owner-authorized OAuth account, not a separate account for each chat
  member. An operator-only local setup command uses the desktop authorization-code flow with
  PKCE, state validation, a loopback callback and offline access. It verifies the signed-in
  account against the configured owner using Drive `about.get(fields=user)` before saving
  credentials. Bind the verified Drive user identity to the credential profile. No public
  callback server or token table is required. See [Google's desktop OAuth flow](https://developers.google.com/identity/protocols/oauth2/native-app).
- Settings (`settings.py`, `BaseSettings`): `GOOGLE_OWNER_EMAIL` and
  `GOOGLE_OAUTH_CREDENTIALS_FILE` pointing to a mode-600 file containing the client configuration
  and refresh token. Secrets stay on the operator's machine or deployment box, never in code,
  tests, logs or chat. Add these settings to the §6a secrets/configuration tables.
- Refresh access tokens at `https://oauth2.googleapis.com/token`; cache them in memory until
  shortly before expiry. Revoked credentials disable access and require owner reauthorization.
  Calls use plain `httpx` REST; declare any additional dependency in the implementation PR.
- Deferred alternative (not implemented): domain-wide service-account delegation requires a Workspace super
  administrator has authorized the required scopes. Pin JWT `sub` to the configured owner;
  chat users and tool arguments cannot select it. Do not assume a personal Gmail account can
  use delegation. Choose this setup only after verifying domain support, as described in
  [Google's delegation instructions](https://developers.google.com/identity/protocols/oauth2/service-account#delegatingauthority).
- Scopes: `documents`, `spreadsheets`, `drive.file`, and `drive.metadata.readonly` for the type
  lookup in §3. Not the full `drive` scope.
- The plugin is inert (no tools offered) when owner credentials are unset. Existing files must
  be accessible to the owner, and every access must also pass §2 provenance and §6 chat gates;
  the owner's Google permissions alone never authorize a bot request.

## 2. Recognising links and IDs (`refs.py`)

One pure function, `parse_google_ref(text) -> GoogleRef | None`, with `kind`
(`document`, `spreadsheets`, `drive_file`), `file_id`, and optional `gid` (Sheet tab).

Accepted forms:

- `docs.google.com/(document|spreadsheets)/d/<ID>/...`, including `/u/N/` account prefixes
- `drive.google.com/file/d/<ID>/view` and `drive.google.com/open?id=<ID>`
- `?tab=` and `#gid=` suffixes
- bare IDs are not accepted by v1 tools; require a human-posted link

ID characters are `[A-Za-z0-9_-]`, so a trailing `-` is valid (the example link
`.../d/1GWyj1XMHbwXRHwFtS5mcxudU4KkrsoF-/edit` ends in one).

Authenticated Google access requires **human-posted provenance**, checked before even the Drive
metadata lookup. `ctx.turn.seen_urls` is insufficient: `search_web` adds search-result URLs to
that set, and those results must never authorize access using the owner's credentials.

- Add request-only `TurnState.human_urls` as an immutable URL collection, seeded by core from
  the triggering human message and its same-chat/topic human reply chain, with the existing
  hop and reset boundaries. Core collects URLs without Google-specific parsing. Ambient
  messages, bot replies, stored model turns, tool output, search results, document contents
  and recalled memories cannot populate this collection; `search_web` still updates only
  `seen_urls`.
- Canonicalize approved links to file IDs with `parse_google_ref`; reject lookalike hosts and
  embedded credentials. Derive approved file IDs from `human_urls` inside the Google plugin.
  Both reads and append targets must be members of that derived set. The model
  cannot grant provenance by supplying an ID or a different spelling of a URL.
- A confirmed create action may populate a new file internally as part of that action. Its
  returned URL is not automatically authorized for later reads/appends; ask a human to post
  or quote the link. Confirmation payloads bind the validated target and exact write data so
  a callback does not reconstruct authority from a fresh turn's URL set.

## 3. Check the real file type first (Drive lookup)

A `/document/d/` URL does not guarantee a native Google Doc. A **Word (.docx) file stored in
Drive and opened in the Docs editor has the same URL shape** (its ID is typically 33 characters
where native IDs are typically 44; this is an observation, not a rule the code may rely on).
The Docs API rejects such files.

Every read therefore starts with `GET drive/v3/files/<ID>?fields=id,name,mimeType,size,modifiedTime`
and branches on `mimeType`:

| `mimeType` | Handling |
| --- | --- |
| `application/vnd.google-apps.document` | Docs API `documents.get`, flatten to text |
| `application/vnd.google-apps.spreadsheet` | Sheets API `values.get` |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` (.docx) | Read-only Drive DOCX text/table extraction under the [DOCX extension](2026-11-02-drive-docx.md); requires renewed Drive content consent; no Word writes |
| `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` (.xlsx) | Explain that Excel extraction is deferred; ask for a native Google Sheet or a CSV attachment |
| `text/csv` | Ask the requester to attach the CSV in chat for v1; do not download it using owner credentials |
| anything else | Refuse with the type named |

Errors map to plain messages: file-access 404 or 403 → "I can't open that. It must be shared
with the configured owner account as Viewer (Editor for writes)"; authentication, quota and
API-configuration failures get distinct messages; size over cap → refuse with the size.

### CSV input (selected for v1)

Start with CSV **attachments posted by a human**, read through `read_attachment` and its
existing current-chat attachment-reference and download checks. Use Python's built-in `csv`
module for tabular parsing, with byte, row, column and field-size caps. Support UTF-8 (including
a BOM), quoted commas and embedded newlines; report malformed or unsupported-encoding input
plainly. Preserve headers and empty cells and label truncated output. Treat cells as untrusted
data and never evaluate formulas; apply the normal formula-neutralization rules if imported
rows are later written to Sheets. CSV needs no parser dependency; the DOCX extension declares `defusedxml` separately.

CSV is an import format alongside native Docs/Sheets reads; the live itinerary remains a Google
spreadsheet. A CSV file has no workbook tabs. Drive-hosted CSV downloads are deferred: the
current metadata scope and `drive.file` grant do not promise content access to an arbitrary
owner-accessible file. Ask for an attachment instead of broadening Drive scopes for v1.
CSV parsing stays in `plugin-search`, which owns `read_attachment` and its tests/settings;
the Google/itinerary composite reuses that tool. General attachment reading remains available
independently of Google credentials or the Google per-chat toggle. Importing the parsed rows
into Google Sheets still requires the Google gates and confirmation.

## 4. Tools

| Tool | Does | Writes? |
| --- | --- | --- |
| `google_read_doc(url)` | Name, type, text (capped, truncated with a notice) | No |
| `google_read_sheet(url, range?, tab?)` | Values for a range or the `gid` tab, capped rows and columns | No |
| `read_attachment(message_id?, index?)` (existing tool) | CSV attachment input, parsed into capped tabular data with the built-in `csv` module | No |
| `google_create_doc(title, body)` | New Doc owned by the configured owner account; return its URL | Yes, confirm |
| `google_create_sheet(title, rows)` | New Sheet owned by the configured owner account; return its URL | Yes, confirm |
| `google_append(url, text_or_rows)` | Append to the end of a Doc or to a Sheet tab | Yes, confirm |
| `expenses_export_to_sheet(...)` | Expenses export written to a new Sheet | Yes, confirm |

Rules for every tool:

- Argument descriptions are filled in, per `AGENTS.md`.
- Write tools go through `ConfirmService` (`request` → button or reply code). The confirmation
  shows the owner account, file name, target and a short preview. No automatic sharing changes;
  the returned URL does not grant chat members access. The owner shares files manually.
- Row, cell and character caps are in `settings.py`. Sheets formulas in written values are
  neutralised (prefix with `'` when a value starts with `=`, `+`, `-`, `@`) so chat content
  can't plant formulas.
- The handler checks permission at call time even when the gate already hid the tool. The
  pending-action commit re-checks current requester/chat permissions and owner-account identity;
  an account change invalidates the action rather than retargeting it.

## 5. Untrusted content

Doc, Sheet and CSV text is written by other people. It is wrapped in the same untrusted-content
markers as fetched web pages, run through `security/injection.py` for logging, and never used
as an instruction. The system prompt addition for the skill says so in one line.

## 6. Access and gates

- `gates.py`: `build_gates(plugin)` returns gates only. Read tools are offered where the plugin is
  enabled; write tools are offered only to admins and users on an explicit list (a setting, e.g.
  `GOOGLE_WRITE_USER_IDS`). Treat `gates.py` and `permissions.py` as a permission change in the PR.
- Owner decision (2026-11-02): once owner credentials are configured, the plugin is **enabled
  by default in every allowed chat**, with a per-chat opt-out using the existing feature-setting
  permission pattern. An absent setting means enabled; an explicit opt-out disables both reads
  and writes in that chat. Gates and handlers enforce it before authenticated lookups, and
  pending-action commits re-check it so opting out also prevents an already-previewed write.
  This does not bypass the chat allowlist or the separate write-user restrictions.
- `/google` shows the configured owner account and authorization status to admins only, so
  they can coordinate file access. Chat membership never selects the Google identity.

## 7. Network

- The authenticated client's fixed host allowlist contains `oauth2.googleapis.com`,
  `docs.googleapis.com`, `sheets.googleapis.com` and `www.googleapis.com`, scoped to this
  plugin rather than to `fetch_web_page`. Authenticated API calls use HTTPS on port
  443 only and do not follow redirects. `safe_get` currently has no authenticated POST/write
  interface or host allowlist: implement the fixed-host authenticated client explicitly,
  reusing the public-address checks without changing ordinary web-fetch policy.
- Logs carry file ID prefix, tool name, status and latency only: no titles, no content, no
  tokens (§6a).

## 8. Package layout, tests and rollout

Layout follows `AGENTS.md`: `__init__.py` (docstring, re-exports, `__all__`), `plugin.py`,
`gates.py`, `settings.py`, `permissions.py`, `refs.py`, `client.py` (token + REST),
`service.py` (type branching, caps, sanitising), `render.py` (Doc and Sheet to text). The
additive `0001_google.sql` stores per-chat settings, durable write reservations and explicit
itinerary/expense trip links; it stores no copied file contents.

Skill: declared in `plugin.py` `skills()` with plugin-owned `Skill.instructions`, following the
current `AGENTS.md`. Cross-plugin composites and their instructions live in
`apps/sb_bot/src/sb_bot/skills.py`. Expense export is routed by an app
composite, which groups registered tools rather than implementing handlers. Its executable
handler and pending-action commit live in `plugin-google`, receiving an app-injected expense
export provider through a narrow protocol. The app resolves the authorized ledger with the
expenses plugin's normal group/DM rules. Neither plugin imports the other's `skills` or
`gates`. Update `routing_set.jsonl` and the request snapshot where the tests require.

Tests (no network; `httpx.MockTransport`):

- `parse_google_ref` for every form in §2, including a trailing `-` and `/u/0/`.
- Type branching in §3, including a .docx mime type and 403/404 messages.
- CSV attachment parsing: UTF-8/BOM, quoted commas/newlines, empty cells, malformed rows,
  unsupported encodings, byte/row/column/field caps and truncation notices. Reject forged or
  cross-chat attachment references; Drive CSV links return an attachment hint without a media
  download. CSV cell instructions stay untrusted and formula-like values are never evaluated.
- Provenance: accept current human/reply-chain Google links and aliases for the same ID; refuse
  search-only links, bot/model output, ambient links, bare IDs, forged hosts and cross-chat/topic
  chains before any HTTP call. Confirm create-then-populate works without authorizing later reads.
- Write tools do nothing until confirmed; formula neutralisation; caps.
- Default-on access in allowed chats with configured credentials, explicit per-chat opt-out
  before any HTTP request, rejection of pending writes after opt-out, and inert behavior when
  credentials are unset. Other chats' settings and the chat allowlist remain independent.
- Owner OAuth refresh/cache/expiry/revocation, wrong-account rejection and account changes between
  preview and commit; a secrets-in-logs test in the style of §7 of `ARCHITECTURE.md`.
- A mutation check on patch targets, per `AGENTS.md` "Moving or splitting code" rule 2, if any
  code is moved.

Release-control: this touches `pyproject.toml`, `uv.lock`, `gates.py`, `permissions.py` and
`__init__.py` files, so it goes through maintenance review, not the bot-generated PR path.

Live rollout remains staged: (1) read tools, one test chat; (2) create and append behind the
write list; (3) expenses export. Implementation is delivered together with the companion
itinerary plan in one PR, with staged verification recorded in the execution plan.

Execution: the initial design was drafted in a cloud Claude session. This implementation was
executed and self-reviewed inline by Codex, following the current repository layout and skill
ownership. No separate Claude review was available in this session. Checks that need Postgres or a
real Google project are named in the PR if they can't run.

## 9. Later phases

High-level trip tools (itinerary tracker, Sheet/Doc/CSV import, suggester) are specified in
[2026-11-01-itinerary-tools.md](2026-11-01-itinerary-tools.md) and implemented in the same PR. There is
one spreadsheet per trip, with day/city tabs following the user's prompt. Workflows remain app
composites in `apps/sb_bot/src/sb_bot/skills.py`; executable handler/gate ownership and the
new-tab/append service operations are specified in that plan. Route ordering and travel times
are deferred until a routing data source is chosen.

## 10. Owner decisions and remaining setup

1. **D1 Auth — decided:** one owner-authorized account for v1 (§1). Default to owner OAuth;
   domain-wide delegation is a deferred alternative requiring verified Workspace admin setup.
2. **D2 Write access — implementation default:** bot admins, plus any explicit namespaced IDs
   in `GOOGLE_WRITE_USER_IDS`; every write still requires confirmation.
3. **D3 Created files — decided:** owned by the owner's account. Sharing stays manual in v1.
4. **D4 Input formats — decided:** native Docs/Sheets and human-shared CSV attachments first.
   Drive DOCX reads are an approved extension with separate read consent and a declared XML
   dependency; Word writes, Excel extraction and Drive CSV downloads remain deferred (§3).
5. **D5 Google Cloud setup:** owner email, a project with Docs, Sheets and Drive APIs enabled,
   an OAuth client/consent configuration and the owner's offline authorization. Verify that
   the app's publishing status supports unattended refresh; do not assume a temporary testing
   grant lasts indefinitely. Domain-wide delegation instead requires the verified §1 setup.
   Nothing here can be checked end to end without these operator-provided credentials.
6. **D6 Chat scope — decided:** enabled by default in allowed chats once credentials are
   configured, with per-chat opt-out; no opt-in setup is required (§6).
