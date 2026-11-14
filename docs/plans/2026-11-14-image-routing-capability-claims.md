# Image follow-up routing and capability claims: proposed design and implementation plan

**Status:** [PR #48](https://github.com/keenlok/clawdot-platform/pull/48), v0.17.0 release candidate on `codex/image-routing-repair` (base `551aeb0`). Claude
implemented both slices and fixed all five review findings; independent Codex review and Claude
final review found no code blockers. The owner explicitly authorized merge and release on
2026-11-14, including SBot adoption through the existing CI-gated deployment path. This supersedes
the historical diagnosis-only and blocked records below. Credentials, photo permissions and
image-generation spending are unchanged; no paid image trial or manual chat message is authorized.

**Goal:** Make explicit, contextual image requests reach the existing image skill, while
keeping unsupported setup/quota explanations out of replies and preserving photo permissions.

**Architecture:** Repair bounded skill discovery and expose fresh eligibility separately
from tools offered this turn. Reuse requester-scoped continuation and validated photo context
for image follow-ups. Loading a skill offers tools; it never authorizes or executes an image.

**Stack:** Existing Python, skill declarations/gates, conversation state and fake providers.
No new dependency or migration is proposed.

**Roles:** Codex investigated and authored the proposal (original record below). The owner
approved it and named Claude as executor, overriding the default Codex executor in
`ARCHITECTURE.md` §7b. Claude refined the interfaces, implemented both slices and self-reviewed
the diff; Codex subsequently reviewed it and found the issues recorded below. The original proposal noted that a Claude planning attempt had
failed with an expired OAuth session; the owner fixed that before this execution.

## Evidence and limits (original investigation record)

Source baseline: platform `origin/main` at `551aeb0` on 2026-11-14. The shared workspace is
on `claude/release-v0.8.0` (`5ff6af0`) with unrelated existing documentation changes. Those
changes were preserved. Source was inspected through Git and a temporary archive, without
switching branches or changing implementation files.

The live container was created on 2026-11-14 at 01:42:52 Asia/Shanghai (2026-11-13 17:42:52
UTC). SHA-256 checks of installed `chatcore.agent` and `chatcore_image.plugin` exactly matched
`origin/main`, including the post-v0.14.0 explicit-reply photo policy. The deployment checkout's
v0.14.0 adoption commit alone is therefore insufficient to identify the running framework.
Installed package metadata reports `0.1.0` and is also insufficient. The old failing process's
source identity is not established by these current-file checks.

| Observation | Confidence and implication |
| --- | --- |
| Saved trace for “try again” at 2026-11-14 01:31 Asia/Shanghai has four `read_explainer` and four `read_chat_history` calls, no `generate_image`, and empty skill continuation. The reply claimed quota was spent. | Prior investigation supplied with this task; no image-provider or image-quota failure is established. Eight ordinary calls could consume the default agent tool-step budget, but the exact final-forcing reason is not proved. |
| Saved trace for “iron man pls” at 01:32 has three explainer calls, no `generate_image`, and empty skill continuation. Image overview/limitations results explicitly say `generate_image: available`; the reply claimed disabled/missing key. | Prior saved evidence contradicts the categorical explanation. Gate availability does not prove provider health or remaining image quota. |
| The running bot has `SKILLS_MODE=on`, default router `embed`, and image generation/editing enabled. | Fresh read-only check. This does not establish historical embedding readiness, scores or image-provider availability. Credentials were not displayed or changed. |
| `SkillRouter.route(text)` considers only the first 500 characters of current text. | Source fact in `packages/core/src/chatcore/skill_router.py`. An offline invocation of the extracted current keyword matcher returned no image hit for “try again”, “iron man pls” and “can u do so”; “make this photo a watercolour” did hit. Embedding misses for the historical turns remain a hypothesis. |
| `SkillTurn.build()` runs before `_context()` resolves photos. | Source fact in `agent.py`. Existing `pin(ctx)` runs while attachment/owner/reply provenance fields are still empty; adding an image pin that reads those fields alone cannot work. |
| Carryover reads the latest same-requester record within the conversation/topic and 30 minutes; default stickiness is two requester turns. | Source fact in `conversation.py` and `skill_turn.py`. Another person's image request does not transfer skill state. A newest empty record supersedes an older populated record; explainer reads do not refresh image skill state. Preserve this isolation. |
| The loader is `use_skill`, not `load_skill`. Its catalog stops at 1,000 summary characters and its argument enum includes only displayed entries. | Source fact in `_refresh_use_skill_spec()`. Offline execution with 21 eligible skills, image last, exposed only 17 and omitted image. This proves a discoverability defect; it does not prove that exact historical requests omitted image. SBot appends image late in plugin wiring. |
| The image skill summary says “Create a picture from a description”. | Source fact in image `plugin.py`. The summary shown for dynamic discovery omits editing, although tool text and loaded instructions support it. |
| `read_explainer` is a general tool; it returns generic setup prose followed by runtime eligibility, all marked untrusted. It does not load the image skill. | Source fact in `explain_plugin.py` and `skills.py`. Generic defaults can compete with current facts. Runtime eligibility is different from a tool being offered to the model now. The existing prose already asks for that distinction, but did not prevent the observed replies. |

The original full model requests, router metrics and old container logs were not recovered.
Do not promote catalog truncation, embedding failure, instruction hierarchy confusion or a
particular deadline/step cap to a proven historical root cause. The proven behavioral failure
is that the bot neither loaded nor attempted the image capability and gave unsupported reasons.

## Options

| Option | Benefit | Cost or limitation |
| --- | --- | --- |
| A. Discovery and evidence guidance only | Smallest patch: repair catalog, improve image summary, give an explicit skill-load route and distinguish setup defaults from runtime facts. | Depends on the model interpreting conversation context; may still miss terse requests. Useful first independently testable slice. |
| B. A plus bounded contextual image routing — recommended | Handles reference-photo follow-ups using validated scope, current human intent and requester-local state; keeps existing gates and tool execution. | Needs careful ordering of lightweight context resolution before pins, plus positive and negative replay cases. |
| C. General conversation-aware semantic router | Could improve terse follow-ups across all skills. | Larger core/API and evaluation surface; adds ambiguity, latency and potentially inference cost. Not justified by this incident alone. |

Implement B in two reviewable slices, starting with A. Do not expand global keywords with
“try again”, character names or “can u do so”, add image generation to general tools, increase
all budgets, change providers, or transfer skill state between requesters.

## Proposed behavior

### 1. Discoverability and capability evidence

Keep the `use_skill` name enum complete for all currently loadable, visible skills within the
existing maximum skill count (verify its actual bound before implementation). Preserve the
1,000-character human description budget by shortening or omitting summaries, rather than
removing legal names. Put the loadable names in the schema enum even when summaries do not fit.
If the framework has no suitable catalog-count bound, add an explicit bounded discovery/paging
contract before accepting an unbounded schema; do not silently drop late skills again.

Change the image summary to cover creation and photo editing. For a named explainer topic,
provide a gate-checked mapping from capability/tool to loadable skill and distinguish:

- configured/registered and eligible for this caller/chat;
- offered this step or needing `use_skill`;
- operation outcome, including quota or provider failure, only when actually returned.

Put eligibility prominently ahead of setup-default prose. In trusted agent guidance, require
the model to treat validated runtime evidence as authoritative for availability, load the
mapped skill when an explicit action needs it, and avoid another explainer lookup as a proxy
for execution. Keep documentation untrusted. Any new trusted runtime hint must be constructed
from internal gate states, skill identifiers and bounded enums, never copied from documentation
or user text. Implement an internal, non-model-writable capability observation only if needed
to bridge the explainer result into the next step. Do not parse free-text tool results into
trusted state.

An explainer read must remain read-only. It must never generate an image or silently execute
an example; automatic loading is also unnecessary when `use_skill` is discoverable. A question
such as “can you edit photos?” gets an accurate answer without generation.

Use precise guidance: missing tools in the current offer do not imply disabled plugins;
configuration defaults do not describe this deployment; an agent step/deadline limit is not
the image quota. State quota exhaustion, missing configuration or provider failure only from
matching runtime/tool evidence, and distinguish an unattempted request from a failed attempt.
Prompt guidance reduces false claims but does not deterministically prevent arbitrary model
text. Do not claim a guaranteed prevention mechanism without separately designing one.

### 2. Contextual follow-ups

Extract lightweight, scoped reference resolution from `_context()` so the same resolved
metadata can be supplied before `SkillTurn.build()` and reused during prompt/tool preparation.
Do not download or describe images during routing; do not create a second reply resolver with
different permission rules. Preserve exact-message lookup, bot-generated receipt handling,
requester attribution, chat/topic checks and explicit direct-reply provenance.

Represent current human text and reference metadata through a small generic routing context
available to plugin pin hooks, rather than hard-coding `chatcore_image` imports into core.
Image-specific intent rules remain in the image plugin or an image helper module. Never trust
image bytes, OCR text or transcript content as instructions for loading or executing tools.

Use an eligible current/direct-reply photo plus an actual action request to activate the image
skill for a contextual edit. A terse style/subject request such as “iron man pls” is actionable
only when its photo/edit context resolves unambiguously. “Try again” and “can u do so” require
an identifiable prior image request from this requester or an explicit same-topic reply that
resolves that request; generic retries for another task must not activate image generation.
Where semantics remain ambiguous, load/expose the relevant skill if useful and ask one concise
clarification; do not generate merely because a photo exists. A deterministic rule set cannot
infer arbitrary subject/style intent from all short utterances: test the named supported
patterns explicitly and use model context for the remainder rather than claiming broad coverage.

Retain same-requester sticky state. When no skill was ever active, stickiness cannot rescue
the request: fix initial discovery/context, rather than extending the TTL or falling back to
another person's populated record. Do not introduce persistent pending-intent state in the
first implementation. If replay shows that needed intent is otherwise lost, return with a
separate bounded state proposal covering requester, topic, reset, source message and expiry.

An inherited image skill does not preserve permission to use an old photo. If the current
message/reply has no eligible reference for a requested edit, ask the requester to upload or
reply directly to the intended photo. Do not silently make a text-only substitute or mine
ambient chat photos. A clearly requested new text-only picture can still use generation.

### 3. Photo permission compatibility

The historical v0.14.0 rule required requester-owned group photos. Current live source includes
platform PR #47: another person's photo is usable only through an exact, validated direct
reply; requester attribution on generated outputs remains tracked. This investigation does
not authorize reverting or broadening either policy. Implement against the approved execution
baseline and preserve its handler checks. If the owner requires restoring requester-only edits,
treat that as a separate permission decision and PR, not a routing repair.

## Implementation tasks (original proposal; outcome in the implementation record)

- [ ] **1. Freeze the execution baseline and replay fixtures.** Start an isolated worktree from
  the reviewed current framework baseline; record commit and actual installed-file identity.
  Reconstruct the two failed turns using normalized aliases and stubbed provider/history
  results. Preserve exact utterances, requesters, reply relationships and tool ordering; do not
  commit private chat identifiers, credentials or real photos. The inherited investigation
  record is evidence, not a complete replay fixture: retrieve missing saved metadata read-only
  or mark it absent. Add failing cases to image `tests/test_edit_turn.py` and core
  `tests/test_agent_skills.py`/`test_skill_turn.py`. Include a long catalog with image last.
- [ ] **2. Repair loader discovery.** Modify core `skill_turn.py` and its tests so every visible
  loadable skill is legally selectable despite summary truncation. Test long summaries, image
  last, OFF/NOT_ALLOWED hints, hidden UNCONFIGURED skills, max loads, empty catalog and changes
  after a load. Update image `plugin.py` summary. Keep current gate evaluation and next-step
  offering semantics; loading must not bypass the request snapshot for the current step.
- [ ] **3. Repair capability guidance.** Modify core `explain_plugin.py`, `agent.py` and tests
  `test_explainers.py`/`test_agent_skills.py` to supply prominent fresh eligibility, skill mapping
  and bounded trusted follow-up guidance where necessary. Update image `EXPLAIN.md` to explain
  that setup defaults are not deployment status. Test available-but-not-offered, already loaded,
  off, not allowed, unconfigured, observation failure and stale historical false claims. A pure
  explanation must not call the image service. Do not change credentials, access gates or quota
  accounting. If the observation bridge changes public `ToolResult`/`TurnState`, document that
  contract and cover malformed or forged observations before extending it.
- [ ] **4. Supply validated routing context.** Refactor core `agent.py` and, if needed,
  `tools/registry.py` to resolve photo metadata once before pins. Extend tests
  `test_skill_context_pin.py` and image `test_edit_turn.py` for the ordering contract. Build
  image-specific contextual pin rules in image `plugin.py` or a focused helper. No core import
  of the image package; no plugin cross-import; no image-byte fetch before routing.
- [ ] **5. Run the complete regression matrix below.** Keep service calls and platform sends
  fake. Use image `test_plugin.py`/`test_service.py`, core `test_skill_continuation.py`,
  `test_skill_router.py` and `test_agent_skills.py`. Confirm OFF mode/request snapshots and skill
  catalog tests remain stable except for intentional declaration/prompt changes. Locate the
  routing corpus in the execution baseline; this inspected snapshot has no tracked
  `routing_set.jsonl`, so do not invent a path or claim it was updated.
- [ ] **6. Document and review.** Add the user-visible impact to `CHANGELOG.md` under Unreleased,
  update this index row with delivery status/PR, and record any accepted architecture contract
  in `ARCHITECTURE.md` without renumbering sections. Name executor/reviewer roles, dependencies
  (none expected), permission changes (none expected) and unavailable checks in the PR. Core
  changes require maintenance review. Obtain the §7b Codex review and Claude final review;
  record any model availability fallback explicitly.

Each implementation task starts with a failing regression, makes the smallest change for that
case, and reruns the affected tests before proceeding. The task list is a proposed design-level
plan, not an approved line-by-line patch. Final interfaces should be reviewed after the fixture
and catalog-count investigation, before implementation.

## Acceptance matrix

| Scenario | Required result |
| --- | --- |
| Exact short follow-ups in established image-edit context | Image skill offered/loaded; a scripted action reaches one fake `generate_image` call with the correct reference. No explainer/history cycle. |
| Same phrases without image context; retry of another task; “thanks”; capability question; photo with no action request | No image generation. Clarify only when necessary. |
| Requester changes | No inherited skill or pending action from another requester. A valid explicit photo reply is evaluated under the baseline's own policy. |
| Requester returns after another requester speaks | Their own live continuation remains selectable; another user's newest state does not overwrite it. |
| Empty newest same-requester state, expiry, reset, different topic/chat | No revival of stale or foreign intent/reference. |
| Own current photo, own reply photo, tracked generated-photo reply | Preserve reference numbering, requester attribution and edit behavior. |
| Other person's exact direct reply vs ambient/indirect reference | Preserve current baseline's handler policy; no broader photo use. |
| Missing/stale/untracked output reference; edit disabled | No unsupported edit or text-only substitution; accurately explain/ask for the required source. |
| Eligible image skill late in a >1,000-character catalog | `use_skill` schema still permits `image_generation`; next step offers only gated tools. |
| Generic docs say “disabled by default”; runtime says available | Runtime eligibility is prominent and the skill-load path is exposed; no setup-default inference in trusted guidance. |
| Actual fake quota/busy/refusal/unavailable errors | Explain the matching result accurately; one attempt per turn; unchanged budget reservation and permissions. |
| Agent tool-step/deadline exhaustion before any image attempt | Say the request was not attempted/finished; do not label it image quota or provider failure. |

ScriptedProvider tests prove routing, schema, context and execution wiring. They do not prove
DeepSeek will choose the correct call or avoid false prose. If the owner authorizes later model
qualification, replay normalized conversations through the actual chat model with image
execution replaced by a local recorder and all sends disabled. Include positive and negative
cases, count tool choices and unsupported claims, and report actual results before proposing
rollout. No paid image generation or real chat message is required for that qualification.

## Validation and rollout boundaries

For a future implementation PR run the repository-required checks:

```sh
uv sync --locked --all-packages
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check .
uv run pytest -q -m "not browser and not sandbox_integration"
```

Format only changed files where main already has format failures; record the baseline failures.
Run Postgres-backed continuation tests with the test DSN when available, and report skipped
integration checks. Preserve `packages/core/tests/snapshots/agent_requests_off.json` unless an
intentional, reviewed behavior change requires an update. Run package-boundary, explainer
catalog and plans-index checks. No implementation suite was run for this diagnosis-only work.

Documentation verification for this proposal: `uv run --no-sync pytest -q
tests/test_plans_index.py` passed (5 tests); `git diff --check` passed; an explicit check also
confirmed the new, untracked plan is indexed and that its/index local links resolve.

This session performed source inspection, read-only live configuration/source identity checks,
two offline source-method reproductions, and a failed Claude planning invocation. It changed
only this proposed record and its index entry. No production repair, rollout or live-model
qualification was performed. No UI was used, so no screen-lock action was needed.

After implementation approval and review, framework release and downstream adoption must be
separately authorized. Pin the reviewed platform revision, preserve SOPS/release-config env
regeneration, and leave the separately fixed credential persistence path alone. Rollback means
reverting the reviewed routing/guidance release, never changing photo permissions or runtime
`.env` as an ad hoc workaround.

## Implementation record (2026-11-14)

Everything above this heading except the status and roles paragraphs is the original proposal
and investigation record. This section records what was built. All tasks 1–6 were carried out
except the parts listed under "Not done".

### Baseline findings

- Worktree at `551aeb0`, clean apart from this plan. Targeted baseline (skill, explainer, image
  and root tests): 509 passed, 11 skipped, 1 failed. The failure was
  `tests/test_plans_index.py::test_every_plan_is_indexed`, because this plan was copied in
  without its index row.
- `ruff check`: clean. `ruff format --check .`: 16 files already unformatted on the baseline;
  none of them is touched here.
- **Catalog bound:** the framework had no limit on the number of skills, so a complete
  `use_skill` enum would have been unbounded. The plan required an explicit bound in that case.
- `SkillTurn.build()` ran before `_context()` resolved photos, and `Skill.context` hooks also
  ran before `TurnState.attachment_owners` was set, as the investigation said.
- No `routing_set.jsonl` exists in this repository, so none was updated.
- The replay fixtures use the exact utterances with fake people, photos and providers. The
  historical reply relationships and request metadata were not recovered and are not invented:
  tests cover the supported contexts, not a reconstruction of the two failed requests.

### Interfaces as built

| Area | Interface |
| --- | --- |
| Catalog bound | `chatcore.skills.MAX_CATALOG_SKILLS = 64`; `SkillSet.build` raises `ValueError` above it. |
| `use_skill` | Enum is every loadable skill. Description: full lines if they fit 1,000 characters, else all summaries shortened to one common length (minimum 16), else names only. |
| Routing context | `chatcore.tools.registry.RoutingContext(text, reply_author, reply_text, reply_bot_image)` on `TurnState.routing`. |
| Reply resolution | `AgentResponder._reply_scope()` runs once before pins; `_context(..., reply_scope=)` reuses it. `TurnState.attachments`, `attachment_owners` and `explicit_reply_attachment_ids` are set before `SkillTurn.build`. |
| Offer state | `TurnState.offered_tools` (`None` unless skills are on), set by core each step. |
| Explainer | `ExplainPlugin(lookup_skills=)`, `SkillSet.skills_for_tool()`; availability block first, with "in your tools now" or the skill to load. |
| Trusted guidance | Skills-on only: extended "More skills" text, a "Capability claims" section, and one sentence added to the forced-final instruction. |
| Image plugin | `chatcore_image/routing.py` (`KEYWORDS`, `FOLLOW_UPS`, `wants_image`, `is_image_request`, `usable_references`, `pinned`, `reference_note`); skill `pin` and `context`; edit-aware summary. |
| Photo rule | `permissions.may_use_reference()` holds the handler's existing rule, moved unchanged so the pin and handler share it. |

### Decisions and deviations

- **No observation bridge and no parsed tool text.** The explainer reads `TurnState.offered_tools`,
  which only core writes; a forged or odd value can at most change the wording of an already
  gate-checked line. Hidden, admin-only and unconfigured tools stay hidden (tested).
- **`permissions.py` gained a function.** It is a behaviour-neutral move of the handler's
  condition; the policy (DM any photo; group own photo or exact direct reply) is unchanged.
  Flag it as a permission-file change in the PR.
- **Reset isolation:** a replied-to request older than the last context reset supplies no author
  or text to pins. An explicit photo reply keeps working across a reset, as before.
- **Retry phrases** need a directly replied-to generated photo or the requester's own
  replied-to image request. Without a reply they rely on existing same-requester stickiness only.
  A bare "try again" with no reply and no live state therefore still activates nothing; the
  model can load the now-discoverable skill or ask. No pending-intent state was added.
- **Terse requests** ("iron man pls") activate with a usable photo or own replied-to request:
  at most four words plus a politeness word, excluding thanks, questions and a short list of
  other tasks (read, translate, summarise, receipt…). This is a narrow heuristic, not intent
  understanding.
- **Skills-off prompts are unchanged**; `agent_requests_off.json` passes unmodified. One
  existing test (`test_use_skill_enum_evaluates_catalog_and_excludes_unconfigured`) asserted the
  truncated enum and was updated to the new contract.
- `/explain <topic>` output order changed for people too (availability first).

### Not done

- No real-model qualification: scripted providers prove wiring, not model choices or wording.
- No Postgres- or Valkey-backed run: `TEST_PG_DSN` was unset and no local Postgres answered, so
  those tests skipped (57 skips in the full run, including the Postgres skill-continuation
  cases). The memory-store paths ran. CI runs them with Postgres.

### Verification

`uv sync --locked --all-packages` succeeded; `ruff check` passed; `ruff format --check .` listed
the same 16 baseline files and no changed file; `pytest -q -m "not browser and not
sandbox_integration"` gave 4450 passed, 57 skipped, 29 deselected, 1 xfailed. Before the
implementation the new tests failed (49 failures) for the missing behaviour.
- No commit, PR, release or downstream adoption. No change to the private deployments repo.

### Codex review and first (blocked) fix round

Codex and a read-only reviewer confirmed five issues after the first implementation pass:

1. `agent.py` says a missing offered tool is not disabled, although the loader includes OFF
   and NOT_ALLOWED skills. Absence alone establishes neither enabled nor disabled status.
2. Image `routing.py::is_image_request` uses substring matching rather than the router's word
   boundaries: “withdraw a payment” is incorrectly classified as an earlier image request.
3. Image routing incorrectly activates on photo questions/other tasks such as “is this
   watercolour?”, “describe this anime” and “translate this cartoon”. Activation alone is not
   a paid tool call, but violates the intended action-intent rule.
4. `SkillTurn.use_skill()` does not render the skill's contextual reference guidance, so a
   dynamically loaded image skill lacks the missing-reference/safe-clarification note supplied
   for initially active skills. Share the bounded, error-handled renderer between both paths.
5. The new explainer wrappers can exceed the command's 3,500 UTF-16 limit for valid maximum
   metadata (offline reproduction: 3,546 versus the baseline's 3,288). Bound fixed overhead too.

No additional reply-resolver, permission-policy, catalog-bound or eligibility-mapping defect
was found in that review. Full prior test results above do not invalidate these review findings;
they require new regressions and fixes.

The owner-authorized Claude fix round exited before editing: “You've hit your 5-hour limit”,
with a reported reset at 07:10 Asia/Shanghai. No Codex implementation fallback was taken because
the owner explicitly requested Claude execution. Fix instructions and reproduction details are
preserved at `/tmp/clawdot-image-review-fixes.txt` and
`/tmp/clawdot-image-review-extra.txt` on this host. Resume Claude with those instructions after
its quota resets, then rerun targeted checks, the full suite and review before integration.

### Fix round (2026-11-14, Claude)

The resumed Claude fix round reproduced all five findings with failing regressions (49 failing
tests before any code change), then fixed them. No finding was disproved.

| # | Fix | Where |
| --- | --- | --- |
| 1 | The "More skills" text no longer says a missing tool is not disabled. It says absence proves neither enabled nor disabled, that a listed skill may be off or not allowed and `use_skill` then says so, and to load and attempt an explicit action unless an "Unavailable here" note or the `use_skill` result refuses it. "Capability claims" now needs this turn's runtime availability for off/not-allowed/unconfigured, and the operation's own error result for quota or provider failure; setup documentation, fetched text and earlier messages are named as not evidence. | `agent.py` |
| 2 | `is_image_request` uses the router's matcher, now exported as `chatcore.skills.keyword_matches` and used by `SkillRouter` itself, so the two cannot drift. | `skills.py`, `skill_router.py`, image `routing.py` |
| 3 | `wants_image` rejects other tasks (read, describe, translate, explain, identify, what/which/who…, and Chinese equivalents) before any style match. A question activates only as a polite instruction to change the photo: `can/could/will/would you` (or 能/可以…) or a leading edit verb, plus an edit verb and a target, with a usable photo. | image `routing.py` |
| 4 | `chatcore.skills.render_skill_context` is the single renderer (400 characters, length-only and class-only logging, non-text ignored). The system prompt and `use_skill` both call it; the agent keeps its `chatcore.agent` logger name. A refused (OFF/NOT_ALLOWED) load does not run the hook. | `skills.py`, `skill_turn.py`, `agent.py` |
| 5 | When the full availability wording would exceed the reply limit, a short fixed header and the previous one-sentence note are used; body, source identity and status lines are kept. Reproduced at 3,546 UTF-16 units before the fix. | `explain_plugin.py` |

Also found while writing the regressions, and fixed: `_without_politeness` strips punctuation,
and the old `polite = bare != normalized` comparison therefore treated any short message with a
comma ("the usual, like before", "look, my lunch!") as a polite terse request and activated the
image skill when a photo was present. Politeness is now an explicit flag.

Behaviour notes for review:

- "do it in anime style" stays an instruction; "do you …" is a question.
- A question about a style with no edit verb ("can you do watercolour?", "anime style?") no
  longer activates; the skill stays loadable through `use_skill`.
- `use_skill` results for a skill with a `context` hook are longer by at most 400 characters.
- New public core names: `keyword_matches`, `render_skill_context`,
  `MAX_SKILL_CONTEXT_CHARS`. `ARCHITECTURE.md` and `CHANGELOG.md` are updated. No dependency,
  migration, gate or permission-policy change in this round (`permissions.py` and `gates.py`
  untouched by it).

Verification (this round, local, all providers and platforms fake):

- `uv sync --locked --all-packages`: succeeded.
- `uv run ruff check packages services apps tests .github/scripts`: passed.
- `uv run ruff format --check .`: 16 files listed, the same baseline set; no file changed on
  this branch is among them.
- `uv run pytest -q -m "not browser and not sandbox_integration"`: 4531 passed, 57 skipped,
  29 deselected, 1 xfailed (264.9 s). Skips are the Postgres/Valkey-backed tests
  (`TEST_PG_DSN` unset); deselected are browser and sandbox-integration tests.
- `packages/core/tests/snapshots/agent_requests_off.json` is unmodified; package-boundary and
  plans-index tests pass (26 passed); `git diff --check` is clean.
- During the full run another process changed `pyproject.toml`, `uv.lock` (version 0.17.0),
  the `CHANGELOG.md` release heading and `docs/wiki/Plugin-image.md` in this worktree. Those
  edits are not part of this fix round. Root, skill, explainer, agent and image tests were
  rerun on the resulting tree: 798 passed, 11 skipped. The full suite was not rerun after them.

Not done: no real-model qualification (scripted providers prove wiring, not model wording or
tool choice), no Postgres/Valkey run, no commit, PR, release or deployment.

## Final release validation and authorization

The owner subsequently requested “merge and release” to address recurring missing-tool claims.
Claude's fix round completed, Codex independently re-reviewed all five findings (193 focused
tests passed), and a separate Claude final review found no code blockers. Codex prepared the
v0.17.0 version/changelog metadata and regenerated the packaged explainer wiki page.

Final full suite with disposable local Postgres and `PG_BIN`: **4587 passed, 1 skipped,
29 deselected, 1 xfailed**. This supersedes the earlier no-Postgres limitation. Locked sync,
full Ruff lint, formatting of all 16 changed Python files, generated-explainer consistency,
secret scanning, and 10 platform-release tests pass. Whole-tree formatting still lists the
same 16 pre-existing files; they remain untouched. The skills-off request snapshot is unchanged.

The root version and virtual workspace lock entry both read 0.17.0. Changelog release date is
2026-11-14 with a new Unreleased section and updated comparison links. No registry dependency,
applied migration or gate change is included. `permissions.py` was changed only to share the
existing predicate, and requires the repository's permission-file maintenance review despite
preserving policy. Core changes received the independent reviews described above.

Merge waits for hosted CI/scaffold checks; main CI publishes the immutable tag. SBot adoption
is tracked separately in the private deployment repository's
`docs/plans/2026-11-14-tool-routing-release.md`, using one exact published tag/SHA and unchanged
configuration, encrypted credentials and migration manifest. Runtime health/source identity
will be verified after deployment. Scripted wiring tests and installation health do not prove
all real-model choices or unsupported prose are eliminated.
