# Google output tools PR 1: apostrophe fix, member creates, output journal, preview switch

Execution plan for PR 1 (v0.21.0) of
[General Google Sheets and Docs output tools](2026-11-16-google-office-tools.md). Read that plan's
"Owner decisions" and "Design" sections first (only the PR 1 parts), then `AGENTS.md`
("Plugin module layout", "Moving or splitting code", "Changelog").

Read these files before changing anything:
`packages/plugins/google/src/chatcore_google/{settings.py,permissions.py,gates.py,plugin.py,service.py,render.py,repository.py,link_sharing.py,expense_service.py,itinerary_service.py,edit_service.py,EXPLAIN.md}`,
`packages/plugins/google/src/chatcore_google/migrations/*.sql`, and the tests in
`packages/plugins/google/tests/` (especially `test_service.py`, `test_plugin.py`,
`test_refs_render.py`, `test_pg_google.py`, `conftest.py`).

## You own

- `packages/plugins/google/src/chatcore_google/`: `settings.py`, `permissions.py`, `gates.py`,
  `plugin.py`, `service.py`, `render.py`, `repository.py`, `link_sharing.py`,
  `expense_service.py`, `itinerary_service.py`, `EXPLAIN.md`
- new `packages/plugins/google/src/chatcore_google/migrations/0006_outputs.sql`
- `packages/plugins/google/tests/**` (edit and add)
- `CHANGELOG.md` (one entry under `Unreleased`)

You may **not** edit: `packages/core/**`, other plugins, `apps/**`, `workbook.py`,
`edit_snapshot.py`, `edit_service.py` beyond what section 1 needs, any applied migration
(`0001`–`0005`), `pyproject.toml`, `uv.lock`. Report if one must change.

## 1. Apostrophe fix

- `render.literal_rows`: write `str` values as `{"stringValue": value}` unchanged. Delete
  `neutralize` if nothing else uses it (grep the repo, including other plugins and apps).
  Sheets stores `stringValue` as literal text and never evaluates it (verified against the live
  API in v0.18.0), so the prefix was only visible noise.
- Check every comparison that assumed the prefix (read-backs, previews, `edit_service`
  `_confirmed_sheet_values`, itinerary rows) and update the tests that expected `"'=BAD()"`
  (`test_service.py` ~line 944, `test_refs_render.py`
  `test_sheet_values_are_literal_and_formula_neutralized`). Rename that test to say values are
  literal `stringValue`s.
- New test: across `google_create_sheet`, `google_append` (Sheet) and itinerary writes, no
  request body sent to the fake client contains `formulaValue`, and `"=IMPORTXML(\"x\")"`,
  `"-5 apples"`, `"+65 1234"`, `"@here"` are written verbatim as `stringValue`.

## 2. Settings (`settings.py`)

Add to `GoogleSettings`:

- `create_users: Literal["writers", "members"] = "writers"` (env `GOOGLE_CREATE_USERS`)
- `creates_per_user_day: int = Field(default=20, ge=1, le=500)` (env `GOOGLE_CREATES_PER_USER_DAY`)

## 3. Permission tiers (`permissions.py`, `gates.py`, `service.py`)

This is a permission change.

- `authorize(..., write=False, configure=False, create=False)`. When `create=True` and
  `write=False`: all existing scope checks apply (configured, platform/chat checks, DM/chat
  allow, restriction, chat setting on), then the user must be a writer (bot admin or
  `settings.writers`) **or** `settings.create_users == "members"`. Add a helper
  `is_writer(settings, access, user_ref) -> bool` in `permissions.py` and use it in both places.
- Tiers per tool:
  - `google_create_doc`, `google_create_sheet`, `google_append`: **create** tier in the gate.
  - All other write tools keep the **write** tier. `configure_google` keeps configure.
- Runtime rules in `service.propose` / `commit` (handlers always check; the gate is not the only
  check):
  - Creates: `authorize(create=True)`.
  - `google_append`: if the target is a journaled output in this chat/topic (section 4) and the
    requester is its `requester_key` → create tier suffices; otherwise require write tier
    (`authorize(write=True)`). Re-check the same at commit.
