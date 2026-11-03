# Reusable jobs follow-up review

Review scope: main merge `009e505` plus the uncommitted repair changes on `codex/basic-tools`. No commits, pushes, live messages, remote services, or deployments were made for this repair pass.

## Findings F1–F7

### F1 — CONFIRMED, fixed

Before the repair, settings and group-wide operations accepted only the Telegram role check. `is_group_manager` now refreshes the administration snapshot and accepts `AccessPolicy.can_setup(user, group)` or a successful current Telegram group-admin lookup; a failed refresh cannot authorize from stale assignments, and a failed/unsupported live lookup denies that branch. See `packages/plugin-jobs/src/chatcore_jobs/permissions.py:39` and `packages/plugin-jobs/src/chatcore_jobs/service.py:211`, `:267`, `:352`, and `:369`. Global admin status alone is not consulted.

Coverage: `test_owner_and_assigned_delegate_can_manage_only_assigned_group` proves owner access, group-scoped delegation, and denial on another group; `test_delegated_setup_revocation_before_confirmation_denies` proves store revocation is rechecked at commit. The pre-existing `test_group_configuration_confirmation_rechecks_live_admin_role` proves live Telegram-role revocation denies at commit. `test_all_members_list_requires_live_role_and_keeps_current_topic` retains the global-admin-only denial.

### F2 — CONFIRMED, fixed

The DM branch previously allowed global admins and live allowlisted-group members only. It now also accepts users with a current setup assignment; the same-DM-identity and global DM-enabled checks still happen first. See `packages/plugin-jobs/src/chatcore_jobs/permissions.py:73`–`:85`.

Coverage: `test_dm_job_access_includes_current_setup_delegate_assignments` verifies an assigned delegate is allowed without a membership call.

### F3 — CONFIRMED, fixed

Worker authorization previously relied on the in-process allowlist snapshot. `JobsWorker.run_once` now performs one bounded refresh before claiming due jobs; refresh failure logs only the exception class and returns before claiming, leaving work for the next poll. The app-wired worker uses `AccessPolicy.refresh`. See `packages/plugin-jobs/src/chatcore_jobs/worker.py:52`–`:66` and `packages/plugin-jobs/src/chatcore_jobs/plugin.py:345`–`:350`.

Coverage: `test_worker_refreshes_dynamic_allowlist_before_authorizing_batch` disables a group in the store between snapshot refreshes and proves no fetch or send occurs. `test_access_refresh_failure_retries_next_poll_without_claiming` proves a transient refresh failure does not consume or cancel work and does not log the exception message.

### F4 — CONFIRMED as a coverage gap; fixed and covered

The repository and service already carried explicit scopes, but the requested broad-manager isolation matrix was not covered. List authorization now uses the shared setup-manager rule, and cancel confirmation rechecks that rule while passing the original group and topic scope. See `packages/plugin-jobs/src/chatcore_jobs/service.py:258`–`:270`, `:345`–`:362`, and repository scope predicates at `packages/plugin-jobs/src/chatcore_jobs/repository.py:352` and `:390`.

Coverage: `test_assigned_manager_all_members_list_and_cancel_stay_in_current_topic` verifies group/topic isolation for list and cancel; `test_group_manager_cannot_list_or_cancel_other_users_dm_jobs` denies DM-wide access and proves no `group_for_dm` or group-role lookup occurs in the DM path. `test_all_members_list_requires_live_role_and_keeps_current_topic` verifies ordinary-member denial.

### F5 — CONFIRMED as a coverage gap; fixed and covered

The previous restart test reused a repository or worker path and did not prove recovery through a newly constructed worker and separate database pool. The durable lease and reservation recovery behavior remains in `claim_due`; stale reservations become unknown and are not replayed. See `packages/plugin-jobs/src/chatcore_jobs/repository.py:416`–`:481`.

Coverage: `test_fresh_worker_and_database_pool_recover_pre_reservation_but_never_replay_unknown` has worker A claim an unreserved reminder, then uses a new pool, repository, and worker to recover and deliver it once after lease expiry. It then simulates a crash after reservation and proves a new worker marks the outcome failed/unknown without sending again.

### F6 — CONFIRMED, fixed

`send_job` previously allowed a history-log exception to escape after the platform accepted the message. It now logs only the exception class and returns the platform message reference, allowing the worker to persist the delivery as sent. See `apps/sb_bot/src/sb_bot/app.py:287`–`:303`.

