# Full self-improvement delivery plan

This is the implementation breakdown of the approved Revision 3 design at
`docs/plans/2026-10-31-self-improvement-plan.md`.
The earlier `2026-10-31-self-improvement.md` is the record of a limited policy prototype,
not the full implementation plan. The design has four delivery stages and twelve
cross-cutting SI acceptance criteria. It is split here into ten implementation parts.

## Scope and authority

Work locally in the existing `codex/self-improvement` worktree based on `0568954`.
The user now authorizes parallel implementers. Preserve all unrelated work and earlier
prototype files. No commits, pushes, PRs, production installation, runner registration,
Telegram messages/ingress changes, token rotation, or production DB changes. Source
for those components may be implemented and tested with synthetic inputs locally.
Missing operational access blocks the associated proof, not all local implementation.

## Parts and dependencies

| Part | Deliverable | Dependencies | SI coverage |
| --- | --- | --- | --- |
| P01 | Installed policy, immutable Git evidence and full D/B/H review bundle | None | 02, 04, 08, 09, 12 |
| P02 | Isolated development worker, generation controller and bounded test reports | P01 | 01, 09, 12 |
| P03 | Immutable quarantine publication gateway and Publication App lifecycle | P01, P02 | 02, 09, 11 |
| P04 | Trusted hosted CI evidence collector and pinned merge control | P01, P03, P05, P07 | 04, 05, 06, 09 |
| P05 | Protected Postgres approval ledger, transactional outbox, target fencing and reconciliation | P01 | 03, 06, 07, 12 |
| P06 | Sole-token Telegram bridge, message ownership and token-free ordinary adapter | P05 for release callbacks; ordinary bridge independently testable | 03, 07, 12 |
| P07 | OIDC verification, bounded broker endpoints and operation receipts | P01, P05 | 02, 03, 06 |
| P08 | Protected host service: durable journal, admission, fixed build/deploy and truthful recovery | P01, P05, P07; journal independently testable | 06, 08, 10 |
| P09 | Disposable whole-runner supervisor, constrained relay and lifecycle cleanup | P07, P08 | 01, 06, 09, 11 |
| P10 | Integration, attack/crash rehearsals, resource evidence and owner bootstrap runbook | All preceding parts | 01–12 |

Parts are implementation boundaries, not separate owner approvals. A normal eligible
release retains the one-tap decision in the design.

## First parallel batch

Each writer owns separate files and writes a detailed part plan before implementation.
No writer may edit root workspace configuration, the existing `policy.py`, or another
part's files. The main chat owns integration and review.

- P01: `chatcore_release_control/git_evidence.py`, `tests/test_git_evidence.py`,
  `docs/plans/self-improvement-p01.md`. Read-only Git inspection must
  derive ancestry, tree identities and complete delta lists from actual local objects;
  inspect introduced history for protected/private paths including add/remove history.
  Sanitize Git environment/config, disable replacements/external helpers, bound output
  and runtime, never checkout/execute source, and fail closed on incomplete evidence.
- P06: `chatcore_release_control/telegram_bridge.py`, `tests/test_telegram_bridge.py`,
  `docs/plans/self-improvement-p06.md`. Implement an executable bridge
  service core using an injectable Bot API transport plus a token-free ordinary client
  contract. Enforce authenticated principal, allowed chats/methods, message ownership,
  protected release messages/callback namespace, and token-free file proxy results.
  Test actual calls with fake transport; never call Telegram. Integration/rotation proof
  remains pending.
- P08: `chatcore_release_control/host_journal.py`, `tests/test_host_journal.py`,
  `docs/plans/self-improvement-p08.md`. Implement a durable local host
  journal with atomic writes and OS locking, same-operation reconciliation, bounded
  start deadlines, observed-state recovery decisions, terminal receipts and config
  generation protection. No Docker operations or application wiring. Tests use temp
  directories and reopen records to verify durability and crash ambiguity handling.

## Review and continuation

For every part record implemented files, meaningful checks, explicit remaining work,
and SI evidence. Main chat reviews the actual code for plan compliance and regression
risk, repairs findings, and verifies integration. A part remains partial until its full
deliverable and dependencies work. Local unit evidence never substitutes for live App,
runner, Telegram owner, Postgres role, container or production proofs.

## Historical execution record (2026-10-31)

