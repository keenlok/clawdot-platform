# P05/P07 broker admission and runtime integration

Main writer owns `broker.py`, `broker_http.py`, `broker_schema.sql`, their tests and
this plan. P04 coordinator owns the client of these contracts; P06 serving and P09
runner source are separate writers. No production writes or live API calls are run.

## Local execution plan (2026-10-31)

**Goal:** Implement maintenance-installed broker persistence, independently authenticated
job and host routes, and a private-UDS ASGI runtime that fulfills the existing P04
coordinator contract without granting authority from caller claims.

**Architecture:** `broker_schema.sql` adds broker-owned target health/owner/policy state,
phase-specific lease and switch-operation receipts, and append-only arbitrary-parent
observations beside the P05 ledger. `broker.py` enforces the P05 ledger fence and broker
state in short transactions, while mandatory identity/evidence providers perform live
OIDC, GitHub, P01, and P04 checks outside transactions and are revalidated at admission.
`broker_http.py` is a bounded strict ASGI router and runtime builder; it checks protected
configuration before binding a private Unix socket and never migrates the schema.

**Files:** Create `src/chatcore_release_control/broker.py`,
`src/chatcore_release_control/broker_http.py`,
`src/chatcore_release_control/broker_schema.sql`,
`tests/test_broker.py`, and `tests/test_broker_http.py`; update this plan only.
The existing P05 ledger/schema, P07 verifier, P01/P04 evidence, and P08 host journal
remain read-only contracts, except that the new broker schema may extend its own
blocked/anomaly representation without coercing arbitrary parents into `MergeEvidence`.

### Ordered implementation steps

1. Import the real disposable Postgres fixtures from `test_ledger.py`; add failing tests
   proving the broker schema can be installed explicitly, has PUBLIC/default grants
   revoked, requires bootstrap target state, and rejects stale/mismatched target setup.
2. Write `broker_schema.sql` with target health/current identity, immutable merge phase
   lease and admission rows, append-only merge observations, immutable switch operation
   rows, actual terminal receipts, and readiness/reconciliation flags. Keep schema
   installation maintenance-only; test ordinary-role denial.
3. Add failing transaction tests for merge context, current full-D state, exact run/
   attempt/fence lease binding, stale-attempt denial, lease expiry, and same-run replay.
   Implement typed broker models and `BrokerStore` locking P05 target/card rows plus
   broker target state in one short PostgreSQL transaction.
4. Add failing admission/evidence tests using actual temporary Git commits and synthetic
   GitHub snapshots: identical Git SHA with changed image/config/generation is denied;
   inherited D-to-B content remains visible; incomplete delta/check evidence drift and
   wrong B/H/tree/policy deny; only fresh full facts and exact check digest persist
   expected merge tree. Implement mandatory injected evidence/OIDC adapters with no
   permissive callback defaults and safe fixed errors.
5. Add failing merge-reconciliation tests for immutable successful two-parent M, lost
   response replay, wrong tree/parent, arbitrary zero/one/three-or-more parents,
   unexpected/late effect and lease expiry. Store every bounded actual observation
   append-only, mark reconciliation required, and never synthesize two-parent evidence.
6. Add failing switch tests for exact prepared P05 operation and full payload, current
   GitHub M and actual host D, approval/policy/owner/fence/sequence, expiry/revocation
   before admission, revocation after admission, start/recovery deadline, duplicate
   operation, terminal receipt identity, and a completed receipt replay after later
   maintenance. Persist admission and target state atomically; recover split-store
   writes only from the same immutable authenticated receipt.
7. Add failing ASGI tests for exact paths/methods/content types/schemas, duplicate JSON
   keys, malformed IDs, body and total-time limits, fixed errors, signed OIDC route
   verification, host credential separation, auth failure, and no DB readiness. Add
   config tests for protected file ownership/modes and missing secrets before listener.
8. Implement a concrete runtime config parser, fresh JWKS fetch, installed workflow
   closure verifier, independent `GitHubJobCorrelationVerifier`, fixed-repository
   GitHub evidence client, P01 bundle collection/P04 check digest comparison, broker
   service construction, and private UDS CLI. Fake GitHub and JWKS via MockTransport;
   never open a listener or use real key material/network in tests.
9. Run focused broker/HTTP tests, adjacent P05/P07/P04 tests as compatible, Ruff and
   `git diff --check`; inspect full owned diff against this plan and record external
   proof/config gaps honestly. Do not run schema migration from app startup.

### Review checklist

- [x] No action is authorized by unsigned/caller-supplied identity booleans or JSON
  assertions; all evidence providers are mandatory.
- [x] No SQL transaction spans HTTP/Git/network calls; every operation is fenced and
  rechecks exact current immutable state under the same target row lock.
