# Generic polls (`packages/plugin-polls`, `chatcore_polls`)

Read `ARCHITECTURE.md` §3, §3d, §6a and §7c, `docs/reusable-jobs.md`, and these files, which define the
contracts and patterns you build against:

- `packages/core/src/chatcore/events.py`, `platform.py`, `plugin.py`, `app.py`, `access.py`,
  `group_access.py`, `pending.py`, `confirm.py`, `tools/registry.py`, `testing.py`, `privacy.py`
- `packages/plugin-jobs/src/chatcore_jobs/` — `plugin.py`, `service.py`, `permissions.py`,
  `timing.py`, `settings.py`, `repository.py`, `worker.py`, `migrations/0001_jobs.sql`, and its
  tests (especially `tests/test_pg_jobs.py` for the integration fixture)
- `packages/adapter-telegram/src/chatcore_telegram/__init__.py` and its tests
- `apps/sb_bot/src/sb_bot/app.py`, `apps/sb_bot/tests/test_app.py`, `tests/test_tool_audit.py`

## Goal

Generic group polls ("where should we go for dinner?"): any question, 2–N options, single or
multiple choice, anonymous or named, optional scheduled close. Meetup dates are a preset only.
Native platform polls (Telegram `sendPoll`/`stopPoll`), not a custom button grid.

## Verified platform facts (Telegram Bot API 10.x, checked 2026-10-31; PTB 22.8 is installed)

- `sendPoll`: question 1–300 chars, **1–12 options**, option text 1–100 chars, `is_anonymous`
  defaults to **True**, `allows_multiple_answers` defaults False, `allows_revoting` defaults True
  for regular polls, `message_thread_id` supported. `open_period`/`close_date` accept up to
  2,628,000 s — but **do not use them** (see §5: we close ourselves so we get the final totals).
- `stopPoll(chat_id, message_id)` returns the final `Poll` with `options[i].voter_count` and
  `total_voter_count`. Only works for polls the bot sent.
- `poll_answer` updates arrive **only for non-anonymous polls sent by this bot**; they must be
  listed in `allowed_updates`. `PollAnswer` has `poll_id`, `user` (None when voting as a chat),
  `option_ids` (0-based; **empty means the vote was retracted**).
- `telegram.constants.PollLimit` has these numbers; use it in the adapter instead of literals.

## You own

- `packages/plugin-polls/**` (new package: `pyproject.toml`, `src/chatcore_polls/**`,
  `src/chatcore_polls/migrations/0001_polls.sql`, `tests/**`)
- `packages/core/src/chatcore/polls.py` (new) and its test `packages/core/tests/test_polls_core.py`
- Additive edits only to: `packages/core/src/chatcore/events.py` (new `PollAnswer` event, added to
  the `Event` union), `packages/core/src/chatcore/app.py` (dispatch `PollAnswer`),
  `packages/adapter-telegram/src/chatcore_telegram/__init__.py` and its tests,
  `apps/sb_bot/src/sb_bot/app.py`, `apps/sb_bot/pyproject.toml`, root `pyproject.toml`
  (`[tool.uv.sources]` entry only), `uv.lock` (via `uv sync`), `apps/sb_bot/tests/test_app.py`,
  `tests/test_tool_audit.py` (only if the audit needs the new tool listed)
- `docs/polls.md` (new short user/operator guide)

You may **not** change the `ChatPlatform` protocol, `Capabilities`, `Plugin`/`BasePlugin`,
`CommandContext`, `ToolContext`, `PendingAction`, `ConfirmService`, `AccessPolicy`, or anything in
`packages/plugin-jobs`. Existing events and their fields stay as they are. If one must change,
stop and say so in your report.

## 1. Core: `chatcore/polls.py` and the `PollAnswer` event

Platform-neutral, optional capability, same pattern as `chatcore/group_access.py`.

In `events.py` add:

```python
@dataclass(frozen=True, slots=True)
class PollAnswer:
    """A vote change on a native poll this bot sent. ``poll_id`` is the platform's poll id,
    namespaced only by ``user.ref.platform``. Empty ``option_ids`` means the vote was retracted."""
    poll_id: str
    user: User
    option_ids: tuple[int, ...]
```

