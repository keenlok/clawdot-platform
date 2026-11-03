# Google files and trip itineraries

Google tools are optional. With no `GOOGLE_OWNER_EMAIL` and
`GOOGLE_OAUTH_CREDENTIALS_FILE`, no Google or itinerary tools are registered. CSV attachment
reading continues to work through `read_attachment` without Google credentials.

Once configured, Google tools default on in allowed chats. A group manager can request
`/google off` or `/google on` and confirm the change. An opt-out also invalidates earlier
pending writes in that chat. Other chats are unaffected. `/google` shows account/status to
bot admins. Bot admins can write; `GOOGLE_WRITE_USER_IDS` can add explicit namespaced writers.

## Authorize the owner's account locally

1. Enable the Google Drive, Docs and Sheets APIs in the owner's Cloud project. Configure an
   OAuth consent screen and a **Desktop app** OAuth client. Download that client's JSON file
   to a private location outside the repository. Ensure the publishing/test-user configuration
   permits the intended offline access; external apps left in Testing can have short-lived
   refresh grants.
2. On the owner's computer, run:

   ```sh
   uv sync --locked --all-packages
   uv run chatcore-google-auth --client-file /private/path/client.json \
     --owner-email owner@example.com --output /private/path/google-owner.json
   ```

   The system browser opens Google's consent flow. A temporary callback binds only to
   `127.0.0.1`; PKCE and state are checked. The program verifies Google's reported email and
   Drive user identity before atomically writing a mode-600 profile. It never prints tokens
   or sends credentials to chat. The local callback closes after completion or three minutes.
3. For a local console run, set the owner email and the absolute credential profile path in
   the environment. For the deployed container, install the profile in its private host
   directory, owned by the container UID/GID, using the operator's normal secure file-copy path:

   ```sh
   sudo install -d -m 700 -o 10001 -g 10001 /srv/example-bot/google-oauth
   sudo install -m 600 -o 10001 -g 10001 /private/staging/google-owner.json \
     /srv/example-bot/google-oauth/owner.json
   ```

   In the host's private `.env`, set:

   ```dotenv
   GOOGLE_OWNER_EMAIL=owner@example.com
   GOOGLE_OAUTH_CREDENTIALS_FILE=/run/google-oauth/owner.json
   GOOGLE_OAUTH_HOST_DIRECTORY=/srv/example-bot/google-oauth
   ```

   Compose mounts that directory read-only; an empty directory is harmless while Google is
   unconfigured. Runtime refresh tokens stay in the profile and access tokens are cached only
   in memory. Never commit the client/profile files, copy them into the image, or paste them
   into chat. Account changes invalidate pending writes. Reauthorize after grant revocation.

This implements one owner OAuth account. Domain-wide service-account delegation and separate
accounts per chat member are not implemented. New files belong to the owner and sharing stays
manual; posting a created URL does not give chat members Google permissions. The owner's
Google permissions apply to file access: a human-posted link in an enabled allowed chat can
request a read of a file accessible to that account.

New authorization requests include `drive.readonly` for Drive-hosted DOCX content, alongside
Docs, Sheets, `drive.file` and `drive.metadata.readonly`. This grants broad read/download access
at the Google layer; the bot requires human-posted links or exact confirmed saved trip
associations, with enabled, allowed chats. It
does not add a Drive search/list tool. Google classifies `drive.readonly` as a restricted scope;
production publishing is separate from Google verification requirements.

Existing profiles continue to work for native Docs/Sheets without reauthorization. For DOCX,
the owner must explicitly approve the additional read permission through the local authorization
command above, then securely replace the private profile. A metadata-only or per-file grant can
return `appNotAuthorizedToFile` even when metadata is visible. Check sharing and download
restrictions as well; a 403 does not always mean the scope is missing. Update the public privacy
disclosure before requesting the new grant. Never change a stored scope list to pretend that
Google granted access. Coordinate profile replacement with deployment operations; the client
notices the changed private profile fingerprint without requiring a bot restart.

## Reads and confirmed writes

Post a Google Doc/Sheet or Drive-hosted DOCX URL in the current message or its same-chat/topic reply chain.
For a later read, `google_read_file`, `google_read_doc`, `google_read_sheet` and `itinerary_summary` accept
`source_message_ref`: the original human post's message ID. The handler loads the full retained
message, checks its exact chat/topic and human author, matches the Google file ID, and rechecks
current Google access and remote identity/metadata. Another member of the same group can ask
about that human-shared file. Missing/purged messages, bot answers, other chats/topics and
mismatched links cannot authorize a read. No raw document body or new source-reference table is
stored. The original message follows normal chat retention and privacy deletion.

