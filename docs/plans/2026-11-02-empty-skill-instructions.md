# Complete the remaining skill instructions

Date: 2026-11-02. Status: implemented and verified in
history PR #43; merge authorized in chat.

## Goal and design

The audit after PRs #38/#41 found three declared skills with empty instructions:
`bot_model`, `feature_settings` and `plan_outing`. Their tools exist; service availability does
not account for the missing guidance. In particular, the outing composite can expose
`create_poll` without activating the polls skill and its instruction not to repeat the bot echo.

Use the existing per-skill instruction mechanism. Core owns `BOT_MODEL_INSTRUCTIONS` beside
`ModelPlugin`; the app owns instruction constants beside its two composites. Keep each block
under 800 characters. Do not add cross-plugin instruction inheritance or change tool registration,
permissions, router configuration, legacy prompts, browser deployment or dependencies.

Acceptance criteria:

- A1: all 15 existing declared skills, including optional model switching, have nonblank
  instructions; the previous 12 instruction texts remain byte-for-byte unchanged.
- A2: model guidance requires current `get_model_info` results and explains that people switch
  models through `/model`; the agent cannot switch one itself.
- A3: feature settings target only the requested feature in the current group, respect tool
  availability and permissions, and await requester confirmation before claiming success.
- A4: outing guidance covers quick search versus research, user-opened rental links, explicit
  poll requests, the bot's poll echo and loading the polls skill for undo/end actions.
- A5: guidance is injected on routing and returned by `use_skill`, stays absent when inactive,
  and does not make an unavailable optional poll tool callable.

## Implementation plan

1. Add regression tests to the declaration ownership audit and a focused app prompt test file.
   Exercise actual app catalog construction, keyword routing, context generation and `use_skill`.
   Run them before implementation and confirm failures identify the missing guidance.
2. Add the three instruction constants and wire them into their existing `Skill` declarations.
   Keep the previous instruction fixture and routing examples unchanged.
3. Run focused tests, ownership/catalog/routing/snapshot checks, Ruff lint and formatting, and
   the required workspace suite. Distinguish the known local database checksum mismatch from
   regressions; CI supplies a clean Postgres database.
4. Self-review the complete diff and acceptance criteria. Codex implements and self-reviews;
   Claude is unavailable in this session. Create a PR, wait for CI, merge, and observe main CI
   and the existing auto-deploy outcome where accessible.

## Review and validation

- Codex implemented and self-reviewed. Claude's final-review invocation failed authentication
  because its OAuth session expired; the repository's unavailable-model fallback applies.
- Self-review: **PASS**. A1-A5 are covered by declaration, prompt/context and explicit-load
  checks. The optional poll gate is exercised both enabled and disabled, with the polls skill
  inactive. No tool, gate, routing-example, legacy-prompt or dependency changes were needed.
- Red/green: nine new regression cases failed on the original empty instructions and passed
  after the three declarations were filled. The existing 12 instruction baseline texts match.
- Instruction sizes: model 402, feature settings 593, outing 698 characters (cap: 800).
- `uv sync --locked --all-packages`, `uv lock --check` and workspace Ruff lint pass.
- Changed Python files pass Ruff formatting. The full formatting check reports the same
  16 pre-existing documentation files as the baseline; those files are outside this change.
- Focused catalog/prompt/model/snapshot/boundary/index checks: 77 passed. Routing checks:
  7 passed, 1 skipped.
- Full required workspace suite using fresh local Postgres: **3284 passed, 10 skipped,
  22 deselected**, with 14 existing Telegram deprecation warnings. Browser and real Docker
  sandbox tests are excluded by the required command.
- PR/merge and automatic deployment verification follow implementation.