User authorized parallel implementation after the initial prototype. Three Luna High
implementers work only on assigned files; main retains integration/review and config.
All ten part plans now exist as `self-improvement-p01.md` through `-p10.md`.

First parallel increments: P01 actual Git evidence/closure checks, P06 bridge core,
P08 host journal. Main added P07 real OIDC signature/claim checks and fixed-origin
JWKS fetch. Integrated first-batch suite passed (86 tests before later increments).
Main review reproduced and repaired extraction incorrectly attesting human review,
intermediate nonallowlisted history paths, old symlink mode conversion, sequential
journal retention and archive operation-ID validation. Further bounded-recovery
journal repair is in progress.

Second parallel increments: P02 generation/container controller, P03 quarantine
publication/App lifecycle, P05 real Postgres decision/outbox ledger. P05 disposable
Unix-only PostgreSQL evidence initially passed 23 cases; main review requests canonical
DeploymentIdentity compatibility, persisted external-operation identities and safe
connection errors before acceptance. P02 review requests an executable protected
worker and verified image/base identity binding before accepting its testing claim.
These are implementation findings, not missing live proofs.

P04 hosted control, P09 whole-runner supervision and P10 integrated rehearsal remain
planned. P06 ingress/cards/adapter, P07 endpoints/receipts and P08 actual fixed host
build/deploy still require implementation as well as their separate live proofs.
No full part or end-to-end readiness is marked passed merely because an increment
has unit tests. Scope has not been reduced to this batch.

## Current part status

The table below supersedes the historical batch descriptions. All ten parts have
local source implementations. Final combined verification passed **1001 tests**, with
**1 skipped and 10 deselected**; no live acceptance result is inferred from these tests.
The numbered part files retain their incremental development records; historical
statements that a later-implemented component is missing are superseded by this table.

| Part | Local implementation state | Remaining operator provisioning and live acceptance |
| --- | --- | --- |
| P01 | Installed-policy types and actual Git full-delta/history/closure evidence implemented | Actual owner-installed closure inventory |
| P02 | Git baseline/archive, pinned offline image recipe, fixed harness and strict reports implemented and reviewed | Real Linux/runsc isolation and immutable image preparation |
| P03 | Generation CLI, initial-history audit, immutable publication, concrete transports, bounded durable submission and exact retry implemented | Real remote App/scanner/race proofs |
| P04 | Trusted CI bootstrap/artifact service, complete object fetch, coordinator and combined merge→relay workflow implemented and independently reviewed | Disposable hosted Actions proof |
| P05 | Durable decision/outbox/fences, restricted runtime roles/durability guards, correlated dispatch/requeue/report worker and increasing sequences implemented | Actual installed role/permissions and crash proofs |
| P06 | Sole-token bridge, protected cards/reports/revocation ingress, durable ordinary relay and app bridge factory implemented | Actual ingress, compatibility and token cutover/rotation |
| P07 | OIDC/JWKS/job closure, dispatch UUID/run correlation, ordinary/reconciliation eligibility, phase admissions, revocation and retained receipts implemented and independently reviewed | Real OIDC/network proof |
| P08 | Durable host journal/service, fixed bounded build/runtime, renewable source credentials and bridge-only ordinary config implemented and independently reviewed | Real Docker/recovery proof |
| P09 | Whole-runner lifecycle/registration, pinned standalone relay and capability enforcement implemented; workflow handoff independently reviewed | Disposable Linux/GitHub isolation and cleanup proof |
| P10 | Synthetic generation→publication→approval and broker↔host lost-reply/replay flows, watchdog, evidence index and maintenance runbook implemented | Failure-injected live rehearsals and owner production acceptance |

All live App/Telegram/runner/isolation/host resource/production acceptance proofs are
still pending. This table records incomplete source work explicitly; unavailable
production access is not used as a reason to mark that source work complete.

## Historical batch checkpoint evidence

- Parallel execution used three GPT-6 Luna agents at High reasoning; main owned
  integration, source review and common dependency/configuration edits.
- Checkpoint combined command: `uv run pytest packages/plugin-release-control/tests
  packages/plugin-extensions/tests -q -m 'not integration and not sandbox_integration'`
  — **320 passed, 10 deselected**. Ledger cases create/stop isolated Unix-only
  temporary Postgres instances; other external transports and process runners are
  synthetic. No actual Docker/runsc or Telegram/GitHub publication was exercised.
