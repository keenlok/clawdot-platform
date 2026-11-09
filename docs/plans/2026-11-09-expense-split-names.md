# Expense split names: echo who shares, keep names as written

Status: Released in v0.6.2 (PR #25, owner approved 2026-11-09); downstream rollout tracked
privately.

## Incident

On 2026-11-09 (SGT) in a group trip chat, a member asked the bot to split an expense with
three named people. The model called `add_expense` with four `@usernames` instead of the
names as written. It had converted the names itself and, for one person, picked the handle
of a different member with a similar-looking name. Passing the name as written would have
resolved exactly through `_resolve_member`. (Real names and handles are kept out of this
public record.)

Two things made it worse:

1. The deterministic `Added:` echo for an equal split says only `split 4 ways`, so the wrong
   person was invisible unless someone read the model's own follow-up closely.
2. The correction (`update_expense`) echoed `split 4 ways → 4 ways`, which doesn't show what
   changed.

Ledger data was corrected by the group within a minute; no data repair is needed.

## Changes

All in `packages/plugins/expenses/src/chatcore_expenses/plugin.py` plus tests. No migration,
no gate or permission change, no new dependency.

### 1. Name the people in every split echo

- **Add, equal split** (immediate path near the `split {n} ways` text in `add_expense`, and
  the pending/confirmed renderer that builds `Added:` from a snapshot): render
  `split N ways: A, B, C` using member display names, in a stable order (the order of
  `share_amounts`, which follows the resolved participants). Unknown keys fall back to the
  key as today.
- **Update, shares changed** (immediate `update_expense` echo and the snapshot renderer):
  replace `split N ways → M ways` with a description of the change in names:
  - members added or removed, written as
    `split: added Carol; removed Bob`;
  - same people, different amounts: `split amounts changed (A, B, C)` or the exact
    per-person form already used for non-equal splits;
  - keep the `N ways → M ways` count in the text only when the count changed.
- Long lists: if more than 8 people, show the first 8 names and `+K more`, so the echo stays
  short in large groups.

### 2. Keep names as the user wrote them

- `paid_by` and `split_with` item descriptions on `add_expense`, `update_expense` (and any
  other tool in this plugin that takes member names, e.g. `settle_up`, trip member tools):
  `"Name as the user wrote it (display name, @username, or me). Don't convert a name into an
  @username the user didn't type."`
- `EXPENSES_INSTRUCTIONS` (cap 800, currently 746): add a short sentence, e.g.
  `"Pass people's names as written; never swap in a guessed @username."` and tighten
  existing wording to stay under 800. Same for `TRIPS_INSTRUCTIONS` (782) only if a
  sentence fits after tightening; otherwise rely on the tool descriptions.

Out of scope: changing `_resolve_member` matching, model behaviour around pronouns ("you" to
the wrong speaker), and nickname memories.

## Tests

- Equal-split add echo lists display names (immediate path and pending/snapshot renderer).
- Update echo for a member swap names who was added and removed; count-only text absent
  when the count is unchanged.
- Update echo when only amounts change.
- More than 8 participants truncates with `+K more`.
- Existing tests that assert the old `split N ways` text are updated, not deleted.
- Skill catalog test keeps instructions ≤ 800 characters.
- Update `packages/core/tests/snapshots/agent_requests_off.json` and any routing/request
  snapshots only for the changed descriptions and instructions.

## Release notes

`CHANGELOG.md` `Unreleased` → `Changed`: expense echoes now name who shares each split and
what changed on update; tool descriptions tell the model to keep names as written.

## Owned files

- `packages/plugins/expenses/src/chatcore_expenses/plugin.py`
- `packages/plugins/expenses/tests/**`
- Snapshot/fixture files that the existing tests compare tool specs or skill instructions
  against (e.g. `packages/core/tests/snapshots/agent_requests_off.json`, routing/request
  snapshots), only for the changed text
- `CHANGELOG.md` (`Unreleased` entry only)

Don't touch `gates.py`, `permissions.py`, `repository.py`, migrations, `__init__.py` or core.
Don't change tool names, argument names or types, or the stored payload shape of pending
actions (only the human-readable `echo` text and descriptions).

## Checks

```sh
export UV_CACHE_DIR="$PWD/.uv-cache"
uv sync --locked --all-packages
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check <changed .py files>
uv run pytest -q -m "not browser and not sandbox_integration"
```

Postgres-backed tests may skip in the sandbox; say which.

## Done when

- Every `Added:` echo for an equal split names all participants (≤ 8, then `+K more`).
- Every `Updated:` echo with changed shares names who was added/removed, or that amounts
  changed, with no `N ways → N ways` when the count is unchanged.
- Name-argument descriptions and `EXPENSES_INSTRUCTIONS` say to keep names as written;
  instructions ≤ 800 chars.
- Lint clean, tests pass, snapshot diffs limited to the changed text.

## Execution

Claude planned; Codex executes; Codex reviews, then Claude's final review. Owner asked on
2026-11-09 to use `gpt-6-luna` at high reasoning effort for both execution and review
(instead of the usual review model). Checks from `AGENTS.md` run before the PR.
