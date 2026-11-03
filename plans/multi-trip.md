# Multiple trips per chat (planned, active, closed) + `&amp;` fix

Read first: `ARCHITECTURE.md` §3 (expenses, "Trips and currencies"), §6a (security baseline: every write
needs a human confirmation click; no user text or secrets in logs); the whole of
`packages/plugin-expenses/` (source and tests); `packages/core/src/chatcore/store/migrations.py`
(migration rules: per-package `NNNN_name.sql`, additive, an applied file must never change);
`packages/core/src/chatcore/pending.py` and `confirm.py` (pending-action flow);
`apps/sb_bot/tests/test_tool_markup_e2e.py` and `tests/test_app_integration.py` (console e2e and
integration patterns).

## Synthetic example motivating the behavior

A chat may plan one future trip while another trip is active. A single-active-trip
constraint wrongly forces one to end before the other can be created. Names containing
an ampersand must also be stored as text rather than an HTML-escaped display value.
These examples are synthetic; production incident records remain private.

Root cause: `migrations/0001_ledger.sql` has `status IN ('active','closed')` and a unique index
`trips_one_active_per_chat`; every tool assumes "the active trip".

## You own

- `packages/plugin-expenses/src/chatcore_expenses/**` (new migration
  `migrations/0002_multi_trip.sql`, `repository.py`, `plugin.py`, `export.py`, `balances.py`,
  `receipt.py` only if needed)
- `packages/plugin-expenses/tests/**` (update existing tests, add new ones)
- `apps/sb_bot/src/sb_bot/prompts/system.md` (only the expenses paragraph)
- `apps/sb_bot/tests/test_multi_trip_e2e.py` (new)

You may **not** edit `migrations/0001_ledger.sql` (already applied in production; its checksum is
verified), anything under `packages/core/`, other plugins, adapters, or `ARCHITECTURE.md`. If a core
change looks necessary, explain it in the report instead.

Environment: run `export UV_CACHE_DIR="$PWD/.uv-cache"` before any `uv` command; `uv sync
--all-packages` has already been run. There is no Postgres in your sandbox: integration tests must
skip when it is unreachable (follow `tests/test_pg_integration.py`).

## Decisions (already made; implement them)

- **Statuses**: `planned` (future), `active` (happening now), `closed`. Any number of planned and
  active trips per chat.
- **Optional dates**: `start_date`, `end_date` (`date`, nullable). Informational only: they never
  change status automatically. `end_date >= start_date` when both are set.
- **Default trip**: one per chat, stored in a new table. It must point at an `active` trip of the
  same chat. Starting an active trip, activating a planned trip, or reopening a trip as active
  makes it the default. Ending a trip or reopening/moving it to planned clears the default if it
  pointed there. `set_default_trip` changes it explicitly.
- **Names**: unique per chat among non-closed trips, case-insensitive (partial unique index).
  Closed trips may share a name with others.
- **Expenses on non-active trips**: an explicitly named `planned` trip may take expenses and
  settlements (pre-booked flights/hotels); a `closed` trip may not (error: reopen it first).
  Implicit attribution only ever picks an `active` trip.
- **Never end, close or modify another trip as a side effect** of starting/activating one.
- **Rollback safety**: the migration only relaxes constraints and adds nullable columns/tables, so
  the previous release still runs against it.

## 1. Migration `0002_multi_trip.sql`

```sql
ALTER TABLE trips DROP CONSTRAINT trips_status_check;   -- name Postgres gave the 0001 CHECK
ALTER TABLE trips ADD CONSTRAINT trips_status_check
    CHECK (status IN ('planned', 'active', 'closed'));
ALTER TABLE trips ADD COLUMN start_date date, ADD COLUMN end_date date;
ALTER TABLE trips ADD CONSTRAINT trips_dates_order
    CHECK (start_date IS NULL OR end_date IS NULL OR end_date >= start_date);
DROP INDEX trips_one_active_per_chat;
CREATE UNIQUE INDEX trips_open_name_per_chat ON trips(chat_key, lower(name))
    WHERE status <> 'closed';
CREATE TABLE trip_defaults (
    chat_key text PRIMARY KEY,
    trip_id uuid NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    set_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO trip_defaults(chat_key, trip_id)
    SELECT chat_key, id FROM trips WHERE status = 'active';
```

Verify the real constraint name of the 0001 inline CHECK (Postgres names it `trips_status_check`);
if unsure, drop it with a `DO` block that looks it up in `pg_constraint`. Existing rows keep their
status; at most one active per chat exists today, so the default insert and the name index cannot
conflict. Existing closed rows get NULL dates. Do not touch `started_at` semantics (creation time).

