# Bucket A: direct writes for the remaining low-stakes tools

Read first: `ARCHITECTURE.md` §6a (security baseline), `docs/plans/expense-ux.md` §1 (the direct-write class
this task reuses; the owner approved relaxing "write tools need a click" for low-stakes,
reversible writes). Then read `packages/core/src/chatcore/tools/registry.py` (`Tool.direct`,
`direct_after`, `direct_write_allowed`, the write-tool check in `ToolRegistry.call`),
`packages/core/src/chatcore/agent.py` (the `result.pending_action` branch in
`_respond_after_typing`), `packages/core/src/chatcore/confirm.py` (`commit_direct`),
`packages/core/tests/test_agent_direct_writes.py`, and how `plugin-expenses` registers
`add_expense`/`delete_expense` with `direct=True` and words its echoes. Then read all of
`packages/plugin-polls/`, `packages/plugin-jobs/`, `packages/plugin-memory/` (source and tests),
the `update_trip` paths in `packages/plugin-expenses/src/chatcore_expenses/plugin.py` and
`repository.py`, `tests/test_tool_audit.py`, `apps/sb_bot/tests/test_expense_ux_e2e.py` (console +
Postgres e2e pattern) and `apps/sb_bot/src/sb_bot/prompts/system.md`.

Environment: run `export UV_CACHE_DIR="$PWD/.uv-cache"` before any `uv` command; `uv sync
--all-packages` has been run. No Postgres in your sandbox: integration tests must skip when it is
unreachable (follow the `TEST_PG_DSN` pattern in `packages/plugin-jobs/tests/test_pg_jobs.py`).

## How the mechanism works (no core change needed)

A tool registered with `writes=True, direct=True` still returns a `PendingAction` exactly as a
confirmed tool does. The agent then calls `direct_write_allowed(tool, ctx.turn)`: when the turn
read nothing untrusted it commits the action through the plugin's own handler
(`ConfirmService.commit_direct`), posts the handler's return string to the chat as the **echo**
(reply in the same topic) and lets the turn continue. When the turn read untrusted content it
falls back to the normal confirmation button. So for each tool below the work is:

1. flag it `direct=True`;
2. make its handler's `commit` return a good one-message echo (the same text is used after a
   confirmation click);
3. make sure an "undo" exists, either a tool the model can call with the id shown in the echo, or,
   where that would need a new store, a sentence in the echo saying how to reverse it;
4. keep every permission, toggle and validation check exactly where it is today (both at proposal
   time and again at commit time).

Do **not** change `registry.py`, `agent.py`, `confirm.py` or anything else in `packages/core`.

## You own

- `packages/plugin-polls/src/chatcore_polls/plugin.py`, `service.py` and `packages/plugin-polls/tests/**`
- `packages/plugin-jobs/src/chatcore_jobs/plugin.py`, `service.py`, `repository.py` and
  `packages/plugin-jobs/tests/**`
- `packages/plugin-memory/src/chatcore_memory/plugin.py` (and `repository.py` only if needed) and
  `packages/plugin-memory/tests/**`
- `packages/plugin-expenses/src/chatcore_expenses/plugin.py` (`update_trip` registration, its
  echo text and the commit-message wording in `repository.py` for `update_trip` only) and
  `packages/plugin-expenses/tests/**`
- `apps/sb_bot/src/sb_bot/prompts/system.md` (the expenses/trip sentence about confirmation, the
  memory paragraph, and one new short paragraph for polls/reminders/watches)
- `tests/test_tool_audit.py`, new `apps/sb_bot/tests/test_direct_writes_e2e.py`

You may **not** edit `packages/core/**`, any existing migration file, `packages/plugin-web/**`,
`packages/plugin-browse/**`, `packages/plugin-release-control/**` (protected), `ARCHITECTURE.md` or
anything under `docs/plans/`. **No new migrations**: none of the undos below needs one. If you find one
is truly needed, stop and explain in the report.

## 1. `create_poll` (plugin-polls)

- Register with `direct=True`. Update the description: "Post a native poll in this group. It is
  posted immediately (after untrusted content, the requester confirms first)."
