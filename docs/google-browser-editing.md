# Google Docs browser editor worker

This is an inactive qualification worker for two bounded operations on a new native Google Doc
copy: one exact literal replacement and one exact bold toggle. The Google API broker and the
separate owner-authorized live qualification are prerequisites to any real job. The worker does
not accept browser URLs, scripts, selectors or credentials in requests, and it is not a general
browser service.

The existing public renderer remains read-only and session-free. This worker uses its own fresh
Chromium context per job, a separate editor account session, an authenticated private protocol,
and the core guarded CONNECT proxy. Production navigation is fixed to the broker-provided Docs
file ID and tab ID. A checked-in list of exact resource hosts blocks all other CONNECT tunnels;
the current list still requires live qualification before this worker can be considered ready.

## Dedicated editor account and session seed

Use a fresh consumer Google account that is separate from the owner and group members. Give it
Drive access only to trial copies shared by the broker. Do not grant it OAuth scopes, Cloud or
Workspace roles, or unrelated service access. Store its recovery information outside the repo.

Bootstrap its session on an operator workstation with a visible browser:

```sh
google-browser-editor-bootstrap \
  --editor-email editor@example.com \
  --output /run/google-browser/seed-state.json
```

Complete Google sign-in and MFA manually in the visible browser, then return to the terminal.
The helper verifies the account in the UI and exports Playwright storage state. It accepts no
password and never prints session data. The output must be outside the repository and has mode
0600. Mount it into the worker read-only as the configured `STATE_PATH`; do not mount a personal
browser profile or a default Chrome directory. A stale session or identity mismatch stops a job
before typing. Reauthenticate by running the visible helper again and replacing the dedicated
seed file through the operator's secret-management process.

## Broker settings and confirmation

These settings belong to the bot-side Google plugin. Their defaults expose no browser tool and
make no worker connection. Do not put live values or member addresses in the public repository.
The bot-side token file is separate from the worker's bearer-token file and must be mode 0400 or
0600, owned by the bot process. The URL must resolve only to the worker's private service address.

| Setting | Purpose |
| --- | --- |
| `GOOGLE_BROWSER_ENABLED=false` | Explicitly enable the optional browser trial after qualification |
| `GOOGLE_BROWSER_URL` | Fixed private worker origin; never model or chat input |
| `GOOGLE_BROWSER_TOKEN_FILE` | Bot-side mode-0400/0600 worker bearer-token path |
| `GOOGLE_BROWSER_ALLOWED_CHAT_KEYS` | Comma-separated chat keys permitted to see the trial tool |
| `GOOGLE_BROWSER_EDITOR_EMAIL` | Exact dedicated editor identity; startup refuses the owner address |
| `GOOGLE_BROWSER_SHARE_EMAILS` | JSON map from allowed chat key to at most 20 member email addresses |
| `GOOGLE_COPY_QUALIFIED=false` | Separate owner-grant qualification gate required before any copy/import |

Use a throwaway qualification chat and a dedicated editor account. A chat without a share-map
entry grants writer access only to the editor account. Do not infer member addresses from chat
profiles. The preview lists the exact new Doc title, selected tab, operation and all intended
writer grantees. No copy, permission change, browser navigation or job starts before the
requester's confirmation. API editing remains the default; the browser trial is available only
on explicit request and never as an automatic fallback after an API failure.

After confirmation, the broker rechecks the owner identity, action authority, source/copy
preconditions and trial recipients. It creates or imports a new native Doc, validates the
bounded selected-tab snapshot, grants writer access only on that copy, and reads permissions back.
The editor's writer role must be verified before worker dispatch. If a member grant is missing,
the editor grant may proceed and the missing member access is reported; if the editor grant cannot
be verified, the copy is returned as partial and no browser job is sent. The source and parent
folder are never shared. The API checks the complete selected tab after save: replacement must
preserve the surrounding text, while bold must change only the requested UTF-16 range and preserve
all other observed styles. A worker UI success signal alone is never verification.

## Qualification gate

No live Google qualification is recorded for this implementation. Before enabling the tool,
authorize and run the separate 20-trial copy-only qualification on the deployment's actual OS and
browser build: 10 literal replacements and 10 bold changes. Include ASCII, CJK, emoji, multiple
Docs tabs and DOCX imports. Verify every result after editor reopen through the API. Also run the
plan's login-expiry, challenge, focus-loss, crash/restart, response-loss, revoked-permission,
changed-source, duplicate-confirmation, wrong-owner and wrong-tab fault cases. Require 20/20
verified normal trials and zero source, duplicate or wrong-target edits. Any unexpected mutation
fails qualification and requires the complete normal/fault suite again.

The bold operation has a strict accessibility contract: the unique Find target must be exposed
as selected text and the focused control must be the accessible Docs editor. If the live UI cannot
provide those signals, the worker refuses before typing; it does not fall back to coordinates or
arbitrary page scripting. This operation may remain unavailable pending qualification. Synthetic
browser and rebuilt-wheel tests do not count as live trials. No editor account was provisioned and
no live Google writes were run for the current delivery.

