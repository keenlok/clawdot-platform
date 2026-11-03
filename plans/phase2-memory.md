# Phase 2 run M: per-chat long-term memory (`plugin-memory`)

Read `ARCHITECTURE.md` §3c (all of it), §3b ("Embeddings", "Costs and limits"), §6a ("LLM-specific",
"Data and privacy", "DM and group isolation") and §7c. Phase 1 is merged: look at
`packages/plugin-search` and `packages/plugin-expenses` for how a plugin is built and tested, and
at `chatcore/agent.py` for how tools are called.

## You own

- `packages/plugin-memory/` (package `chatcore_memory`), including
  `migrations/0001_memories.sql` and `tests/`
- `packages/core/src/chatcore/store/es.py`: **only** to add the memories index template to
  `ensure_templates()` (keep the chunks template unchanged)
- `packages/core/tests/test_m_*.py` (for the template test, if you need one there)

Don't edit any other `chatcore` file. A parallel run is changing `agent.py`, `plugin.py`,
`app.py` and the stores right now, so you **must not** touch them.

## Interfaces you build against

Existing (Phase 0, do not change): `BasePlugin`, `Command`, `CommandContext`, `Tool`,
`ToolContext` (with `ctx.turn.read_untrusted`), `ToolResult`, `PendingAction.create(...)`,
`PendingActionHandler`, `Stores` (`db`, `search`, `cache`, `repo`), `SearchIndex`, `Embedder`
(`chatcore.embed`), `Provider` (`chatcore.llm.base`), `Limits` (`chatcore.limits`).

New, being added to `chatcore/agent.py` by the parallel run. **Implement these two methods on
`MemoryPlugin` with exactly these signatures**; the agent calls them on every plugin that has
them. Test them by calling them directly.

```python
async def before_turn(self, message: IncomingMessage, text: str, stores: Stores) -> str | None:
    """Context for this turn. The agent wraps the returned text as untrusted data and puts it
    after the persona (pinned memories) and summary, per §3c prompt order."""

async def after_turn(self, message: IncomingMessage, text: str, stores: Stores) -> None:
    """Runs after the reply is sent. ``text`` is the human's own message text, nothing else."""
```

## Constructor and settings

```python
class MemorySettings(BaseSettings):  # env_prefix="MEMORY_"
    recall_k: int = 5            # recalled per turn
    pinned_max: int = 10
    auto_extract: bool = True
    dedupe_threshold: float = 0.92   # cosine, when vectors are available
    max_chars: int = 300

class MemoryPlugin(BasePlugin):
    name = "memory"
    migrations_package = "chatcore_memory.migrations"
    def __init__(self, embedder: Embedder | None = None, *, provider: Provider | None = None,
                 limits: Limits | None = None, settings: MemorySettings | None = None): ...
```

`embedder` is `None` when the app has no Elasticsearch: then no vectors are computed and recall
uses text matching. `provider` is `None` to disable auto-extraction. `limits` (optional) records
extraction token use against the daily budget and skips extraction once the budget is spent
(`await limits.budget_used() >= limits.settings.DAILY_TOKEN_BUDGET`).

## Storage

**Postgres** (source of truth), `0001_memories.sql`:

```
memories(id bigserial PK, chat_key text NOT NULL, subject_user_key text NULL,
         text text NOT NULL CHECK (char_length(text) <= 500),
         source text NOT NULL CHECK (source IN ('explicit','auto')),
         pinned boolean NOT NULL DEFAULT false, created_by text NOT NULL,
         created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())
index (chat_key, id); index (chat_key, subject_user_key)
```

A small repository with a Postgres implementation (`stores.db`) and an in-memory one (used when
`stores.db is None`). **Every method takes `chat: ChatRef` as a required argument** and filters
by it, including `get`, `update` and `delete` by ID: deleting memory 5 from chat B must not
delete chat A's memory 5.