Coverage: `test_scheduled_send_survives_history_logging_failure` makes history logging fail and verifies the successful platform send still returns its message reference.

### F7 — CONFIRMED, fixed

Disable confirmation previously lacked both the permanence statement and the active-job count. The request summary now includes the number currently active across all topics and explains that re-enabling does not restore cancelled jobs. Confirmation returns the exact number cancelled; the repository locks those active rows inside the settings transaction before counting and disabling. See `packages/plugin-jobs/src/chatcore_jobs/service.py:236`–`:256`, `:368`–`:389`, and `packages/plugin-jobs/src/chatcore_jobs/repository.py:156`–`:210`.

Coverage: `test_disabling_confirmation_names_permanent_cancellation_and_count` checks the user-facing summary; `test_disable_reports_exact_permanent_cancellation_count_across_topics` verifies the transactional count and permanent cancellation across group topics.

## Prior repair round R1–R6

All six accepted independent-review findings remain fixed and covered:

- R1 lease recovery: the `reserved -> unknown` CTE updates jobs only for reservations transitioned in that poll (`repository.py:423`–`:436`); `test_unknown_old_occurrence_does_not_clear_new_watch_lease` proves an old unknown occurrence cannot clear a fresh watch lease.
- R2 interval floors: confirmation and repository creation revalidate current floors; group floor changes cancel noncompliant active watches and dispatch authorization rejects jobs below changed global floors (`service.py:312`–`:322`, `repository.py:183`–`:198`, `plugin.py:353`–`:377`). `test_watch_confirmation_rechecks_interval_after_floor_changes`, `test_interval_floor_change_cancels_old_jobs_and_rejects_new_fast_jobs`, and `test_global_floor_change_suppresses_existing_watch_dispatch` cover the paths.
- R3 retention: terminal completion time and configured retention are used, and old non-reserved occurrences are pruned from active watches while the latest no-replay record is retained (`repository.py:724`). `test_job_retention_uses_terminal_time_and_prunes_old_active_watch_deliveries` exercises the plugin lifecycle hook.
- R4 DM cancellation: commit allows cancellation after DM tools are disabled while still rechecking identity/membership (`service.py:295`); `test_pending_dm_cancel_commits_after_global_tool_disable` covers it.
- R5 cancellation fencing: job state updates after delivery are restricted to the still-active job with the matching lease token; suppressed outcomes cannot terminalize (`repository.py:609`). `test_delivery_finish_never_undoes_cancellation` covers suppressed, sent, and unknown late outcomes.
- R6 blocked watch restart: blocked-watch reservation persists terminal `failed` intent; recovery applies it before any retry (`worker.py:161`, `repository.py:423`–`:436`). `test_blocked_watch_reservation_recovers_as_terminal_after_restart` proves a fresh worker does not fetch or send again.

### R7 — CONFIRMED, fixed

The second review found two lease-boundary gaps. Reservation recovery previously used a strict `<` timeout while job claims became eligible at `lease_until <= now`; claim selection also did not exclude a still-reserved delivery, and the global lease count ignored reservations whose original claim lease had expired. Recovery now uses an inclusive deadline, an unresolved reservation holds its global worker slot, and jobs with any reserved delivery are excluded from claims. `reserve_delivery` and `delivery_allowed` also require the current token and an unexpired lease. See `packages/plugin-jobs/src/chatcore_jobs/repository.py:423`–`:456`, `:543`–`:551`, and `:589`–`:606`.

Cancellation or unexpected failure after reservation now resolves all reserved rows for that claim, including the narrow cancellation window before `reserve_delivery` returns. Blocked-watch terminal intent is persisted in the reservation and applied to active jobs on suppression/unknown outcomes without overwriting a concurrently cancelled job. Pre-send authorization and `delivery_allowed` are inside cancellation/error handling. See `packages/plugin-jobs/src/chatcore_jobs/repository.py:639`–`:700` and `packages/plugin-jobs/src/chatcore_jobs/worker.py:92`–`:136`, `:155`–`:163`, and `:243`–`:312`.

Coverage: the three PostgreSQL reproductions initially failed, then passed after the fix: `test_reserved_reminder_recovers_at_exact_lease_expiry_without_new_occurrence`, `test_unresolved_reservation_holds_global_slot_past_claim_expiry`, and `test_cancel_during_blocked_watch_pre_send_finalizes_reservation`. The latter blocks the third authorization call with an `asyncio.Event`, cancels the worker, starts a fresh worker, and proves there is no second fetch/send. `test_delivery_preflight_error_finalizes_reservation_without_sending` also proves a repository preflight exception cannot strand a reserved notification.

