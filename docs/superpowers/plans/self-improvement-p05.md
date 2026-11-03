# P05 Protected Ledger Implementation Plan

> **For agentic workers:** Local inline execution under the approved P05 assignment.

**Goal:** Add maintenance-installed Postgres schema and an async ledger that binds immutable review evidence to a private owner card, atomically records approve/reject with update deduplication and an approval-only dispatch outbox, and serializes target sequencing and dispatch claims.

**Architecture:** `schema.sql` is an operator-run schema source only; application code never applies migrations. `ReleaseLedger` uses asyncpg pools/transactions and fixed SQL against a private schema. A frozen `ReleaseBinding` validates and canonicalizes candidate/review data; decision requests must match the stored binding digest, owner, private chat, exact card message and one-time nonce. Target rows serialize sequence allocation and outbox claims; monotonically increasing fences bind a claimed dispatch. Ordinary failures have fixed `LedgerError` codes with no database exception text.

**Tech Stack:** Python 3.12, asyncpg, PostgreSQL 18 disposable local Unix-socket server, pytest, Ruff.

---

### Task 1: Write the maintenance schema and local Postgres fixture plan

**Files:**
- Create: `packages/plugin-release-control/src/chatcore_release_control/schema.sql`
- Test: `packages/plugin-release-control/tests/test_ledger.py`

- [x] **Step 1: Define the disposable server fixture** using only `/opt/homebrew/bin/initdb` and `pg_ctl`, unique temp data/socket/log directories, `listen_addresses=''`, socket permissions `0700`, and a scrubbed process environment without inherited `PG*` variables.
- [x] **Step 2: Add failing schema tests** for maintenance-only installation, private schema privileges, strict status and decision constraints, uniqueness of card/message/update/sequence/outbox identities, and app-role denial.
- [x] **Step 3: Run the test file and observe missing schema/test API failures.**
  - Run: `uv run pytest packages/plugin-release-control/tests/test_ledger.py -q`
  - Expected before implementation: missing module/schema fixture failures.
- [x] **Step 4: Write idempotent schema SQL** with target queue, immutable approval-card binding, deduplicated Telegram updates, and decision outbox tables. Revoke all schema/table/sequence privileges from `PUBLIC`; create no role, network listener, or automatic migration path.

### Task 2: Implement immutable card/binding creation and target sequencing

**Files:**
- Create: `packages/plugin-release-control/src/chatcore_release_control/ledger.py`
- Modify: `packages/plugin-release-control/src/chatcore_release_control/schema.sql`
- Test: `packages/plugin-release-control/tests/test_ledger.py`

- [x] **Step 1: Add failing tests** for binding validation/canonical digest, exact owner DM/message/nonce fields, fixed 24-hour expiry, immutable binding, and concurrent card creation yielding unique monotonic per-target sequences.
- [x] **Step 2: Run tests and confirm the ledger API/schema is missing.**
- [x] **Step 3: Implement frozen value types** `ReleaseBinding`, `ReleaseStatus`, `Decision`, `DecisionCommand`, `LedgerError` and async `ReleaseLedger.create_card`. Require exact SHA/digest shapes, positive numeric repository/PR/owner IDs, a private DM chat equal to owner ID, a strong nonce, and a JSON review bundle whose canonical SHA-256 matches its supplied digest. Store only the nonce digest. Under a target-row lock, allocate a monotonically increasing sequence and bind the immutable candidate/review JSON and digest.
- [x] **Step 4: Re-run the focused tests on the real disposable Postgres database.**

### Task 3: Implement atomic owner decision, update deduplication, and outbox

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/ledger.py`
- Modify: `packages/plugin-release-control/src/chatcore_release_control/schema.sql`
- Test: `packages/plugin-release-control/tests/test_ledger.py`

- [x] **Step 1: Add failing tests** for approve creating exactly one dispatch outbox row, reject recording an explicit decision with no outbox row, same-update replay returning the recorded result, same update ID with changed payload rejection, a second matching tap returning the original decision, opposite decision rejection, wrong sender/chat/message/nonce/binding rejection, expired card rejection, and update decision/nonce/outbox atomic rollback.
- [x] **Step 2: Run those tests and verify the current implementation cannot satisfy them.**
- [x] **Step 3: Implement `record_decision` as one database transaction**: lock the approval row, deduplicate by Telegram update ID plus canonical request digest, verify owner/chat/card/nonce/expiry/binding, persist explicit approve/reject and nonce consumption, insert a dispatch outbox row only for approval, and persist the update result before returning. Same matching decision retries are idempotent; any mismatched replay or conflicting decision fails with a fixed code. No rejection may create dispatch work.
- [x] **Step 4: Inject an outbox constraint/trigger failure and prove approval, nonce consumption and update receipt all roll back together. Re-run all focused tests.**

### Task 4: Implement typed transitions, target dispatch fencing, and role isolation

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/ledger.py`
- Modify: `packages/plugin-release-control/src/chatcore_release_control/schema.sql`
- Test: `packages/plugin-release-control/tests/test_ledger.py`