and append it to `Event`.

In `chatcore/polls.py`:

```python
@dataclass(frozen=True, slots=True)
class PollLimits:
    max_options: int
    max_question_len: int
    max_option_len: int

@dataclass(frozen=True, slots=True)
class SentPoll:
    message: MessageRef      # thread_id preserved
    poll_id: str             # platform poll id, used to match PollAnswer events

@dataclass(frozen=True, slots=True)
class PollResult:
    counts: tuple[int, ...]  # per option, same order as sent
    total_voters: int

@runtime_checkable
class PollPlatform(Protocol):
    poll_limits: PollLimits
    async def send_poll(self, chat: ChatRef, question: str, options: Sequence[str], *,
                        multiple: bool, anonymous: bool,
                        thread_id: str | None = None) -> SentPoll: ...
    async def stop_poll(self, message: MessageRef) -> PollResult: ...

@runtime_checkable
class PollAnswerListener(Protocol):
    async def on_poll_answer(self, answer: PollAnswer, stores: Stores) -> None: ...
```

Both methods raise `PlatformError` on failure (class name only). `ChatPlatform` is unchanged; the
console adapter does not implement `PollPlatform`, and the polls plugin then replies
"Polls are not supported on this platform." (fail closed, no crash).

In `app.py` `BotApp.handle`, add a `case PollAnswer():` that calls `on_poll_answer` on every
plugin that `isinstance(plugin, PollAnswerListener)`, each in its own try/except logging only the
plugin name and the exception class name (same as `on_message`). Do not log poll ids or options.
The core does no access check here: the event carries no chat; the listener resolves the poll and
checks access (§4).

## 2. Telegram adapter

- `TelegramPlatform` implements `PollPlatform`: `poll_limits` from `telegram.constants.PollLimit`
  (`MAX_OPTION_NUMBER`, `MAX_QUESTION_LENGTH`, `MAX_OPTION_LENGTH`).
- `send_poll` → `bot.send_poll(chat_id, question, options, is_anonymous=anonymous,
  allows_multiple_answers=multiple, type="regular", message_thread_id=int(thread_id) if
  thread_id else None)`. Always pass `is_anonymous` explicitly (Telegram defaults to True). No
  `open_period`/`close_date`. Return `SentPoll(MessageRef(chat, str(msg.message_id), thread_id),
  msg.poll.id)`. Reuse the existing retry-once-on-`RetryAfter` behaviour (generalise
  `_send_with_retry` to take the bot method, without changing its behaviour for messages).
- `stop_poll(message)` → `bot.stop_poll(chat_id, int(message_id))` →
  `PollResult(tuple(o.voter_count for o in poll.options), poll.total_voter_count)`.
- `allowed_updates` gains `"poll_answer"`. In `_handle_update`, map `update.poll_answer` to
  `PollAnswer(poll_id, self._user(answer.user), tuple(answer.option_ids))`; ignore it when
  `answer.user` is None (vote cast as a chat).
- Update the adapter tests that assert `allowed_updates`, and add tests with fake bot objects for
  send/stop/answer mapping including `message_thread_id` and empty `option_ids`.

## 3. Package, settings, storage

`packages/plugin-polls/pyproject.toml`: name `chatcore-polls`, deps `["chatcore"]` only. Register
in root `[tool.uv.sources]` and as an `sb-bot` dependency; run `uv sync --all-packages`.

`PollsSettings(BaseSettings)`, `env_prefix="POLLS_"`, strict ints as in `JobsSettings`:

| Setting | Default | Bounds |
|---|---|---|
| `max_options` | 12 | 2–12 (effective max = min with `platform.poll_limits.max_options`) |
| `max_open_per_chat` | 20 | 1–200 |
| `max_open_per_user` | 5 | 1–50 |
| `close_horizon_days` | 30 | 1–90 |
| `min_close_lead_seconds` | 60 | 5–3600 |
| `scan_seconds` | 30 | 1–300 |
| `lease_seconds` | 60 | 10–600 |
| `max_close_failures` | 3 | 1–10 |
| `retention_days` | 30 | 1–3650 |

Migration `0001_polls.sql` (additive, package-owned, applied by `chatcore.store.migrations`):

