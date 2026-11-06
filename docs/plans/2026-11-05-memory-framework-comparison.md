# Memory framework comparison and recommendations

**Status:** Record; research completed 2026-11-05 after the [initial improvement plan](2026-11-05-memory-improvements.md) was written. The plan's research-driven decisions section records the resulting changes. No framework was installed, benchmark executed, or runtime code changed.

**Method:** Compare our checked-out implementation with current first-party documentation and source repositories. Separate framework primitives, optional packages, managed-service features and legacy APIs. The comparisons below are engineering assessments, not measured rankings or proof that any system is more accurate on our chats.

## 1. Where our system currently lands

SBot has a recognizable layered memory design: a recent transcript, conversation-specific summaries, chat-wide durable facts, pinned facts, and hybrid retrieval of both facts and chat history. Postgres owns facts and original messages; Elasticsearch is a search projection. A new conversation in the same chat can reuse durable facts, while another chat has a separate memory boundary.

That is a useful application-specific memory system, beyond a transcript-only chatbot. Its main gaps concern how facts are maintained: identity-aware deduplication, evidence links, safe correction, time validity, reliable extraction effects and evaluation. Adopting a framework does not by itself resolve those product rules.

Local evidence checked for this assessment:

| Capability or gap | Implementation evidence |
| --- | --- |
| Pinned facts and relevant recall on every turn | [`MemoryPlugin.before_turn`](../../packages/plugins/memory/src/chatcore_memory/plugin.py) |
| Hybrid fact recall, authoritative database hydration, text fallback | `_recall_rows` in the same module |
| Hybrid raw-chat retrieval with chat/user/date filters | [`SearchPlugin._search_chat`](../../packages/plugins/search/src/chatcore_search/plugin.py) |
| Similarity-only duplicate selection and text replacement | `_similar` / `_save` in the memory plugin |
| Creator and optional subject, without source-message links | [`repository.py`](../../packages/plugins/memory/src/chatcore_memory/repository.py), [`0001_memories.sql`](../../packages/plugins/memory/src/chatcore_memory/migrations/0001_memories.sql) |
| Single-human-message extraction, defensive parsing and sensitivity filtering | `MemoryPlugin.after_turn` |
| Privacy purges and conversation summaries | [`privacy.py`](../../packages/core/src/chatcore/privacy.py), [`conversation.py`](../../packages/core/src/chatcore/conversation.py), `ARCHITECTURE.md` §3c/§6a |

## 2. Comparison with other approaches

