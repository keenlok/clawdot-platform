# Formatted expense workbook export

Status: Approved by the owner 2026-11-15; implemented, pending review and release.
Execution: planned, implemented and self-reviewed in a single Claude session (owner chose the
`implement-project` workflow, which replaces the Codex execute/review split for this work).

## Problem

`expenses_export_to_sheet` turns the CSV export into rows and sends them through the generic
`create_sheet` path. The result is one raw tab of text cells (amounts are `stringValue`), with
trip metadata repeated on every row, and the generic 300-row limit makes the export fail for
any ledger above roughly 300 expense + share + settlement rows. People then ask the model to
rebuild a readable sheet by hand, which is slow and error-prone.

## Outcome

A confirmed export creates one private, formatted, multi-tab Google Sheet computed
deterministically in code from one consistent ledger snapshot. The model only chooses the trip
(or general ledger) and an optional title.

## Acceptance criteria

| ID | Criterion |
| --- | --- |
| A1 | **Summary** tab: trip name, dates, snapshot time, expense/repayment counts; per person paid upfront, allocated share, repayments sent/received, rounding adjustment (convert mode) and net; "Owed to you" / "You owe" / "Settled"; original-currency totals per person in separate columns. |
| A2 | **Transactions** tab: one row per current expense sorted by `spent_on`, `created_at`, `id`; date, description, payer, currency, original amount, stored FX rate, settlement equivalent, FX status; share columns per participant labelled original / settlement currency. No trip metadata or internal IDs. |
| A3 | One **person tab** per participant (payer, share holder or repayment party): summary block, then chronological expenses they paid or share in with full bill, payer, personal share, paid upfront and settlement equivalents, then their recorded repayments. Participants derived from data, keyed by stable user key; tab names are safe and unique. |
| A4 | **Repayments** tab: suggested transfers (from canonical balances) and recorded repayments in clearly separated, labelled tables. |
| A5 | **Raw ledger** tab: every expense, share and repayment record with stable IDs, user keys, minor units and the exact stored FX rate. |
| A6 | Numbers and dates are native typed cells. Money shows at the currency's precision (JPY 0, SGD 2, KWD 3); stored values keep full calculation precision; FX rates keep stored precision. |
| A7 | Bold banded headers, frozen header rows, basic filter on each main table, alternating row colours, sensible column widths, wrapped descriptions. |
| A8 | Totals use `SUM` formulas generated only from fixed templates; every tab states it is a snapshot that does not update from the ledger. |
| A9 | Balances, FX conversion and rounding reuse `ledger_balances` / `convert_balances` / `simplify_balances`, including the group rounding adjustment; summary nets equal those used by the balance tools. |
| A10 | Convert mode and per-currency mode both supported. Expenses without an FX rate are listed, flagged and excluded from settlement-currency figures with an explicit "excludes N expenses" label; original-currency totals still include them. |
| A11 | Deleted expenses are excluded. No ledger write happens anywhere in the export path; suspected duplicates are not removed. |
| A12 | Ledger reads for one export happen in one read-only repeatable-read transaction. |
| A13 | Authorization, chat/DM scoping, requester confirmation, Google owner checks, private ownership and "sharing unchanged" behaviour are preserved. |
| A14 | Descriptions and names are always written as literal string cells (existing `neutralize` rule); numbers are never written as strings. |
| A15 | No silent truncation: the export is bounded by a workbook cell cap with a clear error; Sheets writes are batched by size and count. |
| A16 | Partial failure is reported honestly with the file link, and the file keeps an "(incomplete)" title; one confirmation creates at most one workbook (existing reservation); no automatic retry. |
| A17 | Legacy provider (CSV only) and already-pending legacy confirmations keep working. |
| A18 | sb_bot (clawdot-deployments) pins the release, wires the workbook provider and updates its skill text and tests. |

## Decisions

- **D1 Latest data at confirmation (owner decision).** The preview is built when proposed (counts,
  per-person nets, tab list) and states that the ledger is read again on confirmation. Commit
  re-resolves the same trip ID or general scope, re-reads one snapshot and writes it. The
  confirmation reply reports the counts actually written and the snapshot time.
- **D2 Split of responsibilities.** `chatcore_expenses.workbook` (pure) builds a neutral,
  JSON-shaped workbook model from ledger rows. `chatcore_google.workbook` (pure) validates that
  model and renders Sheets API requests. Neither plugin imports the other; the app connects them
  through a new optional `GooglePlugin(expenses_workbook_provider=...)`, wired to
  `ExpensesPlugin.sheet_export_workbook`. Without it, the CSV path is unchanged.
- **D3 Model contract (`schema: 1`).** Sheets have rows of cells
  `{"k": kind, "v": value, "s": style}` with kinds `text`, `number`, `money` (with `exp`),
  `rate`, `date`, `datetime`, `sum` (column range only); styles `title`, `note`, `header`,
  `total`, `warn`, `wrap`, `pos`, `neg`; plus `widths`, `frozen_rows`, and `tables` (header row,
  row span, column count, `filter`). The renderer accepts nothing else, so no free-form formula
  can be produced.
