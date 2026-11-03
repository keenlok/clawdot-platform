# Expense UX: direct undoable writes, trip members, receipt image reuse, in-topic replies

Read first: `ARCHITECTURE.md` §3 (expenses, trips), §6a (security baseline); this plan **deliberately
relaxes one §6a rule with the owner's approval** (see §1). Then read the whole of
`packages/plugin-expenses/` (source and tests), `packages/core/src/chatcore/tools/registry.py`,
`packages/core/src/chatcore/agent.py` (`_respond_after_typing`, `_images`, `_context`),
`packages/core/src/chatcore/app.py` (`_on_press`, reply-code route, `command_context`),
`packages/core/src/chatcore/confirm.py`, `pending.py`, `images.py`, `platform.py`
(`PlatformError`), `packages/adapter-telegram/src/chatcore_telegram/__init__.py` (`download`,
`_safe`) and `bridge.py` (`download`, `_request`, `TelegramBridgeHTTPClient.request`),
`packages/plugin-search/src/chatcore_search/__init__.py` (`read_attachment`),
`packages/core/src/chatcore/store/migrations.py` (migration rules), `tests/test_tool_audit.py`,
`apps/sb_bot/tests/test_multi_trip_e2e.py` (console e2e pattern) and
`apps/sb_bot/src/sb_bot/prompts/system.md`.

Environment: run `export UV_CACHE_DIR="$PWD/.uv-cache"` before any `uv` command; `uv sync
--all-packages` has been run. No Postgres in your sandbox: integration tests must skip when it is
unreachable (follow `tests/test_pg_integration.py` / `TEST_PG_DSN`).

## You own

- `packages/core/src/chatcore/tools/registry.py`, `agent.py`, `app.py`, `confirm.py`,
  `images.py`, `platform.py` (only if a helper belongs there) and their tests under
  `packages/core/tests/`
- `packages/adapter-telegram/src/chatcore_telegram/__init__.py` (`download` only),
  `packages/adapter-telegram/src/chatcore_telegram/bridge.py` (`download` only) and adapter tests
- `packages/plugin-expenses/src/chatcore_expenses/**` (new migration
  `migrations/0003_trip_members_and_expense_events.sql`) and `packages/plugin-expenses/tests/**`
- `packages/plugin-search/src/chatcore_search/__init__.py` (`read_attachment` download/error path
  only) and its tests
- `apps/sb_bot/src/sb_bot/prompts/system.md` (expenses and receipt paragraphs only)
- `tests/test_tool_audit.py`, new `apps/sb_bot/tests/test_expense_ux_e2e.py`

You may **not** edit `migrations/0001_ledger.sql` or `0002_multi_trip.sql` (applied in
production; checksums verified), `packages/plugin-release-control/**` (protected), other plugins,
`ARCHITECTURE.md` or anything under `docs/plans/`. Never delete, revert or `git checkout` files you don't
own. The `Tool`, `TurnState` and `ConfirmService` additions are sanctioned core contract changes
for this task. If you need anything else changed, explain it in the report.

## 1. Direct, undoable writes (framework)

Owner-approved change: some expense writes no longer need a confirmation click. The invariant
"a write tool returns a `PendingAction` and only the handler commits" **stays for every tool**;
what changes is who triggers the commit.

### Tool flags (`registry.py`)

```python
@dataclass(frozen=True, slots=True)
class Tool:
    ...
    writes: bool = False
    direct: bool = False
    """Opt-in: a write that is undoable and may be committed by the core without a click,
    but only while the turn is clean (see ``direct_write_allowed``). Requires ``writes``."""
    direct_after: frozenset[str] = frozenset()
    """Tools whose untrusted output does not block this direct write (e.g. read_receipt)."""
```

- `__post_init__`: `direct=True` requires `writes=True`; `direct_after` non-empty requires
  `direct=True`. Raise `ValueError` otherwise.
- The registry check at the end of `call` is unchanged for every write tool, including direct
  ones: a `writes` tool that returns neither a `pending_action` nor an error is a
  `RuntimeError`. Direct tools still build a `PendingAction` exactly as today.
