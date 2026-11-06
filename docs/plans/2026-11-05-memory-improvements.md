# Memory reliability and evidence implementation plan

> **For agentic workers:** Use the executing-plans skill after owner approval. Execution and review follow `AGENTS.md` and `ARCHITECTURE.md` §7b. Implement one approved slice at a time.

**Goal:** Make per-chat memories attributable when written and when read, recoverable from supporting messages, safe to correct and erase, and measurable across new conversations.

**Status:** Slices 0–2a implemented on branch `codex/memory-reliability`, not yet merged or released; revised 2026-11-06 after review. The owner approved implementation of slices 0–2a on 2026-11-06 with the D2 and D3 defaults. Slices 2b, 2c, 3 and 4 remain unapproved. Measures that need a live model run stay unmeasured; [the baseline](2026-11-05-memory-baseline.md) lists them.

**Architecture:** Keep Postgres authoritative, Elasticsearch rebuildable, conversation state separate from durable facts, and every operation scoped to one chat. Improve the existing memory plugin incrementally rather than adopting a new memory backend. Evidence is read from the existing message log through core interfaces.

**Tech stack:** Existing Python, asyncpg-style database abstraction, Elasticsearch hybrid search, pytest and Ruff. No new dependency or service is proposed for the first release.

**Planning roles:** Codex drafted and self-reviewed the first version on 2026-11-05 under the unavailable-model fallback in `AGENTS.md`, because Claude's CLI session had expired. A Claude session reviewed it against the code on 2026-11-06 and rewrote it as this single spec. Implementation requires the repository's pre-code approval and execution/review workflow.

## 1. Decisions for the owner

The review proposed these defaults and the plan is written to them. Changing one changes the named section.

| # | Decision | Default in this plan | Affects |
| --- | --- | --- | --- |
| D1 | Which repository owns the change? | **`clawdot-platform`**, resolved 2026-11-06. Downstream apps pick the change up through a pinned platform release. This repository's Ruff command includes `.github/scripts`, and implementation PRs add `CHANGELOG.md` entries. | §4, §9 |
| D2 | Does `/forgetme` delete a shared explicit fact? | Delete when the person is the subject, contributed context to a composite, or is the sole supporter. If another author independently stated the same whole fact, remove only the person's links and keep the fact. | §7.3 |
| D3 | When do explicit corrections ship? | A minimal atomic replacement ships in the first release, in phase 1. Validity intervals and superseded history stay in phase 3. | §5 phase 1, phase 3 |

## 2. Current behavior and gaps

Code-observed; none of this is a measured production incident.

**Write path**

- `_similar` compares text within a chat, without subject matching. `_save` overwrites a similar row's text, while repository updates preserve its original subject, creator and source. Rows already overwritten this way carry the wrong attribution today.
- The `memories` schema has creator and optional subject, but no supporting message IDs, version or validity interval.
- Automatic facts get a subject only when `about_speaker` is true. Group facts and facts about third parties have a null subject.
- `after_turn` produces at most two filtered facts and logs failures. There is no durable extraction job or retry record.

**Read path**

- `after_turn` sends the model `Message.user(text)` with no speaker identity.
- `before_turn`, `_recall` and `/memory` render `#id text` with no subject, author or date. The model cannot tell whose fact it is or which of two conflicting facts is newer.

**Extraction input**

- `text` is not always the current message body. `_bare_tag_request` substitutes the same user's earlier message, and a bare-tag reply substitutes a synthetic prompt ("Please respond to the message I'm replying to…"), which is then sent to extraction.

**Erasure**

- `/forgetme` deletes by subject or automatic creator. A fact Alice stated about Bob has a null subject and Alice as creator, so Bob's `/forgetme` misses it, although `/privacy` promises to delete "memories about you".
- `purge_user`, `purge_chat` and `_delete_memory` log and swallow Elasticsearch failures, so the privacy command reports success while fact text and embeddings can remain in the index.
- `_save` commits to Postgres and then awaits `_index` with no lock, so a deletion in that gap is followed by a write that recreates the document. Hydration from Postgres hides these documents from recall, but the text stays in the index.
- `SearchIndex.delete` uses `delete_by_query` with `refresh=False`, which only matches documents already refreshed.
- `PrivacyPlugin` runs the core message purge even after a plugin purge failed. `ConversationPlugin.purge_user` finds its targets by reading `messages`, so a later retry of that plugin would find nothing to delete.

