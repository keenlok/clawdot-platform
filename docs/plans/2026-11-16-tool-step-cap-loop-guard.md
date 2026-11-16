# Tool-step cap and loop guard

Status: Planned; owner approved on 2026-11-16. Claude planned. Codex CLI is installed, so Codex
executes and reviews, then Claude does the final review (`AGENTS.md`, Execution and review).

## Problem

The owner finds 8 tool steps per turn too few for multi-step work, such as reading a trip's
Google Sheets and Drive files to answer itinerary questions. They want about 20, without risking
a turn that loops on tool calls.

### Evidence (Loki, 2026-11-15)

The container logs go back to the last restart (2026-11-15 17:44 UTC), so they cover five turns.
The database turn records weren't read: production database access from this session was
refused. So the evidence is the `turn metrics` lines, which record the step count, the distinct
tools called and why the final answer was forced.

| Steps | Distinct tools | `final_forced` | Time |
| --- | --- | --- | --- |
| 8 | `list_trips`, `trip_list_files`, `itinerary_summary`, `read_chat_history`, `google_read_sheet`, `google_read_file` | `steps` (global cap 8) | 38 s, 92k input tokens |
| 6 | `google_read_sheet`, `google_read_file` | `steps` (**`google_workspace` skill cap 6**) | 56 s, 68k input tokens |
| 5 | `google_read_file`, `google_read_sheet` | `-` (ended by `max_tokens` fallback) | 147 s, 101k input tokens |
| 2 | `google_read_file`, `google_read_sheet` | `-` | 23 s |
| 2 | `list_trips`, `start_trip` | `-` | 8 s |

What this shows:

1. **The cap is hit on productive turns.** The 8-step turn used six different tools across trips,
   chat history and Google files, the trip-itinerary workflow the owner described. Nothing in the
   logs shows a loop, but the metrics list distinct tool names, not arguments, so repeated reads of
   the same file can't be ruled out.
2. **Raising the global cap alone doesn't help Google-only turns.** `google_workspace` declares
   `max_steps=6`. When only Google tools run, the turn's cap is that skill's cap, so the 6-step
   turn stopped at 6 with the global cap at 8.
3. **Time and tokens grow with steps.** Each round resends the whole context: 8 steps cost 92k
   input tokens. The 150 s turn deadline (25 s reserved for the final answer) already forces an
   answer gracefully, and at about 20 steps it will sometimes be what ends the turn.

### Where the cap lives today

| Place | Value |
| --- | --- |
| `chatcore.agent.AgentSettings.tool_steps` | 8 |
| `chatcore.skill_turn.SkillSettings.tool_steps` (set by `SkillsEnvSettings.to_settings(tool_steps=...)`) | 8 |
| `chatcore.app.BotApp(agent_tool_steps=...)` → `SkillSet.build(step_cap=...)` | 8 |
| `Skill.max_steps`, clamped to `step_cap` in `SkillSet._clamp` | per skill; `google_workspace` 6, `cards` 6, `expenses` 5/4, others 2–4 |
| `SkillTurn._current_step_cap()` | largest offered capping skill's `max_steps`; the global cap once a tool outside a capping skill runs |

The platform reads no environment variable for it. The downstream `sb_bot` app (which Loki also
runs) reads `AGENT_TOOL_STEPS` (default 8, allowed 1–20) and `AGENT_TIMEOUT_S` (default 150,
allowed 20–300) and passes them to all three places. Loki sets neither, so it runs at 8.

### Protection that already exists

- At the step cap the next request is sent **without tools**, so the model has to write an answer
  (`final_forced=steps`). Calls beyond the cap in the same response get "Tool step limit reached."
- A round limit (`tool_steps + 1`, plus `max_loads` when skills are on).
- The 150 s turn deadline with a forced no-tools final answer in the reserved time.
- The daily token budget, which forces a final answer when exhausted.

None of these notices a loop. A model that repeats the same call spends the whole cap, time and
tokens, then answers. At 8 steps that is tolerable; at 20 it is not.

## Change

### 1. Default cap 20