- **Forced link sharing for non-writers.** For a non-writer requester of a create:
  - `google_create_doc`: resolved access (existing `resolve_link_access`) is raised to
    `"viewer"` if it is `"private"`. An explicit `link_access="private"` from a non-writer is
    raised too, and the preview says so ("Access: anyone with the link can view (required for
    files you create)").
  - `google_create_sheet`: gains an optional `link_access` (`private`/`viewer`/`editor`) parameter
    and the same resolution. Writers keep today's default for Sheets: private, sharing
    unchanged, unless they pass `link_access`. Non-writers get at least viewer.
- **Link sharing for Sheets** (`link_sharing.py`): generalise `publish_doc_link_access` so the
  same checks apply to a native Sheet (`SHEET` mime) created by a confirmed `create_sheet`
  action: owned by me, app-authorized, not trashed, `canShare`, safe existing ACL, anyone
  permission with `allowFileDiscovery: false`, verified after granting. Keep the Doc path and
  its tests unchanged in behaviour; keep `publish_doc_link_access` importable (wrapper is fine).
  Sheet sharing does not need `google_edit_results` journaling; bind it to the
  `google_outputs` row instead (action ID, chat, file ID must match). If sharing fails after the
  Sheet was created, the reply links the file and says sharing failed; no retry.
- **Daily cap for non-writers.** Count creates per `(chat_key, requester_key)` in the last 24
  hours from `google_outputs` (section 4). At propose and again at commit, a non-writer at or
  over `creates_per_user_day` is refused: "You've reached today's limit of N new Google files;
  try again tomorrow or ask a bot admin." Writers are not capped.

## 4. Output journal (`migrations/0006_outputs.sql`, `repository.py`)

```sql
CREATE TABLE google_outputs (
    chat_key TEXT NOT NULL,
    file_id TEXT NOT NULL CHECK (char_length(file_id) BETWEEN 1 AND 200),
    mime TEXT NOT NULL,
    requester_key TEXT NOT NULL,
    thread_id TEXT,
    owner_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    trashed_at TIMESTAMPTZ,
    PRIMARY KEY (chat_key, file_id)
);
CREATE INDEX google_outputs_daily ON google_outputs (chat_key, requester_key, created_at);
ALTER TABLE google_settings ADD COLUMN confirm_writes BOOLEAN NOT NULL DEFAULT TRUE;
```

Repository methods (Postgres and the in-memory fallback the repository already has, if any;
match how existing methods handle `database=None`):

- `record_output(chat, file_id, mime, requester_key, thread_id, owner_id, action_id)`
  (idempotent on the primary key)
- `output(chat, thread_id, file_id) -> dict | None` (not trashed, same thread)
- `creates_today(chat, requester_key) -> int`
- `settings(chat)` keeps returning `(enabled, revision)` for existing callers; add
  `confirm_writes(chat) -> bool` (default `True` when no row) and extend `configure(...)` with an
  optional `confirm_writes` argument that updates only what is given and bumps `revision`.
- `purge`, `purge_older_than`: delete `google_outputs` rows the same way other per-chat/user
  rows are purged (for `purge_older_than`, by `created_at`).

Record an output row after the file ID is known in: `create_sheet` commit, `_create_output_doc`
(after `documentId` is returned), and the expense workbook export (`expense_service.py`, after
`write_workbook` returns a URL, including the incomplete case; parse the ID from the URL or
return it from the call site you own). A file is "an output here" only when `output()` returns
the row **and** Drive metadata at use time shows the owner account owns it and it isn't trashed.

## 5. Preview switch

- `configure_google` tool: parameters `enabled` (boolean, optional) and `confirm_writes`
  (boolean, optional); at least one required. Description must let the model map plain language:
  "Turn Google tools on/off in this chat, or turn write previews on/off (e.g. 'stop asking me to
  confirm Google files', 'always show me a preview first'); a manager confirms."