- [x] **Step 1: Add failing tests** for invalid status transitions, target-serialized ordered claims, monotonic fence increments, stale-fence rejection, queue release on terminal status, and inability of an ordinary synthetic DB role to read/write ledger rows.
- [x] **Step 2: Implement typed status transitions and `claim_next_dispatch` / reconciliation methods** using target and outbox row locks, ordered sequence selection, durable claim owner/lease, and a monotonically increasing fence. A claim retry for the same logical approval returns the persisted claim; a stale fence cannot acknowledge or advance it. Terminal outcomes release the target only when approval ID and fence match.
- [x] **Step 3: Run real Postgres tests** for app-role denial and claim/rollback/concurrency behavior. No external DB, TCP listener, or production credentials are used.

### Task 5: Final review and evidence

**Files:**
- Review: `packages/plugin-release-control/src/chatcore_release_control/ledger.py`
- Review: `packages/plugin-release-control/src/chatcore_release_control/schema.sql`
- Review: `packages/plugin-release-control/tests/test_ledger.py`
- Review: `docs/superpowers/plans/self-improvement-p05.md`

- [x] **Step 1: Inspect the complete P05 diff against the request and current callers**; verify no application migration path, root dependency/config edit, existing module edit, or external database access was introduced.
- [x] **Step 2: Run focused verification:**
  - `uv run pytest packages/plugin-release-control/tests/test_ledger.py -q`
  - `uv run ruff check packages/plugin-release-control/src/chatcore_release_control/ledger.py packages/plugin-release-control/tests/test_ledger.py`
  - `git diff --check`
- [x] **Step 3: Record concrete local Postgres evidence and remaining limits.** The test server is synthetic and temporary. Schema-role provisioning, broker/Telegram callback wiring, deployment reconciliation, production Postgres role/network/bootstrap, and failure rehearsal on the protected service remain unimplemented.
- [x] **Step 4: Map SI coverage accurately:** transactional decision/outbox and update replay contribute local SI-03/SI-07 evidence; OIDC, webhook authenticity, process restart integration, external callback ack/retry, GitHub dispatch and owner-removal behavior remain unproven. The immutable binding and sequence evidence contribute partial SI-06/SI-12 only; card generation/full D-to-H verification and inherited-code scenarios remain upstream.

## Local result and limits

The local integration suite runs **31 tests** against temporary Postgres 18.4 databases over a Unix socket with `listen_addresses=''`, socket permissions `0700`, scrubbed `PG*` process environment, synthetic rows/roles, and fixture cleanup through `pg_ctl` in `finally`. No TCP or existing DSN is used. An injected outbox insertion failure proves the decision, nonce consumption, update receipt and outbox entry roll back together. The DB trigger and ordinary-role test establish immutability and privilege denial for this local schema setup. A synthetic function created after schema installation confirms PUBLIC cannot execute future functions under the installer role's defaults.

The public API is intentionally narrow:

- `ReleaseBinding(...).digest` and `.to_json()` provide a canonical snapshot of PR/base/head/tree, deployed identity, policy digest and complete review bundle.
- `CardNonce()` creates a fresh URL-safe random card nonce; `ReleaseLedger.create_card(binding, owner_user_id, chat_id, message_id, nonce, now=None)` accepts only this trusted value object, stores only its digest, and allocates the next locked target sequence; expiry is always 24 hours. The broker retains `.value` only to create callback markup.
- `ReleaseLedger.record_decision(DecisionCommand(...), now=None)` atomically deduplicates the Telegram update, checks the owner/chat/message/nonce/binding, stores explicit approve/reject and consumes the nonce. Approval creates one `dispatch_release` outbox record; rejection creates none.
- `ReleaseLedger.claim_next_dispatch(target, worker_id, now=None, lease=...)` claims only the lowest queued sequence and assigns a monotonically increasing target fence. `acknowledge_dispatch(approval_id, fence, now=None)` is idempotent for the same dispatched fence. Stale fences cannot acknowledge or advance state.
- `ReleaseLedger.get_card_by_message(chat_id, message_id)` is an internal trusted-ingress resolver for exact positive private chat/message IDs; it is not an ordinary application RPC.
- `ReleaseLedger.bind_run_attempt(approval_id, fence, run)` stores an immutable GitHub repository/run/attempt/workflow/check binding under the target fence before merge authorization. `record_merge_result(approval_id, fence, evidence, expected_merge_tree)` stores the actual merge commit/tree/parents and records `merged_verified` or `merged_blocked` from the supplied evidence.
- `ReleaseLedger.bind_host_operation(approval_id, fence, operation)` persists an immutable full baseline and expected payload identity before transitioning to `deploying`. The payload must match recorded M, installed policy, sequence, and the baseline configuration generation/digest.
- `ReleaseLedger.record_host_receipt(approval_id, receipt)` persists a canonical terminal host receipt and digest. A healthy receipt must match the bound operation ID and every field of its expected payload identity; a restored-healthy receipt must match its baseline identity. `get_host_receipt(approval_id)` verifies the receipt digest on readback. Only a matching receipt can record `healthy`, `restored_healthy`, or `recovery_failed` and release the fence.
- `ReleaseLedger.advance_status(approval_id, status, fence=None)` enforces typed non-evidence transitions. A dispatch claim grants no merge or host-switch authority. A target stays fenced through active deployment, `merged_blocked`, and `recovery_failed`; this part does not add an operator reconciliation API to clear those blocked states.

