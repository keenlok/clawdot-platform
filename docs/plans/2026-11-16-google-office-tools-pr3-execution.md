# Google output tools PR 3: `google_create_rich_doc`

Execution plan for PR 3 (v0.23.0) of
[General Google Sheets and Docs output tools](2026-11-16-google-office-tools.md). Read that plan's
"Owner decisions", "Design" (Permissions, Output journal, Confirm switch, Link sharing, **PR 3**)
and the PR 3 acceptance criteria first, then `AGENTS.md` ("Plugin module layout", "Skills",
"Changelog"). PRs 1 and 2 are already on this branch
([PR 1 plan](2026-11-16-google-office-tools-pr1-execution.md),
[PR 2 plan](2026-11-16-google-office-tools-pr2-execution.md)): the create tier, `CREATE_KINDS`,
`google_outputs`, `reserve_create`, the preview switch and `google_create_workbook` exist.
Reuse them.

Read these files before changing anything:
`packages/plugins/google/src/chatcore_google/{service.py,plugin.py,gates.py,link_sharing.py,doc_verification.py,edit_snapshot.py,render.py,client.py,repository.py,settings.py,schemas.py,workbook_builder.py,EXPLAIN.md}`
and the tests in `packages/plugins/google/tests/` (especially `test_service.py`, `test_plugin.py`,
`test_edit_service.py`, `test_client.py`, `conftest.py`). Study `GoogleService._create_output_doc`
closely: the new tool reuses its journal, validation, verification and sharing steps.

## You own

- `packages/plugins/google/src/chatcore_google/`: new `rich_doc.py`, `service.py`, `plugin.py`,
  `gates.py`, `link_sharing.py`, `client.py` (one new method), `schemas.py`, `EXPLAIN.md`
- `packages/plugins/google/tests/**` (edit and add; `test_workbook_golden.py` and its fixture
  stay unchanged)
- `CHANGELOG.md` (entries under `Unreleased`)

You may **not** edit: `packages/core/**`, other plugins, `apps/**`, `repository.py`,
`edit_service.py`, `edit_snapshot.py`, `doc_verification.py`, `workbook.py`,
`workbook_builder.py`, any migration, `pyproject.toml`, `uv.lock`. **No migration**: the result
journal (`google_edit_results`) records a rich Doc under the existing journal kind `create_doc`
(output mode `create`, no source), which is exactly what it is. Report if one must change.

## 1. Block model and validation (`rich_doc.py`, no I/O)

`compile_doc(blocks, settings) -> DocPlan` validates the tool's `blocks` argument and returns a
frozen plan; it raises `ValueError` before any write. `preview_doc(blocks) -> str` renders the
preview body.

```json
[{"type": "title", "text": "Kyoto day plan"},
 {"type": "heading", "level": 1, "text": "Day 1"},
 {"type": "paragraph", "spans": [{"text": "Meet at "}, {"text": "Kyoto Station", "bold": true},
                                  {"text": " map", "link": "https://maps.example"}]},
 {"type": "bullets", "items": ["Fushimi Inari", "Lunch"]},
 {"type": "numbered", "items": ["Buy pass", "Board bus 5"]},
 {"type": "table", "header": ["Time", "Place"], "rows": [["09:00", "Inari"]]},
 {"type": "divider"}]
```

- Block types: `title` (TITLE style), `heading` (`level` 1–3 → HEADING_1..3), `paragraph`,
  `bullets`, `numbered`, `table`, `divider`.
- `title`, `heading` and `paragraph` take exactly one of `text` (plain string) or `spans` (list
  of `{text, bold?, italic?, underline?, code?, link?}`, 1–100 spans, each `text` nonempty).
  `bullets`/`numbered` take `items`: 1–100 entries, each a string or a span list. `table` takes
  `header` (1–8 strings) and `rows` (0–50 rows, each exactly `len(header)` strings or `null`).
  `divider` takes nothing.