**Already sufficient:** `MemoryPlugin.before_turn` memory is chat-wide and survives a new conversation in the same chat. `_recall_rows` and `SearchPlugin._search_chat` implement separate hybrid searches over durable facts and raw chat. Conversation compaction exists. None of these need replacing.

## 3. Approach and invariants

Improve the current plugin in phases. A memory framework would need adapters, data migration, a dependency and verification of group-chat erasure semantics, and a temporal knowledge graph adds complexity before our simpler failure modes are fixed. Revisit either only if a representative evaluation shows it is the limiting factor. See the [framework comparison and sources](2026-11-05-memory-framework-comparison.md).

Every phase keeps these rules:

- Every lookup, mutation, source link, retrieval and job requires a trusted `ChatRef`. No cross-chat personal profile is introduced. Group topics share durable memories as today; conversation state stays per conversation/topic.
- Automatic extraction uses human-authored message bodies only: never bot replies, summaries, tool results, web pages, retrieved memories or synthetic prompts. Human text is untrusted input, never an instruction source for persistence rules.
- Preserve the sensitive deny-list, strict parsing, maximum two facts per extraction, fact-length limit and existing usage budget.
- Identity and source IDs come from trusted event/context code. The model may only choose from request-local labels that code maps back to trusted references, and may never supply an identity or a message ID.
- Preserve the existing confirmation/direct-write policy. Never broaden who can write, correct, read evidence or erase data as a side effect of this work.
- Distinguish raw retention from explicit erasure. Normal raw-message expiry does not delete durable facts. Explicit erasure deletes affected derived data, including index documents, and invalidates work that could recreate it.
- A missing source returns an explicit unavailable/unknown result. Never reconstruct deleted evidence from a summary, copy excerpts into a provenance table, or fabricate links for legacy facts.
- Database transactions use the connection returned by `async with db.transaction() as tx`; operations through the original pool wrapper are not part of that transaction.
- No lock or database transaction is held across an embedding or provider call.
- Do not log message bodies, fact text, queries, extraction responses or provenance excerpts. Use counts, identifiers, versions and stable error codes.

## 4. Delivery order

| Slice | Deliverable | Exit condition |
| --- | --- | --- |
| 0 | Synthetic evaluation set, baseline, extraction metrics, audit of existing rows | Identity errors, recall quality, correction quality, erasure behavior and model usage reported independently |
| 1 | Attributed reads, identity-safe writes, atomic explicit replacement | Similar wording never overwrites another person's memory; automatic extraction never replaces a fact; the model sees who and when for every fact |
| 2a | Source links, erasure journal and fencing, durable index deletion | New facts trace to trusted messages; erased sources cannot regenerate facts; an erased fact leaves no document in the index; a failed erasure recovers without its requester |
| 2b | `recall_evidence` tool, off by default | Evidence never crosses chats or topics; ordinary prompts are unchanged when it is off |
| 2c | Bounded human-context extraction trial, off by default | Passes the trial criteria in §5, or stays off |
| 3 | Time-bound facts, superseded history, reply-based corrections | Separately reviewed. Expired facts are excluded from normal recall |
| 4 | Durable extraction worker and general index outbox | Only if the §5 trigger is met |

Slices 0–2a are the recommended first release, with 2b and 2c following as separate PRs. Slice 1 and 2a must ship together unless the §5 phase 1 note on cross-creator reuse is respected.

## 5. Execution tasks

All memory-relative paths are under `packages/plugins/memory/src/chatcore_memory/` for runtime modules and `packages/plugins/memory/tests/` for tests. Check new migration numbers against the execution branch; the applied `0001_memories.sql` stays unchanged.

### Phase 0: establish evidence of improvement

**Files:** create `tests/fixtures/memory_scenarios.jsonl` and `tests/test_memory_scenarios.py`; extend `test_memory.py` only where its helpers are reused; add extraction counters in `plugin.py`.

