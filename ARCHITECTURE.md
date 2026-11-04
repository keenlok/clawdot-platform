# Clawdot architecture

The architecture spec and decision record for the reusable Clawdot platform. It was
`PLAN.md` until 2026-11-01; section numbers remain stable for references such as §3d.
Every plan and its status is indexed in [docs/plans/README.md](docs/plans/README.md). Repository
rules are in [AGENTS.md](AGENTS.md).

The platform contains a platform-independent core, chat adapters, capability plugins,
and standalone services. `apps/_scaffold` is a working generic application whose tests,
wheel installation and container build run in platform CI. Bot personas, plugin selection,
private configuration, secrets and operator infrastructure belong in the owner-only
`clawdot-deployments` repository. Downstream apps pin reviewed platform releases.

Historical feature records describe the original application before the split. They do
not imply that a platform merge deploys a bot or enables a feature in a downstream app.
The split is complete; this repository starts from a sanitized framework snapshot, and
earlier history (`clawdot-platform-history`) remains private.

## 1. Decisions so far

| Area | Decision |
|---|---|
| Language | Python 3.12, `uv`, async throughout |
| Structure | **Reusable framework** in a `uv` workspace monorepo: `core` + adapters + plugins + thin apps (§3d). Working name `chatcore` (placeholder, rename freely) |
| Chat platforms | Behind an adapter interface. v1 ships **Telegram** (`python-telegram-bot` v21+, async, **long polling**, so no inbound port) and a **console** adapter for local dev and tests. Discord and Slack are later candidates |
| LLM | Pluggable providers: **Anthropic** (native SDK) plus **OpenAI-compatible** (OpenAI, OpenRouter, OpenCode) through one client with a configurable `base_url` |
| Runtime model | Chosen by each application through provider settings |
| Web search | Tavily |
| Storage | Postgres, Elasticsearch and Valkey backends, or in-memory stores for the scaffold (§3a) |
| Security | Basic baseline in §6a: scoped credentials, untrusted-content rules, SSRF guard, hardened container, retention and deletion |
| Google output Docs | A confirmed new native Doc may be private or shared as non-discoverable anyone/reader or anyone/writer when its preview freezes that choice. A retained output journal binds continuation authority to the same owner and exact chat/topic. Originals, external sources, Sheets and browser trials keep separate sharing rules; the reusable framework defaults to private. |
| Deploy target | Selected and operated by each downstream application (§6) |
| Web page tool | A single `fetch_web_page`: HTTP fetch plus text extraction, with no headless browser |
| Expense currency | SGD by default. A **trip** sets its own currencies, and each trip chooses whether to settle in SGD or per currency (§4) |
| Where it answers | The allowlisted group(s), plus **DMs from people who are members** of those groups |
| Stock integration | Read-only public API only for v1 |

## 2. Tools exposed to the LLM

| Tool | What it does | Notes |
|---|---|---|
| `search_web(query, max_results)` | Tavily search; returns title, URL and snippet | Results are capped to keep context small |
| `fetch_web_page(url)` | Downloads a page and extracts readable text (`trafilatura`); falls back to raw text for PDFs and plain text | Treated as one tool, covering both "open_web_page" and "read_web_page". **URL provenance rule**: it only fetches URLs that a human posted or that `search_web` returned in this turn. **SSRF guard** plus size and time limits (§6a) |
| `read_chat_history(query?, from_user?, since?, limit)` | Reads earlier messages from this group's stored log | Telegram bots cannot fetch history, so the bot logs every message it sees from the moment it joins. **Privacy mode must be disabled in BotFather** |
| `read_attachment(message_ref)` | Reads a file or photo shared in the chat: PDF (`pypdf`), text/markdown/CSV, images (sent to the LLM as vision input when the model supports it) | Files are downloaded on demand via the Telegram file API, with a size cap |
| `add_expense`, `update_expense`, `delete_expense`, `restore_expense`, `undo_expense_change`, `list_expenses`, `get_balances` | Splitwise-like ledger, scoped per group (§4) | Expense writes apply directly and are echoed with an undo hint; after untrusted content in the turn they need the requester's confirmation button (§6a) |
| `settle_up`, `set_fx_rate` | Settle-up and FX overrides (§4) | Writes need a confirmation button **pressed by the requesting user** |
| `start_trip`, `update_trip`, `end_trip`, `activate_trip`, `reopen_trip`, `set_default_trip`, `list_trips`, `get_trip` | Plans, opens, edits, closes or reopens trips with their countries, currencies, settle mode and members (§4) | Writes need a confirmation button pressed by the requesting user |
| `trip_attach_file`, `trip_list_files`, `trip_read_file`, `trip_remove_file` | A primary itinerary and supporting Drive files on an existing trip (§4) | Attach/remove require confirmation; exact saved-file reads recheck group/owner/access |
| `google_read_file` | Bounded native Docs/Sheets, DOCX, text PDF and UTF-8 CSV reads | Human-posted file link; unsupported formats return metadata only |
| `google_edit_doc`, `google_edit_sheet` | Preview bounded native Doc text or typed Sheet range edits | Requester confirmation, current writer authority, frozen destination and content checks; qualified copies only; DOCX always imports into a new native Doc |
| `update_trip_members` | Adds or removes trip members, the default split (§4) | Applies directly with an echo, like expense writes |
| `export_expenses` (and `/export`) | Sends one trip's ledger, or the general ledger, as a CSV file for checking by hand (§4) | Read-only; group members only, DMs use the usual group resolution |
| `stock_analysis(ticker)`, `stock_digest()`, `stock_prices(ticker, days)`, `stock_list(sector?, recommendation?)` | Read-only calls to the stock-investor public API (§5) | No credentials needed |
| `remember(fact)`, `recall(query)`, `forget(memory_id)` | Per-chat long-term memory (§3c) | Scoped to the current chat only. If the turn has read untrusted content, `remember` and `forget` need a confirmation button (§6a) |

Plain slash commands that skip the LLM, so they are cheap and predictable: `/balance`, `/expenses`, `/help`, `/privacy`, `/reset`, `/memory`, `/forget`, `/forgetme`, `/persona` (§3c, §6a). Group commands include the bot username, e.g. `/help@example_bot`. **Admin-only** commands (user IDs in `ADMIN_USER_IDS`): `/reindex`, `/search` (debug), `/usage`, `/purgechat`.

## 3. Architecture

```
clawdot-platform/                        # repo root = uv workspace
  pyproject.toml                         # workspace members
  uv.lock                                # committed; hashes for every package
  .gitignore                             # .env*, *.key, *.pem (except .env.example)
  .pre-commit-config.yaml                # gitleaks + ruff
  .claude/settings.json                  # Codex allow/deny rules (§7b)
  packages/
    core/src/chatcore/                   # platform-independent framework
      events.py                          # IncomingMessage, Attachment, User, ChatRef (namespaced IDs)
      platform.py                        # ChatPlatform protocol + Capabilities
      plugin.py                          # Plugin protocol: tools, commands, migrations, settings
      app.py                             # BotApp: wires adapter + plugins + stores; message routing
      routing.py                         # when to respond: mention, reply-to-bot, /ask, DM, chatmode
      access.py                          # chat allowlist, current-member check (via adapter), admin IDs
      confirm.py                         # PendingAction store + confirm flow (buttons or reply-code fallback)
      agent.py                           # tool loop (max N steps, time cap), context assembly, untrusted wrapping
      conversation.py                    # turns, rolling summary, persona
      llm/                               # types, Provider protocol, anthropic, openai_compat (+ OpenCode headers), factory
      tools/registry.py                  # Tool, ToolContext, ToolResult
      security/net_guard.py              # SSRF guard: shared by every plugin that fetches URLs
      store/                             # interfaces + backends: pg, es, valkey, sqlite, memory
      embed.py                           # Embedder protocol + fastembed backend
      retention.py                       # purge hooks each plugin implements
      log.py                             # redaction formatter (not logging.py: avoids shadowing stdlib)
      migrations/                        # core tables (messages, members, turns, pending actions…)
    adapters/                            # chat platform adapters
      telegram/src/chatcore_telegram/    # python-telegram-bot ↔ events; buttons, edits, files, getChatMember
      console/src/chatcore_console/      # terminal chat: dev REPL + end-to-end test harness
    plugins/                             # plugins the bot loads (layout: AGENTS.md "Plugin module layout")
      web/src/chatcore_web/              # search_web, fetch_web_page
      search/src/chatcore_search/        # read_attachment, read_chat_history, hybrid search, /search
      memory/src/chatcore_memory/        # remember/recall/forget, /memory, auto-extraction
      expenses/src/chatcore_expenses/    # ledger, trips, FX, /balance
      stocks/src/chatcore_stocks/        # stock-investor public API
      browse/, jobs/, polls/, research/, extensions/   # added after Phase 2
  services/                              # standalone framework-side services (not bot plugins)
    browser-renderer/                    # sandboxed JavaScript renderer (chatcore_browser_renderer)
    release-control/                     # protected release-control services (chatcore_release_control)
  apps/
    _scaffold/
      src/example_bot/                   # console wiring + packaged generic persona
      .env.example                       # optional provider configuration, no credentials
      Dockerfile, compose.yaml           # pinned working example image
      deploy/deploy.sh                   # generic local compose skeleton
      tests/                             # core/app wiring and access tests
  tests/                                 # cross-package end-to-end tests (console adapter)
```

Each package has its own `tests/`. Plugins depend only on `core`; adapters depend only on `core`; apps depend on whatever they pick. Nothing in `core` or a plugin imports Telegram.

**Provider abstraction.** Tools are defined once as JSON Schema. Each provider translates neutral messages and tools into its own wire format and back. The agent loop stays provider-agnostic. Switching providers is a config change:

```dotenv
LLM_PROVIDER=openai_compat        # or: anthropic
LLM_BASE_URL=https://opencode.ai/zen/go/v1   # OpenRouter: https://openrouter.ai/api/v1
LLM_MODEL=deepseek-v4.1-flash
LLM_API_KEY=...
```

**OpenCode specifics:**
- Send an `x-opencode-session: <stable id>` header, **only when the `base_url` host is `opencode.ai`**. Use `tg-<chat_id>` (plus the thread ID for forum topics), so one group maps to one OpenCode session.
- Send `User-Agent: sb-bot/1.0`.
- Pass these as `default_headers`/`extra_headers` on the OpenAI client. Other providers get no extra headers.
- Use `parallel_tool_calls=False` for OpenCode models. The loop still handles multiple tool calls if a provider returns them.

**When the bot responds in the group:** only when explicitly @mentioned or given a command addressed to its username, such as `/ask@example_bot <text>`. Untagged replies to its messages and bare commands stay quiet. Replies stay attached to the triggering message/topic. Pending-action confirmation buttons/codes remain deliberate interactions.

**DMs:** the bot answers a private message only if the sender is a **current** member of an allowlisted group, and ignores everyone else. Membership is checked with `getChatMember` (cached for 1h in Valkey), not just the `members` table, so people who leave lose access. In a DM:
- Expense and trip tools act on the user's group. If the user is in more than one group, the bot asks which.
- `read_chat_history` only searches groups the user belongs to.
- DM conversations are logged separately and are never readable from the group.

**Context per turn:** see §3c.

**Guardrails**
- `ALLOWED_CHAT_IDS` allowlist. The bot ignores other chats, and **leaves** any group it is added to that isn't on the list, so strangers can't spend your tokens.
- Per-user and per-chat rate limits, plus a daily token budget, counted in Valkey. Usage is also logged to Postgres. Limits are in §6a.
- Content from web pages, files, search results, chat history and recalled memories is wrapped as **untrusted data** in the prompt. Write tools only act on what the requesting user asked, and need a button confirmation **from that user** (§6a).
- Long replies are split at Telegram's 4096-character limit; markdown is escaped safely. **Link previews are disabled** on every bot message (§6a).

