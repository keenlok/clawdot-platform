# P08 Protected Host Journal and Service Implementation Plan

> **For agentic workers:** Implement this plan task-by-task in the current worktree. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add a durable, host-local journal that serializes one deployment operation, records its baseline and payload identities, and supports safe retry/recovery decisions after ambiguous interruption.

**Architecture:** Keep the journal host-only and independent of broker/Postgres state or Docker. Store the current JSON operation record and immutable per-operation terminal receipt files under one directory, guarded by an OS advisory file lock. Commit replacements by writing sibling temporary files, flushing each with `fsync`, atomically renaming it over its destination, then flushing the parent directory. Archive a terminal receipt before clearing the current record; a crash between those writes leaves the terminal operation readable and the archive retry idempotent. Expose validated immutable operation records and explicit transition/reconciliation methods that bind retries to one operation ID, refuse late starts, and protect newer configuration generations.

**Tech Stack:** Python 3.12 standard library (`fcntl`, `json`, `os`, `tempfile`, `dataclasses`, `enum`, `pathlib`); pytest temporary directories; existing `DeploymentIdentity`, `JournalState`, and transition validator from `policy.py`.

## Task 8: Protected host service source and fixed build/runtime policy

**Status:** Implemented and independently reviewed locally; final combined regression
passed 1001 tests (1 skipped, 10 deselected). Operational
deployment and Linux isolation proof remain separately gated.

The installed host source configuration now points to `source_app_config_file`, not a
static expiring installation-token file. Its strict root-private JSON binds the three
distinct App IDs, the Broker installation, and the protected Broker App key. Source
fetches obtain renewable contents/metadata-read tokens from that same Broker App;
each fetch uses a short-lived private askpass credential file with no token in argv,
environment, source, logs or ordinary runtime. The file-token injection remains only
as a library/test option. Focused issuer/source tests passed 10 cases.

The ordinary runtime uses the separate root-private `app_runtime_env_file`, included
with Compose and resource limits in the complete configuration digest inventory.
Compose is strict JSON and is checked against the installed definition before any
Docker call. The bridge-only entrypoint rejects control authority environment names
and writes only the ordinary bridge credential into a private tmpfs file. Operator
templates require replacement with actual reviewed IDs, digests and measured limits.

**Files:**
- Create: `packages/plugin-release-control/src/chatcore_release_control/host_service.py`
- Create: `packages/plugin-release-control/src/chatcore_release_control/host_service_http.py`
- Create: `packages/plugin-release-control/tests/test_host_service.py`
- Create: `packages/plugin-release-control/tests/test_host_service_http.py`
- Create: `services/release-control/src/chatcore_release_control/templates/p08-build-policy.json.template`
- Create: `services/release-control/src/chatcore_release_control/templates/p08-runtime-compose.yml.template`
- Create/update: this plan.

**Design and trust boundaries:**

- Parse only a root-owned, strict installed host policy. It names one fixed repository,
  target, broker UDS/origin, trusted policy digest, protected credential file, installed
  source/build/runtime paths, fixed Compose services/networks/volumes, allowlisted runtime
  environment keys, health and smoke checks, and bounded resource/time/output limits.
  Missing configuration or capability manifests fail closed before listener readiness.
- Host admission is a distinct service principal using the P05 protected-host routes over
  the private UDS. Relay OIDC and relay-provided JSON are not host authority. Route bodies
  contain only approval/operation/fence IDs and canonical full deployment identities;
  callers never select repository, URL, path, command, image, environment, service, or host.
- Fetch the exact approved merge commit from the configured repository using host-only
  source-read credentials, then verify the full P01 frozen baseline, protected history,
  policy and migration-set equality before building. Feed only the verified P02 baseline
  bundle to a fixed installed offline recipe/backend/cache. Never execute candidate Docker,
  Compose, hooks or scripts on the host; no candidate-controlled Dockerfile/Compose or
  helper defines build security. Build is disposable, bounded, `network=none`, and has no
  production environment, Docker socket, or host bind mounts. Only the resulting image
  digest computed/verified by the service can be used for runtime.
- Retain the previous healthy image and exact compatible source/config/policy artifacts.
  Under the host OS lock, re-read the complete actual deployment identity and installed
  configuration generation/digest before broker switch admission and immediately before
  the first runtime effect. Persist journal prepare and begin-switch before effect. Start
  only the installed fixed Compose/service/network/volume definition and explicitly
  allowlisted application runtime environment; release, GitHub, broker, ledger and bot API
  credentials are excluded. Require both installed health and smoke checks.