## Additional review checklist

No new findings were identified in the requested repository and worker review. User-facing repository methods retain chat/DM/topic/owner predicates; quota creation uses global-then-chat advisory locks; claims use the scheduler lock and `FOR UPDATE SKIP LOCKED`; worker row writes require the claim token. Fetch and send occur outside database transactions. Job logs include identifiers, kinds, and exception classes rather than raw URLs, query strings, page content, or exception messages.

## Verification

Post-merge clean baseline (recorded by the parent before these repairs):

- `uv run pytest -m 'not integration' -q` — 644 passed, 1 skipped, 46 deselected; four existing PTB deprecation warnings.
- First integration attempt against the disposable shared test database failed one migration test because an earlier version of `jobs/0001` had been applied in the public test schema. The parent reset only `sbot-basic-tools-pg`'s `chatcore_test` database, then reran the exact command below successfully. No other service or database was touched.
- `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test uv run pytest -m integration -q -rs` — 43 passed, 3 skipped, 645 deselected. All skips were Elasticsearch-dependent.

Final repair verification:

- `uv run pytest -m 'not integration' -q` — 652 passed, 1 skipped, 49 deselected, four existing PTB deprecation warnings (12.27 seconds).
- `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test uv run pytest -m integration -q -rs` — 46 passed, 3 skipped, 653 deselected (7.34 seconds). Skips: `packages/plugin-memory/tests/test_integration.py:70` (Elasticsearch unreachable), `packages/plugin-search/tests/test_es_integration.py:19` (Elasticsearch not running), and `tests/test_app_integration.py:120` (dev Elasticsearch unavailable). The disposable Postgres cases all ran.
- `uv run ruff check .` — passed.
- `uv run ruff format --check packages/core/src/chatcore/group_access.py packages/adapter-telegram/src/chatcore_telegram/__init__.py packages/adapter-telegram/tests/test_telegram.py apps/sb_bot/src/sb_bot/app.py apps/sb_bot/tests/test_app.py packages/plugin-jobs/src packages/plugin-jobs/tests` — 23 files already formatted.

Repairs are uncommitted on top of merge commit `009e505`. The worktree is ready for independent review. No live services, live bot messages, deploy, push, pull request, or merge was performed by this repair pass.

Final R7 repair verification:

- Red/green Postgres reproductions: `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test uv run pytest packages/plugin-jobs/tests/test_pg_jobs.py -q -k 'exact_lease_expiry or unresolved_reservation or cancel_during_blocked' -rs` first failed all three cases against the pre-fix code, then passed 3 tests after the repair.
- Focused job package checks: `uv run pytest packages/plugin-jobs/tests -m 'not integration' -q` — 68 passed, 29 deselected; `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test uv run pytest packages/plugin-jobs/tests/test_pg_jobs.py -q -rs` — 29 passed.
- Final full nonintegration command: `uv run pytest -m 'not integration' -q` — 653 passed, 1 skipped, 52 deselected, four existing PTB deprecation warnings (9.59 seconds).
- Final full integration command: `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test uv run pytest -m integration -q -rs` — 49 passed, 3 skipped, 654 deselected (4.53 seconds). The same three Elasticsearch-dependent tests listed above skipped; local Postgres integration cases all ran.
- Final lint and format: `uv run ruff check .` passed; the changed-file format command listed above passed with 23 files already formatted.


## Final verdict

**Ready for your review.** Independent Astra review passed after R7: 192 focused tests passed,
with no remaining substantive findings. All F1–F7 are confirmed/addressed; prior R1–R7 are fixed.
The additional findings were R1 and R7 (blockers: reservation/lease concurrency and replay),
and R2–R6 (should-fix: interval revalidation, retention, disabled-DM cancellation, cancellation
state preservation and blocked-watch recovery). None remain open.

Parent verification: 653 nonintegration tests passed (1 skipped); 49 integration tests passed
(3 Elasticsearch skips, explicitly listed above); Ruff, changed-file formatting and diff checks
passed. Exact commands and final timing are in the plan's final execution-evidence section.
The WIP checkpoint is `5e26499`, the main merge is `009e505`, and all subsequent fixes remain
uncommitted. The local source archive and hashes are in
`/path/to/local-artifacts/basic-tools/2026-10-31-final/HANDOFF.md`.
No production or live-platform verification was performed; delivery uncertainty remains documented.