## 3d. Framework: adapters, plugins and apps

**Goal:** the same code runs a bot on Telegram today and on another chat app (or for another project) later, by writing an adapter or picking different plugins, never by editing the core.

**Normalized events (in):** adapters convert platform updates to:
- `IncomingMessage(platform, chat: ChatRef, thread_id, message_id, user: User, text, attachments, reply_to, mentions_bot, is_dm, raw_ts)`
- `ButtonPress(platform, chat, user, action_id)`, `MemberJoined`, `MemberLeft`, `BotAddedToChat`

Plugins and the agent never see `telegram.Update` or any other platform object.

**Normalized actions (out):** the `ChatPlatform` protocol every adapter implements:

| Method | Purpose |
|---|---|
| `send(chat, text, reply_to?, thread_id?) -> MessageRef` | Post a message (the adapter handles length splitting and markup escaping) |
| `edit(msg, text)` | Update a message in place (streaming replies) |
| `typing(chat)` | Typing indicator |
| `confirm(chat, prompt, action_id, requester)` | Show Confirm/Cancel for a pending action |
| `download(attachment) -> bytes` | Fetch a shared file, with the size cap |
| `is_member(chat, user) -> bool` | Current-membership check for DMs and access |
| `leave(chat)` | Leave a non-allowlisted chat |
| `capabilities` | `buttons`, `edit`, `threads`, `files`, `dm`, `max_message_len`, `markup` |

**Capability fallbacks in core:**
- No buttons: the confirmation is "reply `YES 4821` to confirm", where the code is single-use and only valid from the requester.
- No edit: no streaming; the bot sends the final reply once.
- No threads: `thread_id` is ignored and conversations are per chat.

**Namespaced IDs:** every chat and user ID is stored as `<platform>:<id>` (e.g. `tg:-100123`, `tg:42`). That keeps several bots and platforms on the same Postgres, ES and Valkey without collisions, and the per-chat isolation rules (§3c, §6a) apply per namespaced chat. There is no cross-platform identity linking in v1.

**Plugin interface:** a plugin is a class exposing:
- `name`, its `Settings` class (pydantic-settings, env prefix per plugin), and its `migrations/` directory;
- `tools(ctx) -> list[Tool]`, `commands() -> list[Command]`, and pending-action handlers;
- optional hooks: `on_message` (e.g. indexing), `on_startup`, `purge_user`/`purge_chat` (for §6a retention).

Apps list plugins explicitly in `main.py`; there is no auto-discovery, so nothing loads that you didn't pick.

**Storage backends:** `core.store` defines `Relational`, `SearchIndex` and `Cache` interfaces.
- **Persistent applications:** Postgres + Elasticsearch + Valkey, selected by the app.
- **Small projects:** in-memory stores now (data lost on restart); a SQLite backend is a later addition, so a bot can persist with no services at all.
- A plugin that needs vectors (search, memory) declares it, and the app fails fast at startup if the configured `SearchIndex` can't do kNN.

**Application ownership:** persona prompts, plugin selection, composites, allowlists,
admin IDs, private settings and deployment files belong to the downstream application.
The scaffold demonstrates generic wiring without credentials or backing services. Copy
it into `bots/<bot>/` in `clawdot-deployments`, replace workspace dependency sources with
reviewed release pins, and follow its README checklist. The OpenCode header logic remains
in core's provider and derives its session from the namespaced chat ID.

**Standalone services** (`services/`): framework-side processes that run outside the bot, such as the browser renderer and release control. They follow the same rules as `packages/` and are lifted out with it. `packages/plugins/*` is reserved for plugins the bot loads.

**Platform/deployment boundary:** `packages/`, `services/` and the working scaffold
stay here; private apps, secrets/configuration, shared deployment scripts and manifests
live in `clawdot-deployments`. Per-bot encrypted secrets have per-bot recipients. Write
access to deployments is deploy access for every bot; it remains owner-only. Platform
CI builds/tests the scaffold, and downstream CI installs explicitly pinned platform
packages and tests the real bot. Release-control code stays here, but its cross-repository
configuration remains private and inactive pending a separately reviewed redesign.

- **Rules from day one** keep it extractable:
  - Nothing in `packages/` or `services/` imports from `apps/` or reads sb_bot config.
  - Each package has its own version, tests and `README`.
  - `ruff` banned-api rules enforce the import boundary.
  - Apps import plugins and adapters only through each package's top-level exports (`from chatcore_jobs import JobsPlugin`, never `chatcore_jobs.plugin`). Plugins reach the agent through `skills()`/`tool_gates()`, never by import, and skills that span plugins are app composites. `tests/test_package_boundaries.py` enforces this.
  - **Plugin file layout** (layout plan D6): `__init__.py` holds re-exports only; `plugin.py` holds the Plugin class, tools and `skills()`; `gates.py` holds `tool_gates()` via `build_gates(plugin)`; `settings.py` holds env settings; plus domain modules (`service.py`, `repository.py`, `models.py`, `migrations/`). Release-control policy inventory, for whoever writes the manifest:

    | Class | Files | Release path |
    |---|---|---|
    | Gates and permission checks | `*/gates.py`, `chatcore_jobs/permissions.py`, `chatcore_polls/permissions.py`, `chatcore_tutor/permissions.py` | Protected if the owner accepts layout plan D7 (pending); otherwise ordinary |
    | Security-critical runtime | `chatcore_extensions/{authority,runtime,sandbox,worker}.py`, `chatcore_research/quota.py` | Protected |
    | Schema | every `*/migrations/` | Protected (migration identity) |
    | Package wiring | `*/__init__.py`, `pyproject.toml` | Protected (package metadata, import surface) |
    | Ordinary plugin code | the remaining `plugin.py`, `settings.py`, service, repository, model and helper modules, and their tests | Ordinary (enumerated per file) |

    Not yet normalised: `chatcore_memory` keeps its settings in `plugin.py`, and expenses keeps `ReceiptSettings` in `receipt.py`. Core's gates (`display_time_plugin.py`, `conversation.py`) stay in place, because all of core is protected.
**Kept deliberately small:** only two adapters in v1 (Telegram + console). The console adapter proves that the abstraction isn't Telegram-shaped, and it doubles as the end-to-end test harness. A real third platform waits until there's a project that needs it.

## 3a. Storage

Core defines relational, search and cache interfaces. Persistent apps choose Postgres,
Elasticsearch and Valkey with scoped credentials; the scaffold uses in-memory stores.
Host names, service topology, role provisioning and recovery inputs are private operator
records in the pre-split architecture archive (`clawdot-deployments/docs/archives/platform-ac5a4b0/ARCHITECTURE.md`).
Do not derive production configuration from this framework specification.

## 3b. Vector search in Elasticsearch

**Goal:** questions like "where did we say we'd eat in Osaka?" or "that article about rate cuts someone shared" should find matches even when the words differ.

**Embeddings are computed in the bot, not in ES:**
- Default model: **`google/embeddinggemma-300m`** via `fastembed` (ONNX, CPU-only, 768 dimensions, 2048-token input, about 1.2 GB). It is multilingual, so English, Chinese and Singlish mixes work, and it needs no API key.
  - Weights come from Hugging Face at a **pinned revision (commit SHA)**. A one-shot `models` job downloads them into the `app-models` volume and checks their SHA-256. The bot mounts that volume **read-only** and runs with `HF_HUB_OFFLINE=1`, so it never downloads at runtime (§6a supply chain).
  - To check in step 4: `google/embeddinggemma-300m` is gated on HF. If fastembed pulls it from the gated repo, the download job needs a read-only HF token. That token goes only into the one-shot job, never into the bot's `.env`.
  - The box has about 10 GB of RAM available; the model uses about 1.5 GB resident.
  - Its throughput on the 12 cores is to be measured in step 4. Indexing is asynchronous anyway, and only the query embedding is on the reply path.
- Fallbacks if it disappoints:
  - Local: `Qwen/Qwen3-Embedding-0.6B-Q` (1024 dims).
  - Hosted: OpenAI `text-embedding-3-small` (about $0.02 per million tokens).
- Note: `multilingual-e5-small`, from an earlier draft, is **not** supported by fastembed (checked 2026-10-30).
- Why in the bot: ES's built-in models (ELSER/E5 via `semantic_text`) need ML, which is **not available on the free Basic license**, and they would compete with the 2 GB heap anyway. `dense_vector` and kNN work on Basic.

**License check (tested 2026-10-30 on a local ES 9.3.1 with a Basic license):**

| Feature | Basic |
|---|---|
| `dense_vector` + `bbq_hnsw` | ✅ |
| kNN search | ✅ |
| `rrf` / `linear` retrievers | ❌ license error |
| ML (`ml`, ELSER, e5, `semantic_text`) | ❌ |
| `icu_tokenizer` | ❌ plugin not bundled |
| `cjk` analyzer | ✅ |

The design uses only the ✅ rows.
- The embedder is pluggable (`EMBED_PROVIDER=local|openai|openrouter`), so a hosted model can be swapped in later. Changing the model means a new index version and a reindex, never mixed vectors.

**What gets embedded:**
- **Message windows, not single messages.** "ok", "lol" and "same" carry no meaning alone. A sliding window of about 6 consecutive messages from the same chat (or the same reply thread) becomes one doc, with sender names inlined.
- **Attachment and web-page text**, split into chunks of about 400 tokens with overlap.
- **Bot answers** are stored but tagged, so search can exclude them and the bot doesn't cite itself as a source.

**Index mapping (`sb-bot-chunks-v1`, alias `sb-bot-chunks`):**
- `chat_id`, `kind` (message_window, attachment, web_page, bot_answer), `user_ids[]`, `ts_start`/`ts_end`, `trip_id`, `source_url`/`file_id`, and `message_ids[]` pointing back to Postgres
- `text`, analysed for BM25 with the standard analyzer, plus a `text.cjk` subfield using the built-in `cjk` analyzer for Chinese
- `embedding`: `dense_vector`, 768 dimensions, `cosine`, `index_options: bbq_hnsw` (about 32× smaller than float). Rescoring with the original vectors keeps accuracy.

**Query flow for `read_chat_history(query, …)`:**
1. Embed the query with the model's query prefix (EmbeddingGemma uses separate query and document task prefixes, which fastembed's `query_embed` and `embed` apply).
2. Run **BM25 and kNN in parallel**, both filtered by `chat_id` (plus user, date or trip if given).
3. Merge the two lists with **reciprocal rank fusion in Python**: `score = Σ 1/(60 + rank)`, about 10 lines. This gives the same result as ES's `rrf` retriever, which needs a paid license.
4. Expand the top hits to the surrounding messages from Postgres and return them with timestamps and senders.

**Ways to play with it:**
- A `/search <text>` command shows the raw hybrid hits with BM25, vector and fused ranks side by side, for tuning.
- `scripts/eval_search.py` runs a small labelled set of query → expected-message pairs and compares BM25-only, vector-only and hybrid.
- Kibana is already on the box (`:5601`) for inspecting indices and running kNN queries by hand.
- The same embedder and ES setup power per-chat long-term memory (§3c).

**Costs and limits:** at friends-chat volume (say 100k messages in about 20k windows), 768-dim vectors with bbq take about 2 MB quantized (plus about 60 MB of float originals on disk for rescoring) and HNSW graph overhead, which is negligible for the 4 GB container. Indexing runs asynchronously after the Postgres write. If ES is down, search falls back to Postgres `ILIKE`.

