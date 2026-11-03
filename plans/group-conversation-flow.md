# Smoother group conversations

Read first:
- `ARCHITECTURE.md` §3c, §3d, §6a, §16;
- `plans/group-mention-only.md` and `plans/context-reset.md` (the policies this builds on);
- `packages/core/src/chatcore/{routing,app,agent,conversation,limits,plugin,events}.py`,
  `packages/core/src/chatcore/store/{base,memory,pg}.py`,
  `packages/core/src/chatcore/migrations/0005_context_reset.sql`;
- `apps/sb_bot/src/sb_bot/app.py` (wiring, `Built.aclose`);
- `packages/adapter-console/src/chatcore_console/__init__.py` (for the end-to-end run);
- `packages/adapter-telegram/src/chatcore_telegram/__init__.py` (`send`, `split_message`);
- `packages/plugin-research/src/chatcore_research/plugin.py` (`wrap_responder`: it wraps the responder);
- existing tests: `packages/core/tests/{test_app,test_context_reset,test_a_runtime,test_a_pg_conversation,test_pg,test_restrictions}.py`, `apps/sb_bot/tests/test_app.py`.

Environment: `export UV_CACHE_DIR="$PWD/.uv-cache"` before running anything through `uv`.
Tests: `uv run pytest -q -m 'not integration'`. Lint: `uv run ruff format . && uv run ruff check .`.
`test_real_core_button_confirmation_preserves_topic` already fails on main; ignore it.

## Problem (from production logs, forum group, one topic)

1. A user replied to a bot message ("Pls address me as Master Jeremy"); it was ignored, because
   mention-only groups ignore replies. The user expected replies to work.
2. A user asked a question untagged, then sent a bare `@bot`; the bot answered "Sorry, I couldn't
   produce a response." because the request text was empty.
3. `@bot /models` (unknown command) got silence.
4. In the prompt, all ambient chat lines sit in one block *before* the bot's stored turns, so the
   model loses who said what between its own replies.
5. People have to tag the bot on every follow-up.

## Status

- **Phases 1 and 2 are built, merged and deployed** (main `1c20145`). Their text is kept in the
  appendix for reference. Don't rework them except where Phase 3 below says so.
- **Phase 3 (this run): conversations keyed on reply chains.** This final design (approved
  2026-10-31) replaces an earlier draft based on open/closed sessions and an untagged classifier.
  There is **no untagged classifier** in this phase: attaching a message to a conversation is
  deterministic.

## You own (Phase 3)

- `packages/core/src/chatcore/conversation.py`
- `packages/core/src/chatcore/agent.py`
- `packages/core/src/chatcore/app.py`
- new `packages/core/src/chatcore/threads.py` (settings, context variable, resolver)
- new `packages/core/src/chatcore/migrations/0006_conversation_threads.sql`
- `packages/adapter-telegram/src/chatcore_telegram/__init__.py`: only the `sent_message_ids`
  addition in 3b
- `packages/adapter-console/src/chatcore_console/__init__.py`: only `::reply [n]` (reply to the
  n-th most recent bot message, default 1 = the last), with a console test, for the end-to-end run
- `apps/sb_bot/src/sb_bot/app.py`: wiring only
- `docs/admin-setup.md`: extend the "Talking to the bot in groups" section
- tests: new `packages/core/tests/test_threads.py`, additions to `test_a_pg_conversation.py`
  (integration), `test_pg.py` (migration list), `packages/adapter-telegram/tests/test_telegram.py`,
  `apps/sb_bot/tests/test_app.py`, and existing tests that need updating.

Don't change `platform.py` (the `ChatPlatform` protocol), `events.py`, `plugin.py`,
`store/base.py`, `limits.py`, `routing.py`'s existing decisions, or the `Responder` protocol's
`respond(message, text, app)` signature (the research plugin wraps it). Every change to an
existing public signature is an optional keyword argument or a new method. If a contract must
change, stop and say why in your report.

Security rules (`ARCHITECTURE.md` §6a): untagged chatter must still cost **zero** LLM calls; everything
from the chat stays wrapped with `_wrap(..., tag)`; every new query takes the chat and filters by
it (a conversation id alone is never trusted: always also match `chat_key`); logs carry chat keys,
conversation ids, counts, booleans, token numbers and error class names only.

---

## Phase 3: conversations keyed on reply chains

### 3a. Option and settings (threads.py, app.py, sb_bot)