- `AgentSettings.tool_steps`, `SkillSettings.tool_steps`, `BotApp(agent_tool_steps=...)` and
  `SkillSet.build(step_cap=...)` default to **20**.
- Per-skill `max_steps` semantics are unchanged: clamped to the global cap and never raising it.
- `google_workspace` `max_steps` 6 → **16**, so Google-only itinerary turns can read several Sheets
  and files. The other skill caps stay as they are.

### 2. Loop guard (new, in the agent loop)

New `AgentSettings` fields, all per turn:

| Field | Default | Behaviour |
| --- | --- | --- |
| `max_identical_calls` | 2 | A call with the same tool name and the same arguments (compared as canonical JSON: sorted keys, no whitespace) runs at most this many times. A further identical call isn't run; it counts as a step and returns the error "Not run: `<tool>` was already called with these arguments this turn. Use the earlier result." The next request is sent without tools (`final_forced=loop`). |
| `max_consecutive_tool_errors` | 4 | After this many tool results in a row are errors (any tool, across rounds; a success resets the count), the next request is sent without tools (`final_forced=errors`). |
| `turn_input_token_limit` | `None` (off) | When set, once the turn's provider input tokens reach it, the next request is sent without tools (`final_forced=tokens`). Lets an app bound the cost of a long turn without lowering the step cap. |

Details:

- Two identical calls stay allowed, because rereading after a write in the same turn is normal
  (`list_trips` after `start_trip`).
- Meta calls (`use_skill`) are not counted by the guard; they have their own `max_meta_calls`.
- Errors produced by the framework (not offered, unknown tool, invalid JSON, step limit, timeout)
  count toward `max_consecutive_tool_errors`, as do tool error results.
- A forced final answer goes through the existing no-tools path, including the empty-response
  retry and `_empty_answer_fallback`, so the user always gets a reply.
- One log line when the guard fires: `final answer forced by loop guard chat=%s reason=%s
  tool=%s tool_steps=%d`. It names the tool, never its arguments.
- The `turn metrics` line gains `repeats_refused=<n>` and the new `final_forced` values. Existing
  fields keep their names and order.

### 3. Unchanged

The deadline and reserve, the round limit, the daily budget, `max_meta_calls`, the research
plugin's own sub-agent step limit, gates and permissions. No migration. The platform still reads
no `AGENT_*` environment variables; apps keep passing settings in.

## Tests

In `packages/core/tests/` (extend `test_agent_step_cap.py`, scripted fake provider):

- Default cap: 20 distinct calls run; the 21st request has no tools and its answer is returned.
- A skill with `max_steps=30` is clamped to 20; `google_workspace` exposes 16.
- Identical call: two identical calls both run; the third isn't executed (the fake tool's call
  count stays 2), returns the "already called" error, and the next request has no tools;
  `final_forced=loop`, `repeats_refused=1`.
- Argument normalisation: `{"a":1,"b":2}` and `{"b": 2, "a": 1}` are identical; different values
  are not.
- Consecutive errors: four error results force a no-tools request (`final_forced=errors`); three
  errors then a success then three errors don't.
- Token limit: with `turn_input_token_limit` set below the second round's usage, the next request
  has no tools (`final_forced=tokens`); with `None`, nothing changes.
- The forced final answer still uses the empty-response fallback when the model returns nothing.
- Request snapshot (`agent_requests_off.json`), skill catalog and routing-set tests pass; any
  snapshot change must be explained in the PR (none is expected, since tool specs don't change).

## Release

Minor release **v0.19.0** (default behaviour change). `CHANGELOG.md` `Unreleased`, under Changed:
default cap 20, `google_workspace` cap 16; under Added: the loop guard and its settings, with an
upgrade note that apps passing their own cap keep it.

Downstream (separate step in the deployments repo, not in this PR): bump the platform pin, set
`AGENT_TOOL_STEPS=20` for Loki and SBot (or raise the `sb_bot` default), consider
`AGENT_TIMEOUT_S` 180–240 for long itinerary turns, and optionally expose the new guard settings
as environment variables.
