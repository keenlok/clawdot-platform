# Phase 2 run P: privacy, retention and the core hooks Phase 2 needs

Read `ARCHITECTURE.md` §3c ("Prompt assembly order"), §3d, §6a ("Data and privacy", "LLM-specific") and
§7c. Phase 1 is merged; read `chatcore/app.py`, `plugin.py`, `platform.py`, `store/base.py`,
`store/pg.py`, `store/memory.py`, `conversation.py`, `agent.py` and `testing.py` before starting.

This run makes **four additive Phase 0 contract changes, approved by the coordinator** (listed
below). Make exactly these and nothing else to the contracts. Everything new has a default or a
no-op, so existing plugins and adapters keep working unchanged.

## You own

- `packages/core/src/chatcore/`: `platform.py`, `plugin.py`, `app.py`, `agent.py`,
  `conversation.py`, `store/base.py`, `store/pg.py`, `store/memory.py`, `testing.py`, and new
  `privacy.py`
- `packages/adapter-telegram/` (only `pin`, plus its tests)
- `packages/plugin-search/src/chatcore_search/__init__.py` (only `purge_older_than`, plus a new
  test file)
- Tests: `packages/core/tests/test_p_*.py`, `packages/adapter-telegram/tests/test_p_*.py`,
  `packages/plugin-search/tests/test_p_*.py`

Don't touch `store/es.py`, `packages/plugin-memory`, `apps/`, or anything deploy-related:
parallel runs own them.

## 1. Contract additions (approved)

1. **Pin.** `Capabilities.pin: bool = False`. `ChatPlatform.pin(message: MessageRef) -> None`,
   documented "only called when `capabilities.pin` is true". `FakePlatform` gets `pin=True` in its
   capabilities and records pinned refs in a `pinned` list. The console adapter needs no change
   (default `False`).
   Telegram: `capabilities.pin = True`; `pin` calls `pin_chat_message(chat_id, message_id,
   disable_notification=True)` and raises `PlatformError` on failure (the bot needs the
   "pin messages" admin right; log a warning naming that, never the exception args).
2. **`Plugin.on_bot_added(chat, platform, stores)`** with a `BasePlugin` no-op. `BotApp.handle`
   calls it on every plugin for `BotAddedToChat` **only when the chat is allowlisted**
   (non-allowlisted chats are still left, as now). Each plugin call is isolated exactly like the
   `on_message` hooks: one failing is logged by class name and doesn't stop the others.
3. **`Plugin.purge_older_than(cutoff: datetime, stores)`** with a `BasePlugin` no-op, for
   retention.
4. **`CoreRepo`**: `purge_user(chat, user) -> int`, `purge_chat(chat) -> int`,
   `purge_older_than(cutoff) -> int` (rows deleted), in the Protocol, `PgCoreRepo` and
   `MemoryCoreRepo`:
   - `purge_user`: that user's `messages` in that chat, and their `pending_actions` there.
     `members` rows stay (the ledger shows names); say so in the docstring.
   - `purge_chat`: the chat's `messages`, `pending_actions`, `chat_settings`, `members`.
   - `purge_older_than(cutoff, *, now=None)`: `messages` with `sent_at < cutoff`, plus
     `pending_actions` that are **used or expired** (`used_at IS NOT NULL OR expires_at < now`,
     `now` from the app clock, never Postgres `now()`). It leaves `members` and `chat_settings`
     alone. It is the only purge without a `chat` argument (global by design); every other
     method takes `chat` as a required argument.
   - Tests prove chat A's data survives `purge_user`/`purge_chat` on chat B (memory and PG).

Update `chatcore/plugin.py` docstrings and the `Plugin` Protocol. Add a contract test (in
`test_p_contracts.py`) that `BasePlugin` satisfies the protocol and that `FakePlatform`,
`ConsolePlatform` and `TelegramPlatform` have `pin`/`capabilities.pin`.

## 2. Conversation purge hooks (`conversation.py`)