**Elasticsearch** (when `stores.search` is set and `embedder` is set): index name `"memories"`
(the backend prefixes it to `sb-bot-memories`). Doc ID `str(memory_id)`; fields `memory_id`,
`text`, `subject_user_key`, `source`, `pinned`, `created_at`, `embedding` (768). Write Postgres
first, then ES; an ES failure is logged (class name only) and never fails the write. Deletes are
hard in both (`stores.search.delete("memories", chat=..., query={"term": {"memory_id": id}})`).

**Template:** extend `EsSearchIndex.ensure_templates()` to also create `sb-bot-memories-v1`
(alias `sb-bot-memories`, `is_write_index`), mapping: `chat_key`, `subject_user_key`, `source`
(keyword), `memory_id` (long), `pinned` (boolean), `created_at` (date), `text` (standard analyzer
plus a `text.cjk` subfield, like chunks), `embedding` (`dense_vector`, 768, cosine,
`bbq_hnsw`). Same index-prefix logic as the chunks template. Use only Basic-license features.

**Recall:** hybrid when ES + embedder are there (`stores.search.search("memories", chat=...,
text=q, vector=v, size=k)`, which already fuses with RRF). If ES fails or isn't configured: text
match in Postgres (`ILIKE` on each word of the query, ranked by number of matching words, then
recency) or in memory. Never return another chat's memories.

## Tools

All per-chat; memory never crosses chats, so a DM has its own memory (§3c).

- `remember(fact: str, about: str | None = None, pinned: bool = False)`: save an explicit fact.
  `about` is an optional @username or display name, resolved against `repo.find_members(chat)`
  to set `subject_user_key` (unresolved → saved without a subject, no guessing). Reject empty or
  over-long facts (`max_chars`).
  - **If `ctx.turn.read_untrusted` is true:** return
    `ToolResult(pending_action=PendingAction.create(plugin="memory", kind="remember", ...))` with
    a summary quoting the exact fact, and register a `PendingActionHandler` under `"remember"`.
  - Otherwise save directly and say what was saved, including the ID.
  - Dedupe (below) applies either way.
- `recall(query: str, limit: int = 5)`: returns matching memories with their IDs as
  `untrusted=True`.
