# Independent review of the skill-layer design

Reviewer: Codex `gpt-6-astra`, headless and read-only. Final verdict (round 5, revision 6,
2026-11-01): **PASS_WITH_NOTES**: ready for design approval and detailed implementation
planning. The user decisions U1–U8 in plan §11 remain open, and U6 and U7 must be recorded before
those branches are built.

| Round | Plan revision | Verdict | Findings |
|---|---|---|---|
| 1 | 1 | CHANGES_REQUIRED | S01–S13 |
| 2 | 2 | CHANGES_REQUIRED | R2-01–R2-07 |
| 3 | 3 | CHANGES_REQUIRED | R3-01–R3-05 |
| 4 | 5 (revision 4 was never reviewed on its own; the run hung on stdin) | CHANGES_REQUIRED | R4-01–R4-03 |
| 5 | 6 (SHA-256 `c4d1b1f0…74b4`) | PASS_WITH_NOTES | none |

After round 5, the only change to the plan was check 18 in §10. It turns round 5's two
"concerns not promoted to defects" into acceptance checks.

## Round 1 — Revision 1 review, 2026-10-31 (Asia/Singapore)

Date: 2026-10-31 (Asia/Singapore). Verdict: **CHANGES_REQUIRED**.

The direction is workable, but the design needs explicit contracts for authorization caching, new tools, loop accounting, partial availability and rollout before implementation. These corrections preserve DeepSeek V4.1 Flash, the Claude-plans/Codex-builds workflow and ARCHITECTURE.md §6a.

### Evidence and scope

Reviewed `plans/skill-layer.md`, revision 1:

- SHA-256: `af367fa2a3654cf32e02b830f7e55f44ee8312c9dbf97d350e3ed3bc5dab4cb5`
- Git HEAD: `77af71eb29839fd829dc55b29f378da1982aadd5`

Both were rechecked at the end.

Read ARCHITECTURE.md §3d, §6a and §14–§19; core `agent.py`, `tools/registry.py`, `plugin.py`, `routing.py`, `app.py`, `prompts.py`, `limits.py`, `embed.py`, `conversation.py`, `threads.py`, `pending.py`, `confirm.py`, and supporting access, privacy, cache and provider code. Checked the requested plugins’ tool/command declarations and relevant services, plus stocks/web declarations, SBot composition and `prompts/system.md`. The supplied Astra review was read for format only.

Below, `core/` means `packages/core/src/chatcore/`; plugin references use their repository paths. Findings distinguish confirmed contradictions from missing design contracts. No files were modified, network calls made, tests executed or bot processes run. Production configuration and provider behavior were not experimentally verified.

### Findings

#### S01 — High: chat-only caching cannot represent user-dependent availability

**Location:** plan §3.1, §4.2, §5, lines 84–94, 167–171, 245–247; `core/access.py:77`, `:95`; `core/tools/registry.py:112`.

**Classification:** confirmed design defect; an eventual unauthorized commit is not established.

**Failure scenario:** a moderator’s cached `AVAILABLE` result is reused for another member, or a member’s denial blocks the moderator. Revoking moderation need not invalidate the cache because only toggle commits trigger invalidation.

**Evidence:** availability explicitly depends on the user and permissions, but is cached per chat. Registry filtering checks only `admin_only`; it does not implement moderator, native-admin or membership rules. Commit checks remain necessary but cannot make the offered set accurate.

**Correction:** cache deployment configuration and scope settings separately from authorization. Recompute user authorization, or key and invalidate it by the relevant user, scope and policy revision. Recheck availability for continuation and `use_skill`. Startup validation cannot prove an arbitrary asynchronous predicate safe for every future context; enforce declarative permission requirements at runtime.

**Acceptance test:** alternate moderator/member requests in one chat, revoke permission between requests and before confirmation, and repeat through continuation and composites. No cached grant or denial crosses users.

#### S02 — High: new read tools lack an explicit untrusted-result contract

**Location:** plan §6, §10, lines 262, 283, 376–381; `core/conversation.py:1424`; `packages/plugin-polls/src/chatcore_polls/service.py:223`; `core/tools/registry.py:55`, `:128`; `core/agent.py:697`.

**Classification:** missing design.

**Failure scenario:** `get_persona` or `list_polls` returns member-written instructions as trusted tool output. A subsequent `remember` call then writes immediately because `read_untrusted` was never set.

**Evidence:** persona and poll questions contain user-authored text. `ToolResult.untrusted` defaults to false; wrapping and the memory confirmation guard depend on that flag. Calling a tool “read-only” does not establish its output’s trust level.

**Correction:** require `untrusted=True` for persona text, poll questions/options and other user-authored fields returned by new tools. Bound their output and preserve the shared `TurnState` through skill loading.

**Acceptance test:** malicious persona/poll text followed by `use_skill`, `remember`, `forget` and timezone mutations remains wrapped; memory writes require confirmation and timezone mutations refuse after untrusted reads.

#### S03 — High: the proposed `/new` tool introduces an undocumented write exception

**Location:** plan §6, lines 253, 259, 296–299; `core/app.py:507–545`; `core/prompts.py:8–13`.

**Classification:** confirmed design contradiction.

**Failure scenario:** injected content induces `start_new_conversation`, silently changing which context the requester’s next message receives.

**Evidence:** `/new` writes a one-hour, requester-scoped cache marker, subsequently consumed by conversation selection. The plan calls its tool equivalent “no data change, no click,” while requiring every new write tool to return a pending action. Existing immediate-write exceptions name memory and explicit timezone preferences, not conversation routing.

**Correction:** make the new tool a confirmed pending action, or retain `/new` as command-only. Do not introduce a further exception implicitly. Narrow the blanket “no admin/destructive action gains an NL path” statement to the actual excluded operations; confirmed context reset is explicitly proposed elsewhere.

**Acceptance test:** loading the conversation skill after malicious tool output cannot set the next-conversation marker without confirmation. Confirmation affects only the requester’s original chat/topic.

#### S04 — High: exempt meta-tool calls can exhaust rounds without reaching the final-answer path

**Location:** plan §4.5, §7, §10.5, lines 203–213, 307–315, 373–375; `core/agent.py:535–537`, `:649–655`, `:732–737`; `core/tools/registry.py:118–128`.

**Classification:** missing design, with a confirmed loop-integration gap.

**Failure scenario:** repeated unavailable, duplicate or over-limit `use_skill` calls consume rounds without increasing ordinary steps. The loop reaches its generic exhaustion response instead of the required final completion. Multiple calls in one response also contradict “each load consumes a loop round.”

**Evidence:** today’s final condition uses tool-step exhaustion or deadline exhaustion; the loop’s final iteration is not independently forced final. Registry validation does not enforce JSON Schema enums, so excluding a name from the advertised enum is insufficient.

**Correction:** specify counters for provider rounds, ordinary calls and all meta-tool attempts, including failures. Reserve a final completion independently of those counters. Validate names and availability in code. Define whether tools loaded in a response become executable only on the next request. Preserve one result per returned call ID, including rejected and skipped calls.

**Acceptance test:** cover repeated OFF/invalid/duplicate loads, exhausted load allowance, mixed batches, load-plus-new-tool batches, deadline expiry, confirmation early exit and empty-answer retry. Every path terminates with consistent call/result history and the intended final-answer behavior.

#### S05 — Medium: whole-skill availability hides valid operations and leaves composites ambiguous