- [x] Arbitrary parents are represented as observations and reconciliation state only.
- [x] A broker dispatch, merge receipt, or host receipt never itself claims actual
  external truth without the corresponding independent observer.
- [x] CLI checks protected configuration and DB schema readiness before creating the
  UDS; no default TCP listener or automatic DDL exists.
- [x] Local fake transports, synthetic keys and temporary local Postgres are clearly
  distinguished from production or external verification.

## Protected facts and storage

- Extend maintenance schema with broker target state (current owner, installed
  policy digest, full healthy D identity and generation/reconciliation flag), immutable
  phase-specific merge lease/admission, append-only arbitrary-parent merge observations,
  and one immutable switch operation per approval/fence/operation UUID.
- Target row locking serializes merge lease/admission, revocation, maintenance and first
  switch admission. Never keep a SQL transaction open across HTTP requests.
- A dispatch claim is not merge authority. Bind one authenticated run/attempt under the
  existing P05 fence. Rebinding is explicit reconciliation, never silent retry.
- Verify actual local Git D/B/H facts independently against the card's full review
  bundle; mechanical extraction does not set human review. Recompute current required
  check evidence with P04 and require equality with card check_evidence_digest.
- Before merge require approval active/unexpired, current Telegram owner, current
  policy, exact main B/head H/tree, full actual D equality, active sequence/fence,
  full review/check identities and a live same-run lease. Fail closed on outages.
- Reconcile exact immutable M and parents/tree via independent GitHub observation.
  Lost responses return durable receipts. Arbitrary-parent/unexpected/late effects are
  append-only observations with target reconciliation_required and merged-blocked
  status; never fabricate a two-parent MergeEvidence for an octopus result.
- Preparation grants no switch authority. The protected host reports actual D under
  its OS lock; broker atomically admits one prepared P05 HostOperationBinding against
  current main M, owner/policy/approval/fence/sequence and actual full D. Persist bounded
  start/recovery deadlines. Expiry/revocation before admission denies it; after admission
  only this operation's start/reconciliation/recovery is permitted.
- Persist terminal observation via P05 full-identity receipt checks, then update broker
  state. On interruption between stores, new admissions fail closed and same-receipt
  reconciliation finishes; reporting an already completed receipt never overwrites newer
  maintenance or starts a second effect.

## Routes and strict schemas

All job routes require signed GitHub OIDC, installed workflow closure and independent
GitHub job correlation. Request numeric run IDs are only correlation inputs; never
accept caller-provided authenticated identity/approval booleans. Host routes require a
separate protected host credential/private UDS principal, never relay OIDC alone.

- GET `/v1/releases/{uuid}/merge-context`: returns immutable approval ID, repository
  name/numeric repository+GitHub owner IDs, PR number, deployment target, sequence,
  dispatch_fence, full deployed_identity, base_sha/head_sha/head_tree, bound policy
  digest/raw installed PolicyManifest, review_bundle/review_bundle_digest and workflow_sha.
- GET `/v1/releases/{uuid}/current-state`: full actual deployed_identity plus git SHA
  projection and installed policy digest, never Git-only identity.
- POST `/merge-lease`: exact dispatch_fence/run_id/run_attempt/check_run_id; return
  lease_id/fence/expires_at. DELETE same endpoint adds lease_id. Release phase lease
  only; do not release P05 target serialization before deployment terminal receipt.
- POST `/merge-admission`: same IDs/fence + lease_id + head_sha. Broker independently
  evaluates all facts and returns exact bound D/B/H/policy/lease, not arbitrary payload.
- POST `/merge-result`: same admitted lease/run/fence + immutable merge SHA/two parents/
  tree/expected tree. Repeat remote verification; persist result or blocked observation.
- POST `/merge-anomalies`: same identity + bounded actual commit/tree/parent list and
  enumerated reason. Record actual external effect even after phase lease expires;
  a receipt is no grant for another merge.
- Protected host operation routes fetch fixed deployment context, record verified
  prepared identity, admit/start one operation and persist/reconcile terminal receipt.
  They accept IDs/typed identities only, no paths/commands/URLs/images selected by relay.

## Concrete runtime construction

- Broker OIDC authenticator obtains bounded fresh fixed-origin JWKS and validates
  RS256/issuer/audience/time/repo/private/event/ref/workflow/run/attempt/check/runner.
  Unverified token fields may select comparison inputs only; no identity is used until
  signature/claims/closure/job verification succeeds.
- Installed config names fixed repository/object store, policy/closure map, host service
  origin, current owner/target, private socket and secret files. File ownership/modes are
  checked before reads; no secret is logged, returned or included in repr/errors.
- Actual adapters use fixed GitHub paths, authenticated protected-host state lookup,
  P01 Git verification and P04 provenance resolver. No dummy-True callback defaults.