`ConversationPlugin` implements, for Postgres and the in-memory `ConversationStore`:
- `purge_user(chat, user, stores)`: delete `conversation_turns` in that chat whose `message_id`
  is one of that user's messages there (look the IDs up in `messages` **before** they're deleted:
  see the ordering rule in §4), delete the chat's `chat_summaries` (they may quote the user), and
  that user's `token_usage` rows in that chat.
- `purge_chat`: all `conversation_turns`, `chat_summaries`, `token_usage` for the chat.
- `purge_older_than`: turns with `created_at < cutoff`, summaries with `updated_at < cutoff`,
  `token_usage` with `created_at < cutoff`.

## 3. Agent turn hooks (`agent.py`)

A `TurnHook` runtime-checkable Protocol with:

```python
async def before_turn(self, message: IncomingMessage, text: str, stores: Stores) -> str | None
async def after_turn(self, message: IncomingMessage, text: str, stores: Stores) -> None
```

`AgentResponder` calls them on **every plugin in `app.plugins` that has them** (a plugin may
implement only one; check with `hasattr`/`callable`, not the Protocol's isinstance alone).
The memory plugin (built in parallel) implements both with exactly these signatures.
- `before_turn`: each non-empty result is added to the context, **wrapped with the per-turn
  untrusted tag**, right after the rolling summary and before the ambient messages (§3c order).
  Errors are logged by class name and skipped.
- `after_turn`: called after the reply is sent and turns are stored, only when no confirmation is
  pending, with `text` = the human's prompt text and nothing else. Errors are logged and swallowed.
  It must never run for bot messages.

Tests with `ScriptedProvider`: the hook text reaches the provider inside the untrusted wrapper
and in the right position; `after_turn` gets exactly the human text; a failing hook doesn't break
the turn.

## 4. `chatcore/privacy.py`: `PrivacyPlugin(BasePlugin)`, `name = "privacy"`

```python
DEFAULT_NOTICE = (
    "This bot records messages in this group to answer questions about past chat. Messages are "
    "kept 12 months. When someone asks the bot something, recent messages are sent to the AI "
    "provider ({provider}). /privacy for details, /forgetme to delete yours."
)

class PrivacyPlugin(BasePlugin):
    def __init__(self, plugins: Sequence[Plugin], *, provider_label: str,
                 member_groups: MemberGroups | None = None, notice: str = DEFAULT_NOTICE,
                 retention_days: int = 365, timezone: str = "Asia/Singapore",
                 run_at: time = time(3, 15)): ...
```

`plugins` are the **other** plugins (the app passes its list; ignore `self` if present).
`MemberGroups = Callable[[UserRef], Awaitable[list[ChatRef]]]` as in plugin-search.

**Purge ordering rule:** every purge calls each plugin's hook **first**, then the `CoreRepo`
method last, because plugins look up the user's message IDs in the core tables. Each plugin call
is wrapped: log `plugin name + error class`, carry on, and report a partial failure to the user.

**`/privacy`:** the notice (with the provider) plus: what's kept (ledger, memories until
`/forget`), the 12-month retention, what `/forgetme` and `/forget` do, and that DMs are never
readable from the group.

**`/forgetme`** (anyone allowed to talk to the bot):
- In a group: scope = this chat. In a DM: scope = this DM plus every group from
  `member_groups(user)` (list them in the summary).
- **Needs a confirmation**: build `PendingAction.create(plugin="privacy", kind="forgetme",
  payload={"chats": [...keys]}, summary=...)` and call `await ctx.confirm.request(action,
  ctx.user, ...)`; the command itself returns `None` (or a short note when there are no buttons is
  handled by `ConfirmService` already). The summary says exactly what goes: their messages,
  conversation turns that quote them, memories about them, and that **expense records stay** so
  balances still add up.
- The handler (`kind="forgetme"`) re-checks the requester is the same user, then purges
  each chat in the payload and replies with counts.