```python
@dataclass(frozen=True, slots=True)
class ThreadSettings:
    continue_window_s: int = 3600        # a fresh tag continues your conversation active within this
    reopen_compact_after_s: int = 3600   # reopening a conversation quiet this long compacts it first
    auto_compact_turns: int = 30         # unsummarised turns that trigger automatic compaction
    auto_compact_chars: int = 16000      # ... or unsummarised turn text above this many characters
    auto_keep_turns: int = 12            # turns kept verbatim by automatic compaction
    reopen_keep_turns: int = 4           # turns kept verbatim by reopen and manual compaction
```

`BotApp.__init__` gains `conversation_threads: ThreadSettings | None = None` (default off). When
set, `BotApp` finds the plugin named `"conversation"` (a `ConversationPlugin`), raises
`ValueError` if there is none, and calls `plugin.configure_threads(settings)` so `/compact` knows.
**With the option off, behaviour is exactly as today**: no rows in the new tables, no
`conversation_id` on turns, the topic-level summary and compaction as now. Existing tests must
pass unchanged with the option off.

Threads apply to **group chats only**. DMs keep today's topic-level behaviour even with the
option on (their turns get no conversation).

sb_bot: add `conversation_continue_minutes: int = 60` to `AppSettings` (env
`CONVERSATION_CONTINUE_MINUTES`) and pass
`ThreadSettings(continue_window_s=60 * minutes)` to `BotApp`.

`threads.py` also defines `current_conversation: ContextVar[Conversation | None]` (default
`None`). `BotApp` sets it around the responder call for a resolved conversation and resets it in
`finally`. The agent reads it. This avoids changing the `Responder` protocol.

### 3b. Storage (migration 0006, conversation.py)

```sql
CREATE TABLE conversations (
    id bigserial PRIMARY KEY,
    chat_key text NOT NULL,
    thread_key text NOT NULL DEFAULT '',
    title text NOT NULL DEFAULT '',
    participants text[] NOT NULL DEFAULT '{}',
    reset_generation bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_active_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_scope_idx
    ON conversations (chat_key, thread_key, last_active_at DESC);

CREATE TABLE conversation_messages (
    chat_key text NOT NULL,
    message_id text NOT NULL,
    conversation_id bigint NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    primary_message_id text NOT NULL,
    is_bot boolean NOT NULL,
    user_key text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, message_id)
);
CREATE INDEX conversation_messages_conv_idx ON conversation_messages (conversation_id);

CREATE TABLE conversation_summaries (
    conversation_id bigint PRIMARY KEY REFERENCES conversations (id) ON DELETE CASCADE,
    chat_key text NOT NULL,
    summary text NOT NULL,
    covers_until_turn_id bigint NOT NULL,
    reset_generation bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE conversation_turns
    ADD COLUMN conversation_id bigint REFERENCES conversations (id) ON DELETE CASCADE;
CREATE INDEX conversation_turns_conv_idx
    ON conversation_turns (conversation_id, id) WHERE conversation_id IS NOT NULL;
```

`title` stays empty in this phase (it's optional; don't store message text there).
`participants` holds `UserRef.key`s of people who addressed the bot in it (cap 20, drop oldest).
`reset_generation` is the topic's `ResetPoint.generation` (0 without a reset) when the
conversation was created. **A conversation is reachable only while its `reset_generation` equals
the topic's current generation**, so `/reset` (which bumps the generation) makes every
conversation in the topic unreachable without deleting it. `/compact`'s `advance_ambient` doesn't
change the generation.

New dataclass `Conversation(id, chat, thread_id, participants: frozenset[str], reset_generation,
created_at, last_active_at)`. New `ConversationStore` methods, memory and Postgres with the same
behaviour, every one taking `chat`:
- `create_conversation(chat, thread_id, user, *, at=None) -> Conversation` (stamps the current
  generation; participant = user).
- `get_conversation(chat, conversation_id) -> Conversation | None` (`WHERE id=$1 AND chat_key=$2`).
- `touch_conversation(chat, conversation_id, user, *, at=None)`: bump `last_active_at`, add the
  participant.
- `latest_conversation(chat, thread_id, user, *, since: datetime | None = None) -> Conversation | None`:
  the user's most recently active **reachable** conversation in this chat+topic (user in
  `participants`), optionally only if `last_active_at >= since`.
