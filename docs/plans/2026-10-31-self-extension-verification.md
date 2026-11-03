# Self-extension verification and review record

Date: 2026-10-31 (Asia/Singapore).

## Checkout and scope

- Branch: `codex/self-extension`.
- Planning base: `afa88c4ed3ee44be7c47dfa4b995db2d63601de3`.
- First reconciled base: `995eed98b5be4f74a4b3267895c7d4a29774134f`.
- Final compatibility base: `189b1b14a9caf394971a101b4c0ecaa34c1a6eba`, including the separate reminders/website-watches work that landed during repairs.
- Planner: `gpt-6-astra`, medium. Implementer: `gpt-6-luna`, high, explicitly approved by the user. Independent reviewer: `gpt-6-astra`, low.
- Local/test pure JSON self-extension only. No production deployment, merge into main, remote publication, or generated-artifact Git export.
- The separate user-owned reminders/website-watch chat is `01a18de2-021a-7d71-be46-3b55e0116f5d`; its implementation is not part of this branch.

## Initial verification after main reconciliation

- `uv run pytest -q -m 'not integration and not sandbox_integration'`: **636 passed, 1 skipped, 29 deselected**. The skip is optional ShellCheck; four existing Telegram deprecation warnings.
- `SB_EXTENSION_SANDBOX_TESTS=1 uv run pytest packages/plugin-extensions/tests -q -m sandbox_integration`: **3 passed, 42 deselected**.
- `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test uv run pytest packages/plugin-extensions/tests/test_pg_repository.py -q -m integration`: **3 passed** in isolated schemas.
- Ruff found one overlong/unformatted line in `tests/test_service.py`; whitespace diff checks passed.

These results predate the review repairs below and do not establish final completion.

## Independent review, round 1

Outcome: **CHANGES_REQUIRED**. Reviewer independently ran extension unit tests (39 passed, 6 deselected) and reproduced the actual worker CLI failure and memory requester-purge defect. Root checked the affected code and accepted the findings.

| ID | Required repair | Status |
|---|---|---|
| R1 | Correct image resolver/CLI contract; verify actual worker subprocess startup, authenticated health/execution, shutdown. | Closed in independent re-review 1 |
| R2 | Preserve active artifacts and supporting requests during retention; memory/Postgres parity. | Closed in independent re-review 1 |
| R3 | Purge requester-linked private artifacts when requester, builder and reviewer differ. | Closed in independent re-review 1 |
| R4 | Account returned provider usage exactly once on rejected completions. | Closed in independent re-review 1 |
| R5 | Validate all malformed tool argument types, depth and size before reserving quota. | Closed in independent re-review 1 |
| R6 | Review complete digest-bound manifest and case provenance; show exact passing evidence in confirmation. | Closed in independent re-review 1 |
| R7 | Finalize reserved-run metadata on cancellation/stale epoch without exposing output or corrupting current activation. | Closed in independent re-review 1 |
| R8 | Provide scoped build/test/run/quota monitoring, including restricted drafts and failed builds. | Closed in independent re-review 1 |
| R9 | Close worker client when startup health fails. | Closed in independent re-review 1 |
| R10 | Resolve lint/format failure. | Closed in independent re-review 1 |
| R11 | Prevent late container creation and guarantee bounded CLI shutdown, including incomplete HTTP clients. | Closed in independent re-review 3 |

Root additionally clarified that request examples may originate from the conversational model. They must not be described as human-authored merely because they precede code generation; visible requester confirmation and complete admin review establish what was approved.

Final repaired verification, independent re-review, source commit and runtime identities must be recorded before a completion claim.

## Repair round 1 and concurrent integration

Luna reported targeted repairs for R1–R10 complete, with 207 focused tests passing, 6 PostgreSQL tests passing, 3 live sandbox/end-to-end tests passing, and lint/format checks clean. These are implementer results awaiting independent re-review.

Before re-review, main advanced with the completed jobs plugin. Root preserved repaired task changes in stash `ba30abe454320109170e31b09d78c070b5097a22`, fast-forwarded the isolated feature branch to `189b1b1`, and reapplied the task changes. Luna owns resolution of the overlapping app/dependency/test edits and reuse of upstream's optional `is_group_admin` capability. No changes were applied to main or other worktrees. This compatibility step does not reset the repair-round count.

After compatibility resolution, root verified 736 unit tests passed (1 optional ShellCheck skip), 55 integration tests passed (3 Elasticsearch skips), and 3 live sandbox/end-to-end tests passed. Ruff, formatting and whitespace checks passed. An additional actual CLI subprocess using real Docker passed authenticated health, transformation and idle SIGTERM/socket cleanup.

