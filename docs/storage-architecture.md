# How Postgres, Valkey and Elasticsearch fit together

Clawdot separates durable records, temporary working state and search into three
store interfaces. A persistent application can use **Postgres** for records,
**Valkey** for caching and coordination, and **Elasticsearch (ES)** for finding
relevant text. The application connects to all three and moves data between them.
The databases do not automatically synchronize with each other.

This guide describes those implementations in the public platform repository.
The [example scaffold](../apps/_scaffold/README.md) uses in-memory stores and does
not enable search or memory plugins. A downstream application must select and wire
the persistent stores and plugins; starting database containers or setting service
URLs alone does not change the scaffold's behavior.

## The overview

![Clawdot application connected to Postgres for durable records, Valkey for temporary cache and coordination, and Elasticsearch for derived text and vector search. The application saves records before indexing and reads source messages for search results.](images/storage-architecture.png)

[Editable SVG](images/storage-architecture.svg). The labeled arrows represent
application calls, not database replication. For example, a message is stored in
Postgres before the search plugin schedules its ES index write. Valkey helps the
application cache results and coordinate work along the way.

| Piece | What belongs there | Persistence and loss |
| --- | --- | --- |
| **Postgres: durable records** | Messages, members, chat settings, pending confirmations, conversation turns/summaries, and enabled plugins' tables such as memories and jobs | Retain and back up the database. Transactions protect related relational updates. Losing it can lose authoritative application records. |
| **Valkey: temporary working state** | Expiring cached results, rate-limit and budget counters, membership checks and locks, depending on app/plugin wiring | Keys expire. Cache contents can be lost; locks and counters also lose continuity. The development service disables persistence. |
| **Elasticsearch: derived search index** | Message windows, bot-answer documents, extracted attachment chunks, and memory documents with text, metadata and embedding vectors | Search documents complement the source records. Index loss reduces search coverage until data is explicitly indexed again. ES is not the authoritative store for relational records. |

Conversation history and durable memory are different things: recent dialogue and
summaries live in relational tables; the memory plugin stores selected long-term
facts in its repository and also indexes those facts in ES for recall.

## Following a message into storage

Consider a group message: “The hotel booking is for Friday.” On the normal incoming
message path, after access checks, `BotApp` logs it through `stores.repo`, updates
membership for a group, then calls plugin message hooks. With `PgCoreRepo`, that
message log is a Postgres write. Special command paths can return earlier.

```mermaid
sequenceDiagram
    participant User
    participant App as Clawdot app
    participant PG as Postgres
    participant Search as Search plugin + embedder
    participant ES as Elasticsearch
    User->>App: Group message
    Note over App: Access checks; normal message path
    App->>PG: Log message and update member
    PG-->>App: Writes complete
    App->>Search: on_message hook
    Note over Search: Buffer windows; schedule background work
    Search->>Search: Turn text into embedding vectors
    Search->>ES: Index text, vectors and source message IDs
    Note over PG,ES: Separate operations; no shared transaction
```

The search plugin buffers overlapping windows of human messages and flushes pending
windows periodically. It also scans recent stored bot answers. Embeddings are
computed through the application's embedder, outside ES; ES stores and searches the
vectors. Index writes use `refresh=False`, so successful writes need not be visible
to a search immediately.

This is best-effort background indexing. There is no durable indexing outbox or
automatic replay of every failed write. A process exit can lose pending windows,
and an indexing failure is logged while the message remains in Postgres. Search can
therefore lag or miss records already saved in the source database.

Valkey is used by separate application operations, rather than being a mandatory
intermediate step in message storage. For example, `Limits` uses expiring cache keys
to count requests, and `BotApp` uses cache locks to coordinate agent work. Other
plugins can cache expensive external responses.

## Following a history search

A question such as “What did we decide about the hotel?” enters the search plugin's
`read_chat_history` tool. With ES configured, the plugin embeds the query and runs
both keyword search (BM25) and nearest-vector search (kNN). `EsSearchIndex` combines
their rankings in Python using reciprocal rank fusion (RRF).

```mermaid
sequenceDiagram
    participant Tool as History search tool
    participant Embed as App embedder
    participant ES as Elasticsearch
    participant PG as Postgres
    Tool->>Embed: Embed the question
    Embed-->>Tool: Query vector
    Tool->>ES: Keyword + vector search with chat filter
    ES-->>Tool: Ranked hits and source message IDs
    Note over Tool: Fuse rankings in Python
    Tool->>PG: Load referenced source messages
    PG-->>Tool: Text, speakers and timestamps
    Note over Tool: Format history snippets for the agent
```

The app embeds the question once; the ES backend issues the two searches concurrently.
The search result IDs lead back to repository messages, allowing the tool to return
stored source details. If a hit has no retrievable source message, the implementation
can return its indexed text with an unknown speaker instead.

ES queries always include `chat_key`. History search also supports sender/date
filters and excludes bot answers by default. DM search needs an application-supplied
membership lookup and only searches the groups that lookup returns. Prefixes alone
do not implement user authorization.

**History fallback:** if the embedder or ES request fails, or no search store is
configured, `_search_chat` tries a Postgres `ILIKE` substring query when `stores.db`
exists. It respects chat, sender, date and bot-answer filters, with newest matches
first. It provides no vector ranking or semantic matching. A successful ES query
with zero hits does **not** trigger this fallback. If the SQL database is unavailable,
this path cannot provide a general offline mode. The admin `/search` debug command
requires ES and has a different path.

## Where long-term memory fits

The memory plugin saves a fact to its repository first, then tries to embed and index
it under the ES `memories` alias. With Postgres configured, its repository uses the
`memories` table. Index failures are logged after the durable save.