- On interruption, observe actual containers/config before deciding. Unknown observations
  require reconciliation. Restore only while actual state is still the journaled payload
  at the exact config generation; never overwrite a newer generation or restart unrelated
  services. Restoration must pass old-image health and smoke before `restored_healthy`.
  Recovery runs only for the same operation within its persisted deadline.
- The UDS CLI is private/owner-only and supports a narrow typed RPC. Fixed error codes and
  release/receipt/phase IDs only; no app logs, credentials, host paths, commands, or raw
  process output. HTTP transport applies exact method/path/schema/body/time bounds.
- Concrete process, Git, Docker/Compose, broker HTTP and UDS adapters are injectable. Unit
  tests use fake process/HTTP/Docker adapters, temporary Git repositories and temp journals;
  they never run Docker, Compose, external network, or production services.

**Acceptance checks:**

- Competing operation, broker denial, invalid policy/config, P01 mismatch, unknown actual
  state, deadline, and configuration race all fail before a host effect.
- Crash/reopen after prepared, begin-switch, container replacement, health failure,
  restore start, and restore failure reconciles exact actual identity without guessing.
- Healthy success requires exact payload full identity plus both health and smoke; restore
  success requires exact baseline identity plus both checks. Failed restore records failure.
- Relay-supplied host/image/command/path/env values are rejected or ignored; service fixed
  policy controls all host effects. Exact terminal receipt retries are read-only.
- Tests/Ruff and self-review cover all changed files. No statement that fake tests establish
  Docker isolation, capacity, runtime compatibility, or service health in production.

**Operational proofs intentionally out of scope:**

No image is built, no Docker/Compose/runsc command is executed, no service is installed or
restarted, no secret is read, and no live host/broker request is made. A separately reviewed
operator run must still prove installed policy ownership, backend/capacity limits, actual
network-none build behavior, runtime env isolation, service health/smoke compatibility,
crash recovery, watchdog/break-glass and live broker/receipt behavior before deployment.

---

## Scope and acceptance

Only these P08-owned paths may change:

- Create `packages/plugin-release-control/src/chatcore_release_control/host_journal.py` for the locked durable journal and recovery API.
- Create `packages/plugin-release-control/tests/test_host_journal.py` for temp-directory, reopen, race, deadline, and recovery tests.
- Create this plan at `docs/plans/self-improvement-p08.md`.

Acceptance criteria:

- A successful prepared operation stores an operation ID, baseline identity, payload identity, payload identity digest, start deadline, and policy-bounded recovery deadline durably before the caller can begin switching.
- File updates are protected by an OS lock and survive reopen; competing operations cannot replace an active operation.
- Repeating the same operation ID and identical request returns its original record/terminal receipt without starting a second operation. After explicit archival of a terminal receipt, a later operation may be prepared; the prior terminal receipt remains durable and retrievable. Reusing an ID with different input and presenting another operation ID fail closed.
- Phase transitions are restricted to the approved monotonic phase graph from `policy.py`.
- `prepared` cannot transition to `switching` at or after its deadline.
- Ambiguous state is never guessed: an absent/unknown observed identity or unresolved health result yields an explicit reconciliation-required error and cannot mutate the journal into success.
- Recovery decisions use the observed actual deployment identity. Restoration is permitted only while the observed identity still equals the journaled payload identity and configuration generation; a newer/unrelated identity blocks restoration and is recorded as recovery failure.
- Terminal receipts for `healthy`, `restored_healthy`, and `recovery_failed` remain readable and immutable on retry, including after later operations start; the receipt binds the persisted recovery deadline.
- The journal has no Docker, shell, network, broker, database, or production-service calls.

## Task 1: Define journal request, record, and error contract

**Files:**
- Create: `packages/plugin-release-control/src/chatcore_release_control/host_journal.py`
- Test: `packages/plugin-release-control/tests/test_host_journal.py`

