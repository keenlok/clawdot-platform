# Agent guide for clawdot-platform

Rules for any agent (Codex, Claude, or a person) changing this repository. `ARCHITECTURE.md` is the
master spec; these are the rules a PR is checked against. Rules marked **(test)** are enforced
by `tests/test_package_boundaries.py` or `tests/test_plans_index.py` and fail CI.

## Repository layout

| Path | What belongs there |
| --- | --- |
| `packages/core/` | The framework (`chatcore`): agent loop, skills, stores, security, administration. |
| `packages/adapters/*/` | Chat platform adapters. Only `adapters/telegram` may import `telegram` (Ruff `TID251`). |
| `packages/plugins/*/` | **Only plugins the bot loads**: a `Plugin` with tools, commands, skills or gates. |
| `services/` | Standalone framework-side processes that run outside the bot (`browser-renderer`, `release-control`). They aren't plugins. |
| `apps/_scaffold/` | Working generic example app, persona, compose/deploy skeleton and CI tests. |
| `ARCHITECTURE.md` | The architecture spec and decision record (formerly `PLAN.md`; section numbers unchanged). |
| `plans/` | Plans, designs and review records, indexed with their status in `plans/README.md`. |
| `docs/` | Operator guides, deployment records and older design records (`docs/superpowers/`). |

- A new long-running process with its own entry point or container goes in `services/`, not `packages/plugins/`.
- Framework code (`packages/`, `services/`) never imports application modules (including downstream `sb_bot`) **(test)**.

## Plugin module layout

Every plugin package (`packages/plugins/<name>/src/chatcore_<name>/`) uses these modules:

| Module | Contains |
| --- | --- |
| `__init__.py` | Docstring, re-exports and `__all__` only: no classes, functions or logic **(test)**. |
| `plugin.py` | The `Plugin` class, its tools and commands, and `skills()`. |
| `gates.py` | `build_gates(plugin) -> dict[str, ToolGate]`. `plugin.py`'s `tool_gates()` only returns `build_gates(self)`. `Gate(` and `ToolGate(` appear nowhere else in the plugin **(test)**. |
| `settings.py` | The plugin's `BaseSettings` class(es). |
| `permissions.py` | Shared permission helpers, if any. `gates.py` and handlers call them. |
| `service.py`, `repository.py`, `models.py`, other helpers | Domain logic. |
| `migrations/` | `NNNN_name.sql`, additive only. An applied file never changes. |

Known exceptions, not to be copied: `chatcore_memory` keeps its settings in `plugin.py`, and
`chatcore_expenses` keeps `ReceiptSettings` in `receipt.py`.

Why gates are separate: gates decide who may use a tool and what the model is offered. Keeping
them out of ordinary tool code lets release-control policy review them separately (layout plan
decision D7). Treat any change to `gates.py` or `permissions.py` as a permission change, and
call it out in the PR description. Handlers must still check permissions at call time; a gate
is never the only check.

## Plans and status

- `plans/README.md` is the index of every plan, design and review record, with its status
  (Live, Live limited, Shipped inactive, Planned, Record) and PR.
- A PR that adds a plan adds its row; a PR that changes what a plan has delivered (built,
  merged, enabled, switched off) updates the row in the same PR. Every plan file must be linked
  from the index, and every link in it must resolve **(test)**.
- Requests that have no plan yet stay in `ARCHITECTURE.md` §12 and are listed at the end of the
  index.
- Architecture decisions go in `ARCHITECTURE.md`; keep its section numbers stable, because plans
  refer to them ("§3d").

## Skills (`plans/skill-layer.md`)

- A plugin declares its skills in `plugin.py` (`skills()`: name, summary, examples, tools,
  keywords) and its gates in `gates.py`. Core skills (`time_zone`, `conversation`) are declared
  the same way in core.
