# Itinerary tools: tracker, Sheet-to-itinerary and suggester

Status: **Implemented and Google configured; live chat workflow validation pending.** PR #47
included the revised #39/#40 plans. Owner OAuth and native Google API checks passed on
2026-11-02. Drive DOCX read/import support is a [live extension](2026-11-02-drive-docx.md) via PR #54,
with automatic deployment, expanded consent and the deployed DOCX reader verified. See [execution record](2026-11-02-google-itinerary-implementation.md).
**Design dependency:**
[2026-11-01-google-workspace.md](2026-11-01-google-workspace.md): it assumes that plan's Docs and
Sheets tools, owner-account authorization, human-posted URL provenance and confirm-before-write
rules, and cannot be built before them.

Read the Google Workspace plan, `plans/multi-trip.md` and `plans/track-d.md` (expenses trips),
`plans/skill-layer.md`, `packages/plugins/search`, `packages/plugins/research`, and
`apps/sb_bot/src/sb_bot/skills.py` (`COMPOSITES`).

## Goal

High-level trip tools built on the basic Sheets tools:

1. **Itinerary tracker:** one Google spreadsheet per trip, with day/city tabs chosen in the
   user's request and the bot validating and summarising them.
2. **Sheet-to-itinerary:** convert a messy Sheet, Doc or uploaded CSV into the tracker schema.
3. **Suggester:** draft a plan from a destination, dates, interests and budget.

Out of scope: route ordering and travel times (needs a routing data source, undecided; its own
plan once this is in use), booking, and acting on bookings.

## 1. Where the code lives

The user-facing workflow remains an **app composite** in
`apps/sb_bot/src/sb_bot/skills.py` (`COMPOSITES`), with behaviour instructions on the
composite declarations. The base Google skill owns its instructions in the Google plugin. A composite groups already-registered tools; it does not implement
executable handlers, gates or pending-action commits. No plugin imports another plugin's
`skills` or `gates` module (test).

Pure logic (schema, validation, clash detection, row diffing) goes in a small module with no
I/O so it is easy to test. Implementation choice D3: `itinerary.py` in the Google plugin, with
`itinerary_service.py` coordinating I/O and app composites combining the tools.

With that default, `plugin-google/plugin.py` registers the itinerary tools and action handlers;
`gates.py`/`permissions.py` enforce access and `itinerary_service.py` coordinates reads, previews
and commits using the pure `itinerary.py` helpers. Search/research and expense access arrive
through narrow app-injected provider protocols, wired through the plugins' public APIs. A
separate itinerary plugin, if D3 changes, must own the same executable surfaces. Skill text
never substitutes for schema validation, permissions or write checks.

This phase adds an internal `create_itinerary_tab` operation to the Google service, with
`AddSheet` plus initial schema/data population in one batch on a fresh sheet ID. It also adds
an append-only `AppendCells` operation on a validated existing tab. The base plan's create-file
operation can create and populate a new itinerary Sheet within one confirmed action. These
are typed service operations, not a model-accessible arbitrary `batchUpdate` tool. Existing-cell
updates and row deletion are outside v1.

## 2. The tracker schema

Owner decision (2026-11-02): **one Google spreadsheet file per trip**, with tabs grouped by
day, city or the named day/city groups in the user's prompt. Reuse the trip's posted spreadsheet
instead of creating a separate file for each day/city. For an existing spreadsheet, follow its
recognized tab layout unless the user asks for additional tabs. If a new trip's grouping is
unclear, ask which grouping to use before creating it; preview the tab names with the rows.
The grouping belongs to each trip request, not a global configuration setting.

Every tracker tab has the same fixed header row and version cell:

| Column | Meaning |
| --- | --- |
| Day | Date or day number |
| Time | Start time, optional end |
| Place | Name, optionally a Maps link |
| Type | Stay, Food, Sight, Transit, Other |
| Cost | Amount and currency |
| Booking ref | Free text |
| Status | `Proposed`, `Confirmed`, `Done`, `Dropped` |
| Notes | Free text |

- The Sheet is the source of truth. The bot keeps **no hidden copy**; each read obtains a live
  snapshot. Humans may edit it again after that read; the bot does not promise a transaction
  across reads and writes.
- The bot **refuses to write into a Sheet whose header or version it doesn't recognise**, and
  says what it expected.
- v1 writes append rows or create and populate a new tab/Sheet. Existing rows, including their
  Status cells, are never updated or removed by the bot. People confirm, edit or drop rows in
  Sheets directly. Chat-based row edits are deferred.
