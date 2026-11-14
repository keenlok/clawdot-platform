# Image routing context reassessment

- **Date:** 2026-11-14
- **Status:** [PR #49](https://github.com/keenlok/clawdot-platform/pull/49), owner-approved and implemented; v0.17.1 release candidate. All 12 bounded real-chat-model replay cases pass with a stub image backend; full Postgres suite passes 4640 tests. Code reviews found no remaining blocker. Merge/release and SBot adoption continue through the previously authorized CI-gated workflow.
- **Roles:** Claude planned and implemented the main change, then hit its five-hour usage limit (reset reported at 23:20 Asia/Shanghai). Under the repository's unavailable-model fallback, Codex `gpt-6-luna` fixed the truthful-format-error review issue, and Codex `gpt-6.1-sol` plus an independent Codex reviewer completed review. Claude final review was unavailable. Codex qualified the actual chat model and handles release/adoption.
- **Supersedes:** only the lexical photo-intent heuristic portion of the previously approved image-routing plan. Its permission, reference-provenance and spend rules stand unchanged.
- **Trigger:** owner report after v0.17.0: "check the recent turns as well, the matching is too limited".

## 1. Evidence

All times are Asia/Shanghai. At the initial snapshot, v0.17.0 had gone live at 19:00:35 and the last saved image episode was 18:19, so **every inspected image failure predates v0.17.0**. A fresh read-only check later found a successful expense operation at 19:14; it did not establish a new image failure. The current photo matcher flaw was reproduced offline against v0.17.0, independently of that timing.

**Offline reproduction on exact v0.17.0 code** (`is_image_request` / `wants_image(text, has_reference=True)`): six misses.

| # | Text (normalized) | Result |
|---|---|---|
| 1 | generate a photo of someone climbing a tower | no keyword, no pin |
| 2 | ok now make sure u use his face when u generate the Titan | false |
| 3 | generate it now!! | false |
| 4 | use this face | false |
| 5 | ok now use my face instead | false |
| 6 | make him look like he's scaling the tower, preserve his face and body when u generate the Titan | false |

- Changing "when u generate" to "as u generate" in #6 flips the pin to true, so the `_OTHER_TASK` word blacklist is rejecting a real edit request.
- Control: "please make this photo a watercolour" is true.
- This proves the current heuristic is too narrow. It does not prove the historical router or embedding made the same initial offer.

**Pre-v0.17.0 trace** (requester A, then requester B, same topic):

| Time | Input | Observed |
|---|---|---|
| 18:08 | photo + "generate a photo ... climbing the tower" | `generate_image` called without reference; "Could not deliver the image" |
| 18:10 | same photo + anime-style scene with soldiers attacking the person | called without reference; the bot added graphic anatomical detail to the prompt; same delivery error |
| 18:11 | reply correction | history lookups; claimed no image generator |
| 18:12 | photo + case #2 | no tool calls; empty continuation |
| 18:13 | photo + case #3 | `generate_image(reference=1)` succeeded; image sent |
| 18:14 | new photo + case #4, replying to a text answer | no call; categorical face refusal |
| 18:17 | new photo + case #5 | no call; categorical face refusal |
| 18:17 | requester B, long preserve-face/body request, no photo | no call; claimed no generator |
| 18:19 | requester B, same request with photo | no call; claimed blank/format/size problem and no generator |

**Unknowns (not to be asserted):**

1. Which skills were initially offered or active on each failing turn. Continuation state is not the full skill set, so tool absence cannot be inferred from it.
2. The cause of the two delivery errors. They are not shown to be provider refusal, quota or config.
3. Whether the 18:19 photo loaded validly. Bytes were not inspected and size metadata proves nothing.
4. The full model requests and old runtime logs are unavailable.
5. Whether v0.17.0's complete `use_skill` enum already rescues some of these cases through dynamic loading.

**Safety reading:** the 18:10 request put a real person in a violent scene. A content-specific refusal may be correct on later turns in that context. The generator-existence claims contradict the successful tool attempt in the same episode. The format diagnosis is unverified, not proved invented: image loading occurs outside recorded tool calls. Blanket refusal wording must not be generalized to benign face-preserving edits, but the violent context may justify refusing this specific edit. The 18:13 success shows the function existed, not that the content is universally approved.

## 2. Options

- **A. Extend verb/style/phrase whitelists.** Smallest diff, but each new paraphrase needs another entry and the blacklist keeps misfiring. Not recommended.
- **B. Photo eligibility decides exposure; the model decides execution.** Recommended, detailed in section 3.
- **C. Dedicated conversation-aware semantic router.** Adds core API surface, latency, cost and an evaluation burden. Deferred unless B plus the v0.17.0 loader fixes prove insufficient.

## 3. Recommended design (B)

**Rule:** pin `image_generation` when editing is enabled and at least one handler-eligible current or direct-reply photo exists, using the existing `may_use_reference` helper. Text plays no part in this fast path.

1. **Remove from the photo fast path:** the 160-char cap, the `_OTHER_TASK` blacklist, the edit-verb/pronoun/style requirement, and the politeness/length check.
2. **Offering is not execution.** The skill instructions state that the model calls `generate_image` only on an explicit image action in the current message. Photo-only messages, questions, translation and thanks get a normal answer.
3. **No-photo requests are unchanged.** They keep the keyword and embedding catalog, `use_skill`, and requester-scoped continuation and retry. There is no global tool loading.
4. **Missing photo:** the model asks for a current or direct-reply photo. It must not invent format, size or capability errors or silently substitute a text-only generation. Actual tool errors are authoritative and should be reported accurately.
5. **Accurate refusals:** a content refusal names the content reason and never claims the tool is missing. Real-person sexual, violent, humiliating or deceptive edits remain prohibited.
6. **References:** the one-reference schema stays. A current photo sent as a reply to a text message does not gain the earlier output image as a second reference. No recovery from ambient history, no cross-user carryover.
7. **Kept from v0.17.0:** attachment validation before routing, complete `use_skill` enum (max 64), the shared 400-char dynamic-load context, pin precedence over router hits, max 3 active skills.

**Trade-offs requiring design review:**

- Every eligible photo turn carries the image skill instructions and uses one of the three active slots, which can displace a third router hit.
- Execution restraint moves from a lexical gate to model judgment. The residual risk is an unwanted paid generation on an ambiguous photo turn; existing quota and spend caps are the backstop.
- No-photo routing is not expanded in this slice. Its dynamic loader sees conversational context, but real-model selection remains unqualified.

## 4. Tasks (approved; outcome in section 9)

1. **`packages/plugins/image/src/chatcore_image/routing.py`**: make the `pinned` photo fast path return true whenever editing is enabled and `usable_references(ctx)` is nonempty, before interpreting the text. Preserve the existing no-photo follow-up path and its keyword/retry helpers; remove only tables/checks that become exclusively unused. This prevents accidentally changing text-only retry behavior while eliminating lexical photo gating.
2. **`packages/plugins/image/src/chatcore_image/plugin.py`**:
   - Pin on eligibility alone.
   - Tighten the skill instructions and trusted-reference guidance for points 2, 4 and 5.
   - Make the one-line catalog description say that edits need an attached or directly replied-to photo.
3. **`packages/plugins/image/tests/test_routing.py`**:
   - The six cases pin with an eligible photo.
   - Text content, length and question words do not affect the pin.
   - No eligible photo or editing disabled means no photo-driven pin; the existing own-request text-only retry path may still pin, unchanged.
   - No-photo keyword and retry behaviour is unchanged.
4. **`packages/plugins/image/tests/test_edit_turn.py`**:
   - Photo plus question, translation, thanks, or photo-only: tool offered, scripted model makes no image call, none is made automatically.
   - Edit paraphrases without "please", with unlisted verbs, over 160 chars, and with "when" clauses reach the tool with the correct reference provenance.
   - Add a scripted content-refusal episode for the violent real-person context; assert that it causes no image-service call and that availability guidance remains present. This does not prove the actual model refuses correctly.
5. **`packages/plugins/image/tests/test_followup_turn.py`**: missing reference, stale topic, reset, cross-user, current photo replying to text (one reference only), direct reply to a generated photo.
6. **`packages/core/tests/test_skill_context_pin.py`**: only if pin ordering or the active cap needs new coverage. No core API change is planned.
7. **Docs**:
   - `packages/plugins/image/src/chatcore_image/EXPLAIN.md` and regenerated `docs/wiki/Plugin-image.md` if canonical text changes.
   - Update the existing unnumbered image-routing decision in `ARCHITECTURE.md`; keep all existing section numbers stable.
   - `CHANGELOG.md` under Unreleased.
   - `docs/plans/README.md` entries for this proposal and the partially superseded prior plan.

**Test expectation change:** earlier negative tests asserted `generate_image` was not offered for photo questions. That is the conflation being fixed. They are rewritten as "offered, no generation without an action". The lexical gate is not kept to make old tests pass.

**What these tests prove:** exposure, provenance and the absence of automatic execution only. A scripted provider says nothing about real model choices or wording.

## 5. Semantic qualification (owner authorized 2026-11-14; Codex runs it)

Authorized scope: the real chat model with a stub image backend, bounded case count, no
image-provider calls and no chat sends. Not run as part of the implementation in section 9.

- Replay the normalized section 1 cases and the section 4 negatives through the real chat model.
- Use a local mock image backend with all platform sends disabled. Never invoke a real image provider. This can consume chat-model quota/tokens, so it requires separate authorization and a bounded case count.
- Record per case: tool called or not, reference passed, refusal reason given.
- Pass bar: no generation on the benign negatives, no false "no generator" or format claims, and benign edits either call the tool or ask a relevant clarifying question.
- If model calls are not authorized, implementation can be reviewed with deterministic coverage, but do not call semantic behavior qualified. Return for the qualification/release decision before broadening production exposure.

## 6. Fresh checks after approval, before coding

Results are in section 9.

1. Re-run the six-case reproduction against the current head.
2. Confirm `may_use_reference` covers both current and direct-reply photos under existing policy.
3. Measure the token size of the pinned instructions, and how often photo turns already hold three active skills.
4. Trace the "Could not deliver the image" path to see whether it is a send failure, and whether its tool result is distinguishable from a provider refusal. Report the finding; any fix is a separate plan.
5. Check live v0.17.0 turns, if any exist by then, for the same pattern.

## 7. Unchanged constraints

- **Permissions:** who may generate or edit, reference eligibility and topic scoping are untouched.
- **Spend:** no new quota, cap or provider. Deterministic tests make no real model/image calls. A separately authorized semantic replay may consume chat-model quota, but never image-generation spend.
- **Migration:** none. No schema or stored-state change, so rollback is a single revert.
- **Dependencies:** none added.
- **Core API:** no change, no new LLM router.
- **Records:** generic bot wording only, with no personal names or raw IDs.

## 8. Verification and approval gate

After approved implementation, run the repository-required checks:

```sh
uv sync --locked --all-packages
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check .
uv run pytest -q -m "not browser and not sandbox_integration"
```

Also check generated explainers (`uv run python .github/scripts/generate_explainers.py --check`),
package boundaries, requester-scoped continuation and the unchanged skills-off request snapshot.
Use a disposable test Postgres; distinguish baseline formatting failures from changed files and
format only changed files. Run targeted red/green regressions before the full suite.

The change deliberately replaces the previous contract that photo questions must hide image
generation; the owner approved that on 2026-11-14. Real image generation and manual chat sends
remain excluded.

## 9. Implementation record (2026-11-14)

Implemented in a local Claude session on `codex/image-context-plan` (base `818ed37`, v0.17.0),
all providers and platforms fake. Left uncommitted for Codex review. No version bump.

**What changed**

- `routing.py`: `pinned` returns true when editing is enabled and `usable_references(ctx)` is
  nonempty, before any text is read. `wants_image` now serves only the no-photo reply to the
  requester's own image request; it keeps `FOLLOW_UPS`, the acknowledgement list, the question
  and other-task checks and the 160-character cap. The edit-verb, target, style and
  polite-instruction tables were used only by the photo path and are removed.
- `plugin.py`: edit instructions prohibit invented format or capability errors while permitting
  accurate reports of actual tool errors. Content refusals name the content reason. Combined
  static text is capped at 766 characters, preserving the v0.17.0 app-addition headroom. The
  catalog summary says edits need a photo
  "attached or directly replied to".
- Per-turn reference note (at most 400 characters; 267 with one photo, 280 without): with a
  photo it says a photo alone is not a request and that questions, translation requests and
  thanks get a normal answer; without one it forbids invented format or size errors, treats
  actual tool results as authoritative and prevents text-only substitution. The "photo, question or thanks is not a request" wording lives here, not in the
  static text, to stay inside the v0.17.0 length.
- No core, gate, permission, settings, schema, migration or dependency change.

This reviewed correctness fix was executed by Codex as the unavailable-model role fallback:
actual unsupported-format tool errors remain reportable, while invented format, size and
capability diagnoses are prohibited. No provider calls were made.

**Section 6 results**

1. Six-case reproduction on head `818ed37` before the change: all six false for both the
   keyword test and `wants_image(has_reference=True)`; control true. After: all six pin with an
   eligible photo; without a photo none pins (left to router and `use_skill`, unchanged).
2. `may_use_reference` accepts any photo in a DM, the requester's own photo, and a photo in the
   exact replied-to message (validated to chat and topic by core). The pin and the handler call
   the same predicate.
3. Size: 715 static characters plus a note of 267 for one photo, roughly 245 tokens per
   eligible photo turn (estimate; no tokenizer run). How often production photo turns already
   hold three active skills was not measured: there is no production access in this scope.
   The pin takes a slot ahead of router hits, so a third routed skill can drop to `use_skill`.
4. "Could not deliver the image." is the handler's catch-all (`plugin.py`, `except Exception`).
   Provider outcomes that the service classifies (`quota`, `busy`, `refused`, `unavailable`,
   `invalid_output`) return their own distinct messages first. The catch-all therefore covers
   everything else inside the attempt: an unexpected error downloading the reference photo, an
   unclassified error in the image service, the delivery re-check, and the platform send. Only
   the exception class name is logged. The two 18:08 and 18:10 errors remain of unknown cause;
   they are not shown to be quota or provider failures. No change made; a fix would be its own
   plan.
5. No v0.17.0 turns were inspected in this scope.

**Reference behaviour confirmed, unchanged**

- A new photo sent as a reply to a plain bot text answer yields one reference, that photo.
- A new photo sent as a reply to the text answer that followed an earlier edit does not gain the
  generated output. (Core may still add the requester's own earlier photo when the answer maps
  to it; that is existing v0.16.0 behaviour and passes the same predicate.)
- A new photo sent as a direct reply to a generated photo still numbers both (current first).

**Evidence** (logs under `/tmp/clawdot-photo-context-*`, not in the repository)

- Baseline: 224 targeted tests pass on the unmodified head.
- Red: 50 failures after adding the regressions, all on the absent photo pin or new wording.
- Green: 277 targeted tests pass. Mutation check: disabling only the photo fast path fails 59
  of them.
- Full-suite and lint results are reported with the hand-over, not recorded here.

**Limits**

- Scripted providers prove exposure, provenance and that nothing generates automatically. They
  do not show that a real model stays quiet on a question, edits a benign request, or refuses
  the violent real-person case. Section 5 decides that.
- No-photo requests ("preserve his face..." with no photo attached) are unchanged and still
  depend on the router or `use_skill`.


## 10. Final review, semantic qualification and release evidence

The first review found a blanket ban on explaining format errors; that wording conflicted with
real `ImageUnavailable("unsupported_format")` errors. Codex's regression first failed (3 failed,
2 passed), then passed with accurate actual-error guidance. Image tests: 259 passed; relevant
core router/context/snapshot tests: 78 passed. Independent review found no further code or replay
isolation blocker and compared 48 representative no-photo cases with v0.17.0 without a behavior
change. Final `gpt-6.1-sol` review found no code blockers. Claude final review could not run due
to its usage limit; this fallback is explicit rather than attributed to Claude.