## 2. Repository (`repository.py`)

Replace the single-active-trip API. Both `MemoryLedgerRepo` and `PostgresLedgerRepo` implement the
same `LedgerRepo` protocol, every query scoped by `chat.key`:

- `trips(chat, *, statuses: Iterable[str] | None = None, closed_limit: int | None = None)` →
  list of trip dicts (non-closed first by `start_date NULLS LAST, started_at`, then closed by
  `ended_at DESC`, limited by `closed_limit`).
- `default_trip_id(chat) -> str | None` (only if it still points at an active trip of this chat).
- `trip(chat, ref)` keeps working for ids and names (see §3 resolution; keep the Postgres
  lookup efficient: full UUID, 8-hex-char id prefix, then name).
- Remove `active_trip`; update every caller.
- Every trip dict carries `start_date` / `end_date` (as `date` or None) and `is_default: bool`.

`commit()` kinds (all re-check state inside the transaction with `FOR UPDATE`/`FOR SHARE` in
Postgres, and raise `ValueError` with a user-facing message if the state changed since proposal):

- `start_trip`: payload adds `status` (`planned`|`active`), `start_date`, `end_date`. Inserts the
  trip; if active, upserts the default to it. Duplicate open name → `ValueError("A trip named X is
  already planned or active in this chat.")` (Postgres: catch the unique violation on
  `trips_open_name_per_chat` inside the transaction or check first with `FOR UPDATE` on the chat's
  open trips; Memory: check). Message: `Started trip X.` / `Planned trip X.`.
- `activate_trip` (planned → active), `reopen_trip` (closed → `active` or `planned`, clears
  `ended_at`), `end_trip` (planned or active → closed, sets `ended_at`): payload has `trip_id`
  and `expected_status`; fail if the current status differs. Reopen fails if another open trip
  has the same name. Update the default per the decisions above.
- `set_default_trip`: payload `trip_id`; trip must still be active.
- `update_trip`: look up by `payload["trip_id"]` with status `planned` or `active` (not "the active
  trip"); also supports `start_date` / `end_date` changes (`clear_dates: bool` not needed; a
  `null` in payload means "unchanged"). Keep the existing usage checks and change lines (add
  "Dates" line formatting like `2026-12-01 → 2026-12-10`).
- `add_expense`, `settle_up`: when `trip_id` is set, the trip must exist in this chat and not be
  closed (`"The trip was closed; propose this again."`), plus the existing settle-settings check.
- `set_fx_rate`: use `payload["trip_id"]` (not "the active trip"); trip must be planned or active
  and in convert mode; keep the existing settle-settings check and expense backfill.
- `purge_chat`: also deletes `trip_defaults` rows for the chat.

## 3. Trip resolution in the plugin (`plugin.py`)

One helper does all lookups, so tools behave the same:

`_resolve_trip(ledger, chat, ref) -> dict` for an explicit `trip` argument:
1. Full UUID or 8-hex-char prefix of an id in this chat.
2. Case-insensitive exact name match (after the text normalisation in §6); prefer non-closed,
   then most recently started closed one.
3. Otherwise a unique case-insensitive substring match among the chat's trips (prefer
   non-closed if that makes it unique), so "Sample" finds "Sample & Example trip".
4. None or several → `ValueError` naming the candidates (max 5): `Trip "x" not found. Trips:
   ...` / `"x" matches several trips: A, B. Which one?`.

`_current_trip(ledger, chat) -> dict | None` for implicit logging scope:
- default trip (active) if set;
- else the single active trip if exactly one;
- else None if there are no active trips (general ledger);
- else `ValueError("Several trips are active (A, B) and none is the default. Which trip is
  this for? You can also set a default with set_default_trip.")`.

Read tools without `trip` use `_current_trip`, falling back to the general ledger when it is
None. Trip-management tools without `trip` (`update_trip`, `end_trip`, `get_trip`) use the
current trip; if None and exactly one planned trip exists, use it; otherwise ask which.

## 4. Tools

All writes stay `writes=True` and return `self._pending(...)`; only the commit handler writes.
Never let tool arguments carry identity, chat or status preconditions beyond what the plugin
computes (`expected_status`, `trip_id` are filled from the resolved trip, never from raw args).
Every confirmation summary names the trip and its status.

- `start_trip(name, countries, settle_mode, settle_currency="SGD", status?, start_date?,
  end_date?)`: no longer refuses because another trip is active or planned. `status` default:
  `planned` if `start_date` is after today (plugin timezone), else `active`. Dates YYYY-MM-DD,
  validated. Reject a name that matches an open trip. Name 1–100 chars after normalisation.
  Summary lines: `Start trip:` or `Plan trip:`, countries, currencies, settle mode/currency,
  dates if any, and when active and other trips exist: `Becomes the default trip for new
  expenses. Other trips stay as they are: Sample & Example trip (planned).`
