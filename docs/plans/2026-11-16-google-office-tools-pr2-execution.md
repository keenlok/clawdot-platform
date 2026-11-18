# Google output tools PR 2: `google_create_workbook`

Execution plan for PR 2 (v0.22.0) of
[General Google Sheets and Docs output tools](2026-11-16-google-office-tools.md). Read that plan's
"Owner decisions", "Design" (Permissions, Output journal, Confirm switch, Link sharing, **PR 2**)
and the PR 2 acceptance criteria first, then `AGENTS.md` ("Plugin module layout", "Skills",
"Changelog"). PR 1 ([execution plan](2026-11-16-google-office-tools-pr1-execution.md)) is
already on this branch: the create tier, `google_outputs`, `reserve_create`, the preview switch
and `publish_sheet_link_access` exist. Reuse them; do not re-implement them.

Read these files before changing anything:
`packages/plugins/google/src/chatcore_google/{workbook.py,service.py,plugin.py,gates.py,link_sharing.py,settings.py,schemas.py,expense_service.py,repository.py,EXPLAIN.md}`
and the tests in `packages/plugins/google/tests/` (especially `test_workbook.py`,
`test_workbook_golden.py`, `test_service.py`, `test_plugin.py`, `conftest.py`).

## You own

- `packages/plugins/google/src/chatcore_google/`: `workbook.py` (additive only), new
  `workbook_builder.py`, `service.py`, `plugin.py`, `gates.py`, `link_sharing.py`,
  `settings.py`, `schemas.py`, `EXPLAIN.md`
- `packages/plugins/google/tests/**` (edit and add), except
  `tests/test_workbook_golden.py` and `tests/fixtures/expense_workbook_golden.json`, which you
  must not change
- `CHANGELOG.md` (entries under `Unreleased`)

You may **not** edit: `packages/core/**`, other plugins, `apps/**`, `expense_service.py`,
`edit_service.py`, `repository.py`, any migration, `pyproject.toml`, `uv.lock`. No migration is
needed: `google_outputs` and `google_create_reservations` already cover this tool. Report if one
must change.

## 1. `workbook.py`: three new cell kinds (additive)

The expense output must stay byte-for-byte identical: `tests/test_workbook_golden.py` hashes the
rendered plan of a real expense model captured before this change. It must keep passing
unchanged. Rules that keep it so:

- Add kinds `percent`, `bool`, `url` to `_KINDS`. Keep every existing branch's output identical.
- `percent`: `v` is an exact decimal string (same `_decimal` rules) holding a fraction
  (`"0.25"` = 25%); `numberValue` plus `numberFormat {"type": "PERCENT", "pattern": "0.0%"}`.
- `bool`: `v` is a JSON `true`/`false` (`type(v) is bool`); `{"boolValue": v}` plus a checkbox:
  `"dataValidation": {"condition": {"type": "BOOLEAN"}}` on the cell.
- `url`: `v` is a string, ≤ `max_cell_chars` and ≤ 2000 characters, scheme `http` or `https`
  (parse with `urllib.parse.urlsplit`, require a netloc), no whitespace or control characters.
  Render `{"stringValue": v}` plus `"textFormatRuns": [{"startIndex": 0, "format": {"link":
  {"uri": v}}}]`. Never `formulaValue`, never `HYPERLINK`.