- Text anywhere: reject `\n`, `\r`, `\v`, `\f`, U+2028/U+2029 and every other control
  character (Unicode category `Cc`); each block or item is its own paragraph. Each table cell ≤
  `settings.max_cell_chars`.
- `link`: string ≤ 2000 characters, scheme `http`/`https` with a host, no whitespace or control
  characters (same rule as the workbook `url` cell; reuse
  `chatcore_google.workbook`'s check by calling its URL validation, or factor the identical
  rule into `rich_doc.py` without editing `workbook.py`).
- Limits: whole argument `len(json.dumps(blocks, ensure_ascii=False, separators=(",", ":")))`
  ≤ `settings.output_max_chars` (check first); 1–200 blocks; ≤ 10 tables; total text (all spans,
  items and cells) ≤ `settings.max_chars`. Unknown keys anywhere are rejected.
- Every error names the location: `Block 4 (bullets), item 2: …`, `Block 6 (table), row 3,
  column "Place": …` (blocks and rows 1-based).

## 2. Rendering (`rich_doc.py`, no I/O)

The new Doc is empty (body = one empty paragraph at index 1). Build, with UTF-16 offsets
(`edit_snapshot.utf16_length`):

- **Body text:** one paragraph per block or list item, joined with `\n`, inserted by one
  `insertText` at index 1. A `table` block contributes one empty placeholder paragraph (the
  table is inserted there later). A `divider` contributes one empty paragraph.
- **Batch A** (one `batchUpdate`, no revision precondition needed on a doc we just created):
  1. the `insertText`;
  2. `updateParagraphStyle` with `namedStyleType` for every paragraph (`TITLE`, `HEADING_n`,
     `NORMAL_TEXT` for everything else), `fields: "namedStyleType"`;
  3. for dividers, `updateParagraphStyle` with a `borderBottom` (solid, 1pt, grey) on that
     empty paragraph, `fields: "borderBottom"`;
  4. `updateTextStyle` per marked span: `bold`, `italic`, `underline`, `code` →
     `weightedFontFamily {"fontFamily": "Roboto Mono"}`, `link` → `link {"url": …}`; `fields`
     lists exactly the properties set;
  5. `createParagraphBullets` per list block over its paragraphs:
     `BULLET_DISC_CIRCLE_SQUARE` or `NUMBERED_DECIMAL_ALPHA_ROMAN`.
  Bullets go last in the batch because nothing after them depends on indices.
- **Batch B** (only if there are tables): `insertTable` (`rows` = 1 + data rows, `columns` =
  header length) at each placeholder paragraph's start index, **last table first**, so earlier
  indices never shift.
- **Batch C** (only if there are tables): read the document once with
  `client.document_for_edit`, take its `revisionId`, find the body's tables in order (there must
  be exactly as many as planned, each with the planned shape, or stop as a partial failure), and
  fill every non-empty cell from the **last cell of the last table backwards**: `insertText` at
  the cell's first paragraph `startIndex`, plus `updateTextStyle` bold for header cells over the
  inserted range. Send it with `client.edit_document(file_id, requests, revision_id)` so a
  concurrent change refuses the write. Never predict table cell indices; always use the
  read-back.
- `DocPlan` holds: batch A requests, the table specs (placeholder index, header, rows), and the
  **expected structure** for verification: the ordered list of
  `("paragraph", text, named_style)` and `("table", [[cell text, …], …])` items, plus the
  expected bullet/numbered paragraph texts.

## 3. Verification (`rich_doc.py`)

`verify_rich_doc(document, plan) -> (text_ok: bool, warnings: list[str])` from one
`document_for_edit` read (retry the read itself at most 3 times in 5 seconds, as
`verify_doc_readback` does; never repeat a mutation):

