# Track C: attachments, chat-history search, embeddings, Elasticsearch

Read `ARCHITECTURE.md` §2, §3a, §3b (all of it), §6a ("Size and time limits", "Data and privacy",
"DM and group isolation"), and §7c.

## You own

- `packages/plugin-search/` (package `chatcore_search`)
- `packages/core/src/chatcore/embed.py`: the `Embedder` protocol, a fastembed backend, and
  `FakeEmbedder`
- `packages/core/src/chatcore/store/es.py`: the Elasticsearch `SearchIndex` backend
- Tests: `packages/plugin-search/tests/`, and `packages/core/tests/test_c_*.py`

Don't edit other `chatcore` files.

## 1. `embed.py`

```python
class Embedder(Protocol):
    dims: int
    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...
```

**`FastEmbedder`:**
- `fastembed.TextEmbedding("google/embeddinggemma-300m")`, run in a thread, 768 dims.
- Uses the model's separate query and document prefixes: fastembed's `query_embed` and `embed`.
- **Pinned revision:** load from a local cache dir (`EMBED_CACHE_DIR`). Offline loading must work.
- `scripts/download_model.py` in plugin-search fetches a pinned Hugging Face commit SHA and checks
  SHA-256 (§6a). It's for the deploy-time models job, not for tests.

**`FakeEmbedder`:** deterministic hashed bag-of-words vectors, so similar texts are close. Used in
tests.

## 2. `store/es.py`: `EsSearchIndex` implements `SearchIndex`

`supports_knn = True`. Client: the official `elasticsearch` async client, with `ES_URL` and
`ES_API_KEY` (`SecretStr`).

**Every query is filtered by `chat_key`**, added by the backend from the required `chat`
argument. Callers can't omit it.

**`search()`:**
- `text` only: BM25 over `text` and `text.cjk`.
- `vector` only: kNN.
- Both: run them **in parallel** and merge with **reciprocal rank fusion in Python**
  (`1/(60+rank)`). ES's `rrf` and `linear` retrievers need a paid license, so don't use them.

**Writes and deletes:**
- `index()` writes with `refresh=False`.
- `delete(index, chat=..., query=...)` uses `delete_by_query`, always scoped to the chat.

**Index templates** (created by an `ensure_templates()` admin step, not at runtime):
- `sb-bot-chunks-v1`, alias `sb-bot-chunks`. Fields:
  - `chat_key`, `kind` (`message_window` / `attachment` / `web_page` / `bot_answer`), `user_keys[]`,
    `ts_start`, `ts_end`, `trip_id`, `source_url`, `file_id`, `message_ids[]`;
  - `text`: standard analyzer, plus a `text.cjk` subfield with the built-in `cjk` analyzer;
  - `embedding`: `dense_vector`, 768 dims, `cosine`, `index_options: {type: bbq_hnsw}`.
- The index-name prefix comes from settings (`SEARCH_INDEX_PREFIX`, default `sb-bot-`).

**License constraints**, tested on Basic: `dense_vector`, `bbq_hnsw`, kNN and the `cjk` analyzer
work; `rrf`/`linear` retrievers, ML/`semantic_text` and `icu_tokenizer` don't. Use only what
works.

## 3. `plugin-search`: `SearchPlugin(BasePlugin)`, `name = "search"`

**Settings:** `SEARCH_WINDOW_SIZE=6`, `ATTACHMENT_MAX_BYTES=20_000_000`, `PDF_MAX_PAGES=50`,
`PARSE_TIMEOUT_S=30`.

**Indexing (`on_message`):**
- Group messages into sliding windows of about 6 consecutive messages from the same chat and
  thread, with sender names inlined.
- Embed and index each window when it fills, plus a periodic flush for the tail. Keep pending
  windows in memory per chat, and accept that a restart loses at most one partial window.
- Bot answers are indexed as `kind=bot_answer`. Search excludes them unless asked.
- Indexing is asynchronous and must never block or fail the message path: catch, log the class
  name, and move on.

**`read_chat_history(query: str, from_user: str | None, since: str | None, limit: int = 8)`:**
1. Run a hybrid search (BM25 + kNN, then RRF) filtered by the chat (and by user or date if given).
2. Expand the top hits to their surrounding messages via `repo.recent_messages` /
   `repo.get_message`, then return them with timestamps and senders as `untrusted=True`.
3. **In a DM:** search only groups the user is currently a member of. The plugin constructor takes
   `member_groups: Callable[[UserRef], Awaitable[list[ChatRef]]]`, which the app wires in
   Phase 2. Never search across groups the user isn't in.
4. **ES down or no `SearchIndex`:** fall back to Postgres
   `SELECT ... FROM messages WHERE chat_key=$1 AND text ILIKE $2 ORDER BY sent_at DESC LIMIT $3`
   via `ctx.stores.db`, read only. With no db either, return a clear "search unavailable" result.

**`read_attachment(message_id: str | None = None, index: int = 0)`:**
- Defaults to the current message's attachment, or the replied-to message's. Otherwise it reads
  a stored message's attachments via `repo.get_message(ctx.chat, message_id)`.
- Requires `capabilities.files`. Download via `ctx.platform.download(..., max_bytes=ATTACHMENT_MAX_BYTES)`.
- Parse within `PARSE_TIMEOUT_S`:
  - PDF with `pypdf`, at most 50 pages;
  - text, markdown and CSV decoded as UTF-8 with replacement.
- **Images:** return a short note that images are shown to the model directly when vision is on
  (track A handles that). Don't parse images here.
- Return the text, truncated to 50k chars, as `untrusted=True`. Also index it as a
  `kind=attachment` chunk.

**Admin `/search <text>`:** shows raw hybrid hits with the BM25 rank, the vector rank and the
fused rank side by side.

**Retention hooks:**
- `purge_user`: delete that user's chunks in that chat.
- `purge_chat`: delete all of that chat's chunks.

**`scripts/eval_search.py`:** runs a labelled set of query → expected message IDs (JSON fixture)
and prints the hit rate for BM25 only, vector only and hybrid.

## Tests

**Unit (`FakeEmbedder`, an in-memory fake `SearchIndex` you write in tests):**
- windowing and indexing;
- hybrid search and the RRF math;
- the chat filter is always applied, so chat A's text is never returned to chat B;
- DMs search only the user's groups;
- ES-down fallback to ILIKE;
- PDF and text parsing, with tiny fixtures;
- size cap, page cap and timeout;
- `/search` is admin only;
- the purge hooks.

**Integration** (marked; uses `docker compose -f compose.dev.yaml --profile search up -d`, ES at
`http://127.0.0.1:59200` with no auth): templates created, a `bbq_hnsw` mapping accepted, kNN
plus BM25 return hits, and RRF ordering checked.

## Done when

- Unit tests pass and ruff is clean.
- The integration tests skip cleanly without ES.
- The reviewer can run them against the dev ES.
