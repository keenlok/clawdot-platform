# Step budget follow-ups

Status: Planned; owner asked on 2026-11-23 for 20 tool steps per turn and asked for Codex to
implement. Claude planned. Codex (`gpt-6-luna`) executes, Codex (`gpt-6.1-sol`) reviews, Claude
does the final review. Builds on [Tool-step cap and loop guard](2026-11-16-tool-step-cap-loop-guard.md),
which already raised the platform default to 20 and added the loop guard. Must land before
[Voice input](2026-11-23-voice-input.md), which uses the final-tool reservation from part 2.

## Problem

The owner sees SBot stop early and ask to continue. The platform has defaulted to 20 tool steps
since v0.20.0, but SBot still runs at 8.

### Evidence (SBot, 2026-11-08 to 2026-11-18)

The container logs only cover the 12 hours since the last restart and have no `turn metrics`
lines, because the bot doesn't log at INFO. So this comes from the stored `conversation_turns`
rows (read-only). It counts tool calls per user turn. 18 Nov is the last turn the bot answered;
nobody addressed it after that.

| Tool calls in the turn | Turns |
| --- | --- |
| 1–5 | 258 |
| 6–7 | 17 |
| 8 (the cap) | 18 |
| 9 (a parallel call in the last round) | 3 |

What this shows:

1. **SBot's own setting overrides the platform default.** In `clawdot-deployments`,
   `bots/sb_bot/src/sb_bot/app.py` declares `AGENT_TOOL_STEPS: int = Field(default=8, ge=1, le=20)`
   and passes it to `BotApp(agent_tool_steps=...)` and `SkillsEnvSettings.to_settings`. Production
   doesn't set the variable, so the cap is 8.
2. **Most of the capped turns are web-search runs**: 5–8 `search_web`/`fetch_web_page` calls on
   travel questions (stations, restaurants, shops). Some of them are real research. Others search
   again after the sources already agree.
3. **The cap blocked what the person asked for.** On 11 Nov the searches used up the budget before
   `narrate_text` could run. The bot replied that it couldn't send the voice note because its tool
   budget was spent, and offered to send it if asked.
4. **`member_expenses` is called once per member.** It ran five times in a row to answer "who
   spent the most".
5. **`read_explainer` was called 8 times on 15 Nov.** The turn still ended without the answer. An
   unknown topic returns "Use /explain to see topics", which the model can't do inside a turn, so
   it kept guessing names.