**Location:** plan §3.1–§3.3, §4.2, lines 84, 105–129, 167–171; `packages/plugin-jobs/src/chatcore_jobs/service.py:204`, `:302`; `packages/plugin-polls/src/chatcore_polls/service.py:223`, `:236`; `packages/plugin-browse/src/chatcore_browse/plugin.py:75`.

**Classification:** confirmed design defect.

**Failure scenario:** disabling reminders hides listing/cancellation; disabling polls hides listing/closing; denying reset hides `/new` and persona reads. An OFF rendered-fetch skill can also hide the always-available link builder.

**Evidence:** those operations have different gates today. The proposed type returns one availability value for the entire bundle. Composite “AND” availability, optional poll participation and dropping OFF components have no corresponding required/optional dependency representation.

**Correction:** split bundles by gate or add explicit per-tool eligibility. Define required versus optional composite dependencies, canonical ownership for shared tools, all four availability states and cycle rejection. Resolve the separate contradiction between three initial skills, two subsequent loads and the claimed three-skill/2,400-character maximum.

**Acceptance test:** enumerate mixed availability states, including disabled creation with permitted cleanup, non-moderator conversation reads, optional polls and shared tools. Composites must neither restore denied tools nor hide independent permitted operations.

#### S06 — Medium: startup validation conflicts with optional deployment and plugin compatibility

**Location:** plan §3.1–§3.3, §9, lines 89–103, 117–131, 355–358; `core/app.py:116`; `apps/sb_bot/src/sb_bot/app.py:97`, `:438`, `:487`, `:506`; `packages/plugin-expenses/src/chatcore_expenses/plugin.py:811–829`.

**Classification:** confirmed design contradiction.

**Failure scenario:** stage 1 rejects stage-4 tool names; disabling receipt vision makes the expenses declaration invalid; a valid low `AGENT_TOOL_STEPS` setting rejects fixed skill budgets; an unchanged third-party plugin fails orphan validation.

**Evidence:** tools are conditionally registered. `read_receipt` specifically requires a receipt reader. Existing plugins have no skill declaration, and the application accepts global step caps from 1 to 20.

**Correction:** declare skills against the actual installed registry and delivery stage. Define a compatibility policy for undeclared plugin tools, especially in `off` mode. Treat skill budgets as ceilings clamped to the configured global limit. Correct the count claim: 42 is the fully populated composition, not an unconditional count; receipt vision and other deployment conditions matter.

**Acceptance test:** validate each rollout stage with optional plugins absent, receipt vision disabled, global caps of 1 and 8, and a legacy plugin exposing tools without `skills()`.

#### S07 — High: end-of-turn usage recording does not enforce the daily budget between added rounds

**Location:** plan §4.6, §7, lines 219–223, 313–317; `core/agent.py:454`, `:572`, `:793–835`; `core/limits.py:144–178`; `packages/plugin-research/src/chatcore_research/agent.py:287–309`.

**Classification:** confirmed existing limitation inherited by the proposed cost contract.

**Failure scenario:** a turn starts below the daily cap, crosses it, then makes additional skill-loading, final, retry and summary completions. Concurrent chats still see unrecorded usage.

**Evidence:** the main agent checks limits before the turn and records aggregate usage afterward. Recording additional rounds “like any round” does not stop further calls once the cap is reached. Research already checks its budget between completions.

**Correction:** define budget admission for every added completion and account completed usage incrementally, without double-counting research or summaries. Specify bounded in-flight overshoot and how final-answer capacity is reserved. Usage accounting must survive delivery/persistence failures.

**Acceptance test:** cross the cap after the first completion and during research; attempt another load, retry and summary; run concurrent chats. Assert the declared budget behavior and exact usage totals.

#### S08 — Medium: embedding timeout does not bound shared-worker occupancy or startup work

**Location:** plan §4.1, §4.2, §4.6, lines 146, 178–180, 217–218; `core/embed.py:62–81`; `packages/plugin-search/src/chatcore_search/__init__.py:194–196`, `:623`; `apps/sb_bot/src/sb_bot/app.py:162–171`, `:293–338`.

**Classification:** missing design; latency estimates are unverified.

**Failure scenario:** attachment indexing occupies the single worker. Routing repeatedly times out, while a running inference continues occupying that worker. Startup example embedding also raises on disabled-embedder deployments unless handled separately.

**Evidence:** inference uses one `ThreadPoolExecutor`; indexing can submit document batches. An await timeout does not stop an already-running thread operation. SBot deliberately supplies no operational embedder without ES, and ES configured without a model directory is a startup error.

**Correction:** define bounded admission, cancellation behavior and contention metrics for router embedding. Specify startup fallback before computing examples. Benchmark queued latency and indexing impact, not just isolated inference; label multilingual quality and timing as hypotheses pending measurement.

**Acceptance test:** saturate indexing, time out running and queued routing work, and start without ES/model configuration. Replies remain available, outstanding work stays bounded, and fallback performs no model download.

#### S09 — Medium: prompt migration breaks the promised `off` rollback

**Location:** plan §3.3, §9, lines 133–137, 344, 357, 361–362; `core/agent.py:247–268`; `apps/sb_bot/src/sb_bot/app.py:595–608`.

**Classification:** confirmed design contradiction.

**Failure scenario:** stage 3 removes instructions from `system.md` and runtime context. Switching to `off` restores all tools but not their former instructions, despite the byte-for-byte compatibility promise.

**Evidence:** the application loads one prompt file; the plan specifies no legacy prompt path. A stage-1 snapshot cannot verify rollback after stage-3 prompt changes.

**Correction:** retain a mode-specific legacy prompt/context builder for `off` and `shadow`, including chats excluded by `SKILLS_CHATS`. Migrate instructions only for effective `on` turns.

**Acceptance test:** after implementing prompt migration, compare deterministic provider requests in `off`, `shadow` and excluded chats against the baseline, including final and retry requests.

#### S10 — High: requested tool names are not necessarily safe log identifiers

**Location:** plan §9, §10.12, lines 346–350, 389–390; `core/llm/openai_compat.py:131–139`; `core/tools/registry.py:118–121`.

**Classification:** confirmed false premise in the logging design.

**Failure scenario:** a model returns a fabricated tool name containing message text, a URL or a secret-like sentinel. Logging it in `called=<tool names>` persists that content even though execution is rejected.

**Evidence:** provider-returned names are accepted as strings; only registered tool definitions undergo identifier validation. “Tool names … are code identifiers” is true for registry entries, not arbitrary attempted calls.

**Correction:** log names only after exact registry membership validation. Represent unknown names with a constant category and count. Apply the same rule to requested skill names and rejected-call diagnostics; regex validity alone does not establish trusted provenance.

**Acceptance test:** place sentinels in fabricated function names, unknown skill names and malformed arguments. Logs contain only approved identifiers, categories and counts.

#### S11 — Medium: shadow “recall” credits an unobserved fallback and lacks reproducible metrics

**Location:** plan §9–§10, lines 346–357, 395–396; `core/agent.py:649–680`.

**Classification:** confirmed measurement defect.

**Failure scenario:** routing misses every specialized skill, but an oracle simulated load covers the observed calls and passes the rollout gate. Actual DeepSeek turns may choose the wrong skill or exhaust recovery rounds.