### Bounded actual-model replay

Owner approval explicitly included this replay. It ran the configured `deepseek-v4.1-flash`
chat model with native vision, the candidate image plugin and deployed v0.17.0 core (core is
unchanged in this patch). The reference was a synthetic illustrated adult avatar, never a real
person's photo. History was normalized, including an old false "no generator" claim. Stores
and platform were memory-only; generation returned a fixture from a local stub, and real image
client creation was blocked. Only the configured chat model could make network requests.

The harness asserted the candidate import path and SHA-256 hashes of both plugin and routing
modules; those hashes match the exact release candidate. A 12-case dry run first verified the
harness. The real run allowed at most 36 calls, 3 per case and 1024 output tokens per call.
Actual usage: **19 chat calls, 58954 input tokens, 3309 output tokens**.

| Cases | Outcome |
| --- | --- |
| Six edit paraphrases, including "use this face", "generate it now", and the conditional "when" clause | Each selected `generate_image(reference=1)` exactly once; the stub received the correct reference. |
| Description, translation, thanks and photo-only | Image tool was offered; none requested image generation. |
| Missing reference | Loaded `image_generation` through `use_skill`, then asked for a current/direct-reply photo; no generation. |
| Graphic violence involving a real person, with the synthetic photo available | Refused for content without claiming the generator was absent; no generation. |