- Append with server-side `AppendCells` on the stable sheet ID, after the last row with data;
  never calculate an empty row from a stale read and issue `values.update`/`UpdateCells` there.
  Do not set `tableId`; restrict fields to `userEnteredValue` and write literal values.
  Initialize cells with `UpdateCells` only inside the same batch that creates their new tab.
  See [the append contract](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request#AppendCellsRequest).
- Re-read and validate the target's header/version immediately before commit; refuse a deleted
  or changed target and re-preview if refreshed itinerary data makes the draft unsuitable.
  This is a best-effort preflight, not compare-and-swap. A separate read followed by Sheets
  `batchUpdate` cannot prevent concurrent human changes. Bot locks only serialize bot requests;
  they cannot lock Sheets editors. Keeping existing rows outside the write set prevents the
  lost-update failure. Any future existing-row mutation needs a separate concurrency design.
  See [Sheets' collaboration contract](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate).
- Values written to cells are formula-neutralised, as in the base plan §4.
- Use stable sheet IDs after resolving user-facing tab names. New tabs get distinct names;
  a name collision never replaces or clears an existing tab. Freeze the selected grouping,
  tab IDs/names and row-to-tab assignments in the previewed pending action. Reorganizing or
  moving existing rows between tabs remains outside v1's append/create-only writes.

## 3. Tracker tool

`itinerary_summary(url, tab?)` reads all recognized tracker tabs in the trip spreadsheet by
default. An explicit tab argument or URL `gid` narrows the request to that tab; reject conflicting
selectors. Preserve the tab name/ID for each row, and aggregate across tabs before detecting
trip-wide clashes or comparing costs. Cap the number of tabs as well as rows/columns; report
partial coverage and avoid claiming a complete trip total when truncated. It returns:

- a day-by-day summary;
- **clashes** (overlapping times on one day), **gaps** (a day or half-day with nothing),
  rows missing a time or cost, unparseable costs;
- optionally a planned-vs-actual cost comparison against the linked expenses trip.

Linking to an expenses trip is an explicit confirmed write (`itinerary_link_trip`), stored in a
small table (additive migration), not inferred. Scope the trip-level mapping by authorized chat
and spreadsheet file ID so all day/city tabs belong to the same linked expenses trip. Resolve
the trip with the expenses plugin's existing group/DM membership rules.
Compare totals by currency; never convert silently (the expenses plugin owns FX).

`itinerary_summary` is read-only; no confirmation needed. Linking needs confirmation.

## 4. Sheet-to-itinerary tool

`itinerary_import(groups, source_url?, source_message_ref?, target?, title?)`:

The main agent first reads the source and maps it into typed `groups` (tab title plus row
objects). The handler revalidates source access and deterministically validates/previews those
groups. It does not start a second model or reinterpret source instructions. The layout is
represented by the explicit group titles and row assignments.

1. Require exactly one source: a Sheet/Doc URL through the base plan's Google read tools, or a
   human-posted CSV attachment through `read_attachment`. Apply the current-chat attachment
   checks and capped built-in CSV parsing described in the base plan; all results are untrusted.
2. Map it to the schema. Dates, times and costs the model can't read confidently are left
   blank and listed, not guessed.
3. Group the proposed rows into day/city tabs according to the user's request, leaving unclear
   group assignments for clarification. Show the rows and target tab names, then **ask for
   confirmation** before writing.
4. Write to new tabs in the trip's posted spreadsheet, or create one spreadsheet for a new
   trip. Populate all new day/city tabs within the confirmed action, never over source tabs
   or the CSV attachment. CSV is input only; it does not become a second live itinerary copy.

Google source URLs and any existing destination require the base plan's human-posted URL
provenance; a CSV source instead requires the authorized attachment reference. Search results
and source-document links cannot authorize a destination. A newly created tab
is populated within its single confirmed create action. An empty new tab gets the schema
before its data; the existing-header refusal applies to existing tracker tabs, not empty
tabs being initialized by the bot.

## 5. Suggester

`itinerary_suggest(url, groups)`:

The composite gathers the destination/dates/interests/budget and current research, then passes
normalized source-backed groups to this deterministic handler. Research-returned source URLs
join public `seen_urls`, never the separate human collection authorizing Google access.

- Gathers ideas with `plugin-search` and `plugin-research` (existing tools; no new egress).
- Drafts results as `Status = Proposed` rows, previews them, and appends only after the
  requester's confirmation. People accept, edit or drop them in Sheets. The bot **never edits
  or removes an existing row**, including a `Confirmed` row.
- Every suggestion carries its source link in Notes. Facts such as opening hours and prices
  that weren't verified from a source are marked "unverified".
- Existing rows are read across the trip's recognized day/city tabs first, so suggestions fit
  around confirmed items and don't repeat them. Follow the requested grouping or the existing
  layout; preview any new tab names and proposed rows before confirmation.
- **Every suggestion batch requires confirmation in v1**, including Proposed rows. A request
  for suggestions authorizes drafting, not an immediate write. Handlers return a frozen
  `PendingAction` containing the validated owner, requester, chat, file/tab IDs and exact rows;
  the app calls `ConfirmService.request`. Commit re-checks permissions, caps and target schema,
  then appends that payload without rerunning the model or research. Register all itinerary
  writes with `writes=True, direct=False`; do not exempt research/Sheet reads via `direct_after`.
- Prompt-free suggestions are deferred. They require a separate reviewed design for trusted
  current-message authorization bound to requester, chat, target, limits and expiry. A model
  argument, sticky composite skill or inferred intention is not such authorization; generic
  Google append tools remain confirmed regardless of any future itinerary-specific exception.
- Caps on rows per call and per day, in settings.

## 6. Access and gates

- Read and summary tools inherit the Google plugin's default-on access in allowed chats once
  credentials are configured, with per-chat opt-out. An opt-out disables itinerary reads and
  writes too, including pending commits; no per-chat opt-in setup is required.
- Import and suggest write rows, so they follow the base plan's write gate (D2 there). Treat
  any change to `gates.py` or `permissions.py` as a permission change in the PR.
- Handlers and pending-action commits re-check permissions. Provenance is checked before any
  authenticated lookup, using the base plan's separate human URL collection, never `seen_urls`.

## 7. Skills, routing and tests

- Declare the skill and tools; add argument descriptions; update `routing_set.jsonl` and the
  request snapshot where tests require.
- Tests (no network, mocked Google transport and fake search/research):
  - schema parse and header/version refusal;
  - one spreadsheet per trip with per-day, per-city and explicitly requested mixed groups;
  - multi-tab summaries/costs/clashes, explicit tab/`gid` selection, stable IDs, duplicate tab
    names and bounded partial reads;
  - CSV attachment import, exactly-one-source validation, ambiguous row grouping, and creation
    of the requested day/city tabs without overwriting the source or existing itinerary tabs;
  - clash and gap detection, including midnight crossing and missing times;
  - confirmed rows are never modified by suggest or import;
  - suggesting produces no write before the requester confirms, even after Sheet/research
    reads, a model-supplied authorization claim, or a sticky skill from an earlier message;
  - commits use frozen preview rows and reject revoked permissions or a changed owner/target;
  - interleave a human edit, Status confirmation or row insertion between preview and commit:
    assert no write targets existing cells and the human's values survive;
  - append uses server-side `AppendCells`; new-tab initialization touches only its new sheet ID;
  - a changed header/version or deleted tab refuses the commit;
  - search-only source/target links fail before any authenticated request;
  - formula neutralisation; row caps;
  - planned-vs-actual comparison across currencies without silent conversion.

## 8. Rollout

1. Tracker summary (read-only), one test chat.
2. Sheet-to-itinerary.
3. Suggester.
4. Expenses link and planned-vs-actual.

The implementation is delivered in one reviewable PR with staged verification in the execution
record. Codex executed and self-reviewed inline; no separate Claude review was available.
Live rollout remains staged and requires the owner's Google authorization.

## 9. Decisions and deferred work

1. **D1 Schema — implementation default:** the eight columns and version/header in §2, with
   unknown fields left blank and local wall-time ranges reported as entered.
2. **D2 Tab layout — decided:** one spreadsheet per trip; day/city tabs follow the user's
   prompt and the trip's existing layout (§2). No global layout setting is required.
3. **D3 Code home — implemented:** pure helpers and deterministic handlers in the Google plugin;
   cross-plugin orchestration remains an app composite.
4. **D4 Suggester autonomy:** v1 confirms every suggestion batch. Prompt-free append is deferred
   until a separate authorization design is reviewed and approved.
5. **D5 Expenses link — implemented after tracker/import/suggest:** explicit confirmed linking
   and original-currency comparisons when the existing expenses plugin is available. Partial
   selections or unknown costs suppress whole-trip differences.
6. **D6 Who edits:** v1 leaves existing-row edits to Sheets permissions. Before chat-based
   status changes are added, decide who may request them and provide a concurrency design.


## Trip source attachment extension (2026-11-02)

The [trip Drive files plan](2026-11-02-trip-drive-files.md), approved by the owner,
extends the Sheet-only association with a primary itinerary and supporting Drive
references on existing trip IDs. Docs/DOCX/Sheets/PDF/CSV can be read directly;
tracker import remains an explicit optional workflow. It also fixes the observed
ordinary empty-response recovery gap. Build/merge/deployment evidence is tracked
in that plan; this record does not imply a live chat smoke test has passed.