**Evidence:** shadow mode offers all tools, so it does not observe the model’s fallback choice. The specified log lacks explicit initial-set coverage, missed registered tools and simulated-load selection. Baseline comparison also lacks outcome and workload stratification.

**Correction:** report initial routing coverage separately from oracle recoverability. Exclude invalid calls from required-tool ground truth while counting their errors separately. Test actual fallback behavior with bounded multi-turn evaluations on the fixed production model before broader rollout. Record privacy-safe coverage fields and segment by language, continuation, disabled feature and tool family.

**Acceptance test:** an always-general router must fail initial coverage despite high oracle recoverability. Include multilingual paraphrases, topic changes, failed loads and confirmation-ending turns; report latency percentiles, tokens and task outcomes.

#### S12 — Medium: command recommendations do not consistently match executable paths

**Location:** plan §5–§6, lines 229–237, 257–290; `core/routing.py:83–103`; `apps/sb_bot/src/sb_bot/app.py:621`; `packages/plugin-jobs/src/chatcore_jobs/service.py:255`; `packages/plugin-memory/src/chatcore_memory/plugin.py:335–367`, `:443–454`; `packages/plugin-extensions/src/chatcore_extensions/plugin.py:222–346`.

**Classification:** confirmed factual and UX defects.

**Failure scenario:** users copy an enable command that is silently ignored, expect another administrator to approve their request, or lose a command’s functionality when directed to a non-equivalent tool.

**Evidence:**

- SBot ignores bare group commands; the proposed hints show `/polltools on`, `/jobtools …` and `/new` without bot addressing.
- Feature configuration requires an authorized requester before creating the pending action. Another administrator cannot confirm that requester’s action.
- Jobs accepts setup managers as well as native administrators; “group admin” is incomplete.
- `/memory` lists up to 50 memories; `recall` performs query-based retrieval capped at 20.
- Extensions uses `build`, `review`, `activate`, `revoke`, `status` and `list`; “approve” is not the command syntax.

**Correction:** produce executable command hints using trusted app routing configuration. Document requester eligibility separately from confirmation. Preserve command shortcuts where the tool is not functionally equivalent, and correct extension subcommand guidance.

**Acceptance test:** feed generated hints through the real router; test ordinary-member versus manager configuration requests, complete memory listing and every documented extension subcommand.

#### S13 — Medium: continuation invalidation and purge require integration that does not exist today

**Location:** plan §4.3, §8, lines 184–192, 328–334; `core/conversation.py:1235–1239`; `core/privacy.py:213–225`; `core/store/base.py:32–42`; `core/threads.py:110–118`.

**Classification:** missing design and a confirmed incorrect assumption about existing purge behavior.

**Failure scenario:** a DM reset retains its unchanged `chat:thread` continuation key, or chat purge leaves skill-cache entries behind because no component owns their removal.

**Evidence:** reset updates conversation state; privacy purge invokes plugin/database deletion. The cache interface has exact-key deletion, not prefix enumeration. Existing purge does not automatically delete arbitrary chat cache keys.

**Correction:** assign ownership for reset/new/purge integration. Use scoped generations or an explicit key index compatible with the existing cache interface. Define behavior when framework conversation threading is disabled. Preserve intended sharing within one group conversation while isolating distinct conversations and topics.

**Acceptance test:** cover DM reset, group generation reset, `/new`, purge, old-message replies, two conversations in one topic and threading-disabled deployments. Old records cannot reactivate after invalidation.

### Decisions for the user

- Retain the hybrid router as the proposed direction, conditional on separate initial-coverage and actual-fallback evidence.
- Choose a confirmed NL `/new` tool or keep `/new` command-only; the current no-click proposal conflicts with the stated write rule.
- Resolve U3–U6 after correcting availability and command equivalence. Menu removal should follow demonstrated replacement behavior.
- Approve the optional plugin contract together with its legacy-plugin and optional-deployment policy.

### Concerns not promoted to defects

- Full tool specs on ordinary, forced-final and empty-answer-retry requests are confirmed. ARCHITECTURE.md §14 describes an older state; current code uses `tool_choice="none"` while retaining specs.
- Existing per-group toggles generally reject operations at execution rather than removing registered schemas. Existing service checks and requester-only confirmation materially constrain escalation; loading a skill alone is not a demonstrated permission bypass.
- Previous turns are rebuilt as wrapped transcript data, not replayed as raw assistant tool-call history (`core/agent.py:139–170`, `:307–324`). The plan overstates that historical protocol concern. Within-turn call/result consistency still needs S04’s tests.
- The framework placement respects §3d if skills remain platform-neutral. The existing Ruff configuration already bans direct `telegram` imports outside the adapter; a boundary test would complement it.
- `/persona`’s unrestricted mutation is confirmed and acknowledged by the plan. Keeping its permission fix separate is reasonable, but the new read tool still needs S02’s trust contract.
- Existing memory and timezone immediate-write exceptions must be described accurately and preserved narrowly. No broader exception follows from introducing skills.
- No live evidence establishes production’s exact tool count, 20–80 ms routing latency, multilingual recall or provider prefix-cache behavior. These remain measurements to obtain, not acceptance results from this review.

## Round 2 — Revision 2 re-review, 2026-10-31 (Asia/Singapore)

**CHANGES_REQUIRED**

- Reviewed plan: `plans/skill-layer.md`, revision 2.
- SHA-256: `4f5b67b2ba5e936e8631b79245f7252185492098cd419dffa17ee0aac99e4cf2`
- Git HEAD: `77af71eb29839fd829dc55b29f378da1982aadd5`

Both identifiers were rechecked at completion. This was a static, read-only review against the requested core files, plugin services, SBot composition and ARCHITECTURE.md §6a. No files were modified, network calls made, tests executed or bot processes run. Below, `core/` means `packages/core/src/chatcore/`.

### Disposition of S01–S13

| Finding | Disposition | Revision-2 assessment |
|---|---|---|
| S01 — User-dependent availability caching | **PARTIALLY RESOLVED** | §3.1 correctly separates uncached authorization from scope configuration. However, §3.3 assigns user-dependent linked-group eligibility to the scope gate; cache invalidation also needs the actual settings scope. See R2-02 and R2-03. |
| S02 — Trust of new read tools | **RESOLVED** | §6.1 explicitly requires bounded, untrusted persona/poll results and preservation of `TurnState`, matching registry propagation and memory/timezone guards. |
| S03 — `/new` immediate-write exception | **RESOLVED** | §§6–6.1 specify a requester-scoped pending action and no new immediate-write exception. U7 correctly remains a user choice. |
| S04 — Meta-call and round accounting | **RESOLVED** | §§4.5 and 7.2 define failed-attempt accounting, next-request activation, final-round reservation and a result for every returned call ID. §10.6 includes the relevant termination paths. |
| S05 — Partial availability and composites | **PARTIALLY RESOLVED** | Per-tool filtering, canonical ownership, tool-only composites and instruction bounds address the original bundle defects. Argument-dependent eligibility remains underspecified. See R2-02. |
| S06 — Optional deployment and compatibility | **PARTIALLY RESOLVED** | Registry-relative validation, conditional counts and clamped budgets are specified. Literal `<plugin>_other` generation can still reject previously valid plugins. See R2-05. |
| S07 — Budget admission and accounting | **PARTIALLY RESOLVED** | §7.4 specifies incremental recording, later-completion admission and failure-safe turn accounting. Its claimed concurrent overshoot bound is incorrect. See R2-04. |
| S08 — Embedding contention and startup | **RESOLVED** | §4.6 specifies admission by unfinished work, timeout disposal, background example preparation and disabled-embedder fallback. Performance claims are now explicitly hypotheses. |
| S09 — Prompt rollback | **RESOLVED** | D9 and §3.3 retain the legacy prompt/context builder for `off`, `shadow` and excluded chats; §10.2 checks rollback after migration. |
| S10 — Logging unvalidated identifiers | **RESOLVED** | §9 requires exact registry/catalog membership and counts unknown names without printing them. §10.13 covers injected sentinel strings. |
| S11 — Shadow coverage and evaluation | **PARTIALLY RESOLVED** | Initial coverage, oracle recoverability and real-model fallback evaluation are separated. The disabled-feature segment conflicts with the coverage definition. See R2-06. |
| S12 — Executable hints and command equivalence | **PARTIALLY RESOLVED** | Bot addressing, requester eligibility and `/memory` equivalence are corrected. The revised extensions table introduces an incorrect `status` permission claim. See R2-07. |
| S13 — Continuation invalidation | **PARTIALLY RESOLVED** | Reset generations, conversation identity and ownership are explicit. The purge-epoch argument assumes cache consistency that SBot’s configured backend does not provide. See R2-01. |