An arbitrary URL in ambient history, search/research results or document contents still does
not authorize access: the actual human source message must be verified. This does not change
write authority: current-human-link checks, confirmed saved-trip authority, writer permissions
and requester confirmation still apply to the operations that require them. A historical read
does not add that link to the turn's write authority. A newly created file
needs its link posted by a human before reuse. Google links are matched by validated file ID,
so normal Docs/Sheets/Drive URL variants for the same posted file work.

`read_chat_history` search results carry excerpt/continuation metadata. For a clipped stored
message, call it with `message_id` and `offset=next_offset`, omitting search filters; bot messages
require `include_bot_answers=true`. Exact pages are restricted to the current chat/topic and do
not require the search index. The 2,000-character limit applies to each serialized history
record, including escaped Unicode and attribution; `offset` counts original text characters.
Index-only excerpts with no stored message ID cannot be paged. A clipped history answer is
not evidence that the source document itself ends there: recover its original human reference
and reread the source for document questions. Document reads keep their configured size limits.

Doc reads include nested tabs; Sheet reads accept a specific tab/`gid` and a bounded range
such as `A1:H100`. Type checks use Drive metadata, including DOCX files that open at a
`docs.google.com/document/...` URL. Drive-hosted DOCX supports main-body paragraph/table text
only, within the configured compressed byte and output character limits. Download capability is
checked before fetching; actual streaming bytes, ZIP expansion, XML size/depth/node counts and
compression ratio are bounded. DTDs, entities, external resources, encryption, macros and
unsupported packages are rejected. The parser uses the explicitly declared `defusedxml` dependency.

DOCX output states coverage and truncation. Images/drawings, headers/footers, comments, footnotes,
directly hidden/deleted text and visual layout are omitted; style inheritance is not interpreted.
Google tab/heading fragments do not select a DOCX section. The original Word file is unchanged;
Confirmed DOCX edit-copy conversion is described below; Word write-back, legacy `.doc`,
`.docm`, uploaded DOCX and Excel remain unsupported.
`google_read_file` also reads bounded Drive-hosted UTF-8 CSV. CSV itinerary imports use
chat attachments: UTF-8/BOM, quoted commas/newlines and empty cells are supported, with
byte/row/column/field limits and explicit truncation/errors.

Every Google write shows a preview and waits for the requester's click/reply code. File text,
cell text and imported/researched material remain untrusted. Creation/appends retain their
formula-prefix neutralization. Content edits write the exact previewed literal with typed
`stringValue`, without adding an apostrophe. Commits re-check current chat access, writer permissions,
opt-out revision, owner identity and target tabs. The plugin has no arbitrary batch-update,
file search, sharing, deletion or booking tool.

Remote mutations are reserved durably before the first write. A timeout or process failure
can leave a created file or a completed append with an uncertain result. Inspect the owner's
Drive before submitting another write; the same action is not automatically retried. A file
creation followed by population can leave an empty or partially populated new file if Google
fails between those requests. The plugin does not delete it automatically.

## Confirmed content edits

`google_edit_doc` previews one case-sensitive literal replacement in a selected native Doc
tab. It requires one supported main-body paragraph or table-cell match. Multiple tabs need
an explicit tab ID. Headers, footers, mixed styles, structural spans and pending suggestions
that make a match unsafe are refused. The snapshot uses `SUGGESTIONS_INLINE`; the confirmed
atomic delete/insert uses exact UTF-16 ranges, explicit tab IDs and `requiredRevisionId`.
A changed revision requires a fresh preview. Replacement styling is restored explicitly,
and API read-back verifies the bounded result. No tab-wide replacement is issued.

`google_edit_sheet` previews a stable numeric sheet ID, exact tab/range, typed before-values
and literal after-values. The range must be rectangular, within configured row/column/content
limits, and at most `GOOGLE_MAX_EDIT_CELLS` (default and maximum 100). Merged, spilled,
protected and formula cells are refused. `null` explicitly clears a cell. Strings beginning
with `=`, `+`, `-` or `@` stay exact strings; this tool does not author formulas. A type change
is visible in the preview. Only `userEnteredValue` is written, preserving unrelated formatting,
notes and validation.