- [x] **Step 1: Add the first failing test for durable prepare and reopen.** Create a temp-path journal, call `prepare(operation_id, baseline, payload, start_deadline, now)`, reopen a new `HostJournal` for the same path, and assert the operation ID, both full `DeploymentIdentity` objects, payload digest, deadline, and `PREPARED` phase match.
- [x] **Step 2: Run the single new test and confirm it fails because the module/API is absent.** Run `uv run pytest packages/plugin-release-control/tests/test_host_journal.py::test_prepare_is_durable_across_reopen -q`; expected result is a collection failure naming the missing module.
- [x] **Step 3: Define immutable `JournalRecord` and safe bounded errors.** `JournalRecord` contains `operation_id: str`, `baseline: DeploymentIdentity`, `payload: DeploymentIdentity`, `payload_digest: str`, `start_deadline: int`, `phase: JournalState`, and optional terminal `receipt: str | None`. Define `HostJournalError`, `OperationConflict`, `ReconciliationRequired`, and `UnsafeTransition` with fixed non-sensitive messages/codes.
- [x] **Step 4: Implement strict serialization and validation.** Encode only the exact versioned JSON schema; reject duplicate/unknown/missing fields, malformed JSON, invalid IDs/digests/timestamps/phases, and records whose payload digest differs from the canonical serialized payload identity. Decode deployment identities by constructing the existing policy dataclass so its field validation remains authoritative.
- [x] **Step 5: Implement atomic persistence.** Create the parent directory if needed; write a temp file in that directory with restrictive mode, flush and `fsync` the file, `os.replace` it over the journal record, and `fsync` the parent directory. Remove abandoned temp files on write errors without removing the last valid journal.
- [x] **Step 6: Verify the first durability test passes.** Re-run the focused test; expected result is one pass and a reopened journal with exact identity values.

## Task 2: Serialize admission and preserve same-operation receipts

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/host_journal.py`
- Test: `packages/plugin-release-control/tests/test_host_journal.py`

- [x] **Step 1: Add a failing test for same-request idempotency and changed-request conflict.** Repeating `prepare` with the same operation ID and byte-equivalent identities/deadline returns the existing record; changing its payload/deadline under that ID raises `OperationConflict`.
- [x] **Step 2: Add a failing test for a different operation while any journal record exists.** Preparing a second operation while the prior operation is active or terminal-but-unarchived raises `OperationConflict`; the original record remains unchanged.
- [x] **Step 3: Add a failing concurrency test using two journal instances for the same path.** Use a barrier to start two different `prepare` calls concurrently; exactly one succeeds and the other sees `OperationConflict`, with a valid record on disk after both finish.
- [x] **Step 4: Implement cross-process advisory locking.** Lock a stable sibling `.lock` file with `fcntl.flock(LOCK_EX)` for every read-modify-write; reload the journal while holding the lock so separate instances/processes cannot act on stale in-memory state. Release in `finally`.
- [x] **Step 5: Implement prepare idempotency.** Allow exact retries of the current operation only. A new operation remains blocked until the terminal operation is explicitly archived through Task 6.
- [x] **Step 6: Run the idempotency, conflict, and competing-operation tests.** Expected result: all pass, with no sleeps or reliance on scheduler timing.

## Task 3: Enforce phase transitions and start deadlines

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/host_journal.py`
- Test: `packages/plugin-release-control/tests/test_host_journal.py`

- [x] **Step 1: Add failing tests for allowed and forbidden phase transitions.** Exercise `PREPARED -> SWITCHING -> CHECKING -> HEALTHY`, `CHECKING -> RESTORING -> RESTORED_HEALTHY`, and `RESTORING -> RECOVERY_FAILED`; reject skipping phases, terminal reopening, and any mismatched operation ID.
- [x] **Step 2: Add failing boundary tests for the start deadline.** `begin_switch(operation_id, now=deadline-1)` moves to `SWITCHING`; `now == deadline` and `now > deadline` both raise `UnsafeTransition` and leave the durable phase `PREPARED`.
- [x] **Step 3: Implement `begin_switch` and the private transition helper.** Validate each requested edge with `validate_journal_transition`; require the exact operation ID; disallow all writes after terminal state; enforce the deadline before the only `PREPARED -> SWITCHING` edge; persist each accepted transition atomically. Keep phase changes that depend on observed host state inside `reconcile()` so callers cannot skip identity checks through a generic transition method.
- [x] **Step 4: Verify phase and deadline tests pass.** Reopen the file after each rejected transition and confirm the persisted phase did not change.

