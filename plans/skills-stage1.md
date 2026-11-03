# Skill layer, stage 1: budget admission, skill types, declarations, validation

Read `plans/skill-layer.md` first: §1 (D1–D12), §3 (all of it), §5 (hint wording and
`command_form`), §7.4, §9 (flags and the stage 1 row), §10 checks 1–4, 11, 13. This plan is the
stage-1 slice of that design. Where this file and `skill-layer.md` differ, this file wins.

Then read the contracts you build against:
`packages/core/src/chatcore/agent.py` (`_respond_after_typing`), `limits.py`, `plugin.py`,
`app.py` (`BotApp.__init__`), `tools/registry.py`, `testing.py` (`ScriptedProvider`,
`FakePlatform`), `display_time_plugin.py`, each `packages/plugin-*/src/*/plugin.py` (or
`__init__.py` for search/stocks/web), and `apps/sb_bot/src/sb_bot/app.py`.

**Stage 1 is inert for routing.** Production stays `SKILLS_MODE=off`. Skills and gates are
declared, validated at startup and unit-tested, but nothing filters tools yet: every provider
request in `off` mode must be byte-identical to today's. The only behaviour change visible in
production is §7.4 budget admission/recording, which applies in every mode.

Environment: `export UV_CACHE_DIR="$PWD/.uv-cache"`, then `uv sync --all-packages`. Postgres
tests read `TEST_PG_DSN` and skip without it; don't try to start a database.

## You own
- `packages/core/src/chatcore/skills.py` (new)
- `packages/core/src/chatcore/agent.py` (budget admission/recording only; see §1)
- `packages/core/src/chatcore/plugin.py` (optional hooks on `BasePlugin` only)
- `packages/core/src/chatcore/app.py` (`BotApp` params, `SkillSet` build, `command_form`)
- `packages/core/src/chatcore/display_time_plugin.py` (skill + gates only)
- each `packages/plugin-{browse,expenses,extensions,jobs,memory,polls,research,stocks}` plugin
  module: add `skills()` and `tool_gates()` only (no tool changes)
- `apps/sb_bot/src/sb_bot/skills.py` (new), `apps/sb_bot/src/sb_bot/app.py` (settings + wiring)
- `.env.example` if it lists agent settings (names and defaults only)
- new tests: `packages/core/tests/test_skills.py`, `packages/core/tests/test_agent_budget.py`,
  `packages/core/tests/test_agent_request_snapshot.py` + its fixture under
  `packages/core/tests/snapshots/`, `apps/sb_bot/tests/test_skills_catalog.py`, and per-plugin
  gate tests in each plugin's `tests/` (`test_skill_gates.py`)

You may **not** change: `Plugin` protocol required methods, `ToolRegistry`, `Tool`,
`ToolContext`, `Limits` (its public API is enough), tool names/descriptions/parameters, any
prompt file, migrations, `adapter-telegram`. No new dependencies.

## 0. Snapshot first (check 2) — do this before editing agent.py

Write `test_agent_request_snapshot.py` **before** touching `agent.py`, and generate its fixture
from the unmodified code:
- Build a `BotApp` with `FakePlatform`, `memory_stores()`, a `ScriptedProvider`, an
  `AgentResponder`, and a representative tool set (at least two real core plugins plus a couple
  of fake tools, one `admin_only`). Run three scripted turns:
  1. normal: tool call round → answer;
  2. final: `tool_steps` small (e.g. 1) so the second round is forced final;
  3. retry: final round returns empty text → the empty-answer retry fires.
- Serialize every request `ScriptedProvider.calls` recorded (system, messages incl. roles,
  text, tool calls/results, tools specs, tool_choice, max_tokens, session_key) to JSON.
- Make it deterministic only by pinning the nondeterministic inputs: monkeypatch
  `chatcore.agent.secrets.token_hex` and the clock the runtime context uses (or replace the
  wrapper tag and timestamps with fixed placeholders in the serializer). Normalize nothing else.
- `UPDATE_SNAPSHOT=1 uv run pytest packages/core/tests/test_agent_request_snapshot.py` writes
  `packages/core/tests/snapshots/agent_requests_off.json`; without the env var the test asserts
  equality. Generate it now, then never regenerate it. If it ever fails after your agent.py
  edits, fix the code, not the fixture.