- `forget(memory_id: int)`: hard delete. Same `read_untrusted` rule (`kind="forget"`, summary
  quotes the memory's text). Unknown ID or another chat's ID → "no such memory".

Mark `remember` and `forget` with `writes=False` (they only sometimes need a confirmation, and the
registry insists `writes=True` tools always return one).

## Commands (no LLM)

- `/memory`: numbered list of this chat's memories, newest first, capped at 50, each as
  `#<id> <text>` with a marker for pinned and for `auto`. Empty → "I don't remember anything
  about this chat yet."
- `/forget <id>`: hard-delete that memory in this chat (anyone in the chat may). Bad or unknown
  ID → a short usage or "no such memory" reply. This is **not** `/forgetme`; don't add
  `/forgetme` (another run owns it).

## Turn hooks

**`before_turn`:** returns at most `pinned_max` pinned memories plus the top `recall_k` recalled
for `text` (deduplicated), formatted as lines `#<id> <text>`, under two short headings ("Pinned
facts about this chat" / "Possibly relevant memories"). Returns `None` when there are none.
Errors → log class name, return `None`; never fail the turn.

**`after_turn` (auto-extraction, §3c):** conservative.
- Skip if `provider is None`, `auto_extract` is off, the user is a bot, the text is under 12
  characters or starts with `/`, or the budget is spent.
- **Input is only `text`** (the human's own message). Never tool output, recalled memories, page
  text or bot replies. That is the §6a rule that stops a page planting a fact.
- One cheap `provider.complete(...)` call, `tools=()`, `max_tokens=200`, a strict system prompt
  asking for JSON `{"facts": [{"text": ..., "about_speaker": bool}]}` with **at most 2** durable
  facts about the group or its people (plans, preferences, standing arrangements), and none for
  opinions, one-off chatter, questions or anything sensitive.
- Parse defensively: anything that isn't valid JSON of that shape → no facts. Keep at most 2.
- **Hard filter after the LLM** (don't trust it): drop facts over `max_chars`, and facts matching
  a sensitive deny-list: health/medical words, money amounts and account or card numbers,
  passwords/PINs/OTP/API keys, email addresses, phone numbers, NRIC-like IDs (`[STFG]\d{7}[A-Z]`),
  street addresses. Keep the deny-list as a module constant with a unit test per category.
- Save survivors with `source="auto"`, `created_by=message.user`, and `subject_user_key` = the
  speaker when `about_speaker` is true.
- Record token use with `limits.record(...)` when `limits` is set. Errors → log class name only.

**Dedupe and replace** (both explicit and auto): before saving, find the most similar memory in
the same chat (vector score ≥ `dedupe_threshold` when vectors are available; otherwise a
normalized-token Jaccard ≥ 0.8). If found, **update that row's text** (and ES doc) instead of
inserting, and say "updated #id". Contradictions in natural language ("Bob is now vegan") are
left to the LLM: the tool description of `remember` tells it to `forget` the old memory first,
or the dedupe catches near-duplicates.

## Retention hooks

- `purge_user(chat, user, stores)`: hard-delete memories in that chat where
  `subject_user_key = user`, **or** `source='auto' AND created_by = user` (they were derived from
  that user's messages). Postgres and ES.
- `purge_chat(chat, stores)`: hard-delete all of the chat's memories, Postgres and ES.
- Memories are not aged out by retention (§6a: kept until forgotten). Don't add a time-based purge.

## Logging

Log memory IDs, chat keys, counts and error class names. **Never** the fact text, the query or
the extraction prompt/response.

## Tests (`packages/plugin-memory/tests/`)

Unit, with `memory_stores()`, `FakeEmbedder`, an in-memory fake `SearchIndex` written in tests
(or reuse the pattern in plugin-search's tests), and `ScriptedProvider`:
- remember / recall / forget happy paths; `/memory` and `/forget` output.
- **Isolation:** a memory saved in chat A is invisible to `recall`, `/memory`, `before_turn` and
  `forget`/`/forget` (by ID) in chat B and in a DM.
- **Confirmation gate:** with `ctx.turn.read_untrusted = True`, `remember` and `forget` return a
  pending action and write nothing; committing the handler writes; with it false they write
  directly.
- `recall` results are `untrusted=True`.
- Dedupe updates instead of inserting (vector path and Jaccard path).
- Auto-extraction: saves at most 2 facts; drops each sensitive category; ignores malformed JSON;
  skips bots, commands, short text and spent budget; the provider only ever sees the human text
  (assert on the `ScriptedProvider` request).
- No ES / no embedder: recall falls back to text matching and nothing calls the embedder.
- ES failure on write is logged and the Postgres row still exists.
- purge_user / purge_chat delete the right rows and leave other users' and chats' memories.
- Logs never contain the fact text (capture with `caplog` and assert).

Integration (marked `@pytest.mark.integration`, skip when unreachable):
- Postgres (`TEST_PG_DSN`, default `postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test`):
  migration applies; the repo round-trips; cross-chat isolation by ID.
- ES (`http://127.0.0.1:59200`, no auth): `ensure_templates()` with a random prefix creates the
  memories template; index + hybrid recall works; clean up the index and template afterwards.

Command: `uv run pytest packages/plugin-memory packages/core -q`.

## Done when

- `MemoryPlugin` has the tools, commands, hooks and purge hooks above, with the tests passing and
  `ruff check .` clean.
- The memories template is created by `ensure_templates()`.
- Integration tests skip cleanly without services.
- The report lists the exact `before_turn`/`after_turn` signatures you implemented.