- Walk the body. Paragraphs outside tables must match the expected paragraphs in order by text.
  The only tolerated extra paragraphs are empty, unstyled `NORMAL_TEXT` paragraphs directly
  before or after a table (Docs adds these around inserted tables). Tables must match shape and
  cell text exactly. Any text difference, missing/extra content or suggestions → `text_ok`
  false.
- Formatting warnings (text still verified): a paragraph whose `namedStyleType` differs from
  plan, a list paragraph without a `bullet`, a link/bold span whose style didn't read back.
  Warnings are short, content-free category strings ("heading style", "list bullets", "links").

## 4. Service integration (`service.py`, `link_sharing.py`, `client.py`)

New action kind `create_rich_doc`, added to `CREATE_KINDS`, so the create tier, forced viewer
link for non-writers, daily cap pre-check and `reserve_create`, writer-demotion refusal and
revision/owner checks apply unchanged.

- `propose(ctx, "create_rich_doc", title=…, blocks=…, link_access=…)`: title rule as today;
  compile the blocks **before** `client.identity()` (local validation first, as PR 2 did).
  Access resolves exactly as for `create_doc` (`resolve_link_access`, raised to viewer with
  `link_forced` for non-writers; no `link_viewer` argument). Payload: `owner_email`, `owner`,
  `revision`, `title`, `mime` = DOC, `link_access`, optional `link_forced`, `blocks` (deep copy).
  Summary: `Create Rich Doc: <title>`, the access line, `Owner`, `Target: new Doc (<access>
  link access)`, blank line, `preview_doc(blocks)`.
- `commit`: recompile from `payload["blocks"]` with current settings **before**
  `verify_commit`, then run the shared new-Doc path. Generalise `_create_output_doc(action, ctx,
  payload)` so the journal kind passed to `reserve_edit` / `claim_destination_creation` stays
  `"create_doc"` for both action kinds, and only two steps differ for `create_rich_doc`:
  - the create request uses title `payload["title"][:186] + " (incomplete)"`;
  - the content step runs batches A, B and C instead of `append_doc`, and verification uses
    `verify_rich_doc` instead of `verify_doc_readback`. Map results onto the existing journal
    phases (`applying` before batch A; `partial` / `uncertain` with `failed_stage`
    `content_attempt` or `content_verification` exactly as the plain path does).
  - After text verification passes: rename to the final title with a new
    `client.rename_file(file_id, name)` (Drive `PATCH /drive/v3/files/{id}` with
    `{"name": name}`, `fields=id,name`, `supportsAllDrives=true`); a rename failure is reported
    as partial ("content verified, title still ends with (incomplete)"), still shares.
  - Then share exactly as the plain path does (`publish_doc_link_access`), then mark
    `verified`. Text-verified with formatting warnings is a success whose reply adds
    "Formatting could not be fully verified: <categories>." Text not verified: the existing
    partial/uncertain reply, no sharing (same as `create_doc` today).
  - The plain `create_doc` path's requests and replies must not change (its existing tests are
    the check).
- `link_sharing.publish_doc_link_access`: treat `create_rich_doc` like `create_doc` (expected
  journal kind `create_doc`, no source). Change nothing else there.

## 5. Tool, gate, skill, docs

- `schemas.py`: `BLOCKS` JSON schema (array of block objects, `type` enum, the fields above,
  `additionalProperties: false` where JSON schema can express it). Short descriptions.
- `plugin.py`: tool `google_create_rich_doc(title, blocks, link_access=None)`, `writes=True`,
  description "Preview a new owner-account Google Doc with headings, formatted text, lists,
  tables and links; non-writers get viewer access at minimum." Route through
  `_create_or_append` (immediate when previews are off). Add `"create_rich_doc"` to
  `action_handlers`. Add the tool to the `google_workspace` skill after `google_create_doc`, and
  the example `"write up this itinerary as a Google Doc"`. In `GOOGLE_INSTRUCTIONS` say that
  structured documents use `google_create_rich_doc` and `google_create_doc` is plain text;
  keep the constant ≤ 800 characters and keep the "Previews off" sentence.