- Add a fourth case after §6 is done: the same three turns with `SKILLS_MODE` wiring present
  (skills declared, `BotApp(skills=…)` passed) still match the same fixture.

## 1. Budget admission and recording (§7.4, every mode)

In `AgentResponder._respond_after_typing`:
- **Admission.** Before every provider completion after the first in the turn — each later
  loop round, the empty-answer retry, and the post-turn summary/compaction — call
  `await self.limits.budget_exhausted()`.
  - Loop round that would be non-final → make it the final round (same request as a deadline-
    forced final: `_FINAL_ANSWER_INSTRUCTION`, `tool_choice="none"`). Log once:
    `final answer forced by budget chat=%s tool_steps=%d`. Keep a local `budget_final_forced`
    flag next to `deadline_final_forced` (a later metrics line will read it).
  - Round already final → run it (one bounded final completion is allowed).
  - Retry → skip it; use `_empty_answer_fallback(ctx)`.
  - Summary → skip compaction entirely (the previous summary is kept).
  - The first completion is not re-checked (`Limits.check` already ran in `respond`).
- **Recording.** Call `await self.limits.record(in + out)` immediately after each completion
  returns (loop rounds, retry, summary), not once at the end. Remove the end-of-turn
  `limits.record`. Recording failures must not break the turn: wrap each record in a
  try/except that logs `budget record failed chat=%s error=%s` (class name only).
- **Per-user accounting.** `store.record_usage(...)` still writes the turn total once
  (main completions + retry + summary), but in a `finally` that covers everything from the
  first completion through send, persistence and summary, so a send or persistence failure
  still records usage. Exceptions keep propagating after the `finally`, as today.
- **Research** is untouched: the research plugin already records its own completions; the main
  agent never sees or re-records those tokens.
- The order of everything else (send, persist, summary, `after_turn`) is unchanged, and the
  requests sent are unchanged (snapshot stays green).

## 2. Core types (`chatcore/skills.py`, §3.1)

Exactly `GateState`, `Gate`, `ToolGate`, `Skill` as written in `skill-layer.md` §3.1 (same
field names, defaults and docstrings; `scope` takes `(ChatRef, is_dm: bool)`; `user` takes
`ToolContext`). Severity order for combining: `UNCONFIGURED > NOT_ALLOWED > OFF > AVAILABLE`.

Also in `skills.py`:
- `SkillsMode(StrEnum)`: `OFF="off"`, `SHADOW="shadow"`, `ON="on"`.
- Hint rendering: hints are trusted templates that may contain `{cmd:<name>}` markers, e.g.
  `"…send {cmd:jobtools} reminders on."`. `render_hint(hint, command_form) -> str` replaces each
  marker with `command_form(name)`. Gates never build the bot address themselves.