**`/purgechat`** (`admin_only=True`):
- No argument: this chat. With `<chat_key>` (e.g. `tg:-100123`, usable from a DM): that chat.
- Confirmation required (`kind="purgechat"`), summary states that **everything** for the chat
  goes, ledger included. The handler calls every plugin's `purge_chat` then
  `repo.purge_chat`.

**Join notice (`on_bot_added`):** send the notice to the chat; if `capabilities.pin`, pin it.
Pinning is **best effort**: a failure (e.g. no admin right) is logged by class name and the notice
stays. Post it once per add event, never on startup or restart; guard against a duplicated add
update with a cache key `joined:<chat_key>` (10-minute TTL). Being removed and re-added later may
post it again; that's fine.

**`/forgetme` reply** states what was kept and why: expense records (so balances still add up),
the member name (the ledger shows it), and the chat's shared settings.

**Nightly retention:**
- `async def run_retention(self, stores, *, now: datetime | None = None) -> dict[str, int]`:
  `cutoff = now - retention_days`; call every plugin's `purge_older_than(cutoff, stores)`, then
  `repo.purge_older_than(cutoff)`; log counts per step (no content); return them.
- `on_startup` starts one background task that sleeps until the next `run_at` in `timezone`, then
  runs `run_retention`, forever. Guard against several workers or restarts with a cache key
  `retention:<date>` (`cache.set` with a 2-day TTL, skip when present) inside
  `cache.lock("retention", ttl_s=3600)`. A failure is logged and retried the next night.
  Keep a reference to the task; the loop must survive one bad night.
- Inject the clock and the sleep function so tests don't wait.

Logging: chat keys, counts, plugin names, error classes. Never message text or user content.

## 5. `plugin-search`: `purge_older_than`

Delete `chunks` docs with `ts_end < cutoff` for every chat the plugin knows: `SELECT DISTINCT
chat_key FROM messages` via `stores.db` when present (retention calls plugins before the core
repo, so those rows still exist), plus the chats in its in-memory buffers. Use
`stores.search.delete("chunks", chat=..., query={"range": {"ts_end": {"lt": cutoff.isoformat()}}})`.
No-op when `stores.search` is `None`. Errors per chat are logged by class name.

## Tests

Unit (`memory_stores()`, `FakePlatform`, `ScriptedProvider`, a tiny recording fake plugin):
- `/privacy` text includes the provider; `/forgetme` and `/purgechat` create pending actions and
  write nothing until confirmed; only the requester can confirm (reuse `press()`).
- `/purgechat` is admin-only (non-admin: no reply, nothing pending).
- After a confirmed `/forgetme` in chat A: Alice's messages in A are gone; Bob's in A and Alice's
  in chat B remain; every plugin's `purge_user` was called before `repo.purge_user`.
- `/forgetme` in a DM covers the DM plus `member_groups` only.
- A failing plugin purge is logged, the others still run, the reply mentions partial failure.
- Join notice: allowlisted chat → sent and pinned; non-allowlisted → left, no notice; pin failure
  → notice still sent.
- `run_retention` with an injected `now`: old rows deleted, recent kept, ledger plugin untouched
  (use a fake plugin that records calls); the date key stops a second run the same day.
- Conversation purge hooks (memory and PG).
- `MemoryCoreRepo` purges.
- Agent hooks (section 3).
- `ExpensesPlugin` has no `purge_older_than` override (the ledger is kept, §6a); don't add one.
- Telegram `pin` with the adapter's existing fake request object.
- Search `purge_older_than` with an in-memory fake `SearchIndex`.
- No message text in logs during `/forgetme` (caplog).

Integration (marked; skip without services; `TEST_PG_DSN` default
`postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test`): `PgCoreRepo` purges and the
conversation purges on real tables, including cross-chat isolation.

Command: `uv run pytest packages -q`.

## Done when

- The four contract additions are in, with defaults, and every existing test still passes.
- Privacy commands, join notice, retention job, conversation and search purge hooks and agent
  hooks work as above, with the tests passing and `ruff check .` clean.
- The report lists the final signatures of everything new.
