# Release control implementation

Implementation is split into ten parts in
[`2026-10-31-self-improvement-parts.md`](../../docs/superpowers/plans/2026-10-31-self-improvement-parts.md).
That plan records dependencies and the distinction between executable local source,
verified local behavior, and pending disposable/live proof. This package is not wired
into production. Its service entrypoints and inactive workflow/host templates require
operator provisioning, disposable live proofs and owner bootstrap acceptance.

Current building blocks:

- `development.py` / `development_worker.py`: bounded generation/controller and
  fixed container-worker source with pinned image/manifest identity checks. The
  `baseline.py` and `monorepo_harness.py` bind exact Git source/archive inventory,
  offline dependencies and the fixed test command. Installed baseline extras or
  changed bytes fail closed. Reports remain untrusted observations; actual image
  preparation and disposable Linux isolation are still acceptance work.
- `policy.py`: immutable identities, strict exact-path policy, full deployed-delta
  eligibility, required-check matching, merge reconciliation and switch predicates.
- `git_evidence.py`: bounded read-only Git object inspection, actual D/B/H trees and
  ancestry/diffs, introduced-history path/mode checks, and installed blob-digest checks.
  Extraction never attests human review or scans source for secrets by itself.
- `oidc.py`: fixed-origin bounded JWKS fetching, real RS256 JWT signature/claim
  verification and mandatory independent workflow-closure/job-correlation checks.
  A verified caller is not owner approval or switch admission.
- `telegram_bridge.py`: authenticated ordinary-client operation core, owned-message
  mutation restrictions, protected-card controls and token-free file handles, with
  a concrete bounded Bot API transport. `bridge_service.py` mounts protected
  webhook/card/report routes and durable ordinary relay on a private socket;
  the app has explicit token-free adapter selection. Live cutover/rotation remain
  maintenance work.
- `telegram_ingress.py`: authenticated webhook routing consumes release callbacks
  and owner-only revocation commands before ordinary HMAC-authenticated relay;
  decisions and append-only revocations are committed before acknowledgement.
  HTTPS ingress installation and operational token revocation remain pending.
- `host_journal.py`: locked, fsynced atomic operation records and retained terminal
  receipts, observed-state reconciliation and protection against stale restoration.
  `host_service.py` and `host_service_http.py` add concrete fixed build/runtime
  adapters and private serving. Renewable source-read tokens stay in the protected
  service; strict Compose/config inventory permits only ordinary application secrets
  and bridge transport in the app. Real Docker/recovery proof remains pending.
- `ledger.py` and `schema.sql`: protected Postgres decision/outbox source. Schema
  installation is maintenance-only; it is not an ordinary bot startup migration.
  `database_runtime.py` checks separate restricted service logins and actual durable
  database settings. `revocation.py` serializes owner revocation with admission.
- `publication.py` / `github_app.py`: immutable quarantine/object scanning gate,
  durable local provenance, actual fixed-origin GitHub push/PR transport and App
  token lifecycle. `candidate_bundle.py` and `publication_service.py` connect
  isolated candidate output to immutable Git objects and CI context/report storage.
  `generation_service.py` provides the protected request CLI and `baseline_audit.py`
  scans initial full history. Durable private submissions preserve the exact bundle,
  prose and bounded report so retries resume without invoking the model again.
  Actual App permissions, scanner identity and remote race behavior remain unverified.
- `broker.py`, `broker_http.py`, `broker_worker.py` and `worker_service.py`: durable
  merge/switch admissions, renewable distinct Broker App, signed job authentication,
  intent-before-dispatch, exact dispatch-UUID/run correlation, retained run history,
  increasing deployment sequences and typed status reporting. Already admitted
  work retains its bounded retry/recovery authority after revocation.
- `hosted_bootstrap.py`, `git_fetch.py`, `github_evidence.py` and `release_coordinator.py`:
  pinned fresh-runner bootstrap, complete isolated object fetch, trusted check/job
  evidence and immutable merge coordination. One inactive workflow covers hosted
  merge and isolated relay under the same production concurrency group.
- `runner_supervisor.py` and `runner_bootstrap.py`: disposable whole-runner lifecycle,
  pinned registration protocol and installed isolation capability checks.
  `runner_relay.py` is the standalone bounded OIDC relay installed into that image.
  Actual Linux/GitHub isolation and cleanup proof remain pending.
- `watchdog.py`: independent read-only service/health/journal/resource observations
  with durable changed-state alerts through the protected bridge; no auto-repair.

The exact scope and verification are recorded in the numbered plans and the
private deployment SI evidence index. Generic templates ship in
`src/chatcore_release_control/templates/`; they are examples, not installed policy. Synthetic integration
covers generation through owner approval and broker↔host deployment, lost replies
and retained receipt replay. Sandbox compatibility, real App permissions, Telegram
ownership/rotation, actual fixed host build/deploy, failure-injected live rehearsals
and production acceptance require their corresponding live proofs.
Do not treat tests of injected transports as live permission or isolation evidence.

```sh
uv run pytest services/release-control/tests -q
uv run ruff check services/release-control
```

Ledger tests create and stop their own disposable local Postgres process when the
required binaries are available, with fsync/WAL durability enabled. They never use a
production or inherited `PG_DSN`. This is not a power-loss durability proof.
Other tests use temporary files/Git repositories and synthetic keys/transports.