- Evaluation helpers (no caching anywhere; every call re-runs the gate callables):
  - `async evaluate_tool(name, ctx: ToolContext) -> Gate` (method on `SkillSet`): not
    registered → `UNCONFIGURED`; `admin_only` and not `ctx.is_admin` → `NOT_ALLOWED`; otherwise
    the worse of the owner's scope gate (`scope(ctx.chat, ctx.is_dm)`) and user gate (`user(ctx)`);
    a missing gate counts as `AVAILABLE`. A gate callable that raises → `NOT_ALLOWED`, empty hint,
    WARNING `tool gate failed tool=%s error=%s` (class name only).
  - `async evaluate_skill(skill_name, ctx) -> SkillAvailability` with
    `state: GateState`, `offered: tuple[str, ...]` (available required tools, then available
    optional tools, declaration order, deduplicated), `hints: tuple[str, ...]` (non-empty hints of
    unavailable OFF/NOT_ALLOWED tools, deduplicated, `UNCONFIGURED` never hinted),
    `variant_hints: tuple[str, ...]` (from offered tools' `variant_hints`). State rule per §3.1:
    `AVAILABLE` if ≥1 required tool available, else the *best* non-available state among the
    required tools. A composite whose required tool is unregistered is `UNCONFIGURED` (it is
    already excluded at build time, but evaluation must not resurrect it). A composite never
    offers a tool whose own gate denies it.
  - Every offered set must satisfy `offered ⊆ {t.name for t in registry.available(is_admin)}`.

## 3. Plugin hooks and app wiring

- `BasePlugin.skills() -> list[Skill]` and `BasePlugin.tool_gates() -> dict[str, ToolGate]`,
  both returning empty. The `Plugin` protocol gains nothing; core reads both with
  `getattr(plugin, "skills", None)` and checks `callable`, like `before_turn`.
- A plugin may only supply gates for tools it registers itself (its own `tools()`); a gate for
  any other name is a startup `ValueError` naming the plugin and tool.
- `BotApp.__init__` gains keyword params `skills: Sequence[Skill] = ()` (app composites),
  `general_tools: Sequence[str] = ()`, `agent_tool_steps: int = 8`. It builds
  `self.skills = SkillSet.build(...)` after the registry, in every mode, so validation errors fail
  startup. Existing callers that pass none of these keep working.
- `BotApp.command_form(name: str, *, is_dm: bool) -> str`: `/name@<bot_name>` in groups when
  `group_mentions_only`, otherwise `/name`.

## 4. `SkillSet` validation (§3.2)

`SkillSet.build(plugins, registry, *, composites, general_tools, step_cap)`; registry-relative:
- Errors (`ValueError`, message names the skill/plugin): bad name (`^[a-z][a-z0-9_]{0,47}$`),
  duplicate skill names (plugin or composite), summary > 120 chars, instructions > 800, declared
  skill with < 4 or > 12 examples, keywords not already casefolded, `sticky_turns < 1`,
  `optional_tools` on a plugin (non-composite) skill.
- Plugin skill tool not registered → dropped, WARNING `skill tool missing skill=%s tool=%s`;
  plugin skill left with no tools → not loaded (WARNING `skill not loaded skill=%s reason=empty`).
- Composite with an unregistered **required** tool → not loaded (WARNING
  `skill not loaded skill=%s reason=missing_required`); unregistered optional tools dropped
  silently (DEBUG).
- `max_steps` → clamped to `[1, step_cap]`; `None` stays `None`.
- Generated skills: every registered tool that is in no loaded skill (plugin or composite,
  required or optional) and not in `general_tools` goes into its owning plugin's generated skill,
  named exactly per §3.2: casefold plugin name, replace `[^a-z0-9_]` with `_`, prefix `p_` if
  it doesn't start with a letter, truncate to 32, then `_` + first 8 hex of SHA-256 of the
  **original** plugin name (UTF-8) + `_other`. Put the hash in a module-level helper
  (`_name_hash`) so tests can force a collision. Summary `f"Other {plugin.name} tools"[:120]`,
  examples = the tools' descriptions truncated to 200 chars (exempt from the 4–12 rule), no
  instructions. WARNING once per plugin: `skill generated plugin=%s skill=%s tools=%d`.
- A generated name equal to a declared skill name or to another plugin's generated name →
  `ValueError` naming both plugins (U6: this is the one rule that can reject legacy plugins).
- Tools owned by core (registered by plugins that are part of chatcore) follow the same rules.
- Accessors: `names()`, `get(name) -> Skill | None` (post-clamp, post-drop), `owner(tool)`,
  `gate(tool) -> ToolGate | None`, `general -> tuple[str, ...]` (registered ones only),
  `generated -> tuple[str, ...]`.

Logs may contain only plugin/skill/tool identifiers that come from code, counts and exception
class names (check 13).

## 5. Declarations and SBot catalog (§3.3)

Declare in each owning plugin (skill names exactly as §3.3). Gates as listed; "—" means no gate.
Instructions are `""` in stage 1 (the prompt migration is stage 3). Write 4–12 examples per
skill mixing English, Chinese and Singlish; keywords casefolded (EN + ZH). Examples are routing
data only and are not used yet. Leave out every † tool (they don't exist), and leave out the
`conversation` skill entirely (all its tools are †).

| Owner | Skill | Tools (gate) | max_steps |
|---|---|---|---|
| display_time | `time_zone` | `get_display_time_zone`, `set_display_time_zone`, `reset_display_time_zone` — set/reset: user gate NOT_ALLOWED in a group unless the existing native-admin check passes; in a DM the existing own-DM rule. Reuse the plugin's current check, don't duplicate the logic. | 3 |
| expenses | `expenses` | `add_expense`, `delete_expense`, `list_expenses`, `get_balances`, `settle_up`, `export_expenses`, `read_receipt`, `get_trip` | 5 |
| expenses | `trips` | `start_trip`, `update_trip`, `end_trip`, `get_trip`, `set_fx_rate`, `get_balances` | 4 |
| jobs | `reminders` | `create_reminder` (scope: reminders enabled for that chat), `list_reminders`, `cancel_reminder` | 3 |
| jobs | `watches` | `create_website_watch` (scope: watches enabled; `variant_hints` when rendered fetch is off for the chat), `list_website_watches`, `cancel_website_watch` | 3 |
| polls | `polls` | `create_poll` (scope: DM → NOT_ALLOWED "Polls only work in groups."; group with polls off → OFF with the `{cmd:polltools} on` hint) | 3 |
| memory | `memory` | `remember`, `forget`, `recall` | 3 |
| research | `research` | `research` | 2 |
| browse | `rendered_pages` | `fetch_rendered_page` (scope: rendered on), `build_search_link` | 3 |
| stocks | `stocks` | `stock_analysis`, `stock_digest`, `stock_prices`, `stock_list` | 3 |
| extensions | `extensions` | `request_extension`, `list_extensions`, `run_extension` | 3 |

Expenses user gate (all expense/trip tools): group → AVAILABLE; DM → AVAILABLE only if the
plugin's existing `group_for_dm` resolver returns a group for this requester, else NOT_ALLOWED
with a hint ("Expenses work in a group, or in a DM once you're linked to exactly one group.").
No resolver configured → NOT_ALLOWED in DMs.

`configure_jobs` (jobs) and `configure_rendered_fetch` (browse) gates, owned by their plugins:
scope DM → NOT_ALLOWED; user → AVAILABLE only if the requester is eligible to *create* the
configure request under the plugin's existing rule (setup manager, group creator or
administrator). Reuse the existing permission helpers (`chatcore_jobs.permissions`, the browse
service's checks); if eligibility is only checked inside a pending-action builder, extract a
small read-only helper in the same plugin rather than duplicating it.

Scope gates read the same settings rows the handlers read today (jobs repository settings,
polls service, browse service), keyed as the plugin keys them. They must not read the user or
tool arguments. If a toggle is per-topic in storage today, report it rather than inventing a
mapping. Hint wording follows §5 and uses `{cmd:…}` markers (`jobtools`, `polltools`,
`browsetools`).

`apps/sb_bot/src/sb_bot/skills.py`:
- `GENERAL_TOOLS = ("get_current_time", "read_chat_history", "read_attachment", "recall",
  "search_web", "fetch_web_page")` (`use_skill` doesn't exist yet).
- `COMPOSITES`: `feature_settings` (required `configure_jobs`, optional
  `configure_rendered_fetch`, max_steps 2) and `plan_outing` (required `search_web`,
  `fetch_web_page`; optional `research`, `create_poll`, `build_search_link`; max_steps 6).
- `app.py` passes `skills=COMPOSITES`, `general_tools=GENERAL_TOOLS`,
  `agent_tool_steps=agent_settings.AGENT_TOOL_STEPS` to `BotApp`.

## 6. Settings (§9)

In `apps/sb_bot` (pydantic, explicit env like `AgentEnvSettings`): `SKILLS_MODE` (`off|shadow|on`,
default `off`), `SKILLS_CHATS` (comma-separated chat keys, default empty), `SKILLS_MAX_ACTIVE=3`,
`SKILLS_MAX_LOADS=2`, `SKILLS_MAX_META_CALLS=3`, `SKILLS_STICKY_TURNS=2`,
`SKILLS_ROUTER` (`embed|keyword`, default `embed`), `SKILLS_ROUTER_TIMEOUT_MS=300`. Validate
ranges sensibly (all ≥ 1; timeout 50–5000). Only `off` is implemented: `shadow` or `on` fails
startup with a clear `ValueError` ("SKILLS_MODE=shadow is not implemented yet"). Pass the mode to
nothing else yet; `off` changes no request.

## Tests

`packages/core/tests/test_skills.py` (check 1, 3-without-router, 4-partial, 13):
- bad names, duplicates (plugin vs plugin, plugin vs composite), long summary/instructions,
  example-count bounds → errors naming the culprit;
- generated names: hyphenated plugin name, a 60-char plugin name, a plugin name starting with a
  digit, non-ASCII name, and ten plugins sharing one 32-char normalized prefix → all valid
  (`len ≤ 47`, regex), unique, and every tool reachable through some skill or general;
- forced hash-prefix clash (monkeypatch `_name_hash`) and a generated name equal to a declared
  skill → `ValueError` naming both plugins;
- missing tools dropped with the WARNING (caplog); empty skill and composite-missing-required not
  loaded; optional tools dropped; `max_steps` clamped for `step_cap` 1 and 8;
- a legacy plugin with no `skills`/`tool_gates` attributes at all (not a `BasePlugin`) gets its
  `<plugin>_other`; a plugin supplying a gate for another plugin's tool → `ValueError`;
- evaluation: severity combination; admin_only → NOT_ALLOWED for non-admin; raising gate →
  NOT_ALLOWED; offered ⊆ `registry.available(is_admin)` for every skill × admin/non-admin;
  partial availability (one required off → others offered, hint present); composite never
  restores a denied tool; no caching: a gate whose answer flips between calls is seen flipped
  on the very next evaluation, alternating two users in one chat gives each their own answer;
- `render_hint` with `command_form` for group/DM.
- check 13: sentinel strings in plugin-irrelevant inputs (skill text is code, so here: a
  fabricated tool name and malformed args fed through a turn) never appear in caplog.

`packages/core/tests/test_agent_budget.py` (check 11), with a real `Limits` over the memory
cache, a small `DAILY_TOKEN_BUDGET` override and `ScriptedProvider` usage numbers:
- cap crossed after the first completion → second round is sent as final
  (`tool_choice="none"` + final instruction) and is the last completion; INFO log present;
- cap crossed before the retry → no retry request, fallback text sent;
- cap crossed before the summary → compaction not called (provider gets no summary request),
  previous summary intact;
- `Limits` total and the per-turn `record_usage` total both equal the sum of the provider's
  reported usage (rounds + retry + summary) exactly;
- concurrent turns: after turn A's first completion records, turn B's admission sees it;
- **barrier test** (the §7.4 bound): two chats, provider gated by `asyncio.Event`s so both turns
  are admitted to their second completion before either records; one of them crosses the cap;
  assert both in-flight completions complete, each turn then gets at most one more (final)
  completion, and the total past the cap equals exactly (crossing completion's excess) + (the
  other in-flight completion) + (one final per active turn), as the scripted numbers dictate;
- send failure (`FakePlatform.send` raises) → `limits` and `record_usage` still recorded;
- a `limits.record` that raises → turn still answers, warning logged without user text.

`packages/core/tests/test_agent_request_snapshot.py` (check 2): §0.

Per plugin `tests/test_skill_gates.py` (check 3/4 with fakes; use each plugin's existing test
fakes or Postgres fixtures where the settings live in Postgres, skipping without `TEST_PG_DSN`):
- reminders off → `reminders` AVAILABLE, offers list/cancel, hints create with
  `/jobtools@<bot> reminders on`; toggled on from topic A → next evaluation in topic B sees it;
- rendered fetch off → `watches` offers `create_website_watch` with the variant hint;
  `rendered_pages` still offers `build_search_link`;
- polls off / DM; expenses DM with 0, 1, 2 linked groups, and a change between evaluations;
- time-zone set in a group: admin vs member alternated, admin revoked between evaluations;
- configure gates: eligible vs non-eligible requester.

`apps/sb_bot/tests/test_skills_catalog.py`: building the real SBot plugin list (as
`app.py` does, with fakes/env for optional pieces) validates for: full config; no Tavily; no
`BROWSER_RENDER_URL`; extensions off; receipt vision off; no research. Assert the expected
skill names per config, that `plan_outing` is dropped when `search_web` is absent, and that
`feature_settings` loads without `configure_rendered_fetch`. Settings: `SKILLS_MODE` default
`off`, `shadow`/`on` rejected, defaults as §6.

## Done when
- `uv run pytest -q` passes (Postgres tests may skip in your sandbox), and
  `uv run ruff check . && uv run ruff format --check .` are clean.
- The snapshot fixture was generated before `agent.py` changed and still matches.
- No `telegram` import outside the adapter; no framework package imports `sb_bot`.
- No user text, tool arguments or secrets in any new log line.
- **Not in this stage** (don't build): router, embeddings use, continuation, `use_skill`,
  shadow/on behaviour, tool filtering by gates or skills, prompt changes, new tools, metrics line.
