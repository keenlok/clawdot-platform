# Repository layout cleanup: services, plugin boundaries, docs

Revision 2 (2026-11-01). Status: PR 1 merged ([#25](https://github.com/keenlok/clawdot-platform-history/pull/25)); PR 2 merged ([#26](https://github.com/keenlok/clawdot-platform-history/pull/26)); PR 3 merged ([#31](https://github.com/keenlok/clawdot-platform-history/pull/31)) with `AGENTS.md`. Follow-up (2026-11-01, approved in chat): `PLAN.md` renamed to `ARCHITECTURE.md` (section numbers unchanged) and `plans/README.md` added as the status index, enforced by `tests/test_plans_index.py`. PR 5 (nested `packages/`) merged ([#38](https://github.com/keenlok/clawdot-platform-history/pull/38)). PR 4 (docs consolidation) deferred; D7 (protecting gates) is still an owner decision. Revised after the skill layer (stages 0–3, PRs #17–#22), CI and
pull-based auto-deploy (#24) and the human-PR-intake plan (#23) merged. See "What changed since
revision 1" for the deltas.

Read first: `ARCHITECTURE.md` §3d (framework layout and the "lift `packages/` out unchanged" goal), root
`pyproject.toml`, `.github/workflows/ci.yml`, `README.md` (layout table),
`apps/sb_bot/src/sb_bot/app.py` and `apps/sb_bot/src/sb_bot/skills.py`, `plans/skill-layer.md`
§3–§3.4, §8, §9, `plans/2026-11-01-human-pr-intake.md` (D4, §2), and
`plans/2026-10-31-self-improvement-plan.md` ("installed policy manifest").

## Problem

`packages/` is flat by design (one uv member per package, and `adapter-*`/`plugin-*` prefixes
group them). That part stays. What's wrong:

1. **`packages/plugin-release-control` isn't a plugin.** About 33k lines in about 50 modules,
   with no `Plugin` class and no `skills()`. `sb-bot` doesn't depend on it, and only 2 of its
   files import `chatcore`. It runs as separate protected services
   (`python -m chatcore_release_control.*`).
2. **`packages/browser-renderer` is a standalone HTTP service** with app-specific names
   (`sb-browser-renderer`, `sb_browser_renderer`) inside the framework tree.
3. **The app imports plugin internals.** `apps/sb_bot/src` imports 23 deep `chatcore_<plugin>.<module>`
   paths. `chatcore_extensions/__init__.py` exports nothing.
4. **Plugin module layout is inconsistent.** `search` (708 lines), `web` and `stocks` keep
   everything (tools, settings, and for stocks the `skills()` declaration) in `__init__.py`.
   Tool gates (`tool_gates()`, which hold authorization checks) are mixed into each plugin's
   `plugin.py` next to ordinary tool code.
5. **Design records live in five places** (`ARCHITECTURE.md`, `plans/`, `docs/superpowers/`,
   `docs/deployments/`, `handovers/`).

## What changed since revision 1

| Change on `main` | Effect on this plan |
| --- | --- |
| **Skill layer.** `BasePlugin.skills()`/`tool_gates()`; each plugin declares its skills and gates. The app supplies composites, the general tool set and per-skill instructions (`apps/sb_bot/src/sb_bot/skills.py`). New core modules: `skills.py`, `skill_router.py`, `skill_turn.py`, `turn_metrics.py`, `security/injection.py`. | Confirms how responsibility splits: plugins declare *what* (tools, examples, gates), the app owns *how to behave* (instructions, composites). No new package type, so `packages/` layout is unaffected. PR 3 must decide where `skills()` and `tool_gates()` live in each plugin. Stage 4 (new poll, conversation and core tools) and stage 5 still touch plugin files. |
| **Release-control policy (self-improvement plan, human-PR-intake D4).** Bot-generated and human PRs may only change *enumerated ordinary plugin Python files and tests*. `.github`, deploy, Dockerfiles, package metadata, the lockfile and the regression harness are protected. Neither policy manifest is installed yet (intake D5: rehearsal first). | **Ordering now matters.** Every path this plan moves ends up in that manifest. Moving files before the manifest is written costs nothing; afterwards, each move becomes a maintenance release. A predictable per-plugin file layout also makes "ordinary plugin file" easy to enumerate, and lets gates be protected separately (PR 3). |
| **CI** (`.github/workflows/ci.yml`) runs `ruff check packages apps tests` and pytest over the configured testpaths. | PR 1 must add `services` to the CI ruff paths, or the moved code silently stops being linted. |
| **Pull-based auto-deploy** (`apps/sb_bot/deploy/autodeploy/`). | No path references to the moved packages; unaffected. Every merge to `main` deploys, so PR 1 must keep the bot image identical (already a requirement). |
| **About 13 new live plan documents** in `plans/`, some driving in-flight Codex tasks (`human-pr-intake`, `skills-*`). | PR 4 (moving the docs) would break references in active work. Defer it. |

## Production facts that make this safe

- Release-control is **inactive** in production and its policy manifest isn't installed
  (`docs/deployments/2026-10-31-self-improvement-source.md`; intake plan "plan only, not built,
  not installed").
- The browser renderer is **not deployed** (base digest `sha256:REPLACE_BEFORE_DEPLOY`).
- The bot image builds with `uv sync --frozen --no-dev --package sb-bot`, and neither moved
  package is a dependency.
- No migrations live in either moved package; `migration_path_prefixes` in
  `p08-host-service.json.template` doesn't change.
- Skills are off for every chat except the owner's test DM and test group
  (`SKILLS_MODE=on`, `SKILLS_CHATS` limited). Rollback is `SKILLS_MODE=off`. PR 3 doesn't change
  any skill declaration, only where it lives.

## Decisions (recommended; confirm before starting)

| # | Decision | Recommendation |
| --- | --- | --- |
| D1 | Where standalone services live | New top-level `services/` workspace glob. It's framework-side (same "no `sb_bot` imports" rule) and lifted out with `packages/`. |
| D2 | Release-control naming | Move the directory only (`services/release-control`). Keep `chatcore-release-control`/`chatcore_release_control`, because operator docs, `python -m` entry points and `/etc|/var/lib/chatcore-release-control` use them. |
| D3 | Browser-renderer naming | `services/browser-renderer`, distribution `chatcore-browser-renderer`, import `chatcore_browser_renderer`. Keep Compose/image names (`sb-bot-browser`). |
| D4 | App/plugin boundary | Plugins export what apps need from `__init__`, enforced by a workspace test. Skills and gates reach the app through `plugin.skills()`/`tool_gates()`, never by import. |
| D5 | Skill ownership (confirms what's merged) | Plugin declares `Skill` (name, summary, examples, tools, keywords) and gates. App owns instructions, composites, the general tool set and `system_skills.md`. Skills spanning plugins are app composites. No `packages/skill-*`. |
| D6 | Standard plugin module layout | `__init__.py` (re-exports only), `plugin.py` (Plugin class, tools, `skills()`), `gates.py` (`tool_gates()` and the gate functions), `settings.py`, plus domain modules (`service.py`, `repository.py`, `models.py`, `migrations/`). `permissions.py` stays where it already exists, and `gates.py` may call it. |
| D7 | Gates in the release policy | Treat `gates.py` and `permissions.py` as protected (maintenance path) rather than ordinary plugin files when the manifest is written. Handlers re-check at call time, but gates decide what the model is offered and hinted. **Owner decision; it affects the intake plan's D4.** |
| D8 | Docs consolidation | Deferred until the skill stages and human-PR intake are no longer in active Codex work. |
| D9 | Large files (`chatcore_expenses/plugin.py` 2,411 lines, `agent.py` 1,309) | Still out of scope. Splitting expenses into tools, gates and skills by D6 is the natural follow-up once stage 4 lands. |

## Delivery order

1. **PR 1 now.** It touches no plugin and no skill code, and must land before the release-control
   manifest is written and before the intake Codex tasks start editing release-control.
2. **PR 2 any time.** Small; it touches `app.py` and plugin `__init__` files only.
3. **PR 3 after skill stage 4 merges and before the policy manifest is written.** Stage 4 edits
   the polls, jobs and core plugins' `plugin.py`. Doing the gates extraction in parallel means
   double conflicts.
4. **PR 4 deferred** (D8).

---

## PR 1: `services/` for standalone services

### Steps

1. `git mv packages/plugin-release-control services/release-control` and
   `git mv packages/browser-renderer services/browser-renderer`.
2. Rename the browser renderer (D3): `pyproject.toml` name;
   `git mv src/sb_browser_renderer src/chatcore_browser_renderer`; replace
   `sb_browser_renderer` in its `src/` and `tests/`, including the logger name in `server.py`;
   update the Dockerfile's `--package` and `CMD`.
3. Root `pyproject.toml`: `members = ["packages/*", "services/*", "apps/*"]`; rename the
   `[tool.uv.sources]` entry; `testpaths = ["packages", "services", "apps", "tests"]`.
4. **CI:** `.github/workflows/ci.yml` → `uv run ruff check packages services apps tests`.
5. Dockerfiles: add `COPY services/ services/` to `services/browser-renderer/Dockerfile`
   (required) and `apps/sb_bot/Dockerfile` (so workspace discovery matches `uv.lock` under
   `--frozen`).
6. `apps/sb_bot/compose.browser.yaml`: `dockerfile: services/browser-renderer/Dockerfile`.
7. `apps/sb_bot/tests/test_deploy_files.py`: new renderer path and `CMD`. Assert both
   Dockerfiles copy `services/` and that the CI ruff step lists `services`.
8. Paths in docs and evidence:
   - `apps/sb_bot/deploy/release-control/evidence-index.json` and its `README.md`: path-only
     replacement, called out in the PR description.
   - `services/release-control/README.md` test and ruff commands.
   - **Live plans** that name the old path: `plans/2026-11-01-human-pr-intake.md` (header,
     test command). Leave completed historical plans and deployment records unchanged.
   - `docs/rendered-fetch.md`, the `README.md` layout table, and `ARCHITECTURE.md` §3d (layout tree, and
     `git filter-repo --path packages/ --path services/ --path tests/`).
9. `uv lock`, then inspect the diff: only the rename and source-path changes should appear.

### Verification

```sh
uv lock --check
uv sync --locked --all-packages
uv run ruff check packages services apps tests && uv run ruff format --check .
PG_BIN=$(pg_config --bindir) uv run pytest -q -m "not browser and not sandbox_integration"
grep -rn "plugin-release-control\|sb_browser_renderer\|sb-browser-renderer\|packages/browser-renderer" \
  --exclude-dir=.git --exclude-dir=superpowers --exclude-dir=deployments .   # only completed historical plans may remain
docker build -f apps/sb_bot/Dockerfile .
```

- The test count matches the pre-change baseline.
- `uv pip list` for `--package sb-bot` is identical before and after, because auto-deploy ships
  every merge.
- CI is green on the PR.

---

## PR 2: stable plugin exports and an app boundary check

1. Re-export (with `__all__`) from each plugin's `__init__.py` the names `apps/sb_bot/src`
   imports today. Regenerate the list at implementation time with
   `grep -rhoE "^from chatcore_[a-z_]+\.[a-z_]+ import.*" apps/sb_bot/src`. As of revision 2 it
   covers extensions (11 names), jobs (plugin and watch API), browse, expenses, polls, memory,
   research and the Telegram bridge. Re-export only. Watch for import cycles
   (`chatcore_browse` → `chatcore_jobs`) and for import cost in `chatcore_extensions`.
2. Rewrite app imports to top-level packages only. `chatcore.*` (core, including
   `chatcore.skills` and `chatcore.skill_turn`) is the framework API and stays exempt.
3. Add `tests/test_package_boundaries.py` (AST):
   - `apps/*/src` has no `chatcore_<plugin>.<submodule>` imports.
   - `packages/` and `services/` never import `sb_bot`.
   - No plugin imports another plugin's skill or gate module (skills compose only through app
     composites, D5).
4. Add one line to `ARCHITECTURE.md` §3d stating D4 and D5.

Verification: unit suite and Ruff pass. The boundary test fails on a deliberately added deep
import and passes once it's removed.

---

## PR 3: standard plugin module layout (D6, after skill stage 4)

1. `search`, `web`, `stocks`: split `__init__.py` into `plugin.py`/`settings.py` (and
   `gates.py` if gated). `__init__` keeps every currently imported name.
2. Every plugin with `tool_gates()` (browse, expenses, jobs, polls, and core's display-time
   plugin): move the gate functions and `tool_gates()` body into `gates.py`. `plugin.py`'s
   `tool_gates()` becomes `return gates.build(self)`, or similar.
3. Leave `skills()` in `plugin.py`, next to the tools it names. `SkillSet` validation already
   catches mismatched tool names at startup.
4. No behaviour change: skill names, examples, tool names, schemas, gate logic and
   `SKILL_INSTRUCTIONS` stay byte-identical. The agent request snapshot
   (`packages/core/tests/snapshots/agent_requests_off.json`) must not change, and nor must the
   skill catalog and routing-set tests (`apps/sb_bot/tests/test_skills_catalog.py`,
   `test_skill_routing_set.py`).
5. Write down the resulting per-plugin file inventory (ordinary vs protected per D7) in
   `ARCHITECTURE.md` §3d, for whoever writes the release-control policy manifest.

Verification: `git diff -M --stat` shows moves; unit suite, Ruff, snapshot, skill-gate tests
(`packages/*/tests/test_skill_gates.py`) and catalog tests pass unchanged.

---

## PR 4: one place for design records (deferred, D8)

When neither the skill stages nor the human-PR intake have open Codex tasks: `git mv` `plans/` →
`docs/plans/`, `docs/superpowers/plans` → `docs/plans/`, `docs/superpowers/specs` →
`docs/specs/`, `handovers/` → `docs/handovers/`. Plans link to each other relatively, so moving
them together keeps those links intact. Then fix inbound links from `README.md`, the release-control
READMEs and `docs/`, add `docs/README.md`, and run a relative-link check over all `*.md`.

---

## PR 5: nested `packages/` (approved in chat, 2026-11-01)

The owner prefers a nested layout. It's a path-only move like PR 1, and it must land before the
release-control policy manifest records plugin file paths.

```
packages/core/
packages/adapters/{console,telegram}/
packages/plugins/{browse,expenses,extensions,jobs,memory,polls,research,search,stocks,web}/
```

- `git mv` every package. Distribution and import names (`chatcore-web`, `chatcore_web`) don't
  change, so no Python imports change.
- Workspace members become `packages/core`, `packages/adapters/*`, `packages/plugins/*`,
  `services/*` and `apps/*`. Then run `uv lock`; only editable source paths should change.
- Path references: the Ruff `TID251` exemption for the Telegram adapter, the app Dockerfile's
  `download_model.py` copy, the `p08` host-service template's migration prefixes, the
  release-control evidence index, the boundary-test globs, and two extension tests that use
  `parents[3]` as the repo root.
- Docs: `AGENTS.md`, `ARCHITECTURE.md` (layout tree and current-path mentions), `README.md`, and
  the extensions README. Historical plans keep the old paths.
- Verification: `uv lock --check`, the `sb-bot` dependency export identical before and after, the
  image `uv sync` reproduced, and the full suite matching `main`.

## Out of scope

- Splitting `chatcore_expenses/plugin.py` and `agent.py` (D9).
- Grouping core's skill modules (`skills.py`, `skill_router.py`, `skill_turn.py`,
  `turn_metrics.py`) into a `chatcore/skills/` subpackage. Possible after stage 5; moving them
  while stages 4–5 are open only creates conflicts.
- Moving `display_time_plugin.py`, `administration/` or `privacy.py` out of core.
- Entry-point plugin discovery; renaming existing `chatcore_*` packages or `sb-bot`.
