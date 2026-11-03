# Skill step cap and empty final answer

Two bugs made web-research turns end with the canned "I hit my search limit…" reply in
production (`SKILLS_MODE=on`, model `deepseek-v4.1-flash` over the OpenAI-compatible provider).

Read `AGENTS.md`, then `packages/core/src/chatcore/skill_turn.py` (`SkillTurn`,
`_current_step_cap`, `use_skill`), `packages/core/src/chatcore/skills.py` (`SkillSet.build`,
`evaluate_skill`, `Skill.max_steps`), and the tool loop in `packages/core/src/chatcore/agent.py`
(`respond()` from `step_cap = ...` through the tool-call loop, plus `_empty_answer_fallback`).

## Evidence

- **Turn A** (`steps=8 called=search_web,fetch_web_page final_forced=steps markup_stripped=2
  retries=1 end=fallback`): the turn used all 8 global tool steps. The forced final answer
  (`tool_choice="none"`, tools still declared) came back as native tool-call markup only. The
  existing retry also declares the tools with `tool_choice="none"`, and also came back as markup
  only, so `_empty_answer_fallback` sent "I hit my search limit…".
- **Turn B** (`steps=3 ... final_forced=steps skills=rendered_pages source=embed
  top=rendered_pages:0.43`): the router activated `rendered_pages` (`max_steps=3`,
  `packages/plugin-browse/src/chatcore_browse/plugin.py`). `_current_step_cap` returns the max of
  active/loaded skills' `max_steps` and only falls back to `tool_steps` when none set one, so a
  general web-search turn (`search_web`, `fetch_web_page` are general tools, always offered) was
  cut from 8 steps to 3. In production `fetch_rendered_page` isn't registered; only
  `build_search_link` (rental-car links) remained, which the turn never used.

## You own

- `packages/core/src/chatcore/skill_turn.py`
- `packages/core/src/chatcore/skills.py` (only if step 2 needs it)
- `packages/core/src/chatcore/agent.py`
- Tests: `packages/core/tests/test_skill_turn.py`, `packages/core/tests/test_agent_skills.py`,
  `packages/core/tests/test_tool_markup.py`, `packages/core/tests/test_agent_budget.py`,
  `apps/sb_bot/tests/test_tool_markup_e2e.py`, and a new
  `packages/core/tests/test_agent_step_cap.py` if that's tidier.

You may **not** edit `packages/core/src/chatcore/llm/*` (another PR is changing
`openai_compat.py`), any plugin's `plugin.py`/`gates.py`, `Skill`'s fields, the `Provider`
protocol, or log field names in the `turn_metrics` line. Ask in your report if one must change.

## 1. Step-cap rule (`SkillTurn`)

The rule: **a skill's `max_steps` only governs steps spent on that skill's own tools.**

Define a *capping skill* as an active or loaded skill that sets `max_steps` **and** currently
offers at least one tool (`self.availability[name].offered` non-empty; for loaded skills use the
availability recorded by `use_skill`). A skill whose tools are all gated off or missing can still
surface its hints, but never contributes a cap.

`_current_step_cap()` returns:
- `settings.tool_steps` if there is no capping skill;
- `settings.tool_steps` if the turn has made any counted call (see below) to a tool that is not
  in some capping skill's offered tools — e.g. a general tool such as `search_web`, or a tool from
  an active/loaded skill without `max_steps`;
- otherwise the max of the capping skills' `max_steps` (as today).

Add `SkillTurn.note_call(tool_name: str) -> None`, called by the agent loop for every non-meta
call that passed `check_call` as `"ok"` (the same place `ctx.turn.tool_steps += 1` and
`metrics.note_call(..., offered=True)` happen), **before** the per-call step-limit check. It
records whether the tool is outside every capping skill and then sets
`self.step_cap = max(self.step_cap, self._current_step_cap())`. `not_offered`/`unknown` calls and
`use_skill` don't lift the cap. The cap never decreases within a turn (keep the existing
`max(...)` on load, and keep `test_loading_shorter_skill_does_not_shrink_initial_step_cap`).

Effects: an expenses-only turn still stops at the expense skill's `max_steps`; a turn where a
capped skill was routed but the model searches the web gets the global 8; `plan_outing`
(`max_steps=6`, owns `search_web`/`fetch_web_page`) keeps 6 because those tools are its own.

