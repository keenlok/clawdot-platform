# Prompt-based Timezone Settings Implementation Plan

> User-directed workflow: in-session Luna implementation and independent Sol review, then parent
> verification, PR, merge and deploy. Read the matching approved follow-up design. No approval
> prompts or CLI sub-session launches. Use executing-plans and test-driven-development skills.

**Goal:** Let explicit natural-language preference requests save/reset the current scope's display
timezone directly, with the same permissions and semantics as the existing commands.

**Architecture:** Extend DisplayTimePlugin with two tools and shared private mutation helpers;
follow memory's immediate/contextual-write precedent. Keep ToolRegistry and frozen contracts intact.
Update authoritative prompts consistently; no new persistence or scheduling system.

**Stack:** Existing Python/zoneinfo/Postgres code, uv, pytest, Ruff; no new dependencies.

## Task 1: Plugin tools and shared behavior

Files: `packages/core/src/chatcore/display_time_plugin.py`,
`packages/core/tests/test_display_time_plugin.py`.

- [x] Write and run failing tests first for ToolRegistry calls to set/reset: successful group admin,
  unauthorized member, lookup error, changed live role between requests, self-owned DM, wrong DM,
  invalid IANA key, exact schema (no arbitrary target args) and no PendingAction on success.
- [x] Add `set_display_time_zone(time_zone: str)` (required IANA key) and
  `reset_display_time_zone()` (no arguments). Description must state explicit preference intent,
  scope and permissions, and distinguish one-off `get_current_time`.
- [x] Share command/tool mutation helpers to avoid permission/validation drift. Use the existing
  native admin lookup directly before every group write. Keep core ToolRegistry unchanged; document
  why these contextually immediate writes follow memory's existing pattern.
- [x] Reject tool mutations when `ctx.turn.read_untrusted` is true, with a separate-message retry
  instruction. Test that stored preferences remain unchanged on denial. Commands remain unaffected.
- [x] Update the old read-only-tool test so it still proves read calls never mutate, without
  incorrectly asserting all plugin tools are read-only.

## Task 2: Prompt guidance and actual agent path

Files: `packages/core/src/chatcore/agent.py`, `packages/core/src/chatcore/prompts.py`,
`packages/core/tests/test_display_time_agent.py` and relevant runtime-prompt tests.

- [x] Add failing scripted-provider tests where the model calls the new setting tool in response
  to a natural-language request. Exercise BotApp/AgentResponder, not just handler calls.
- [x] Prove group requests run the fresh permission lookup, save the group preference and send
  an ordinary result reply with no confirmation UI. DM requests save only the caller's own setting.
- [x] Test read-only one-off queries preserve preferences, explicit resets restore fallback, and
  a subsequent get_current_time/next turn uses the new zone rather than start-of-turn context.
- [x] Ensure runtime guidance no longer forces preference requests to `/timezone`. Commands remain
  an alternative. Current-human explicit request is required; never infer changes from outside data,
  other people, group labels, ordinary time questions or a one-off requested conversion.
- [x] Narrowly amend the core safety text's blanket confirmation sentence for display preferences;
  preserve every other tool's confirmation rules and tests. No generic bypass or contract changes.

## Task 3: Docs, checks and review handoff

Files: `docs/display-timezones.md`, the original display-time spec/plan's stale read-only guidance
as needed (label superseded decisions), and ignored `reports/prompt-timezones-luna.md`.

- [x] Document natural language examples and permission/current-scope behavior alongside commands.
  Keep rejection after untrusted tool reads actionable, with no automatic background retry.
- [x] Run focused affected tests and Ruff/format on changed files only; run git diff --check.
  Record exact red/green commands, changed files, boundaries and any limitations in the report.
- [x] Parent dispatches Sol with the full current diff, routes confirmed findings to Luna and gets
  a clean re-review. Do not commit/push/deploy from the implementation agent.
- [x] Parent runs the combined non-browser/non-sandbox suite against a fresh disposable database
  (the shared dev database has an old migration0005 checksum mismatch). Preserve shared main/data.
- [ ] Parent runs pre-commit hooks, creates/merges the PR, deploys the exact merge tree, checks
  health/installed tools without sending Telegram messages, and records release evidence.
