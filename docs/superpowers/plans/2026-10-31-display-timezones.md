# Display Time Zones Implementation Plan

> Follow-up decision (2026-10-31): implementation of natural-language timezone preference tools
> is tracked in [the approved prompt-based timezone settings plan](2026-10-31-prompt-timezone-settings.md).
> Its explicit current-user requests supersede the command-only and read-tools-only choices below;
> all UTC, scope, persistence and scheduling boundaries remain unchanged.

> For agentic workers: use the executing-plans and test-driven-development skills. The user
> approved Luna implementation, independent Sol review and fix rounds. Do not ask again for
> routine actions within this scope. The user subsequently authorized PR creation, merge and
> deployment after review. No live Telegram smoke-test messages are needed.

**Goal:** Persist per-group and self-owned DM display zones and apply them to displayed instants
and model time context while UTC storage and execution stay unchanged.

**Architecture:** A core display-time service owns validation, persistence and formatting. Use
the existing `Stores.extra` dictionary for a per-Stores service instance, not process-global state.
A small plugin exposes explicit setting commands and read-only model tools; consumers resolve
the preference using current reply chat/user/is_dm. Existing shared contracts stay unchanged.

**Tech stack:** Python 3.12, stdlib zoneinfo, async database protocol, uv, pytest and Ruff.

## Execution constraints and ownership

Work only in `/path/to/worktrees/display-timezones/sb_bot`, originally detached at
`0568954a14575625c42375e581343c22169a9e63`, now on `codex/display-timezones` with current
`origin/main` incorporated. Preserve the shared main checkout and other sessions' files.
Read the approved spec next to this plan, `plans/COMMON.md`, ARCHITECTURE.md sections 3/3d/6a/7b and
`docs/reusable-jobs.md`. Do not change core events, CoreRepo, ChatSettings, ToolContext, CommandContext,
ChatPlatform or Plugin contracts. Do not touch plugin-memory, routing/tag logic, ARCHITECTURE.md or other
sessions' plans. No new dependencies expected. Use `UV_CACHE_DIR="$PWD/.uv-cache"` for every uv run.
Use fakes only; no external API calls, local Docker changes or production access.
Historical CLI launcher notes in other plans do not apply: Luna implementation and Sol review use
in-session collaboration agents. Do not launch Bash `codex` or `claude` subprocesses.

## Task 1: Baseline and display service

Files: create `packages/core/src/chatcore/display_time.py`,
`packages/core/src/chatcore/display_time_store.py`,
`packages/core/src/chatcore/migrations/0007_display_time.sql`,
`packages/core/tests/test_display_time.py` and `test_display_time_store.py`.

- [x] Run baseline `uv run pytest -m 'not integration and not browser and not sandbox_integration' -q`.
  Record counts and any existing failures; the documented button-topic failure is known on main.
  Investigate unexpected failures without changing unrelated source.
- [x] Write behavioral tests first, run them red, then implement validation and formatting.
  Contract: `validate_zone(value: str) -> str`, `format_instant(instant, zone: str) -> str`.
  Validate installed IANA names plus UTC, reject abbreviations/paths, require aware instants.
  Example assertions:

```python
at = datetime(2026, 11, 1, 1, tzinfo=UTC)
assert format_instant(at, "Asia/Singapore") == ("2026-11-01 09:00:00 Asia/Singapore (UTC+08:00)")
assert "UTC-04:00" in format_instant(datetime(2026, 11, 1, 5, 30, tzinfo=UTC), "America/New_York")
assert "UTC-05:00" in format_instant(datetime(2026, 11, 1, 6, 30, tzinfo=UTC), "America/New_York")
```

- [x] Add a separate preference store and additive SQL table keyed by chat_key, subject_key.
  Empty subject is group setting; DM subject must equal the requesting user's key and DM chat.
  Use parameterized SQL with required chat filters. Preserve no-setting fallback as UTC.
  In-memory store lives with the service in Stores.extra; do not use TTL cache for persistent prefs.
- [x] Expose `display_service(stores)` and async resolve(chat,user,is_dm) returning zone and source.
  Keep the service interface small; setting/reset/purge methods take required chat scope.
- [x] Test independent service/store instances against the same fake durable DB, cross-chat and
  user isolation, group resolution ignoring private prefs, and aware conversion at both DST edges.
  Test SQL persistence with a narrow fake and add marked real-Postgres coverage if established
  fixtures make it practical; do not claim real Postgres was run when unavailable.

## Task 2: Explicit commands, read tools and deletion

Files: create `packages/core/src/chatcore/display_time_plugin.py`,
`packages/core/tests/test_display_time_plugin.py`; modify `packages/core/src/chatcore/privacy.py`
and `apps/sb_bot/src/sb_bot/app.py`; extend related app/privacy tests.