SI-03/SI-07 have local transaction, update-replay, rejection, exact message lookup, immutable run/merge/host evidence storage, and role-isolation evidence, but Telegram webhook authenticity, callback routing, process restart/ack integration, owner removal and token rotation remain unimplemented. SI-06 has local target ordering, outbox claiming, run/merge correlation fields and fencing evidence; GitHub dispatch authenticity and external-effect reconciliation remain unimplemented. SI-12 has immutable binding and evidence storage only; trusted full D-to-H construction/card rendering and inherited-code scenarios remain unimplemented. Queued expiry, current-owner and policy admission, OIDC, and authentication of external GitHub/host evidence remain broker/P07 responsibilities. Stored evidence is only as trustworthy as its caller; it does not prove an external observation was authentic. Production schema/role provisioning, the broker's trusted identity verifier, Telegram update consumer, GitHub dispatch/reconciliation, cross-service crash recovery, and owner bootstrap remain outside this deliverable. Schema installation is an explicit operator maintenance action, never an application startup behavior.

## Main review repair plan

- [x] Replace the duplicated deployment identity dictionary contract with the existing `policy.DeploymentIdentity` dataclass, preserving its exact `image_digest` (`sha256:` prefix), `config_generation`, and `config_digest` fields. Test bindings with real `DeploymentIdentity` instances.
- [x] Persist immutable GitHub run ID/attempt/workflow/check binding under the target fence before the ledger can record `merge_authorized`.
- [x] Persist actual merge commit/tree/parents plus expected merge tree. A dedicated merge-result method records either `merged_verified` or `merged_blocked`; generic status updates cannot set either result.
- [x] Persist terminal host operation/receipt IDs and canonical receipt data/digest, with safe readback. A dedicated receipt method records `healthy`, `restored_healthy`, or `recovery_failed`; generic status updates cannot set those states.
- [x] Add disposable Postgres regression cases for missing run/merge/host evidence, stale fences, unexpected merge parents/tree, immutable evidence, host receipt readback, and no generic transition to evidence-backed statuses. Preserve transactional decision/outbox and ordinary-role tests.
- [x] Explicitly state that queued expiry, current owner/policy admission, OIDC and external evidence authentication remain broker/P07 responsibilities. A dispatch claim or persisted status is not authority to merge or switch a host.
- [x] Revoke PUBLIC function privileges for future schema functions through default privileges. Because PostgreSQL's built-in PUBLIC function grant is global, also revoke that global default for the installer role; schema-scoped table, sequence, function and current-object revokes remain in place.
- [x] Wrap pool acquisition/release failures as fixed `LedgerError(storage_error)` while allowing cancellation to propagate; prove it with an injected connection-acquisition failure and synthetic DSN text.
- [x] Add a trusted-only strict `get_card_by_message(chat_id, message_id)` ledger lookup for P06's short callback resolver and verify exact message lookup against disposable Postgres.
- [x] Re-run the focused real Postgres suite, Ruff and diff checks; repeat the self-review and report remaining external integration proof.

## Additional review repair plan

- [x] Persist a fenced immutable host-operation binding containing the complete expected payload identity and its deployed baseline before host deployment; require healthy receipts to match the bound operation ID and full payload identity. Add schema constraints and Postgres regressions for identity mismatches and target-fence retention.
- [x] Make `ReleaseBinding` snapshot properties return defensive data reconstructed from canonical bytes so mutations to caller input or returned review data cannot diverge from the stored digest.
- [x] Replace raw caller-selected card creation nonces with a trusted RNG-generated `CardNonce`; add a unique nonce-digest constraint and migrate tests/callers to pass the typed value while callbacks continue to use its value.
- [x] Re-run disposable Postgres tests, Ruff and diff checks; update evidence and limits only after they pass.

# Resumed dispatch/runtime integration (main)

Implement a separate Broker App issuer (Actions write, Contents/PR/Checks/Metadata
read only; one repository), bounded fixed-origin HTTP and renew-before-expiry.
Test signed JWT/token scope, mismatched responses, renewal and safe errors using
synthetic keys and MockTransport. Wire this issuer into P07 through a per-request
token provider; never retain a static token as the runtime credential model.

Add an executable broker worker for durable outbox dispatch, typed run polling,
reconciliation and owner reporting. Claim only ledger work, recheck installed target
owner/policy/baseline, dispatch exactly release.yml on main with immutable approval,
PR/H fields. Persist send intent before effect and accept authenticated run claims;
do not guess latest runs after 204 or a lost response. Poll bound run IDs, retain
ambiguous dispatches for reconciliation, and preserve approved logical releases
through cancellation/requeue. Reports use protected bridge APIs and bounded typed
states, not raw logs or workflow artifacts. Test against temporary PostgreSQL and
fake HTTP; no actual dispatch/messages/config installation.