- ASGI app has bounded bodies/headers/total time, exact methods/routes/schema and fixed
  structured errors. CLI constructs actual clients/store/service and serves private UDS;
  missing configuration fails before listener readiness. Tests never install a listener
  or contact external GitHub/Telegram/host services.

## Verification

Use disposable real Postgres from the existing test fixture and actual temporary Git
objects, signed synthetic OIDC/JWKS and fake external HTTP transports. Cover stale full
D with same Git, inherited unreviewed base, check provenance drift, expired/revoked
approval immediately before/after switch admission, old attempts/fences, duplicate and
lost-response calls, arbitrary-parent external effects, bounded deadlines, maintenance
serialization and completed-receipt retries after later maintenance. Add schema role
checks for new tables/helpers. Review actual code separately and repair findings.

## Coordinated client contract additions

Context also includes `status`, nullable `merge_commit/merge_tree/merge_parents`,
nullable `host_receipt: {operation_id,receipt_id,status,fence}`, and nullable
`merge_lease: {lease_id,fence,expires_at,admitted_at,run_id,run_attempt,check_run_id,may_start}`.
This allows reporting/reconciling recorded results after the expected B→M transition;
do not apply PR-open/main=B guards to an already completed result.

Merge admission exact response:
`{approval_id,lease_id,dispatch_fence,expires_at,admitted,may_start,deployed_identity,
base_sha,head_sha,head_tree,policy_digest,expected_merge_tree,run_id,run_attempt,check_run_id}`.
A durable admitted receipt after its start window returns `may_start:false`; it does
not grant another API operation. The coordinator inspects actual closed/merged PR
state first when recovering merge_authorized; no blind repeat merge.

Current-state exact response: `{deployed_identity,deployed_commit,policy_digest}`;
the coordinator compares full identity before exposing the Git projection. GitHub
repository owner IDs, Telegram owner IDs, deployment target and Git main branch are
separate namespaces and must not be compared/interchanged.

## Transaction and recovery implementation details

- Maintenance target state is separate from release cards and stores last healthy D,
  installed policy, Telegram owner, generation and reconciliation_required. Initial
  bootstrap advances target_queue.next_sequence beyond D.target_sequence. New release
  sequence must exceed D; rejected card gaps are allowed. Maintenance must hold host
  lock outside broker transaction and takes the same target DB lock as admissions.
- Target active P05 fence spans dispatch→merge→deployment. A merge phase lease never
  frees that target fence. Bind the one verified run/attempt via P05 before granting
  a same-run phase lease; old attempts cannot acquire a fresh lease by timeout alone.
- Independent review verifier recomputes actual P01 D/B/H facts and compares all Git
  facts against the stored card review (except mechanical `complete_delta_reviewed`
  remains false until compared to the protected explicit attestation). Require the
  card's check_evidence_digest to equal P04's freshly resolved canonical snapshot.
- Use mandatory concrete fixed-repository observer/client and protected host-state
  client; no default-True proof callbacks or caller JSON claims are authority. Test
  fake external transports explicitly as such; actual Git objects and SQL are real.
- Arbitrary-parent merge anomalies need append-only broker observation storage and
  a target reconciliation flag. Extend maintenance schema/card blocked evidence
  constraint to permit genuine recorded anomaly identity rather than inventing two
  parents. Late observations must not overwrite a newer healthy deployment; block
  new admission for explicit reconciliation instead.
- Prepared P05 HostOperationBinding is not switch admission. Add an immutable broker
  switch-operation record with full baseline/payload, approval/fence/run binding,
  admitted/start/recovery deadlines and started/terminal status. Only authenticated
  protected host requests may report actual D/payload under host lock. Relay requests
  contain IDs only and cannot choose image/config/commands/hosts.
- At first admission recheck current GitHub main=M, active owner/policy/approval,
  actual D versus recorded state/card, fence/sequence and the exact prepared payload.
  Existing same-operation receipt is returned before freshness checks for reconciliation;
  starting an unstarted operation still checks its stored deadline. Revocation before
  admission denies; after admission it cannot start a different operation or interrupt
  bounded safe recovery. Maintenance blocks/waits while admitted/started operation is
  unresolved, then may advance config generation after actual-state inspection.
- Record terminal host evidence with P05 full-identity checks, then update broker
  operation/healthy target state. A crash between these writes leaves a fail-closed
  pending operation; same-receipt retry completes it. Already-terminal receipt retries
  are read-only and never revert later maintenance. Recovery failure/restored old image
  must not hide undeployed merged main content; explicit reconciliation remains required.
- Schema source is maintenance-only. New ordinary bot roles have no access; revoke
  PUBLIC and default function grants. Runtime startup validates schema readiness but
  never applies it. CLI mounts private UDS only; external HTTPS ingress is an explicit
  maintenance integration step and is never activated by local tests.

## Remaining local completion plan (2026-10-31)

Before declaring this increment ready, close the remaining operational safety checks in
this order:

