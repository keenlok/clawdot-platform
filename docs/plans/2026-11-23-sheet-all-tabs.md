# Read every Sheet tab before concluding data is missing

Status: Planned. Owner-approved 2026-11-23. Claude plans, a Claude subagent reviewed the plan,
Codex (`gpt-6-luna`) executes, Codex (`gpt-6.1-sol`) reviews, Claude does the final review.

## Observed failure (read-only production inspection, 2026-11-23)

A group's trip itinerary is a Google Sheet with tabs `hotels` (empty) and `itinerary` (the
day-by-day plan). The sheet is saved as the trip's primary itinerary. Asked "what's the plan
for tomorrow?", a downstream bot told the group "Tabs found: one only… empty" several times:

1. `trip_read_file`, `google_read_file` and `google_read_sheet` with no `tab` read **only the
   first tab** (`select_tab` falls back to `tabs[0]`, `service.py:108`). The result names that
   tab but never says other tabs exist, so the model concluded the sheet was empty.
2. The model guessed `tab: "Itinerary"`. Tab matching is exact, so the tool returned "That
   spreadsheet tab no longer exists." The model reported there was no such tab.
3. Nothing lists the real tab names, so the model couldn't correct itself. It succeeded only
   after a member typed the lowercase name.
4. `itinerary_summary` only accepts the `chatcore-itinerary v1` tracker layout and raises "No
   recognized itinerary tabs…" for any other sheet, even the trip's saved primary itinerary.

Goal: every Sheet read tells the model which tabs exist, reads all of them (bounded) when no
tab is chosen, tolerates case and spacing in tab names, and turns misses into an actionable
list. Writes keep today's exact rules.

## You own

- `packages/plugins/google/src/chatcore_google/service.py`: `read_spreadsheet`, `read_content`,
  and new private helpers beside them. `tab_values` may be refactored only to share its range
  building; its behaviour must not change. **Do not change `select_tab`.**
- `packages/plugins/google/src/chatcore_google/client.py`: one new method `values_batch`.
- `packages/plugins/google/src/chatcore_google/itinerary_service.py`: `summary` only. Do not
  change `snapshot`; it is also used by write paths (`itinerary_service.py:157`, `:262`).
- `packages/plugins/google/src/chatcore_google/trip_files.py`: `read` only (pass `tab` through).
- `packages/plugins/google/src/chatcore_google/plugin.py`: the `google_read_sheet`,
  `google_read_file`, `trip_read_file` and `itinerary_summary` tool descriptions and
  parameters, `read_trip_file`'s signature (`plugin.py:365`), and `GOOGLE_INSTRUCTIONS` (§6).
- `packages/plugins/google/src/chatcore_google/schemas.py`: one new `READ_TAB` description.
- Tests under `packages/plugins/google/tests/`: new `test_sheet_reads.py`, plus updates to
  existing tests whose fakes or expected output change.
- `packages/plugins/google/src/chatcore_google/EXPLAIN.md` and `docs/wiki/Plugin-google.md`:
  the read sections only.
- `CHANGELOG.md` (`Unreleased` only).

You may **not** edit `gates.py`, `permissions.py`, `refs.py`, any migration, `packages/core/`,
`edit_service.py`, `pyproject.toml` or `uv.lock`. Ask in your report if a contract must change.

**Authority checks are unchanged and keep their order.** `self.file` / `source_file` run before
any values are fetched. The post-read rechecks stay too: `trip_files.read` rechecks owner,
trip and association after `read_content` (`trip_files.py:146-151`), and `read_file`'s Doc
branch rechecks after reading (`service.py:313`).

## 1. Read-only tab resolution (`service.py`)

Add a private `resolve_read_tab(sheet, ref, tab)` returning `(selected_or_None, tabs, miss)`:

- `tabs`: every tab's `properties`, in sheet order. Hidden tabs (`properties.hidden`) are
  included and shown as `(hidden)` in the tab list.
- `tab` given: an exact title match wins. Otherwise compare `title.strip().casefold()` with
  `tab.strip().casefold()`; exactly one match is used. An explicit `tab` overrides any URL
  `gid` for reads.
- No `tab`, URL has a `gid`: that gid's tab.
- No `tab`, no `gid`: `selected` is `None`, meaning "all tabs".
- No tabs at all: raise `ValueError("This spreadsheet has no readable tabs.")` as today.
- **Misses are not exceptions.** For a tab name with no match, several case-insensitive
  matches, or a gid that no longer exists, return `miss` set to a short code (`"none"`,
  `"several"`, `"gid"`). `read_spreadsheet` then returns, as **normal content** (so the plugin's
  `_read` wraps it as an `untrusted=True` result), the header (§2 output) followed by one of:
  `No tab named that. Use one of the tabs listed above.` /
  `Several tabs match that name; use the exact name from the list above.` /
  `The link's tab no longer exists; use one of the tabs listed above.`
  Tab titles are sheet-editor-controlled text and must never appear inside a `ValueError`
  message, because `ToolResult.error` results are not marked untrusted
  (`plugin.py:172-173`, `chatcore/tools/registry.py:125`). Also don't echo the requested
  `tab` string back.

