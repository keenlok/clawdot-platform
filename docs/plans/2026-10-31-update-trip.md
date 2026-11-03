# Edit the active trip (`update_trip`) and FX wording fixes

Read first:
- `docs/superpowers/specs/2026-10-31-update-trip-design.md` (the design; this plan implements it);
- `ARCHITECTURE.md` §4 ("Trips and currencies");
- `packages/plugin-expenses/src/chatcore_expenses/{plugin,repository,currency,fx}.py`;
- `packages/plugin-expenses/src/chatcore_expenses/migrations/0001_ledger.sql` (the `trips` table);
- `packages/plugin-expenses/tests/{test_plugin,test_repo,test_pg_integration}.py`;
- `tests/test_tool_audit.py` (`WRITES` must list every write tool).

Environment: `export UV_CACHE_DIR="$PWD/.uv-cache"` before running anything through `uv`.
Tests: `uv run pytest -q -m 'not integration'`. Lint: `uv run ruff format . && uv run ruff check .`.
Tests must not call the real FX APIs. Monkeypatch `chatcore_expenses.plugin.get_rate` (it is imported by name there) as the existing tests do, or pass a fake.

## You own

- `packages/plugin-expenses/src/chatcore_expenses/plugin.py`
- `packages/plugin-expenses/src/chatcore_expenses/repository.py`
- `apps/sb_bot/src/sb_bot/prompts/system.md`: only the expenses paragraph (§4)
- tests: `packages/plugin-expenses/tests/test_plugin.py`, `test_repo.py`, `test_pg_integration.py`, a new `packages/plugin-expenses/tests/test_update_trip.py`, and `tests/test_tool_audit.py` (add `update_trip` to `WRITES` and to the set that starts a trip first)
- any existing test whose exact `add_expense` summary or prompt text changes because of §3 or §4; update its expectation and say which ones in your report

Don't add a migration. Don't change `currency.py`, `fx.py`, `balances.py`, the `LedgerRepo` method signatures, or anything in `packages/core`. `commit(chat, kind, payload, ...)` just gains a new `kind`. If something else must change, stop and say why in your report.

## 1. `update_trip` tool (plugin.py)

Add `ExpensesPlugin._update_trip(ctx, *, name=None, add_countries=None, remove_countries=None, add_currencies=None, remove_currencies=None, settle_mode=None, settle_currency=None) -> ToolResult`.

1. Resolve `chat` with `self._chat(ctx)`. Use the same "can't identify your group" error as the other tools when it is `None`.
2. `trip = await ledger.active_trip(chat)`; if none: `ToolResult.error("There is no active trip.")`.
3. Validate the arguments. The registry does not type-check, so check the real values. Any `ValueError` becomes `ToolResult.error(str(exc))`.
   - `name`: must be a `str`. Strip it; it must be 1–100 characters.
   - List arguments: must be lists of `str`. Reject anything else and anything empty after stripping.
   - `add_countries`: each must pass `currency_for_country`. That gives the derived currency.
   - `add_currencies` / `remove_currencies` / `settle_currency`: upper-case, then `currency_exponent` (import it at module top from `.currency`; you may also tidy the existing local imports of it in this file).
   - `settle_mode`: `convert` or `per_currency`.
4. Build the new values with one pure helper, `_apply_trip_changes(trip: dict, changes: dict) -> dict`. Put it at module level in `repository.py`, so both ledgers and the plugin use the same code. The plugin imports it from there.
   - `changes` keys: `name`, `add_countries`, `remove_countries`, `add_currencies`, `remove_currencies`, `settle_mode`, `settle_currency`. Each is `None` or absent when unused. Lists are already validated; currencies are already upper-case.
   - Countries: start from `trip["countries"]`. Remove every entry whose `casefold()` matches a `remove_countries` item. Raise `ValueError(f"{c} is not on this trip.")` if a removal matches nothing. Then append `add_countries` items whose casefold isn't already present, keeping the caller's spelling.
   - Currencies: start from `trip["currencies"]` (upper-cased). Remove `remove_currencies`, raising `ValueError(f"{code} is not on this trip.")` if absent. Then append, in order, the derived currency of each added country, then `add_currencies`, skipping codes already present.
   - The result must have at least one country (`"A trip needs at least one country."`) and one currency (`"A trip needs at least one currency."`).
   - Returns `{"name", "countries", "currencies", "settle_mode", "settle_currency"}` for the new state.
   - It needs the derived currency for added countries. Call `currency_for_country` inside the helper (`repository.py` may import from `.currency`).
