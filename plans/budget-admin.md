# Runtime daily token budget administration

Read first: `ARCHITECTURE.md` §6a (security baseline, cost caps, "Daily token budget across all chats"),
`packages/core/src/chatcore/limits.py`, `packages/core/src/chatcore/administration/`
(`store.py`, `pg.py`, `plugin.py`), `packages/core/src/chatcore/access.py`,
`packages/core/src/chatcore/migrations/` (latest is `0008_message_attribution.sql`), `packages/core/tests/test_administration.py`,
`packages/core/tests/test_administration_pg.py`, `packages/core/tests/test_restrictions.py`,
and the wiring in `apps/sb_bot/src/sb_bot/app.py` (around `Limits(` and `AdministrationPlugin(`).

Setup: `export UV_CACHE_DIR="$PWD/.uv-cache"` then `uv sync --all-packages`.

## Goal

The global daily token budget (`DAILY_TOKEN_BUDGET`, env, via `LimitsSettings`) is the §6a
cost backstop: one shared LLM key pays for every chat, research job and memory extraction.
Today it can only be changed by editing `.env` and recreating the container. Add:

1. a persisted, audited **runtime override** of the daily budget, plus an optional
   **today-only extra allowance**, changed only by **owners** via confirmed DM `/admin`
   commands (never by LLM tools);
2. **one accessor** on `Limits` that every budget check uses, so the override applies
   everywhere;
3. a **Postgres mirror of today's usage counter**, so a Valkey restart (Valkey runs without
   persistence) cannot silently reset the cap.

The env value stays the default: with no override, behaviour is exactly as today. Lowering
below the env value is allowed (owners may cut cost); the hard bounds below are the floor.

## You own

- `packages/core/src/chatcore/limits.py`
- `packages/core/src/chatcore/access.py` (add one accessor only)
- `packages/core/src/chatcore/administration/store.py`, `administration/pg.py`,
  `administration/plugin.py`, `administration/__init__.py` (exports only if needed)
- `packages/core/src/chatcore/migrations/0009_budget_admin.sql` (new)
- `packages/core/src/chatcore/app.py` and `packages/core/src/chatcore/conversation.py`
  (budget call sites and the `/budget` command only)
- `packages/plugin-expenses/src/chatcore_expenses/plugin.py`,
  `packages/plugin-research/src/chatcore_research/plugin.py`,
  `packages/plugin-research/src/chatcore_research/quota.py`,
  `packages/plugin-memory/src/chatcore_memory/plugin.py` (budget call sites only)
- `apps/sb_bot/src/sb_bot/app.py` (wiring only)
- Tests: new `packages/core/tests/test_budget_admin.py`, new integration tests in
  `packages/core/tests/test_administration_pg.py` (append), and minimal edits to existing tests
  that break only because a call site moved to the new accessor. Add to
  `apps/sb_bot/tests/test_admin.py` / `test_app.py` for wiring.
- `ARCHITECTURE.md`: update the one bullet "Daily token budget across all chats…" in the cost-caps
  list to mention the owner `/admin budget` override and the Postgres usage mirror. Nothing else.

You may **not** change existing migrations, the `Plugin`/`Command`/`PendingAction`/confirm
contracts, or the semantics of any existing `/admin` operation. Ask in the report if one must
change.

## 1. Limits: one accessor, override source, usage mirror (`chatcore/limits.py`)

Keep `LimitsSettings` unchanged (`DAILY_TOKEN_BUDGET` is the default). Add:

```python
DAILY_BUDGET_MIN = 100_000
DAILY_BUDGET_MAX = 100_000_000
EXTRA_ALLOWANCE_MIN = 10_000
EXTRA_ALLOWANCE_MAX = 100_000_000


@dataclass(frozen=True, slots=True)
class BudgetOverride:
    daily_tokens: int | None  # None = use env default
    extra_tokens: int | None  # today-only extra allowance
    extra_day: date | None  # BUDGET_TZ date the extra applies to
    set_by: str | None
    set_at: datetime | None


class BudgetUsageStore(Protocol):
    async def load(self, day: date) -> int | None: ...
    async def save(self, day: date, tokens: int) -> None: ...  # monotonic: never lowers
```