## Worker configuration

The service reads only paths and non-secret settings from environment. The bearer secret itself
is read from `SECRET_FILE` at startup and is never copied into the worker environment or logs.

| Setting | Purpose |
| --- | --- |
| `GOOGLE_BROWSER_WORKER_BIND` | IP and port, usually `0.0.0.0:8080` on the private container network |
| `GOOGLE_BROWSER_WORKER_EDITOR_EMAIL` | Lowercase dedicated editor account email |
| `GOOGLE_BROWSER_WORKER_OWNER_EMAIL` | Lowercase owner address; startup refuses a matching editor account |
| `GOOGLE_BROWSER_WORKER_STATE_PATH` | Read-only mode-0400 or mode-0600 Playwright state file |
| `GOOGLE_BROWSER_WORKER_SECRET_FILE` | Read-only mode-0400 or mode-0600 bearer-token file |
| `GOOGLE_BROWSER_WORKER_DB_PATH` | Writable SQLite journal path on the worker's private volume |
| `GOOGLE_BROWSER_WORKER_JOB_TIMEOUT_SECONDS` | Job deadline, from 1 to 90 seconds |
| `GOOGLE_BROWSER_WORKER_TERMINAL_RETENTION_HOURS` | Terminal metadata retention, default 24 hours |
| `GOOGLE_BROWSER_WORKER_TOMBSTONE_RETENTION_DAYS` | Content-free replay tombstone retention, default 400 days |
| `GOOGLE_BROWSER_WORKER_CHROMIUM_CHANNEL` | `chromium` by default; `chrome` is an unqualified alternate |
| `GOOGLE_BROWSER_WORKER_CHROMIUM_EXECUTABLE_PATH` | Optional pinned executable path for controlled qualification |

Use the pinned Playwright v1.64.1 image, run as `pwuser` (UID/GID 1001), and keep Chromium's
sandbox enabled. Give the process no database credentials, bot token, OAuth profile, host mounts,
public port or private-network route. The egress firewall must deny UDP and private destinations;
the browser also disables QUIC and non-proxied WebRTC UDP. Mount only the seed and secret files
read-only and the journal volume writable. The service image includes its matching seccomp
profile in [SECCOMP.md](../services/google-browser-editor/SECCOMP.md).

## Private protocol and outcomes

Only the authenticated bot broker should reach the worker's private network. The unauthenticated
`GET /healthz` endpoint returns only `{"status":"ok"}`. `POST /jobs`, `GET /jobs/{id}`,
`POST /jobs/{id}/cancel`, `POST /jobs/{id}/verification`, and `DELETE /jobs/{id}` require the
separate bearer token. The request body is limited to 16 KiB; it contains one strict operation,
the fixed destination ID/tab, editor account and expiry. An accepted job ID is reserved durably
before the HTTP response.

Job phases are `accepted`, `running`, `awaiting_verification`, `verified`, `needs_login`,
`blocked`, `failed_before_edit` and `uncertain`. The worker never marks a job verified from a UI
signal: the broker must read the saved Doc through its Google API credentials and acknowledge
`verified` or `uncertain`. An interrupted running job is uncertain after restart and is never
replayed. A second POST with the same ID and payload returns stored status; a different payload,
or a retained tombstone, returns conflict.

SQLite stores only the opaque job ID, payload hash, destination file ID, phase, timestamps and a
bounded error category. It never stores operation text. Terminal metadata expires after 24
hours; deleting it leaves a tombstone containing only job ID, payload hash and coarse day for
400 days. `DELETE` on an in-flight job requests cancellation; retry the metadata deletion after
the job reaches a terminal state. It does not delete a Google file or revoke sharing. The owner
must manage previous copies and the editor account's access separately.

No page console or network bodies, screenshots, video, traces, DOM dumps, cookies or session
state are logged. Keep the HTTP access logger disabled. Treat an `uncertain` result as a possible
autosave; inspect the destination copy and use a new previewed broker action for any later edit.

## Qualification boundary

The local synthetic HTTPS browser tests exercise the guarded proxy, account identity checks,
navigation restrictions, accessible controls, save/reopen flow and journal boundary without
Google credentials. They do not establish that Google Docs' live UI, the exact resource-host
list, session reuse or autosave channels work in a headless deployment. Do not enable a broker
tool or run live jobs until the separate owner-authorized qualification has completed and its
supported host list and limitations have been recorded.

Bold formatting requires a bounded accessibility contract: the exact unique Find match must be
announced as the current selected text, and the focused control must be the visible accessible
document editor. The worker checks that contract again after the durable pre-edit journal guard.
If the live Docs UI does not expose both signals, the request ends as `selection_unavailable`
before the edit; if either signal drifts after the guard, it ends `uncertain` without toggling.
Live qualification must verify this behavior in the dedicated account UI before any broker
can expose the operation.