6. Raw doubled-bar DSML markup reached the person once on 11 Nov. That was fixed in
   [PR #59](https://github.com/keenlok/clawdot-platform/pull/59), so it needs no work here.

## Changes

### 1. SBot runs at 20 steps (downstream, `clawdot-deployments`)

- `AGENT_TOOL_STEPS` default 8 → 20 in `bots/sb_bot/src/sb_bot/app.py`. Keep `le=20`.
- Check `AGENT_TIMEOUT_S` (150 s) against the longest turns: when the deadline forces the final
  answer, the turn metrics show `final_forced=deadline`. Don't change the timeout in this PR. Record
  in the PR what the first week of metrics shows.
- **Turn metrics are already visible.** Both bots run with `LOG_LEVEL=INFO` (`config/*.env`), so
  the `turn metrics` lines are logged. The container logs had none because nobody addressed
  the bot after 18 Nov. No logging change is needed.
- This change goes in its own `clawdot-deployments` PR, following that repo's `AGENTS.md`, with its
  tests in `bots/sb_bot/tests/test_app.py`.

### 2. A reserved final step for the deliverable (platform core)

When the step cap or the loop guard forces the final answer, the person should still get the thing
they asked for, such as a voice note.

- New optional field on `chatcore.skills.Skill`: `final_tools: tuple[str, ...] = ()`. It must be a
  subset of `tools`, which `SkillSet` validation checks.
- When the final answer is forced by `steps`, `rounds` or `loop`, and a skill that is active or
  loaded this turn declares `final_tools` that haven't been called this turn, the next request
  offers **only** those tools. At most one call runs, and its gate and handler checks run as usual.
  Then the turn makes the usual no-tools final request.
- The reserved step isn't used when the deadline or budget forces the answer. The time and tokens
  are already spent.
- Turn metrics get a field `final_tool=<name>|-`. Existing field names and order stay as they are;
  append the new field at the end.
- The `voice` plugin's `read_aloud` skill declares `final_tools=("narrate_text",)`.

### 3. Fewer wasted steps (plugins)

- **`member_expenses`** takes `members: list[str]` (1–20 names) in place of `member: str`, and
  returns one section per member. It still accepts a single `member` for older transcripts and
  maps it to `members=[member]`. Each name resolves on its own: a name that doesn't resolve gets
  its own error line and doesn't fail the batch, and the result is an error only when no name
  resolves. Access is still the chat-level check; there is no per-member permission. Update the
  tool's argument descriptions as the tests require (this checkout has no `routing_set.jsonl`).
- **`read_explainer`**: an unknown topic returns the visible topic names and aliases as an error
  result, in the same order `/explain` shows them, so the model can pick one in the next step. The
  visibility rules are unchanged: never list a topic the person can't see.
- **Web search**: `search_web` is a general tool that isn't in any skill, so the rule goes in its
  tool description (`packages/plugins/web/src/chatcore_web/plugin.py`). Once two sources agree, or
  three searches have found nothing new, the model answers with what it has and says what it
  couldn't confirm.

## Execution record (2026-11-23)

- Codex (`gpt-6-luna`) implemented the platform parts (1 run plus 2 fix rounds). Codex
  (`gpt-6.1-sol`) reviewed twice:
  - the first review found the loop-guard and timeout reservation bugs, a missing search rule and
    missing tests, all fixed in the fix rounds;
  - the re-review found two more reservation bugs (a truncated reserved reply retried with tools;
    token exhaustion was ignored once steps had forced the answer). Claude fixed those after the
    fix rounds ran out, with regression tests checked by mutation.
- The review's "mixed permission" `member_expenses` finding needed no code change: there is no
  per-member permission, and the plan wording above was corrected.
- Part 1 is [clawdot-deployments#64](https://github.com/keenlok/clawdot-deployments/pull/64): the default goes from 8 to 20, with no logging change.

## Codex execution

Read `AGENTS.md`, this plan, the [loop guard plan](2026-11-16-tool-step-cap-loop-guard.md), then
`packages/core/src/chatcore/agent.py` (the tool loop from `max_rounds = ...` through the
`turn metrics` emit, the loop guard and the final-answer path), `skills.py` (`Skill`, `SkillSet`
validation, `_clamp`), `skill_turn.py` (`SkillTurn`, `step_cap`), `turn_metrics.py`,
`explain_plugin.py`, `packages/plugins/expenses/src/chatcore_expenses/plugin.py`
(`_member_expenses` and its tool spec), the search plugin's `plugin.py`, and
`packages/plugins/voice/src/chatcore_voice/plugin.py`.

### You own (platform repo)

- `packages/core/src/chatcore/agent.py`, `skills.py`, `skill_turn.py`, `turn_metrics.py`,
  `explain_plugin.py`
- `packages/plugins/expenses/src/chatcore_expenses/plugin.py` and its tests
- `packages/plugins/search/src/chatcore_search/plugin.py` (skill instructions only) and its tests
- `packages/plugins/voice/src/chatcore_voice/plugin.py` (the `final_tools` declaration only)
- `packages/core/tests/**`, `routing_set.jsonl`
- `CHANGELOG.md` (`Unreleased` only)

You may **not** edit gates, permissions, migrations, adapters, `pyproject.toml`, `uv.lock`, `apps/`
or `packages/core/tests/snapshots/*`. The request snapshot must pass unchanged. If it can't, stop
and say why in your report. Part 1 is in the other repository, so leave it out of this run.

### Tests

- A turn at the step cap with an active skill whose `final_tools` haven't been called: the forced
  request offers only those tools, the call runs, then a no-tools final request is made.
  `final_tool=narrate_text` appears in metrics.
- The same turn when `final_tools` were already called: no extra request.
- A deadline-forced and a budget-forced turn: no reserved step.
- A loop-guard-forced turn: the reserved step is offered.
- A gate that denies the final tool: the call returns the gate's error and the final answer still
  arrives.
- `Skill(final_tools=...)` that isn't a subset of `tools`: `SkillSet` validation rejects it.
- `member_expenses(members=[a, b])`: one section each, with a permission failure for one member
  shown only for that member. Legacy `member=` still works.
- `read_explainer` with an unknown topic: the error lists only visible topics.

### Done when

- The `AGENTS.md` checks pass: `ruff check`, `ruff format --check` on changed files, and
  `pytest -q -m "not browser and not sandbox_integration"`, with Postgres where available.
- The request snapshot and the skill catalog and routing-set tests pass unchanged, apart from the
  `member_expenses` spec change, which the plan expects.
- Left for later: changing the timeout, and the downstream PR (part 1), which Claude runs in
  `clawdot-deployments` after this lands.
