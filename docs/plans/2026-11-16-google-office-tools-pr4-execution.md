# Google output tools PR 4: tab edits, Doc block appends and trash

Execution plan for PR 4 (v0.24.0) of
[General Google Sheets and Docs output tools](2026-11-16-google-office-tools.md). Read that plan's
"Owner decisions", "Design" (Permissions, Output journal, Confirm switch, **PR 4**) and the PR 4
acceptance criteria first, then `AGENTS.md` ("Plugin module layout", "Skills", "Changelog").
PRs 1–3 are on this branch ([PR 1](2026-11-16-google-office-tools-pr1-execution.md),
[PR 2](2026-11-16-google-office-tools-pr2-execution.md),
[PR 3](2026-11-16-google-office-tools-pr3-execution.md)): the create tier, `CREATE_KINDS`,
`google_outputs`, the preview switch, `google_create_workbook` (`workbook_builder.py`) and
`google_create_rich_doc` (`rich_doc.py`) exist. Reuse them.

The work runs in **two parts**, each a separate execution run. The prompt says which part to
do. Do only that part; the other part's files may be edited only where that part says so.

## Shared rules (both parts)

### Who may do what (permission change; call it out in your report)

"Output" means a `google_outputs` row for this chat **and topic** (`repo.output(chat,
thread_id, file_id)`), whose `owner_id` equals the current `client.identity()`, and whose Drive
metadata shows `ownedByMe: true`, `trashed` false and the expected mime type. "Own output"
additionally means `requester_key == ctx.user.ref.key`.

| Tool | Own output | Other member's output | Human-shared file |
| --- | --- | --- | --- |
| `google_add_sheet_tab` | create tier | writer tier | writer tier, always confirms |
| `google_replace_sheet_tab` | create tier | writer tier | refused |
| `google_append_doc_blocks` | create tier | writer tier | writer tier, always confirms |
| `google_trash_file` | create tier, always confirms | writer tier, always confirms | refused |

- All four tools use the **create** gate in `gates.py` (members reach the handler); the handler
  then authorizes with `create=True` for own outputs and `write=True` otherwise, exactly like
  `google_append` does (`via_output`). Record `via_output` (own output) and the tier in the
  payload, and in `verify_commit` and again right before the first write re-check the binding
  with `_same_append_output_binding` (or the equivalent for outputs of other members: same
  chat, thread, owner, file, and still not trashed). Never fall back silently to another tier.
- **Previews off** runs immediately only for own or other-member outputs edited by an
  authorized user, never for human-shared files and **never for trash**. Add the new kinds to
  the `_create_or_append` qualification logic in `plugin.py` accordingly (or a sibling helper),
  so the decision is made from the frozen payload's `via_output` / target, not re-derived from
  arguments.
- Every new action kind is added to `action_handlers()`; test the real confirmation flow
  (`ConfirmService.request()` accepts it) for one tool per part.
- Every commit re-runs local validation against the current settings before `verify_commit`
  (as PR 2/3 do) and makes no Google call when validation fails.
- Revision, owner identity, requester/chat/topic checks: `verify_commit` as today.
- `GOOGLE_INSTRUCTIONS` stays ≤ 800 characters (shorten wording without changing meaning) and
  names the new tools in one short sentence. Add the tools to the `google_workspace` skill.
- `EXPLAIN.md` gets a short section per part; regenerate the wiki with
  `uv run python .github/scripts/generate_explainers.py`. `CHANGELOG.md` entries under
  `Unreleased` (Added; Security for the gate/permission change).

### Tests (both parts)

Use the existing fakes; extend `tests/conftest.py` where needed. For Docs reuse
`RequestDrivenRichDocApi` (extend it to start from existing content and to honour `tabId`).
Every behaviour below needs a test that fails without the change. Run
`uv run pytest -q packages/plugins/google/tests` with `TMPDIR="$PWD/.tmp"` (create it, delete it
afterwards), `uv run ruff check packages/plugins/google`, `uv run ruff format --check` on changed
files, and the explainer `--check`. Report counts. Don't weaken existing tests.