Recall searches ES and fetches matching memory IDs from the repository. When no
usable rows are obtained, it falls back to repository text matching. Pinned facts
and conversation summaries have their own context-loading paths; they are not all
cache entries or all ES documents.

## Wiring and isolation

[`Stores`](../packages/core/src/chatcore/store/base.py) is the application-facing
bundle. Its fields select distinct capabilities:

| Field | Persistent implementation | Why it matters |
| --- | --- | --- |
| `repo` | `PgCoreRepo` | Core message/member/settings operations |
| `db` | `PgDatabase` | SQL transactions and plugin repositories; also history fallback |
| `cache` | `ValkeyCache`, optionally wrapped in `FallbackCache` | TTL values, counters and locks |
| `search` | `EsSearchIndex` | Optional text/vector index interface |

The downstream app owns connections, migrations, ES template initialization, plugin
selection and lifecycle. Settings such as `PG_DSN`, `VALKEY_URL`, `CACHE_PREFIX`,
`ES_URL`, `ES_API_KEY` and `SEARCH_INDEX_PREFIX` describe service configuration;
they are not a persistent-mode switch for the public scaffold.

Use separate database credentials, cache prefixes and ES index prefixes for separate
applications. `EsSearchIndex` defaults to the historical `sb-bot-` prefix; choose an
explicit application prefix instead of assuming isolation. Its templates define
versioned `chunks-v1`/`memories-v1` indices with `chunks`/`memories` aliases, and
currently use 768-dimensional vectors. A replacement embedder must match the index
mapping; changing the embedding space requires a deliberate index migration.

Namespaced identifiers such as `tg:42` identify platform users or chats; chat-scoped
repository queries and ES filters keep operations scoped. Deployments still need
appropriate credentials, network isolation and access checks. Valkey needs its own
access controls as well as key prefixes and positive TTLs.

## Outages, restarts and recovery

| Situation | What the implementation does | Operational consequence |
| --- | --- | --- |
| Postgres unavailable | Core/plugin SQL operations can fail; ES and Valkey do not replace those records | Restore database connectivity. There is no automatic switch from a running persistent app to an in-memory database. |
| Valkey unavailable, app uses `FallbackCache` | Supported cache operations fall back to an in-process `MemoryCache` on handled connection errors | Each process has separate temporary state; counters and locks do not preserve distributed continuity. Lock-acquisition timeouts are not all treated as connection failures. |
| Valkey restarted or keys evicted/expired | Reads can miss; caches populate again through their callers | Previous cached results, lock ownership and rate-limit counts are not reconstructed wholesale from Postgres. |
| ES or embedder unavailable | History search attempts SQL fallback; memory recall can use repository text matching; index writes can fail | Basic lookup can remain available, with weaker matching. Failed indexing is not automatically replayed. |
| ES restarted with its data retained | Existing index contents remain subject to ES durability and deployment configuration | Verify index health and templates; application-side pending work may still have been lost. |
| ES data lost | Durable source messages/facts can remain in Postgres | Recovery needs an explicit backfill/reindex implementation. Do not assume a restart repopulates the index. |

`FallbackCache` must be wired explicitly; `ValkeyCache` by itself propagates errors.
Its fallback is not a replicated cache, and successful reconnection does not merge
the fallback process's state back into Valkey. When an app supplies `PgBudgetUsage`
to `Limits`, the daily token-use mirror can recover persisted usage after a missing
cache value. That is a specific recovery mechanism, not persistence for every key.

Back up Postgres as the authoritative relational store. Recovery of search also
depends on retaining the data needed to regenerate each document: original messages,
memory facts, attachment content or another reproducible source, plus a compatible
embedder and mappings. Some extracted attachment text is indexed directly; Postgres
message metadata alone is not a guaranteed archive of that extracted text.

The architecture record discusses `/reindex`, but **the current public platform does
not register that command or ship a general backfill runner**. This guide does not
promise automatic ES reconstruction or introduce a new recovery tool. A deployment
must establish and test its own recovery procedure before discarding an index.

## Try the disposable development services

[`compose.dev.yaml`](../compose.dev.yaml) supplies Postgres 17, Valkey 8 and optional
Elasticsearch 9.3.1 for integration tests:

```sh
docker compose -f compose.dev.yaml --profile search up -d
TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test uv run pytest -q -m integration
```

The addresses, security settings and credentials in that file are for disposable
local testing. Individual integration tests have their own service requirements;
starting these services does not wire them into the scaffold. Postgres uses `tmpfs`,
and Valkey disables persistence; this setup is not a production backup strategy.

## Source map and wiki

These files are the implementation references for this explanation:

- [Store interfaces and bundle](../packages/core/src/chatcore/store/base.py),
  [Postgres backend](../packages/core/src/chatcore/store/pg.py),
  [Valkey and fallback cache](../packages/core/src/chatcore/store/valkey.py)
- [Application event flow](../packages/core/src/chatcore/app.py),
  [limits and budget mirror](../packages/core/src/chatcore/limits.py)
- [ES queries, templates and RRF](../packages/core/src/chatcore/store/es.py),
  [embedding providers](../packages/core/src/chatcore/embed.py)
- [Search indexing and history fallback](../packages/plugins/search/src/chatcore_search/plugin.py),
  [memory save and recall](../packages/plugins/memory/src/chatcore_memory/plugin.py),
  [memory repositories](../packages/plugins/memory/src/chatcore_memory/repository.py)
- [Scaffold wiring](../apps/_scaffold/src/example_bot/app.py),
  [architecture record, storage §3a](../ARCHITECTURE.md#3a-storage)

The [wiki publishing instructions](wiki/README.md) and
[copy-ready wiki page](wiki/Storage-architecture.md) reuse this guide and image.