Wire `note_call` in `agent.py` (on mode only). The `Tool step limit reached.` checks in the loop
already read `skill_turn.step_cap`; make sure they read it after `note_call`.

## 2. Skills with no available tools are not routed

`SkillSet.build` already drops skills with no registered tools (`reason=empty`). Per turn, in
`SkillTurn.build`, a routed or inherited candidate whose `evaluate_skill` result offers no tools
must not take an `active` slot or count as crowding unless it has hints to show (state `OFF` or
`NOT_ALLOWED` with a hint — that's how users learn to turn a feature on). Keep the current
behaviour for those hint-only skills, but they must not set the cap (step 1). Candidates that are
`UNCONFIGURED` are already skipped; keep that. Don't change the router or embeddings.

## 3. Empty final answer: one more retry with no tools

In `agent.py`, the current path is: final completion → `strip_tool_markup` → if empty, one retry
with tools declared and `tool_choice="none"` → fallback. Change it to:

1. Keep the existing first retry unchanged.
2. If that retry is also empty after stripping (and not a refusal, budget not exhausted, at least
   5 s of deadline left — same guards as the first retry), make **one more** call with
   `tools=()` and the default `tool_choice` (so no tool specs are sent at all), and system prompt
   `system + "\n\n" + _FINAL_ANSWER_INSTRUCTION + "\n\n" + _NO_TOOLS_FINAL_INSTRUCTION`, where the
   new constant says, in plain words: no tools exist in this request; answer the user now in
   plain text using the tool results already in this conversation; list the source URLs used.
3. Count it with `metrics.note_retry()`, `record_completion`, and add its usage to `usage`; strip
   markup from its text and log the same `stripped tool markup` line if anything was stripped.
4. If the provider raises `ProviderError` on this call (some providers reject tool history with no
   tools declared), log `final no-tools retry failed chat=%s error=%s` with the exception type only
   and use the fallback. Refusal → `_REFUSAL_TEXT`. Empty → fallback.

Never log message text, URL paths or secrets.

## 4. Fallback wording

`_empty_answer_fallback` must not say "search limit". New text:
- with URLs: `"I ran out of steps before I could finish an answer. Here's what I found so far:\n"`
  followed by up to 8 sorted URLs, one per line (unchanged list logic);
- without: `"Sorry, I couldn't finish an answer this time. Please try again or narrow the
  question."`

Update tests that assert the old text. Leave `plugin-research`'s own quota wording alone.

## Tests

- `SkillTurn` unit tests: (a) capping skill with no offered tools → cap 8; (b) capped skill
  routed, `note_call("search_web")` (general) → cap 8; (c) capped skill, `note_call` of its own
  tool only → keeps its cap; (d) call to a tool of an uncapped active skill → 8; (e) a
  `not_offered` tool name never passes through `note_call` (agent-level).
- Step 2: a routed skill whose only tools are unregistered/gated without hint doesn't appear in
  `turn.active`; one gated `OFF` with a hint still surfaces the hint and leaves cap at 8.
- **Turn B regression** (agent level, scripted provider, on mode): a routed skill with
  `max_steps=3` that offers one tool, plus general `search_web`/`fetch_web_page`; the model calls
  general tools 5 times then answers → all 5 calls run (no `Tool step limit reached.`), the reply
  is the model's text, and `final_forced` isn't `steps`. Also: model calls web tools 9 times →
  forced final after 8, not 3.
- Expenses-style regression: a routed skill with `max_steps=2` whose own tool is called
  repeatedly → final forced after 2.
- **Turn A regression**: the final forced completion and the first retry both return only
  tool-call markup; the second retry is called with `tools` empty and returns text → that text is
  the reply, `end` isn't `fallback`, the second retry's request declared no tools. Also: second
  retry empty → new fallback text with URLs; second retry raises `ProviderError` → fallback.
- Fallback wording: both variants; assert "search limit" no longer appears.

## Done when

- `uv run ruff check packages services apps tests` is clean.
- `uv run pytest -q -m "not browser and not sandbox_integration"` passes (Postgres-backed tests
  may need `PG_BIN`; run what you can and say what you skipped).
- No change outside the owned files.
- Not in scope: provider-level `tool_choice` handling, router thresholds, plugin `max_steps`
  values, registering `fetch_rendered_page` in production.