## 3c. Chatbot experience and per-chat memory

**Goal:** it should feel like talking to one ongoing assistant in each chat, one that remembers what you discussed and what it learned about the group, without leaking between chats.

**Memory scope = one Telegram chat.** Each group has its own memory and each DM has its own. A group's forum topics each get their own conversation thread but share the group's long-term memory. Nothing crosses between chats: what you tell the bot in a DM never shows up in the group, and the reverse is also true.

**Three layers of memory:**

| Layer | What it is | Storage | How it reaches the model |
|---|---|---|---|
| **1. Conversation** | The bot's own back-and-forth in this chat: user turns, bot replies and tool results | Postgres `conversation_turns` | The last N turns verbatim, every time |
| **2. Rolling summary** | A compact summary of older turns, regenerated when the transcript passes about 30 turns | Postgres `chat_summaries` | Injected after the system prompt |
| **3. Long-term facts** | Durable facts about the group and its people, e.g. "Bob is vegetarian", "we prefer outdoor activities", "Alice prefers replies in Chinese" | Postgres `memories` (source of truth) + ES vectors | Top-k recalled by similarity to the current message, plus always-on pinned facts |

Ambient context stays separate from memory: the last ~20 messages in the chat that weren't to the bot give it the flow of the conversation. Older chat is reached through `read_chat_history` (§3b).

**How facts get saved:**
- **Explicitly:** "@example_bot remember that Bob is allergic to peanuts" → `remember(fact)` tool, and the bot confirms what it saved.
- **Implicitly, kept conservative:** after each turn, a cheap extraction pass proposes at most 1–2 durable facts. They are saved with `source=auto` and shown in `/memory`. Opinions, one-off chatter and anything sensitive (health, money details, passwords) are never auto-saved; those must be explicit.
- **Extraction reads only what humans wrote** in this chat, never tool output (pages, files, search results). That stops a web page from planting a "fact" that gets re-injected into every later prompt (§6a).
- Duplicates and contradictions: before saving, the bot searches similar memories. If it finds a near-duplicate, it updates that one instead of adding a new one ("Bob is now vegan" replaces "Bob is vegetarian").

**Commands (no LLM needed):**
- `/memory`: list what the bot remembers about this chat, numbered
- `/forget <n>` or "@example_bot forget that…": delete a memory (hard-deleted, not just flagged). Anyone in the group can do this.
- `/forgetme`: privacy deletion of **your own messages** and the memories about you in this chat (§6a). It is a different command from `/forget`.
- `/reset`: clear the conversation and summary layers. Long-term facts are kept.
- `/persona <text>`: per-chat custom instructions, e.g. "be sarcastic", "reply in Chinese", "keep answers short". `/persona` alone shows the current one. Personas are capped at 500 characters and placed **below** the system prompt's safety rules, so a persona can't switch off the untrusted-content or confirmation rules.

**Feeling like a chatbot:**
- **DMs:** every message gets a reply, with no @mention needed.
- **Groups:** every conversational response requires an explicit bot tag, including follow-ups and replies. Bot-addressed commands such as `/help@example_bot` also work; bare commands are ignored. Help and join notices show the required targeting.
- **Chat mode:** disabled in SBot. The generic framework still supports it for other apps, but SBot's routing gate overrides old stored chatmode settings and does not expose `/chatmode`.
- Typing indicator while working. Long answers stream by editing the message in place as tokens arrive, where the provider supports streaming.

**Schema additions:**
- `conversation_turns(id, chat_id, thread_id, role, content_json, tg_message_id, created_at)`
- `chat_summaries(chat_id, thread_id, summary, covers_until_turn_id, updated_at)`
- `memories(id, chat_id, subject_user_id?, text, source(explicit|auto), pinned, created_by, created_at, updated_at)`, indexed into ES `sb-bot-memories` with the same embedder. Deletes are hard deletes in both Postgres and ES
- `pending_actions(id, chat_id, requester_user_id, tool, payload_json, created_at, expires_at, used_at)`: what a Confirm button refers to (§6a)
- `chat_settings(chat_id, persona, chatmode, updated_by, updated_at)`

**Prompt assembly order**, chosen so the stable parts stay cacheable:
system prompt → chat persona → pinned memories → rolling summary → recalled memories → ambient recent messages → last N conversation turns → current message plus attachments.

**Reply attribution update (2026-10-31):** The implementation follows at most 32 explicit reply links in the same chat and topic, using stored rows for unmapped ancestors, to find a mapped conversation; it stops at chat/topic/reset/generation boundaries and never joins on a handle or name. Sender names, historical usernames, verified mention targets and reply IDs are snapshotted with messages and user turns; legacy rows keep null handles/empty mention data, and legacy turns without a snapshot are rendered as unknown. Summaries receive an instruction to preserve attributed nickname claims without treating them as verified identities. Hydrated `read_chat_history` results reach the active provider turn, while their persisted tool-result rows contain only `[Tool result omitted from persistent history.]`, including when a DM searches an authorized group; other tools and original user/assistant turns retain their existing persistence. `/forgetme` removes the user's own records and linked turns, then clears normalized mention metadata from surviving messages, turns and pending history buffers in that chat; original text by other people and data in other chats remain. This build requires additive core migration `0008_message_attribution.sql` after upstream `0007_display_time.sql`; no environment variables were added, and upstream timezone behavior is preserved.

## 4. Expense splitting (Splitwise-like)

**Plugin explainers (2026-11-04):** Each built-in capability plugin owns a packaged
`EXPLAIN.md`, separate from its short skill behavior instructions. Core's reserved explain
plugin provides `/explain [topic] [section]` and the read-only `read_explainer` tool, loading
only the app-selected plugins' resources plus the framework overview. Documentation and
fresh per-tool availability are separate evidence; handlers still enforce operational rights.
Retrieved document bodies are untrusted content and do not authorize example actions.
Apps may supply a reviewed public source revision; otherwise the document digest identifies
the bundled evidence. Wiki pages are generated from the same source. See the indexed
[implementation and delivery plan](docs/plans/2026-11-04-plugin-explainers.md).

**Data model (Postgres, amounts in integer minor units; JPY and KRW have 0 decimal places, SGD has 2):**
- `members(chat_id, user_id, username, display_name, last_seen)`: filled automatically from messages
- `expenses(id, chat_id, description, amount, currency, paid_by, created_by, created_at, deleted_at)`
- `expense_shares(expense_id, user_id, amount)`: always stored as exact amounts, whatever split was used
- `settlements(id, chat_id, from_user, to_user, amount, currency, created_at)`

**Behaviour:**
- Splits: equal, exact amounts, percentages, or shares. Rounding remainders go to the payer.
- Natural language goes through the LLM, e.g. "@example_bot I paid $120 for dinner, split with Alice and Bob". Names resolve against `members` by @username or display name. If a name is ambiguous, the bot asks instead of guessing.
- Expense adds, edits, deletes and restores **apply directly** (owner-approved exception to §6a, 2026-10-31). The bot then posts what it recorded, e.g. "Added: Dinner MYR 45.00, paid by Alice, split 3 ways (Example city trip). Say "undo" to revert." If the turn read untrusted content first, the same write goes through **Confirm / Cancel** instead.
- **Undo:** deletes are soft (`deleted_at`). `restore_expense` brings one back, and `undo_expense_change` reverts the requester's most recent expense change in that chat (repeated undos walk further back). It refuses if someone changed that expense since. `expense_events` keeps who added, edited, deleted or restored what, with before/after values, so edits can be reverted. Logs never carry descriptions or names.
- The bot asks instead of guessing only when it's genuinely unclear: an unknown person, a missing amount, which currency, which of several active trips, or who shares when the trip has no members.
- `get_balances` returns net balances plus a **simplified "who pays whom"** list (greedy min-cash-flow).

### Trips and currencies

Outside a trip, expenses go to the group's **general ledger** in **SGD** (`DEFAULT_CURRENCY=SGD`).

**Starting a trip**, e.g. "@example_bot we're going to Japan and Korea next week, settle in SGD":
- The bot calls `start_trip(name, countries, settle_mode, settle_currency="SGD")`.
- Countries map to currencies from a built-in ISO table (Japan → JPY, Korea → KRW). The bot shows the result with **Confirm / Cancel**.
- `settle_mode` is set per trip:
  - `convert`: everything is converted to `settle_currency` at settle-up, so each person gets one number.
  - `per_currency`: separate balances for each currency (e.g. JPY and KRW), with no conversion.
- A group can have **several planned and active trips** (statuses `planned`, `active`, `closed`; optional informational dates). One active trip per chat is the **default** (`trip_defaults`); new expenses with no trip named go to the default, else to the only active trip, else the general ledger. With several active trips and no default, the bot asks. Planned trips take expenses only when named. Starting or activating a trip never ends another.
- **Members:** a trip has a member list (`trip_members`), set at `start_trip` ("Example city trip with Alice, Bob and me") and changed later with `update_trip_members` (direct, echoed). `add_expense` with no `split_with` splits equally among all members, including the payer; an explicit `split_with` overrides. If the payer isn't a member or the trip has no members, the bot asks who shares.

**Currency for each expense:**
1. A currency stated in the message wins ("¥3000", "30k won", "SGD 40").
2. Otherwise, if the trip has a single currency, that one is used.
3. Otherwise, the bot asks. A bare amount in a multi-country trip is never guessed.

**FX (for `convert` trips):**
- The rate is captured **on the expense date** and stored on the expense, so balances don't drift later.
- Source: Frankfurter (ECB rates, no key). The fallback is open.er-api.com for currencies the ECB doesn't cover (e.g. TWD, VND).
- `set_fx_rate` lets the group override a rate, e.g. to match what a money changer actually gave.

**Editing a trip:** `update_trip` renames the active trip and adds or removes countries and
currencies. Adding a country adds its currency. A currency can't be removed while an expense or
settlement uses it, and the settle mode or currency can change only before anything is recorded.
The confirmation shows old → new, and the commit applies the change to the current row. In
convert trips `add_expense` fetches the rate automatically and shows it in the confirmation;
`set_fx_rate` is an optional override. An expense in a currency the trip doesn't list is still
allowed, with a note.

**Exporting:** `export_expenses` (or `/export [trip name | general]`) sends one CSV for one
scope: the active trip by default, a named trip, or the general ledger. It has one row per
expense and per share (with the FX rate and the exact converted amount), settlements, each
person's net balance computed by the same code as `get_balances`, suggested transfers, and
expenses with no FX rate. Soft-deleted expenses are left out. Cells use display names, never
user keys, and user text is escaped against spreadsheet formula injection. Platforms opt in
through the optional `FilePlatform` protocol (`chatcore/files.py`); Telegram sends a document.

**Ending a trip:** `end_trip` produces the final settle-up summary and closes the trip. Closed trips stay queryable ("what did we spend in Japan?").

**Trip files:** an existing trip can have one primary itinerary and multiple Drive references,
without changing its financial identity, lifecycle or default. The Google plugin owns local
`google_trip_files` metadata and format readers; app wiring injects the expenses plugin's
read-only explicit trip resolver. Attach/remove require requester confirmation. Confirmed
associations authorize later reads of exactly that file in that authorized trip/group, with
fresh membership, opt-out, owner, Drive access and association checks; stored content/links
never authorize more files. Removal revokes this authority without deleting the remote file.
Docs, Sheets, DOCX, selectable-text PDF and UTF-8 CSV have bounded readers; other files are
references only. Existing Sheet links are backfilled as references and retain per-currency
planned-versus-actual comparisons. Old trip IDs, members and ledger data remain unchanged.
Design and execution: [trip Drive files](docs/plans/2026-11-02-trip-drive-files.md).