- [x] Write failing tests for `/timezone`, `/timezone <zone>`, `/timezone reset`, `/time [zone]`.
  Successful setting/reset acts immediately with no PendingAction and replies with actual scope.
- [x] Validate scope/platform identity and require a fresh native group-admin lookup for each group
  setting/reset; no owner/setup/global-admin bypass. Error/unsupported adapter fails closed.
  Personal writes only affect the caller's own DM. No target-user/group argument.
- [x] Register only read model tools (effective setting/current time). Explicit zone on current-time
  tool is a one-off; it never writes preferences. Invalid tool args return safe errors.
- [x] Implement plugin purge hooks; user deletion removes that user's DM setting in that scope,
  never a shared group setting. Chat deletion removes all settings in that chat.
- [x] Register plugin before migration_sources is applied and before PrivacyPlugin captures list.
  Update privacy disclosure/deletion confirmation to mention voluntarily saved preferences.
- [x] Green tests include admin revocation between commands, lookup errors, non-admin bot owner,
  non-self DM, group-addressed command routing and no accidental setting via model tools.

## Task 3: Authoritative model current-time context

Files: modify `packages/core/src/chatcore/agent.py`, `packages/core/src/chatcore/prompts.py`
or `apps/sb_bot/src/sb_bot/prompts/system.md` as appropriate; add
`packages/core/tests/test_display_time_agent.py` using existing fake-provider test helpers.

- [x] Write a failing fake-clock/provider regression proving runtime context has current UTC,
  effective zone/source and converted current local time. Then add it to `_context`.
- [x] Keep internal UTC history timestamps, ordering and stored turns unchanged. Add guidance to
  convert quoted instants for replies and respect explicitly requested zones. Do not infer zones
  from Telegram or group/member labels; do not include anyone's private preference in group context.
- [x] Tell model that saving requires the explicit `/timezone` command and that date-only ledger
  values and explicit reminder/poll scheduling zone arguments keep their existing semantics.
- [x] Test group zone vs a different saved DM preference, unset UTC, DM self preference and explicit
  zone current-time tool behavior. Use clock injection; don't test real wall-clock equality.

## Task 4: Convert direct displayed timestamps

Files: modify `packages/plugin-jobs/src/chatcore_jobs/service.py`,
`packages/plugin-polls/src/chatcore_polls/service.py`,
`packages/plugin-research/src/chatcore_research/plugin.py`,
`packages/core/src/chatcore/app.py`,
`packages/core/src/chatcore/administration/plugin.py`; extend their existing tests or add focused
`test_display_time_outputs.py` tests in the respective packages.

- [x] Add failing tests for job next-run, poll closing and research-created listings. Resolve using
  current reply chat/user/is_dm and format via the shared formatter. Research status already has
  stores in the caller; thread it through privately rather than relying on startup having run.
- [x] Add tests and convert access-pause notices and admin restriction output. Admin commands are
  in DM, so use that DM's preference for the reply even when listing restrictions for a group.
- [x] Audit detail is a structured stored `until=<UTC ISO>` string for pause records. Preserve it
  in storage; only render a validated known pause-until field as a display timestamp. Preserve
  unknown audit detail unchanged. Do not blindly substitute arbitrary ISO-looking text.
- [x] Leave reminder/poll explicit-zone confirmation labels and parsing, worker due instants,
  intervals, budget/retention clocks, expenses/export date policies and rental wall clocks alone.
- [x] Prove display setting changes leave saved reminder/poll due instants and watch intervals
  identical; use existing repositories/fakes and parser tests without redesigning scheduling.

## Task 5: Documentation, verification and independent review handoff

Files: create `docs/display-timezones.md`; update this checklist. Write an ignored report under
`reports/display-timezones-luna.md` and final handoff under `handovers/display-timezones.md`.

- [x] Document exact commands, group priority, labelled UTC fallback, personal DM-only scope,
  permission checks, DST labels, /forgetme behavior and untouched scheduling/report-date policies.
- [x] Run targeted tests, then full non-integration suite and Ruff. Run Ruff format on modified
  Python files only; do not reformat the repository. Use `git diff --check`.
- [x] Report baseline vs final counts, exact commands, dependency changes, owned files and limits.
  Record real database/integration checks and any unrun browser or sandbox tests explicitly.
- [x] Parent dispatches independent Sol review of full tracked/untracked diff against base and
  spec, sends confirmed findings to Luna, then requests Sol re-review. The user subsequently
  authorized parent to commit, push, create the PR, merge and deploy after review/checks.

## Completion evidence

Work is ready for handoff only when the intended preference behavior and scope/privacy rules are
covered by meaningful passing tests, targeted outputs use one formatter, known baseline failures
are distinguished, Sol has no unresolved material findings, and remaining verification limits
are recorded. The user already chose the in-session Luna/Sol workflow and authorized release;
no additional execution-choice prompt or historical Claude CLI review gate applies.