- `TurnState` gains `untrusted_tools: set[str]` (names of tools that returned
  `untrusted=True` this turn). `call` adds the tool name there whenever it sets
  `read_untrusted`. Keep `read_untrusted` with its existing meaning (memory/display-time still
  use it; a receipt read still makes `remember` need a click).
- New function in `registry.py`:

```python
def direct_write_allowed(tool: Tool, turn: TurnState) -> bool:
    """True when ``tool`` may commit without a click: it opted in and nothing untrusted was read
    this turn, except output of tools it lists in ``direct_after``."""
    if not tool.direct:
        return False
    if not turn.read_untrusted:
        return True
    return bool(turn.untrusted_tools) and turn.untrusted_tools <= tool.direct_after
```

  (If `read_untrusted` is set but `untrusted_tools` is empty, something outside the registry
  set it: fail closed.)

**Relation to the display-time precedent** (`chatcore/display_time_plugin.py`, PR #4):
`set_display_time_zone`/`reset_display_time_zone` are non-`writes` tools that apply immediately
and **refuse** in a tainted turn. Expenses use the new explicit `direct=True` write class
instead, so they stay visible to `tests/test_tool_audit.py` and keep the pending-action handler as
the only commit path. In a tainted turn they **fall back to confirmation** rather than refusing,
because "add this receipt" after a web lookup should still be one click away, not a retry. The
mechanism is generic core (no expense-specific code in `registry.py`/`agent.py`/`confirm.py`);
a later branch will move `create_poll`, reminders/watches, `remember`/`forget`, `update_trip`
and the display-time tools onto it. Don't change those tools in this branch.

### Committing in the agent (`agent.py`, `confirm.py`)

In `_respond_after_typing`, where `result.pending_action` is handled:

- Look up `tool = app.tools.get(call.name)`. If `tool` and `direct_write_allowed(tool,
  ctx.turn)` and `result.pending_action.requester == message.user.ref`: commit directly via a
  new `ConfirmService.commit_direct(action, user, ctx) -> str` that finds the handler (missing →
  log and return `FAILED`), calls `handler.commit(action, ctx)`, catches exceptions like
  `_resolve` (log handler key + exception class only, return `FAILED`) and **does not save the
  action to the pending table**. Build `ctx` with `app.command_context(message.chat,
  message.user, is_dm=message.is_dm, thread_id=message.thread_id, message=message)`.
- Post the returned text immediately with `app.send(message.chat, text, reply_to=message.ref,
  thread_id=message.thread_id)` (this is the "echo"; it must land in the same forum topic).
- The tool result given to the model is: `"Applied directly (no confirmation needed). The bot
  already posted this to the chat, so don't repeat it:\n" + text`. The turn **continues** (so
  "lunch 30 and taxi 12" can add two expenses in one turn). Do not set
  `pending_action_requested`.
- Log `direct write chat=%s handler=%s` (no payload, no text).
- Otherwise (not direct, or the turn is tainted) keep today's confirmation path exactly.
- Count direct writes in the turn. If at least one happened and the model's final answer is
  empty after tool-markup stripping, send nothing more (don't trigger the empty-answer retry or
  the "Sorry, I couldn't produce a response." fallback): the echo already answered.

### Which expense tools are direct

| Tool | direct | direct_after |
|---|---|---|
| `add_expense` | yes | `{"read_receipt"}` |
| `update_expense` (new) | yes | — |
| `delete_expense` | yes | — |
| `restore_expense` (new) | yes | — |
| `undo_expense_change` (new) | yes | — |
| `update_trip_members` (new) | yes | — |
| `settle_up`, `start_trip`, `end_trip`, `update_trip`, `activate_trip`, `reopen_trip`, `set_default_trip`, `set_fx_rate` | **no** (keep the button) | — |

Receipt decision (document it in a comment next to `add_expense`'s registration): the
`read_receipt` output is still returned with `untrusted=True`, so the agent wraps it as data for
the model and `read_untrusted` is set (memory writes still need a click). It is exempt only for
`add_expense`, because its fields are a constrained JSON schema from the vision reader, any add
is echoed and is one "undo" away, and the owner wants receipt adds applied directly. A receipt
read does **not** exempt `update_expense`, `delete_expense`, `restore_expense`,
`undo_expense_change` or `update_trip_members`: a crafted receipt must not be able to steer
those without a click.

### Audit log and undo (`plugin-expenses`)

Migration `0003_trip_members_and_expense_events.sql` (additive only, so the previous release
still runs against it):

```sql
CREATE TABLE trip_members (
    trip_id uuid NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    user_key text NOT NULL,
    added_by text NOT NULL,
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trip_id, user_key)
);
CREATE TABLE expense_events (
    id bigserial PRIMARY KEY,
    chat_key text NOT NULL,
    expense_id uuid NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
    actor_key text NOT NULL,
    action text NOT NULL CHECK (action IN ('add', 'update', 'delete', 'restore')),
    before jsonb,
    after jsonb,
    undo_of bigint REFERENCES expense_events(id),
    undone_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX expense_events_actor ON expense_events(chat_key, actor_key, id DESC);
CREATE INDEX expense_events_expense ON expense_events(expense_id, id DESC);
```

- Snapshot format for `before`/`after` (JSON-serialisable): `description, amount, currency,
  paid_by, spent_on (ISO), trip_id (str|None), fx_rate_to_settle (str|None), shares
  {user_key: minor}`.
- Every expense commit writes one event in the **same transaction** as the change, with
  `actor_key = actor.key`: `add` (after only), `update` (before + after), `delete` (before),
  `restore` (after). Existing `add_expense`/`delete_expense` commits gain this.
- **Undo** (`undo_expense_change`, no arguments; scope = the requester in this chat): pick the
  requester's latest event in this chat with `undone_at IS NULL AND undo_of IS NULL`. Refuse
  (`ValueError`, user-facing) if a later event exists on that expense (someone changed it
  since: say who by display name is not needed; say "it was changed again after that; undo
  that change first or edit it directly"), or if its trip is closed. Apply the inverse:
  `add`→soft delete, `delete`→restore, `update`→write `before` back (expense fields and shares),
  `restore`→soft delete. Record the inverse as a new event with `undo_of = <id>` and set the
  original's `undone_at`. Repeated undos walk back further. Nothing to undo → `ValueError("You
  have no expense change to undo in this chat.")`. Postgres: lock the event row and the expense
  row `FOR UPDATE`.
- **Restore** (`restore_expense(expense_id)`): the expense must be deleted (`deleted_at IS NOT
  NULL`) in this chat; creator or admin only (same rule as delete); trip not closed. Needs a
  lookup that includes deleted expenses (`get_expense(chat, id, include_deleted=True)`, same
  id/8-char-prefix rules).
- **Update** (`update_expense`): arguments `expense_id` (required) and optional
  `description, amount, currency, paid_by, split_with, split_type, shares, spent_on, trip,
  general`. Creator or admin only. Factor the add-expense validation (currency resolution,
  amount parsing, member resolution, split, date checks, FX lookup) into one helper used by
  both tools so they behave the same. Recompute shares when amount, currency, payer or split
  changes: participants = `split_with` if given, else the existing share participants; split
  type = given or `equal` (document that an exact/percentage split is re-split equally when only
  the amount changes; the echo shows the new shares). Re-fetch the FX rate when currency, date
  or trip changes in a convert trip. The payload carries `expense_id`, `expected` (the current
  snapshot) and the new snapshot; the commit refuses if the stored row no longer equals
  `expected` ("The expense changed since; ask again."). No-op update → tool error "Nothing to
  change.".
- Existing `delete_expense` keeps creator-or-admin and soft delete.
- Both `MemoryLedgerRepo` and `PostgresLedgerRepo` implement all of this; every query scoped by
  chat.
- Logs: kind, chat key, ids, counts. Never descriptions or names.

### Echo texts (the commit return values; used for both direct and confirmed commits)

One line each, amounts with `format_amount`, names by display name (resolve through
`stores.repo.find_members`, falling back to the key), trip label `(<trip name>)` or
`(general ledger)`:

- add: `Added: Dinner MYR 45.00, paid by Alice, split 3 ways (Example city trip). Say "undo" to revert.`
  (equal split → "split N ways"; otherwise "split: Alice MYR 20.00, Bob MYR 25.00"). If FX was not
  found, append the existing FX note.
- update: `Updated: Dinner (Example city trip): amount MYR 45.00 → MYR 50.00; split 3 ways → 4 ways. Say
  "undo" to revert.` (list only changed fields).
- delete: `Deleted: Dinner MYR 45.00 (Example city trip). Say "undo" to bring it back.`
- restore: `Restored: Dinner MYR 45.00 (Example city trip). Say "undo" to revert.`
- undo: `Undid your last change: <the inverse's echo without the undo hint>.`
- members: `Example city trip members: Casey, Alice, Bob, Carol (added Carol).`

### Ask instead of guessing

Tools return a `ToolResult.error` question (no write) when it is genuinely unclear: unknown or
ambiguous person (existing `_resolve_member`), several currencies with none given (existing
`resolve_currency`), several active trips and no default (existing `_current_trip`), or no
`split_with` and the trip has no members (see §2). Amount is required by the schema.

## 2. Trip members as the default split

- `trip_members` table above. Repo: `trip_members(chat, trip_id) -> list[dict]`
  (`user_key, added_by, added_at`), scoped by chat through a join to `trips`. Trip dicts from
  `trips()`/`trip()` carry `members: list[str]` (user keys, ordered by `added_at, user_key`).
- `start_trip` gains optional `members: list[str]` ("me", @username or display names, resolved
  with `_resolve_member`; deduplicated). `start_trip` stays confirmed; its summary lists the
  members; the commit inserts them with `added_by = actor.key` in the same transaction.
- New direct tool `update_trip_members(trip?: str, add?: list[str], remove?: list[str])`:
  trip resolution like the other management tools (`_management_trip`); trip must not be closed;
  at least one of add/remove; adding an existing member or removing a non-member is a no-op that
  the echo mentions. Commit kind `update_trip_members`, re-check the trip status under lock.
  Removing a member never changes existing expenses.
- `add_expense`: `split_with` becomes **optional** (remove it from `required`). With no
  `split_with` (absent or empty list) and a selected trip that has members: participants = all
  members, split by `split_type` (default equal). If the payer is not a member, return an error
  question ("Alice isn't on the Example city trip's member list. Add them with update_trip_members, or say
  who shares this.") instead of guessing. No members (or general ledger) and no `split_with` →
  error question "Who shares this? (Tip: set trip members with update_trip_members so I can
  split among everyone.)". An explicit `split_with` always overrides members. `shares` with no
  `split_with` uses the member list as participants.
- `list_trips` and `get_trip` show members by display name (`Members: Casey, Alice, Bob` or
  `Members: none set`).
- Update the tool descriptions: `add_expense` ("applies immediately and the bot posts what was
  recorded; omit split_with to split equally among trip members including the payer"),
  `start_trip` (members), plus the new tools. Remove "Every write requires confirmation" from
  direct tools' descriptions; keep "Requires confirmation" on the confirmed ones.

## 3. Receipt and image reuse within a turn

Incident (2026-10-31 15:09 UTC): vision attached the user's photo to the turn, then
`read_receipt` re-downloaded the same Telegram file, got `PlatformError`, and the reply was the
format/size hint although the bot had seen the slip.

- `TurnState` gains `downloads: dict[str, bytes]` (attachment id → raw bytes). Request-only;
  never persisted, logged or put in history.
- `agent.py`: create the dict before `_context` and pass it to `_images` → `download_images`,
  then into `TurnState(downloads=...)`, so the vision download is reused.
- `images.py`:
  - `class ImageUnavailable(Exception)` with `reason: Literal["download_failed", "too_large",
    "unsupported_format"]`.
  - `async def download_image(platform, attachment, *, chat, max_bytes=MAX_IMAGE_BYTES,
    cache: dict[str, bytes] | None = None) -> ImagePart` — cache hit skips the platform; on
    success stores the bytes; size checks before and after; sniff type; raises
    `ImageUnavailable`.
  - `download_images(..., cache=None)` keeps its behaviour (skip failures) on top of it.
  - `def error_label(exc: BaseException) -> str`: `type(exc).__name__`, plus
    `(<arg>)` when `exc` is a `PlatformError` whose first arg matches
    `^[A-Za-z][A-Za-z0-9_]{0,63}$` (e.g. `PlatformError(TimedOut)`). Use it in the download
    warning log (`attachment download failed chat=%s error=%s`). Never log anything else from the
    exception.
- `read_receipt` uses `download_image(..., cache=ctx.turn.downloads)` and maps reasons to
  accurate errors: `download_failed` → "I couldn't download that image from Telegram just now.
  Please try again."; `too_large` → "That image is over 5 MB. Please send a smaller one.";
  `unsupported_format` → "I can only read JPEG, PNG or WebP images." Do the download **before**
  incrementing the per-user receipt counter, so a failed download doesn't burn a read.
- `read_attachment` (plugin-search): image path uses `download_image` with the turn cache
  (`attachment unavailable: download failed, please try again` / `... exceeds the size limit` /
  `... not JPEG/PNG/WebP`); the document path uses the same cache for its raw download
  (look up by id first; store after a successful download) and on a platform download error
  returns `attachment unavailable: download failed, please try again` (parse errors keep their
  current message).
- Retry once in the adapters' `download`:
  - Direct (`chatcore_telegram/__init__.py`): wrap the `get_file` + `download_as_bytearray`
    sequence; on `telegram.error.TimedOut` or `NetworkError` retry once after 0.5 s; on
    `RetryAfter` retry once after `retry_after` seconds only if it is ≤ 5 s (otherwise fail).
    Other errors fail at once. Final failure raises `PlatformError(<telegram error class
    name>)`. Make the sleep injectable or patchable for tests.
  - Bridge (`bridge.py`): in `download` only, call the client directly (not `_request`) so the
    `BridgeAdapterError` code is kept; retry once after 0.5 s when the code is `upstream_error` or
    `bridge_error`; raise `PlatformError(<code>)` when the code matches
    `^[a-z_]{1,40}$`, else `PlatformError("BridgeAdapterError")`. Don't change any other bridge
    operation.

## 4. Replies stay in their forum topic

Production: after a Confirm press, "Updated trip Example city trip…" and "Saved …" landed in the group's
General topic instead of the originating thread, because `BotApp._on_press` calls `self.send(press.chat,
reply, reply_to=press.message)` without `thread_id`.

- `_on_press`: read the action first (`stores.repo.get_pending(press.chat, press.action_id)`;
  it is already read in the restricted branch, reuse it) and use
  `thread_id = action.thread_id if action and action.thread_id else (press.message.thread_id if
  press.message else None)` for both the command context and `self.send(...)`, and pass it to the
  restricted notice too.
- Reply-code path (`Route.REPLY_CODE`): reply in the thread the code was typed in
  (`message.thread_id`), which it already does; add a test that locks it.
- Direct-write echoes use `message.thread_id` (see §1).
- Check the Postgres store round-trips `pending_actions.thread_id` in `get_pending` and
  `claim_pending`; fix in `chatcore/store/pg.py` only if it does not (you may edit it for that).

## 5. Prompt (`system.md`)

Rewrite the expenses paragraph: expense adds, edits, deletes, restores, undo and trip member
changes apply immediately and the bot posts what was recorded; don't repeat that echo, and don't
ask "shall I save it?". When someone says "undo" about an expense, call `undo_expense_change`.
Ask only when genuinely unclear (unknown person, missing amount, which currency, which trip).
With trip members set, omit `split_with` to split equally among everyone on the trip. Settling
up and trip changes (start, end, reopen, activate, default, FX override, settings) still need a
confirmation click. Receipt paragraph: still call `read_receipt` first; then call `add_expense`
directly with the total; if nobody said who paid, the person who sent the receipt paid; split
among trip members unless told otherwise; mention warnings.

## Tests

Unit (fakes), grouped:

- **Registry** (`packages/core/tests`): `Tool(direct=True)` without `writes` raises;
  `direct_after` without `direct` raises; a direct tool returning no pending action still raises
  `RuntimeError`; `direct_write_allowed` truth table (clean turn; tainted by web; tainted only by a
  `direct_after` tool; `read_untrusted` set with empty `untrusted_tools` → False; non-direct tool
  → False).
- **Agent** (scripted fake provider, `chatcore.testing`): direct write commits without a pending
  row and posts the echo in the message's `thread_id`; the turn continues to a second tool call;
  after an untrusted tool result (e.g. a fake web tool) the same write goes through
  `confirm.request` and nothing is written; after `read_receipt`-like untrusted output listed in
  `direct_after` the add commits directly but a `delete` direct tool is confirmed; empty final
  answer after a direct write sends no fallback text; handler exception → `FAILED` echo, error
  class logged only.
- **Confirm/app**: `_on_press` result is sent with the action's `thread_id` (and falls back to
  `press.message.thread_id`); reply-code result is sent in the message thread; restricted notice
  on press keeps the thread.
- **Images**: second `download_image` for the same attachment id with a shared cache does not
  call `platform.download` again; vision download in the agent + `read_receipt` in the same turn
  → exactly one `platform.download` call; `read_attachment` reuses it too; error reasons map to
  the three messages; failed download does not increment the receipt counter; `error_label`
  formats `PlatformError("TimedOut")` and refuses unsafe args.
- **Adapters**: direct `download` retries once on `TimedOut`/`NetworkError` then succeeds; fails
  after the second error with `PlatformError("TimedOut")`; `RetryAfter(2)` retries,
  `RetryAfter(30)` doesn't; `BadRequest` doesn't retry. Bridge: `upstream_error` retried once;
  `forbidden` not retried; raised `PlatformError` carries the code.
- **Expenses**: add applies with echo text; update (amount, payer, split) with before/after
  event; delete then restore; undo of add/update/delete/restore; repeated undo walks back; undo
  refused when someone changed the expense since; undo only sees the requester's own events and
  only this chat (chat A/B isolation); restore of a non-deleted expense is an error; non-creator
  non-admin can't update/restore; closed trip refuses; members: start_trip with members,
  `update_trip_members` add/remove/no-op, add_expense without split_with splits among members
  including the payer, explicit split_with overrides, payer not a member → question, no members →
  question, `list_trips`/`get_trip` show members; members of chat A's trip invisible to chat B.
  Run every repository test against both Memory and Postgres repos (Postgres as integration,
  skipping without `TEST_PG_DSN`), including the migration applying on top of 0001+0002.
- **Tool audit** (`tests/test_tool_audit.py`): add the new tools to `WRITES`; assert every
  direct tool still returns a `pending_action` from `registry.call` (so the core decides);
  assert exactly the expected set of tools is `direct` (the table in §1), so a new direct tool
  can't appear silently; assert confirmed expense tools are not direct.
- **Console e2e** (`apps/sb_bot/tests/test_expense_ux_e2e.py`, integration with real Postgres,
  follow `test_multi_trip_e2e.py`): start a Example city trip with members "Alice, Bob and me" (confirm by
  button); "I paid 30 for lunch" → `add_expense` without split_with → echo "Added: lunch MYR
  30.00, paid by …, split 3 ways (Example city trip)…" posted with no confirm prompt, shares 10/10/10;
  "undo" → expense soft-deleted, echo; receipt add: a message with a photo attachment, fake
  receipt reader, `read_receipt` then `add_expense` → applied directly, one platform download for
  the turn; a turn that calls a fake untrusted tool before `add_expense` → confirmation prompt
  instead. Echoes and the confirm result land in the message's thread when it has one.

## Done when

- `uv run ruff format --check . && uv run ruff check .` clean; `uv run pytest -q` passes in your
  sandbox (integration tests skip).
- Every item in Tests exists and asserts real behaviour (stored rows, sent messages with
  `thread_id`, call counts), not just return codes.
- Not in scope: production backfill of members (the reviewer writes it), ARCHITECTURE.md edits, undo for
  settlements or trip changes, item-level receipt splits.
