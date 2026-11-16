# Tool-step cap and loop guard: execution

Codex execution plan for [Tool-step cap and loop guard](2026-11-16-tool-step-cap-loop-guard.md):
`gpt-6-luna` executes, `gpt-6.1-sol` reviews, Claude does the final review.

Read `AGENTS.md`, then the design plan above (it is the spec; this file is how to build it), then
`packages/core/src/chatcore/agent.py` (the `AgentSettings` dataclass and the tool loop in
`AgentResponder`, from `max_rounds = ...` through the `turn metrics` emit), `skill_turn.py`
(`SkillSettings`, `SkillTurn.step_cap`, `_current_step_cap`), `skills.py` (`SkillSet.build`,
`_clamp`), `app.py` (`BotApp.__init__`), the turn-metrics helper the agent calls
(`metrics.emit`, `metrics.note_call`), and `packages/core/tests/test_agent_step_cap.py`,
`test_agent_deadline.py`, `test_turn_metrics.py`.

## You own

- `packages/core/src/chatcore/agent.py`
- `packages/core/src/chatcore/skill_turn.py` (default only)
- `packages/core/src/chatcore/skills.py` (default only)
- `packages/core/src/chatcore/app.py` (default only)
- the turn-metrics module the agent uses (new field and `final_forced` values only)
- `packages/plugins/google/src/chatcore_google/plugin.py` (`google_workspace` `max_steps` only)
- `packages/core/tests/**`, `packages/plugins/google/tests/**`
- `CHANGELOG.md` (`Unreleased` section only)

You may **not** edit gates, permissions, migrations, `pyproject.toml`, `uv.lock`, adapters, other
plugins, `apps/`, or `packages/core/tests/snapshots/*` (the snapshots must pass unchanged; if one
can't, stop and explain in your report). Don't change tool specs, prompts or log field names that
already exist.

## 1. Default cap 20

- `AgentSettings.tool_steps = 20`, `SkillSettings.tool_steps = 20`,
  `BotApp(agent_tool_steps: int = 20)`, `SkillSet.build(step_cap: int = 20)`.
- `google_workspace` `max_steps=16`.
- Existing tests that depend on the old default of 8 (or on the Google cap of 6) must pin it
  explicitly (`AgentSettings(tool_steps=8)`, etc.) so they keep testing what they tested. Don't
  loosen their assertions.

## 2. Loop guard

Add to `AgentSettings` (frozen dataclass, keep field order stable by appending):

```python
max_identical_calls: int = 2
max_consecutive_tool_errors: int = 4
turn_input_token_limit: int | None = None
```

In the tool loop, per turn:

- **Identical calls.** Key = `(call.name, json.dumps(call.arguments, sort_keys=True,
  separators=(",", ":"), ensure_ascii=False, default=str))`. Count only non-meta calls that reach
  the point where the tool would actually execute (after the not-offered/unknown/invalid-JSON/step
  limit/out-of-time checks). If the key has already executed `max_identical_calls` times, don't
  call the tool: the result is `ToolResult.error(f"Not run: {call.name} was already called with
  these arguments this turn. Use the earlier result.")`, the step still counts, increment
  `repeats_refused`, and set a `loop_final_forced` flag so the **next** request is a final answer
  (no tools) through the existing final-answer path. Further calls in the same response after
  the refusal are also refused with the same kind of error (don't execute them).
- **Consecutive errors.** After each non-meta call's result is decided, if `result.is_error`
  increment a streak, else reset it to 0. When the streak reaches `max_consecutive_tool_errors`,
  set `errors_final_forced` so the next request is a final answer. Framework-produced errors
  count.
- **Token limit.** When `turn_input_token_limit` is not `None` and the turn's accumulated provider
  input tokens (the same usage the turn already sums) reach it, the next request is a final
  answer (`tokens_final_forced`).
- When any of these fires, log once per turn at INFO:
  `"final answer forced by loop guard chat=%s reason=%s tool=%s tool_steps=%d"` with reason
  `loop`, `errors` or `tokens` and the tool name (or `-`). Never log arguments or results.
- Forced finals reuse the existing no-tools path (same as `steps_final_forced`), including the
  empty-response retry and `_empty_answer_fallback`.
- `turn metrics`: `final_forced` gets `loop`, `errors`, `tokens` (precedence: deadline, budget,
  meta, loop, errors, tokens, steps, rounds). Add `repeats_refused=<n>` as a new field **after**
  the existing fields; don't rename or reorder existing ones. Update `test_turn_metrics.py`
  accordingly.

Make sure the loop has enough rounds: a forced final must happen on a round that exists
(`max_rounds` already allows `tool_steps + 1`; the guard only ends things earlier).

## Tests

Use `ScriptedProvider` and the patterns in `test_agent_step_cap.py`. Assert on the requests the
provider received (tools present or absent), tool execution counts, the reply text and the
metrics line.

- Default cap 20 (skills off and skills on): 20 distinct calls execute; the 21st request has no
  tools; the answer is returned; `final_forced=steps`.
- A skill with `max_steps=30` is clamped to 20. `google_workspace` exposes `max_steps == 16`.
- Two identical calls both execute; the third doesn't (execution count stays 2), returns the
  "already called" error, the next request has no tools, `final_forced=loop`,
  `repeats_refused=1`. Identical calls in one response (parallel) count the same way.
- `{"a": 1, "b": 2}` vs `{"b": 2, "a": 1}` are identical; `{"a": 1}` vs `{"a": 2}` are not.
- `max_identical_calls=1` refuses the second identical call.
- Four consecutive error results force a no-tools request (`final_forced=errors`); three errors,
  one success, three errors don't.
- `turn_input_token_limit` below the usage after round one forces the next request without tools
  (`final_forced=tokens`); `None` changes nothing.
- A forced loop final where the model returns empty text gets the existing fallback reply.
- The log line contains the tool name and no argument values (use an argument value like
  `"secret-arg-value"` and assert it's absent from captured logs).

## CHANGELOG

Under `Unreleased`: **Changed**: default per-turn tool-step cap 8 → 20 (`AgentSettings`,
`SkillSettings`, `BotApp`, `SkillSet.build`); `google_workspace` skill cap 6 → 16. **Added**: loop
guard settings and behaviour, new `final_forced` values and `repeats_refused` metrics field.
Upgrade note: apps that pass their own `tool_steps` / `agent_tool_steps` keep their value; to keep
the old behaviour pass 8.

## Done when

- `uv run ruff check packages services apps tests .github/scripts` is clean; `uv run ruff format`
  applied to changed files only.
- `uv run pytest -q -m "not browser and not sandbox_integration"` passes (tests needing Postgres
  may skip in the sandbox; say which).
- Snapshot, skill catalog and routing-set tests pass unchanged.
- Not in scope: downstream env vars, deployments, release version bump, ARCHITECTURE.md.