Sheets has no equivalent to the Docs revision condition. Typed before-values are reread
before the update, but a collaborator can still write between that read and the mutation.
Read-back cannot eliminate this race. The preview states this limitation; request a copy
when isolation from collaborators is needed. There is no automatic overwrite or rollback.

Editable native Docs/Sheets use the original unless the requester explicitly chooses a copy.
This uses the installed full Docs/Sheets scopes plus current effective file permissions;
it does not require a per-file Drive grant. A copy selected in a preview remains a copy, and
lost original access never silently switches the confirmed destination.

Copy fallback and DOCX conversion are disabled by default with `GOOGLE_COPY_QUALIFIED=false`.
Before an operator enables them, separately authorize owner-grant qualification for app-created,
externally created editable and viewer-only native sources whose owners permit copying.
Native Drive `files.copy` uses per-file `drive.file` authorization; metadata visibility and
account copy capability alone do not establish that grant. Missing per-file authority returns
`needs_file_authorization`. Do not widen OAuth scope or retry a write to discover permission.
DOCX download instead uses the existing `drive.readonly` grant, followed by a new app-created
native upload; source `isAppAuthorized` is not required for that read/import path.

A DOCX edit always downloads and validates the original package, freezes its SHA-256 and
binary revision, then imports those original bytes into a **new native Google Doc** after
confirmation. It never reconstructs a document from extracted text or modifies the Word file.
The imported first-tab mapping is stated in the preview. If conversion changes the supported
match, the unedited copy is retained and reported as partial. Native copies explicitly target
the owner's My Drive root and do not reproduce source-folder sharing. Requests use
`ignoreDefaultVisibility=true`; organization policy can still affect effective visibility.
API edit copies are not automatically shared with chat members.

The preview freezes the owner, source, destination mode/title, tab/range and exact changes.
Confirmation rechecks writer/chat/requester/topic binding, opt-out revision and content-specific
preconditions. Drive `version` is not a blanket conflict condition: harmless metadata changes
do not substitute for content checks. A created copy does not replace or attach a trip itinerary.
Action-bound internal reads of the new destination do not grant general link authority;
later unrelated edits still need a human-posted link.

Google migration `0003_edit_results.sql` adds a result row tied to the durable action reservation.
Reservation and initial result are transactional; an action cannot execute twice while retained.
Known destination IDs are stored immediately. Outcomes distinguish verified coverage, conflict
requiring a fresh preview, partial copies and uncertain application. Uncertain creation can have
no known ID; inspect Drive before requesting a new action. No uncertain mutation is retried.

Age-based cleanup retains unresolved reservations/results (`reserved`, `destination_created`,
`shared`, `applying`, `partial`, `uncertain`) for at least
`GOOGLE_EDIT_UNRESOLVED_RETENTION_DAYS` (default 90), even if the normal action cutoff is earlier.
The existing normal retention cutoff still governs resolved actions. Scoped privacy deletion
removes these records immediately, including source/destination metadata; privacy takes priority
over copy traceability. Neither cleanup deletes remote files or revokes sharing.

## Save a trip itinerary or supporting files

Existing trips keep their IDs, members, dates, expenses, balances and status. With the trips
plugin enabled, use an explicit existing trip name or ID:

- "Set this DOCX as our Sample & Example trip itinerary."
- "Attach this booking PDF to our Example trip as a reference."
- "Read our Example trip itinerary" or "List our Example trip files."
- "Remove that file from the trip" removes only the association, after confirmation.

A trip has one primary itinerary and multiple references. Setting another primary preserves
the old primary as a reference and names that change in the preview. An explicit attachment
request produces the Confirm/Cancel preview directly, without a separate prose permission step.
Nothing is saved before
the requester confirms. Attaching does not convert the source, activate a trip, change the
default trip, or record an expense. An already linked file cannot silently move to another
trip: explicitly remove its old association first.

`trip_attach_file`, `trip_list_files`, `trip_read_file` and `trip_remove_file` implement this
workflow. `google_read_file` reads a newly posted link. Native Docs, Sheets, Drive DOCX,
text PDFs and UTF-8 CSV are readable. Other ordinary Drive files are attachable as metadata
references, with an explicit unsupported-reader notice. Folders and shortcuts need the actual
target file link. Reading PDF/CSV uses the owner's already-granted `drive.readonly` scope;
no extra scope is introduced here.

