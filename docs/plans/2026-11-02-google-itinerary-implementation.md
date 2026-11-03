# Google Workspace and itinerary implementation

Approved for implementation in this chat on 2026-11-02. Executed and self-reviewed inline by
Codex; no delegation. Initial base: `d01d7f9`; fast-forwarded to `5bb7c41` (`origin/main`)
before final verification. Branch: `codex/google-itinerary`.

## Brief and decisions

Implement plans #39 and #40 using the current repository layout and plugin-owned skill
instructions. Google tools use one owner OAuth profile, never chat-selected identities.
Configured tools default on in allowed chats, with confirmed per-chat opt-out. CSV uploads
remain independent of Google credentials. One spreadsheet represents a trip; user prompts
choose day/city tabs. All Google writes require confirmation, and existing itinerary cells
are never overwritten.

Remaining routine choices: admin-only writes by default (optional explicit namespaced user
allowlist); keep the proposed eight itinerary columns; helpers/handlers in the Google plugin;
expenses comparisons follow tracker/import/suggest and use the existing ledger scope/FX rules.
User approval covers implementation, not merging, deploying or authorizing a Google account.

## Acceptance criteria

- A1: Only current-message/reply-chain human URLs authorize authenticated Google reads/targets.
  Search, ambient history, bot text and file contents cannot grant authority.
- A2: Bounded native Doc/Sheet and uploaded CSV reads return untrusted data. Word/Excel and
  Drive CSV downloads provide clear alternatives without additional extraction dependencies.
- A3: Local PKCE/state/loopback OAuth setup verifies the owner and saves a mode-600 profile;
  runtime refresh/cache verifies identity and fails closed on revocation/account changes.
- A4: Every write returns a frozen pending payload. Commit rechecks requester/chat permissions,
  current opt-out, credential identity and target schema. No automatic replay after uncertain
  remote write results; no generic arbitrary batch-update tool.
- A5: One trip spreadsheet, user-selected day/city tabs, fixed version/header per tracker tab,
  stable tab IDs, server-side append or atomic initialization of fresh tabs only.
- A6: Summary aggregates recognized tabs; reports coverage, malformed rows, missing values,
  midnight-spanning clashes/gaps and Decimal currency totals without silent conversion.
- A7: Import/suggest tools accept normalized rows prepared from untrusted reads/research by the
  main model, validate them deterministically, show destination tabs/rows and require a click.
  Suggestions force Proposed status, cite sources, mark unverified facts and respect daily caps.
- A8: Expense export consumes the real ledger rows through an app-injected provider; explicit
  confirmed trip links are scoped to chat and spreadsheet, with same-currency comparisons.
- A9: Disabled credentials expose no Google tools; per-chat opt-out blocks reads and pending
  commits, but authorized managers can turn it back on. Other chats remain independent.
- A10: App wiring, routing, migration/privacy behavior and operator docs are tested. No live
  Google setup/API claim without owner credentials. Existing unrelated changes stay untouched.

## Ordered implementation and verification

1. [x] Read current callers/contracts, run baseline boundary/provenance/catalog checks.
2. [x] Red/green CSV parser and human-only provenance tests; retain legacy `seen_urls` behavior.
   Files: search `csv_input.py`, `settings.py`, `plugin.py`; core `agent.py`, tool registry.
3. [x] Red/green Google reference, literal-value, Doc-tab rendering and OAuth/REST client tests.
   New package `packages/plugins/google`: `refs.py`, `render.py`, `settings.py`, `client.py`,
   `oauth.py`; use `httpx.MockTransport` and pinned public destinations.
4. [x] Red/green default-on settings, confirmed mutation, permission-revocation and unknown-write
   reservation tests. Add `repository.py`, additive migration, `permissions.py`, `gates.py`,
   `service.py`, `plugin.py`, public exports and workspace wiring. Reuse existing confirmation.
5. [x] Red/green itinerary header/grouping/time/cost tests; implement `itinerary.py` and
   `itinerary_service.py` with append-only commits, import/suggest previews and bounded summaries.
6. [x] Red/green expense-provider/link and composite tests. Wire app, add bilingual routing
   examples, document Google configuration/privacy and run console end-to-end confirmations.
7. [x] Stop edits and self-review full diff against this brief, all acceptance criteria and
   existing callers. Record findings and repair/review rounds below.
8. [x] Final checks: `uv sync --locked --all-packages`; Ruff check; changed-file format check
   plus repository format check; `uv run pytest -q -m 'not browser and not sandbox_integration'`.
   Use a fresh disposable test database if the shared dev DB has the known migration mismatch.
9. [x] Commit/push and open implementation PR; verify head/base/CI, update plan index/records.

## Evidence and review record

Initial environment: Codex CLI available; this chat performs all stages itself. Current main
has plugin-owned skill instructions and `packages/plugins/` paths, so older plan references
are adapted. Live Google project/credentials have not been supplied.