### New findings

#### R2-01 — Medium: purge epochs can revert when the cache recovers

**Location:** plan §4.3, lines 213–226; `core/store/valkey.py:95–107`; `core/conversation.py:741–785`; `apps/sb_bot/src/sb_bot/app.py:311–316`.

**Classification:** confirmed design defect in the proposed invalidation scheme.

**Failure scenario:** a DM has a live continuation record under epoch `"0"`. Valkey becomes unavailable, and `/forgetme` successfully deletes database data while `bump_epoch` writes only to the in-memory fallback. Valkey recovers within 30 minutes. Reads resume against the primary, where the epoch remains absent and the old `"0"` continuation record survives. The deleted user’s previous skill state becomes reachable again.

**Evidence:** `FallbackCache` selects the backend separately for each operation and does not reconcile fallback writes upon recovery. `/forgetme` does not change the DM’s reset generation. Consequently, the plan’s 24-hour expiry argument does not cover this supported deployment path. Early eviction of an epoch key creates a similar problem; TTL is not a minimum retention guarantee.

**Correction:** make missing or inconsistent invalidation state produce a continuation miss. Specify how purge invalidation survives primary/fallback transitions—through authoritative generations or an explicit cache-recovery protocol. Merely replacing absent `"0"` with a random token does not fix recovery to a primary that still contains an older valid epoch.

**Acceptance test:** create continuation state, fail primary-cache writes during `/forgetme`, restore the primary before the continuation TTL expires, and verify no old state is reused. Also test selective epoch eviction and a turn overlapping the purge.

#### R2-02 — Medium: declared gates mix scope, user and argument-dependent eligibility

**Location:** plan §3.1, lines 81–87 and 103–115; §3.3, lines 140–145; `packages/plugin-expenses/src/chatcore_expenses/plugin.py:76–81`; `apps/sb_bot/src/sb_bot/app.py:371–377`; jobs `service.py:120–131`; polls `service.py:236–254`.

**Classification:** confirmed contract mismatch, with missing design for argument-dependent availability.

**Failure scenario:** the expenses scope gate must decide whether a DM requester has exactly one linked group, despite being forbidden to read the user. Separately, treating rendered-fetch availability as a gate for all `create_website_watch` calls hides valid plain watches. A `close_poll` user gate cannot determine whether the requester created the selected poll before the poll argument exists.

**Evidence:** expense resolution uses `ctx.user.ref`; rendered-watch checks depend on the `rendered` argument; poll ownership is checked only after resolving the requested poll. The proposed scope callback receives only `ChatRef` and a boolean, and the user callback receives no tool arguments.

**Correction:** move linked-group eligibility into the user check. Explicitly distinguish **eligibility to offer a tool** from **authorization of a particular invocation**. Keep tools available when a permitted variant exists, retain argument/resource checks in the handler and commit path, and define how variant-specific restrictions contribute hints.

**Acceptance test:** cover changed DM linked-group membership, plain watches with rendered fetching disabled, and a non-manager closing their own poll versus another person’s poll. Valid operations remain discoverable; denied variants cannot execute or commit.

#### R2-03 — Medium: scope-cache invalidation lacks a group-wide contract

**Location:** plan §3.1, lines 82–84; §10.14; jobs `repository.py:94–99`; polls `repository.py:38–43`; browse `repository.py:21–26`; `core/store/base.py:32–42`.

**Classification:** missing design integration.

**Failure scenario:** topics A and B cache “reminders off.” An administrator enables reminders from topic A. Invalidating the request’s `(chat, thread)` entry leaves topic B advertising the old state for up to 60 seconds. Disabling a feature produces the inverse inconsistency. Rendered-fetch changes can also leave a dependent watch gate stale.

**Evidence:** the proposed cache is partitioned by topic, but all three existing settings repositories store these toggles by `(chat_key, is_dm)`, without a topic key. The plan promises invalidation on commit but supplies neither group-wide invalidation nor dependency tracking; the cache interface cannot enumerate matching keys.

**Correction:** cache configuration at its actual storage scope, or specify a shared revision that invalidates every topic and dependent gate. Cover command confirmations as well as tool confirmations, and invalidate configuration cached before a chat-settings purge.

**Acceptance test:** populate two topic caches, toggle through either topic, and immediately route and `use_skill` in both. Repeat for rendered-watch dependencies and chat purge. An unrelated group remains unaffected.

#### R2-04 — Medium: the stated budget overshoot bound excludes in-flight completions

**Location:** plan §7.4, lines 398–410; `core/limits.py:144–146`, `:178–202`; `core/agent.py:454`, `:559–574`.

**Classification:** confirmed accounting-bound error; accepting bounded overshoot remains a user decision.

**Failure scenario:** two chats pass admission just below the cap and start provider requests. Chat A finishes and crosses the cap. Chat B’s already-admitted ordinary completion then finishes, after which B is allowed an additional final completion. B contributes both an outstanding ordinary completion and a final completion after the cap has been crossed.

**Evidence:** `budget_exhausted()` observes completed usage; it reserves no capacity for admitted requests. Recording immediately improves visibility but cannot remove already-running work. Even without concurrency, total excess includes the portion of the cap-crossing completion above the remaining allowance.

**Correction:** state the bound as outstanding admitted work plus the explicitly permitted final completions, including relevant nested provider work. If the intended bound is strictly one final completion per turn, admission needs reservations. No stricter spending policy is required merely to correct the arithmetic.

**Acceptance test:** use barriers to admit two completions before either records usage, then cross the cap with one. Verify the exact excess, each turn’s subsequent final allowance, skipped retries/summaries and absence of double-counting.

#### R2-05 — Medium: automatic skill names can break the legacy-plugin compatibility promise

**Location:** plan §3.1, line 91; §3.2, lines 122–130; §3.4; `core/plugin.py:51–58`, `:81–85`; `core/app.py:116`.

**Classification:** confirmed compatibility defect.

**Failure scenario:** an otherwise valid legacy plugin named `acme-tools` receives the generated skill name `acme-tools_other`, which fails the skill-name grammar. A long plugin name exceeds the 48-character limit. A generated name can also collide with an explicitly declared skill, triggering the startup duplicate error.