- `poll_settings(chat_key text, is_dm boolean, polls_enabled boolean NOT NULL DEFAULT false,
  revision bigint NOT NULL DEFAULT 0, updated_at timestamptz, PRIMARY KEY(chat_key, is_dm))`.
  A missing row means **disabled** (default-off).
- `polls(id uuid PK, chat_key, is_dm, thread_id text NULL, number integer NOT NULL,
  creator_key text, creator_name text, question text, options jsonb (array of strings),
  multiple boolean, anonymous boolean, status text CHECK IN ('sending','open','closed','failed'),
  close_at timestamptz NULL, platform_message_id text NULL, platform_poll_id text NULL,
  final_counts jsonb NULL, total_voters integer NULL, close_failures integer DEFAULT 0,
  lease_token uuid NULL, lease_until timestamptz NULL, source_action_id text NULL,
  created_at, closed_at NULL)`, with `UNIQUE(chat_key, number)`,
  `UNIQUE(chat_key, source_action_id)`, a partial unique index on
  `(split_part(chat_key, ':', 1), platform_poll_id)` where not null, and indexes on
  `(status, close_at)` and `(chat_key, is_dm, thread_id, status)`.
  `number` is a small per-chat counter (`COALESCE(max(number),0)+1` inside the insert
  transaction, serialised with `pg_advisory_xact_lock(hashtext('polls:' || chat_key))`), so users
  type `/closepoll 3`, not a UUID.
- `poll_votes(poll_id uuid REFERENCES polls ON DELETE CASCADE, chat_key text, voter_key text,
  voter_name text, option_index integer, voted_at timestamptz,
  PRIMARY KEY(poll_id, voter_key, option_index))`, index on `(chat_key, voter_key)`.