**Confirmed Google edits:** the Google plugin owns native content edits and action-scoped
destination/result primitives. Native Docs/Sheets use effective original write access under
the full API grants; Drive native copies require per-file authorization. Qualified DOCX edits
import the original package into a new native Doc. All copy/conversion/content writes require
the exact requester preview, and never change a trip association implicitly. Docs use explicit
tab-aware UTF-16 body ranges, suggestions-inline snapshots and revision conditions. Sheets
use typed bounded target preconditions and literal value writes; a collaborator can race
between the final read and write. Result IDs and uncertain/partial outcomes are durable and
cannot authorize replay; unresolved retention includes the browser's `shared` phase, while
privacy purge cascades immediately. Copy qualification remains disabled by default.
See [API editing](docs/plans/2026-11-02-google-api-editing.md) and the
[operator guide](docs/google-workspace.md#confirmed-content-edits). Public framework delivery
does not update the separately pinned downstream application or authorize live Google writes.

**Native Doc output authority:** newly created Docs, including private outputs, are journaled with
the owner identity, chat/topic and creation mode. A complete retained binding allows later native
Doc reads/edits only in that same chat/topic after owner and Google permission checks; legacy,
purged, ambiguous or mismatched output records do not authorize access. Sharing choices are frozen
as private, anyone/reader or anyone/writer with discovery disabled, and are applied only after
bounded content read-back. The framework default is private. External source ACLs and the separate
Sheet and browser authority paths are unchanged. See [output authority and access](docs/plans/2026-11-04-google-output-followups-and-access.md).

The optional browser trial is a separate explicit backend, never an automatic API fallback. It
supports exact text replacement and bold formatting only, always on a new native Doc copy. Its
preview includes the editor account and configured chat members who receive writer access; the
broker shares only that copy and requires API read-back before reporting a verified result. A
dedicated editor-account session stays outside the owner OAuth profile. The worker is a separate
service behind the shared guarded proxy, with a fixed private protocol and no API credentials.
`GOOGLE_BROWSER_ENABLED=false` by default, and copy qualification is a separate prerequisite.
The browser implementation is shipped inactive; release-pin integration, live qualification and
deployment remain pending. Synthetic tests do not establish
live Google UI or session reliability. See the [browser trial plan](docs/plans/2026-11-02-google-browser-editing.md)
and [operator guide](docs/google-browser-editing.md).

**Schema additions:**
- `trips(id, chat_id, name, countries, currencies, settle_mode, settle_currency, status, start_date, end_date, started_at, ended_at)`, `trip_defaults(chat_key, trip_id)`, `trip_members(trip_id, user_key, added_by, added_at)`
- `expense_events(id, chat_key, expense_id, actor_key, action, before, after, undo_of, undone_at, created_at)`: the audit and undo log
- `expenses.trip_id` (null means the general ledger) and `expenses.fx_rate_to_settle`

## 5. Stock-investor integration

**MVP: read-only public API.** Base: `https://api.stockinvesttoday.com/api`

| Tool | Endpoint |
|---|---|
| `stock_analysis(ticker, lang)` | `GET /analysis/:ticker?lang=en` |
| `stock_digest(lang)` | `GET /digests/latest?lang=en` |
| `stock_prices(ticker, days)` | `GET /stocks/:ticker/prices?days=N` |
| `stock_list(sector?, recommendation?)` | `GET /analysis?...` |

This needs no secrets and no changes to stock-investor, and it is safe for a group chat because there is no private portfolio data. Responses are trimmed before they go to the LLM. The bot states that this is stored analysis, not a live quote.

**Later, optional: talk to the portfolio chat agent.** This would route a question into a separately authenticated portfolio conversation service, so group members could ask about their linked portfolios. It needs per-user auth in the Go backend and touches private data, so it is a separate piece of work in stock-investor. **Not in the MVP.**

## 6. Deployment

`apps/_scaffold` provides a runnable container and generic compose/deploy skeleton.
Platform CI builds the example and exercises it outside the checkout using installed
wheels. Downstream applications own deployment, CI, encrypted secrets, shared operator
scripts, backups and rollback in the private deployment repository. A platform merge
validates framework compatibility; a downstream pin update selects what is deployed.

Production instructions and the original topology are preserved in the
private architecture archive (`clawdot-deployments/docs/archives/platform-ac5a4b0/ARCHITECTURE.md`). The owner selected a clean public history; earlier application and operator history remains private.

## 6a. Security

A basic baseline for a friends' bot on a home server, not an enterprise program. Each item is cheap to do from day one and painful to retrofit.

### Secrets

| Secret | Lives in | Scope / notes | Rotate by |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | box `.env` | Anyone with it can read the group's updates | BotFather `/revoke` |
| `LLM_API_KEY` (OpenCode) | box `.env` | A separate key per application, independently revocable | OpenCode dashboard |
| `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` (optional) | box `.env` | Only if that provider is enabled; set a **monthly spend limit** in the provider console | Provider console |
| `TAVILY_API_KEY` | box `.env` | Own key, with a monthly credit cap | Tavily dashboard |
| Google OAuth client/refresh token profile (optional) | mode-600 file in the private host Google directory, mounted read-only at `/run/google-oauth` | One owner; human-posted file provenance (reads may verify a retained human message in the same chat/topic) or confirmed trip-file authority; existing write permission/confirmation rules apply; no automatic sharing | Owner reauthorizes with `chatcore-google-auth`; revoke the old Google grant |
| `PG_DSN` password | box `.env` | Role `<bot>` only | `ALTER ROLE <bot> PASSWORD …` |
| `ES_API_KEY` | box `.env` | `<bot>-*` only, created with `expiration: 365d` | Mint new, then invalidate old |
| `VALKEY_PASSWORD` | box `.env` | Sidecar only | Edit `.env`, restart |
| `HF_TOKEN` (only if the model is gated) | one-shot `models` job, passed at run time | Read-only token, **not** in the bot's `.env` | HF settings |

- **`.env` only on the box**, mode 600. The repo has `.env.example` with names only.
- **`.gitignore`** from the first commit: `.env`, `.env.*` (except `.env.example`), `*.key`, `*.pem`, `secrets/`.
- **Pre-commit `gitleaks`** hook, plus GitHub secret scanning and push protection if the repo is on GitHub.
- **Never logged:** `config.py` uses pydantic `SecretStr` for every secret, so they don't show up in reprs or tracebacks. `httpx` and `telegram` loggers stay at `WARNING`, because at `INFO`, httpx logs Telegram request URLs, which **contain the bot token**. A logging filter also redacts anything matching known secret patterns.
- **Rotate** right away if a secret might have leaked, when someone with box access no longer needs it, and otherwise once a year (the ES key expiry forces this).

### Least privilege

| What | Rule |
|---|---|
| **Postgres** | Role `<bot>`: `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE`, owns only database `<bot>`. `pg_hba.conf`: `host <bot> <bot> <app-network subnet> scram-sha-256`, so the role can't log in to other DBs or from elsewhere |
| **Elasticsearch** | Runtime API key: no cluster privileges; on indices `<bot>-*` it gets `read`, `write`, `create_index`, `view_index_metadata`, `manage` (for alias swaps on reindex). Index templates are created once with admin credentials, which are never in `.env` |
| **Valkey** | Own sidecar (§3a), `<bot>:` prefix, TTL on every key, `maxmemory` capped |
| **Telegram chats** | `ALLOWED_CHAT_IDS` only; the bot leaves any other group it's added to. DMs only from current members (§3) |
| **Admin commands** | `/reindex`, `/search`, `/usage`, `/purgechat` only work for user IDs in `ADMIN_USER_IDS`. Everyone else gets no reply |
| **Container** | Not in the `docker` group, no Docker socket, no host mounts except the read-only model volume |

### LLM-specific

**Prompt injection.** Anything that isn't the current human's own message is **data, not instructions**: web pages, search results, attachments, chat history, recalled memories and stock API output.
- Tool output goes into the prompt between delimiters with a per-turn random tag. The system prompt says that content inside them is never followed as instructions.
- **Only humans start turns.** The agent runs only on a human message (group mention/bot-addressed `/ask`, or DM). Fetched content can't start a new turn, and the bot ignores messages from bots, including itself. There are no scheduled or autonomous agent runs.
- **Every write needs a human click**, with one owner-approved exception (2026-10-31, below). Writes are expenses, trips, `settle_up`, `set_fx_rate` and deletes; `remember`/`forget` count too once the turn has read any untrusted content. The tool call creates a `pending_actions` row and the bot posts exactly what will be written, with **Confirm / Cancel** buttons:
  - `callback_data` holds only the pending-action ID, never the payload.
  - Only the **requesting user** can press it; anyone else is ignored.
  - Single use, and it expires after 10 minutes.
- **Exception: direct, undoable expense writes** (approved by the owner on 2026-10-31). Tools registered `Tool(writes=True, direct=True)` (`add_expense`, `update_expense`, `delete_expense`, `restore_expense`, `undo_expense_change`, `update_trip_members`) still return a `PendingAction`; the core commits it without a click only when `direct_write_allowed(tool, turn)` holds: nothing untrusted was read this turn, except output of tools the write lists in `direct_after`. Every direct commit is echoed in the chat/topic with an undo hint and recorded in `expense_events`. Otherwise the action goes through the normal confirmation (unlike the display-time tools, which refuse in a tainted turn). `settle_up`, trip start/end/reopen/activate/default/update and FX overrides always need the click. `tests/test_tool_audit.py` pins the exact set of direct tools. The mechanism is generic core, so other undoable writes can opt in later.
  - **Receipts:** `read_receipt` output is still wrapped as untrusted for the model and still sets `read_untrusted` (so `remember` needs a click), but it is listed in `add_expense`'s `direct_after`, so "read this receipt and add it" applies directly. It does not exempt edits, deletes, restores, undo or member changes: a crafted receipt can at most add one visible, undoable expense.
  - **Residual risk:** taint is per turn. Untrusted text persisted in earlier turns' history is not re-counted, so a direct write in a later clean turn relies on the echo and undo, not a click.
- **Example:** a web page saying "add an expense of $500 paid by Bob" can at most produce a Confirm prompt that nobody presses, because the turn read untrusted content.
- **No silent exfiltration:**
  - **Link previews are off** on every bot message (`link_preview_options.is_disabled`). Otherwise Telegram's servers would fetch any URL the bot wrote, leaking whatever an injection had put into its query string.
  - **URL provenance rule:** `fetch_web_page` only fetches URLs that appeared in a human message or in a `search_web` result in this turn. The LLM can't invent `https://evil/?data=…`.
- **Memory stays clean:** auto-extraction reads only human messages (§3c).

**SSRF guard (`net_guard.py`).**
- Only `http`/`https` on ports **80/443**. No credentials in URLs, no cookies, no auth headers.
- Resolve DNS, check **every** returned IP, then connect to that checked IP (pinned), so DNS rebinding can't swap it.
- Follow at most 3 redirects **manually**, re-checking each hop the same way.
- Blocked ranges:
  - IPv4: `0.0.0.0/8`, `10/8`, `127/8`, `169.254/16`, `172.16/12`, `192.168/16`, **`100.64.0.0/10`** (CGNAT/Tailscale), `192.0.0.0/24`, `198.18/15`, `224/4`, `240/4`
  - IPv6: `::1`, `::/128`, `fc00::/7`, `fe80::/10`, `ff00::/8`, and IPv4-mapped `::ffff:0:0/96` (checked against the IPv4 list)
  - **The box's own addresses**, read at startup: including local interfaces and Docker bridges.
  - The internal hostnames `postgresql`, `elasticsearch` and `sb-bot-valkey`.
- Unit tests cover each range, a redirect to `127.0.0.1`, a DNS answer mixing public and private IPs, and decimal or octal IP forms.

**Size and time limits.**

| Input | Limit |
|---|---|
| Fetched page | 10 s total, 5 MB downloaded (streamed, aborted past the cap), `text/*`, HTML, PDF and JSON only; 50k characters to the LLM |
| Attachment | 20 MB (Telegram's bot download limit), PDFs up to 50 pages, Pillow `MAX_IMAGE_PIXELS` guard, 30 s parse timeout |
| Search | 5 results, snippets trimmed |
| Agent turn | At most 8 tool steps, 3 fetches, 60 s wall clock, capped output tokens |

**Cost caps and rate limits** (counted in Valkey, with in-process fallback):
- Per user: 20 bot requests an hour. Per chat: 150 a day.
- Daily token budget across all chats. When it's hit, the bot says so and stops calling the LLM until midnight SGT. `/usage` (admin, formerly `/budget`) shows usage; owners can change the runtime cap with confirmed `/admin usage` commands (formerly `/admin budget`), and Postgres mirrors today's usage counter.
- Framework `/chatmode` classifier calls count against the same budget; SBot disables this feature.
- Provider-side backstops: spend limits on OpenRouter and Anthropic keys; OpenCode Go and Tavily are plan-capped.

### Container hardening

All three services:
- Non-root user (`sbbot` 10001; valkey runs as its image's `valkey` user).
- `read_only: true`, with a `tmpfs` at `/tmp` (64 MB) for the heartbeat file and temp files.
- `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`.
- **No published ports.** Long polling means nothing listens.
- Limits: `sb-bot` gets `mem_limit: 4g`, `cpus: 4`, `pids_limit: 256`; Valkey gets `mem_limit: 128m`.
- Images pinned by **digest**. Rebuilt monthly to pick up base-image patches.
- Docker log rotation: `json-file`, `max-size: 10m`, `max-file: 3`.

### Supply chain

- **`uv.lock`** is committed (it records hashes) and builds use `uv sync --frozen`, so nothing resolves at build time.
- **Embedding model pinned** to a Hugging Face commit SHA, with the SHA-256 checked, downloaded once and loaded offline (§3b). ONNX only, never `trust_remote_code`.
- **Dependabot** (pip/uv and Docker ecosystems) if the repo is on GitHub. Otherwise a monthly `uv lock --upgrade` plus a test run.
- **`pip-audit`** on the exported lockfile, in CI or as a monthly manual check.
- GitHub Actions, if used, are pinned by commit SHA.

### Data and privacy

The bot records **every message** in allowlisted groups (privacy mode is off), and sends recent ones to a third-party LLM provider when it is asked something. Everyone in the group should know that.

- **Disclosure:**
  - Agree it with the group before adding the bot.
  - When the bot joins, it posts and pins a short notice: *"This bot records messages in this group to answer questions about past chat. Messages are kept 12 months. When someone asks the bot something, recent messages are sent to the AI provider (OpenCode). `/privacy` for details, `/forgetme` to delete yours."*
  - `/privacy` shows the same text plus which provider is active.
- **Retention: 12 months.**
  - A nightly job hard-deletes messages, conversation turns, summaries and ES chunks older than that, plus cached page and attachment text.
  - Kept: the ledger (expenses, shares, settlements, trips), because balances depend on it, and memories until someone `/forget`s them.
  - Token-usage logs are kept 12 months.
- **`/forgetme`** (any member, in the group or in DM):
  - Hard-deletes that user's messages in that chat from Postgres and ES, and memories with them as `subject_user_id`.
  - Ledger rows stay so balances still add up; the bot says so.
- **`/purgechat`** (admin) deletes everything for a chat, e.g. when the bot is retired from a group.
- **No secrets or PII in logs.** Logs carry IDs, tool names, token counts, latency, error class and URL *hostnames* only. No message text, prompts, tool payloads or full URLs.
- **Tutor pronunciation (optional and disabled by default):** when explicitly requested, the tutor sends only the target phrase to ElevenLabs for speech generation, receives an MP3 in memory, and uploads it as a Telegram voice message before its text reply. The pronunciation annotation stays local and is placed in the caption. The framework keeps no audio cache; failed or successful generation attempts consume the per-turn and process-local quota reservation. Provider requests are not made at startup.
  - ElevenLabs retains submitted data by default; model-training use depends on the account's terms and settings, with an opt-out for future submissions. Zero Retention Mode is an eligible Enterprise feature, not a standard default. Do not promise an ordinary-plan retention period when none is specified. Before activation, the operator must record the current training opt-out and retention arrangement, establish a hard credit/overage cap where supported, and pass the live language, voice and slow-audio listening qualification in `docs/plans/2026-11-04-tutor-pronunciation-tts.md`.
  - Slow speech uses the FFmpeg system binary (`ffmpeg`) with MP3 support and the `atempo` filter. Downstream images must install it before enabling slow requests. Normal requests do not run FFmpeg. No native reply-chain attribution is guaranteed for the voice attachment.
- **DM and group isolation:**
  - `chat_id` is a **required** argument on every store and search function, not an optional filter.
  - Every ES query includes a `chat_id` filter.
  - Tests try to read group A's history and memories from chat B, and from a DM by a non-member.
- **Backups** are aged out after 14 days, so deleted data doesn't linger longer than that.

### Host exposure findings

Host-specific findings and remediation records are retained in the
private architecture archive (`clawdot-deployments/docs/archives/platform-ac5a4b0/ARCHITECTURE.md`). Downstream operators must
review firewall policy, published ports, SSH access and sibling-service isolation for
their own hosts. Framework SSRF checks do not replace host firewall policy.

### Deploy access, backups and rollback

Downstream deployments own access control, encrypted database backups, restore drills,
release pins and rollback. Keep recovery identities separate from runtime identities,
verify the intended backup filesystem before writing, and test restoration independently.
Schema migrations remain additive; an applied migration never changes. The deployment
repository records the production schedule, retention and cutover evidence.

## 7. Build order

1. **Scaffold**: `uv` workspace with the `packages/` and `apps/` layout (§3), the §3d interfaces (events, `ChatPlatform`, `Plugin`, store interfaces), the console adapter, `.gitignore` and gitleaks pre-commit **before the first commit**, config with `SecretStr`, logging redaction, Postgres migrations, ES and Valkey clients. Local dev uses a `compose.dev.yaml` with throwaway Postgres, ES and Valkey containers. Echo bot that logs messages and responds to mentions, with the chat allowlist, auto-leave and admin-ID check.
2. **LLM core**: neutral types, OpenAI-compatible provider (OpenCode), agent tool loop with step and time caps, untrusted-content wrapping, system prompt, the conversation layer and rolling summary, DM auto-reply (current-member check) and reply-to-continue, `/reset` and `/persona`, rate limits and the token budget, link previews off. Then the Anthropic provider.
3. **Web tools**: `search_web` (Tavily), `fetch_web_page` with the URL provenance rule and the SSRF guard (`net_guard.py`, test-first).
4. **Read tools**: `read_attachment` (PDF, text, images). Then `read_chat_history`, first on BM25, then the embedder, `dense_vector` index, hybrid RRF, `/search` debug command and `eval_search.py`.
4b. **Long-term memory**: `remember`/`recall`/`forget`, `/memory`, auto-extraction, dedupe. This reuses the embedder and ES from step 4. Then `/chatmode`.
5. **Expenses**: ledger, split math, debt simplification, confirmation buttons (`pending_actions`, requester-only, expiry), `/balance`. Then trips, currency detection, FX capture and the two settle modes.
6. **Stocks**: public-API tools.
6b. **Privacy**: retention job, `/forgetme`, `/purgechat`, `/privacy`, join notice.
7. **Deploy**: hardened compose (§6a) plus deploy script; Postgres role and `pg_hba`, scoped ES key, nightly `pg_dump` with one test restore; first deploy to the downstream host; smoke test in a test group, then the intended group after the members have agreed (§6a disclosure).

### 7a. Parallel build layout

Steps 2–6 can be built side by side once the scaffold has defined the shared contracts.

**Phase 0, serial (main session):** step 1, plus the contracts every track builds against:
- **`git init`** on `main`, with `.gitignore` and gitleaks in place before the first commit. Each parallel track runs in its own worktree and branch.
- **Workspace skeleton:** every package from §3 exists with an empty `pyproject.toml`, so tracks only fill in their own package.
- **Framework interfaces (§3d)** in `packages/core`: `events.py`, `platform.py` (`ChatPlatform`, `Capabilities`), `plugin.py` (`Plugin`, `Command`), and the `store` interfaces. These are the contracts every track codes against.
- **Tool interface:** `chatcore.tools.registry` holds a `Tool` (name, description, JSON schema, `async handler(ctx, **args) -> ToolResult`). `ToolContext` carries the namespaced `chat`, `thread_id`, the requesting `user`, an `is_dm` flag, the platform `capabilities`, and the store clients.
- **Neutral LLM types** in `chatcore.llm.types`: `Message`, `ToolCall`, `ToolResult`, with content parts for text and images.
- **Pending-write hand-off:** a write tool never commits directly. It returns `ToolResult(pending_action=PendingAction(...))`. The agent loop asks the adapter to `confirm(...)` (buttons, or the reply-code fallback) and ends the turn. `chatcore.confirm` runs the action's `commit()` when the requester confirms. This boundary between tracks A and D is defined here, so neither track invents its own.
- **Plugin registration:** each plugin implements the `Plugin` protocol (tools, commands, settings class, migrations, hooks). The downstream app lists its plugins explicitly, so tracks never edit a shared handler file.
- **Per-package migrations:** each package ships its own `migrations/`, applied in the order core → plugins, and recorded per package (`schema_migrations(package, version)`). Tracks can't collide on numbering.
- **Console adapter:** done in Phase 0, so every track can run end-to-end locally without Telegram.
- **Test fixtures:** shared pytest fixtures for a throwaway Postgres, ES and Valkey (from `compose.dev.yaml`), a **fake `ChatPlatform`** that records sends and edits and can simulate button presses, and a scripted fake LLM provider. These replace the fake Telegram update builder.

**Phase 1, parallel (one Codex run and one worktree branch per track; see §7b):**

| Track | Plan steps | Owns | Done when |
|---|---|---|---|
| **A. Core runtime** | 2 | `packages/core`: `llm/`, `agent.py` (an LLM `Responder` replacing `EchoResponder`), `conversation.py`, `/reset` `/persona`, the chatmode classifier, rate limits and budget, the Valkey `Cache` backend | Multi-turn chat works through the console adapter with the fake provider and the real OpenCode one; a write tool's pending action reaches `app.confirm.request` |
| **B. Web + stocks** | 3 + 6 | `packages/plugins/web`, `packages/plugins/stocks`, `core/security/net_guard.py` | SSRF test suite passes; tools pass recorded-fixture tests |
| **C. Search + read** | 4 | `packages/plugins/search`, `core/embed.py`, ES `SearchIndex` backend, `/search`, `eval_search.py` | Hybrid search returns the expected hits on the eval set; ES-down falls back to Postgres |
| **D. Expenses** | 5 | `packages/plugins/expenses` | Split, settle and FX unit tests pass; confirm flow works via the Phase 0 contract |
| **E. Telegram adapter** | — | `packages/adapters/telegram` | Passes the adapter conformance tests; a test bot in a private group sends, edits, confirms, downloads files and checks membership |

Track E is small and independent of A–D: it implements `ChatPlatform` and nothing else.

Rules for the tracks:
- A track edits only its own package(s) plus their migrations and tests.
- Nothing outside `packages/adapters/telegram` may import `telegram`; a lint rule (`ruff` banned-api) enforces this.
- A change needed to a Phase 0 contract goes back to the main session, never into a track branch.
- Each track merges into `main` on its own once its tests pass.

**Phase 2, serial (main session):**
1. Merge the branches in order E → A → B → C → D, rerunning the full test suite after each.
2. End-to-end check on the console adapter, then against a private Telegram test bot.
3. **4b, long-term memory** as `packages/plugins/memory` (needs A's conversation layer and C's embedder), then `/chatmode`.
4. **6b, privacy**: retention, `/forgetme`, `/purgechat` across every track's tables.
5. **7, deploy**: needs your box access and the test group.

**Expected merge friction:** A↔D, around pending actions, is the main risk, and the Phase 0 contract limits it. A↔E meet only at the `ChatPlatform` interface. B and C touch nothing shared besides the registry.

### 7b. Execution: Claude plans, Codex executes

This describes the owner's computer, where Codex CLI is installed. Sessions without Codex (for
example cloud sessions) follow `AGENTS.md` "Execution and review": the session's own model
executes and reviews, and the PR says so.

Tracks are executed by **Codex CLI** (`codex-cli` 0.166.0). Models: execute with **`gpt-6-luna`**, review with **`gpt-6.1-sol`**; Claude does the final review and commits. (Between 2026-10-30 and §16, some runs also executed with `gpt-6.1-sol`.) Claude works on two levels: one coordinator session, and one track session per track.

| Who | Does |
|---|---|
| **Coordinator** (the main Claude session) | Phase 0 (scaffold, contracts, `git init`); writes one execution plan per track; starts one Claude track session per track; owns the Phase 0 contracts; merges, integration-tests and runs Phase 2 |
| **Track session** (one Claude session per track, in its own worktree) | Runs Codex headless on its track plan, reviews the result, sends fix rounds back to Codex, runs unit tests, and reports to the coordinator |
| **Codex** (execute `gpt-6-luna`, review `gpt-6.1-sol`) | Executes the track plan inside the track session's worktree: writes the code and unit tests, runs them, and writes a report |

You watch and steer each track session from the Claude sidebar. No terminal tabs are needed.

**Per-track execution plans:** `docs/plans/track-{a,b,c,d,e}.md`, written by Claude after Phase 0 lands. Each plan is self-contained, since Codex doesn't see this conversation. It covers:
- the goal, and the files the track owns (and may not leave);
- the exact Phase 0 interfaces to use: `Tool`, `ToolContext`, `PendingAction`, `register()`, the settings class, and its migration range;
- the module-by-module implementation steps;
- the required tests (including that track's §6a security tests) and the command to run them;
- the "done when" checklist from §7a;
- hard rules:
  - Don't edit Phase 0 contracts; stop and report if one needs changing.
  - Don't add dependencies without listing them in the report.
  - No secrets in code or tests.
  - Commit to the track branch only.
- the report format: a summary, files changed, tests run with results, deviations from the plan, and open issues.

**Worktrees:** each track session is started by the coordinator with its own worktree and branch (`track/{a,b,c,d,e}`), cut from `main` after Phase 0.

**Launch:** the coordinator starts the five track sessions in parallel. Each track session runs Codex headless, as a background command, in its own worktree:

```bash
mkdir -p reports logs && cat docs/plans/COMMON.md docs/plans/track-a.md > reports/track-a.prompt.md
codex exec -m gpt-6-luna -C . \
  -s workspace-write -c sandbox_workspace_write.network_access=true \
  --json -o reports/track-a.md < reports/track-a.prompt.md > logs/track-a.jsonl
```

- **Codex never commits.** Its sandbox can't write the worktree's `.git`, which lives in the main repo. The track session reviews and commits.
- Codex sets `UV_CACHE_DIR=$PWD/.uv-cache` itself (`docs/plans/COMMON.md`), because the sandbox can't write `~/.cache`.

**Permissions:** `.claude/settings.json` is committed, so every worktree inherits its rules.
- Allow `codex exec *`, so starting and resuming Codex needs no approval.
- Deny the sandbox escapes: `--dangerously-bypass-approvals-and-sandbox`, `--dangerously-bypass-hook-trust`, `danger-full-access` and `--yolo`. These are refused even if approved; tested 2026-10-30.
- The terminal-tab tool is deliberately **not** auto-allowed. Deny rules only cover Bash, so auto-allowing tabs would let a sandbox escape run without a prompt.

**Sandbox and safety:**
- Always `workspace-write`, never `--dangerously-bypass-approvals-and-sandbox`. Codex can write only inside its own worktree.
- Network access is on so `uv` can install packages. No secrets exist in the worktrees for Codex to leak; `.env` lives only on the box.
- No Docker inside the sandbox, so Codex runs **unit tests with fakes** (fake LLM provider, fake Telegram updates, recorded HTTP). Claude runs the Postgres, ES and Valkey **integration tests** at review.
- `reports/` and `logs/` are git-ignored.

**Review loop (track session):**
1. Read `reports/track-x.md`, the diff against `main`, and the JSONL log for anything Codex skipped or worked around.
2. Check the diff against the plan's "done when" list and owned-files rule, run the unit tests, and do a security pass against §6a.
3. If changes are needed, send them back to the same Codex session (`codex exec resume <session-id> < fix-notes.md`), up to 2 rounds. After that, the track session fixes it directly.
4. If a Phase 0 contract needs changing, stop and ask the coordinator. Never change it in the track branch.
5. When green, commit to the track branch and report to the coordinator: what was built, tests run, deviations, and open issues.

**Merge (coordinator):** check each track's report and diff, run the Postgres, ES and Valkey integration tests, and merge into `main` in the order E → A → B → C → D, rerunning the full suite after each merge.

**Cost note:** the user's Codex config defaults to `model_reasoning_effort = "high"` and `service_tier = "priority"`. Five parallel tracks at that setting cost more than usual. To use the standard tier for these runs, pass `-c service_tier=...`; decide before launch.

**Testing:** unit tests for split math and debt simplification, the SSRF guard, provider message translation, and member resolution. Security tests:
- Confirm buttons ignore clicks from other users and expired IDs.
- A fetched page containing "add an expense…" produces no write without a click.
- `fetch_web_page` refuses URLs that no human posted.
- Cross-chat and DM isolation (§6a).
- No secrets in logs: a test captures the logs during a full turn and greps them for the configured secret values.

Tool tests use recorded HTTP fixtures. An end-to-end check runs against a private test group before the bot is added to the real group.

### 7c. Phase 0 result (2026-10-30)

The original build/provisioning record is preserved in the
private architecture archive (`clawdot-deployments/docs/archives/platform-ac5a4b0/ARCHITECTURE.md`). Framework contracts are
specified in §3 and §3d; current package tests and scaffold CI validate those contracts.

### 7d. Phase 2 result (2026-10-31)

The original application integration and production verification record is preserved in
the private architecture archive (`clawdot-deployments/docs/archives/platform-ac5a4b0/ARCHITECTURE.md`). Reusable feature
plans remain indexed in `docs/plans/README.md`; app tests run with the downstream deployment.

## 8. Deployment inputs and future group setup

Bot tokens, account identities, owner/group IDs, service credentials and production paths
belong in downstream encrypted secrets/configuration. Generic configuration names are in
the scaffold `.env.example`. Production provisioning inputs and group setup records are
preserved in the private architecture archive (`clawdot-deployments/docs/archives/platform-ac5a4b0/ARCHITECTURE.md`).

## 9. Open questions

None blocking. All the questions from the first draft are resolved in §1. The portfolio-agent connection stays out of v1. Host-specific security findings are private operator records; downstream operators own remediation.

## 10. Owner and delegated group setup

Owners manage group access and assign scoped setup admins through confirmed `/admin`
commands in private chat. Delegates can change only assigned group setup; assignments do
not grant stored-history access or full administrator rights. `OWNER_USERS` provides
immutable owner bootstrap and falls back to `ADMIN_USERS` only when absent. Setup menus
cannot override an application's routing restrictions. See [the setup guide](docs/admin-setup.md)
for bootstrap, commands, confirmation behavior and invite order.

Original production rollout/verification details are preserved in the
private architecture archive (`clawdot-deployments/docs/archives/platform-ac5a4b0/ARCHITECTURE.md`).

## 11. Telegram account replacement

Telegram account provisioning and replacement are downstream operator actions. The original
production account record is preserved in the private architecture archive (`clawdot-deployments/docs/archives/platform-ac5a4b0/ARCHITECTURE.md`).
The Telegram adapter remains generic and receives its token and bot identity from app settings.

## 12. Backlog

Requests and their detail. The status of every plan, built or not, is tracked in
[`docs/plans/README.md`](docs/plans/README.md); keep both in step.

- [x] **Per-user bot access restrictions and rate limits** (requested 2026-10-31): built on
  branch `claude/access-restrictions-reset-89e6cb` (commit `0a9ea85`); see §16. Not yet deployed.
- [x] **Real context reset** (`/reset` reset point, Telegram command menu) and manual
  **`/compact`**: built on the same branch (commit `73b4cad`); see §16. Not yet deployed.
- [x] **Edit the active trip** (`update_trip`, requested 2026-10-31): add or remove countries and
  currencies (e.g. Hong Kong/HKD on a Example trip), rename, change the settle mode or currency
  only before any expense is recorded. The bot also stops claiming that FX rates must be set
  by hand. Design `docs/superpowers/specs/2026-10-31-update-trip-design.md`, plan
  `docs/plans/2026-10-31-update-trip.md`. Built and merged 2026-10-31; see §4
  "Editing a trip". Pending `add_expense`, `settle_up` and `set_fx_rate` confirmations now
  refuse to commit if the trip's settle settings changed after they were proposed.
- [x] **CSV export of the expense ledger** (requested 2026-10-31): `export_expenses` and
  `/export` send one trip's ledger (or the general ledger) as a CSV, so a person can check the
  balances by hand and confirm no expense crossed between trips. Design
  `docs/superpowers/specs/2026-10-31-expense-export-design.md`, plan
  `docs/plans/2026-10-31-expense-export.md`. Built and merged
  2026-10-31; see §4 "Exporting". Not yet deployed.
- [ ] **Interactive browsing: filling forms, clicking through searches, booking** (requested
  2026-10-31): let the bot operate JavaScript search and booking forms, for example rental-car
  or travel availability and prices. Deliberately excluded from the first rendered-page work,
  which is read-only. Needs a sandboxed browser behind the network guard, per-site adapters
  rather than a general form-driving agent, and the pending-action confirmation flow for any
  submit or reservation. Not started.
- [ ] **Bot detection and CAPTCHAs** (requested 2026-10-31): do not bypass them. When a site
  blocks a rendered fetch, the bot says so plainly and points the user to the page or a
  prefilled link instead of retrying or evading. Record the plain-blocked-message behaviour as
  part of the rendered-fetch work; any further handling stays here until decided.
- [x] **Smoother group conversations, phases 1–2** (requested 2026-10-31): replies to the bot
  answer without a tag, a bare tag uses your previous message, unknown addressed commands point
  to `/help@bot`, and the prompt carries one time-ordered transcript. Built on branch
  `claude/group-convo-flow-0ec35b`; see §17. Not yet deployed.
- [x] **Group conversations keyed on reply chains** (phase 3 of the same request, final design
  approved 2026-10-31): a reply to any bot message reopens that message's conversation with its
  own summary and turns; a tag within an hour continues your conversation; `/new`; compaction per
  conversation. Built on branch `claude/group-convo-flow-0ec35b`; see §18. Not yet deployed. The
  earlier draft (untagged classifier, idle close, `/done`) was dropped.
- [ ] **Poll OpenCode for the real remaining balance** (requested 2026-10-31): the daily
  `DAILY_TOKEN_BUDGET` counts tokens locally, so it can't see the actual credit left on the
  configured provider account. Periodically query the provider for the
  account's remaining balance or usage (find out which endpoint exists, and its auth and rate
  limits), store the reading, show it in the admin budget view, and DM the owner when it drops
  below a threshold. Read-only: never top up or change billing. Log no key material.
  Account usage, spend limits and production budgets belong to downstream configuration. Not started.
- [ ] **Bot release history and "what changed?"** (requested 2026-11-01). Deployment release
  notes use reviewed PR titles. The deployer and CI never commit or force-push note updates
  to `main`; that would create a commit → CI → deploy loop and conflict with parallel PRs.
  The platform separately maintains a curated [changelog](CHANGELOG.md) through reviewed
  PRs (approved 2026-11-03); contributor and release rules are in `AGENTS.md`.
  - Keep every release note the deploy writes (today only `release-notes/latest.json` survives)
    as a bounded history on the box, e.g. under `deploy-metadata/`, with retention.
  - Let the owner (and maybe allowed groups) ask the bot "what changed recently?" from that
    history. Read-only, and no GitHub write access.
  - Manual `deploy.sh` deploys and rollbacks send no changelog today, so their DMs list no
    commits. Have `deploy.sh` build the same list locally (`git log --first-parent
    <running>..HEAD`) and pass it as `SB_DEPLOY_CHANGELOG_FILE`.
  - Optional later: a separate post-deploy workflow that creates a GitHub Release with
    generated notes. It needs Contents write in Actions, so weigh that against the collaborator
    plan before adding it. Never touch `main`.

  Not started.
- [x] **Share public source and learn links in allowed chats** (requested 2026-11-01;
  delivered downstream on 2026-11-03). SBot supplies the same owner-maintained public
  project information to its legacy and skills-on system contexts. Source/contribution
  questions point to the [public framework repository](https://github.com/keenlok/clawdot-platform);
  architecture and storage questions point to the
  [wiki visual explainer](https://github.com/keenlok/clawdot-platform/wiki/Storage-architecture)
  and [detailed storage guide](docs/storage-architecture.md).
  Reading or cloning the public framework needs no invitation; contributors can use
  normal GitHub issues and pull requests. Private bot configuration/deployment is
  separate and is not a contribution target. No GitHub write token, invitation tool
  or extra general tool is needed for these fixed facts. Existing group allowlists
  and DM membership policy still govern access. App implementation and rollout
  evidence are maintained in the owner's deployment records.

- [x] **Temporary per-chat model switch via `/model`** (requested 2026-11-01; shape agreed
  2026-11-01; plan `docs/plans/model-switch.md`): anyone in an allowlisted chat can switch that
  chat's model for 30 minutes, then it reverts automatically. Built 2026-11-01 on branch
  `claude/models-opencode-backlog-8fs7p8` in a cloud Claude session (no Codex there; see
  `AGENTS.md` "Execution and review"). **Off until deployed with choices:** run the plan's
  model smoke test per model, set `LLM_MODEL_CHOICES` in the box `.env`, recreate the bot
  service. No migration. Rollback: empty `LLM_MODEL_CHOICES`. Not yet deployed.
  - **Only the `/model` command switches.** The LLM gets no tool for it and can't change the
    model from natural language; when asked, it points people to `/model`. A crafted message,
    page or receipt can at most make the bot mention the command.
  - `/model` shows the current model, the allowed list and any revert time; `/model <name>`
    switches for 30 minutes; `/model default` reverts now. Groups use `/model@bot`.
  - **Anyone in an allowlisted chat can switch** (decided 2026-11-01). Guards: the reply names
    who switched, which model and when it reverts; a per-chat cooldown between switches;
    anyone can run `/model default`; re-switching doesn't extend an override and a chat gets
    at most 60 override minutes per rolling 3 hours. `LLM_MODEL_SWITCH=admins` limits
    switching to group admins and setup managers.
  - Allowed models are a **per-app setting** (`LLM_MODEL_CHOICES`), never free text, so each
    bot app offers its own list. Only tool-capable models that pass a live smoke test (tool
    call, `use_skill`, `tool_choice="none"` final answer, no tool-syntax leak, image unless
    marked `:text`) go on it, and until the balance poll exists only models priced near the
    default: the token budget doesn't see per-model prices. The override lives in Valkey with
    a TTL; a restart falls back to the default.
  - A text-only choice runs the turn with vision off, which also drops `read_attachment`
    images that arrive mid-turn. Resolved once in `respond()`, so usage rows, the metrics line
    and `_BudgetedProvider` follow the override; the chatmode classifier stays on the default.
  - The `x-opencode-session` header and `read_receipt`'s `VISION_*` provider are unchanged; a
    switch costs one cold prompt cache each way.
  - Later: pairs with the OpenCode balance poll above to refuse or shorten switches to pricier
    models when the balance is low.
  - Unblocked: skill-layer stages 2–3 merged in PR #22; `/model` joins a stage-4 `/help` group
    and the menu limit guard (PR #29). Open decision: which OpenCode Go
    models go on sb_bot's list.
- [x] **Working generic app scaffold**: `apps/_scaffold` runs without credentials or backing
  services, packages a generic persona, and has wiring tests, a pinned container and a
  compose/deploy skeleton checked in platform CI. New private apps copy it to `bots/<bot>`
  in `clawdot-deployments`, choose plugins/composites and explicit platform release pins,
  and provision their own scoped secrets and infrastructure. See the scaffold README.
  Production provisioning and group activation remain downstream operator actions.

## 13. Web tools enabled

Web tools are provided by the web plugin and registered by an application's plugin selection.
API credentials and feature activation belong to downstream configuration. The original
production activation record is preserved in the private architecture archive (`clawdot-deployments/docs/archives/platform-ac5a4b0/ARCHITECTURE.md`).

## 14. Final answers at the tool budget boundary

The reported web-search failure was traced to successful searches followed by repeated fetch
failures and further research calls. The agent exhausted eight tool executions, offered a ninth
call, and replaced the gathered evidence with a generic step-limit error.

Release `32504ecfd180` reserves the existing final completion round for an answer once the tool
budget is used. That request offers no tools and instructs the model to use collected evidence,
cite existing URLs, explain gaps and avoid invented facts or unconfirmed writes. The eight-call
cap, nine-round maximum, 60-second timeout, permission checks and confirmation early exit are
preserved. No conversation history was deleted.

Verification: 654 unit tests passed (one ShellCheck skip), 49 integration tests passed (three
unavailable local ES checks skipped), Ruff and commit checks passed, and independent review
found no blockers. The regression reproduced the exact old error before the fix. The regression covers replay of collected read results into the final completion without
sending a chat message or writing production data. Original live rollout and provider
configuration evidence belongs in the private deployment record (`clawdot-deployments/docs/deployments/2026-10-31-tool-budget-answer.md`).

## 15. Research agent (built, not deployed)

Built on `claude/research-agent-8eeb2a` (2026-10-31) to address §14's root cause: large
questions exhausting the main agent's eight-call budget. Plan: `docs/plans/research-agent.md`;
user/operator doc: [docs/research.md](docs/research.md). Codex `gpt-6-luna` implemented it,
`gpt-6.1-sol` reviewed three times, and Claude completed the final fixes after two fix rounds.

**What it does.** A new `chatcore-research` plugin runs a separate read-only research sub-agent
over the web plugin's own `search_web`/`fetch_web_page` (same `net_guard`, no new fetching code):
- **Inline:** `/research@<bot> <topic>`, `@<bot> /research <topic>`, `/research <topic>` in DMs,
  or the main agent's `research` tool. Up to 6 searches / 8 fetches, 90 s cap (30 s for the
  tool, which runs inside the main agent's 60 s turn). Typing indicator plus an edited
  "Researching… (searching 3/6, reading 2/8)" status message where edits are supported.
- **Background:** `/research --long <topic>`, or an inline run hitting its time cap, becomes a
  `research_jobs` row (the plugin's own migration; `chatcore-jobs` is reminder/watch-specific).
  A lease-fenced worker runs it (12 searches / 20 fetches / 600 s per attempt, at most 2 attempts),
  survives restarts, and posts to the original chat and thread. `/research status` and
  `/research cancel` act only on the caller's jobs in the current chat.
- **Report:** findings with `[n]` citations, a "Gaps and uncertainty:" section, then numbered
  sources. The system renders the source links from URLs that search actually returned or
  that a human posted. Any URL the model types is replaced with `[link removed]`, so an injected
  page can't get an exfiltration link posted.

**Security (§6a).** Each run gets a fresh `TurnState`, so URL provenance is per run. All web
content is wrapped in per-run untrusted tags with closing tags stripped, and page text is capped
at 12k characters. Only the two web tools are offered: no writes and no recursion. Logs carry
hostnames, counts and error class names only. Access is re-checked at worker claim and before
every delivery. `/forgetme` and `/purgechat` purge job rows and block a running inline run's
background handoff. Unknown sends are never replayed.

**Cost defaults.**
- Caps: 5 research starts per person per day, 20 per chat per day, and one active research per
  person across inline and background runs and across chats.
- Token ceilings: 150k per inline run and 400k per background job.
- Global budget: research tokens go through `Limits.record`. While the daily budget is
  exhausted, the worker claims nothing, and running research stops before its next completion.

**Seam for per-user restrictions (§12).** `ResearchAccess.can_research(chat, user)` is wired as
`RestrictionResearchAccess` in `build_app` (§16): blocked or paused people cannot start research,
and a queued job is refused when it is re-checked at start.

**Verification.**
- Tests: 1138 passed, 11 skipped (extension sandbox/PG env-gated tests and ShellCheck, none
  research). Research's own 68 pass, including Postgres restart-survival and concurrent-claim
  tests; integration-marked: 75 passed. Ruff is clean.
- End-to-end: `tests/test_research_e2e.py` builds the real console app with recorded Tavily and
  page fixtures and checks inline (with a page fetch), `--long`, status and cancel.
- Not done: no live Tavily or LLM calls, and nothing on the deployment host.

**Follow-ups.**
- Switch the mention path to core's `routing.addressed_command()` when access-and-reset lands
  (it currently wraps the responder).
- ~~Wire `research_access` to the per-user restriction store.~~ Done in §16.
- A process that dies mid-job waits out the 660 s lease before the job is retried.

## 16. Access restrictions, context reset and /compact

Built 2026-10-31 on branch `claude/access-restrictions-reset-89e6cb` by a track session: Claude
planned (`docs/plans/access-restrictions.md`, `docs/plans/context-reset.md`), Codex `gpt-6-luna` executed,
Codex `gpt-6.1-sol` reviewed (two rounds per feature, all confirmed findings fixed), and Claude
did the final review, console end-to-end runs and commits. Not deployed.

**Per-user access restrictions** (`0a9ea85`):
- Owner DM commands `/admin block|pause|unblock|limit|mod|unmod|moderators|restrictions`;
  every change is confirmed, persisted in the §10 store (migration 0004, additive) and audited.
- Moderation is explicit and group-scoped; setup access does not grant it. Moderators cannot
  target owners, full admins, other moderators of that group or themselves. Owners are immune.
- Omitting the group makes a restriction global for that platform (`<platform>:*`, used only via
  `global_scope()`; `*` is rejected at the adapter, event and purge layers). Group restrictions
  apply only in that group. Pauses are 1m–30d; per-user hourly limits 1–10000, owner only.
- Enforced in `BotApp` before any LLM cost, for messages, button presses and reply codes; at
  most one notice per chat and person every 10 minutes. `/privacy`, `/forgetme` and the
  `privacy.forgetme` confirmation stay usable in DM. Restricted users' group messages are still
  logged. Pending confirmations from a since-restricted user are refused.

**Context reset and /compact** (`73b4cad`):
- `/reset` records a per-chat/thread reset point (migration 0005, additive) instead of deleting
  turns. Turns, summaries and ambient messages before it are ignored; the message log and
  long-term memories are kept, and the reply points to `/memory`, `/forget` and `/forgetme`.
  Retention, `/forgetme` and `purge_*` still delete turns and reset rows. A reset generation
  invalidates a summary from a `/compact` that raced the reset.
- `/compact` summarises everything since the reset point, merged with the previous summary,
  keeps the last four turns and moves the ambient cutoff to the moment it read the chat. It uses
  no tools, counts against the request and token limits, has a five-minute per-chat cooldown,
  and reports approximate size before and after.
- Permission for both (user decision): DM owner of the chat, or in groups the owner and that
  group's moderators. A denied user gets one notice per 10 minutes.
- Mention-only groups reach both via `/reset@bot` or `@bot /reset`; `@otherbot /reset` and bare
  commands are ignored. The Telegram command menu is registered at startup (best effort);
  `/admin` is left out of both menus and stays listed in `/help`.
- Ambient context cap (reviewed per coordinator request): 1500 characters per message and 6000
  in total, newest kept.

**Contract extensions approved by the coordinator** (2026-10-31): restriction exemptions and
gating of button presses/reply codes; the `<platform>:*` global-scope sentinel with rejection at
the adapter/event layer; the stable `Limits(..., user_limit=)` seam; bot-verified
`addressed_command`; turns kept on reset with retention and purges unchanged; adapter-only
`set_menu_commands` called from app wiring; core migrations 0004 and 0005, additive.

**Research gate** (`49a1df5`, after rebasing onto the research merge `9ee738b`): research uses
`RestrictionResearchAccess` over the same restriction store, so blocked or paused people cannot
start `/research` and queued jobs are refused at start. Native poll votes are not gated (no LLM
cost).

**Verification** (rebased on `9ee738b`): 1122 unit tests passed, 5 skipped; the one failure is
`test_real_core_button_confirmation_preserves_topic`, which also fails on clean main. 86
integration tests passed against the compose.dev services (Postgres, Valkey, Elasticsearch).
Ruff clean. Console end to
end on dev Postgres covered both features including persistence across a restart.

**Open issues:** the research mention path still wraps the responder instead of using
`routing.addressed_command()` (§15 follow-up); jobs already scheduled by a since-blocked user still run; jobs permission
parsing of `Scope.chat_key` has no sentinel check (scopes are not user input); an unclosed
aiohttp session warning in integration tests; if retention removes a reset row while a
`/compact` is in flight, a reused generation number could keep that compaction's summary
(year-old reset rows only).

**Deploy needs:** migrations 0004 and 0005 (additive; take the deploy script's pre-migration
backup); no new environment variables; the Telegram menu registers on startup.

## 17. Smoother group conversations (phases 1–2)

Built 2026-10-31 on branch `claude/group-convo-flow-0ec35b`: Claude planned
(`docs/plans/group-conversation-flow.md`), Codex `gpt-6-luna` executed, Codex `gpt-6.1-sol` reviewed
(three review rounds, all findings confirmed and fixed), and Claude did the final
review and the console end-to-end run. Not deployed. Driven by production logs from a forum topic:
an ignored reply to the bot, a bare `@bot` answered with "Sorry, I couldn't produce a response.",
and silence on `@bot /models`.

**Routing (phase 1):**
- In mention-only groups a Telegram reply to any bot message now routes to the responder. This
  reverses that part of `docs/plans/group-mention-only.md` at the user's request. Untagged chatter,
  untagged replies to humans and bare `/cmd` (also as a reply to the bot) stay ignored, at zero
  LLM cost.
- A bare `@bot` sent as a reply answers the replied-to message: a person's message is carried in
  the wrapped "Reply from" block (the author's name never enters the unwrapped request), and a
  bot message is continued with the wrapped "Replying to your earlier message" block. A reply
  target missing from the log gets "What can I help with?" without an LLM call.
- A bare `@bot` that isn't a reply uses the same person's most recent untagged message in the
  same chat and topic from the last 5 minutes. If there is none, the bot asks "What can I help
  with?" without an LLM call. The agent has the same empty-request guard.
- An explicitly addressed unknown command (`@bot /x`, `/x@bot`, or `/x` in a DM) gets "I don't
  know /x. See /help@bot …", at most once a minute per chat and person. Admin-only commands run
  by non-admins get the same text, so their names aren't revealed. Restricted users still get
  the restriction notice.

**Context (phase 2):**
- Ambient messages and stored turns are merged into one attributed, time-ordered transcript per
  chat and topic. The bot's own lines are marked `(you)`, and each message appears once: a turn
  is dropped when its log row is present. The reset point, the 1500/6000-character caps (now
  also covering bot lines) and untrusted wrapping still apply. Old turns are no longer replayed
  with the assistant role.
- A reply to a bot message adds that message as a wrapped, capped "Replying to your earlier
  message" block, even after `/reset` or outside the recent windows.

**Verification:** 1471 unit tests passed and 5 were skipped. The one failure is the known
`test_real_core_button_confirmation_preserves_topic`, which also fails on main. 90 integration
tests passed and 18 were skipped, against the compose.dev services; this needed a fresh
database, because the shared dev Postgres had another branch's edited `0005` applied. Ruff is
clean. The console end-to-end run in echo mode covered tag, reply, bare tag, unknown command
and its cooldown, and ignored chatter and bare commands.

**Deploy needs:** none beyond the code; no migrations or new environment variables.


## 18. General image recognition with the main model

Built 2026-10-31 on branch `claude/image-vision-ec7639`: Claude planned
(`docs/superpowers/specs/2026-10-31-image-vision-design.md`,
`docs/plans/2026-10-31-image-vision.md`), Codex `gpt-6-luna` executed, Codex
`gpt-6.1-sol` reviewed (one P2 fixed in one round, then clean), and Claude did the final review.
Not deployed.

**Finding:** a configured OpenAI-compatible vision model accepts images. OpenCode's
public model metadata lists JPEG, PNG, GIF and WebP input. The receipt-vision plan's
"text-only" assumption was wrong. So general image questions use the existing `LLM_VISION=true`
path: no new tool and no separate provider. `read_receipt` and `VISION_*` are unchanged, and
receipts still go through `read_receipt` for its amount, currency and date checks.

**Changes:**
- The system prompt covers describing images, reading and translating text in them, image text
  as data, no face identification, and the current-or-replied-to scope.
- With vision on, images that fail to load (HEIC, GIF, over 5 MB, download error) get a wrapped
  "could not be loaded" note instead of "You cannot see them", and a partial load reports
  "N of M".
- `read_attachment` on an image explains the scope instead of claiming vision.
- `/privacy` names the main provider for shared images when vision is on.

**Limits unchanged:** at most 4 images per turn, 5 MB each, JPEG, PNG or WebP. Image tokens count
toward `DAILY_TOKEN_BUDGET`, and the per-turn rate limits apply. Images are never stored. In
groups an image is read only when its caption tags the bot, or when a reply to it tags the bot or
replies to the bot.

**Verification:** 1481 unit tests passed and 1 was skipped. The one failure is the known
`test_real_core_button_confirmation_preserves_topic`. 90 integration tests passed and 18 were
skipped (Playwright) on a fresh scratch database. Ruff is clean. No live image call was made to
the real provider.

**Deploy needs:** `LLM_VISION=true` in the box `.env`, then recreate the bot service. No
migration and no image rebuild beyond the code. Afterwards, send one photo in the private test
chat. Rollback: `LLM_VISION=false`.

## 19. Group conversations keyed on reply chains (phase 3)

Built 2026-10-31 on branch `claude/group-convo-flow-0ec35b`, after phases 1–2 were merged
(`1c20145`) and deployed. Claude planned it (the final design is in
`docs/plans/group-conversation-flow.md` Phase 3), Codex `gpt-6-luna` executed it, and Codex
`gpt-6.1-sol` reviewed it over three rounds; every finding was confirmed and fixed. Claude did the
final review and a console end-to-end run on dev Postgres. Not deployed. There is no untagged
classifier: attachment is deterministic and untagged chatter still costs zero LLM calls.

**Model** (core migration 0006, additive):
- New tables: `conversations` (chat, topic, participants, reset generation, last active),
  `conversation_messages` (message id → conversation, with the logged primary id) and
  `conversation_summaries`.
- `conversation_turns.conversation_id` links each turn to its conversation.
- Every bot message sent during a turn is mapped to the conversation. That covers every part of
  a split Telegram answer, through the adapter-only `TelegramPlatform.sent_message_ids`.
- The `ChatPlatform`, `Responder`, events and plugin contracts are unchanged. The current
  conversation reaches the agent through a context variable.

**Attaching a message** (groups only; DMs unchanged):
- A reply to a mapped message reopens its conversation, however old, unless it predates the
  topic's `/reset`. A reply to an unmapped or pre-reset bot message starts a new conversation,
  which still sees the replied message through the phase 2 reply block.
- A tag with no usable reply target continues the tagger's latest conversation in the topic if
  it was active within `CONVERSATION_CONTINUE_MINUTES` (default 60); otherwise it starts a new
  one.
- `/new@bot` makes your next message start a new conversation; `@bot /new <text>` answers
  `<text>` in a new one.

**Context and compaction:**
- Each turn sees its conversation's own summary and turns, inside the phase 2 topic transcript.
- Compaction is automatic past 30 unsummarised turns or 16,000 characters.
- Reopening a conversation that has been quiet for over an hour compacts it first. This keeps 4
  turns, respects the daily budget and a per-conversation cooldown, and runs inside the turn lock
  already held.
- `/compact@bot` compacts the current conversation; the owner and moderators keep the
  permission.
- `/reset@bot` bumps the topic generation, so every conversation in the topic becomes
  unreachable and its summaries are ignored.
- Retention keeps a reset row while conversations created after it remain.

**Privacy:** `/forgetme` removes the user's mappings, their participant entries and the chat's
conversation summaries. `purge_chat` and retention delete conversations together with their
mappings, summaries and turns. Every query filters by chat, and tests prove chat B can't reach
chat A's conversations by id or message id.

**Option:** `BotApp(conversation_threads=ThreadSettings(...))`, default off. With it off,
behaviour is unchanged, which the existing suite confirms. sb_bot turns it on.

**Verification:**
- Unit: 1489 passed, 5 skipped. The one failure is the known
  `test_real_core_button_confirmation_preserves_topic`, which also fails on main.
- Integration: 93 passed, 18 skipped, against the compose.dev services. This includes migration
  0006 on real Postgres and two consecutive agent turns in one conversation.
- Ruff is clean.
- Console end to end on dev Postgres: parallel conversations, reopening an old answer by reply,
  `/new` in both forms, ignored chatter, and a reply across `/reset` starting a new conversation.
  The run checked the mapping rows.

**Deploy needs:** migration 0006 (additive; take the deploy script's pre-migration backup) and
the optional env `CONVERSATION_CONTINUE_MINUTES` (default 60, listed in `.env.example`).
Conversations start empty after deploy: older bot messages aren't mapped, so replying to one
starts a new conversation with that message as context.