| Framework/product | How it handles memory | Where ours lands | Useful input |
| --- | --- | --- | --- |
| LangGraph | Thread state uses checkpoints; long-term documents use separately scoped stores. LangMem is an optional memory-processing package. | Similar separation of conversation state and persistent facts; we have narrower, product-specific lifecycle rules. | Make chat, conversation, subject and extraction-policy scope explicit. Keep workflow recovery separate from factual memory. [LangGraph memory overview](https://docs.langchain.com/oss/python/concepts/memory) |
| LangMem | Supports tool-driven and background memory management; its executor can delay processing and cancel redundant pending work per thread. | We have explicit tools and after-turn extraction, but no durable processing queue or batching. | Evaluate bounded batching after conversational activity settles. [Delayed processing](https://langchain-ai.github.io/langmem/guides/delayed_processing/) |
| LlamaIndex | Combines token-limited recent history with static, fact-extraction and vector memory blocks, using priority-based truncation. | We already have pinned/recalled facts and summaries, but memory injection uses item/length limits rather than a separate aggregate budget. | Add a deterministic memory-context budget and priorities; measure tokens as well. [Memory guide](https://developers.llamaindex.ai/python/framework/module_guides/deploying/agents/memory/) |
| Letta | Current SDK memory uses a git-backed MemFS: `system/` content is always in context; other files are loaded on demand. Dreaming can run background consolidation. Legacy V1 uses memory blocks and archival search. | Our pinned/retrieved split serves a comparable context-selection purpose, but our records are facts in a database rather than an agent-owned file repository. | Keep compact always-visible context distinct from on-demand evidence; protect owner-approved facts from automatic rewrites. [Current SDK memory](https://docs.letta.com/agent-sdk/memory), [legacy blocks](https://docs.letta.com/v1-sdk/memory/memory-blocks) |
| Mem0 | Current docs describe additive extraction, contextual fact lookup and scoped retrieval. Managed Platform also supplies additional consolidation behavior. | Similar write-then-search flow; our similarity-based text replacement is more destructive than an additive ingestion policy. | Stop treating a similarity match as permission to overwrite. [How it works](https://docs.mem0.ai/core-concepts/how-it-works) |
| Zep / Graphiti | Episodes support derived facts, source provenance and temporal validity. Graphiti is a self-operated graph framework; Zep is a broader managed context service. | We already store raw messages and searchable facts separately, but cannot follow a fact back to its evidence or represent changing truth explicitly. | Adopt source relations and validity metadata before considering a graph backend. [Source traceability](https://help.getzep.com/v4/source-traceability), [facts](https://help.getzep.com/v4/facts), [Graphiti repository](https://github.com/getzep/graphiti) |

### LangGraph / LangMem: borrow scope and scheduling, retain application policy

LangGraph provides persistence and store building blocks; it does not prescribe our identity resolution or privacy contract. Its documentation discusses both profile documents and collections of facts, and notes that collections shift difficulty into updates and retrieval. Our collection-of-facts design is reasonable for independently erasable group memories. The improvement is safer maintenance, not turning the whole chat into one continually rewritten profile. [Memory overview](https://docs.langchain.com/oss/python/concepts/memory)

LangMem's debounce example helps explain why isolated fragments are a weak extraction unit. Its sample is not a guarantee of restart-safe job processing; the docs explicitly distinguish local threads from a remote executor for serverless lifetimes. Any queue we build still needs durable leases, idempotent effects and erasure fencing. Use same-conversation human messages only, despite examples that also process assistant output. [Delayed processing](https://langchain-ai.github.io/langmem/guides/delayed_processing/)

### LlamaIndex: borrow context accounting

Its memory blocks are a useful model for prioritizing scarce prompt space. We can implement a small aggregate character ceiling now using existing dependencies, preserve fact boundaries, prioritize pinned items, and observe actual provider token usage. A character ceiling is not an exact token limit, particularly for Chinese/Japanese. Retrieval improvements must be evaluated under the same context budget as the baseline. [Memory guide](https://developers.llamaindex.ai/python/framework/module_guides/deploying/agents/memory/)

### Letta: distinguish current design from historical MemGPT patterns

Do not describe today's Letta solely as “core memory plus archival vector memory”: that is still documented under the legacy V1 SDK, while the current SDK emphasizes MemFS. The useful shared idea is a small always-visible portion and a larger on-demand portion. SBot does not need git-backed personal-memory files, background self-editing instructions or shared cross-chat repositories to gain that benefit. [Current SDK](https://docs.letta.com/agent-sdk/memory), [legacy archival memory](https://docs.letta.com/v1-sdk/memory/archival-memory)

The legacy block API explicitly warns that direct whole-value replacements are last-write-wins. Treat this as a reason to require optimistic versions for corrections, not as evidence that mutable blocks automatically solve concurrency. A version history is also not an erasure guarantee. [Memory blocks](https://docs.letta.com/v1-sdk/memory/memory-blocks)

### Mem0: separate ingestion, consolidation and read policy

The current add API describes additive ingestion, optional expiry and contextual interpretation using earlier Platform messages. This supports moving beyond isolated-turn extraction, while preserving attribution and explicit product scope. Hosted features must not be assumed to exist in OSS. [Add memory](https://docs.mem0.ai/core-concepts/memory-operations/add)

Platform Dream separately handles superseding and merging. Its docs say superseded facts can still appear in ordinary reads unless `latest_only=true` is requested; merged records have different defaults. For SBot, define “current facts only” at our authoritative read boundary, with history an explicit request. Avoid copying any API's defaults without specifying their user-visible meaning. [Dream](https://docs.mem0.ai/platform/features/dream)

### Zep / Graphiti: provenance and time are valuable without adopting a graph

Zep explicitly distinguishes source traceability from truth: a source association tells us where a claim came from, not whether it is correct. Our evidence interface should likewise say “Alice stated…” and show missing sources honestly. Do not copy its suggestion to retain source content for audits into an unlimited second archive; our existing retention and erasure policy still applies. [Source traceability](https://help.getzep.com/v4/source-traceability)

Its fact model separates world-validity times from database record/invalidation times. SBot can represent those dimensions in relational columns and exclude expired/superseded facts from ordinary recall. Graph traversal becomes worthwhile only if relationship-heavy questions remain a measured weakness afterward. [Facts](https://help.getzep.com/v4/facts)

## 3. What changes in our plan after research

1. **Move prompt-space accounting into phase 1.** Add an aggregate memory budget alongside existing item and fact-length bounds. Keep evidence opt-in and separately bounded.
2. **Make automatic ingestion non-destructive.** Similarity retrieves candidates; equivalence, subject and explicit/auto policy govern whether a candidate can be reused. Differing facts do not overwrite each other automatically.
3. **Introduce bounded conversational interpretation before durable workers.** An optional phase-2 trial uses recent human messages in the same conversation and explicit reply context. Keep every supporting message reference, prevent historical-only re-extraction and use a baseline comparison to decide whether to enable it.
4. **Make lifecycle states explicit.** Active, superseded and expired have distinct meanings. Normal recall excludes the latter two; explicit erasure remains hard deletion, including any retained historical fact.
5. **Evaluate abstention and multi-session behavior.** Measure correct “I don't have enough evidence” responses alongside recall. Do not optimize only how many facts are stored.
6. **Keep framework adoption out of the first release.** None of the reviewed documents establishes that replacing our storage solves multi-person identity or in-flight privacy deletion. Their primitives inform the implementation; our application retains responsibility.

## 4. Evaluation and honest comparison

The original LongMemEval suite covers extraction, cross-session reasoning, updates, temporal reasoning and abstention. Its repository now also points to a V2 suite, so record the exact suite/revision rather than mixing results. These are useful dimensions for our synthetic cases. LoCoMo provides another long-conversation QA/RAG reference. Neither replaces group-chat identity, erasure and authorization tests. [LongMemEval](https://github.com/xiaowu0162/LongMemEval), [LoCoMo](https://github.com/snap-research/locomo)

For an eventual head-to-head trial, use the same synthetic histories, answering model, fact/evidence context budget and question set. Record extraction model, prompts, retrieval depth, persistence mode, total write/read tokens and p50/p95 latency. Compare our baseline, safer writes alone, source evidence, and bounded-context extraction as separate variants. A framework adapter is optional research work requiring a separate approved slice; use isolated local stores or explicitly approved synthetic hosted uploads.

Do not publish a numeric framework ranking from this research. We have not run the same workload across these systems. Vendor benchmark numbers depend on task mix, model, retrieval depth and context budget; they cannot be transferred to SBot's small group-chat workload.

## 5. Recommendation

Keep our existing storage and plugin architecture. Deliver evaluation, identity-safe/non-destructive writes, bounded context, evidence and erasure protection first. Trial multi-message human extraction before building a queue. Add explicit correction/time semantics next; add durable workers and a search outbox only if the operational evidence warrants them.

This positions SBot as a deliberately scoped memory application using common modern memory patterns, with stronger provenance and lifecycle handling as the next engineering milestone. It does not claim parity with an orchestration runtime, managed memory service or temporal graph system.