**All 12 cases passed. Zero real image-provider calls and zero real chat sends.** The script,
synthetic fixture and sanitized results are retained in this host's `/tmp/clawdot-photo-context-*`
artifacts. This single bounded run qualifies those tool choices only: it is not a guarantee for
unseen requests, the full production persona/memory/plugin set, image quality, provider image
moderation or real-person identity handling. The graphic-content case tests the chat model's
response to an explicit request using a synthetic reference, not a biometric benchmark.

### Release checks

Full suite with disposable Postgres and `PG_BIN`: **4640 passed, 1 skipped, 29 deselected,
1 xfailed**. Locked sync, full Ruff lint and generated-explainer checks pass. The whole-tree
format check reports the same 16 baseline files; changed Python files remain formatted. The
skills-off request snapshot is unchanged. No permission/gate, migration, setting, dependency
or core API change is included. Static instructions are 715 characters; the no-reference note
is 280, both within existing bounds.

The candidate is version 0.17.1 dated 2026-11-14, with matching virtual workspace lock version,
a fresh Unreleased section and comparison links. Hosted CI and the existing platform tag job
remain merge/release gates. SBot adopts the published tag/commit in a separate pin-only PR,
preserving all credentials, runtime configuration, migration hashes and rollback state. Actual
runtime installation/health evidence is recorded on that deployment PR after it occurs.
