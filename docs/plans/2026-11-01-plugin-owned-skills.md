# Plugin-owned skill instructions and core router settings

Date: 2026-11-01. Status: merged and live (history #41, merged to `main` with history #38). Follows the nested-packages move
(`docs/plans/2026-10-31-repo-layout.md`, PR 5) and the skill layer (`docs/plans/skill-layer.md`).

## Problem

A second bot built on chatcore would get skills with no behaviour guidance and would have to
copy SBot's router setup.

- **Instructions live in the app.** Every skill's behaviour text (expenses undo, receipt flow,
  "don't repeat the echo", time-zone rules, persona rules) is in
  `apps/sb_bot/src/sb_bot/skills.py` (`SKILL_INSTRUCTIONS`). Core replaces each skill's own
  `instructions` with it, and no plugin fills that field in. All 12 entries describe plugin or
  core behaviour, so none of it is really specific to SBot. Even "Expenses default to SGD" is
  the expenses plugin's `DEFAULT_CURRENCY`.
- **Router settings are parsed in the app.** `apps/sb_bot/src/sb_bot/app.py` reads the 11
  `SKILLS_*` environment variables, validates them and builds core's `SkillSettings`.
- **The general tool set lives in the app.** SBot's `GENERAL_TOOLS` list is a sensible default
  for any bot.

The router itself is already core (`chatcore.skill_router`, `chatcore.skill_turn`). This
change moves the setup around it. Skill-layer decision D1 already defines a skill as a bundle
that includes its short instructions.

## Design

1. **Plugins own their default instructions.** Each skill's text moves onto its
   `Skill(..., instructions=...)` declaration, next to the tools it describes. Plugin skills go
   in that plugin's `plugin.py`; `time_zone` goes in `chatcore/display_time_plugin.py` and
   `conversation` in `chatcore/conversation.py`. `SkillSet` already validates the
   800-character cap.
2. **Apps add or override.** `SkillSet.build` / `BotApp` keep `instructions=` /
   `skill_instructions=` as *overrides* (the existing replace behaviour, so the API is
   compatible) and gain `instruction_additions=` / `skill_instruction_additions=`, which append
   bot-specific text after the plugin's. The cap applies to the merged text.
3. **Core owns router settings.** `chatcore.skill_turn.SkillsEnvSettings` (a
   `pydantic-settings` class) reads the same `SKILLS_*` names with the same defaults, bounds and
   validation, and `to_settings(tool_steps=...)` builds `SkillSettings`. The app calls it
   through its existing `settings_from_env` helper, so the process environment still can't leak
   in. Env names and production behaviour don't change.
4. **Core owns a default general tool set.** `chatcore.skills.DEFAULT_GENERAL_TOOLS` holds
   today's six tools. `BotApp(general_tools=None)` uses it, and `()` opts out.
5. **The app keeps only what makes SBot SBot.** `skills.py` shrinks to `COMPOSITES`
   (`plan_outing`, `feature_settings`). `GENERAL_TOOLS` and `SKILL_INSTRUCTIONS` are removed.
   The `system_skills.md` persona prompt stays in the app. Its line telling the model to load
   the expenses skill when its tools aren't offered stays global, because skill instructions
   are only injected once a skill is active.

## No behaviour change for SBot

- `apps/sb_bot/tests/data/skill_instructions_baseline.json` captures today's 12 instruction
  texts from `main`. A new test asserts that each owning plugin or core skill declares
  exactly that text, and that SBot passes no overrides or additions, so the effective text is
  byte-for-byte identical.
- A test asserts `SkillsEnvSettings` defaults and bounds match the old `AgentEnvSettings`
  `SKILLS_*` fields. The existing catalog, routing-set, prompt-migration and on-mode e2e tests
  must pass unchanged, and the agent request snapshot must not change.
- `AGENTS.md` "Skills" is updated: plugins own instructions, apps add or override, and
  composites stay in the app.

## Out of scope

- Ready-made shared composites in core. Wait until a second bot needs `plan_outing` or
  `feature_settings`.
- Moving persona or prompt text out of `system_skills.md`.