Independent re-review 1 closed R1–R10, running 113 targeted tests and confirming lint/format/whitespace checks. It reproduced R11 with a delayed-create fake Docker subprocess: the daemon handler could finish creating an owned container after CLI shutdown cleanup. Round 2 is limited to draining/cancelling active requests and Docker child operations before final cleanup, with active-create and active-run shutdown regressions. The preceding passing checks do not close R11.

Round 2 changed handlers to non-daemon threads and joined them before final cleanup. Delayed-create/run/timeout regressions passed. Independent re-review and root's actual CLI reproduction found the remaining R11 bounded-shutdown gap: an incomplete HTTP request trickling bytes resets socket read timeouts and can keep the join alive indefinitely. Root observed the worker still running 16 seconds after SIGTERM. Round 3 closes accepted client transports before draining active sandbox operations, with partial-line/header/body shutdown regressions. No production action occurred.

## Final verification and independent approval

Independent re-review 3: **PASS**, R1–R11 closed. Astra inspected connection registration and shutdown ordering, ran all 14 worker regressions, and repeated the trickling-peer reproduction (exit in 0.11 seconds, socket removed). No actionable finding remained. SE-01–08 passed review; the immutable source handoff completes SE-09 after the final local commit.

Root reran checks against the unchanged reviewed implementation on base `189b1b1`:

| Check | Result |
|---|---|
| Full unit suite, excluding integration/sandbox markers | **742 passed, 1 skipped, 61 deselected**, 63.82 seconds |
| Full integration suite with local development Postgres/Valkey | **55 passed, 3 skipped, 746 deselected**, 44.17 seconds |
| Opt-in live Docker sandbox and real BotApp/worker end-to-end suite | **3 passed, 76 deselected**, 61.71 seconds |
| Actual installed worker CLI, real Docker, authenticated health/transformation/idle SIGTERM | Passed |
| Actual CLI SIGTERM during delayed real Docker creation | Passed; exact owned container absent afterward |
| Actual CLI SIGTERM during real container execution | Passed; exact owned container absent afterward |
| Actual CLI incomplete request trickling during SIGTERM | Passed; exit in **0.299 seconds**, socket removed |
| Ruff check, formatting and whitespace checks | Passed |

The unit skip is unavailable optional ShellCheck. Three integration skips require unavailable local Elasticsearch. Four Telegram-library deprecation warnings are pre-existing. No live provider request, Telegram group operation, production deployment or production sandbox suitability is claimed.

Runtime identities from actual CLI health:

- Image ID: `sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`.
- RepoDigest: `python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`.
- Policy SHA-256: `745d7f62a2f34e91d1e75d2c9947858231c3073290e82503e3ad94f82a9fe06f`.
- Harness SHA-256: `a344ed3e72076d65991d419666453111af05e77c67a6793af63f11e99ce5062c`.

## Acceptance coverage and remaining boundaries

| Criterion | Evidence |
|---|---|
| SE-01 | Real BotApp/provider-fixture/Unix-worker/Docker lifecycle, group member/admin and private DM flows. |
| SE-02 | Scoped repositories, known foreign IDs, distinct-actor privacy purge, private DM separation. |
| SE-03 | Fresh optional platform-role checks; demotion, lookup failure and global-admin bypass rejection. |
| SE-04 | Immutable canonical artifacts, request-confirmed examples, complete manifest pages, exact evidence-bound activation. |
| SE-05 | Real Docker host sentinel/path/symlink, network, environment, resource, isolation and recovery probes. |
| SE-06 | Authentication and size/concurrency tests; actual CLI lifecycle; delayed-create/run/timeout/trickle shutdown regressions. |
| SE-07 | PostgreSQL quota races, retained active/suspended tools, purge/revoke/cancellation epochs and scoped monitoring. |
| SE-08 | Default disabled; full combined jobs/administration/extension regression suite; production deployment files unchanged by this task. |
| SE-09 | Local reviewed commit and exact SHA/runtime/test handoff; no remote configured or publication attempted. |

Only pure JSON transformations are supported. Request-confirmed cases are visible examples, not independent proof of semantic quality. Generated artifacts are database revisions identified by content digest; their Git export is a separate follow-up. Production worker hosting/transport and stronger isolation require a separate release decision. Self-improvement remains the documented roadmap. The feature branch/worktree is retained for review; no merge into main or production activation is part of this handoff.