PDF extraction is selectable text only (50-page maximum, five-second parser deadline, bounded
input/output and process resources). Scans without a text layer need OCR; mixed/partial reads
report their coverage. Encrypted PDFs are not decrypted. CSV accepts UTF-8/BOM and quoted
commas/newlines, preserves empty cells/formulas as text, and applies row/column/cell limits.
File content stays untrusted and is omitted from persistent tool history; generated replies
may still quote it. Unknown formats are not downloaded.

A confirmed association authorizes later reads of exactly that saved file within the same
trip/group. Every read rechecks group membership, Google opt-out, configured owner, current
Drive access and association state. This does not grant Drive search or access to links found
inside documents. Removal revokes the saved authority; the Drive file remains unchanged.
DM requests resolve the group and honor its settings. Legacy Sheet links become references;
reconfirm them before using the new saved-source reader because old links did not pin the
owner identity. Existing posted-link Sheet summaries still work.

## Optional spreadsheet tracker

Use a trip's existing posted spreadsheet when it exists; otherwise create one trip file.
Choose day, city or explicitly named day/city groups in the prompt. Each tracker tab has:

- Row 1: `chatcore-itinerary`, `1`.
- Row 2: `Day`, `Time`, `Place`, `Type`, `Cost`, `Booking ref`, `Status`, `Notes`.
- Data from row 3. Day uses `YYYY-MM-DD` or `Day N`; times use local `HH:MM-HH:MM`; costs use an
  explicit currency such as `JPY 1200` or `12.50 SGD`. Type is Stay/Food/Sight/Transit/Other;
  Status is Proposed/Confirmed/Done/Dropped.

Examples: “Import this CSV into our Japan spreadsheet, one tab per city”, “Import this Drive DOCX
itinerary, one tab per city”, “Summarize every day
in this itinerary”, or “Research food stops and suggest rows for Kyoto”. The main model reads
the source/research, normalizes rows with unknown fields left blank, and passes named groups
to a deterministic proposal handler. Imports always create new tabs or a new trip file;
suggestions append Proposed rows or create new tabs. Their source URLs must have come from
the current human/search/research inputs; hours/prices are explicitly marked unverified.
Every suggestion batch requires confirmation.

DOCX imports also recheck source download access when the requester confirms. Imported rows
stay frozen from the preview; rechecking access does not silently regenerate them. No raw DOCX
bytes are stored in the pending action. Read-tool content is omitted from persistent tool
history, but generated chat replies and write previews may retain document-derived text.

Itinerary imports and suggestions never overwrite existing cells. Explicit bounded edits use
the separate confirmation flow above. New tabs are initialized atomically with
their header/data; existing tabs use server-side appends. Human edits between a suggestion
preview and commit force a fresh preview. The edit tool can change an explicitly selected
Status/value range; row deletion and reorganization remain manual. The bot refuses unknown
tracker schemas and tab-name collisions.

Trip-wide summaries aggregate recognized tabs and report coverage, gaps, missing/unparseable
values and clashes, including overnight activities. Stay rows can overlap sightseeing but
overlapping stays are flagged. Times are local wall times as entered: timezone conversion,
route ordering and travel times are not inferred. Read limits produce partial-coverage notices.

If trips are enabled, explicitly link the spreadsheet to an existing trip with
confirmation. The link applies across its day/city tabs and is isolated by chat. Summaries can
compare planned and actual totals per original currency; incomplete costs, partial reads or
a selected single tab suppress the whole-trip difference. Expense export reads the actual
ledger, not model-generated amounts, and creates a separate confirmed ledger export file.

Daily suggestion-row caps are per chat and reset at UTC midnight. The cap is reserved before
remote writes, so unknown/failed outcomes may still consume it. Limits are listed in
the Google plugin settings and downstream environment template. `/forgetme` and `/purgechat` remove scoped local links/reservations;
they do not delete Google files. The owner manages exported-file retention in Google Drive.

## Verification and rollout

The implementation has mocked Google REST and console confirmation tests plus disposable-schema
Postgres tests for settings, reservation replay, concurrent quota and privacy scope. Run:

```sh
uv run pytest -q packages/plugins/google/tests \
  packages/plugins/search/tests/test_csv_input.py
```

Owner credentials are required for a live check of account consent, Google permissions, API
enablement, nested Doc tabs, read/append/create and manual file sharing. Start with a test chat
and non-sensitive sample files; opt out other chats during that live verification if desired.
This implementation work does not authorize deployment or send any live Google/Telegram data.