- `map_messages(chat, conversation_id, message_ids: Sequence[str], *, primary_message_id,
  is_bot, user: UserRef | None)`: upsert one row per id (all parts of a split answer share
  `primary_message_id`, the id that was logged in the message log).
- `conversation_for_message(chat, message_id) -> tuple[Conversation, str] | None`: the
  conversation and the `primary_message_id` for any mapped id, **reachable or not** (the caller
  checks reachability).
- `conversation_summary(chat, conversation_id) -> tuple[str, int] | None`, ignoring a summary
  whose `reset_generation` differs from the topic's current generation (same rule as `summary`).
- `save_conversation_summary(chat, conversation_id, summary, covers_until, *, generation)`.
- `recent_conversation_turns(chat, conversation_id, limit) -> list[Turn]` and
  `unsummarized_conversation_turns(chat, conversation_id) -> list[Turn]`: `id > max(reset
  after_turn_id, conversation summary covers_until)`, oldest first, filtered by chat **and**
  conversation id.
- `add(...)` gains keyword `conversation_id: int | None = None`, stored on the turn.
  `Turn` gains `conversation_id: int | None = None` (last field, default `None`).
- `compact_conversation(chat, conversation, provider, system, model, *, force=False, keep=12,
  min_turns=30, min_chars=16000) -> CompactResult`: like `compact` but over the conversation's
  unsummarised turns, merged with the conversation's prior summary, **no ambient lines**, same
  input caps (2000 per line, 24000 total, prior summary always kept), `tools=()`, saves a
  conversation summary with the generation read at the start. Without `force` it runs only when
  unsummarised turns `> min_turns` or their text exceeds `min_chars`.
- `reset(...)` additionally deletes `conversation_summaries` rows for that chat+topic's
  conversations (the generation check already hides them; deleting is tidy).
- Purges (same as turns):
  - `purge_chat`: delete the chat's conversations (cascades to mappings, summaries, and turns'
    conversation link; turns are deleted by the existing statement).
  - `purge_user(chat, user, message_ids)`: delete mappings where `chat_key=$1 AND (message_id =
    ANY(ids) OR user_key=$2)`; remove the user from `participants` of the chat's conversations;
    delete the chat's `conversation_summaries` (a summary can contain the user's words, as
    `chat_summaries` is already handled).
  - `purge_older_than(cutoff)`: delete conversations with `last_active_at < cutoff`, mappings
    with `created_at < cutoff`, conversation summaries with `updated_at < cutoff`.

Telegram multi-part answers: `TelegramPlatform.send` splits long text into several messages but
returns only the last ref. Add `TelegramPlatform.sent_message_ids(ref: MessageRef) ->
tuple[str, ...]`: all part ids of the send that returned `ref` (from a bounded dict of the last
1000 sends, keyed by `(chat.key, message_id)`), or `(ref.message_id,)` if unknown. In
`threads.py` define a `runtime_checkable` Protocol `MultipartSender` with that method. Don't add
it to `ChatPlatform`.

### 3c. Mapping outgoing bot messages (app.py)

`BotApp.send` (all bot replies go through it) keeps returning the ref and logging the message as
today. Additionally, when `current_conversation.get()` is set and the chat is a group:
`ids = platform.sent_message_ids(ref) if isinstance(platform, MultipartSender) else
(ref.message_id,)`, then `map_messages(chat, conv.id, ids, primary_message_id=ref.message_id,
is_bot=True, user=None)`. This covers the answer, limit notices and error replies sent during
the turn. Turns stay keyed by the human `message_id` as today (purge relies on it), plus
`conversation_id`.

When a conversation is resolved for an incoming message, also map that human message
(`is_bot=False`, `user=message.user.ref`, primary = its own id).

### 3d. Resolving the conversation (threads.py resolver, called from app.py)

Only when the option is on, the chat is a group, and the route is `RESPOND` (after the Phase 1
bare-tag handling has produced the request text). Untagged chatter never gets here. Run it
**inside** the per-chat turn lock, before calling the responder. In order:

1. **`/new` pending:** if the cache key `conversation-new:{chat}:{thread or ''}:{user}` is set,
   delete it and create a new conversation. The reply-target context from Phase 2 still applies.