## Part A: Sheet tabs and trash

### You own (Part A)

`packages/plugins/google/src/chatcore_google/`: new `sheet_tabs.py`, `service.py`,
`plugin.py`, `gates.py`, `repository.py` (two methods only), `client.py` (two methods only),
`schemas.py`, `EXPLAIN.md`; `packages/plugins/google/tests/**`; `CHANGELOG.md`. Not
`workbook.py`, `workbook_builder.py`, `rich_doc.py`, migrations, `pyproject.toml`, `uv.lock`.
**No migration**: `google_outputs.trashed_at` already exists.

### A1. `google_add_sheet_tab(url, tab)`

- `tab` is one tab object in the `google_create_workbook` schema (reuse `schemas.TABS["items"]`).
  Compile with `compile_workbook([tab], settings, title)` and `render_workbook` (title is the
  file's name; only the tab's requests are used).
- In new `sheet_tabs.py`, `tab_requests(plan, *, sheet_id, index) -> list[list[dict]]`: turn
  the one-sheet `WorkbookPlan` into requests for an existing spreadsheet: an `addSheet` with the
  plan's sheet properties (title, gridProperties, `sheetId` = `sheet_id`, `index` = `index`),
  then the plan's data batches and finish requests with every `sheetId` 1 rewritten to
  `sheet_id`, and **without** the `updateSpreadsheetProperties` title rename. Deep-copy; never
  mutate the plan. Assert no request references another `sheetId`.
- Propose: resolve the file (`self.file(ctx, url, SHEET)` handles human-link rules and
  ownership metadata), decide output/own/human as in the table, authorize, read
  `client.sheet(file_id)`; refuse if a tab with the same name exists (case-insensitive) or the
  spreadsheet would exceed `settings.max_tabs` tabs. Payload: file ID, `via_output`, target
  kind (`own_output`, `output`, `human`), the tab spec, `owner`, `revision`. Preview: file name,
  target, then `preview_workbook([tab])`.
- Commit: re-validate; `verify_commit`; re-read the sheet; refuse if the name now exists; pick
  `sheet_id` = an unused random int in `1..2**31-1` (deterministic in tests via an injectable
  helper), `index` = number of tabs; send the requests with `client.batch(file_id, batch)` in
  order. On a failure after `addSheet` succeeded, report partial with the URL and the tab name
  and never retry. Reply `Added tab "<name>" to <url>.`

### A2. `google_replace_sheet_tab(url, tab_name, tab)`