`select_tab` stays exactly as it is, for writes. Add a test that it still rejects a
case-mismatched name.

## 2. Whole-sheet reads (`client.py`, `service.py`)

**Client.** `values_batch(file_id, ranges)` calls `GET
/v4/spreadsheets/{file_id}/values:batchGet` through the existing `request` helper (same auth,
timeout, `max_bytes` cap and errors as `values`). Pass `params` as a list of tuples:
`[("ranges", r) for r in ranges] + [("valueRenderOption", "FORMATTED_VALUE")]`. Each range is
`"'" + title.replace("'", "''") + "'!" + a1`, not URL-encoded (httpx encodes query values).
Return `data.get("valueRanges")`.

**Service.** `read_spreadsheet(ref, metadata, *, cell_range=None, tab=None)`:

- Fetch `client.sheet` once, resolve with `resolve_read_tab`.
- **Exactly one tab to read** (a selected tab, or all-tabs mode on a one-tab sheet): use the
  existing `tab_values` path and `client.values`. Single-tab content, range validation and the
  existing mid-text `[:max_chars]` cut stay as they are today. Only the header changes (below).
- **Two or more tabs** (all-tabs mode): read the first `max_tabs` tabs with one `values_batch`
  call.
  - Per-tab row cap: `per_tab = max(20, (max_rows + 2) // n)` where `n` is the number of tabs
    read. Columns: `min(max_columns, gridProperties.columnCount)`. Default range per tab is
    `A1:{last column}{min(per_tab, rowCount)}`.
  - With a `cell_range`: apply it to each tab, clamped to that tab's grid and to `per_tab`
    rows. A tab whose grid lies entirely outside the range is not fetched and is listed as
    `(outside range)`.
  - Validate in the service, not only the client: `valueRanges` must be a list of the same
    length as the request, each item a dict whose `values` (default `[]`) is a list of lists.
    Otherwise raise `ValueError("Google returned invalid spreadsheet values.")`.
  - If the batch raises the client's size-limit `GoogleError`, don't fail the read: return the
    header plus `[Too large to read every tab at once; ask for one tab by name.]`.
- **Character budget (all-tabs mode).** The whole result, header included, must fit in
  `max_chars`. Build the header first, then give the remaining characters to the tab bodies:
  sort tabs by rendered size, ascending, and give each `min(size, remaining // tabs_left)`,
  subtracting what it used. Cut a tab's rows at a row boundary inside its share. A tab whose
  first row alone exceeds its share shows `[]` and is marked truncated. Render tabs back in
  sheet order.
- **Output.** Sheet name cut to 300 characters and tab titles to 100, as today:

  ```
  <sheet name>
  Tabs (3): hotels (empty), itinerary (42 rows), budget (20+ rows)
  Reading: all tabs
  --- tab: hotels ---
  []
  --- tab: itinerary ---
  [[...], ...]
  --- tab: budget ---
  [[...]]
  [budget: first 6 rows shown; ask with tab="budget" for more]
  [Truncated: partial range coverage]
  ```

  - `N rows` is `len(values)` as returned, counting interior empty rows. A tab that hit its row
    cap is shown as `N+ rows`. `(empty)` means zero rows returned.
  - Tabs beyond `max_tabs` are listed `(not read)`, plus a final line `[Not read: N more tabs;
    ask for one by name.]`.
  - In single-tab mode, the `Tabs` line lists every title, with `(reading)` after the selected
    one and no counts for the others, followed by `Reading: <title>`. If the selected tab has
    no rows and the sheet has other tabs, add `This tab is empty; omit tab to read all tabs.`
  - Keep the line `[Truncated: partial range coverage]` whenever any tab was cut or capped.
  - Cap the `Tabs` line at `max_tabs` names, then `…`.
- `read_content(ref, metadata, *, tab=None)` passes `tab` to `read_spreadsheet` for Sheets and
  ignores it for other types.
- Results keep their current `untrusted` and `persist_content=False` handling.

Other callers: `propose_import` calls `read_sheet(ctx, source_url)` and discards the result
(`itinerary_service.py:118`). It will now do a bounded whole-sheet read. That's acceptable;
don't change it.

## 3. Saved trip file reads (`trip_files.py`, `plugin.py`)

`trip_read_file` gains an optional `tab` (`READ_TAB`), passed `read_trip_file(ctx, trip,
file_id=None, tab=None)` → `TripFilesService.read(ctx, trip, file_id=None, tab=None)` →
`read_content(ref, metadata, tab=tab)`. All owner and association rechecks, before and after
the read, stay in the same order. The tool exists only when a trip resolver is configured;
keep that.