- The posted poll is the echo. Change the `create` commit return so that a successful post
  returns one line: `Poll #<n> posted. To close it early, reply to it with /closepoll or use
  /closepoll <n>.` and, when it has a close time, `Poll #<n> posted; it closes at <label>. ...`
  (same closing hint). Keep the existing failure texts ("Couldn't send the poll; nothing was
  posted.", the "already posted"/"already handled" idempotency texts, the `rejected` text).
  `PollsService.send` is shared with the `/poll` command: keep `/poll`'s user-visible behaviour
  unchanged (return the new text from the commit path only, e.g. via a small wrapper, or change
  `send` to return the number and build the text in each caller; your choice, keep it small).
- Unchanged: `/polltools` toggle (disabled → "Polls are off in this group. A group admin can
  enable them with /polltools on."), group-only, allowlist and platform checks, revision check at
  commit. `configure` stays confirmed (it's a command, not a tool).
- Undo: none beyond the hint above (Telegram polls can't be deleted; closing is the reversal).

## 2. Reminders and website watches (plugin-jobs)

Tools `create_reminder`, `cancel_reminder`, `create_website_watch`, `cancel_website_watch` get
`direct=True`. `configure_jobs` stays confirmed (not direct). Slash commands (`/remind`,
`/cancelreminder`, `/watch`, `/watchrendered`, `/cancelwatch`, `/jobtools`) keep the confirmation
flow unchanged; this task is only about model tools.

Descriptions: drop "confirmed"/"Request"; say the write applies immediately and the bot posts the
result, and that `cancel_*` / `restore_*` are the undo.

### Echo texts (`JobsService.commit` return values)

Use the requester's display time zone for times (`display_service(ctx.stores).resolve(...)` and
`format_instant`, as `list_jobs` does). Full job id (the cancel tools need the full UUID).

- create reminder: `Reminder set for <time>: "<text>" (id <uuid>). Say "undo" to cancel it.`
- create watch: `Watching <url> every <interval> seconds (id <uuid>). The first check is silent;
  only readable-text changes are reported. Say "undo" to stop it.` Rendered watches add
  "(headless browser)" after the URL.
- cancel: `Cancelled reminder <uuid> ("<text>", was due <time>). Say "undo" to bring it back.` /
  `Stopped watching <url> (id <uuid>). Say "undo" to restart it.` Keep the existing note that a
  notification already reserved by the worker may still arrive.
- restore (below): `Restored reminder <uuid>: "<text>" at <time>. Say "undo" to cancel it.` /
  `Restarted watching <url> every <interval> seconds (id <uuid>). Say "undo" to stop it.`
- Keep the existing failure texts (settings changed, authorization or scope changed, no matching
  active job, invalid request).

The reminder text and URL appear only in the chat echo, never in logs.

### Undo

- Undo of a create = the existing `cancel_reminder` / `cancel_website_watch` with the id from the
  echo (the echo is logged to chat history, so a later "undo" turn sees it).
- Undo of a cancel = new direct tools `restore_reminder(job_id)` and
  `restore_website_watch(job_id)` (`writes=True, direct=True`), each a `PendingAction` of a new
  kind `restore` handled by `JobActions` (add `"restore"` to `action_handlers()`).
- To tell a user's cancel from a cancel by `/jobtools ... off` or an interval-floor change, the
  user cancel in `JobsRepo.cancel` also sets `last_delivery_state='cancelled_by_user'` (the
  configure paths set nothing or `interval_floor_raised`, so those jobs are never restorable;
  the `/jobtools off` promise "re-enabling will not restore them" holds).
- `JobsService.request_restore(ctx, kind, raw_id)`: same id validation, scope, owner-or-current-
  group-manager rule as `request_cancel` (normal `_allowed`, not `allow_disabled_dm`). The job
  must be `status='cancelled'`, `last_delivery_state='cancelled_by_user'`, `terminal_at` within
  `JobsSettings.restore_window_seconds` (new setting, default 86400, must be > 0), same kind. The
  tool must be enabled for the scope (disabled → the same clear error as create, see below).
  Reminders: `next_run_at` must still be in the future, else error "That reminder's time has
  passed; create a new one.". Watches: re-validate URL, interval floor/ceiling and the rendered
  checks exactly as create does. Payload carries scope, actor, kind, job_id, expected revision,
  all_members.
- `JobsRepo.restore(scope, actor, kind, job_id, *, all_members, expected_revision, now)`: in one
  transaction with the same advisory locks as `create`, re-check enabled + quota (global, chat,
  user, rendered) exactly like `create` (factor the shared quota/limit checks into a private
  helper used by both; don't duplicate the SQL), then `UPDATE scheduled_jobs SET
  status='active', terminal_at=NULL, last_delivery_state=NULL, lease_token=NULL,
  lease_until=NULL, revision=revision+1, next_run_at=GREATEST(next_run_at, $now)` with the full
  scope/kind/status/marker/revision predicate (owner unless `all_members`). Return the `Job` or
  `None`; raise `JobConflict` on disabled/quota.
- `JobsService.commit` handles `restore` with the same scope/actor re-check and manager re-check
  as `cancel`.

### Disabled tools must stay clear

Today `request_create` raises `ValueError("This job tool is disabled for this scope.")`. Change
that text (create and restore) to name the command: `Reminders are off here. A group admin can
enable them with /jobtools reminders on.` / `Website watches are off here. A group admin can
enable them with /jobtools watches on.` Everything else (`can_use_scope`, DM setting, URL
provenance via `ctx.turn.seen_urls`, intervals, quotas, rendered limits, revision checks at
commit) is unchanged.

## 3. `remember` and `forget` (plugin-memory)

Today both are `writes=False`: they write immediately on a clean turn and return a pending action
only after untrusted reads. Move them to the explicit class:

- Register both with `writes=True, direct=True` (no `direct_after`).
- The tool handlers **always** return a `PendingAction` (or a `ToolResult.error`); they never
  write. Validation stays in the handler: fact length (1..`max_chars`); `forget` of an unknown id
  is now `ToolResult.error("No memory #<id> in this chat.")` (a writes tool may not return plain
  content without a pending action).
- Payloads: `remember` → `{"fact", "about", "pinned"}`; `forget` → `{"memory_id"}`. The
  **security rule holds**: the model never sets `source` or `created_by`; the commit always uses
  `source="explicit"`, `created_by=ctx.user.ref`, and resolves `about` at commit time with the
  existing `_resolve_subject`. Keep `test_memory_internal_arguments_cannot_bypass_confirmation`.
- The summary (shown on the confirmation button after untrusted reads) stays as today: `Save this
  memory exactly as written: "<fact>"` / `Delete memory #<id>: "<text>"`.
- `_MemoryActionHandler.commit` does the write (call `_save` / `_delete_memory` directly, not
  through the tool path) and returns the echo:
  - new: `Saved memory #<id>: "<fact>". Say "undo" to forget it.`
  - deduplicated update: `Updated memory #<id>: "<new>" (was "<old>"). Say "undo" to put the old
    wording back.` (capture the old text from `_similar` before updating; `_save` may return it).
  - forget: `Forgot memory #<id>: "<text>". Say "undo" to save it again.` (fetch the row before
    deleting so the echo has the text; missing at commit → `No memory #<id> in this chat.`).
- Undo in words, no new store: undo of a save = `forget` with the echoed id; undo of a forget or
  an update = `remember` with the echoed text (memories are hard-deleted and keeping deleted rows
  would need a migration; the echo carries the text instead). The memory paragraph of
  `system.md` says this.
- Unchanged: the auto-extraction path (`after_turn` → `_save`, `source="auto"`), `/memory`,
  `/forget`, recall, indexing, purge.
- Log lines stay ids/counts only.

## 4. `update_trip` (plugin-expenses)

- Register with `direct=True`. Description: drop "Every write requires confirmation."; say the
  change applies immediately and the bot posts what changed.
- Echo: the commit text becomes `Updated trip <name>:` + the existing change lines (which show old
  → new), then `Say "undo" to change it back.` (both `MemoryLedgerRepo` and
  `PostgresLedgerRepo`). Undo in words: the model reverses it with another `update_trip` using
  the old values from the echo. This needs no event log. Note the one gap in the report: a date
  that was unset before cannot be cleared again by `update_trip` (out of scope).
- All validation, the trip-status lock, the name-uniqueness check and
  `_check_trip_change_usage` stay at both proposal and commit time.
- Do **not** make any other trip tool direct. `settle_up`, `start_trip`, `end_trip`,
  `reopen_trip`, `activate_trip`, `set_default_trip`, `set_fx_rate` keep the button.

## 5. System prompt (`system.md`)

Keep it short; edit only these sentences:
- Expenses paragraph: `update_trip` changes now apply immediately with an echo; the
  still-confirmed list becomes "Settling up and trip lifecycle changes (start, end, reopen,
  activate, default trip, FX override) still need a confirmation click."
- New paragraph: polls, reminders and website watches are posted/created/cancelled immediately and
  the bot posts the result; don't repeat it. "undo" after creating a reminder/watch → cancel it
  by the id in the echo; after cancelling → `restore_reminder`/`restore_website_watch`. Polls are
  undone by closing (`/closepoll`).
- Memory paragraph: remember/forget apply immediately with an echo; "undo" → forget the echoed id,
  or remember the echoed text again.

## Not in this branch (record in your report, don't build)

- `set_display_time_zone` / `reset_display_time_zone` (`chatcore/display_time_plugin.py`) apply
  directly but **refuse** on untrusted turns; a later branch moves them onto this class.
- Slash commands keep their confirmation flow.

## Tests

Unit tests use fakes; Postgres tests skip when `TEST_PG_DSN` is unreachable.

**Per-tool flag/audit (`tests/test_tool_audit.py`)**
- Replace `test_only_approved_expense_tools_are_direct` with `test_only_approved_tools_are_direct`
  covering **every** plugin, including polls and jobs: build `PollsPlugin` and `JobsPlugin` with
  fakes just to list their `tools()` (no DB needed). Exactly this set is direct: the six expense
  tools already there + `update_trip`, `create_poll`, `create_reminder`, `cancel_reminder`,
  `restore_reminder`, `create_website_watch`, `cancel_website_watch`, `restore_website_watch`,
  `remember`, `forget`. And none of `settle_up`, `start_trip`, `end_trip`, `reopen_trip`,
  `activate_trip`, `set_default_trip`, `set_fx_rate`, `configure_rendered_fetch` (wherever it is
  registered), `request_extension`, `configure_jobs` is direct.
- `test_writes_require_confirmation`: `remember`/`forget` now return a pending action **without**
  setting `read_untrusted` (drop that special case); the store snapshot is unchanged after the
  call (the tool never writes). Every direct tool is also `writes=True`.

**Polls** (fake poll platform, as the existing tests do; PG tests under `test_pg_polls.py`)
- direct commit returns `Poll #n posted. ...` and the platform got exactly one `send_poll`;
  with close time, the label is in the echo.
- `/polltools off` → `create_poll` returns the clear admin error, nothing pending, no send.
- DM / disallowed chat → error, nothing pending.
- `/poll` command output unchanged.
- agent-level: a `create_poll` tool call on a clean turn is posted with no confirmation row;
  after an untrusted tool result in the same turn it asks for confirmation instead (use the
  `test_agent_direct_writes.py` pattern with the real `PollsPlugin` tool and a fake poll
  platform, or the e2e below if the console platform supports polls).

**Jobs** (`test_service.py`, `test_plugin.py` with fakes; `test_pg_jobs.py` for repo SQL)
- create reminder/watch echo text and id; times in the requester's display zone; no reminder text
  or URL in any log record (`caplog`).
- cancel echo; `cancel` sets the `cancelled_by_user` marker.
- restore: owner restores own cancelled job; a non-owner non-manager can't; a manager can restore
  another member's; refuses after the window, a passed reminder time, a job cancelled by
  `/jobtools off` or by an interval-floor raise, a disabled tool (clear `/jobtools` error), a full
  quota, a stale revision; a watch re-checks interval and rendered limits.
- disabled tools: `create_*`/`restore_*` errors name `/jobtools reminders on` / `watches on`.
- `configure_jobs` is still a confirmed (non-direct) write and its manager check is unchanged.
- PG: `restore` SQL scoping (another chat/thread/kind can't be restored), quota race uses the
  advisory locks, `GREATEST(next_run_at, now)`.

**Memory** (`test_memory.py`; `test_integration.py` on PG)
- `remember`/`forget` tool calls never write; the commit writes and returns the echoes above
  (new, dedupe update with old text, forget with text); unknown id is an error.
- commit uses `source="explicit"` and `created_by` = the committing user even if the payload is
  tampered to include `source`/`created_by` (ignored).
- auto-extraction still saves with `source="auto"`.

**update_trip** (`plugin-expenses/tests`)
- echo format for Memory and Postgres repos; direct flag; validation and the usage check still run
  at commit; other trip tools still confirmed.

**E2E (`apps/sb_bot/tests/test_direct_writes_e2e.py`, integration, console + Postgres, isolated
schema like `test_expense_ux_e2e.py`)** with `ScriptedProvider`:
- enable reminders and watches through the confirmed `/jobtools` flow (or the repo directly as
  setup), then a clean turn `create_reminder` → echo posted, no pending row, job active; "undo"
  turn `cancel_reminder(<id>)` → cancelled; `restore_reminder(<id>)` → active again.
- `remember` on a clean turn → echo, memory saved; `forget` → echo, gone.
- `update_trip` on a clean turn → echo with old → new.
- a turn where an untrusted tool (a fake registered tool returning `untrusted=True`, as the
  existing e2e does) runs before `create_reminder` / `remember` → confirmation requested, nothing
  written until the button press; a press by another user does nothing.
- `create_poll` too if the console platform implements polls; otherwise cover it in the agent-level
  polls test above.

## Done when

- `uv run pytest -q` passes (Postgres tests skip in your sandbox) and `uv run ruff check .` and
  `uv run ruff format --check .` are clean.
- The direct set in the audit test is exactly the list above; the confirmed list is not direct.
- No file outside "You own" changed; no migration added; nothing in `packages/core` changed.
- Report lists: the words-only undos (polls, memory forget/update, `update_trip`), the
  `update_trip` unset-date gap, and the display-time tools as a later migration.