- `list_trips()` (read): planned and active trips, plus the 5 most recent closed; each line has
  name, status, `(default)` marker, dates, countries, settle mode/currency, expense count is
  optional (skip if it needs a new query per trip).
- `get_trip(trip?)`: one trip's details (status, dates, default marker). Without `trip` and no
  current trip, list open trips briefly or say there are none.
- `set_default_trip(trip)` (write): trip must be active (a planned one: tell them to
  `activate_trip`).
- `activate_trip(trip)` (write): planned → active and default.
- `reopen_trip(trip, status="active"|"planned")` (write): closed → status. Summary includes:
  `The settle-up summary posted when it ended was informational only; no settlements were
  recorded by ending it, and balances are recomputed from its expenses and settlements.`
  (true: `end_trip` never writes settlements — keep it that way).
- `end_trip(trip?)`: acts on the named/current trip (active or planned). Summary keeps the final
  settle-up summary for that trip only. Never ends a trip other than the one named in the
  summary.
- `update_trip(trip?, ...existing args, start_date?, end_date?)`: works on planned or active trips.
- `add_expense(..., trip?, general?)`: `trip` explicit (planned or active; closed → error), else
  `general=true` → general ledger, else `_current_trip` (ambiguity → error asking which trip,
  nothing proposed). Currency resolution and FX use the chosen trip exactly as today.
- `settle_up(..., trip?, general?)`: same attribution as `add_expense`.
- `set_fx_rate(currency, rate, trip?)`: target trip explicit or current; must be convert and not
  closed.
- `list_expenses(limit, trip?, general?)`: default scope = current trip, else general ledger;
  never mixes trips (the old default listed every expense in the chat; change that).
- `get_balances(trip?, general?)` and `/balance`: same scope rule; per-trip only.
- `export_expenses(trip?, general?)` and `/export [trip | general]`: same scope rule; the CSV
  header/caption shows the trip's status (and dates if set).
- `read_receipt`: unchanged behaviour; its description should say to pass `trip` to
  `add_expense` when the user names a trip.
- Ambiguity errors from implicit scope are `ToolResult.error(...)` texts the model relays as a
  question.

Register handler kinds: add `activate_trip`, `reopen_trip`, `set_default_trip` to
`action_handlers()`. `_CommitHandler`: the `sqlstate == "23505"` message becomes `"Not saved: a
trip with that name is already planned or active in this chat."`. `end_trip` commit reply keeps
"Trip ended: X\nFinal settle-up summary:\n..." for that trip.

Tool descriptions (the model reads these): make clear that several trips can exist, that starting
a trip never ends another one, that "we're in X now, start an X trip" while another trip is
planned means `start_trip` for X (active), and that `trip` picks a trip by name.

## 5. System prompt (`apps/sb_bot/src/sb_bot/prompts/system.md`, expenses paragraph only)

Replace "the active trip" wording. Add, briefly: a chat can have several planned and active
trips; to start logging for a new trip, call `start_trip` (it never ends or replaces other
trips; future trips use `status: planned` or a future `start_date`); use `list_trips` when unsure
which trips exist; when several trips are active and the tool asks which one, ask the user; only
end a trip when someone asks to end that trip; use `reopen_trip` to undo an accidental end. Keep
it short; don't restate tool docs.

## 6. `&amp;` fix (stored names)

Finding: nothing in the codebase HTML-escapes text before storage. The Telegram adapter renders
replies via markdown-it into plain text + entities (it decodes `&amp;` to `&`), but confirmation
prompts are sent as raw text, so a stored `&amp;` shows literally there. The conversation history
is sent to the model as JSON with `&` written as `&`, and the model (some providers' native
XML-style tool-call formats) returns entity-escaped tool arguments, e.g. `Sample &amp; Example
trip`. So the fix belongs at the tool-input boundary of this plugin:

- Add `_plain_text(value: str) -> str` in `plugin.py`: `html.unescape` applied repeatedly (max 2
  passes, so `&amp;amp;` → `&`) only when the string contains an `&...;` entity, then `.strip()`.
  Apply it to free-text inputs before validation and storage: trip `name` (start/update), trip
  references used for lookup, expense `description`, `countries` entries, and member names passed
  to `_resolve_member` (paid_by, split_with, shares keys, from_user, to_user).