- `sum` gains an optional `"as"` field: `"money"` (default, needs `exp`, today's behaviour),
  `"number"` (pattern `#,##0.########`, no `exp`) or `"percent"` (`0.0%`, no `exp`). Add `"as"` to
  the allowed cell fields. Absent `"as"` must render exactly as today.
- The `updateCells` `fields` mask: keep `"userEnteredValue,userEnteredFormat"` for any request
  whose rows contain no `bool`/`url` cell. Only when a request's rows contain one, append
  `,dataValidation` (bool present) and/or `,textFormatRuns` (url present), in that order.
- `write_workbook(client, plan, *, on_created=None)`: if `on_created` is given, await
  `on_created(file_id)` right after the create call returns a valid ID and before any batch, so
  the output journal row exists even if a later batch fails. If `on_created` raises, stop (no
  batches), log the error class only, and return `(url, "recording the new file failed")`. Validate the ID with
  `re.fullmatch(r"[A-Za-z0-9_-]{1,200}", file_id)`, as `service.commit` does for `create_sheet`;
  reject otherwise with the existing `ValueError`. Without `on_created`, behaviour is unchanged.
- Update the module docstring to list the new kinds.

## 2. `workbook_builder.py`: compile the model-facing schema (new module)

`compile_workbook(spec, settings) -> dict` turns the tool argument into a `schema: 1` model and
raises `ValueError` before any write. `preview_workbook(spec) -> str` renders the preview body.
No Google calls, no I/O.

Input (the tool's `tabs` argument; `title` and `link_access` are separate tool arguments):

```json
[{"name": "Compare", "heading": "Shortlist (Mar 2027)", "note": "Prices per night",
  "columns": [{"header": "Hotel", "type": "text", "width": 220},
              {"header": "Price", "type": "money", "decimals": 0, "total": true},
              {"header": "Rating", "type": "number"},
              {"header": "Breakfast", "type": "bool"},
              {"header": "Check-in", "type": "date"},
              {"header": "Share", "type": "percent"},
              {"header": "Link", "type": "url"}],
  "rows": [["Hotel A", 21000, 4.5, true, "2027-03-02", 0.25, "https://example.com"]]}]
```

Validation (every message names where: `Tab "Compare", row 3, column "Price": …`; rows are
1-based data rows, columns use the header):

- `tabs`: list, 1 ≤ len ≤ `settings.max_tabs`. Tab `name`: string, 1–100 characters, stripped,
  printable, unique case-insensitively (the existing `render_workbook` check stays as a backstop,
  but the builder must report the tab by name first). Unknown keys in a tab, column or the spec
  are rejected.
- `heading` and `note`: optional strings, ≤ 200 and ≤ 1000 characters.
- `columns`: 1 ≤ len ≤ `settings.max_columns`. `header`: nonempty string ≤ 200 characters.
  `type` ∈ `text, number, money, percent, date, datetime, bool, url`. `decimals`: only for
  `money`, int 0–4, default 2. `total`: bool, only for `number`, `money`, `percent`. `width`:
  optional int 20–1000.
- `rows`: list, 0 ≤ len ≤ `settings.max_rows`. Each row is a list of exactly `len(columns)`
  values. `null` is an empty cell for every type.
- Values by type: `text` string ≤ `settings.max_cell_chars`; `number`/`money`/`percent` a JSON
  number (`int` or `float`, never `bool`, finite), converted with `Decimal(str(value))` and
  emitted as a decimal string (`format(d, "f")`); `date` ISO `YYYY-MM-DD` string; `datetime`
  ISO local time without offset; `bool` JSON boolean; `url` as in section 1. Reuse
  `workbook.py`'s validators where you can rather than duplicating rules; wrap their
  `ValueError` with the location prefix.
- Whole argument: `len(json.dumps(tabs, ensure_ascii=False, separators=(",", ":")))` ≤
  `settings.output_max_chars`; check this first, before walking the structure.
- Total cells across tabs ≤ `settings.export_max_cells` (the renderer enforces it; make the
  message generic for this tool: "This workbook needs more than N cells; split it up.", not
  the expense wording). Pass the cap to `render_workbook` and catch/rewrite only that message,
  or check it in the builder before rendering.

Layout per tab (row indices 0-based):

- If `heading`: a row with one `text` cell, style `title`. If `note`: a row with one `text`
  cell, style `note`. If either is present, one empty row after them.
- Header row: one `text` cell per column, style `header`. `frozen_rows` = header index + 1.
- Data rows: typed cells per column (`money` uses `exp` = `decimals`; text cells longer than
  60 characters use style `wrap`).
- If any column has `total: true` and there is at least one data row: a totals row. The first
  column's cell is `{"k": "text", "v": "Total", "s": "total"}` unless that column itself is a
  total column (then leave the label out). Each total column gets
  `{"k": "sum", "v": [first_data, last_data], "s": "total", "as": <money|number|percent>,
  "exp": decimals}` (`exp` only for money); other cells `{"k": "text", "v": "", "s": "total"}`.
- `tables`: one table `{"header": header_index, "last": last_data_index (or header_index when
  there are no rows), "columns": len(columns), "filter": true}`.
- `widths`: given width, else estimate
  `min(400, max(80, 7 * longest + 24))` where `longest` is the longest display length of the
  header and the column's values (dates 11, datetimes 17, bools 4, numbers `len(str(value))`).

Model: `{"schema": 1, "title": title, "sheets": [{"title": name, "rows": …, "widths": …,
"tables": …, "frozen_rows": …}]}`.

Preview (`preview_workbook`): per tab, `Tab "<name>": <n> rows`, then the columns as
`Header (type)` joined by `, `, then up to the first 3 rows as compact JSON, then
`… <n-3> more rows` if applicable.

## 3. Tool and settings

- `settings.py`: `output_max_chars: int = Field(default=60_000, ge=5000, le=200_000)` (env
  `GOOGLE_OUTPUT_MAX_CHARS`).
- `schemas.py`: add the `TABS` JSON schema used by the tool (array of tab objects with the
  properties above, `columns`/`rows` required, `additionalProperties: false`, column `type`
  enum). Keep argument descriptions short; they go into every request that offers the tool.
- `plugin.py`: new tool `google_create_workbook(title, tabs, link_access=None)`,
  `writes=True`, description: "Preview a new owner-account Google Sheet with typed, formatted
  tabs (headers, number/money/percent/date/bool/url columns, totals); non-writers get viewer
  access at minimum." Route it through `_create_or_append` (kind `create_workbook`) so the
  previews-off path runs it immediately, exactly like `create_sheet`. Add `"create_workbook"` to
  `action_handlers`. Add the tool to the `google_workspace` skill's tools, after
  `google_create_sheet`, and add the example `"make a spreadsheet comparing these hotels"`.
  Update `GOOGLE_INSTRUCTIONS` to say, in place of nothing else growing, that formatted or
  multi-tab tables use `google_create_workbook` and `google_create_sheet` is for plain rows.
  The constant must stay ≤ 800 characters; shorten other wording if needed without changing
  meaning, and keep the "Previews off" sentence.
- `gates.py` (**permission change; say so in your report**): `google_create_workbook` uses the
  `create` user gate, like `google_create_sheet`.

## 4. Service (`service.py`)

Add kind `create_workbook` alongside `create_doc`/`create_sheet`. Introduce a module constant
`CREATE_KINDS = frozenset({"create_doc", "create_sheet", "create_workbook"})` and use it in every
place that today lists the two create kinds (`propose`, `verify_commit`, `commit`), so the
create tier, the non-writer forced viewer link, the frozen-private refusal after demotion, the
daily-cap pre-check and `reserve_create` all apply to workbooks with no special case.

- `propose(ctx, "create_workbook", title=…, tabs=…, link_access=…)`: authorize (create tier),
  validate the title as today, compile with `compile_workbook` and render once with
  `render_workbook` (so every error happens now, before the preview). Link access resolves as
  for `create_sheet` (explicit choice, else private for writers; raised to `viewer` with
  `link_forced` for non-writers). Payload: `owner_email`, `owner`, `revision`, `title`,
  `mime` = SHEET, `link_access`, optional `link_forced`, and `tabs` (the validated input, deep
  copied). Summary: `Create Workbook: <title>`, the access line (same wording as create_sheet),
  `Owner: …`, `Target: new Sheet`, a blank line, then `preview_workbook(tabs)`. Long previews
  attach through `action()` as today.
- `commit`: after `verify_commit`, recompile and re-render from `payload["tabs"]` with the
  current settings (a `ValueError` here means limits changed; the plugin reports "not
  performed"). Then `await self.reserve(action, ctx)`, then
  `write_workbook(self.client, plan, on_created=record)` where `record` calls
  `repo.record_output(chat, file_id, SHEET, requester, action.thread_id, payload["owner"],
  action.id)`. Then, if `link_access` is viewer/editor, call `publish_sheet_link_access` even
  when the workbook is incomplete (the requester must be able to open it to check or remove it).
  Replies:
  - success: `Created <url> (<access> link access).` or `Created <url> (sharing unchanged).`
  - incomplete: `Created <url>, but the workbook is incomplete (<failure>). Its title ends with
    "(incomplete)". Nothing was retried; check or delete it, then request it again.` plus the
    sharing outcome.
  - sharing failed: `Created <url>, but <message>` as create_sheet does.
- `link_sharing.py`: `publish_sheet_link_access` accepts `create_workbook` as well as
  `create_sheet` (line ~324). Change nothing else there.

## 5. Docs and changelog

- `EXPLAIN.md`: a short section for `google_create_workbook`: what it builds, the column types,
  that text is never a formula and `SUM` totals are the only formulas, limits and their settings,
  incomplete-title behaviour, and that it follows the create tier, sharing and preview switch.
  Then run `uv run python .github/scripts/generate_explainers.py` to regenerate `docs/wiki/`
  (you may write the generated `docs/wiki/Plugin-google.md`).
- `CHANGELOG.md` under `Unreleased`: Added (the tool, the three cell kinds, `sum` `as`,
  `GOOGLE_OUTPUT_MAX_CHARS`); Security note that `google_create_workbook` uses the create tier
  (permission change in `gates.py`).

## Tests

All new tests use the existing fakes; none call Google.

- `test_workbook.py`: percent/bool/url cells render as specified; `fields` mask gains
  `dataValidation`/`textFormatRuns` only when those cells are present; url rejects `javascript:`,
  `ftp:`, `data:`, no-host, whitespace and over-long values; `sum` `as` number/percent formats and
  absent-`as` unchanged; `on_created` is awaited once before the first batch, and a raising
  `on_created` sends no batch.
- `test_workbook_golden.py` passes unchanged.
- New `test_workbook_builder.py`: a 3-tab spec with every column type (acceptance criterion 1):
  typed cells, header style and frozen rows, one banded filtered table per tab, `SUM` only in
  total columns; `=IMPORTXML("x")`, `-5`, `+65`, `@here` in text columns stay `stringValue`
  and the whole rendered plan contains no `formulaValue` other than `=SUM(`; every rejection
  (wrong type per column type, bool given as number, number given as bool, too many
  tabs/rows/columns, oversize argument, oversize cell, duplicate tab names case-insensitively,
  ragged row, unknown key, `total` on a text column, `decimals` on a non-money column, bad date)
  raises with the tab/row/column named; width estimates; heading/note layout; totals row omitted
  with zero rows; preview text.
- `test_service.py` / `test_plugin.py`:
  - propose → pending action with the summary; commit creates via the fake client, records one
    `google_outputs` row with the requester/thread/owner, and shares when requested.
  - Validation errors return a tool error and the fake client received no request.
  - Partial failure (fake `batch` raises on step 2): the reply contains the URL and
    "(incomplete)", an output row exists, and sharing still ran for a non-writer.
  - Non-writer under `create_users="members"`: forced viewer link, `link_forced` in payload,
    daily cap refuses at propose and at commit (`reserve_create`), and writer demotion with a
    frozen private payload is refused.
  - Writer-tier only (`create_users="writers"`): a non-writer is refused by the gate and by the
    service.
  - Previews off: `google_create_workbook` runs immediately (no pending action, fake client got
    the create); previews on: pending action.
  - Settings revision change between propose and commit refuses.
  - Lowering `GOOGLE_MAX_ROWS` between propose and commit refuses with no Google call.
  - The skill lists `google_create_workbook`; `GOOGLE_INSTRUCTIONS` ≤ 800 characters; the gate
    for the new tool is the create gate.
- Existing tests must pass. If the agent request snapshot
  (`packages/core/tests/snapshots/agent_requests_off.json`) or skill catalog tests change,
  stop and report instead of regenerating them (they should not: that snapshot doesn't load the
  google plugin).

Run `uv run pytest -q packages/plugins/google/tests` with `TMPDIR` pointed at a writable
`.tmp/` inside this worktree (delete it afterwards), plus
`uv run ruff check packages/plugins/google` and `uv run ruff format --check` on the files you
changed. Report counts.

## Done when

- Every PR 2 acceptance criterion in the master plan has a test that fails without the change.
- `test_workbook_golden.py` passes unchanged; existing expense and google tests pass.
- The request for every created workbook contains no `formulaValue` except generated `=SUM(`.
- Lint and format are clean on changed files; the explainer check
  (`uv run python .github/scripts/generate_explainers.py --check`) passes.
- The report lists the permission change in `gates.py`, the new setting, and any deviation.
