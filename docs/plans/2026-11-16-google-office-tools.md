# General Google Sheets and Docs output tools

Status: Shipped inactive. Approved by the owner 2026-11-16; released 2026-11-19 as v0.21.0–v0.24.0
(PRs #53, #55, #56, #60). Member creates are off until an app sets `GOOGLE_CREATE_USERS=members`.
Date: 2026-11-16.

## Goal

Let any clawdot bot produce formatted, typed Google Sheets and native Google Docs for content
other than the expense ledger (comparisons, itineraries, lists, reports), edit those outputs, and
trash them, without giving the model raw Sheets/Docs API access. The model sends a bounded,
validated content model; code renders it into batched API requests.

## Owner decisions (2026-11-16)

1. Scope: all four areas, released as **stacked PRs** (below).
2. **Anyone may create.** Any member of an allowed chat with Google on may create new files.
   Files created by someone who is not a bot admin or `GOOGLE_WRITE_USER_IDS` writer **always
   get an anyone-with-link viewer link**, because they cannot open a private file in the owner's
   Drive. They may ask for editor; they cannot pick private.
3. **Preview can be turned off per chat.** A manager toggles it with `/google confirm on|off`
   **or in plain language** ("stop asking me to confirm Google files"), through the
   `configure_google` tool. With preview off, creates and edits of files the bot made in this chat
   run immediately.
4. **Delete** = move a file **the bot created in this chat** to Drive trash. Never human files.
5. DM rules stay as they are (`authorize()`: `can_dm`, per-DM on/off, owner configures DMs).

## What exists (v0.18.1)

- `google_create_sheet` writes one plain tab; `google_create_doc` writes plain text;
  `google_append`, `google_edit_sheet`, `google_edit_doc` change existing files. All writes need
  a bot admin or writer and a confirmed preview.
- `render.literal_rows` prefixes text starting with `= + - @ \t \r` with an apostrophe. The
  values go in `userEnteredValue.stringValue`, which Sheets never evaluates (verified live in
  v0.18.0), so the apostrophe is only visible noise.
- `chatcore_google/workbook.py` validates a neutral `schema: 1` workbook and renders bounded,
  batched requests (typed numbers/dates/money, styles, banded/filtered tables, frozen rows,
  `SUM` totals, "(incomplete)" title until the last batch succeeds).
- New Doc outputs are journaled in `google_edit_results` (output binding: owner, chat, thread).
  New Sheets are not journaled. Link sharing (`link_sharing.publish_doc_link_access`) is
  Doc-only.

## Design

### Permissions (`permissions.py`, `gates.py`: permission change, call out in PR 1)

Two write tiers replace the single `write` check:

| Tier | Who | Covers |
| --- | --- | --- |
| `create` | Members when `GOOGLE_CREATE_USERS=members`; otherwise admins and writers (default, unchanged) | New files; edits and trash of **their own** outputs in this chat |
| `write` | Bot admins and writers (unchanged) | Everything above for any output in this chat, plus edits to human-shared files |

- Framework default stays `GOOGLE_CREATE_USERS=writers`, so v0.21.0 is behaviour-neutral; sb_bot
  sets `members` in its wiring PR.
- Non-writer creates are capped per user per day: `GOOGLE_CREATES_PER_USER_DAY` (default 20,
  1–500), counted in `google_actions` with a new `kind` column. Writers are not capped.
- Handlers re-check the tier at call/commit time; gates only decide what the model is offered.

### Output journal (migration `0006_outputs.sql`, additive)

`google_outputs(chat_key, file_id, mime, requester_key, thread_id, owner_id, action_id,
created_at, trashed_at)`, primary key `(chat_key, file_id)`. Every create (old and new tools,
expense export) records a row after the file ID is known. "A file the bot made in this chat"
means a row here with matching chat, thread, owner identity, and Drive metadata showing the
owner account owns it and it is not trashed.

### Confirm switch (migration adds `google_settings.confirm_writes BOOLEAN NOT NULL DEFAULT TRUE`)

- `configure_google` gains optional `confirm_writes: bool` (and `enabled` becomes optional; one
  of them required). The tool description names plain-language phrasings so the model calls it.
  `/google confirm on|off` calls the same path. Changing the switch is itself a manager-confirmed
  action and bumps `revision` (so pending previews expire).
- With `confirm_writes=false`, these run immediately (the handler builds the same frozen action
  and calls `commit` with the same checks, then replies with the result): every create, and
  every edit of a journaled output. **Always confirmed regardless**: trash, edits to
  human-shared files (`google_edit_doc`, `google_edit_sheet`, `google_append` on non-outputs,
  trip edits), configuration changes, browser edits.
- Status (`/google`) shows the switch.

### Link sharing for all new outputs

Generalise `publish_doc_link_access` to `publish_link_access(file_id, mime, access)` for Docs
and Sheets, with the same ownership, ACL-safety and discovery-off checks. Access resolution:
explicit tool choice, else chat/framework default, then **raised to at least `viewer` for a
non-writer requester**. The preview (or the immediate reply) states the access.

### PR 2: `google_create_workbook`

Model-facing schema (compiled in code into the `schema: 1` model, then `render_workbook`):

```json
{"title": "Tokyo hotels", "link_access": "viewer",
 "tabs": [{"name": "Compare", "heading": "Shortlist (Mar 2027)", "note": "Prices per night",
   "columns": [{"header": "Hotel", "type": "text", "width": 220},
               {"header": "Price", "type": "money", "decimals": 0, "total": true},
               {"header": "Rating", "type": "number"},
               {"header": "Breakfast", "type": "bool"},
               {"header": "Check-in", "type": "date"},
               {"header": "Share", "type": "percent"},
               {"header": "Link", "type": "url"}],
   "rows": [["Hotel A", 21000, 4.5, true, "2027-03-02", 0.25, "https://example.com"]]}]}
```

- Types: `text`, `number`, `money` (decimals 0–4, no currency symbol; header names it),
  `percent` (fraction, `0.0%`), `date`, `datetime`, `bool` (checkbox), `url` (literal text plus
  a Sheets hyperlink via `textFormatRuns.link`, never a formula). `null` = empty cell.
- `workbook.py` additions (additive; expense output unchanged): `percent`, `bool` and `url`
  kinds. `SUM` remains the only formula.
- Each tab: optional heading (title style) and note, a styled frozen header, banded table with a
  filter, optional totals row (`SUM` per `total` column), column widths (given or estimated).
- Limits: tabs ≤ `GOOGLE_MAX_TABS` (32), rows per tab ≤ `GOOGLE_MAX_ROWS`, columns ≤
  `GOOGLE_MAX_COLUMNS`, cell text ≤ `GOOGLE_MAX_CELL_CHARS`, whole argument ≤ new
  `GOOGLE_OUTPUT_MAX_CHARS` (default 60,000). Validation errors name the tab, row and column and
  happen before any write.
- Preview: title, access, per-tab name, columns with types, row count and first 3 rows; full
  content attached as a file when long (existing `action()` behaviour).

### PR 3: `google_create_rich_doc`

Block model (≤ 200 blocks, ≤ `GOOGLE_MAX_CHARS` text, ≤ 10 tables of ≤ 50×8 cells):

```json
{"title": "Kyoto day plan", "blocks": [
  {"type": "heading", "level": 1, "text": "Day 1"},
  {"type": "paragraph", "spans": [{"text": "Meet at "}, {"text": "Kyoto Station", "bold": true},
                                   {"text": " map", "link": "https://maps.example"}]},
  {"type": "bullets", "items": ["Fushimi Inari", "Lunch"]},
  {"type": "numbered", "items": ["Buy pass", "Board bus 5"]},
  {"type": "table", "header": ["Time", "Place"], "rows": [["09:00", "Inari"]]},
  {"type": "divider"}]}
```

- `text` is accepted wherever `spans` is (plain). Span marks: `bold`, `italic`, `underline`,
  `code` (monospace), `link` (http/https only). Heading levels 1–3, plus `title`.
- Rendering in code: one `insertText` of the computed text, then `updateParagraphStyle`
  (named styles), `updateTextStyle`, `createParagraphBullets`, then tables inserted last-to-first
  with cells filled from the end so earlier indices never shift. UTF-16 offsets as in the edit
  service. Same "(incomplete)" title until the last batch succeeds.
- Read-back verification: compare the document's text (`render.doc_text`) to the expected text
  and check heading styles; report formatting warnings separately from verified text, as the
  edit service does.
- `google_create_doc` stays for plain text; the skill instructions prefer the rich tool for
  structured content.

### PR 4: edits and trash

| Tool | Target | Confirm when preview is off |
| --- | --- | --- |
| `google_add_sheet_tab(url, tab)` | Output Sheet; or human Sheet (writer tier) | Output: no. Human: yes |
| `google_replace_sheet_tab(url, tab_name, tab)` | Output Sheet only | No |
| `google_append_doc_blocks(url, blocks, tab_id?)` | Output Doc; or human Doc (writer tier) | Output: no. Human: yes |
| `google_trash_file(url)` | Output only, not already trashed | Always confirms |

- `tab` uses the PR 2 tab schema; `blocks` the PR 3 block schema. Replacing a tab clears it
  (`updateCells` with `fields: "*"` over its grid, then delete bandings/filters) and rewrites it;
  it never touches other tabs.
- Trash uses Drive `files.update {trashed: true}` (recoverable for 30 days), records
  `trashed_at`, and refuses files whose metadata no longer shows owner-account ownership.

### PR 1 also: apostrophe fix

`literal_rows` stops calling `neutralize` (text stays `stringValue`, never parsed), matching
`workbook.py`. `edit_service`/`edit_snapshot` comparisons and `test_service.py` expectations are
updated. Itinerary rows inherit the fix. A regression test asserts `"=BAD()"` is written as
`{"stringValue": "=BAD()"}` and that no request anywhere in the plugin contains `formulaValue`
except workbook `SUM` totals.

## Releases (stacked, each based on the previous branch)

| PR | Version | Contents | sb_bot wiring |
| --- | --- | --- | --- |
| 1 | v0.21.0 | Apostrophe fix, create/write tiers, output journal, confirm switch (tool + command), link sharing for Sheets, daily create cap | Pin; `GOOGLE_CREATE_USERS=members` |
| 2 | v0.22.0 | `google_create_workbook`, workbook kinds `percent`/`bool`/`url` | Pin; routing check |
| 3 | v0.23.0 | `google_create_rich_doc` | Pin |
| 4 | v0.24.0 | `google_add_sheet_tab`, `google_replace_sheet_tab`, `google_append_doc_blocks`, `google_trash_file` | Pin |

Version numbers shift if another release lands first. Each PR carries its CHANGELOG entry,
`EXPLAIN.md` update, skill/tool declarations (`google_workspace` skill tools, instructions ≤ 800
characters), `routing_set.jsonl` rows and request snapshot updates where tests require them, and
this plan's index row. The sb_bot changes can be one deployments PR after the platform PRs, or
one per release; nothing is merged or deployed without the owner's go-ahead.

## Acceptance criteria

PR 1
- [ ] Text starting with `=`, `-`, `+`, `@` is written as plain `stringValue` with no apostrophe
      by `google_create_sheet`, `google_append`, `google_edit_sheet` and itinerary writes.
- [ ] With `GOOGLE_CREATE_USERS=writers` (default), behaviour and the agent request snapshot are
      unchanged for existing tools except the apostrophe fix.
- [ ] With `members`, a non-writer can create a Doc/Sheet; the file gets a viewer (or requested
      editor) link; they cannot choose private; they cannot edit human files; the daily cap
      refuses the 21st create with a clear message.
- [ ] Every create records a `google_outputs` row; a create in another chat/topic is not an
      output there.
- [ ] A manager can turn previews off with `/google confirm off` and by natural language through
      `configure_google`; a non-manager cannot. The change is confirmed and bumps revision.
- [ ] With previews off, creates and output edits run immediately with the same commit checks;
      human-file edits, trash and configuration still show a preview.
- [ ] Migration 0006 is additive; purge hooks delete `google_outputs` rows.

PR 2
- [ ] A 3-tab workbook with every column type renders typed cells: money/number/percent as
      numbers with formats, dates as date serials, bools as checkboxes, URLs as linked literal
      text; header frozen; banded filtered table; `SUM` totals only where `total` is set.
- [ ] Text such as `=IMPORTXML(...)` stays literal; the request contains no formula except `SUM`.
- [ ] Invalid input (wrong type, too many rows/columns/tabs, oversize argument, duplicate tab
      names) is rejected before any API call, naming the tab/row/column.
- [ ] Partial failure leaves "(incomplete)" in the title and the reply links the file.
- [ ] The expense workbook output is byte-for-byte unchanged (existing expense tests pass).

PR 3
- [ ] A doc with every block type renders the expected named styles, bullets, numbering, marks,
      links and tables; read-back text matches; table indices are correct for multiple tables
      with non-BMP text (UTF-16).
- [ ] Non-http(s) links and oversize content are rejected before any write.
- [ ] Partial failure leaves "(incomplete)" and reports verified vs unverified.

PR 4
- [ ] Add/replace tab and append blocks work on outputs (immediately when previews are off) and
      require writer tier plus confirmation on human files; replace never alters other tabs.
- [ ] Trash works only for outputs in this chat/topic owned by the owner account, always
      confirms, records `trashed_at`, and a second trash is refused.
- [ ] Non-writers can edit or trash only outputs they requested.

All PRs: the `AGENTS.md` checks pass (`ruff check`, `ruff format --check` on changed files,
`pytest -m "not browser and not sandbox_integration"` with Postgres), and a live Sheets/Docs
smoke test in the owner's account is run before each release is pinned.

## Execution and review

Owner's computer with Codex CLI installed: Claude wrote this plan; **Codex executes**
(`gpt-6-luna`) and **Codex reviews** (`gpt-6.1-sol`) each PR; Claude does the final review and
opens the PRs. Each PR description lists permission changes (`gates.py`, `permissions.py`),
migrations and new settings.

## Out of scope

Charts, conditional formatting, images in Docs, Slides, editing arbitrary ranges of human
Docs beyond today's bounded tools, Drive folder management, permanent deletion, and syncing
outputs with live data.