- Stored text stays raw; summaries/replies use the raw text (no escaping in the plugin — the
  adapter handles rendering).
- Check `export.py` CSV output keeps raw text (csv module quoting only).

## Tests

Unit tests use `MemoryLedgerRepo` and the existing fakes; put new ones in
`tests/test_multi_trip.py` (and update existing tests that assumed one active trip).

Trips and lifecycle:
- Start an active trip while a planned trip exists → both exist, planned untouched, new one is
  default. Start a second active trip → both active, default moves to the new one.
- Start with a future `start_date` → planned by default; explicit `status` respected; bad date
  format and `end_date < start_date` rejected.
- Duplicate open name rejected (case-insensitive); same name as a closed trip allowed.
- `list_trips` shows planned/active/closed with default marker and dates; chat isolation (another
  chat's trips never listed or resolvable, by name, id or id prefix).
- `activate_trip` planned → active + default; `set_default_trip` only on active trips.
- `end_trip(trip=...)` ends exactly that trip; default cleared when it was the default; other
  trips untouched. Ending without a name when two active and no default → asks.
- `reopen_trip` closed → planned and closed → active; summary contains the informational note;
  `ended_at` cleared; stale `expected_status` (trip changed after proposal) → "Not saved: ...".
  Reopen blocked when an open trip has the same name.
- Every write tool returns a pending action and nothing is written until the handler commits.

Attribution:
- Default trip used when set; single active trip used without default; no active trips → general
  ledger even when planned trips exist; explicit planned trip accepted; explicit closed trip
  rejected; two active + no default → error asking, no pending action; `general=true` → general.
- Pending `add_expense` for a trip that is closed before confirmation → "Not saved".
- `settle_up` and `set_fx_rate` follow the same rules (set_fx_rate on a planned convert trip
  works; FX override lookups are per trip).

Balances/export:
- Two trips with expenses in different currencies: `get_balances(trip=A)` contains only A's
  amounts; default scope shows only the current trip; general ledger excludes all trip expenses.
  `list_expenses` default no longer lists other trips' expenses.
- `export_expenses(trip=...)` CSV contains only that trip's rows; ambiguity rule applies to the
  default scope.

Escaping:
- `start_trip(name="Sample &amp; Example trip")` stores and summarises `Sample & Example trip`;
  `&amp;amp;` → `&`; plain `&` unchanged; trip lookup "Sample & Example" works; expense
  description `Fish &amp; chips` stored as `Fish & chips`.

Integration (`tests/test_pg_integration.py`, Postgres, skip if unreachable; use `TEST_PG_DSN`):
- Migration of existing rows: apply only `0001`, insert an active trip and a closed trip (with
  expenses) for one chat and an active trip for another chat, then apply `0002`: statuses kept,
  defaults created for both active trips, dates NULL, closed trip unchanged; then two active
  trips and several planned trips can coexist; duplicate open names violate the index; `status`
  outside the three values violates the CHECK. Check the migration runner checksum test still
  passes (0001 unchanged).
- Postgres repo: the lifecycle commits above (start planned + active, activate, end, reopen,
  set default, per-trip balances, add_expense to a closed trip refused, chat isolation).

Console e2e (`apps/sb_bot/tests/test_multi_trip_e2e.py`, integration; skip when Postgres is
unreachable; follow `test_tool_markup_e2e.py` with `build_app("console", env=..., platform=...,
provider=ScriptedProvider(...))` and `PG_DSN` set to `TEST_PG_DSN`; check
`tests/test_app_integration.py` for how a real DB is wired and whether Valkey is needed; use a
unique console chat or clean up rows for it):
- Seed a planned "Sample & Example trip" with synthetic countries/currencies for the console chat.
- Turn: `@test_bot start an Example city trip`; the scripted model calls
  `start_trip(name="Example city trip", countries=["Malaysia"], settle_mode="convert")`.
  The user confirms with the console code or button. Assert the confirmation says the
  Sample trip stays planned. Both trips then exist: Example city trip is active/default,
  Sample & Example trip is still planned, no settle-up was posted, and an `add_expense`
  without `trip` uses Example city trip.

## Done when

- `uv run ruff check .` and `uv run ruff format --check .` clean.
- `uv run pytest -q packages/plugin-expenses apps/sb_bot` passes (integration tests skip in your
  sandbox; the reviewer runs them against Postgres).
- No tool can end, close or alter a trip other than the one shown in its confirmation summary.
- No new logging of message text, names or descriptions (IDs and counts only).
- Left for later (do not attempt): automatic status changes from dates; a core-level fix for
  entity-escaped tool arguments; production data repair (the reviewer writes that SQL).