- Outputs only (own: create tier; other member's: writer tier); a human file is refused with a
  clear message. `tab_name` selects an existing tab by exact title; its `sheetId` is frozen in
  the payload with the title. `tab` uses the same schema; its `name` must equal `tab_name`
  (case-insensitive) or be omitted-equivalent: require `tab["name"] == tab_name` for clarity.
- New `client.sheet_for_replace(file_id)`: `GET /v4/spreadsheets/{id}` with fields
  `sheets(properties,bandedRanges(bandedRangeId,range),basicFilter,merges,conditionalFormats,charts(chartId),protectedRanges(protectedRangeId),filterViews(filterViewId))`.
- Refuse (no write) if the tab has charts, protected ranges or filter views (they would be
  destroyed or left inconsistent; say so).
- Requests, all limited to that `sheetId`, in one `batchUpdate` before the data:
  `deleteBanding` for each banded range on the tab; `clearBasicFilter` if a basic filter exists;
  `deleteConditionalFormatRule` for each rule (highest index first); `unmergeCells` over the
  whole tab if it has merges; `updateCells` with `range: {"sheetId": id}` and `fields: "*"`
  (clears values, formats and validation); `updateSheetProperties` setting `gridProperties`
  (rowCount, columnCount, frozenRowCount 0) to the new plan's grid, with exactly those `fields`.
  Then the plan's data batches and finish requests rewritten to this `sheetId`, plus an
  `updateSheetProperties` for the frozen rows the plan sets. Never touch another tab; assert in
  tests that no request names another `sheetId` and no request is spreadsheet-wide.
- Commit re-reads and refuses if the tab's `sheetId`/title changed or it gained charts,
  protected ranges or filter views. Partial failure after the clear: report partial ("the tab
  was cleared but not fully rewritten"), URL, no retry.

### A3. `google_trash_file(url)`

- Outputs in this chat/topic only (own: create tier; other member's: writer tier). Human files,
  files attached to a trip (`repo.trip_file(chat, file_id)` is not None: tell them to remove it
  from the trip first) and already-trashed outputs are refused. **Always** a pending action,
  even with previews off; the preview says the file moves to Drive trash and can be restored
  from Drive for 30 days.
- Repository: `output_any(chat, file_id)` (the row regardless of thread/trash, for the "already
  in trash" message) and `mark_output_trashed(chat, file_id, action_id) -> bool` (sets
  `trashed_at=now()` only where it is NULL; returns whether a row changed). Memory and Postgres;
  add a Postgres test in `test_pg_google.py` style (skips without Postgres).
- New `client.trash_file(file_id)`: Drive `PATCH /drive/v3/files/{id}` with
  `{"trashed": true}`, params `fields=id,trashed`, `supportsAllDrives=true`.
- Commit: re-validate binding and permissions; fresh metadata must show `ownedByMe`, an owner
  whose `permissionId` is the current identity, not trashed, expected mime; call
  `trash_file`; require the response to show `trashed: true` (otherwise read metadata once and
  report uncertain if it doesn't); then `mark_output_trashed`. A second trash (same or new
  action) is refused before any Google call. Reply `Moved <url> to Drive trash (restorable for
  30 days).`

### Part A tests

- `tab_requests`: addSheet properties, sheetId rewrite everywhere, no title rename, no other
  sheetId, input plan unchanged.
- Add tab: own output with previews off runs immediately; other member's output needs writer
  (member refused); human Sheet needs writer and stays pending with previews off; name collision
  at propose and at commit; tab-count limit; partial failure after addSheet.
- Replace tab: exact clear/rewrite request list for a tab with banding, filter, merges and two
  conditional-format rules; no request touches another tab; refusal for human files, charts,
  protected ranges, filter views, changed sheetId; partial failure after clear.
- Trash: always pending (previews off too); own vs other member vs human; trip-attached
  refusal; second trash refused without a Google call; non-owned metadata refused; response
  without `trashed: true` handled; `trashed_at` set (memory and Postgres); a trashed output is
  no longer an output for append/add-tab/replace.
- Confirmation flow for `trash_file`; `GOOGLE_INSTRUCTIONS` ≤ 800; gates for the three tools.

## Part B: append blocks to a Doc

### You own (Part B)

`packages/plugins/google/src/chatcore_google/`: `rich_doc.py` (additive: new parameters with
defaults that keep `google_create_rich_doc` byte-for-byte identical), `service.py`, `plugin.py`,
`gates.py`, `schemas.py`, `EXPLAIN.md`; `packages/plugins/google/tests/**`; `CHANGELOG.md`.
Not `repository.py`, `client.py`, `workbook*.py`, migrations, `pyproject.toml`, `uv.lock`.

### B1. `google_append_doc_blocks(url, blocks, tab_id=None)`

- `blocks` uses the `google_create_rich_doc` schema (reuse `schemas.BLOCKS`), except `title`
  blocks are refused (a title belongs at the top).
- Resolve the target with `self.doc_source(ctx, url, ...)` (it handles human-link and output
  rules; pass `write=` per the permission table), decide own/output/human, authorize. For a
  multi-tab Doc `tab_id` is required and must name an existing tab (no child tabs below it are
  affected); for a single-tab Doc it is optional.
- `compile_doc(blocks, settings, *, base_index=1, leading_newline=False, tab_id=None)`: shift
  every index by `base_index - 1`; when `leading_newline` is true the inserted text starts with
  `\n` (so the new content becomes new paragraphs after the existing last one) and every
  paragraph range accounts for it; add `tabId` to every `location` and `range` when given. The
  existing create path calls it with defaults and must produce identical requests (add a test
  comparing against a fixture captured **before** your change, as PR 2 did with the golden
  workbook hash: capture it first, then change the code).
- Batch A for an append additionally starts (after `insertText`) with an
  `updateTextStyle` over the whole inserted range with `textStyle: {}` and `fields: "*"`, an
  `updateParagraphStyle` over it with `paragraphStyle: {"namedStyleType": "NORMAL_TEXT"}` and
  `fields: "*"`, and a `deleteParagraphBullets` over it, so the new paragraphs don't inherit the
  last existing paragraph's style, marks or bullets. Then the plan's own styles, marks and
  bullets.
- `fill_tables(client, file_id, plan, *, tab_id=None, after_index=0)`: consider only tables in
  the target tab whose `startIndex >= after_index`, in order. `_body_content` gains a `tab_id`
  parameter that selects that tab from the tab tree (recursively) and keeps the "nothing
  unexpected" checks only for that tab's own body.
- Commit: re-validate; `verify_commit`; read the Doc once (`document_for_edit`), record its
  `revisionId`, the target tab body's last paragraph and its end index. If the last paragraph
  is empty and unstyled, insert at its start without a leading newline; otherwise insert at
  `endIndex - 1` with `leading_newline=True`. Send batch A with
  `client.edit_document(file_id, requests, revision)` (revision precondition), batch B likewise
  with the revision returned by A (or re-read), batch C via `fill_tables(..., after_index=...)`.
- Verify: re-read; the target tab's content **before** the insertion point must equal the
  snapshot taken at commit (same paragraphs and tables by text; compare with
  `flatten_doc_snapshot` text or an equivalent structural walk), and the content from the
  insertion point on must satisfy `verify_rich_doc` for the plan (add a `verify_rich_doc`
  variant or parameter that verifies a content slice). Text verified → success with any
  formatting warnings; not verified → partial/uncertain reply, nothing else attempted.
- Previews off runs immediately only for outputs (per the table); human Docs always confirm.
  Human Docs keep every existing safety check `google_edit_doc` applies at commit (suggestion
  and revision checks): reuse `doc_source`/`recheck_doc_source` rather than re-implementing.
- Reply `Appended <n> blocks to <url>.` plus warnings.

### Part B tests

- The create-path golden fixture for `compile_doc` defaults is unchanged.
- Offset compile: indices shifted, `leading_newline` ranges, `tabId` on every location/range,
  non-BMP text before the insertion point and in the appended blocks, two appended tables.
- Through the request-driven fake starting from existing content (a heading, a bullet list
  last, an existing table): new paragraphs don't inherit bullets or styles; only the new tables
  are filled; the prefix is unchanged; verification passes; a modified prefix fails.
- Multi-tab Doc: `tab_id` required; content lands in that tab only; other tabs unchanged.
- Permissions per the table (own/other/human; members refused on human Docs and others'
  outputs); previews off runs own-output appends immediately and keeps human appends pending.
- `title` blocks refused; validation errors make no Google call (including no identity call).

## Done when

- Every PR 4 acceptance criterion in the master plan has a test that fails without the change.
- No request from these tools touches another tab, another file or spreadsheet-wide
  properties (except `addSheet`), and no Docs table cell index is predicted.
- Existing tests pass; `google_create_rich_doc` and `google_create_workbook` output unchanged.
- Lint, format and the explainer check are clean.
- The report lists permission changes (`gates.py`, the per-target tiers), new client and
  repository methods, and any deviation.
