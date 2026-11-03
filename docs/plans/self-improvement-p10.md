# P10: Integration, rehearsal and owner bootstrap

Dependencies: P01–P09. Own `tests/test_release_flow.py`, `tests/rehearsal/`, deployment runbook and evidence-index source.

1. Integrate a synthetic local request through trusted Git evidence, scanning, publication records, owner-card issuance, decision/outbox, OIDC claim, merge reconciliation, switch admission, durable host journal and terminal reporting. Require one affirmative decision for the expected B/H→M and D→M transitions.
2. Add failure injection at decision/outbox/ack, dispatch/run binding, merge response, host admission/journal/switch/health/restoration and report boundaries. Reopen real stores; do not replace durability evidence with boolean fixtures.
3. Exercise approval reject/expiry/revoke/owner removal before and after atomic admission, queued A/B/C work, stale attempts/fences/sequences, main movement, maintenance config changes, lost replies and expired start windows. Completed receipts survive later maintenance without repeating effects.
4. Cover all SI-01–SI-12 with an evidence index marking local tests, disposable proofs, pending/blocked and owner acceptance separately. Evidence records include exact policy/source/runtime/App/workflow identities and command results; never store credentials or raw production logs.
5. Write maintenance bootstrap/rollback/watchdog/break-glass instructions including sole-token ingress cutover, old-token invalidation, dedicated Postgres roles, fixed installed closure, prior healthy image/config retention and host resource reservation. Generated releases still refuse migrations.
6. Run the relevant local suite once writers stop, inspect all code independently, repair material findings and rerun affected checks. Preserve the known baseline polls test failure as pre-existing.

Do not execute production installation, registration, ingress switch, rotation, schema changes, deployment or publishing. Those remain separate owner-authorized operational steps after actual disposable rehearsals. Missing proof keeps production enablement blocked; it does not mark implementation parts passed.

First actual local integration evidence: `test_ledger.py` now runs the authenticated
TelegramIngress through a real disposable PostgreSQL ReleaseLedger. The ack callback
observes committed decision/nonce/outbox before acknowledgement; replay has one
update receipt and outbox, and ordinary relay does not alter the approval. All 28
ledger/integration cases pass. This is local durable decision-path evidence only;
external Telegram, Actions, switch admission and deployment remain unproved.

## Local integration increment

`tests/test_release_flow.py` now covers two additional local paths with real temporary
Git repositories, disposable PostgreSQL, the SQLite publication record store, and a
real fsync-backed HostJournal:

- `test_generated_candidate_becomes_immutable_card_and_atomic_approval` drives the
  actual `execute_generation` and `DevelopmentController` through a synthetic provider
  and report runner, scanner/provenance gate, local publication store/GitHub fake,
  canonical trusted Git bundle extraction, immutable card creation, and atomic
  approval/outbox persistence.
- `test_real_git_merge_runs_through_postgres_and_host_journal_to_healthy_receipt`
  verifies actual D/B/H/M Git objects and the two-parent merge with P01 evidence, then
  exercises the real ledger, broker store/application and HostService ASGI routes with
  mock GitHub HTTP, a synthetic builder/runtime, and lost-response retry. One switch
  creates one durable healthy receipt; exact replay does not switch again. After a
  later card advances the target sequence, the original signed synthetic Actions token
  can read the terminal merge context through the HTTP route, while an unrelated run
  ID and an unbound card are denied.

Focused command: `uv run pytest packages/plugin-release-control/tests/test_release_flow.py -q`
passed (2 tests); Ruff and `git diff --check` passed for the focused changes.

These are local integration proofs only. The generation provider/worker, GitHub API,
Telegram transport, builder and runtime are synthetic or mocked. The full deployment
test constructs the persisted admission evidence directly; its terminal read uses a
synthetically signed OIDC token and mocked JWKS/GitHub job APIs. It does not prove live
GitHub Actions, external provider behavior, Docker image builds, real Telegram, or a
production switch. Other P10 failure-injection cases, maintenance rehearsals, owner
acceptance remain outstanding as live work. The local matrix, SI-01–SI-12 evidence
index and maintenance runbook have since been added as described below.

## Consolidated local matrix and operational handoff

The failure-injection cases are kept beside the actual components rather than
duplicated in a separate rehearsal implementation:

| Boundary | Local test source and behavior |
| --- | --- |
| Decision, nonce, outbox and acknowledgement | `test_ledger.py`: transaction rollback on outbox failure, exact update/tap replay, rejection/expiry and ordered fenced A/B/C claims |
| Publication and remote response loss | `test_publication_service.py`: reopened durable submissions resume the same H/report, reconcile push/PR response loss and reject changed logical inputs/cap exhaustion |
| Dispatch/run cancellation and replacement | `test_broker_worker.py`: intent before HTTP, uncertain reply without resend, cancelled-run requeue with retained history/new UUID/fence, retired run-attempt denial |
| Owner revocation races | `test_revocation.py`: actual PostgreSQL lock-wins race, pre-admission denial, post-admission exact retry and receipt preservation; `test_telegram_ingress.py`: private owner-only command provenance |
| Merge/admission identity and maintenance | `test_broker.py`: expired lease/start, wrong parents/tree, full image/config identity, newer sequence, same-operation recovery and maintenance fencing |
| Host crash/reopen and restoration | `test_host_journal.py` and `test_host_service.py`: journal-before-effect, payload observation, expired start/recovery, health AND smoke, failed restoration, newer-config refusal and retained old terminal receipts |
| Runtime authority and isolation contracts | `test_host_service.py`, `test_host_source_issuer.py`, `test_runner_supervisor.py`, `test_runner_bootstrap.py`: strict bridge-only runtime, private source key, fixed process/capability/cleanup contracts with injected external effects |
| Watchdog and typed reporting | `test_watchdog.py` and `test_protected_bridge_http.py`: fixed-enum incident/resolution reports, stable send intent, quiet unchanged state and no auto-repair |

`apps/sb_bot/deploy/release-control/evidence-index.json` maps every SI criterion to
local test files and separately lists unrun live proofs and owner acceptance. Its
production flag remains false. The adjacent `README.md` documents maintenance
provisioning, schema/grant order and durability settings, credential separation,
full installed closure, source/runner images, ordinary/reconciliation cards,
revocation, retry/recovery, measured limits, watchdog, ingress/token rotation and
console/Tailscale break glass. All service/workflow/config templates remain inactive.

Final combined local regression passed after all writers froze: **1001 passed,
1 skipped, 10 deselected**. Eight existing PTB deprecation warnings came from adapter
tests. Ruff, formatting, `uv lock --check`, template JSON/workflow structure and
whitespace checks passed. No live proof or production acceptance is marked passed
by this source/test matrix. The known pre-existing polls failure was reproduced at
the original clean base earlier; this combined command covers the changed package,
Telegram adapter, app and extension regression scope, not every workspace test.