- `gates.py` (**permission change; say so in your report**): `google_create_rich_doc` uses the
  `create` gate.
- `EXPLAIN.md`: a short "Rich Doc output" section (blocks, marks, limits, links http(s) only,
  "(incomplete)" until verified, verified text vs formatting warnings, same create tier,
  sharing and preview switch). Regenerate the wiki with
  `uv run python .github/scripts/generate_explainers.py`.
- `CHANGELOG.md` under `Unreleased`: Added (the tool, `client.rename_file`); Security
  (create-tier gate).

## Tests

All new tests use fakes; none call Google. Build a fake Docs document helper that applies the
recorded requests well enough to produce a realistic read-back (body content with
`startIndex`/`endIndex`, `paragraphStyle.namedStyleType`, `bullet`, `textStyle`, tables with
cells), including Docs' extra empty paragraph before an inserted table, or hand-write read-back
fixtures for the specific documents under test.

- `test_rich_doc.py`:
  - a doc with every block type renders the expected requests: one `insertText` with the exact
    text; named styles per paragraph; bold/italic/underline/code/link `updateTextStyle` ranges
    in UTF-16 units; bullets and numbering ranges; divider border; tables inserted last-first at
    placeholder indices;
  - non-BMP text (emoji, `𠀋`) before and between **multiple tables** gives correct UTF-16
    ranges and placeholder indices (acceptance criterion);
  - batch C fills cells last-to-first from read-back indices with header bold, and refuses
    (partial) when the read-back has the wrong number or shape of tables;
  - rejections, each naming the location: `javascript:`/`data:`/`ftp:`/hostless links, newline
    or control characters in text, too many blocks/tables/rows/columns/items/spans, oversize
    argument, over `max_chars`, oversize cell, both `text` and `spans`, unknown keys, bad
    heading level, ragged table row;
  - `verify_rich_doc`: exact match passes; tolerated empty paragraphs around tables pass; a
    changed cell, missing paragraph or extra text fails; a wrong heading style passes text with
    a warning.
- `test_service.py` / `test_plugin.py`:
  - propose → pending action with preview; commit creates with the "(incomplete)" title, runs
    A/B/C, verifies, renames, journals one `google_outputs` row and one `google_edit_results`
    row of kind `create_doc` reaching phase `verified`, and shares when requested;
  - validation errors return a tool error with `client.identity` **not** awaited and no request;
  - verification failure: partial reply, no rename, no sharing; formatting-only mismatch:
    success with the warning; rename failure: partial reply that still shares;
  - non-writer under `create_users="members"`: forced viewer link; cap at propose and at commit;
    writer demotion with a frozen private payload refused; `create_users="writers"` refuses a
    non-writer at the gate and in the service;
  - previews off: immediate execution, no pending action; previews on: pending;
  - lowering `max_chars` between propose and commit refuses with no Google call;
  - the skill lists `google_create_rich_doc`; `GOOGLE_INSTRUCTIONS` ≤ 800 characters; the new
    tool's gate is the create gate;
  - existing `create_doc` tests pass unchanged.
- `test_client.py`: `rename_file` sends the PATCH with the right path, params and body.

Run `uv run pytest -q packages/plugins/google/tests` with `TMPDIR="$PWD/.tmp"` (create it,
delete it afterwards), `uv run ruff check packages/plugins/google`, `uv run ruff format --check`
on changed files and `uv run python .github/scripts/generate_explainers.py --check`. Report
counts.

## Done when

- Every PR 3 acceptance criterion in the master plan has a test that fails without the change.
- No request for a rich Doc contains a link that isn't http(s), and no table cell index is
  predicted rather than read back.
- `create_doc` requests and replies are unchanged; the golden workbook test passes.
- Lint, format and the explainer check are clean.
- The report lists the permission change in `gates.py`, the new client method and any
  deviation (especially anything about Docs table index behaviour you had to assume).