**Evidence:** the existing plugin contract declares `name: str` without the new skill identifier restrictions. Revision 2 specifies literal `<plugin>_other` generation while requiring invalid and duplicate skill names to fail validation.

**Correction:** define deterministic normalization, length bounding and collision handling for generated identifiers. Generated display text must also satisfy the new text bounds without imposing those bounds retroactively on legacy plugin metadata.

**Acceptance test:** exercise hyphenated, long and colliding plugin names, including a collision with an explicit skill. Every previously valid registered tool remains reachable in `on`; compatibility processing leaves `off` and `shadow` operational.

#### R2-06 — Medium: disabled-feature coverage penalizes correct filtering

**Location:** plan §9, lines 445–446 and 455; §10.15; polls `service.py:96–102`; jobs `service.py:99–102`.

**Classification:** confirmed measurement contradiction.

**Failure scenario:** in shadow mode, the legacy model calls `create_poll` while polls are disabled. The router correctly identifies the polls skill, excludes the unavailable tool and supplies the enable hint. Nevertheless, `initial_cover` is false because the called tool was not offered, and `oracle_cover` is false because no legal skill load can expose it. A synthetic disabled-feature segment containing these cases cannot meet the stated 85% initial-coverage gate even with correct routing.

**Evidence:** coverage is defined over all validated called tools, while availability filtering deliberately removes some registered tools. Registry membership establishes a valid identifier, not eligibility for this requester and scope.

**Correction:** separate coverage of eligible operations from correct handling of unavailable requests. Score disabled/not-allowed cases on capability recognition, correct exclusion and appropriate hints or outcomes. Specify denominators and how turns with no eligible calls are treated.

**Acceptance test:** a correctly routed disabled poll request passes the unavailable-request measure without receiving execution-coverage credit. An enabled poll missed by the router fails initial coverage. An always-general router still fails the specialized routing gate.

#### R2-07 — Low: `/extensions status` is incorrectly documented as administrator-only

**Location:** plan §6, line 323; `packages/plugin-extensions/src/chatcore_extensions/plugin.py:320–343`.

**Classification:** confirmed factual error introduced in revision 2.

**Failure scenario:** help or skill guidance derived from the command table tells an ordinary requester that they cannot inspect extension status, although the command permits a filtered view.

**Evidence:** the handler computes `manager` and passes `include_drafts=manager` together with `viewer=ctx.user.ref`; it does not reject non-managers.

**Correction:** describe `status` as command-only with viewer-filtered visibility, with additional draft visibility for managers. Preserve the existing permission behavior.

**Acceptance test:** an ordinary requester receives their permitted status view; a manager receives the additional authorized information; neither receives cross-chat data.

### Decisions for the user

These remain choices rather than defects:

- U1–U3: retain the hybrid router, defer an LLM classifier, and decide whether web tools remain general, subject to corrected evaluation gates.
- U4: handle persona-setting permissions as the proposed separate fix.
- U5: defer menu trimming until the replacement paths have been demonstrated.
- U6: approve the additive plugin contract after its generated-name compatibility policy is corrected.
- U7: choose confirmed NL `/new` or command-only `/new`.
- U8: approve the capped live evaluation and the accurately stated budget-overshoot policy.

### Readiness

Revision 2 resolves six findings and partially resolves seven. **R2-01–R2-06 remain material to design approval and detailed implementation planning; R2-07 is a small factual correction.**

The fixed DeepSeek Flash model, Claude-plans/Codex-builds workflow and ARCHITECTURE.md §6a require no changes.

## Round 3 — Revision 3 re-review, 2026-10-31 (Asia/Singapore)

**CHANGES_REQUIRED**

- Reviewed plan: `plans/skill-layer.md`, revision 3.
- SHA-256: `18a42b897e07e43028416adb59c4f33e41589349a02c4f61840fcabb37122304`
- Git HEAD: `77af71eb29839fd829dc55b29f378da1982aadd5`

Both identifiers were rechecked at completion. The plan is untracked, so the SHA-256 identifies the reviewed document independently of HEAD.

This was a static, read-only review against the requested core files, plugin services, SBot composition and ARCHITECTURE.md §6a. No files were modified, network calls made, tests executed or bot processes run. Below, `core/` means `packages/core/src/chatcore/`.

### Disposition of open findings

| Finding | Disposition | Revision-3 assessment |
|---|---|---|
| S01 — User-dependent availability caching | **RESOLVED** | D6 and §3.1 require fresh scope/user checks; linked-group eligibility is now a user check. No shared gate-result cache remains. |
| S05 — Partial availability and composites | **RESOLVED** | §3.1 distinguishes offer eligibility from invocation authorization. §3.3 preserves plain watches and creator-owned poll closure while retaining handler/commit checks. |
| S06 — Optional deployment and compatibility | **PARTIALLY RESOLVED** | Registry-relative declarations and compatibility generation remain appropriate, but collision suffixes can exceed the identifier limit. See R3-04. |
| S07 — Budget admission and accounting | **RESOLVED** | §7.4 includes the crossing completion’s excess, outstanding admitted work and the permitted final completions. Research remains separately accounted. |
| S11 — Shadow coverage and evaluation | **PARTIALLY RESOLVED** | Eligible-operation coverage and disabled-feature scoring are separated correctly. Eligibility for calls outside the router’s candidates still lacks a consistent measurement contract. See R3-05. |
| S12 — Executable hints and command equivalence | **RESOLVED** | The corrected extensions row matches viewer-filtered status access; bot-addressed hints and command shortcuts remain specified. |
| S13 — Continuation invalidation | **PARTIALLY RESOLVED** | Turn storage removes cache-epoch drift, but requester attribution and persistence overlapping purge remain unresolved. See R3-02 and R3-03. |
| R2-01 — Purge epochs reverting | **PARTIALLY RESOLVED** | Removing cached continuation fixes primary/fallback recovery and epoch eviction. The required overlapping-purge case is not covered by deleting existing rows. See R3-03. |
| R2-02 — Mixed scope/user/argument gates | **RESOLVED** | Linked groups moved to the user gate; offered tools may retain restricted variants, with hints and invocation checks. |
| R2-03 — Group-wide scope-cache invalidation | **RESOLVED** | Fresh reads at the repositories’ actual settings scope remove the topic-cache invalidation problem. |
| R2-04 — Incorrect overshoot bound | **RESOLVED** | The revised accounting includes outstanding completions and explicitly permitted finals. Accepting that policy remains U8. |
| R2-05 — Generated-name compatibility | **PARTIALLY RESOLVED** | Normalization, initial truncation and bounded summaries fix the common cases; suffix growth still violates the name grammar. See R3-04. |
| R2-06 — Disabled-feature coverage | **PARTIALLY RESOLVED** | Correct exclusion no longer counts as an execution-coverage miss. However, the new denominator depends on gate observations the candidate-only flow does not produce. See R3-05. |
| R2-07 — Extensions status permissions | **RESOLVED** | §6 correctly describes open, viewer-filtered status access with additional draft visibility for managers, matching `chatcore_extensions/plugin.py:320–343`. |

### New findings

#### R3-01 — Medium: inherited skills renew their own continuation indefinitely

**Location:** plan §4.2, lines 209–212; §4.3, lines 228–242.

**Classification:** confirmed design defect introduced by the continuation write/read rules.