- **The plugin owns each skill's behaviour instructions.** They go on the `Skill(...,
  instructions=...)` declaration, as a `<SKILL>_INSTRUCTIONS` constant in the same module,
  and are capped at 800 characters. Write them for any bot, not just SBot.
- An app only adds to or overrides them: `BotApp(skill_instruction_additions={...})` appends
  bot-specific text; `skill_instructions={...}` replaces it and should be rare. Downstream apps own bot-specific additions and test their resulting prompts.
- Router settings come from core: `chatcore.skill_turn.SkillsEnvSettings` reads the `SKILLS_*`
  environment variables. The default general tool set is `chatcore.skills.DEFAULT_GENERAL_TOOLS`.
  Apps don't redefine either.
- A skill that uses tools from more than one plugin is an app composite (`COMPOSITES` in
  `apps/<app>/src/<app>/skills.py`, which holds nothing else). No plugin imports another
  plugin's `skills` or `gates` module **(test)**.
- A new tool needs its argument descriptions, a gate if it's restricted, a place in a skill, and
  an updated `routing_set.jsonl` and request snapshot where the tests require it.

## Import boundaries

- Apps import plugins and adapters only through the package's top level
  (`from chatcore_jobs import JobsPlugin`, never `chatcore_jobs.plugin`) **(test)**. When the
  app needs a new name, export it from the package's `__init__.py` and add it to `__all__`.
  Core (`chatcore.*`) is the framework API and may be imported by submodule.
- Every name in a plugin's `__all__` must resolve **(test)**.
- `chatcore_extensions` exports lazily (module `__getattr__`), so the privileged extension
  worker loads only `models`, `runtime`, `sandbox` and `worker` **(test)**. Don't add eager
  imports to its `__init__.py`.
- A plugin may depend on another plugin only through its declared `pyproject.toml` dependency.
  Today that's browse → jobs and research → web.

## Moving or splitting code

When you move code between modules (for example splitting an `__init__.py`):

1. **Keep logger names.** If code used `logging.getLogger(__name__)`, pin the old name
   (`getLogger("chatcore_web")`), so log records, filters and alerts don't change.
2. **Move test patch targets with the code.** `monkeypatch.setattr("pkg.name", ...)` patches the
   module where the name is *looked up*. After a move, point patches at the new module
   (`chatcore_web.plugin.safe_get`). A stale target can still pass while the fake is never
   used. Check with a mutation run: make the real function raise and compare failing tests
   against `main`.
3. **Keep public names importable** from the package by re-exporting them.
4. **Use `git mv`** so history follows the file, and show in the PR that the diff is a move.
5. **Prove there's no behaviour change:** the agent request snapshot
   (`packages/core/tests/snapshots/agent_requests_off.json`) and the skill catalog and
   routing-set tests must pass unchanged.

## Release-control and protected files

Bot-generated and human PRs released through release control may only change enumerated
ordinary plugin files and their tests (`plans/2026-10-31-self-improvement-plan.md`,
`plans/2026-11-01-human-pr-intake.md`). Expect maintenance review for:
`.github/`, scaffold deploy files, Dockerfiles, Compose files, `.dockerignore`,
`pyproject.toml`, `uv.lock`, every `migrations/`, package `__init__.py` files, core, `services/`,
and, if D7 is accepted, `gates.py` and `permissions.py`. `ARCHITECTURE.md` §3d has the per-file inventory.
Moving a file that the installed policy manifest lists requires a manifest update.

## Checks before a PR

```sh
uv sync --locked --all-packages
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check .
uv run pytest -q -m "not browser and not sandbox_integration"
```

`ruff format --check` already lists some files on `main`; format only the files you change.
CI (`.github/workflows/ci.yml`) runs lint and tests with Postgres. Platform CI builds/tests the real scaffold and independently installed wheels. Downstream
applications pin reviewed platform releases and own their deployments. Keep framework changes
behaviour-neutral unless the PR says otherwise.

## Working rules

- Secrets never go in code, tests or logs. Gitleaks runs in pre-commit.
- Don't add dependencies without saying so in the PR.
- Don't edit an applied migration.
- Follow the execution rules below for who plans, writes and reviews the code.

## Execution and review

Who writes the code depends on the session's environment and which models it can reach. Check
before starting (for example `command -v codex`) instead of assuming.

| Environment | Plan | Execute | Review |
| --- | --- | --- | --- |
| Owner's computer, Codex CLI installed | Claude | Codex (`ARCHITECTURE.md` §7b models) | Codex, then Claude's final review |
| Cloud or remote session without Codex | The session's model | The session's model | The session's own review pass on the diff; the owner may add a Codex review from their computer |
| Any session where the model the plan names isn't available | Use what is available and say so in the PR | | |

- The plan is written to `plans/` and approved before code is written, whoever executes it.
- The PR description says who executed and who reviewed, by role (for example "Codex executed,
  Claude reviewed" or "implemented in a cloud Claude session, self-reviewed").
- A missing tool is not a reason to skip a step: run the checks above in every environment, and
  name in the PR any check that couldn't run (for example integration tests that need Postgres)
  and why.