- `GooglePlugin.configure(ctx, enabled=None, confirm_writes=None)` →
  `service.propose_configure(ctx, enabled=None, confirm_writes=None)`. The pending action payload
  stores only the fields being changed plus revision and owner email. The summary states the
  change, e.g. "Turn off write previews in this chat? New Google files and edits to files the
  bot made here will happen without a confirmation step. Edits to other files, trashing and
  settings still need confirmation." Commit applies via `repo.configure`.
  Keep the existing `enabled=False` browser cleanup behaviour in `GooglePlugin.commit`.
- `/google` command: `/google confirm on|off` (`on` = previews on). Status (`/google`) adds
  "Write previews are on|off here." Update the usage message.
- **Immediate execution.** In `GooglePlugin.create_doc`, `create_sheet`, `append`: after
  `propose` returns the action, if `await repo.confirm_writes(chat)` is false **and** the action
  qualifies, call `await self.commit(action, ctx)` and return `ToolResult(<commit text>)` (no
  `pending_action`). Qualifies: `create_doc`, `create_sheet`, and `append` whose target is an
  output in this chat/topic. Everything else returns the pending action as today. Immediate
  execution goes through the same `commit` → `verify_commit` path, so every check still runs.
  Do not change `expenses_export` (keeps confirmation in PR 1).

## 6. Skill text, docs, changelog

- `GOOGLE_INSTRUCTIONS` (≤ 800 characters; there's a test) gets a short clause: when previews are
  off a write tool result is already done, report it, do not ask to confirm. Trim elsewhere if
  needed without dropping existing rules.
- `EXPLAIN.md`: describe member creates, forced link sharing, the daily cap, the preview switch
  (what it covers and what always confirms), and the output journal under Storage. Replace the
  sentence saying Sheets keep their existing sharing policy.
- `CHANGELOG.md` `Unreleased`: Changed (no apostrophe on literal text), Added (create tier,
  `GOOGLE_CREATE_USERS`, `GOOGLE_CREATES_PER_USER_DAY`, Sheet link sharing, preview switch),
  Migration (`0006_outputs.sql`), Security note (permission change; default unchanged).

## Tests

Unit tests with the existing fakes; Postgres tests in `test_pg_google.py` style (they skip when
Postgres is unreachable).

- Apostrophe: section 1 cases.
- Tiers: with `create_users="writers"` a non-writer is refused create (gate NOT_ALLOWED and
  handler PermissionError); with `"members"` a non-writer can propose and commit a create; a
  non-writer still cannot `google_edit_doc`/`google_edit_sheet`/append to a human file; a
  non-writer can append to their own output but not to another member's output; a writer can
  append to any output and human files as before.
- Sharing: non-writer `create_doc` with `link_access="private"` ends with viewer and the preview
  says why; non-writer `create_sheet` gets a verified anyone/reader permission with discovery
  off; writer `create_sheet` with no `link_access` stays private (no permission calls);
  unsafe existing ACL or failed grant → file linked, sharing failure reported, no retry.
- Cap: the 21st create in 24 hours for a non-writer is refused at propose; a commit that
  crosses the cap after propose is refused; writers aren't capped.
- Journal: each create path records exactly one row; `output()` misses for another chat,
  another thread, or a trashed file; purge removes rows.
- Preview switch: non-manager cannot propose; the summary describes the change; commit bumps
  revision and invalidates an older pending create; `/google confirm off` and the tool both work;
  `/google` status shows it. With previews off: `create_doc`/`create_sheet` return the result text
  with no pending action and the file is created; `append` to an output is immediate; `append`
  to a human file, `google_edit_doc`, `configure_google` still return a pending action.
- Behaviour-neutral default: with defaults (`writers`, previews on), existing plugin tests pass
  with only the apostrophe expectation changes. The tool list and skill tool names are unchanged
  except the new optional parameters.

## Done when

- `uv run ruff check packages services apps tests .github/scripts` is clean;
  `uv run ruff format --check` is clean on every file you changed.
- `uv run pytest -q packages/plugins/google tests` passes (Postgres tests may skip in your
  sandbox; say so).
- Everything above is covered by tests that fail if the behaviour is removed.
- Left for later (don't build): `google_create_workbook`, rich Docs, add/replace tab, trash, any
  sb_bot change, live Google API checks, version bump.