**Failure scenario:** three skills become active. Subsequent unrelated messages inherit those skills, and each response writes them again as its current active set. They therefore always appear on the latest assistant turn and never expire through `sticky_turns` or the 30-minute age check while conversation continues. Because continuation precedes keyword and embedding candidates, those three skills can repeatedly displace newly relevant skills and force fallback loads.

**Evidence:** storage includes all skills active at turn end, including inherited skills. Reading treats presence on a recent assistant turn as renewed eligibility. There is no distinction between fresh activation and inheritance. Compaction does not necessarily help: retained recent turns contain renewed copies.

**Correction:** preserve activation age or remaining lifetime across inheritance. Define which events refresh it—such as a fresh routing match, explicit load or actual use—and do not refresh it merely because the skill was carried forward. Define the counting unit explicitly.

**Acceptance test:** activate three skills, then send unrelated messages at intervals below 30 minutes. Without a qualifying refresh, each skill expires after the declared continuation window, and a newly matched skill enters the initial set without requiring `use_skill`.

#### R3-02 — Medium: assistant turns lack the requester identity required by continuation

**Location:** plan §4.3, lines 231–236; `core/agent.py:99–106`, `:765–792`; `core/attribution.py:187–198`; `core/conversation.py:151–165`.

**Classification:** incorrect assumption about existing storage, with a missing serialization contract.

**Failure scenario:** a threading-disabled group alternates requests from two people. The continuation reader filters assistant rows by requester, but those rows contain no requester identity. Treating absent identity as ineligible disables continuation; treating it as unrestricted mixes users. Recovering identity from nearby user rows is unreliable because the bounded recent window can contain assistant/tool rows after their corresponding user row has fallen out.

**Evidence:** `user_turn_content` stores `speaker`, but `_turn_data` stores only `text` and `tool_calls`. Assistant rows retain the triggering human `message_id`, but the plan specifies neither an identity lookup nor new requester metadata. `recent()` limits rows across all roles before any proposed requester filtering.

**Correction:** specify a trusted requester key on continuation-bearing assistant rows, populated from the incoming message, or an explicit reliable lookup through the triggering message ID. Keep requester identity distinct from assistant speaker attribution. Legacy rows without recoverable identity should produce a continuation miss.

**Acceptance test:** alternate users with threading disabled, including a tool-heavy response whose user row falls outside the recent window. Each requester inherits only their own continuation. Legacy assistant rows fail closed.

#### R3-03 — Medium: deleting stored turns does not fence an overlapping turn’s later persistence

**Location:** plan D12 and §4.3, lines 237–242; `core/privacy.py:185–200`; `core/conversation.py:741–785`; `core/agent.py:763–792`; `core/app.py:632–664`.

**Classification:** missing lifecycle integration; an existing persistence race invalidates the new authoritative-invalidation claim.

**Failure scenario:** a DM response reads existing continuation and waits for its provider completion. The requester confirms `/forgetme`; existing conversation turns are deleted. The response then finishes and persists its user/assistant rows, including inherited skills. The next request can reuse continuation from work that overlapped the completed deletion.

**Evidence:** `purge_user` deletes rows present when its statements execute. Agent persistence occurs afterward and has no purge-generation check. Confirmation callbacks do not acquire the responder’s turn lock. Telegram enables concurrent updates (`chatcore_telegram/__init__.py:86`), and DM turns have no conversation foreign key whose deletion would reject the late insert.

**Correction:** define ordering or an authoritative persistence fence between purge and continuation-bearing writes. A pre-purge turn must either finish persistence before deletion completes or have its later persistence rejected. The guarantee must survive the supported cache failure/recovery path; merely adding a cache-only epoch would recreate R2-01.

**Acceptance test:** pause a DM response after continuation is read, complete `/forgetme`, then release the response. No pre-purge continuation becomes readable afterward. Repeat for `/purgechat` and with Valkey unavailable or recovering.

#### R3-04 — Low: generated-name collision suffixes can exceed 48 characters

**Location:** plan §3.1, line 95; §3.2, lines 139–146; `core/plugin.py:51–58`, `:81–85`.

**Classification:** confirmed residual compatibility defect in the revised algorithm.

**Failure scenario:** ten distinct legacy plugin names normalize or truncate to the same 40-character stem. The base name is 46 characters after `_other`; the tenth allocation adds `_10`, producing 49 characters and triggering startup validation.

**Evidence:** truncation occurs before collision handling, and the suffix has no reserved space or final length check. Existing plugin names have no corresponding restriction.

**Correction:** reserve space for the complete suffix on every allocation attempt, or use another deterministic bounded naming scheme. Check final length and uniqueness against explicit and generated names.

**Acceptance test:** register at least ten legacy plugins sharing a normalized 40-character prefix, plus explicit-name collisions. Every generated identifier remains unique and at most 48 characters, and every tool remains reachable.

#### R3-05 — Medium: candidate-only gate evaluation cannot supply the new coverage denominator

**Location:** plan §3.1, lines 111–114; §4.2, lines 209–215; §9, lines 467–481.

**Classification:** missing measurement contract and contradictory evaluation scope.

**Failure scenario:** shadow routing misses `polls`, while the legacy model calls an enabled `create_poll`. Candidate-only evaluation never checked that tool’s gate. Excluding unobserved gates from `eligible_calls` removes precisely the miss the metric should penalize; assuming eligibility misclassifies disabled requests. Evaluating the gate after execution cannot establish its state at route time if settings changed meanwhile.

**Evidence:** §3.1 says gates run **only** for candidates’ tools. §9 defines eligible calls using availability **at route time**, including calls produced later by shadow mode’s full tool list. “Shadow mode evaluates gates too” does not specify the broader observation set needed for missed candidates.

**Correction:** define an eligibility snapshot independent of candidate selection for shadow measurement—for example, evaluate all relevant registered tools at route time in shadow mode, deduplicating shared checks. Alternatively, change the observation-time definition explicitly and handle intervening changes. An unevaluated gate must not silently remove a call from the denominator. Account separately for shadow-only gate work when comparing routing latency.

**Acceptance test:** use identical shadow calls with a router that deliberately misses polls. Enabled `create_poll` must enter the denominator and fail initial coverage; disabled `create_poll` must enter unavailable-request evaluation without coverage credit. Include a settings change during the provider call and verify the declared observation-time rule.

### Decisions for the user

These remain choices rather than defects:

- U1–U3: approve the hybrid router, defer an LLM classifier, and retain web tools in the general set, subject to rollout evidence.
- U4–U5: approve the separate persona-permission fix and defer menu trimming until replacement paths are demonstrated.
- U6: approve the additive plugin contract after correcting generated-name allocation.
- U7: choose confirmed NL `/new` or command-only `/new`.
- U8: approve the capped live evaluation and the corrected overshoot policy without token reservations.

### Readiness

Revision 3 resolves the scope-cache invalidation problem, argument-dependent gate ambiguity, overshoot arithmetic and extensions-status error. **R3-01–R3-03 and R3-05 remain material to design approval and detailed implementation planning; R3-04 is a small compatibility correction.**

Fresh gates’ actual cost remains a rollout measurement, not an established performance failure. Implementation must honor freshness through underlying authorization helpers as well as the new gate layer: SBot’s existing `group_for_dm` path ultimately uses the one-hour membership cache in `AccessPolicy.member_groups`.