5. Compare the new state with the trip. If nothing differs: `ToolResult.error("Nothing to change on this trip.")`.
6. If `remove_currencies` is set, list the trip's non-deleted expenses (`ledger.expenses(chat, trip_id=trip["id"], limit=None)`) and settlements (`ledger.settlements(chat, trip_id=trip["id"])`). Refuse with `"<CODE> is used by expenses or settlements on this trip; it can't be removed."` if any uses a removed code.
7. If the settle mode or settle currency actually changes and the trip has any expense or settlement: `"Settle mode and settle currency can't change after expenses or settlements are recorded."`
8. Payload: `{"trip_id": trip["id"], **changes}`. Store only the requested deltas (with `None` for unused keys), not the computed result.
9. Summary: first line `Update trip: <old name>`. Then one line per changed field only, in this order and format:
   - `Name: <old> → <new>`
   - `Countries: <old joined by ", "> → <new joined>`
   - `Currencies: <old> → <new>`
   - `Settle mode: <old> → <new>`
   - `Settle currency: <old> → <new>`
10. Return `self._pending(ctx, chat, "update_trip", payload, summary)`.

Register the tool in `tools()` after `start_trip`, with `writes=True` and a JSON schema whose properties match the arguments above (arrays of strings; `settle_mode` enum). Description:

> Propose changes to the active trip: rename it, add or remove countries (adding a country adds its currency), add or remove currencies directly (e.g. HKD), or change the settle mode/currency before any expense is recorded. Every write requires confirmation.

Add `"update_trip"` to the kinds in `action_handlers()`.

`_CommitHandler.commit` needs no special branch beyond what is already there, and the `ledger.commit` result string is returned.

## 2. Commit branch in both ledgers (repository.py)

`kind == "update_trip"` in `MemoryLedgerRepo.commit` and `PostgresLedgerRepo._commit`:

1. Load the active trip for `chat`. In Postgres use `SELECT * FROM trips WHERE chat_key=$1 AND status='active' FOR UPDATE`. If there is none, or its id differs from `payload["trip_id"]`, raise `ValueError("The trip changed or ended; propose the update again.")`.
2. `new = _apply_trip_changes(current, payload)`. This uses the **current** row, so two pending updates both apply.
3. Repeat the checks from §1 steps 6 and 7 against the current state, inside the transaction:
   - expenses with `deleted_at IS NULL` and settlements of that trip using a removed currency;
   - any expense/settlement when the settle mode or currency changes.
   Raise the same messages as `ValueError`. In Postgres, query `expenses`/`settlements` directly by `chat_key` and `trip_id` (scoped by `chat_key` like every other query).
4. If nothing differs from the current row, raise `ValueError("Nothing to change on this trip.")`.
5. Write `name, countries, currencies, settle_mode, settle_currency` (Postgres: one `UPDATE trips SET ... WHERE chat_key=$1 AND id=$2`).
6. Return `Updated trip <new name>.` followed by the same changed-field lines as the proposal summary, computed from old vs new at commit. Put the line formatting in a module-level helper in `repository.py` (e.g. `_trip_change_lines(old, new) -> list[str]`) and reuse it from the plugin for the proposal summary.

## 3. `add_expense` summary (plugin.py)

In `_add_expense`, after the `Trip:` line:
- If a trip is active and `currency` is not in its upper-cased `currencies`, add
  `Note: <CODE> is not one of this trip's currencies (<codes joined by ", ">). Use update_trip to add it.`
- In convert mode with `currency != settle_currency`:
  - rate found: `FX: 1 <CODE> = <rate> <SETTLE>`, with ` (approximate)` appended when `approximate` is true;
  - rate `None`: `FX: no rate found yet; set one with set_fx_rate`.
  This replaces the existing `FX: approximate rate` line. Render the rate with `str(rate)`. Normalise it only if a trailing-zero form would look wrong; say what you chose.