Implementation evidence: baseline 33 targeted checks passed. New Google/CSV/app tests currently
pass (65 targeted, including 3 live local Postgres schema tests); a console CSV import creates
one workbook with two city tabs only after the requester's click. The first broad unit run
had 3,270 passes and one missing `.env.example` coverage failure, now fixed. Router fixture
coverage, index/boundary checks and legacy request snapshots pass. Current full verification
will be rerun against final reviewed code.

Adaptations: owner OAuth only (service-account impersonation deferred); row mapping/research
stays in the main composite agent, while import/suggest handlers accept and validate typed
`groups`. Large write previews are attached in full before confirmation. Container setup adds
an optional readonly Google credential directory; no live account/configuration was changed.

Pre-review fixes: unknown remote outcomes now survive the real ConfirmService path with
inspection advice; incomplete plan costs/single-tab selections suppress whole-trip expense
differences; stays may overlap activities; positional TurnState arguments remain compatible.

Self-review round 1: CHANGES_REQUIRED. Re-read the production diff and existing agent,
confirmation, group-access, attachment and expenses callers against A1–A10. Findings:

- R1 (P2): default ranges could exceed the actual Sheet grid. Bound reads by the grid, reject
  overly wide append rows, and size new tabs explicitly. Regression tests pass.
- R2 (P2): Doc appends silently ignored spreadsheet-tab arguments. Reject those arguments and
  require the Doc tab's URL instead. Regression test passes.
- R3 (P2): limits lowered after a preview could allow an empty file creation before failure.
  Revalidate all rows/text and total destination tabs before reservation/remote writes, at
  both proposal and commit where applicable. New regressions failed first, then passed.
- R4 (P2): skipping blank rows renumbered later entries in summaries. Preserve grid positions,
  ignore blank rows during analysis, and allow trailing empty cells. Regression failed first,
  then passed along with the 20 itinerary tests.

Repair round 1 complete. Google/plugin/console tests: 65 passed before R4; itinerary tests pass
after R4. Dependency sync, lint,
format checks for all 45 changed Python files, wheel/source build and `git diff --check` pass.
Repository format check reports 17 unchanged documentation files; no changed file is listed.
The first full suite passed: 3,422 passed, 9 skipped, 22 deselected. Final verification after
R4 and the main fast-forward passed: **3,424 passed, 9 skipped, 22 deselected**, in 122.78s.
The 14 warnings are existing Telegram dependency deprecations. The full suite ran against a
fresh disposable Postgres database; the Google migration/quota/privacy tests ran, not skipped.

Self-review round 2: **PASS**, no unresolved findings. A1–A4 checked against core provenance,
mocked REST/OAuth and the real confirmation dispatcher; A5–A7 against itinerary normalization,
multi-tab import/suggest, human-edit race and summary tests; A8 against real expense-ledger
scope/export tests; A9–A10 against app wiring, routing, Postgres/privacy and full regression
checks. This is a Codex self-review, not an independent or Claude review. Live Google consent,
permissions and API behavior still require owner credentials and remain rollout work.

Packaging verification: wheel and source archive build successfully, including the SQL
migration and `chatcore-google-auth` entry point. No additional third-party dependency was
introduced. Pre-commit secret scanning, lint and formatting pass.

Handoff: implementation PR #47 (history PR #47), branch
`codex/google-itinerary` against `main`. Implementation commit: `4967e9e`. GitHub confirms the
PR is open and mergeable against `5bb7c41`; remote CI is inspected at handoff (its current result
is shown in the PR). The plan index links the implementation and both design PRs. No merge,
deployment or owner authorization has been performed. The task-owned disposable database was
removed after the successful test run; the shared development database was not changed.

Merge authorization (2026-11-02): the owner asked to merge before completing Google setup.
CI for `3d72d23` passed; GitHub reported the PR mergeable with no review submissions or open
review threads. The status-only follow-up records the approved integration and inactive Google
state. The exact final head is checked before merging; credentials and live Google tests remain
the owner's next setup step. Normal main auto-deployment follows the merge; its host outcome
is separate from the confirmed GitHub merge result.

Integration refresh before merge: `main` advanced to `6aedf2d` with tutor PRs #48–#50. Merged
those changes into this branch, retaining both workspace dependencies, app/core wiring and
skill catalogs. Preserved main's routing fixture IDs and appended the Google cases as
`rs-243`–`rs-254`; the combined fixture contains 254 unique cases. No Google runtime behavior
changed during conflict resolution. Dependency sync, lint, changed-file formatting and diff
checks passed.

The refreshed local full run had 3,402 passes, 9 skips and 93 setup errors, all from the same
isolated release-control Postgres fixture: `initdb` could not create a shared-memory segment.
Read-only host diagnostics confirmed all 32 macOS shared-memory IDs were occupied
(`kern.sysv.shmmni=32`). No processes, kernel limits or unrelated IPC resources were changed.
The refreshed head must pass the complete suite in Linux CI before merge.

API contracts checked against Google's current [Sheets requests reference](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request),
[Docs requests reference](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request)
and [desktop OAuth guide](https://developers.google.com/identity/protocols/oauth2/native-app).