The fixed DeepSeek Flash model, Claude-plans/Codex-builds workflow and ARCHITECTURE.md §6a require no changes.

## Round 4 — Revision 5 re-review, 2026-11-01 (Asia/Singapore)

**CHANGES_REQUIRED**

- Reviewed plan: `plans/skill-layer.md`, revision 5.
- SHA-256: `c38a82f85d2eec7111cbda5f6bfbe745607ae0860bf4222170978f5c68ec893b`
- Git HEAD: `77af71eb29839fd829dc55b29f378da1982aadd5`

Both identifiers were rechecked at completion. The plan is untracked, so its SHA-256 identifies the reviewed document independently of HEAD.

Static, read-only review of the prior reports, revision 5, the injection-heuristic brief, requested core files, and relevant plugin/composition code. No files were modified, network calls made, tests executed or bot processes run. Below, `core/` means `packages/core/src/chatcore/`.

### Disposition of open findings

| Finding | Disposition | Revision-5 assessment |
|---|---|---|
| S06 — Optional deployment and compatibility | **PARTIALLY RESOLVED** | Registry-relative validation, optional-tool handling and budget clamping remain sound. Generated identifiers now meet the length bound. The remaining explicit-name collision exception narrows the compatibility promise; see R2-05 below. |
| S11 — Shadow coverage and evaluation | **RESOLVED** | §9 specifies an independent route-time eligibility snapshot, separate initial/oracle coverage, unavailable-request scoring and live fallback evaluation. These address the remaining measurement gap. |
| S13 — Continuation invalidation | **RESOLVED** | §4.3 supplies trusted requester metadata, a dedicated scoped reader and triggering-message existence checks. Existing reset boundaries and conversation selection support the normal reset/new paths. The purge fix addresses the previously identified late-persistence case. |
| R2-01 — Purge epochs reverting | **RESOLVED** | Continuation no longer depends on cache epochs. Message deletion in Postgres remains authoritative through cache failure and recovery. |
| R2-05 — Generated-name compatibility | **PARTIALLY RESOLVED** | Normalization, truncation and bounded hash suffixes fix ordinary legacy names and suffix growth. However, §3.2 explicitly rejects collisions with declared skills or hash prefixes, while still promising that legacy metadata is never rejected. Collision behavior is now defined, but unconditional compatibility is not delivered. Treat acceptance of that exception as part of U6. |
| R2-06 — Disabled-feature coverage | **RESOLVED** | Disabled calls receive separate unavailable-request scoring. Enabled missed tools remain in the coverage denominator through the all-tool shadow snapshot. |
| R3-01 — Inherited skills renew forever | **RESOLVED** | Per-skill `left` and `fresh_at` survive inheritance without renewal; refresh events and requester-turn counting are explicit. §4.3 also explicitly prioritizes fresh matches over inherited skills. |
| R3-02 — Assistant rows lack requester | **RESOLVED** | `skill_state.requester` comes from the incoming event, independently of speaker attribution. A dedicated query avoids bounded-context-window loss; legacy rows fail closed. This supplies metadata absent from current `_turn_data`. |
| R3-03 — Persistence after overlapping purge | **RESOLVED** | `app.py:291` logs the triggering message before response execution. `store/pg.py:281–303` deletes it during successful purges; late assistant persistence at `agent.py:785–792` retains that message ID. Requiring the original message to exist therefore rejects the late state. The join must use the existing scoped identity `(chat_key, message_id)`, consistent with the plan’s chat-isolation requirement. |
| R3-04 — Collision suffix overflow | **RESOLVED** | The new allocation is bounded to `32 + 1 + 8 + 6 = 47` characters. Collisions terminate with an error rather than growing the suffix. The separate compatibility exception remains under R2-05. |
| R3-05 — Candidate-only eligibility measurement | **RESOLVED** | Shadow mode evaluates all registered tools before the provider call, retains those observations across settings changes, reports `unevaluated` turns explicitly and separates snapshot cost from routing latency. |

### New findings

#### R4-01 — Medium: `start_new_conversation` has no valid undo contract

**Location:** plan §6.3, lines 458–467; §6, line 342; §10.7, lines 642–646; `core/app.py:507–515`, `:539–551`; `core/threads.py:52–75`.

**Classification:** confirmed incorrect reversibility claim and missing integration with the approved direct-write contract.

**Failure scenario:** the tool sets the requester’s next-conversation marker. The requester changes their mind and continues the existing discussion, including by replying to an earlier message. Their next addressed message consumes the marker and starts a new conversation anyway.

**Evidence:** §6.3 calls the operation “reversible by just continuing.” Existing code does the opposite: `_respond_locked` consumes the marker and passes `force_new=True`; `resolve_conversation` creates a new conversation before considering reply ancestry. Subsequent ordinary messages can then select that new conversation. No cancellation or undo mechanism is specified for this new tool.

The approved `direct=True` class is not the defect. Its application here lacks the promised undo behavior.

**Correction:** define an explicit requester/chat/topic-scoped undo of the unconsumed marker, including how the echo exposes it and what happens after consumption or expiry. Remove the claim that ordinary continuation reverses it. Retain the untrusted-read confirmation fallback.

**Acceptance test:** activate the tool, undo before the next addressed message, and verify normal selection of the existing conversation. Without undo, the next message starts a new conversation even when replying to an older message. Undo cannot affect another requester/topic; consumed or expired actions return a defined outcome. After an untrusted read, no marker exists before confirmation.

#### R4-02 — Medium: rendered-fetch guidance requires an optionally absent tool

**Location:** plan §6.2, lines 410 and 444; §2, lines 42 and 51; `apps/sb_bot/src/sb_bot/app.py:272`, `:378–380`, `:434–439`; `packages/plugin-browse/src/chatcore_browse/plugin.py:75–116`; browse `service.py:107–139`.

**Classification:** confirmed deployment-compatibility regression introduced by the description/overlap pass.

**Failure scenario:** a deployment has Postgres and enabled rendered fetching, but no Tavily key. A requester supplies a permitted URL. The model has `fetch_rendered_page`, but its new instructions permit using it only after `fetch_web_page` returns empty or unreadable text. That prerequisite cannot be satisfied because `fetch_web_page` is not registered.

**Evidence:** SBot registers the web plugin only when Tavily is configured. Browse registration is independent, and its rendered-fetch handler requires authorization, enabled settings and URL provenance—not a previous plain fetch. Revision 5 introduces an unconditional cross-plugin prerequisite that the supported registry can lack.

**Correction:** make the guidance conditional: prefer plain fetching first when that tool is available, while permitting rendered fetching directly for an otherwise authorized, provenance-valid URL when it is absent. Preserve the existing handler checks.

**Acceptance test:** with rendered fetching configured and enabled but Tavily absent, a user-posted URL remains usable through `rendered_pages`. With both tools present, the normal plain-then-rendered fallback works. Disabled rendering and unknown URL provenance remain refused.

#### R4-03 — Medium: stage-1 description rewrites contradict the unchanged-request rollout gate

**Location:** plan §6.2, lines 398–454; §9 stage 1, line 587; §10.2, lines 624–625; §10.16, lines 679–680; `core/tools/registry.py:93–95`, `:112–116`; `core/agent.py:548–552`, `:601–610`.

**Classification:** confirmed rollout-contract contradiction introduced by revision 5.