The expense is still allowed when the currency is off-trip. Don't change `resolve_currency`.

## 4. FX wording

- `add_expense` tool description: append
  `In convert trips the FX rate for the expense date is fetched automatically (Frankfurter, then open.er-api as an approximate fallback); don't ask the user for a rate.`
- `set_fx_rate` description: replace with
  `Optional: override the automatic FX rate for a currency in the active convert trip, e.g. to match a money changer. Applies to later expenses in that currency and fills expenses that have no rate. Requires confirmation.`
- `start_trip` description: append `Use update_trip to change it later.`
- `apps/sb_bot/src/sb_bot/prompts/system.md`: extend the "Expenses default to SGD…" paragraph with two sentences.
  - Rates: in convert trips, `add_expense` fetches the FX rate for the expense date automatically, so never tell users they must set rates; `set_fx_rate` is only an optional override.
  - Edits: use `update_trip` to add countries or currencies or to rename the active trip.
  Keep the paragraph's style. Change nothing else in the prompt.

## Tests

New `packages/plugin-expenses/tests/test_update_trip.py`, using `MemoryLedgerRepo` and the fixtures/patterns from `test_plugin.py`:
- Example trip (CN, CNY, convert, SGD): `add_countries=["Hong Kong"]` gives a pending action. Its summary is exactly `Update trip: China\nCountries: China → China, Hong Kong\nCurrencies: CNY → CNY, HKD`. Nothing changes before commit. After commit, `get_trip` shows Hong Kong and HKD, and the commit reply starts with `Updated trip China.`
- `add_currencies=["hkd"]` adds HKD without touching countries; `name="Example & City"` renames (summary line `Name: China → Example & City`).
- After adding HKD, `add_expense` with no currency in a now two-currency trip asks which currency (existing behaviour).
- Adding something already present plus nothing else → "Nothing to change" error; unknown country → error from `currency_for_country`; bad ISO code → error; non-list or non-str values → error; empty name → error.
- `remove_currencies=["CNY"]` is refused when a CNY expense exists. It is allowed after that expense is soft-deleted, provided another currency remains. Removing the last currency or the last country is refused. Removing an absent country → error.
- `settle_mode` / `settle_currency` change: allowed on an empty trip, refused once an expense or a settlement exists.
- Commit-time recheck: propose removing HKD, then commit an HKD expense, then confirm the update → `Not saved: ...` and the trip is unchanged.
- Two pending updates (add JP, add KR) both committed → both present (deltas, not overwrite).
- Trip ended between proposal and confirmation → `Not saved: The trip changed or ended...`.
- No active trip → error. Another chat's trip is not visible or editable (chat isolation).
- `add_expense` summary: the off-trip note appears for HKD on a CNY trip and not for CNY. The FX line shows the fetched rate, `(approximate)` when flagged, and the no-rate message when `get_rate` returns `(None, False)`. Monkeypatch `get_rate`.
- `update_trip` is in `action_handlers()` and is a `writes=True` tool.

`test_repo.py`: `MemoryLedgerRepo` `update_trip` commit branch directly (apply, wrong trip id, recheck).

`test_pg_integration.py` (integration, skips without Postgres): extend the existing test or add one. Cover start trip → `update_trip` add country+currency → `active_trip` reflects it. Removing a used currency is refused. A settle change with an expense is refused. Another chat can't update it.

`tests/test_tool_audit.py`: add `"update_trip": {"add_currencies": ["HKD"]}` to `WRITES` and add `update_trip` to the names that start a trip first.

## Done when

- `uv run pytest -q -m 'not integration'` passes (report counts), and `uv run ruff format . && uv run ruff check .` is clean.
- The tests above exist and assert real behaviour (summary text, stored row, refusal messages), not just "no exception".
- Integration tests are written and skip cleanly without Postgres; the reviewer runs them.
- Not in scope: migrations, editing closed trips, re-pricing expenses, editing expenses, deployment.