2. **Reply target mapped:** if `message.reply_to` is set and `conversation_for_message(chat,
   reply_to.message_id)` returns a conversation in the same topic:
   - reachable (generation matches): **reopen that conversation, whatever its age**, for a reply
     to a bot message or for a tagged reply to a human message in it;
   - unreachable (from before the topic's `/reset`): create a new conversation. The replied-to
     message still reaches the model through the Phase 2 "Replying to your earlier message" /
     "Reply from" block.
3. **Reply to a bot message that isn't mapped** (older than this feature, or option was off):
   create a new conversation.
4. **Otherwise** (a fresh tag, a bare tag, a tagged reply to an unmapped human message):
   `latest_conversation(chat, thread, user, since=now - continue_window_s)` → continue it, else
   create one. No subject-similarity check.

Then `touch_conversation` (participant + `last_active_at`), map the human message (3c), and, when
an existing conversation was reopened whose previous `last_active_at` is older than
`reopen_compact_after_s`, run **reopen compaction** before responding (3f).

Reply targets that are later parts of a split answer: resolve them through
`conversation_for_message`'s `primary_message_id` wherever the code fetches the replied-to bot
message (the Phase 1 bare-tag-reply check in `app.py` and the Phase 2 reply block in `agent.py`),
so a reply to part 1 of a 3-part answer behaves like a reply to the logged message.

### 3e. `/new` (app.py)

When the option is on, register a built-in `Command("new", "Start a new conversation with me")`
(like `/help`). Reachable as `/new@bot` or `@bot /new` (bare `/new` stays ignored in mention-only
groups; routing is unchanged). In a group:
- no args: set `conversation-new:{chat}:{thread or ''}:{user}` (TTL 3600 s) and reply
  `"Okay, your next message to me starts a new conversation."`;
- with args: answer the args in a new conversation, exactly like a `RESPOND` with that text
  (same restriction, limits and lock handling; bypass the pending key; the args are the request).
In a DM: `"Conversations only apply in groups; just keep chatting here."`.
Restriction enforcement for commands applies as it does today.

### 3f. Context and compaction (agent.py, conversation.py)

When `current_conversation.get()` is set:
- **Summary block:** the conversation's own summary (`conversation_summary`) instead of the topic
  `summary`. The topic-level summary isn't used.
- **Turn history:** `recent_conversation_turns(chat, conv.id, recent_turns)` instead of
  `store.recent(chat, thread)`. Keep the Phase 2 topic transcript (log rows of the topic, caps,
  dedup, untrusted wrapping) built from those turns. The reply-target block stays.
- **Turn storage:** every `store.add` in `respond` passes `conversation_id=conv.id`.
- **Automatic compaction after the turn:** replace the topic `store.compact(...)` call with
  `compact_conversation(..., min_turns=auto_compact_turns, min_chars=auto_compact_chars,
  keep=auto_keep_turns)`; the agent reads the settings from the `ThreadSettings` BotApp holds
  (`app.conversation_threads`). Usage is added to the turn's usage as today.

Without a current conversation (option off, DMs) the agent behaves exactly as today.

**Reopen compaction** (3d): `compact_conversation(..., force=True, keep=reopen_keep_turns)` when
the conversation has more than `reopen_keep_turns` unsummarised turns. It must:
- run inside the turn lock that is already held (don't take `turn:{chat}` again: the cache lock
  isn't re-entrant);
- skip when the daily token budget is reached (`limits.budget_used() >=
  limits.settings.DAILY_TOKEN_BUDGET`; don't call `limits.check`, which would spend the user's
  request count);
- respect a per-conversation cooldown: skip if `compact-conv:{chat}:{conv_id}` is set, else set
  it (300 s) before the provider call;
- record usage (`limits.record`, `store.record_usage` with the requesting user);
- use the same summarising system prompt as manual `/compact` and `tools=()`;
- on failure, log `chat`, conversation id and the error class only, and continue with the reply.
The provider and limits come from the `ConversationPlugin` (`provider`, `limits`), which sb_bot
already passes. No provider → skip.

**Manual `/compact`** with threads configured (`configure_threads` was called) in a group:
compacts the **current conversation**: the conversation of the replied-to message if the command
is a reply to a mapped message in a reachable conversation, else `latest_conversation(chat,
thread, user)` (any age). Same permission (owner and that group's moderators), same denial
cooldown, same `compact:{chat}:{thread or ''}` cooldown, same `limits.check`, same turn lock,
`force=True, keep=4`, no ambient and **no** `advance_ambient`. No conversation, or nothing beyond
4 turns: `"Nothing to compact yet."` with no provider call. Reply: `"Compacted {n} turns of this
conversation into a summary (~{a} → ~{b} tokens). Kept the last 4 turns."`. Without threads,
`/compact` is unchanged.

**`/reset`**: unchanged command; through the generation bump every conversation in the topic
becomes unreachable as turns and its summary is invalidated (plus the tidy delete in 3b).

### 3g. Docs

Extend "Talking to the bot in groups" in `docs/admin-setup.md`: replying to any of the bot's
messages continues that conversation, however old (unless the topic was reset since); tagging
again within an hour continues your current conversation; `/new@bot` starts a fresh one; each
conversation keeps its own summary; `/compact@bot` compacts the current conversation; `/reset@bot`
clears all of them for the topic.

---

## Tests (Phase 3)

Use the existing fakes (`chatcore.testing`, memory stores, `ScriptedProvider` recording calls,
`FakePlatform`). Assert on provider inputs, call counts and sent messages. Use `at=` arguments or
direct row edits on the memory store for time, not sleeps.

`test_threads.py` (unit):
1. **Reopen by reply:** conversation A (Alice asks, bot answers), then conversation B in the same
   topic (Bob asks fresh, bot answers), then 3 hours later Carol replies to A's bot answer: the
   provider context has A's summary/turns and **none of B's** (set `recent_context_messages`
   small so log rows don't leak B; assert on B's turn text and B's summary text). Carol is added
   to A's participants and the new turns carry A's id.
2. **Parallel conversations:** Alice and Bob alternate tagged messages in one topic; each
   continues their own conversation (separate ids, separate turn histories).
3. **Continue window:** Alice tags again after 59 minutes → same conversation; after 61 minutes
   → a new one. Setting `continue_window_s` changes this.
4. **`/new`:** `/new@bot` then a tag → new conversation; `@bot /new what now?` → answered in a
   new conversation with request "what now?"; bare `/new` ignored; DM text; `new` not registered
   with the option off.
5. **Reply across `/reset`:** reply to a pre-reset bot message → a **new** conversation; no
   pre-reset turns or summary in the context; the "Replying to your earlier message" block still
   carries the bot text.
6. **Multi-part mapping:** a `FakePlatform` subclass implementing `sent_message_ids` returns three
   ids for one long answer; replying to the **first** part reopens the conversation and the reply
   block shows the logged answer. Telegram adapter test: a send split into 3 chunks →
   `sent_message_ids(returned_ref)` gives all 3 ids in order; an unknown ref gives itself.
7. **Auto-compaction on size:** more than `auto_compact_turns` turns (or text above
   `auto_compact_chars`) in a conversation → one summarising call with `tools=()`; the summary is
   stored for that conversation only (another conversation's summary is untouched).
8. **Reopen compaction:** reopening by reply after > 60 minutes with more than 4 unsummarised
   turns → a summarising call before the answer call, inside one turn-lock acquisition (no
   deadlock); cooldown set → skipped; budget reached → skipped; provider error → logged by class
   only (`caplog`, no message text), and the answer is still sent.
9. **`/compact`** (owner or moderator) compacts the current conversation only; a denied user gets
   the denial; nothing to compact → no provider call. **`/reset`** makes the next tag start a new
   conversation and hides old summaries and turns.
10. **Purges:** `purge_user` removes the user's mappings and participant entry and the chat's
    conversation summaries; `purge_chat` removes the chat's conversations, mappings and summaries;
    `purge_older_than` removes old conversations and mappings, keeping recent ones. **Isolation:**
    chat B can't reach chat A's conversation, even by replying to a message id that exists in A
    (`conversation_for_message` with chat B returns `None`), and `get_conversation(chat_b, a_id)`
    is `None`.
11. **Option off = today:** with `conversation_threads=None`, run a representative flow (tag,
    reply, compaction) and assert no conversation rows, turns without `conversation_id`, the topic
    summary used. The whole existing suite passes unchanged.
12. **Zero LLM calls:** untagged chatter, an untagged reply to a human message, and a bare `/cmd`
    with the option on → provider never called and no conversation created.
13. DMs with the option on behave as today (no conversation rows).

Integration (`@pytest.mark.integration`, skip without Postgres), in `test_a_pg_conversation.py`:
migration 0006 applies (update the `test_pg.py` list); the Postgres conversation methods match
the memory implementation (create, latest with window, map and resolve including split ids,
reachability after reset, summaries, turns filtered by conversation and chat, all three purges,
chat isolation); a new store instance sees the same data.

sb_bot (`apps/sb_bot/tests/test_app.py`): `build_app` passes `ThreadSettings` with the env
minutes (default 60); the Telegram group menu includes `new`.

## Done when

- `uv run pytest -q -m 'not integration'` passes (except the known pre-existing failure) and
  `uv run ruff format . && uv run ruff check .` is clean (format only Python files you changed if
  the repo-wide formatter touches unrelated Markdown).
- Integration tests are written and skip cleanly without Postgres.
- The reviewer can run the console adapter and see: two people's parallel conversations, a reply
  to an old bot message reopening its conversation, `/new@bot`, `/compact@bot`, `/reset@bot`.
- Not in scope: an untagged follow-up classifier, conversation titles, changing routing
  decisions, deployment, ARCHITECTURE.md (the reviewer updates it).

---

## Appendix: Phases 1 and 2 (built and deployed; reference only)

### Phase 1: routing (no new table)

#### 1a. Reply to the bot responds (routing.py)

In the `group_mentions_only` group branch of `decide()`:
- A message with `reply_to_bot=True` routes to `Route.RESPOND`, like a mention. This reverses
  that part of `plans/group-mention-only.md` at the user's request.
- A bare `/cmd` (no `@bot` target, no leading mention) stays `IGNORE`, **also when it is a reply
  to the bot**. Today `addressed_command` returns a bare `/cmd`, so a reply-to-bot `/cmd` would
  fall through to `COMMAND`; make sure it doesn't.
- Plain untagged chatter that is not a reply to the bot stays `IGNORE`.
- DMs and non-mention-only groups are unchanged.

#### 1b. Bare tag (app.py, agent.py)

When the route is `RESPOND`, the message is not a DM, `prompt_text(...)` is empty after stripping,
and the message has no attachments:
- Look up the same person's most recent earlier message in the same chat **and the same
  `thread_id`** (compare `row.thread_id == message.thread_id` explicitly: `recent_messages` with
  `thread_id=None` returns every thread) from `repo.recent_messages(chat, limit=20,
  thread_id=message.thread_id)`. It qualifies if: same `user`, not `is_bot`, not the current
  message, `sent_at >= message.sent_at - 300 s`, non-empty text, not starting with `/`, and not
  itself mentioning the bot (`@<bot_name>` case-insensitive). Make the 300 s a module constant
  `BARE_TAG_LOOKBACK_S`.
- If found, call `responder.respond(message, <that text>, app)`. The reply still replies to the
  bare-tag message.
- If none, send `"What can I help with? Reply to this message or tag me with your question."`
  (reply to the message, same thread) with **no** LLM call and no turn stored.
- Defence in depth in `AgentResponder.respond`: if `text.strip()` is empty and the message has no
  image attachments, send the same prompt text and return before calling the provider.
- In DMs an empty message stays as today.

#### 1c. Unknown command (app.py)

`_run_command` currently returns silently for unknown commands and for admin-only commands a
non-admin runs. Change it so that, when the command was **explicitly addressed** (DM, `/cmd@bot`,
or `@bot /cmd`; i.e. it got this far through routing) and is unknown *or* forbidden, the bot
replies `"I don't know /<name>. See /help@<bot> for what I can do."` (`/help` without the suffix
in DMs). Use the same text for unknown and forbidden so admin-only command names aren't revealed.
- `<name>` is the parsed name (already `[a-z0-9_]{1,32}`), safe to echo.
- At most one such reply per (chat, user) per 60 s: cache key `unknown-cmd:{chat}:{user}` set
  under a lock, the same pattern as `_restricted_notice`. Silent inside the cooldown.
- Restriction enforcement still runs first (a restricted user gets the restriction notice path as
  today, not this reply).
- Bare `/models` in a mention-only group is still ignored (routing never gets here).


### Phase 2: one time-ordered transcript (agent.py)

Replace, in `AgentResponder._context`, the separate ambient block plus the verbatim replay of
`store.recent(...)` turns with **one** attributed, time-ordered transcript per chat+topic.

Sources:
1. Message-log rows from `repo.recent_messages(chat, limit=settings.recent_context_messages,
   thread_id=message.thread_id)`, human and bot, keeping only rows with
   `row.thread_id == message.thread_id`, excluding the current message, and excluding rows with
   `sent_at <= reset_point.reset_at`.
2. Turns from `store.recent(chat, thread_id, settings.recent_turns)` (already filtered by the reset
   point and the summary), `user` and `assistant` text only (tool turns are skipped, as now).

De-duplication (each message appears once):
- A `user` turn is represented when a fetched human row has `message_id == turn.message_id`; keep
  the row, drop the turn.
- An `assistant` turn is represented when a fetched bot row has the same text (compare
  `.strip()`); each bot row can match at most one turn. Keep the row, drop the turn.
- Unrepresented turns (older than the log window, or a log without them) are kept and ordered by
  `created_at`; rows are ordered by `sent_at`. Merge both into one list sorted by timestamp; ties
  keep rows before turns.

Rendering, one line per entry, oldest first:
- humans (log rows): `[<ISO UTC time>] <display_name>: <text>`; unrepresented user turns (no
  author stored): `[<ISO UTC time>] (earlier request): <text>`;
- the bot (bot rows and assistant turns): `[<ISO UTC time>] <bot_name> (you): <text>`.
- Truncate each entry's text to `ambient_message_chars` (1500) with `…[truncated]`, then keep the
  **newest** lines whose combined length fits `ambient_total_chars` (6000), dropping the oldest;
  output stays chronological. The caps now apply to bot lines and turns too.
- Put a first line inside the block: `Recent conversation here, oldest first. Lines marked (you)
  are your own earlier replies.` (not counted against the cap).
- Wrap the whole block in one `_wrap(..., tag)` user message, placed where the ambient block is
  today (after the summary and `before_turn` hook text, before the replied-to message). If there
  are no lines, add nothing.

Replied-to bot message (added at the coordinator's request): today `_context` adds the replied-to
message only when it is a human message. When `message.reply_to` points at a **bot** message
(`replied.is_bot`), also add one wrapped block, in the same place as the human "Reply from …"
block: `Replying to your earlier message (<ISO UTC time>): <text>`, text truncated to
`ambient_message_chars` with the marker. Include it regardless of the reset point, summary or
recent-turns window: the user referenced it explicitly. It may also appear in the transcript;
that's fine. Bot reply targets still contribute no URLs or attachments (as now).

Unchanged: the summary block, hooks, the human replied-to message block, image notes, the final
`Name: text` user message, memory recall, URL collection, and everything after `_context`
(turn storage, compaction, usage). `store.recent` still filters by reset point and summary.

### Phase 1 and 2 tests (exist already)

Phase 1 (`test_group_flow.py` + routing tests):
1. Mention-only group: a reply to the bot → `RESPOND`, the responder is called, reply stays in the
   topic. A reply to the bot with bare `/models` → `IGNORE`. Untagged chatter → `IGNORE`, zero
   provider calls. `/reset@bot` and `@bot /reset` still → `COMMAND`. DM unchanged.
2. Bare tag: earlier untagged message from the same user in the same topic within 5 min → the
   provider sees that text as the request. A message from another user, another topic, older than
   300 s, a command, or a bot message is not used. None qualifying → the prompt text is sent and
   the provider is never called. `AgentResponder.respond` with empty text sends the prompt with no
   provider call.
3. Unknown command: `@bot /models` and `/models@bot` → the "I don't know /models" reply with
   `/help@<bot>`; in a DM `/models` → the same with `/help`. A non-admin running an admin-only
   command gets the identical text. Second attempt within 60 s → silent. Bare `/models` in a
   mention-only group → silent. A restricted user gets the restriction notice, not this reply.

Phase 2 (`test_transcript.py`):
4. Interleaving: Alice asks (tagged), bot replies, Bob chats untagged, Alice follows up: the
   transcript block lists them in time order with names and `(you)` on the bot line; the bot reply
   appears once even though it is both a turn and a log row; Alice's tagged message appears once.
5. Turns older than the log window still appear (from turns), ordered before the log rows.
6. Reset point: nothing before it appears (rows or turns).
7. Caps: a 10,000-char line is truncated to 1500 + marker; with many long lines the newest fit in
   6000 chars, chronological; a long bot reply is truncated too.
8. Other topics' rows (`thread_id` differs, including `None` vs a topic id) never appear.
9. The block is wrapped in the untrusted tag; no assistant-role replay of old turns remains.
9a. A reply to a bot message older than the recent-turns/log window: the bot's text appears in
    the "Replying to your earlier message" block, wrapped and capped. Same after `/reset` (the
    reset excludes it from the transcript but not from the reply block).