## Task 4: Reconcile actual state and fence configuration generations

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/host_journal.py`
- Test: `packages/plugin-release-control/tests/test_host_journal.py`

- [x] **Step 1: Add a failing test that unknown actual state requires explicit reconciliation.** Call reconciliation with `None` for actual identity, or omit a required health observation, and assert `ReconciliationRequired`; assert no phase or receipt was written.
- [x] **Step 2: Add failing tests for crash-after-switch observations.** For a `SWITCHING` record, observing the payload identity advances only to `CHECKING` and requests a health check; observing baseline while no switch is confirmed requests reconciliation and never reports healthy. Health success for payload can produce `HEALTHY`; health failure moves to `RESTORING` only after an exact payload identity observation.
- [x] **Step 3: Add failing tests for configuration-generation races.** A payload with a newer configuration generation observed during recovery cannot be overwritten. Any identity other than the journaled payload blocks restoration and persists `RECOVERY_FAILED`; a matching payload identity may enter `RESTORING`.
- [x] **Step 4: Add failing tests for restored-health truthfulness and terminal retry.** From `RESTORING`, only observing the exact baseline identity plus explicit healthy result creates `RESTORED_HEALTHY`; unhealthy/unknown baseline remains non-terminal or becomes `RECOVERY_FAILED`. Retrying a terminal operation returns the exact stored receipt and never mutates phase/identity.
- [x] **Step 5: Implement explicit reconciliation outcomes.** Add a small enum/dataclass result describing `switch_allowed`, `switch_not_started_expired`, `verify_payload`, `restore_allowed`, or `already_terminal`; require the caller to provide an observed `DeploymentIdentity` and a health result only for a matching observed target. Compare the full identity and configuration generation before any restore transition. Unknown state always raises `ReconciliationRequired`.
- [x] **Step 6: Keep receipts deterministic and operation-bound.** Generate a bounded receipt from the operation ID and terminal phase only after the corresponding observed state and health result are verified. Terminal operations accept same-operation reads/retries only.
- [x] **Step 7: Run the entire focused journal test module.** Expected result: all persistence, competition, terminal-retry, unknown-state, deadline, crash-recovery, and generation-race tests pass.

## Task 5: Self-review, Ruff, and scope confirmation

**Files:**
- Review: the three P08-owned paths and the approved design sections on admission, recovery, and SI-06/SI-08/SI-10.

- [x] **Step 1: Run focused tests and Ruff.** Run `uv run pytest packages/plugin-release-control/tests/test_host_journal.py -q` and `uv run ruff check packages/plugin-release-control/src/chatcore_release_control/host_journal.py packages/plugin-release-control/tests/test_host_journal.py`; expected result is all tests passing and Ruff reporting no issues.
- [x] **Step 2: Review the actual diff against this plan and the P08 acceptance list.** Confirm only the three owned paths changed from this part; inspect persistence ordering, lock lifetime, strict decoding, idempotency, deadlines, terminal immutability, unknown-state behavior, and config-generation fences. Record `PASS`, `CHANGES_REQUIRED`, or `BLOCKED` in the delivery report and repair/review again if needed.
- [x] **Step 3: Report remaining integration and proof work.** State explicitly that host deployment service admission/broker wiring, fixed build and deploy, health/smoke orchestration, production host installation, and live crash/resource/service proof remain unimplemented or unverified by this local journal foundation.

## Task 6: Archive terminal receipts and advance sequential operations

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/host_journal.py`
- Test: `packages/plugin-release-control/tests/test_host_journal.py`

- [x] **Step 1: Add a failing sequential-operation test before implementing the API.** Added the `release-7` terminal archive then `release-8` prepare scenario, including historical lookup and exact old `prepare` retry. It failed because `archive_terminal` was absent.
- [x] **Step 2: Add failing tests for archive fencing and crash-safe retry.** Wrong operation ID and wrong receipt are rejected. An injected interruption after receipt creation but before current-state clearing leaves both copies; retry verifies equality and completes clearing.
- [x] **Step 2a: Add failing tests for safe historical lookup and path validation.** Added before the corresponding fix: archived operation retries through `reconcile`, `begin_switch`, and `get_record` must return the terminal receipt while a newer operation is current; malformed IDs such as `../outside` must fail before path construction.
- [x] **Step 3: Implement immutable receipt lookup.** Stores terminal records in `<journal>.receipts/` under validated operation IDs. Rejects symlink/non-regular files and malformed or nonterminal archived records. `get_record` looks in current state then receipt archive.
- [x] **Step 4: Implement `archive_terminal`.** Under the same OS lock, requires exact current operation ID and receipt, creates/fsyncs an immutable receipt before removing/fsyncing current state, and verifies exact equality on retry without overwriting a different receipt.
- [x] **Step 5: Allow next operation only after archive.** `prepare` creates the next current record only after archival; exact old retries return archived receipts even when a newer operation is active. Changed inputs and archive conflicts fail closed.
- [x] **Step 6: Re-run sequential and focused journal tests plus Ruff.** All 20 tests pass, including archive-before-clear interruption, sequential operations, historical receipt lookup, old-operation retry, invalid operation IDs, and all prior state/deadline/recovery cases. Ruff passes after the final code changes.