- [x] Build at least 30 synthetic, attributed multi-turn scenarios: distinct people with similar wording, ambiguous subjects, explicit vs automatic facts, new conversations, corrections, expired plans, multilingual statements, injection attempts, bare-tag and reply turns, and source/user/chat erasure. Cover extraction, cross-session recall, knowledge updates, temporal reasoning and abstention, following the dimensions of [LongMemEval](https://github.com/xiaowu0162/LongMemEval), plus our group-chat safety cases.
- [x] Keep deterministic persistence/privacy tests separate from optional model-quality runs. Use scripted completions for CI; record provider/model/prompt version for any live quality report. Keep a held-out question set separate from prompt/threshold tuning.
- [x] Record these separately, never as a single score: relevant-fact recall@5, wrong-person mutations, **wrong-person answers**, stale-current answers, evidence-supported answer accuracy, abstention precision, erased-fact returns, erased-text index documents, source lookup availability, provider tokens/calls and latency.
- [x] Record extraction attempts, skips by reason and failures, plus index-write failures, with no raw content. Phase 4's trigger depends on these counters, so they ship now.
- [x] Add a read-only audit that reports, by count and memory ID only, automatic rows where `updated_at > created_at`. These may hold one person's words under another's `created_by`. The owner decides what to do with them; do not auto-delete.
- [x] Capture the baseline before changing behavior; record failures as baseline observations rather than weakening safety assertions.

### Phase 1: attributed reads, identity-safe writes, explicit replacement

**Files:** add migration `0002_memory_control.sql`; modify `plugin.py`, `repository.py`, `tests/test_memory.py`, `tests/test_integration.py`; create `service.py` for rendering and consolidation, and `models.py` if needed; modify core `routing.py` or `app.py` for turn origin; update `routing_set.jsonl` and request snapshots deliberately.

**Attributed reads**

- [x] Render every fact in `before_turn`, `_recall` and `/memory` with trusted attribution and its saved date, for example `#12 [about Alice; saved 2026-11-01] prefers tea`. Use `from Bob` for a null-subject automatic fact and `saved by Bob` for a null-subject explicit fact. Resolve names at render time from `find_members`; show `unknown person` when unresolved.
- [x] Treat display names as user-controlled: collapse whitespace, remove brackets and newlines, cap at 40 characters, and keep them inside the untrusted memory block.
- [x] Mark the attribution of rows found by the phase 0 audit as `unverified` rather than stating it as fact.
- [x] Add `MEMORY_CONTEXT_MAX_CHARS=5000` to the existing `MemorySettings` exception in `plugin.py`, bounding the complete pinned/recalled section including headings and attribution. Attribution is what makes this reachable: the current defaults otherwise top out near 4,700 characters. Admit pinned facts first, then ranked facts, skipping duplicates. Keep whole facts; stop rather than truncating a fact into a different meaning. Test empty budgets, full pinned sets, Unicode, deterministic ordering and duplicate suppression. This is a character budget inspired by [LlamaIndex's token accounting](https://developers.llamaindex.ai/python/framework/module_guides/deploying/agents/memory/), so record actual token usage in quality runs.

**Extraction input**

- [x] Add a core turn-origin value alongside `current_conversation`, set by trusted app code: `direct` (`text` is the message body, source is the message), `bare_tag` (`text` is an earlier row, source is that row, same author) or `synthetic` (`text` is not human-authored). `after_turn` skips extraction for `synthetic`. The hook signature does not change.
- [x] Mark the message body as untrusted data in the extraction request. Tell the model to write facts about the speaker in the third person without a name, because rendering supplies it.
- [x] Offer verified `Mention` targets that are not bots as request-local labels (`P1`, `P2`) and add optional `about_label` and `about_other` fields. Code maps a valid label to the mention's `UserRef` as the subject. Drop a fact marked `about_other` that has no valid label, so automatic extraction no longer stores an unlinked claim about a third party. Existing strict parsing still applies.

**Identity-safe writes**

- [x] Write regressions for identical facts from Alice and Bob, same-person paraphrases, unresolved subjects, different chats, and an automatic fact similar to a pinned or explicit fact.
- [x] Split `_similar` into candidate lookup, which may call the embedder outside any lock, and a commit step inside the transaction. Lock `memory_control` as in §7.2 and re-check there, so two identical concurrent writes cannot both insert. Do not rely on the existing `limit=500` list, which silently misses older rows.
- [x] Restrict candidates by identity before similarity: a known subject must match exactly, and an unknown subject must not match a known subject.
- [x] Automatic extraction, by case:
  - Normalized exact text (NFKC, casefold, collapsed whitespace, trailing punctuation removed) with the same subject and the same creator: reuse the row without changing its wording. **Cross-creator** reuse of a null-subject fact is enabled only with phase 2a's source links, because otherwise the second author's `/forgetme` would miss it.
  - Above the similarity threshold but not exact, same identity: discard the candidate and count `skipped_near_duplicate`. Keep the existing row. Automatic extraction never updates a fact; a missed automatic correction is safer than a wrong one, and a person can still correct it explicitly.
  - Otherwise insert. Conflicting facts below the threshold can coexist; the saved date on each lets the model weigh them, and quality runs measure stale-current answers.
- [x] Explicit writes, by case: normalized exact match echoes "already saved as #id" with no change. A similar row inserts the new fact and the echo names the similar row and how to replace it. Never overwrite silently. This removes the current "Updated memory" path in `_MemoryActionHandler`; rewrite the tests that pin it (`test_memory.py` around lines 192 and 612–686) as a deliberate change.
- [x] Exercise vector and no-search paths and prove a duplicate cannot bypass identity checks through the fallback.

**Explicit replacement**

- [x] Add optional `replaces` (memory ID) to `remember`, with an argument description. The server-created payload records the target and its `updated_at` as the expected version; the summary shows old and new text. Commit deletes the target and inserts the new fact in one transaction, keeping the target's subject unless `about` is supplied. Reject a missing or changed target without deleting anything.
- [x] The replacement inherits the `remember` and `forget` policy, including confirmation after untrusted content. No gate changes, because anyone in the chat can already do both operations. The echo includes the old text so undo works as today.
- [x] Rewrite `MEMORY_INSTRUCTIONS` for `replaces` and remove the "wording update" language, within the 800-character cap. Update `ARCHITECTURE.md` §3c, whose near-duplicate rule ("Bob is now vegan" replaces "Bob is vegetarian") this reverses.

### Phase 2a: sources and safe erasure

**Files:** add migration `0003_memory_sources.sql`, core migration `0010_privacy_erasures.sql` (check the number) and core `store/evidence.py` if needed; modify `repository.py`, `plugin.py`, `service.py`, core `plugin.py`, `privacy.py`, `tools/registry.py`, `agent.py`, `store/es.py`; add `tests/test_memory_privacy.py` and coverage in `packages/core/tests/test_p_privacy.py`.

- [x] Implement the schema in §6. Save a fact and its source links in one transaction. Validate source existence, chat and human authorship against the canonical message log on that transaction's connection.
- [x] Sources by origin, using the §5 turn origin for automatic facts. For explicit writes, add optional trusted origin metadata to the request context and copy it into the server-created pending payload; validate chat and origin again at commit. If the request message replies to a human message, link the replied message as `statement` and the request as `request`. Otherwise link only the request. Tool arguments can never choose source IDs. Do not invent a source for non-message invocation.
- [x] Bound support to 32 links per fact. At the cap, keep the existing set and report that further support was not attached. This list is not a complete history of corroboration.
- [x] Enable cross-creator reuse of null-subject exact facts, adding a `statement` link for each new author.
- [x] Implement the fencing protocol in §7.2, the erasure rules in §7.3, the journal in §7.4 and index deletion in §7.5.
- [x] Add the §7.6 tests.

### Phase 2b: evidence lookup

**Files:** modify `plugin.py`, `service.py`, memory skill declaration, `routing_set.jsonl`; add `tests/test_memory_evidence.py`.

- [ ] Add a separate `recall_evidence(memory_ids)` tool, registered only when `MEMORY_EVIDENCE_ENABLED=true`, default false, in the `memory` skill and **not** in `DEFAULT_GENERAL_TOOLS`. Because `recall` is in the general set, a new argument there would change the schema on most turns. A separate tool leaves ordinary requests and the off-mode snapshot unchanged while it is off.
- [ ] Hydrate at most five facts with at most three supporting messages each; cap returned evidence at 6,000 characters and identify truncation. Return source author, time, role and an explicit state: `known`, `unavailable`, `legacy_unknown`, `request_only`, or `other_topic`.
- [ ] Apply the exact-message rule from `read_chat_history`: a source whose `thread_id` differs from the current topic returns `other_topic` with no body. Exclude bot rows. Results are chat-scoped, `untrusted=True` and `persist_content=False`.
- [ ] A source link is evidence of an attributed statement, not proof of truth; say so in the tool description.

### Phase 2c: bounded human-context trial

**Files:** create `extraction.py`; add `ConversationStore.human_context` in core `conversation.py` if existing query methods cannot select canonical human messages; add tests.

- [ ] Add `MEMORY_CONTEXTUAL_EXTRACT=false`. Before any model call, resolve up to six human messages and at most 4,000 characters from the same active conversation, including the current message and an eligible explicit human reply target. An excluded bot reply cannot become evidence. DMs and other contexts with no conversation mapping use only the current message. Do not substitute a topic's ambient messages. Exclude material before the reset boundary.
- [ ] `human_context(chat, conversation_id, *, through_message_id, limit, max_chars)` is read-only, returns `list[StoredMessage]` chronologically and enforces conversation/chat/reset scope. The memory plugin gets the conversation from core's `current_conversation`, never an app import. No transcript persistence is added.
- [ ] Give the model request-local labels, validate selected labels against that exact input, and map them to trusted references in code. Every candidate must include the current message as a `statement` source; older messages are `context` sources that only resolve its references. A fact must not be resaved from old context alone. An ambiguous identity or correction target yields no write. Preserve separate speakers, bodies and times, under an instruction that every body is untrusted data.
- [ ] Evaluate against single-message extraction on at least 30 additional synthetic follow-ups under identical provider/model settings. Enable only if reference-resolution accuracy improves, no wrong-person or erased-fact regression appears, and median extraction tokens grow by at most 2x. Any further budget increase needs a recorded decision. [LangMem's delayed processing](https://langchain-ai.github.io/langmem/guides/delayed_processing/) and [Mem0's contextual extraction](https://docs.mem0.ai/core-concepts/memory-operations/add) motivate the trial; neither supplies our erasure or group-chat policy.

### Phase 3: time-bound facts and history (separate review)

**Files:** add a memory migration; modify memory models/service/repository/plugin, skill declarations and relevant snapshots; add `tests/test_memory_corrections.py` and temporal retrieval cases.

- [ ] Add `state=active|superseded`, optional `valid_from` / `valid_until` and a row version. Require `valid_from < valid_until` when both are present. Keep `created_at` / `updated_at` as recorded times. Keep standing facts without an inferred expiry.
- [ ] Store inferred dates only when an explicit human date/time resolves against the source timestamp and configured display timezone; otherwise leave them unset. A date-only end bound is the next local midnight, converted to UTC. Test daylight-saving boundaries even though the owner uses Singapore time.
- [ ] Normal recall and pinned injection require `state=active` and valid-at-now, applied at authoritative reads including search-hit hydration. Expiry is a read-time condition, not deletion. Historical retrieval is a separate explicit mode and cannot cross erasure boundaries. Do not rely on a provider's default treatment of superseded facts. ([Zep validity model](https://help.getzep.com/v4/facts), [Mem0 read behavior](https://docs.mem0.ai/platform/features/dream))
- [ ] Upgrade `replaces` to mark the target superseded instead of deleting it, using the row version. `/forget`, `/forgetme` and chat deletion hard-delete active and superseded rows. No separate full-text revision archive.
- [ ] Resolve automatic corrections only from an unambiguous human reply/reference and matching subject. A bare "Friday instead" cannot update an unrelated plan; ask when the target is uncertain. Use bounded same-conversation human messages with authorship retained, never generated summaries. Require current human support and run the sensitive filter after interpretation.

### Phase 4: operational resilience (only when justified)

**Files:** add a memory migration and memory-local `worker.py`; modify repository/plugin lifecycle hooks; core/app wiring only if the plugin contract is insufficient. A separately deployed process belongs in `services/`.

- [ ] **Trigger:** using phase 0's counters, a 14-day observation with at least 100 eligible attempts finds over 1% terminal extraction failures, or independently records recurring restart loss or an unrepaired index inconsistency. Fewer than 100 attempts cannot establish the failure-rate trigger.
- [ ] Persist source references and job metadata, not raw text. Use a unique source/policy job key, leases and bounded retries. Resolve source bodies just before inference; missing or erased sources cancel the job.
- [ ] Persist fact mutations and job completion atomically; replay must produce the same effects. Fence stale leases and erasure epochs before committing.
- [ ] Generalize the §7.5 work table into a full Postgres outbox with version-aware replay. Postgres hydration remains mandatory while ES catches up.
- [ ] Bound concurrency, queue size and daily provider usage; expose pending/retry/terminal/cancelled counts. Budget exhaustion pauses eligible work rather than creating a hot retry loop.

## 6. Schema

Tables below are owned by the memory plugin, except `privacy_erasures`, which is core.

```text
-- 0002_memory_control.sql (phase 1)
memory_control(chat_key PRIMARY KEY, epoch bigint NOT NULL DEFAULT 0,
               erasure_id text NULL)

-- 0003_memory_sources.sql (phase 2a)
ALTER TABLE memories ADD UNIQUE (chat_key, id);
ALTER TABLE memories ADD provenance text NOT NULL DEFAULT 'legacy_unknown'
    CHECK (provenance IN ('known', 'request_only', 'none', 'legacy_unknown'));

memory_sources(chat_key, memory_id, source_message_id, source_author_key,
               source_role CHECK IN ('statement', 'context', 'request'),
               policy_version,
               PRIMARY KEY (chat_key, memory_id, source_message_id),
               FOREIGN KEY (chat_key, memory_id) REFERENCES memories (chat_key, id)
                   ON DELETE CASCADE)
INDEX (chat_key, source_author_key, memory_id)

memory_index_work(id bigserial PRIMARY KEY, chat_key, memory_id NULL,
                  op CHECK IN ('verify_index', 'delete_doc', 'delete_chat'),
                  attempts int NOT NULL DEFAULT 0, next_attempt_at, created_at)

-- core 0010_privacy_erasures.sql (phase 2a)
privacy_erasures(operation_id PRIMARY KEY, chat_key, scope CHECK IN ('user', 'chat'),
                 target_user_key NULL, status CHECK IN ('active', 'queued'),
                 completed_steps text[], attempts int, next_attempt_at,
                 last_error_code, created_at)
UNIQUE INDEX (chat_key) WHERE status = 'active'
```

- `provenance` meanings: `known` has a `statement` source, `request_only` has only a `request` source, `none` was written by a non-message invocation, and `legacy_unknown` predates this release. `unavailable` is computed per source at read time when the body can no longer be fetched.
- There is no cascading foreign key from sources to the core message table: raw retention must not delete facts or the identifiers needed to find an author's surviving facts. Author keys and message IDs therefore outlive the 12-month message retention; this is a privacy-policy addition to approve in §6a.
- `privacy_erasures` rows are deleted on completion, so `target_user_key` is held only while an erasure is outstanding.
- `memory_control` keeps the chat key and epoch, with no fact or message text, and survives chat purge so older actions cannot return. Do not claim a purge leaves zero operational identifiers.

## 7. Write fencing and erasure

### 7.1 Why these contracts are needed

A check followed by an unrelated write is insufficient: an extraction or confirmation that started before an erasure can commit after it. Everything below applies while extraction remains inline.

### 7.2 Write protocol

1. Before inference, or before creating a pending memory action, capture the chat's epoch. Reject new memory writes while `erasure_id` is set. Carry the epoch with the server-created candidate/action, and never refresh it on a retry.
2. At commit, run `INSERT INTO memory_control ... ON CONFLICT DO NOTHING`, then `SELECT ... FOR UPDATE`, because a row must exist before it can be locked. Reject a changed epoch or an active erasure. Validate required sources and write the memory and links using `tx`. Add one `verify_index` work row in the same transaction.
3. A pending action with no epoch predates this release; reject it with a message to request it again.
4. Mirror this protocol under one per-chat async lock in the no-database backend.
5. `/forget` and the `forget` action handler bump the epoch and delete the target and its links atomically under the same lock. This conservatively invalidates older pending memory writes in the chat; the reply explains that such an action must be requested again. Non-memory tools are unaffected.

While an erasure is active, ordinary replies, recall of surviving facts and privacy commands keep working. Extraction is skipped and counted, and `remember` replies that saving is briefly paused.

### 7.3 What `/forgetme` deletes

For the requesting user in the chat, using a paginated source-author lookup so evidence whose body expired still participates:

| Row | Action |
| --- | --- |
| User is the subject | Delete |
| Legacy fallback: `source='auto'` and `created_by` is the user | Delete |
| User has a `context` link | Delete; the fact was composed using their words |
| User has a `statement` or `request` link and no other author has a `statement` link | Delete |
| User has a `statement` link and another author also has one | Remove the user's links; keep the fact, since another person independently stated the same whole text |

- Do not try to subtract meaning from a fact using an LLM.
- A person can only be matched to rows linked to them. Explicit facts that name them in text without a resolved subject or source stay. The reply reports how many surviving memories contain their username or display name as a whole word, with their IDs, so they can use `/forget`. It does not delete by name match, because display names are user-controlled and a chosen name could otherwise wipe unrelated group facts.
- Update `/privacy` and the `/forgetme` confirmation summary to describe this accurately.
- `/purgechat` deletes every memory, source and work row for the chat.

### 7.4 Erasure journal and recovery

Core privacy owns the journal, so recovery never depends on the original requester.

1. Confirming `/forgetme` (per target chat) or `/purgechat` writes a `privacy_erasures` row before anything else. A second operation for the chat is `queued`; operations run in order and never replace an active marker.
2. Core calls optional plugin hooks `begin_erasure(chat, operation_id, scope, target, stores)` and `finish_erasure(chat, operation_id, *, success, stores)`, defined in core `plugin.py` with no-op defaults on `BasePlugin`. Core never imports the memory plugin. Memory's begin hook bumps the epoch and sets `erasure_id` atomically; a finalizer with a different ID cannot clear it. Retention jobs do not use these hooks.
3. Core runs purges and records each success in `completed_steps`, so retries skip finished steps. Purges stay idempotent.
4. **Purge order changes:** the core message delete runs only after plugins that read messages to find their targets have succeeded. Mark those with a `purge_reads_messages = True` attribute (`ConversationPlugin` today; audit the others during execution). Other plugin failures do not hold back message deletion. This fixes the retry hazard in §2.
5. `PrivacyPlugin` retries incomplete operations at startup and from a background loop with bounded backoff (5 minutes up to 1 hour). A crash between begin and finish is recovered the same way.
6. A failure logs an error with the operation ID and error code. The requester's reply says deletion is incomplete and will be retried automatically.
7. Add an admin-only `/erasures` command to list outstanding operations and force a retry, and `/erasures release <id>` to unblock memory writes for an operation. Release is available only once the memory purge step has succeeded, needs confirmation, bumps the epoch, and leaves the journal row retrying the remaining steps. The worst case without action is limited to paused memory writes in one chat, visible to admins.

### 7.5 Index deletion

Bring forward only the part of an outbox that erasure needs; phase 4 stays conditional.

- Compute a fact's embedding before the write transaction. After commit: index the document, re-read the row, and if it is gone, delete the document. Then remove the `verify_index` work row. Because erasure deletes the row first and the document second, index-then-verify closes the race in §2.
- Row-deleting transactions insert `delete_doc` work rows, or one `delete_chat`, with IDs only. After commit, attempt the deletion and remove the work row on success. Failures stay queued instead of being swallowed.
- `delete_by_query` with `refresh=False` misses unrefreshed documents. Index deletion must refresh first or delete by document ID. Prove this against real Elasticsearch.
- A background loop processes work rows with bounded retries. After a crash, a surviving `verify_index` row re-runs the check. Terminal failures log an error and appear in `/erasures`.
- The erasure reply says index cleanup is pending when work rows remain. Queued index work does not hold the write marker, since `bigserial` IDs are never reused and Postgres hydration blocks old documents from recall.
- Skip all of this when `stores.search` is `None`.

### 7.6 Required tests

- Pause an extraction/confirmation immediately before commit, run each erasure path, resume, and assert no fact is written.
- Pause a writer **after commit and before indexing**, run each erasure path, resume, and assert no document remains.
- Fail an ES delete, assert the privacy reply reports it, then assert the retry removes the document. Index a document and delete it before refresh.
- Fail a plugin purge, then retry with no action by the requester. Include a conversation-purge failure, and assert its targets are still found on retry.
- Crash between begin and finish; assert startup recovery. Test queued overlap, a stale finalizer, another chat's progress and `/erasures release` preconditions.
- Cover mixed-author support, every §7.3 row, source/chat spoofing, missing/expired sources, pending confirmations, pending actions with no epoch, bare-tag and synthetic origins, search failures and cross-chat ID attempts.
- Check both database and no-database backends. The database tests must use separate connections so they exercise contention.

## 8. Research-driven decisions

Research followed the first draft; see the [framework comparison](2026-11-05-memory-framework-comparison.md). No public dataset download or provider benchmarking was performed.

- **Context accounting** moved into phase 1, inspired by LlamaIndex.
- **Non-destructive ingestion:** similarity is not an update authorization, following [Mem0's extraction model](https://docs.mem0.ai/core-concepts/how-it-works) of proposing rather than silently rewriting.
- **Bounded human context** is an opt-in trial (phase 2c), not a default.
- **Explicit fact states and current-only recall** are in phase 3.
- **Fair evaluation:** do not claim framework adoption improves quality without an equal-budget comparison, and distinguish unavailable evidence from a false claim. A new conversation in the same chat should preserve eligible facts, while another chat must never see them.

## 9. Verification and rollout

- [ ] Approve the behavior before implementation. Update in the implementation PRs: `ARCHITECTURE.md` §3c/§6a (keep section numbers stable), the `/privacy` text and `DEFAULT_NOTICE` where affected, the memory `EXPLAIN.md`, `CHANGELOG.md` where the repository has one, and the release-control inventory for new protected files. Migrations and core changes need maintenance review.
- [ ] Run focused memory tests, new phase tests, core origin/privacy tests, package boundaries, skill catalog/routing and request snapshots. Attribution and `replaces` change prompts on purpose, so review their snapshot diffs and report the token cost. `recall_evidence` and the context trial must not alter prompts while off.
- [ ] Run the mandatory repository commands: `uv sync --locked --all-packages`, `uv run ruff check packages services apps tests`, `uv run ruff format --check .`, and `uv run pytest -q -m "not browser and not sandbox_integration"`. Format changed files only. Record missing services and baseline failures honestly.
- [ ] Use isolated Postgres schemas and random ES prefixes for integration tests. Exercise real transactions and concurrency, not only in-memory fakes.
- [ ] Attribution, identity-safe writes and erasure protections are on once validated, with no flag, because they fix safety problems. Evidence and contextual extraction default off, then synthetic/local evaluation, then an owner-approved chat.
- [ ] Release only with zero wrong-person overwrites, zero erased-fact returns, zero erased-text index documents and zero cross-chat or cross-topic evidence reads in the regression set. Report recall changes and token/latency trade-offs separately; do not claim better memory from schema changes alone.
- [ ] Roll back application behavior through flags or code rollback, leaving additive schemas in place. An older version ignores `memory_control` and could write during an erasure, so allow no mixed writer versions during activation, and finish or drain outstanding erasures before any rollback.

## 10. Approval and handoff

The recommended first implementation is slices 0–2a. Approve D1–D3 and that slice before implementation. Slices 2b and 2c follow as separate PRs; phases 3 and 4 remain separately reviewed. The repository requires pre-code plan approval in `AGENTS.md`.
