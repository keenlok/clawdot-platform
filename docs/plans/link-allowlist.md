# Link allowlist: seed `seen_urls` from human links in the reply chain and recent history

Setup first: `export UV_CACHE_DIR=$PWD/.uv-cache && uv sync --all-packages`.

Read `packages/core/src/chatcore/agent.py` (`_urls`, `_transcript_text`, `AgentResponder._context`,
`_respond_after_typing`), `packages/core/src/chatcore/store/base.py` (`StoredMessage`, `Repo`),
`packages/core/src/chatcore/store/memory.py`, `packages/core/src/chatcore/store/pg.py`,
`packages/core/src/chatcore/app.py` (`ChatApp.send`) and
`packages/plugin-web/src/chatcore_web/__init__.py` (the `fetch_web_page` `seen_urls` check, ~line 160-180).

## Background

`fetch_web_page` (and `fetch_rendered_page`, jobs URL provenance) only open URLs in
`ctx.turn.seen_urls`. That set is seeded in `_context` from `human_urls`: URLs in the current
message text plus the directly replied-to message if it is not a bot message. Production bug:
human posts link (A) -> human replies to A mentioning the bot (B) -> bot answers (C, replying to B)
-> human replies "try again" to C. `replied` is C (bot), so A's link is never seeded and the fetch
is refused.

Bot messages are logged by `Repo.log_bot_message` **without** `reply_to_id`, so today the chain
cannot be walked through a bot message. The `messages.reply_to_id` column already exists in
Postgres (no migration needed).

## You own
- `packages/core/src/chatcore/agent.py` — ONLY the URL-seeding code inside `_context` (plus at most
  one small module-level helper next to `_urls`). Another branch is editing this file heavily;
  keep the diff minimal and local. Do not reformat, move or rename anything else.
- `packages/core/src/chatcore/store/base.py`, `store/memory.py`, `store/pg.py` — only `log_bot_message`.
- `packages/core/src/chatcore/app.py` — only the `log_bot_message` call in `ChatApp.send`.
- New test file `packages/core/tests/test_link_allowlist.py`; you may add a case to
  `packages/plugin-web/tests/test_web.py` if useful.

Do not edit any other file. Do not change `_urls`, the web plugin's check, or `TurnState`.

## 1. Record the reply target on bot messages

- `Repo.log_bot_message(self, chat, message_id, text, *, thread_id, reply_to_id: str | None = None)`
  — new keyword-only optional parameter, default `None` (existing callers keep working).
- Memory store: set `StoredMessage.reply_to_id=reply_to_id`.
- PG store: add `reply_to_id` to the INSERT column list and values.
- `ChatApp.send`: pass `reply_to_id=reply_to.message_id if reply_to is not None and reply_to.chat == chat else None`.

## 2. Walk the reply chain in `_context`

After `replied` is resolved (keep the existing resolution code unchanged), replace the
`human_urls` seeding with:

- `human_urls = _urls(text)` (unchanged).
- Walk: start at `replied`. For up to `_REPLY_CHAIN_HOPS = 4` messages total (module constant):
  - if the row is not a bot message, extend `human_urls` with `_urls(row.text)`;
  - next = `row.reply_to_id`; stop if `None`, already visited (cycle guard), or the lookup
    `await app.stores.repo.get_message(message.chat, next)` returns `None`, or the fetched row's
    `chat != message.chat` or `thread_id != message.thread_id` (same checks as the existing
    `replied` lookup). A row that fails the check contributes nothing and ends the walk.
  - Continue past bot rows (that is the point) but **never** take URLs from a bot row's text.
- Keep it inside `_context` or one small `async def _reply_chain_urls(...)` helper; no new classes.

## 3. Recent history

`ambient` (from `recent_messages`) is already loaded in `_context`. Add URLs from rows that are
`not row.is_bot`, `row.thread_id == message.thread_id`, `row.message_id != message.message_id`, and
after the reset point (`reset_point is None or row.sent_at > reset_point.reset_at`) — the same
eligibility rule `_transcript_text` uses. Do not seed from `recent_turns` (assistant turns are bot
text; tool results are untrusted).

## Security rules
- Never seed URLs from bot messages, assistant turns, tool results, summaries, plugin hook text,
  or any other model-visible content that is not a human-authored message row / the current text.
- Same chat and same thread only. Respect the reset point.
- Normalise only through `_urls` (existing trailing-punctuation strip). Deduplication is optional
  (it becomes a set).
- ARCHITECTURE.md §6a: add no logging of message text, URLs or URL paths.

## Tests (`packages/core/tests/test_link_allowlist.py`, unit, in-memory stores)

Look at existing agent tests (e.g. `packages/core/tests/test_agent_vision.py`,
`test_transcript.py`, `test_threads.py`) for how to build an `AgentResponder` with a fake
provider and call `_context` or a full turn; reuse their fakes/patterns.

1. Regression for the production chain: human A posts `https://share.google/abc123`; human B
   replies to A mentioning the bot; bot message C logged via `app.send(..., reply_to=B.ref)` (or
   `log_bot_message(..., reply_to_id=B)`); human D replies "try again" to C. With
   `recent_context_messages` small enough that A is NOT in the ambient window (e.g. 1, or pad with
   filler rows), the seeded URLs include A's URL. Prefer also asserting end-to-end that a
   `fetch_web_page` tool call for that URL passes the `seen_urls` check (the web plugin test file
   shows how the check is exercised), if practical.
2. A URL that appears only in a bot message (bot message replied to directly, and bot message in
   the ambient window) is NOT seeded.
3. Cross-thread: a chain hop whose row is in another thread contributes nothing (and its own
   ancestors are not followed). Cross-chat: `reply_to_id` is chat-scoped by the stores; assert a
   message in another chat with the same id is not used.
4. Hop limit: a chain longer than `_REPLY_CHAIN_HOPS` stops; a reply cycle terminates.
5. Recent history: a human link in the ambient window (same thread, after reset) is seeded; one
   before the reset point or in another thread is not.
6. Memory store: `log_bot_message(..., reply_to_id="x")` stores `reply_to_id`; `ChatApp.send` with
   `reply_to` records it. Add a PG assertion to an existing integration test only if trivial;
   otherwise skip (Claude runs integration tests separately).

## Done when
- `uv run pytest -q` passes; `uv run ruff check packages apps tests` and
  `uv run ruff format --check` on changed files are clean.
- The diff to `agent.py` is confined to the URL-seeding lines in `_context` plus a constant /
  small helper.
- Not in scope: the research plugin's own `/research` seeding (`_human_urls` in
  `plugin-research/plugin.py`), PG integration runs, deploys.