- Ruff, Ruff formatting, `uv lock --check`, `git diff --check` all pass. One final
  blank-line formatting correction in a publication test changed no test behavior.
- Separate read-only review found/repaired total JWKS deadline, installed journal
  start maximum, full prepared host identity matching, defensive binding snapshots,
  generated/fresh nonce contract and protected-card restart readiness. Targeted
  independent re-reviews: **PASS**. Main publication review also repaired introduced
  root iteration and exact scan-surface provenance binding; focused tests pass.
- At that checkpoint, local increments: **PASS within the exact documented scope**. Full design:
  **INCOMPLETE**, with remaining source work explicitly listed in the status table.
  No claim that tests establish sandbox safety or production readiness.
- All task edits remain uncommitted/unstaged in `codex/self-improvement` based on
  `0568954`. Primary checkout's untracked design/review/handovers remain preserved.
  No production installation, runner registration, ingress switch, real token
  rotation, production DB change, task commit/push/PR, merge or deploy occurred.

## Historical resumption after blocker clarification

The user asked whether work was blocked. There is no blocker to further authorized
local source implementation; the earlier batch checkpoint was not a blocking gate.
Main resumed P02 baseline/harness source (baseline.py, monorepo_harness.py, modern worker
and strict reports; 74 focused cases pass). Luna High agents resumed: git_evidence owns
P04 evidence/merge/coordinator concrete runtime; telegram_bridge owns P06 serving,
card/adapter plus durable queue and concrete protected transport/runtime; host_journal
owns P09 supervisor/bootstrap/capability/registration/relay concrete source.
No new production permission was inferred. Main owns dependency edits and remaining
P05/P07 broker admission/service integration; P08 actual host deploy/build source and
P10 full local pipeline will follow dependencies. Do not stop solely at another plan
or protocol stub when the remaining code is locally implementable.

### Earlier integration review

The main baseline/archive repair is verified by 87 focused cases and independent
review. Ordinary application bridge wiring passes 75 app/composition cases. The
concrete P03 gateway and P04 CI runtime are now source implementations, with their
live effects mocked in local tests. Root review reproduced and repaired publication
HTTPS transport being disabled by its general protocol policy and a missing module
CLI entrypoint. No network was needed to reproduce either issue.

At that interim checkpoint, parallel source work was: git_evidence owned hosted CI bootstrap; telegram_bridge
owns dispatch-ID OIDC correlation and broker review; host_journal owns P08 review
repairs before queued P09 relay/resource fixes. Main owns worker/report integration,
cross-service tests, evidence/runbook and overall review. An independent reviewer
identified an old run's attempt-2 reclaiming a new fence; each dispatch now receives
a persisted UUID, with mandatory GitHub recorded run-title correlation under repair.
Host review also identified a current-state callback/effect-lock cycle; that is a
source repair, not an access blocker. Those interim source findings have since been
resolved and verified as recorded below.

## Final local verification and handoff

All ten source parts are implemented and the final independent reviews found no
remaining concrete source blocker. The combined command was:

```sh
uv run pytest packages/plugin-release-control/tests packages/adapter-telegram/tests apps/sb_bot/tests packages/plugin-extensions/tests -q -m 'not integration and not sandbox_integration and not browser'
```

Result: **1001 passed, 1 skipped, 10 deselected**, with eight existing PTB deprecation
warnings, in 284.35 seconds. Ruff and formatting passed (94 files), `uv lock --check`
and `git diff --check` passed, 122 new package/template files passed trailing-whitespace
inspection, and template JSON/workflow structure checks passed. The first combined
run exposed two test-fixture defects: missing newly required ordinary-card review
metadata and cluster-wide role recreation. Both were repaired, verified together,
then the complete combined command above passed afresh.

The actual local integration scope and all twelve pending live criteria are recorded
in `apps/sb_bot/deploy/release-control/evidence-index.json`; its production flag is
false. The adjacent maintenance runbook and inactive templates are the operator
handoff. This completes local implementation, not the four-stage design's disposable
live rehearsals or owner-approved production bootstrap. No production installation,
runner registration, external Telegram/App effect, ingress switch, real credential
rotation, production database change, commit, staging, push, PR, merge or deployment
was performed. HEAD remains `0568954a14575625c42375e581343c22169a9e63`.