`Limits.__init__` gains keyword-only `budget_override: Callable[[], BudgetOverride | None] | None
= None` and `usage_store: BudgetUsageStore | None = None` (same style as `user_limit`).

Public methods (keep `_day()` working for existing callers; add a public `day()` returning a
`date` in `BUDGET_TZ`):

- `def base_budget(self) -> int` — override `daily_tokens` if set, else `settings.DAILY_TOKEN_BUDGET`.
- `def extra_today(self, now: datetime | None = None) -> int` — `extra_tokens` if
  `extra_day == day(now)`, else 0.
- `def daily_budget(self, now=None) -> int` — `base_budget() + extra_today(now)`. **This is the
  only place the effective budget is computed.**
- `async def budget_exhausted(self) -> bool` — `await self.budget_used() >= self.daily_budget()`.
- `def reset_at(self, now=None) -> datetime` — next midnight in `BUDGET_TZ` (aware).
- `check()` must use `daily_budget()` and the same restored-usage read as `budget_used()`.

Defensive: if the override source raises or returns values outside the bounds, ignore that
field and fall back to the env default (log a warning with the error class name only).

Usage mirror:
- `record(tokens)`: under the existing cache lock, if the cache key is missing and a
  `usage_store` exists, seed `current` from `usage_store.load(day)` first; then write the cache
  as today, then `await usage_store.save(day, current)`. A store failure logs
  `log.warning("budget usage mirror failed error=%s", type(exc).__name__)` and does not raise.
- `budget_used()`: read the cache; if the key is missing (`None`) and a `usage_store` exists,
  load the persisted value; if positive, write it back to the cache with the same
  until-midnight TTL `record` uses (factor out a `_ttl(now)` helper) and return it. Load
  failure: warning (class name only), return the cache value (0). Do not hit Postgres when the
  cache key exists.
- `PgBudgetUsage(db: Database)` in `limits.py` implementing the protocol:
  `INSERT INTO budget_usage(day, tokens) VALUES ($1,$2) ON CONFLICT (day) DO UPDATE SET
  tokens=GREATEST(budget_usage.tokens, EXCLUDED.tokens), updated_at=now()`; `load` selects by
  day. Import `Database` from `chatcore.store.base`.

## 2. Migration `0009_budget_admin.sql`

