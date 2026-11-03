# Memory Extraction JSON Failure Implementation Plan

> **For agentic workers:** Execute the steps in order. Keep this change inside `packages/plugin-memory` and its tests; the provider-neutral completion contract already supplies the fields needed by the fix.

**Goal:** Keep auto-extraction from failing on harmless JSON presentation noise while refusing empty, truncated, malformed, or wrong-schema model replies without saving any facts.

**Architecture:** `MemoryPlugin.after_turn` will classify the provider stop reason before parsing, then use a small memory-local parser that finds one schema-shaped JSON object in the response. It will validate the existing `facts`/`text` fields and continue to assign `source="auto"`, `created_by=message.user.ref`, and `subject_user` in trusted code. No provider or API contract changes are planned.

**Tech Stack:** Python 3.12, uv workspace, pytest, Ruff.

---

## Diagnosis

The extraction prompt requests a JSON object shaped as `{"facts":[{"text":"...","about_speaker":false}]}` and caps output at 200 tokens. `after_turn` currently trims whitespace, strips a code fence only when it encloses the entire reply, then calls `json.loads` on the full remaining string. Therefore leading/trailing prose, empty content, and incomplete JSON can raise `JSONDecodeError`. The provider completion contract carries `stop_reason`; OpenAI-compatible `finish_reason="length"` maps to `"max_tokens"`, but extraction does not inspect it. Wrong-schema objects do not currently raise but are ignored when `facts` is not a list; malformed entries are skipped unless `text` is a string. Logs record only chat key and exception class, so the exact production response shape cannot be inferred from the reported warning.

## Files

- Modify `packages/plugin-memory/src/chatcore_memory/plugin.py`: add the memory-local response parser/classification and use completion stop reason before accepting extraction results.
- Modify `packages/plugin-memory/tests/test_memory.py`: cover prose-wrapped and fenced JSON, valid empty facts, empty/malformed/truncated/wrong-schema replies, strict fact filtering, chat scoping and trusted provenance.
- Create `plans/memory-json-fix.md`: diagnosis, approach and verification plan.

## Task 1: Regression tests

- [x] Add a test proving a valid object surrounded by model prose is extracted without including surrounding prose.
- [x] Add parameterized no-save cases for empty content, malformed/truncated JSON (including an incomplete wrapper with nested facts), and wrong root schemas (object and array).
- [x] Add a case where `stop_reason="max_tokens"` accompanies otherwise valid JSON and confirm nothing is saved; confirm completion usage is still recorded by the limits object.
- [x] Add an extraction test with unexpected `source`/`created_by` response keys and assert saved provenance remains `source="auto"` and `created_by` is the triggering user.
- [x] Run the focused module with task-local cache. New parser, truncation, and `max_tokens` tests failed before the implementation changes; final result: 40 passed.

## Task 2: Minimal memory-local fix

- [x] Parse one complete root JSON object, allowing prose and code fences around it. Accept only a root object with a `facts` list; retain per-item type, length, sensitivity, and speaker checks.
- [x] Refuse facts when the provider reports `max_tokens`, after recording completion usage.
- [x] Log content-free reasons (`empty`, `truncated`, `invalid_json`, `wrong_schema`, or `max_tokens`) without model text, prompts, or fact values.
- [x] Keep writes scoped to `message.chat`; assign `source="auto"`, `created_by=message.user.ref`, and `subject_user` only from trusted code. No untrusted wrapping, sensitive filtering, deduplication, quota, provider, or core API changes.
- [x] Final focused memory module: 40 passed. Full Ruff: all checks passed.

## Task 3: Verification and review handoff

- [x] Run the full suite and reproduce the documented polls failure on the base. Final run: 1,585 passed, 60 skipped, 3 failed. The polls test fails on the base (`ctx.platform.polls` is empty). Two Postgres-backed integration tests fail because the shared dev DB reports `chatcore/0005_context_reset.sql changed after it was applied`.
- [x] Run `UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check packages apps tests` and `git diff --check`; both pass.
- [x] Obtain a separate in-session `gpt-6-luna` review. It found no remaining actionable issues after two parser edge cases were fixed and re-reviewed.
- [x] The user explicitly authorized committing, creating a PR, merging, and deploying in a follow-up request. The clean Luna review remains the independent review; no Claude review was performed.

## Handoff Record

- **Root-cause confidence:** High for the code defect: `after_turn` passed the entire response to `json.loads` after stripping only a full-response fence, and ignored `stop_reason="max_tokens"`. The logged `JSONDecodeError` confirms invalid JSON reached that call, but content-free logs do not establish whether the specific production replies were empty, prose-wrapped, fenced incorrectly, or truncated.
- **Final regression results:** Memory tests 40 passed; full Ruff passed; `git diff --check` passed. Full suite: 1,585 passed, 60 skipped, 3 failed (one reproduced polls baseline failure and two dev DB integration tests blocked by the existing migration checksum mismatch).
- **Independent review:** Final separate in-session `gpt-6-luna` review reported no actionable findings. Two review findings about nested JSON and trailing prose were fixed before the clean review.
- **Review boundary:** The final separate in-session `gpt-6-luna` review is clean. The original handover reserved final review and committing for Claude; the user subsequently explicitly authorized this session to commit, create a PR, merge, and deploy. No Claude review was performed.
- **Worktree:** `/path/to/worktrees/memory-json-fix/sb_bot`, on `codex/memory-json-fix`, based on `0568954a14575625c42375e581343c22169a9e63`.
- **Release status:** Commit, PR, merge, and deployment are in progress following the user’s explicit authorization; final identifiers and health result will be added after verification.
- **Files:** `packages/plugin-memory/src/chatcore_memory/plugin.py`, `packages/plugin-memory/tests/test_memory.py`, and this plan.