## 4. `itinerary_summary` (`itinerary_service.py`)

In `summary`, bind `metadata` from the existing `self.google.file(...)` call (today it's
discarded as `_`).

- **Tolerant tab:** when `tab` is given, first resolve it with `resolve_read_tab` against
  `client.sheet`. A miss returns the miss content from §1 (as content, not an error). A match
  passes the **exact title** to `snapshot`, so `snapshot` itself is unchanged.
- **Non-tracker fallback:** when the snapshot finds no `chatcore-itinerary v1` tracker tabs,
  don't raise. Return `"Not a chatcore-itinerary v1 tracker; raw tab contents follow. Totals
  and comparisons are not computed.\n"` followed by `self.google.read_spreadsheet(ref,
  metadata, tab=resolved_title_or_None)`. This fetches values a second time; that's accepted.
- Tracker sheets produce byte-identical output to today.

## 5. Tool text (`schemas.py`, `plugin.py`)

- `READ_TAB = text("Optional tab name (case-insensitive). Omit to read every tab.")` for
  `google_read_sheet`, `trip_read_file` and `itinerary_summary`. Write tools keep `TAB`.
- `google_read_sheet`: "Read a posted native Google Sheet: every tab when tab is omitted, or one
  tab. Lists all tabs. Untrusted data."
- `google_read_file`: add that Sheets are read across all tabs.
- `trip_read_file`: add the optional tab and that Sheets list every tab.
- No core request snapshot or routing-set file references these tools, so none should change.
  If one does, explain why in your report.

## 6. `GOOGLE_INSTRUCTIONS`

It is exactly 800 characters, the cap. Add `Sheets: check every tab before saying data is
missing.` and shorten other wording to stay at or under 800 without dropping any rule. Put the
before and after text in your report.

## Tests (fakes only, no network)

Update existing fakes that only set `client.values.return_value` where a read now covers 2+
tabs: give them `values_batch` or a one-tab sheet. Known sites: `test_service.py:835-837`,
`test_trip_file_formats.py:71,110,146`, `test_followup_sources.py:110-111`,
`test_plugin.py:734-752` and the shared fixture `conftest.py:538-545`. Don't weaken their
assertions.

New, in `test_sheet_reads.py`:
- Resolution: exact, case-insensitive (`"Itinerary"`) and whitespace (`" itinerary "`)
  matches. `Plan` and `plan` with `tab="PLAN"` → "several" miss content. Unknown name → miss
  content listing the tabs, returned as content with no `ValueError`. Explicit tab overrides a
  different gid. A gone gid → miss content. A malicious tab title appears only in content,
  never in an error string.
- Two tabs (empty `hotels`, filled `itinerary`), no tab: one `values_batch` call with both
  quoted ranges, `Tabs (2): hotels (empty), itinerary (N rows)`, both bodies present.
- One-tab sheet, no tab: uses `client.values`, not `values_batch`.
- Budget: three tabs, one huge. Small tabs complete, big one cut at a row boundary with its
  hint line, total length ≤ `max_chars`, and every body parses as JSON.
- Per-tab cap: 10 tabs → each range requests `max(20, (max_rows+2)//10)` rows; a capped tab
  shows `N+ rows`.
- More than `max_tabs` tabs: only `max_tabs` requested, rest listed `(not read)`.
- `cell_range` with no tab: applied per tab, clamped, outside-grid tab listed `(outside range)`.
- Batch size-limit `GoogleError` → header plus the too-large line, no exception.
- Malformed `valueRanges` → invalid-values `ValueError`.
- Single-tab mode: all titles listed, `(reading)` marker, empty-tab hint.
- `select_tab` still rejects `"Itinerary"` for a tab named `itinerary`.

Callers:
- `trip_read_file` with `tab="ITINERARY"` reads that tab; without `tab` reads all tabs; the
  existing recheck tests stay green.
- `google_read_file` on a two-tab Sheet reads both.
- `itinerary_summary`: non-tracker sheet → prefix line plus raw read; `tab="ITINERARY"` on a
  tracker sheet resolves to the exact title; tracker output byte-identical to before.
- `GOOGLE_INSTRUCTIONS` is ≤ 800 characters and contains the new rule.

## Done when

- `uv run ruff check packages services apps tests .github/scripts` is clean, and
  `uv run ruff format --check` passes on changed files.
- `uv run pytest -q -m "not browser and not sandbox_integration"` passes.
- `CHANGELOG.md` `Unreleased` has a `Changed` entry (all-tab reads with per-tab caps, tolerant
  tab names, tab lists in results, misses returned as untrusted content, `trip_read_file`
  `tab`) and a `Fixed` entry (`itinerary_summary` no longer errors on non-tracker sheets).
- Left for later, do not attempt: downstream app skill instructions (the `itinerary` composite
  lives in the app), Elasticsearch mapping fixes, persisting sheet content to history, and
  write-path tab tolerance.