**Failure scenario:** stage 1 rewrites registered descriptions, adds parameter descriptions and shortens policy-heavy descriptions. The unchanged `off` path immediately sends those revised schemas. Its required baseline request snapshot fails, and switching skills off cannot restore the previous tool descriptions.

**Evidence:** descriptions and parameter schemas are part of `ToolSpec`; normal, final and retry requests obtain them directly from the shared registry. Revision 5 requires these edits in stage 1 while retaining the gate “`off` request snapshot unchanged” and the cross-mode baseline identity check. Preserving the legacy system prompt alone cannot satisfy both requirements.

**Correction:** choose and document one consistent boundary:

- Preserve legacy tool metadata for `off`, `shadow` and excluded chats, applying the rewrites to effective `on` requests; or
- Ship the shared description cleanup independently, establish a new baseline afterward, and explicitly scope skill rollback and snapshot equivalence to that baseline.

Keep comparisons between routing modes on otherwise equivalent tool metadata.

**Acceptance test:** after the description pass, deterministic normal/final/retry requests satisfy the chosen baseline contract in `off`, `shadow` and excluded chats. Switching modes restores exactly the documented rollback behavior, and routing evaluation does not silently compare different description versions.

### Decisions for the user

- **U1–U3:** the hybrid router, deferred classifier and general web-tool set remain choices subject to rollout evidence.
- **U4–U5:** the separate persona-permission fix and deferred menu trimming remain choices.
- **U6:** approve the additive plugin contract and explicitly decide whether generated-name collisions may fail startup. Accepting that exception requires narrowing the unconditional legacy-compatibility wording; preserving the original promise requires bounded collision resolution.
- **U7:** extending the approved direct-write class to NL `/new` remains a choice, conditional on a real undo contract under R4-01. Pending or command-only handling remain alternatives.
- **U8:** capped live evaluation and the stated budget-overshoot policy remain choices.

DeepSeek Flash, the Claude-plans/Codex-builds workflow, §6a and the already-approved `direct=True` class are fixed choices, not findings.

### Concerns not promoted to defects

- **Late persistence after privacy deletion:** the pre-existing race can still write conversation rows after a purge. The new triggering-message existence requirement prevents those rows from supplying continuation state, so a general persistence-fence fix can remain separate.
- **Membership freshness:** §4.3 now acknowledges that linked-group gates reuse `AccessPolicy.member_groups`, including its existing one-hour cache. Immediate platform-membership freshness is therefore not established; this is not a newly introduced authorization cache. D6/check 3 should be read consistently with that declared limitation.
- **Injection heuristic:** no material design defect found in §9.1. Its bounded, log-only behavior, restricted log fields and unchanged-content checks match the brief. Detection quality and regex cost remain implementation verification work; routing does not depend on detection success.

### Readiness

Revision 4 addresses the five round-3 findings. Revision 5 introduces **three material corrections: an actual `/new` undo contract, deployment-aware rendered-fetch guidance, and a consistent description/baseline rollout boundary**.

Resolve R4-01–R4-03 before design approval and detailed implementation planning. The remaining generated-name compatibility exception needs an explicit U6 disposition. Stage 0b remains independently plan-ready; no implementation or runtime success is asserted by this review.

## Round 5 — Revision 6 re-review, 2026-11-01 (Asia/Singapore)

**PASS_WITH_NOTES**

- Reviewed plan: `plans/skill-layer.md`, revision 6.
- SHA-256: `c4d1b1f0fdafc0f4ce47603515f96aa8c75e76879a95e3e27ba9ef925fef74b4`
- Git HEAD: `77af71eb29839fd829dc55b29f378da1982aadd5`

Both identifiers were rechecked at completion. The plan is untracked, so its SHA-256 identifies the reviewed document independently of HEAD.

Static, read-only review of the open findings, revision-6 changes and relevant code. No files were modified, network calls made, tests executed or bot processes run. Below, `core/` means `packages/core/src/chatcore/`.

### Disposition of open findings

| Finding | Disposition | Revision-6 assessment |
|---|---|---|
| R4-01 — `/new` undo contract | **RESOLVED** | §§3.3, 6 and 6.3 consistently recommend a pending `start_new_conversation`; check 7 prohibits setting the marker before confirmation and scopes it to the requester’s chat/topic. The incorrect reversibility claim is removed. This matches `core/app.py:539–551` and `core/threads.py:52–63`: consuming the marker forces a new conversation before reply ancestry is considered. The optional direct version now explicitly requires scoped marker undo and a defined consumed/expired outcome. |
| R4-02 — Rendered fetch without `fetch_web_page` | **RESOLVED** | §6.2, lines 412 and 446, makes guidance registry-dependent: plain-first when registered, direct rendered fetching with valid provenance when absent. SBot independently registers browse and Tavily-backed web tools (`apps/sb_bot/src/sb_bot/app.py:378–380`, `:434–439`). Browse’s handler checks authorization, settings, URL validity, provenance and limits, without requiring a preceding plain fetch (`chatcore_browse/service.py:107–139`). |
| R4-03 — Description pass versus unchanged-request gate | **RESOLVED** | §6.2’s rollout boundary moves shared metadata changes into stage 0a, before the baseline week. Removed policy guidance transfers to the legacy prompt in the same change. §9 and checks 2/16 consistently use the resulting baseline; skill rollback explicitly retains stage 0a. This fits the shared `ToolSpec` path used by normal, final and retry requests (`core/tools/registry.py:93–116`; `core/agent.py:548–552`, `:601–610`). |
| R2-05 — Generated-name compatibility | **RESOLVED — design contract** | §3.2 now explicitly qualifies legacy compatibility with the collision exception. Bounded naming and deterministic failure remain specified, and check 1 covers forced collisions. The contradictory unconditional promise is removed. Acceptance of startup failure remains U6; this review does not infer user approval. |
| S06 — Optional deployment and compatibility | **RESOLVED — design contract** | Registry-relative validation, missing-tool handling, budget clamping and compatibility generation remain specified. The remaining collision-policy qualification is now explicit and assigned to U6, rather than presented as unconditional compatibility. |

### New findings

None. No new material defect was identified in stage 0a, the rollout boundary, pending `start_new_conversation`, registry-conditional rendered-fetch guidance or the stated collision exception.

### Decisions for the user

- **U6:** approve the additive plugin contract and its explicit generated-name collision exception. The policy is now reviewable; acceptance remains outstanding.
- **U7:** choose pending NL `/new` as recommended, the direct alternative with its specified undo contract, or command-only handling.
- **U1–U5 and U8:** remain the choices recorded in §11, subject to their stated rollout evidence and dependencies.

DeepSeek Flash, the Claude-plans/Codex-builds workflow, §6a and the approved `direct=True` class require no changes.

### Concerns not promoted to defects

- **Stage 0a verification:** detailed implementation planning should include checking that removed policy guidance reaches the legacy prompt in the same change. Description-length and parameter-description tests alone do not establish that semantic preservation.
- **Rendered-fetch verification:** retain the round-4 deployment cases in implementation tests: rendering enabled with Tavily absent, both fetch tools present, rendering disabled and invalid provenance. The revised design supports all four.

### Readiness

Revision 6 is ready for design approval and detailed implementation planning. No material design corrections remain within this review’s scope. Record the user’s U6/U7 choices before implementing those branches of the design.

This verdict establishes design readiness only; it does not assert implementation completion or runtime success.