- **D4 Precision.** Money is written as numbers converted from exact `Decimal` and formatted per
  currency exponent; settlement equivalents are the exact product (not pre-rounded). The Raw
  ledger also carries integer minor units and the exact rate as text.
- **D5 Write strategy.** Create the spreadsheet with every tab's properties and the title
  "<title> (incomplete)"; write cell data with `updateCells` in batches of at most 1.5 MB and
  500 requests; a final batch adds banding, filters, widths, merges and renames to the final
  title. A failure after creation returns the link and says the file is incomplete and was not
  retried.
- **D6 Limits.** New `GOOGLE_EXPORT_MAX_CELLS` (default 200 000, max 2 000 000) and a fixed cap
  of 60 participant tabs. Above either, the export is refused with a clear message.
- **D7 Tab names.** Fixed tabs `Summary`, `Transactions`, `Repayments`, `Raw ledger`; person tabs
  from display names with control and `'[]*?/\:` characters removed, at most 50 characters,
  deduplicated case-insensitively against fixed and other tabs with ` (2)` suffixes; unknown
  members keep the existing `Former member N` labels.
- **D8 Snapshot reads.** New `LedgerRepo.export_snapshot(chat, trip_id, general_only)` returns
  trip, expenses and settlements inside one `REPEATABLE READ READ ONLY` transaction (Postgres) and
  is used by the CSV export, the provider and the workbook.

## Steps

1. Repository snapshot (A11, A12): `export_snapshot` on protocol, memory and Postgres repos;
   CSV export and `sheet_export_data` use it. Tests: memory + Postgres integration.
2. Workbook builder (A1–A6, A8–A11, A14): `chatcore_expenses/workbook.py` plus
   `ExpensesPlugin.sheet_export_workbook`, exported from the package. Tests: full export,
   participant selection and tab names, rounding adjustment equality with `convert_balances`,
   missing FX, per-currency mode, ordering, deleted exclusion, injection text kept as text.
3. Renderer (A6–A8, A14–A16): `chatcore_google/workbook.py` validate/render/batch. Tests:
   typed cells, formats, formula templates only, request batching bounds, cell cap.
4. Service and plugin (A13, A15–A17): `ExpenseService.propose_export`/`commit` workbook branch,
   new setting, tool description, legacy compatibility, partial-failure message. Tests: scope
   recheck, reservation prevents duplicate, partial failure, legacy payload commit, large ledger
   (> 300 rows) succeeds.
5. Cross-package contract test in `tests/` rendering the real builder output.
6. Native verification: render a synthetic multi-currency workbook locally, send the request
   JSON to the production host and run it in the bot container with the mounted owner OAuth
   profile (credential never leaves the host) to create one private test workbook; read back
   types, formats and values.
7. Docs: `EXPLAIN.md` for both plugins, `docs/plans/README.md` row, `CHANGELOG.md`, version
   bump to 0.18.0.
8. Self-review, required checks, platform PR.
9. clawdot-deployments draft PR: pin v0.18.0, wire provider, skill instruction text, routing and
   app tests, deployment plan record. Goes green after the owner merges and tags the release.

## Non-goals

- No syncing or updating of existing workbooks; every export is a new private file.
- No low-level spreadsheet tools for the model; no sharing changes.
- No changes to financial records, balance rules or FX lookup.
- No production deployment (separate authorization).

## Implementation record

Changes made during implementation, with reasons:

- **Literal text without the apostrophe prefix (A14).** The first native test showed that the
  generic `neutralize` prefix is displayed in `stringValue` cells (`'-5 taxi`). The read-back also
  showed that `=HYPERLINK(...)` sent as `stringValue` stays a string with no formula. Workbook text
  is therefore written as plain `stringValue`. The generic sheet tools and the CSV export keep
  their existing prefix rule.
- **Blank display names** become "Unnamed member N" instead of an empty or generic label; keys
  without a current member keep "Former member N" as in the CSV export.
- `_export` passes its already-resolved chat to the shared scope helper, so an app's DM group
  resolver is called once per export, as before.

Native verification (step 6), 2026-11-15: a synthetic 12-expense, four-currency ledger (HKD, CNY
with a 19-digit rate, JPY, SGD; one expense without a rate; one repayment outside the settlement
currency; formula-like and Unicode descriptions; a member named "summary" and one named "Bob/B")
was rendered locally and sent to the production host. It ran inside the bot container with the
mounted owner OAuth profile; the credential never left the host. The read-back confirmed:

- Final title applied (the "(incomplete)" create title was replaced), and 8 tabs in order. The
  name-clash tabs came back as `summary (2)` and `Bob B`.
- Dates and date-times are number cells shown as `dd mmm yyyy`; amounts are number cells shown with
  `#,##0.00`, and JPY with `#,##0`. Negatives show in red parentheses and zero as `–`. Rates show up
  to 10 decimals.
- Frozen header rows (Transactions and Raw ledger: 5), a filter on each main table, banding on every
  table, and `SUM` totals as the only formulas.
- Summary nets of +71.12, −78.49, +30.24 and −22.87 sum to zero and match the canonical balances.
- Suggested and recorded repayments sit in separate tables, and the HKD repayment is marked "No (not
  in SGD)".

The test spreadsheet remains private in the owner's Drive for inspection.