1. Add focused regression tests for current-owner/policy revocation at switch admission,
   immutable run correlation on merge release/result, start/recovery deadlines, and
   operator reconciliation that holds the same target lock as release admission.
2. Implement a direct maintenance-only `BrokerStore` command (not an HTTP route) that
   requires an externally inspected healthy full host identity and matching current
   GitHub main, rejects any unresolved operation/active target fence, serializes under
   the P05 target queue lock, advances target generation/sequence safely, and clears a
   reconciliation fence only with that explicit trusted command.
3. Harden protected runtime config against private repository paths and malformed
   duplicate JWT JSON keys; ensure construction failures close the pool/HTTP clients and
   preserve fixed token-free errors.
4. Run focused broker/HTTP/Postgres tests and Ruff. Inspect every owned changed file and
   record which external provider, host, webhook, and production deployment proofs are
   intentionally still unperformed.

## Phase-specific self-hosted relay work (2026-10-31)

The merge job and the deploy/reconcile runner are separate authenticated GitHub jobs.
Keep each signed token's `check_run_id` tied to its own independently fetched completed
GitHub job. Bind the relay job to the existing immutable merge lease by repository,
run ID, attempt, workflow SHA, and dispatch fence; do not require its check ID to equal the
merge job's check ID. The configured workflow closure is shared, but runner environment
is fixed per phase: GitHub-hosted for merge, self-hosted for host relay.

Add one exact runner-only operation route for host deployment and one for reconciliation.
Each accepts only the approval UUID in the path plus operation UUID in an exact JSON
body. After phase-specific OIDC and immutable run-binding validation, proxy only the
fixed `/v1/deploy` or `/v1/reconcile` method/path/body over the protected host UDS, with
bounded request/response and no runner-visible host credential. Validate the exact
`{operation_id,phase,receipt,payload_digest}` response and pass only that typed journal
record through. The host server already owns journaling, switch admission, and broker
receipt reporting; broker relay does not establish host truth or return arbitrary host
response content.

Runtime config also installs the complete GitHub App role-ID map (`publication`,
`broker`, `runner_registration`) and rejects any duplicate IDs. Broker token minting uses
only the `broker` role's app ID so renewable token scope is backed by a distinct App
principal, not just distinct permission JSON.

## Local completion evidence and external limits

- `uv run pytest packages/plugin-release-control/tests/test_broker.py
  packages/plugin-release-control/tests/test_broker_http.py -q`: 28 passed against the
  suite's disposable Unix-socket-only PostgreSQL fixture, synthetic signed OIDC/JWKS,
  and fake GitHub/host transports.
- `uv run ruff check` on `broker.py`, `broker_http.py`, `test_broker.py`, and
  `test_broker_http.py`: passed. `git diff --check` on owned source/tests/plan: passed.
- Tests cover retired run-attempt denial, distinct self-hosted relay check binding,
  fixed host relay paths/body/response schema, owner revocation before/after switch
  admission, bounded switch/recovery deadlines, maintenance-role reconciliation, and
  duplicate App role-ID rejection.
- The maintenance reconciliation primitive is intentionally not an ASGI endpoint and
  requires a separately provisioned `release_control_maintenance` database role. Its
  caller must gather healthy full host state and current GitHub main outside the short
  transaction. No production role grants or maintenance command runner are installed.
- No real GitHub App, OIDC issuer/JWKS, host UDS, Telegram bot, network API, deployed
  image, or production DB was contacted. No production credentials, schema installation,
  role provisioning, ingress, service startup, or end-to-end deploy was performed.
- The private runtime config, workflow object store, App IDs/installation, maintenance
  role, protected host socket and operating-system permissions still require an
  operator-owned installation/bootstrap procedure. The broker's local source and fake
  transport integration do not prove those external deployments or their runtime
  availability.

## Dispatch identity and host evidence handoff (2026-10-31)

Before further broker source edits, add tests and implement these trust bindings:

1. Make `broker_dispatch_intent.dispatch_id` a persisted immutable UUID. Under the
   current approval/fence, resolve that ID from the DB and require signed GitHub run
   `display_title` to equal exactly `release:<dispatch_id>`; never accept a title/nonce
   from request JSON or fall back when the intent is missing/retired. This prevents an
   old run rerun from claiming a newer fence after a cancelled attempt was requeued.
2. A run ID present in append-only terminal run history is retired for every attempt.
   Deny the ID before binding or issuing another merge lease.
3. Extend only the protected host deployment-context response with immutable D/B/H,
   expected tree, and recorded merge tree/parents so host verification can independently
   validate raw M and P01 ancestry; preserve the P04 job context's exact schema.
4. Test matching and mismatching run display title against active/retired dispatch IDs,
   all-attempt run history denial, and the expanded host-context response. Do not create
   GitHub runs, host listeners, or external API calls.