## Known boundaries

This part does not authorize deployment, admission policy integration, broker or Postgres changes, source fetching, image building, Compose/Docker operations, health checks against a service, production installation, or live host proof. Local tests establish only the journal's filesystem and state-machine behavior with synthetic identities.

## Task 7: Bound recovery by a persisted per-operation deadline

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/host_journal.py`
- Test: `packages/plugin-release-control/tests/test_host_journal.py`

- [x] **Step 1: Add failing tests before changing the journal.** Added tests for reopen under a different constructor max, expired `RESTORING`, terminal receipt read after expiry, and receipt tampering; they failed before the recovery deadline fields/API existed.
- [x] **Step 2: Add persistence and receipt-binding assertions.** Reopened records preserve the deadline; the archived receipt includes it; changing only the archived deadline causes strict receipt validation failure.
- [x] **Step 3: Persist and validate `recovery_deadline`.** New deadlines are bounded by the configured maximum and later than the start deadline. The stored value is serialized and included in terminal receipt identity. Existing records are never reinterpreted against a reopened instance's max window.
- [x] **Step 4: Enforce expiry without authorizing host effects.** `begin_switch` checks both stored deadlines. Expired nonterminal `reconcile` returns `reconciliation_required` with the observation and no state mutation or restore authorization. Terminal records still report the immutable receipt and optional latest observation.
- [x] **Step 5: Run P08 focused tests and Ruff.** Ran focused pytest (24 passed) and Ruff (all checks passed).

## Task 8: Implement the protected host service source

**Status:** Local source implemented; production installation and isolation proofs remain
pending. Tests use fake adapters and temporary Git repositories only.

**Files:**
- Create: `packages/plugin-release-control/src/chatcore_release_control/host_service.py`
- Create: `packages/plugin-release-control/src/chatcore_release_control/host_service_http.py`
- Create: `packages/plugin-release-control/tests/test_host_service.py`
- Create: `packages/plugin-release-control/tests/test_host_service_http.py`
- Create: `services/release-control/src/chatcore_release_control/templates/p08-app.Dockerfile.template`
- Create: `services/release-control/src/chatcore_release_control/templates/p08-entrypoint.sh.template`
- Create: `services/release-control/src/chatcore_release_control/templates/p08-smoke.py.template`
- Create: `services/release-control/src/chatcore_release_control/templates/p08-build-policy.json.template`
- Create: `services/release-control/src/chatcore_release_control/templates/p08-runtime-compose.yml.template`
- Create: `services/release-control/src/chatcore_release_control/templates/p08-runtime-config.json.template`
- Create: `services/release-control/src/chatcore_release_control/templates/p08-host-service.json.template`
- Create: `services/release-control/src/chatcore_release_control/templates/p08-host-service.service.template`
- Modify: `packages/plugin-release-control/src/chatcore_release_control/host_journal.py`
- Modify: `packages/plugin-release-control/tests/test_host_journal.py`
- Modify: this plan.

**Implementation sequence and local acceptance:**

1. Add failing fake-adapter service tests before implementation. Cover journal-before-effect,
   broker denial, policy/D→M evidence failure, unknown actual state, full configuration
   generation/digest races, health/smoke failure, bounded restore admission, terminal retry,
   sequential operation advancement, and restart reconciliation from `switching`, `checking`
   and `restoring` observations.
2. Add a strict root-owned service configuration loader and executable
   `python -m chatcore_release_control.host_service_http --config
   /etc/chatcore-release-control/host-service.json` entrypoint. Missing, malformed,
   group/world-readable, or incomplete installed inputs fail before the UDS is bound.
   The UDS is mode `0600` in an owner-only directory. The local relay input routes accept
   IDs only: `POST /v1/deploy` and `POST /v1/reconcile`, each with exactly
   `{approval_id, operation_id}`. `GET /v1/host/targets/{fixed_target}/current-state`
   returns exactly `{deployed_identity, healthy, policy_digest, target_sequence}` from two
   matching Docker/config observations around fixed health and smoke checks. It does not
   acquire the target effect lock: the broker calls it while deployment retains that lock,
   so reacquiring would deadlock. This path is read-only and never authorizes host effects.
3. Implement `BrokerHostClient` against the protected host-auth UDS routes:
   `GET /v1/host/releases/{approval_uuid}/deployment-context`; `POST` to `prepare`,
   `switch-admission`, `switch-started`, `restore-started`, and `receipt`. It uses a distinct
   owner/mode-checked host credential, strict schemas, bounded responses and timeouts, and
   never accepts a caller-selected host, image, command, environment, URL, or path.
4. Implement `GitExactCommitSource` with the configured HTTPS GitHub URL only, exact commit
   IDs (D, B, H and M as needed), complete rather than shallow history, a hard fetch timeout
   and repository byte ceiling, bounded output, a mode-0600 temporary askpass token file, no
   shell, no inherited environment, and redirects disabled. Remove the temporary token file
   on every outcome. The source credential path must differ from the broker host credential
   path.
   It creates a disposable bare object store and never checks out candidate scripts to run
   on the host.
5. Implement `P01HostEvidence` using actual Git D→M evidence and the installed
   `PolicyManifest`. It requires D ancestry, exact D/M, the installed policy digest, and
   protected-history/path validation. Since this initial host policy has no independently
   authenticated migration manifest, it conservatively denies any change under installed
   migration path prefixes or migration/versions path components. It cannot grant a
   migration exception based on candidate content.
6. Implement `FixedOfflineImageBuilder` using P02 `build_baseline_bundle`, an installed
   fixed Dockerfile and helper scripts, a digest-pinned operator dependency cache, an
   inspected named local BuildKit builder and image, `network=none`, `--pull=false`, fixed
   BuildKit cgroup parent, no mounts/secrets, bounded timeout/output, and labels for exact
   M/policy/manifest digest. The service verifies the
   resulting image ID and labels before constructing payload `DeploymentIdentity`.
   Candidate Dockerfile, Compose, hooks and helpers are never selected or run on the host.
7. Implement `FixedComposeRuntime` against the installed fixed Compose file only. It checks
   the exact local image ID plus M/policy labels, runs `up --detach --no-deps sb-bot`, and
   observes the running container image ID and full identity label. It does not start or
   restart databases or unrelated services. A fixed root-owned runtime config manifest
   binds generation and a canonical digest of an exact installed list of runtime config
   files; the container label, manifest digest and P05 full identity must agree. The
   Telegram token is mounted as an operator secret and read by the fixed entrypoint; it is
   not placed in build inputs, host command arguments, or Compose interpolation variables.
8. Under the target OS lock, fetch context, observe full baseline/config, verify/build exact
   M, re-observe baseline/config, prepare with broker, obtain one switch admission, durably
   prepare and begin the journal, report `switch-started`, re-read full state immediately
   before effect, and then switch. Require both fixed health and smoke checks for success.
   Recovery calls broker `restore-started` only for this same admitted operation, observes
   exact payload/config before restore, and requires baseline full identity plus both checks
   before `restored_healthy`. A new config generation/digest or unknown observation prevents
   restore and leaves explicit reconciliation required.
9. Persist the exact integer terminal observation timestamp in versioned journal v2 and
   bind it into the immutable receipt. The broker HTTP adapter reuses that timestamp on
   terminal retries after process restart, avoiding a changed P05 receipt digest. Version-1
   journal data is rejected as corrupt rather than assigned an invented terminal time.
   Archive only after broker receipt acceptance, then allow the next sequential operation.
10. Run focused P08 journal/service/HTTP tests and Ruff. Review every host command for fixed
    argv, environment, paths, timeout/output bounds, and absence of candidate-selected
    effects. No Docker, Compose, GitHub network, broker socket, production service, or secret
    is used by local tests.

**Local source boundaries still requiring an operator integration pass:**

- `p08-host-service.json.template`, `p08-build-policy.json.template`, service resource
  limits, migration prefixes, runtime config manifest and digest fields contain explicit
  operator-review placeholders. The service fails closed until real root-owned files are
  installed with exact measured values and canonical config digest matching P05 target D.
- The builder requires an exact root-owned resource manifest, an explicit default Docker
  context, a fixed named `docker-container` BuildKit builder, a digest-pinned image, and live
  inspection of its CPU quota, memory, memory-swap, PID, cgroup parent, network, tmpfs
  scratch settings and absence of host mounts. The build selects that same builder/context.
  Templates remain invalid until the operator pins and provisions the builder and measures
  capacity. Docker daemon layer storage and kernel enforcement are outside source
  archive/cache accounting; actual resource and no-egress behavior still require disposable
  Linux host proof.
- The fixed Compose/runtime template contains no Bot API, GitHub, broker, release or ledger
  environment values. Telegram API token delivery uses the explicit secret mount and fixed
  entrypoint. Other provider/runtime integrations still need an operator-reviewed allowlist;
  no production environment file was read or copied here.
- The P01 migration rule is a conservative path-prefix denial, not a complete database
  migration-manifest equality proof. Add a separately reviewed immutable migration manifest
  and loader/path coverage before permitting any release that changes migration assets.
- No live GitHub fetch, broker HTTP/UDS call, build, image inspection, Compose operation,
  health/smoke check, configuration race, crash recovery, service install, or Linux host
  proof was run. Fake tests establish command construction/state handling only.

**P08 service retry contract:** `POST /v1/deploy` begins a deployment and is also the
idempotent retry entrypoint for that exact `{approval_id, operation_id}`. For an archived
terminal operation it resends the persisted exact receipt without rebuilding or switching.
A durable `prepared` operation can resume only under its original admission/deadlines. An
operation already in `switching`, `checking`, or `restoring` is reconciled against fresh
actual state and never repeats the switch. `POST /v1/reconcile` is recovery/status only and
fails closed if no journaled operation exists. The broker deployment-context route returns
the immutable card-bound baseline even after target advancement; the separate current-state
route reports live host state. Host routes are listed in Task 8 step 2/3 above.

## Execution record

- The first durability test was run before implementation and failed on the missing journal module as expected.
- The focused module now passes: `uv run pytest packages/plugin-release-control/tests/test_host_journal.py -q` (24 passed).
- Ruff passes: `uv run ruff check packages/plugin-release-control/src/chatcore_release_control/host_journal.py packages/plugin-release-control/tests/test_host_journal.py` (all checks passed).
- Self-review result: **PASS** for local P08 journal behavior. The journal uses locked atomic replacements, supports sequential operations through exact terminal archival without losing old receipts, bounds switch and recovery deadlines in each persisted operation, requires observed identities and health evidence for recovery transitions, and blocks restore authorization after expiry or when actual identity/configuration generation differs from the recorded payload.
- Remaining P08 work: host service admission and broker/ledger wiring, source fetch and fixed build/deploy policy, real health/smoke orchestration, production service installation, and live crash, resource, and service proof. This local journal does not establish those controls.

Independent review repair P08-R1: constructor configuration could exceed the installed
120-second start-window bound. An oversized-window fixture reproduced acceptance;
construction now rejects values above MAX_START_WINDOW_SECONDS. Existing records still
retain their persisted deadlines, and completed receipts remain readable. Updated P08
focused count is 25. Targeted independent re-review: PASS.

P08 service implementation execution record: added the protected host controller,
owner-only UDS API, broker host-auth client, fixed exact-commit Git source adapter,
independent P01 evidence check, offline fixed-recipe builder, fixed Compose runtime,
runtime config-manifest digest binding, root-owned strict service loader, and inactive
service/build/runtime templates. Journal v2 persists terminal observation time so broker
receipt retries preserve their exact digest. Added restart and retry coverage for prepared,
switching, checking and restoring, terminal receipt replay, sequential operations, unknown
state, configuration races, and recovery failure. Focused verification: `uv run pytest
packages/plugin-release-control/tests/test_host_service.py
packages/plugin-release-control/tests/test_host_service_http.py
packages/plugin-release-control/tests/test_host_journal.py -q` (55 passed); scoped Ruff
format/check passed. `/v1/deploy` starts and idempotently retries an operation; `/v1/reconcile`
only reconciles an existing journaled operation. The broker confirmed its deployment-context
baseline is immutable card-bound D, while current-state is a separate live observation.

P08 local source review: **PASS** for the implemented interfaces and fake/local evidence.
This is not deployment authorization or a production-readiness claim. Remaining source/config
integration: operator must replace and review every template placeholder, root-own the exact
policy/config/cache/credential/runtime files, provision the host-private UDS relationship and
separate host/source credentials, and validate fixed resource/network settings. Remaining
operational proof: build and inspect the immutable candidate image, prove actual BuildKit
resource and no-egress enforcement, validate the chosen Compose/network/secret semantics,
install the systemd service, and exercise switch, health/smoke, crash recovery, restore and
configuration-generation races on an isolated disposable Linux host. None was run here.

## Task 9: Repair independent host-service review findings

**Status:** Source corrections implemented; see execution record below. Tests use local
temporary Git repositories and fake command output. Host OS, BuildKit and production proof
remain explicitly pending.

1. Replace shallow Git fetch with complete-object fetch semantics. Source scratch storage,
   total timeout, output and object-directory limits bound the host fetch. Do not bypass
   `git_evidence` shallow-repository rejection.
2. Expand immutable broker host context with card-bound `base_sha`, `head_sha`, `head_tree`,
   expected and recorded merge trees, and recorded merge parents. Independently verify actual
   M raw commit parents exactly `(B,H)`, M tree exactly expected/recorded tree, H tree,
   D→B/D→H ancestry, and P01 D→M policy/path/history evidence before build.
3. Add an explicit installed maintenance-reviewed source-script exception list to the fixed
   build config. Pass only the exact reviewed `apps/sb_bot/deploy/backup.sh` exception to
   `build_baseline_bundle`; validate it as sorted, unique, relative `.py`/`.sh` files and keep
   it separate from release-modifiable `allowed_paths`. Private environment/config files
   remain forbidden.
4. Select only a fixed named local BuildKit builder, explicit `default` Docker context, and
   expected `docker-container` driver/image. Inspect the live builder/container and compare
   CPU quota, memory and swap, PID, cgroup parent, network mode, immutable image digest, and
   bounded BuildKit storage configuration to the installed root-owned resource manifest.
   Reject an implicit/default builder, arbitrary context, missing fields, host mounts, or any
   mismatch before building. Build invocation must select the same explicit builder/context.
5. Require the complete runtime config inventory to include and hash the exact fixed Compose
   file, runtime environment/limits file, and every mounted runtime secret/config file.
   Re-read and verify the same manifest digest immediately before every effect.
6. Remove source credential material from child environment. Write a short-lived mode-0600
   token file for a fixed askpass script; disable Git HTTP redirects; keep the source URL fixed
   to the configured GitHub repository; erase/remove temporary credential artifacts on all
   outcomes.
7. Break the target lock inversion: broker admission reads actual host state while the host
   request retains the effect lock. Serve the observation through a read-only path that does
   not reacquire that lock; require two identical full identities around real health and
   smoke checks. Add nested ASGI callback coverage while deploy owns the lock.

**Verification boundary:** tests use temporary Git repositories and fake command output only.
They do not perform GitHub fetches, inspect or run Docker/BuildKit, or prove Linux limits.
Actual backend creation/inspection, network isolation, resource/capacity behavior, image
reproducibility, runtime config races and disposable-host switch/recovery proof remain pending.

Task 9 execution: added bounded complete Git fetches with askpass token files and redirects
disabled; exact B/H/M tree, parent and ancestry checks; the reviewed backup-script exception;
live named-builder capability inspection and explicit context selection; complete runtime
config inventory checks including Compose; and read-only lock-independent actual-state
observation. Added tests for merge parent/tree mismatch, builder memory-swap mismatch,
Compose edits, token absence from child env, fetch flags, and an ASGI callback during deploy.
Focused verification after repairs: 58 P08 journal/service/HTTP tests passed; scoped Ruff and
format checks passed. No in-process `BrokerApplication` integration or actual host adapter
was exercised here; full cross-service ASGI coverage and disposable Linux proofs remain
needed before operational use.

## Task 10: Keep Bot API credentials inside the protected Telegram bridge

**Status:** Local source and template repair implemented; no deployment performed.

- [x] Replace the ordinary service Compose template with the exact JSON Compose definition
  accepted by `FixedComposeRuntime`. Mount only the ordinary `app-runtime.env` and require
  explicit bridge transport and a generated credential file path. Do not mount a Bot API
  token, host socket, or release-control secret.
- [x] Extend `FixedComposeConfig` with the mandatory app runtime env path and require it in
  the complete sorted runtime config manifest inventory. Before any Docker command, parse
  and exact-match the installed Compose definition, then validate the ordinary env file
  against an explicit app-setting allowlist. Require an HTTPS bridge origin, bridge mode,
  and a bounded ordinary bridge credential. Reject direct Bot API, GitHub, Actions, broker,
  release-control, host-control, and ledger credential variables.
- [x] Make the app entrypoint reject control-authority variables, write only the bridge
  credential to a new mode-0600 file on the container's private tmpfs, unset the value, and
  pass the file path to the ordinary Telegram adapter.
- [x] Include app runtime env in both runtime manifest and host config template; replace the
  source read-token field with the protected source App config path.
- [x] Add tests for a valid bridge-only service, credential/config tampering, privileged
  Compose fields, Docker socket mounts, command overrides, and template/runtime contract.

Focused verification: `uv run pytest packages/plugin-release-control/tests/test_host_service.py
-q` passed (41 tests); Ruff and `git diff --check` passed. Tests use synthetic credentials and
fake Docker commands. The operator still must populate and protect the ordinary env file,
compute/review the runtime manifest, and validate the bridge configuration on an isolated
host. No real Bot API token, Docker daemon, production service, or Telegram network was used.