`PollsRepo(db, settings)` requires a `PgDatabase` (raise `TypeError` otherwise, like `JobsRepo`).
Every query takes the scope (`chat_key`, `is_dm`, and `thread_id` where listing/looking up) as a
required argument and filters by it. Methods (names are suggestions; keep them small):
`settings(scope)`, `configure(scope, enabled, expected_revision)`,
`reserve(scope, creator, draft, source_action_id, now)` (quota checks + insert `sending`, returns
the row; on a duplicate `source_action_id` return the existing row so a repeated confirm is
idempotent), `mark_open(id, message_id, poll_id)`, `mark_failed(id)`, `list_open(scope)`,
`get_by_number(scope, number)`, `get_by_message(scope, message_id)`,
`find_open_by_platform_poll(platform, poll_id)`, `replace_votes(poll, voter, option_ids, now)`
(one transaction: delete that voter's rows, insert new ones; empty list = retraction),
`claim_close(id_or_none, now, limit)` (set `lease_token`/`lease_until` on `open` polls that are
due — or the one given id — using `FOR UPDATE SKIP LOCKED`, skipping unexpired leases),
`finish_close(id, lease_token, counts, total, now)` (only if status still `open` and the token
matches → `closed`), `record_close_failure(id, lease_token)`, `votes(poll_id)`,
`purge_user(chat_key, user_key)`, `purge_chat(chat_key)`, `purge_older_than(cutoff)`.

Quotas in `reserve`, inside the same transaction as the insert: open+sending polls in the chat
`< max_open_per_chat` and by that creator `< max_open_per_user`.

## 4. Service, permissions, commands, tool

Put the shared logic in `PollsService` so commands, the tool's confirm handler, the answer
listener and the close worker all use the same validation.

**Scope and access** (mirror `chatcore_jobs.permissions`, copy the small helpers you need; do not
import `chatcore_jobs`):
- Polls are **group-only**. In a DM every poll command and the tool reply
  "Polls work in group chats." and do nothing.
- Use requires `access.is_chat_allowed(chat)` and the chat's `polls_enabled` (default off).
  Disabled → "Polls are off in this group. A group admin can enable them with /polltools on."
- `/polltools on|off` (and only this) requires a **group manager**: `access.can_setup(user,
  chat)` after `access.refresh()` (owners and delegated setup admins) **or** the live native
  group admin check via `GroupAdminPlatform.is_group_admin`, failing closed on errors — exactly
  as `chatcore_jobs.permissions.is_group_manager`. It goes through the pending-action
  confirmation (`polls.configure`), re-checking manager status and the settings revision at
  commit, like `/jobtools`. Turning polls off does not close open polls; it blocks new ones.
- `/closepoll` is allowed for the poll's creator or a group manager.

**Validation** (`build_draft`): question stripped, 1..`max_question_len`; options stripped,
non-empty, each ≤ `max_option_len`, no case-insensitive duplicates, count 2..effective max
(say the max in the error). Close time optional; when given it needs both an ISO local datetime
(with seconds precision, as `/remind` requires) and an IANA zone, parsed by
`chatcore_polls/timing.py`, a **copy** of `chatcore_jobs/timing.py` with poll wording
("close time must be in the future"), the horizon set to `close_horizon_days`, plus the
`min_close_lead_seconds` lower bound. Keep the DST ambiguity/nonexistent-time rejection.

**Commands** (no LLM). `/poll` sends directly after validation — the user typed the exact
content — without a confirmation step:

```
/poll [--multi] [--anon] [--close <ISO local time> <IANA zone>] Question? | A | B | C
/poll [--single] [--anon] [--close ...] dates <YYYY-MM-DD>..<YYYY-MM-DD> [Question?]
/polls
/closepoll <number>        (or reply to the poll message with /closepoll)
/polltools <on|off>
```

- Flags come first, in any order, each at most once. The question may be wrapped in straight or
  curly double quotes; strip one surrounding pair. Options are separated by `|`.
- Defaults: single choice, **named** (non-anonymous, so results can list voters).
- `dates` preset: inclusive range of ISO dates; start ≤ end; labels like `Sat 7 Nov`
  (`f"{d:%a} {d.day} {d:%b}"`, add the year only if the range crosses a year); **multi-choice
  by default** (`--single` overrides, `--multi` is redundant but allowed); question defaults to
  "Which dates work?". Reject a range longer than the effective option max with a message naming
  the max and the range length. It is sugar only: it produces an ordinary draft.
- Unknown flags or malformed input → a one-line usage reply; nothing is sent.
- Flow: validate → `reserve` (status `sending`) → `platform.send_poll(..., thread_id=ctx.thread_id)`
  → `mark_open`. On `PlatformError`, `mark_failed` and reply "Couldn't send the poll; nothing
  was posted." Successful `/poll` replies nothing extra except, when a close time was set, a
  short "Poll #N closes at <label>." line. Log the bot's poll message into conversation history
  with `stores.repo.log_bot_message(chat, message_id, f"Poll #{n}: {question} — options: ...",
  thread_id=...)`, swallowing and logging (class name only) a failure.
- `/polls`: open polls in this chat **and topic**: `#N · question · single/multi · named/anon ·
  closes <label or "manually">`. None → "No open polls here."
- `/closepoll`: resolve by number in this chat+topic (or via `ctx.message.reply_to` message id);
  authorise; run the shared close routine (§5) immediately; it posts the results.

**LLM tool** `create_poll` (`writes=True`), neutral description "Propose a poll for this group;
the requester must confirm before it is posted". Schema, `additionalProperties: false`:
`question` (string), `options` (array of strings, `minItems: 2`), `multiple` (boolean),
`anonymous` (boolean), `close_local_time` (string), `close_timezone` (string); required
`question` and `options`; the two close fields must be both present or both absent. It never
sends: it validates with `build_draft`, then returns `ToolResult(content=summary,
pending_action=PendingAction.create(plugin="polls", kind="create", ...))`. The summary shows
**exactly** what will be posted:

```
Post a named, single-choice poll in tg:-100… topic 7:
Question: Where should we go for dinner?
1. Ramen
2. Tacos
Closes: 2026-11-05 18:00:00 Asia/Singapore (UTC+08:00; …)   ← or "Closes: when closed manually"
```

The payload stores the scope, requester, the validated draft (question, options, flags, close
instant as ISO UTC), and the settings revision. The `PollActions.commit` handler (registered as
`"create"` and `"configure"` in `action_handlers()`) re-checks: same chat/requester/thread/is_dm
as the action, chat still allowed, polls still enabled, revision unchanged, draft re-validated,
close time still in the future; then runs the same reserve→send→mark flow with
`source_action_id=action.id` so a double commit cannot post twice. Any mismatch → a plain
"Nothing was posted" message. Do not add tools for listing or closing; commands cover them.

**Answers** (`PollsPlugin.on_poll_answer`, the `PollAnswerListener`):
`find_open_by_platform_poll(answer.user.ref.platform, answer.poll_id)`; ignore unless found,
`status == 'open'`, not anonymous, and `access.is_chat_allowed(chat)`. Drop the answer if any
option index is out of range, or more than one index on a single-choice poll. Otherwise
`replace_votes` with the user's key and display name (fallback to `@username`). Answers for polls
this bot didn't send, closed polls, or unknown polls are ignored silently. Never reply.

## 5. Scheduled close: in-plugin scan (decision)

**Decision: a small close scan inside the polls plugin, not a new `plugin-jobs` job kind.**
Reasons, to repeat in `docs/polls.md`:
- `scheduled_jobs.kind` has a `CHECK (kind IN ('reminder','watch'))`, and `JobsWorker`,
  `JobsService.commit`, `_authorize_job`, job settings (`reminders_enabled`/`watches_enabled`),
  quotas and `/reminders`-style listings all branch on those two kinds. A poll-close kind would
  need a jobs migration, a jobs-side dependency on poll storage or a callback registry, and
  changes to deployed, reviewed scheduler code — coupling two plugins for one `UPDATE`.
- A poll already has its own durable row with `close_at`; the scan is one indexed query plus
  a lease, and the same close routine serves `/closepoll`, so there is exactly one close path.
- Telegram's native `close_date` (now up to ~30 days) is not used, because a natively closed
  poll can no longer be stopped by `stopPoll`, so we'd lose the authoritative final totals for
  anonymous polls and have no hook to post results.

`PollsCloser` (started in `on_startup` when `stores.db` is present and the platform is a
`PollPlatform`, stopped by `aclose()`): every `scan_seconds`, `access.refresh()` (on failure skip
this tick), then `claim_close(None, now, limit=5)` and close each claim. Follow `JobsWorker`'s
structure for the loop/task/cancellation, but keep it much smaller.

Close routine (shared with `/closepoll`, which calls `claim_close(poll_id, …)` first and replies
"That poll is already closing." if the claim fails):
1. `platform.stop_poll(MessageRef(chat, platform_message_id, thread_id))` → `PollResult`.
2. On `PlatformError`: `record_close_failure`; once failures reach `max_close_failures`, close
   anyway with `counts=None` and post results built from stored votes (named polls) or
   "Final results are unavailable." (anonymous). Otherwise leave it `open`; the lease expiry
   lets the next scan retry.
3. `finish_close(...)` (status/lease fenced) — only the winner of that update posts results, so
   results are posted at most once.
4. Post results via `platform.send(chat, text, thread_id=thread_id)` and log them with
   `log_bot_message`. A send failure is logged (class name only); do not retry.

Results text (`format_results`, a pure function, tested directly):

```
📊 Poll #3 closed: Where should we go for dinner?
Ramen — 4
Tacos — 4
Pho — 1
Tie: Ramen and Tacos (4 votes each).          ← or "Winner: Ramen (4 votes)." / "No votes."
Voters — Ramen: Alice, Bob, …; Tacos: …        ← named polls only, from stored votes
```

Counts come from `PollResult` (authoritative, both modes); voter names from stored votes. Multi
choice adds "(voters could pick several)" after the header. Keep it under the platform's message
length; truncate long voter lists with "+N more".

## 6. Wiring and privacy

`apps/sb_bot/src/sb_bot/app.py`: when `db` is set, build `PollsPlugin(settings_from_env(
PollsSettings, env), access, platform)`, `stack.push_async_callback(polls_plugin.aclose)`, and
append it to `plugins` before migrations are applied (so its migration runs) and before
`PrivacyPlugin` is created (so purges reach it). Without Postgres log
"polls plugin disabled: PG_DSN is not set". Console + Postgres must still start (the plugin
degrades: commands reply that polls aren't supported; no closer task).

Privacy hooks:
- `purge_user(chat, user)`: delete that user's votes in that chat's polls, and the polls they
  created in that chat (votes cascade).
- `purge_chat(chat)`: delete all polls, votes and `poll_settings` rows for the chat.
- `purge_older_than(cutoff)`: delete `closed`/`failed` polls with `closed_at` (or `created_at`
  for failed) before the cutoff.

## Security rules that apply

- Every repo query is scoped by the required chat (and topic for listing/lookup); write tests
  proving group A can't list, close, or vote into group B's poll, and topic 7 can't see topic 8.
- Answers are matched only to rows this bot created (by platform poll id) and only while `open`.
- Logs: poll UUIDs/numbers, chat keys, counts, error class names. Never question/option text,
  voter names, or exception args.
- The tool never posts; only a confirmed pending action does.
- No secrets, no `telegram` import outside the adapter (ruff enforces).

## Tests

Unit (fakes; extend `chatcore.testing.FakePlatform` locally in the polls tests with a
`FakePollPlatform` subclass — do not change `testing.py` unless you must, and say so):
- Core: `PollAnswer` dispatch reaches a listener plugin, an exception in one listener doesn't
  stop another, non-listener plugins are skipped; `FakePlatform` is not a `PollPlatform`.
- Parsing: plain poll, quoted/curly-quoted question, each flag, flag repeats/unknown flags,
  `--close` with bad zone / no seconds / past / beyond horizon / DST-nonexistent, 1 option,
  13 options, duplicate options (case-insensitive), empty option, over-long question/option.
- Dates preset: 10-day range → 10 labels, multi default, `--single`, reversed range, range over
  the max rejected with the message, cross-year labels, default question.
- Permissions: DM rejected; non-allowlisted group rejected; disabled by default; `/polltools`
  by a non-manager rejected; by owner, delegated `can_setup`, and live group admin accepted;
  admin lookup error fails closed; confirm re-checks manager status and revision.
- Tool: returns a pending action with the exact summary and never calls `send_poll`; commit
  posts once (second commit with the same action id doesn't post again); commit after polls were
  disabled / revision changed / close time passed posts nothing; close fields half-present
  rejected; extra keys rejected.
- Answers: named vote stored, re-vote replaces, empty retracts, out-of-range and multi-on-single
  dropped, anonymous/closed/unknown poll ignored, poll in a now-disallowed chat ignored.
- Close: scheduled close posts results once; `/closepoll` by creator and by manager works, by
  another member refused; concurrent close (claim already held) refused; stop failure retries
  then gives up after `max_close_failures` with the fallback text; results formatting — winner,
  tie (2- and 3-way), no votes, multi note, named voter lists, truncation.
- Console: polls commands on a non-`PollPlatform` reply "not supported".
- Telegram adapter: send/stop/answer mapping, thread id kept, `is_anonymous` always passed,
  `poll_answer` in `allowed_updates`, `user is None` ignored.
- App: `build_app` with Postgres includes the polls plugin (and its commands/tool); without
  Postgres it doesn't; `PrivacyPlugin` reaches it.

Integration (`packages/plugin-polls/tests/test_pg_polls.py`, `pytestmark =
pytest.mark.integration`, same throwaway-schema fixture and `TEST_PG_DSN` default as
`test_pg_jobs.py`): migration applies; per-chat numbering under concurrent `reserve`; quotas
under concurrency; `source_action_id` idempotency; scope isolation across chats and topics;
`find_open_by_platform_poll` ignores closed polls and other platforms; `replace_votes`
transactionality; `claim_close` with two concurrent claimers yields one winner and respects
lease expiry; `finish_close` fencing by token; all three purge hooks delete exactly the scoped
rows.

## Done when

- `uv run ruff format . && uv run ruff check .` is clean.
- `uv run pytest -m 'not integration' -q` passes (whole repo, not just the new package).
- The integration tests exist and skip cleanly without Postgres (the supervising session runs
  them against a disposable local DB).
- `docs/polls.md` explains commands, defaults, limits (12 options, named vs anonymous, why
  results for anonymous polls only come at close), enabling with `/polltools`, and the
  close-scan decision.
- Left for later — do **not** attempt: live Telegram calls, deployment, quizzes, adding
  options after creation, DM polls, editing polls, a `close_poll`/`list_polls` LLM tool.
