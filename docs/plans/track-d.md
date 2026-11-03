# Track D: expenses, trips, FX (Splitwise-like)

Read `ARCHITECTURE.md` §2, §4 (all of it, including "Trips and currencies"), §6a "LLM-specific" (every
write needs a click), and §7c.

## You own

- `packages/plugin-expenses/` (package `chatcore_expenses`), including its
  `migrations/0001_ledger.sql`

Don't edit any `chatcore` file.

## Model

**Amounts** are integers in **minor units**. Currency exponents come from a table:
- ISO 4217 subset with JPY 0, KRW 0, VND 0, SGD 2, USD 2, EUR 2, GBP 2, MYR 2, THB 2, TWD 2,
  HKD 2, AUD 2, IDR 2, CNY 2, PHP 2;
- default 2 for anything else on the ISO list;
- an unknown code is rejected.

**Tables** (all keyed by namespaced `chat_key`, user keys namespaced too):
- `trips(id, chat_key, name, countries text[], currencies text[], settle_mode ('convert' | 'per_currency'), settle_currency, status ('active' | 'closed'), started_at, ended_at)`,
  with at most one active trip per chat (partial unique index)
- `expenses(id, chat_key, trip_id null, description, amount, currency, paid_by, created_by, spent_on date, fx_rate_to_settle numeric null, created_at, deleted_at null)`
- `expense_shares(expense_id, user_key, amount)`: shares always sum exactly to the amount
- `settlements(id, chat_key, trip_id null, from_user, to_user, amount, currency, created_at)`
- `fx_overrides(chat_key, trip_id, currency, settle_currency, rate, set_by, created_at)`

## Logic (pure functions, heavily unit-tested)

**Splits** (`split.py`):
- equal, exact amounts, percentages, or shares;
- validate that exact amounts sum to the total and percentages to 100;
- **rounding remainders go to the payer** (if the payer isn't in the split, to the first
  participant, deterministically).

**Balances** (`balances.py`):
- net per user per currency;
- `convert` trips: each expense converted to `settle_currency` with its stored `fx_rate_to_settle`,
  rounded half-even at the end, not per expense;
- **simplified "who pays whom"** using greedy min-cash-flow (largest debtor pays largest creditor,
  repeat), so it produces at most n−1 transfers.

**Currency resolution** for a new expense:
1. an explicit currency in the tool call;
2. otherwise the active trip's currency, if it has exactly one;
3. otherwise SGD when no trip is active (`DEFAULT_CURRENCY=SGD`);
4. otherwise (a multi-currency trip with no currency given) **ask**: return a tool error listing
   the trip's currencies. Never guess.

**FX** (`fx.py`), for `convert` trips only:
- The rate is captured on `spent_on` at creation: an `fx_overrides` entry first, then Frankfurter
  `GET https://api.frankfurter.dev/v1/{date}?base=X&symbols=Y`, then the fallback
  `https://open.er-api.com/v6/latest/X` (marked approximate).
- Cache 24 h in `ctx.stores.cache`.
- If both sources fail, the expense is still recorded with `fx_rate_to_settle = null`.
  `get_balances` then lists it as unconverted and suggests `set_fx_rate`.

**Countries to currencies:** a built-in table for common destinations (JP→JPY, KR→KRW, TH→THB,
VN→VND, TW→TWD, MY→MYR, ID→IDR, HK→HKD, CN→CNY, US→USD, GB→GBP, EU members→EUR, AU→AUD, SG→SGD,
PH→PHP), matched by English name or ISO code, case-insensitively.

**Member resolution:**
- `@username` or display name (case-insensitive, unique prefix) against `repo.find_members(chat)`;
  "me" and "I" mean the requester.
- Ambiguous or unknown names return a tool error listing the candidates. Never guess.

## Tools (`ExpensesPlugin(BasePlugin)`, `name = "expenses"`)

**Writes** (`writes=True`): each returns a `PendingAction` with a `summary` that shows exactly
what will be saved (description, amount with currency symbol, payer, each person's share, trip),
plus a `PendingActionHandler` that commits in **one transaction**:
- `add_expense(description, amount, currency?, paid_by, split_with[], split_type, shares?, spent_on?)`
  (`amount` as a decimal string, parsed with `Decimal`, never float)
- `delete_expense(expense_id)`: soft-delete; only the creator or an admin
- `settle_up(from_user, to_user, amount, currency)`
- `set_fx_rate(currency, rate)`
- `start_trip(name, countries[], settle_mode, settle_currency="SGD")`: fails if a trip is already
  active
- `end_trip()`: the commit message includes the final settle-up summary

**Reads:**
- `list_expenses(limit=20, trip?)`
- `get_balances(trip?)`: defaults to the active trip, else the general ledger
- `get_trip()`

**Commands** (no LLM):
- `/balance`: same as `get_balances`;
- `/expenses`: the last 10.

**DMs:** if `ctx.is_dm`, resolve the group with the constructor-injected
`group_for_dm: Callable[[ToolContext], Awaitable[ChatRef | None]]`, which the app wires in
Phase 2. When it returns `None` (not wired, or ambiguous), return a tool error asking the user to
use the group or say which group.

**Retention:** the ledger is kept (§6a). `purge_user` is a no-op that logs it. `purge_chat`
deletes everything for the chat.

## Tests

**Pure logic:**
- every split type;
- remainder to the payer, including 0-decimal currencies (JPY 1000 / 3);
- percentages that don't sum to 100 are rejected;
- min-cash-flow (property test: balances net to zero, at most n−1 transfers);
- convert vs per-currency balances;
- `Decimal` parsing ("1,234.50", "¥3000" rejected with a helpful error as the tool gets a separate
  currency argument).

**Tools** (using `FakePlatform`, `memory_stores()`, and an in-memory ledger repo plus a Postgres
repo behind one interface):
- writes return a pending action and write **nothing** until `commit`;
- the summary text is exact;
- currency resolution order, including the multi-currency ask;
- ambiguous member;
- only one active trip;
- `end_trip` summary;
- delete permissions;
- FX order: an override beats Frankfurter, the fallback is marked approximate, and failure leaves
  null (all via `respx`);
- chat A's ledger is invisible from chat B;
- the DM path without wiring gives a helpful error.

**Integration** (marked): the migrations apply, and the Postgres repo round-trips including the
transaction rollback on failure.

## Done when

All tests pass, ruff is clean, and no float arithmetic touches money (use `Decimal` and ints).