```sql
CREATE TABLE administration_budget (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    daily_tokens bigint CHECK (daily_tokens BETWEEN 100000 AND 100000000),
    extra_tokens bigint CHECK (extra_tokens BETWEEN 10000 AND 100000000),
    extra_day date,
    set_by text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((extra_tokens IS NULL) = (extra_day IS NULL))
);

CREATE TABLE budget_usage (
    day date PRIMARY KEY,
    tokens bigint NOT NULL CHECK (tokens >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

`budget_usage` holds one row a day and no user data, so no purge job is needed.

## 3. Administration store (`administration/store.py`, `pg.py`)

New operations, all **global scope only** (`global_scope(platform)`, `user=None`) and **owner
only**:

| operation | value | effect | audit `detail` |
|---|---|---|---|
| `budget_set` | int in `[DAILY_BUDGET_MIN, DAILY_BUDGET_MAX]` | set `daily_tokens` | `budget=<n>` |
| `budget_reset` | `None` | `daily_tokens = NULL` (env default) | `budget=default` |
| `budget_extra` | int in `[EXTRA_ALLOWANCE_MIN, EXTRA_ALLOWANCE_MAX]` | set extra for `change.day` (replaces, does not add) | `extra=<n> day=<YYYY-MM-DD>` |
| `budget_extra_reset` | `None` | clear extra | `extra=none` |

- `AdminChange` gains `day: date | None = None` (default keeps all existing callers valid).
  `validate_change`: `day` must be a `date` for `budget_extra` and `None` for every other
  operation. Values must be `type(value) is int` (reject `bool`).
- `validate_change` currently only allows global scope for block/pause/unblock/limit; add the
  four budget ops to the allowed global set and require global scope for them; they take no
  user target.
- `authorize_change`: the four budget ops are owner operations (`PermissionError` otherwise).
  Moderators, setup assignees and `admin_users` who are not owners are refused.
- `AccessSnapshot` gains `budget: BudgetOverride | None = None` (import from `chatcore.limits`;
  check there's no import cycle — `limits.py` must not import administration).
- `MemoryAdministrationStore` and `PgAdministrationStore` implement the ops with the same
  revision bump + `administration_audit` row as other ops (`chat_key` = global scope key,
  `subject_key` NULL, `set_by` = actor key, `set_at` = now). Pg `snapshot()` reads the
  `administration_budget` row (absent row → `None`). Upsert the singleton row; a reset that
  leaves both `daily_tokens` and extra `NULL` may keep or delete the row — either is fine, but
  snapshot must return `None` or an all-None override consistently.
- `AccessPolicy.budget_override(self) -> BudgetOverride | None` returning
  `self._snapshot.budget if self._snapshot else None` (mirrors `user_limit`).

## 4. `/admin budget` commands (`administration/plugin.py`)

`AdministrationPlugin.__init__` gains keyword-only `limits: Limits | None = None`.

Commands (DM only, same rules as the rest of `/admin`; all owner-only — for a non-owner,
including full `admin_users`, setup assignees and moderators, `/admin budget …` replies
"Owner access is required for this administration change." and nothing is queued; in a group
the existing "Run /admin in a private chat…" reply applies):

- `/admin budget` — read-only status, no confirmation:
  ```
  Daily token budget:
  Used today: 1,234,567 / 12,000,000 (10.3%)
  Base: 10,000,000 (override by tg:1 at <time>)   |  Base: 2,000,000 (default from env)
  Extra today: 2,000,000                           (line omitted when 0)
  Remaining: 10,765,433
  Resets: <reset_at in the viewer's display zone via format_instant> (midnight Asia/Singapore)
  ```
  If `limits is None`, reply "Budget administration is unavailable." Times use the same
  `display_service(ctx.stores).resolve(...)` + `format_instant` as `_restrictions`.
- `/admin budget set N` → `budget_set`.
- `/admin budget default` → `budget_reset`.
- `/admin budget extra N` → `budget_extra` with `day = limits.day()`.
- `/admin budget extra clear` → `budget_extra_reset`.

`N` parsing (`_parse_tokens`): `^[1-9][0-9_,]*$` with `_`/`,` stripped, or a number with `k`/`m`
suffix (`500k`, `15m`, `1.5m` → integer tokens; reject results that aren't whole numbers).
Out-of-range or unparsable → a short message naming the bounds, e.g. "Daily budget must be
between 100,000 and 100,000,000 tokens." (not the generic usage text).

Confirmation: build the `PendingAction` exactly like other changes (kind `"change"`,
`expected_revision`). Summaries, e.g. "Confirm: set the daily token budget to 15,000,000
(currently 10,000,000).", "Confirm: reset the daily token budget to the env default
(2,000,000).", "Confirm: add an extra 2,000,000 tokens for 2026-10-31 only.", "Confirm: clear
today's extra token allowance.". For `budget_extra` the payload carries an extra `"day"` key
(ISO string). `_commit` must accept the payload key set
`{"operation","group","user","value","expected_revision"}` for every op, plus `"day"` only
when `operation == "budget_extra"`; parse it with `date.fromisoformat`; if it is not
`limits.day()` at commit time, reply "Today's date changed since this request. Run the
command again." and do not apply. Add the four ops to the `_commit` operation allowlist.
Success reply: "Saved budget_set everywhere." style is acceptable, but prefer a clear message
like "Daily token budget saved: 15,000,000." — keep it short.

Also: add `/admin budget` lines to `USAGE` and the owner section of `_menu`; `_audit` already
prints `detail`, which is enough.

Security: budget values come only from the command text typed by the owner. Do **not** add any
LLM tool, and do not expose the setter to `AgentResponder` tools. No user text in logs.

## 5. Route every budget check through the accessor

Replace each `budget_used() >= …settings.DAILY_TOKEN_BUDGET` (and the `<` in research
`_budget_ok`) with `await limits.budget_exhausted()` (or `daily_budget()` where a number is
needed):

- `chatcore/app.py` (`_reopen_compact`)
- `chatcore/limits.py` `check()`
- `chatcore/conversation.py` (`/budget` command — show `used / daily_budget()` and, when an
  extra applies, the same numbers as `/admin budget`'s "Used today" line; keep its
  `admin_only=True`; the no-limits fallback stays)
- `chatcore_expenses/plugin.py`, `chatcore_research/plugin.py` (two sites),
  `chatcore_research/quota.py`, `chatcore_memory/plugin.py`

After the change, `grep -rn "DAILY_TOKEN_BUDGET" packages apps --include=*.py | grep -v tests`
must show only `limits.py`.

## 6. Wiring (`apps/sb_bot/src/sb_bot/app.py`)

```python
limits = Limits(
    cache,
    settings_from_env(LimitsSettings, env),
    user_limit=access.user_limit,
    budget_override=access.budget_override,
    usage_store=PgBudgetUsage(db) if db else None,
)
administration = AdministrationPlugin(..., limits=limits)
```

## Tests

Unit (`packages/core/tests/test_budget_admin.py`, fakes/in-memory cache + `MemoryAdministrationStore`;
reuse helpers/fixtures from `test_administration.py` / `test_restrictions.py` where they exist):

- Limits accessor: default = env; override applies; extra applies only on its day (use a fixed
  `now`); out-of-bounds or raising override source falls back to env; `budget_exhausted`
  true/false at the boundary; `check()` refuses at the override cap, not at the env cap
  (override both higher and lower than env).
- Every call site uses the override: for each of conversation `/budget`, app `_reopen_compact`
  (skip when exhausted), research quota + `_budget_ok`, expenses receipt read, memory
  extraction — a test where the env cap is exceeded but the override is higher (proceeds) and
  /or the override is lower than usage (blocked). Put plugin-specific ones in that plugin's
  tests directory (you own adding tests there).
- Usage mirror: `record` saves to the store; with an emptied cache, `budget_used` restores the
  persisted value and re-seeds the cache; `record` after cache loss continues from the
  persisted value (not from 0); store save/load exceptions don't raise; Pg-free fake store
  only in unit tests.
- Parsing/bounds: `99_999`, `100_000`, `100_000_000`, `100_000_001`, `0`, `-5`, `abc`, `1.5m`,
  `15m`, `500k`, `1,000,000`, `true`; extra bounds likewise.
- Authorization: owner → confirmation queued; full admin (in `admin_users`, not owner),
  setup assignee, moderator → refused, nothing queued; command in a group → refused;
  `authorize_change`/store `apply` with a non-owner actor raises `PermissionError` even if a
  forged pending action is committed.
- Confirmation: nothing changes before confirm; confirm applies; stale revision rejected;
  `budget_extra` confirmed on a different day rejected; payload with unexpected keys (or `day`
  on a non-extra op) rejected.
- Audit: each op writes one audit row with actor, global scope, and the `detail` above;
  `/admin audit` shows it.
- `/admin budget` status text: used, effective, base source (env vs override by whom), extra
  line, remaining, reset time.

Integration (append to `packages/core/tests/test_administration_pg.py`, skip when Postgres is
unreachable, same DSN default `127.0.0.1:55432`): migration applies; budget ops round-trip
through `PgAdministrationStore` with audit rows; DB CHECKs reject out-of-range values;
`PgBudgetUsage` save is monotonic (`save(d, 10)`, `save(d, 5)` → `load(d) == 10`).

App (`apps/sb_bot/tests`): `build_app` wires `budget_override` and passes `limits` to the
administration plugin (console/no-DB path: `usage_store is None`).

## Done when

- `uv run pytest -q -m 'not integration and not sandbox_integration'` passes (one known
  unrelated failure may remain: `packages/plugin-polls/tests/test_service.py::test_real_core_button_confirmation_preserves_topic`).
- `uv run ruff check .` and `uv run ruff format --check .` are clean.
- The grep in §5 shows `DAILY_TOKEN_BUDGET` only in `limits.py`.
- Out of scope: deployment, `.env` changes, per-chat budgets, LLM tools for budgets.
