# P03 immutable publication gateway and GitHub App plan

## Generation service wiring (main)

Add a protected CLI runtime which reads only bounded explicit request/spec/examples
and title/body from its request file. Installed root-owned configuration chooses the
LLM/model/key, P02 immutable image/manifest/resources, scanner, fixed publication
config, target and host socket. Construct the actual existing Provider and
DevelopmentController, verify fresh host D and GitHub main B against the installed
baseline image, then invoke generate_and_publish. Caller cannot choose commits,
policy, Docker command/image, provider endpoint or credentials. Retain only typed
PR/H/request receipt output, no raw reports/model source. Test config isolation and
the actual generation entrypoint with fake external adapters. No live generation,
Docker, GitHub publication or production changes.

## Baseline audit preparation (main)

Provide a real baseline-history receipt tool, rather than requiring a manually asserted
`history_complete`. Enumerate every ancestor commit and tree from the exact baseline
using isolated P01 Git plumbing. Reject private paths/nonregular modes before any
blob read, then scan all commit metadata, paths and unique blobs with the pinned
scanner under explicit bounds. Reviewed backup-source scripts may be explicit
maintenance exceptions; INI/env/credential data cannot be excepted. Emit the canonical
existing P03 receipt only after complete clean scanning. Tests use synthetic Git and
fake scanner; do not audit actual private config or execute candidate code.

## Scope and intent
Build a local, fail-closed publication core plus a GitHub App installation-token issuer. The publication core consumes a bounded Git bundle, ingests it into a gateway-owned temporary bare quarantine, freezes full D/B/H object IDs, applies P01 policy to all introduced history, scans commit metadata/blob bytes and all required text surfaces through a mandatory trusted scanner callback, records provenance before publication, then calls a mandatory injected exact-ref transport. Tests use only temporary repositories and fake transports. The App issuer uses a protected key provider and injected HTTP callback; it never makes a real request here.

## Ordered work
1. **Write bounded models and failures** — define immutable publication request/surface/artifact, scanner input/verdict/identity, provenance and receipt types. Require scanner, provenance store and transport dependencies explicitly; no permissive defaults. Validate request IDs, canonical full object IDs, manifest, bytes/surface names and size ceilings.
2. **Create isolated quarantine** — initialize gateway-owned bare object store with fixed configuration/environment, disabled hooks/replacements/helpers, bounded runtime/output, no checkout. Write bounded incoming bundle bytes to a private temporary file, verify/unbundle into quarantine, then resolve the supplied full D/B/H commit IDs and freeze H.
3. **Validate and scan introduced Git history** — use P01 collector/policy to enforce exact paths and D/B/H deltas. Enumerate every commit newly reachable from H over D, compare against every parent (or root), reject private/protected/unallowlisted paths, non-regular modes, malformed metadata, and copied blobs whose object IDs occur at private/protected baseline paths. Scan each introduced commit object's full metadata and each changed blob object's bytes once. Reject non-UTF-8/NUL/binary/uninspectable data and scanner findings/errors. Require completed baseline-history scan identity before candidate scanning. Bound per-item and aggregate bytes and wall time.
4. **Scan non-Git surfaces** — require specification, PR title and body; include every supplied artifact. Pass each UTF-8 text surface as bytes to the scanner under a stable surface name and size limit. Any omission, duplicate, invalid encoding, oversize surface or redaction/incomplete result blocks.
5. **Freeze and publish** — derive only `refs/heads/clawdot/<request-id>`; persist a provenance record containing full frozen IDs, policy/scanner digests and scan summary before calling transport. The transport receives the quarantine object store path, frozen H and one explicit `<H>:<destination>` refspec. Its response must echo the exact commit/ref. Retry semantics are idempotent only at the same H/ref; different existing content fails. No configured remote, worker-selected ref, force/mirror/tags or actual network in this task.
6. **Implement GitHub App issuer** — sign short-lived RS256 App JWTs on demand via a protected-key provider; request a repository-selected installation token with fixed Contents/PR and Metadata permissions through injected HTTP transport. Validate repository/installation IDs, expiry and token length. Renew cached token before expiry. Tokens/private keys/JWTs never appear in repr or exception text.
7. **Test, review, verify** — use temporary Git bundle + bare remote fixtures, fake scanner/store/transport and fake HTTP. Exercise intermediate secrets/path/mode, config/hooks/replacement isolation, ref substitution/duplicate behavior, failures/limits, helper/policy enforcement, token scope/expiry/renewal and sanitized errors. Run focused pytest/Ruff and review final diff.

## Files owned
- `packages/plugin-release-control/src/chatcore_release_control/publication.py`
- `packages/plugin-release-control/src/chatcore_release_control/github_app.py`
- `packages/plugin-release-control/tests/test_publication.py`
- `packages/plugin-release-control/tests/test_github_app.py`
- This plan.

## Acceptance boundaries
- No source checkout or execution, no arbitrary Git config/remotes/helpers/hooks, no real GitHub/API/network call, no real private key or private INI content.
- Bundle, object graph, output, each scan surface, total scanned bytes and runtime have explicit bounds; every ambiguity, missing scanner, scanner failure or non-clean result denies.
- Candidate source cannot be redacted into a passing release. Review readiness is distinct from mechanically complete Git evidence.
- Exact immutable H is the only object the required transport may publish; the request contains no caller-selected remote/ref.
- App installation tokens are repository-selected and least-privilege, short-lived, renew-before-expiry and represented without secret leakage.
- Binary scanner/config and baseline-history scan identity are explicit injected trusted facts. Local tests do not prove their real installed identity or completeness; operator bootstrap evidence remains required.

## Implementation and verification record

- Implemented the bounded quarantine/scanning service, a mandatory scanner interface and binary adapter, and the mandatory provenance/transport dependencies in `publication.py`.
- Added an append-only SQLite provenance implementation with WAL/FULL durability, immutable request-ID binding, conflict rejection, concurrent-insert coverage, and reopen coverage.
- Added a canonical scan-surface manifest digest to provenance and receipts. It hashes sorted `(surface name, raw content SHA-256)` entries for every clean-scanned surface, including commit metadata, paths, blobs, specification, PR text, and artifacts; the immutable request-ID record therefore rejects changed content on a retry.
- Added `FixedBareGitTransport`, which is restricted to one configured local bare repository and atomically creates only the exact frozen commit ref. The temporary bare remote in tests proves this local path; it is not a GitHub transport and performs no network calls.
- Implemented the RS256 GitHub App token issuer with exact permissions, repository binding, renewal, and sanitized token/key handling in `github_app.py`. Its HTTP callback remains injected and tests use only a fake callback.
- Root commits that enter through a merge are scanned by their actual `_RawChange` fields; a temporary unrelated-root merge test proves the root commit metadata and allowed root blob reach the scanner.
- Focused verification after review repairs: Ruff passed, the focused publication/App suite passed 30 tests, and `git diff --check` passed.

## Remaining live evidence and integration

- No production GitHub publication transport or real GitHub API call is included. A separately reviewed transport must bind the configured repository and create the same exact ref without changing the frozen commit; the local bare implementation is a reference path only.
- No live GitHub App, installation, protected private key, scanner binary/configuration, or baseline-history scan was exercised. Operator bootstrap must supply and independently verify these facts; injected `baseline_history_scan_complete=True` records that trusted assertion but does not establish completeness itself.
- The publication service does not establish human review. P01's `complete_delta_reviewed` stays false; a separate human review/approval workflow must remain an independent gate before any release action.
- Production wiring must bind the fixed repository identity, policy manifest, installed scanner identity, baseline scan evidence, durable store location, and authorized publication transport. Local fixtures do not prove those deployment bindings.

## Concrete candidate-to-GitHub gateway increment

This follow-up replaces the local-transport-only gap with executable source and
testable local contracts. It does not use the live GitHub repository.

### Owned files and boundaries

- `packages/plugin-release-control/src/chatcore_release_control/candidate_bundle.py`
  accepts an immutable `DevelopmentResult` plus trusted request metadata, reads
  exact D/B commit objects from the configured source repository, creates H by
  raw Git plumbing in a private temporary bare gitdir, and emits a full bundle
  and `PublicationRequest`. It has no checkout, candidate subprocess, config
  mutation, filters, hooks, commit identity selector, or source-tree writes.
- `packages/plugin-release-control/src/chatcore_release_control/publication_github.py`
  provides bounded fixed-origin `httpx` App-token issuance transport, numeric
  owner/repository verification, create-only Git smart-HTTPS push through a
  protected 0600 token file and fixed 0700 askpass helper, exact pre/post ref
  checks, and idempotent exact PR lookup/create reconciliation.
- `packages/plugin-release-control/src/chatcore_release_control/publication_service.py`
  composes candidate bundle creation, mandatory scan/provenance publication,
  push receipt and PR creation; also owns append-only SQLite publication/PR/CI
  context/report records and a bounded ASGI app for the fixed P04 CI routes.
  A CLI loads only digest-pinned protected configuration and starts Uvicorn on
  a private Unix socket with restrictive mode; no runtime factory is supplied
  by callers or event data.
- `tests/test_candidate_bundle.py`, `tests/test_publication_github.py`, and
  `tests/test_publication_service.py` use temporary Git repositories,
  `httpx.MockTransport`, synthetic keys/tokens, fake scanner adapters and a
  local ASGI client. They must never read process secrets or invoke GitHub.
- Existing `publication.py`/`github_app.py` may receive narrowly required
  transport hooks, only after a behavior test demonstrates the need.
- Update this P03 plan with verified source/test results and separate live
  bootstrap proof requirements.

### Ordered red-green tasks

1. Test `CandidateBundleBuilder` using actual temporary Git objects: generated
   added/modified files produce exact deterministic H with parent B and fixed
   author/message/time; D/B/H are all reachable in the bundle; policy/path,
   duplicate, malformed and base/policy mismatch candidates fail. Assert the
   source worktree/index/config is unchanged and only private temporary Git
   objects are created. Then implement isolated raw plumbing.
2. Test GitHub App HTTP adapter with MockTransport for exact fixed origin/path,
   bounded response/body/deadline, repository and permission mismatch, token
   expiry and sanitized failures. Implement it as an injected `HttpPost`
   adapter compatible with the existing `GitHubAppTokenIssuer`.
3. Test `GitHubPublicationClient` against fixed repository IDs/owner IDs,
   exact absent/same/different remote ref state, and PR pagination over all
   states. Cover exact head/base/H identity, ambiguous records, lost create
   response reconciliation, no duplicate PR, and bounded 404/error handling.
   Implement only fixed `api.github.com` paths and canonical request schemas.
4. Test `GitHubGitPushTransport` using a local bare Git HTTP-compatible fixture
   where available and mocked API ref checks; verify exact refspec, no force,
   no tags/mirror, create-only semantics, token-file permissions and cleanup,
   sanitized askpass, transport output/time bounds, and same-H idempotency.
   Implement separate empty push gitdir with only validated object alternates.
5. Test orchestration requires clean publication/provenance before pushing and
   creates a PR only after an exact push receipt. Add durable SQLite insert-once
   publication/context/report state, reopen/conflict/concurrent request tests,
   and P04's exact context and report HTTP schemas. Reports are scanned and
   stored before immutable receipt; no raw report is returned to Actions.
6. Test the ASGI route adapter and protected UDS CLI config with mock auth and
   no real server or token. Implement a real Uvicorn Unix-domain socket entry
   point with no caller-supplied runtime factory, fixed private socket parent,
   mode 0600, bounded request/response/time, strict bearer token comparison,
   duplicate-free JSON, fixed route/methods, and generic safe errors.
7. Run the new focused tests, existing P03 publication/App tests and Ruff;
   inspect all owned paths and report which acceptance items still require
   disposable GitHub/App and gateway deployment proof. No live API/push/PR,
   secret reads, Docker, source execution, or task-repository commits.

### Local evidence distinction

Local tests can prove deterministic object construction, request schemas,
temporary local Git and DB semantics, token non-disclosure, and behavior under
mocked transport outcomes. They cannot prove GitHub permissions, remote race
behavior, App installation configuration, socket ownership in deployment,
scanner coverage, baseline scan completeness, or production service wiring.

### Concrete gateway implementation evidence (2026-10-31)

- Added `PublicationGatewayService.generate_and_publish`, which invokes the
  existing `DevelopmentController.generate_and_run`, builds a candidate from
  its immutable `candidate_files`, scans/persists provenance, pushes the exact
  frozen H ref, then creates or reconciles the PR and persists the P04 context.
- Added protected configuration loading via
  `RELEASE_CONTROL_PUBLICATION_CONFIG_PATH`, matching SHA256, and
  `RELEASE_CONTROL_PUBLICATION_MODE=gateway`; `checkconfig` and `serve` build
  the concrete App/GitHub/scanner/store/ASGI runtime. CI context/report routes
  are served on the configured private UDS. Deployment configuration and
  protected credentials were not supplied or read during this task.
- Local verification: 46 tests passed across candidate bundle, GitHub
  transport, gateway service, publication policy, and App issuer suites;
  Ruff check and format check passed; `git diff --check` passed.
- No live App token request, GitHub repository lookup, push, PR creation,
  private UDS deployment, worker execution, or scanner execution was performed.
  Those proofs remain required before enabling the inactive workflows.

### Publication transport review repairs (2026-10-31)

- Replaced direct publication to the final ref with a deterministic staging
  ref containing request ID and frozen H. The Git upload is non-force and
  transfers only the already-scanned H; the final branch is created through
  GitHub's create-reference API, which atomically rejects a concurrent creator.
  The gateway verifies final H and deletes only the exact staging ref. If
  cleanup fails, it returns the fixed `publication_cleanup_pending` code; the
  final ref may already exist, and an exact retry reconciles and retries cleanup.
- PR parsing now checks `merged_at is None`, matching GitHub's documented open
  pull-request payload rather than relying on a `merged` boolean absent from
  the documented response shape.
- Regressions cover a race where the final ref appears at B before final
  creation, preserving that ref, PR payloads without `merged`, and uncertain
  staging cleanup followed by exact retry. These use only fake GitHub/Git
  transports and synthetic objects.
- The production `serve` command still mounts only the P04 context/report ASGI
  routes. The source has no configured generation trigger that invokes
  `generate_and_publish`; root is implementing that runtime trigger separately.
  Until that wiring is installed and reviewed, the publication service cannot
  claim an end-to-end candidate-generation path.

## CI scan surfaces and hosted bootstrap gateway integration (2026-10-31)

- `PublicationGatewayService` now validates synthetic examples before generation
  effects, canonically serializes them into the reserved
  `synthetic-examples` scan surface, and includes those exact bytes in durable
  provenance and the immutable PR context. Caller-supplied artifacts cannot
  reuse reserved names. The generated development report is similarly bound
  as `development-report` before push/PR and appears in context only after a
  clean scan and provenance record. Tests cover scanner findings before
  publication/context and same-length changed examples conflicting on replay.
- The concrete P03 ASGI gateway serves only fixed authenticated bootstrap
  routes from protected source/manifest/archive files, validates the source
  against the installed module and the exact canonical inventory at startup,
  and rechecks hashes on response. Archive access is content-addressed; the
  HTTP request cannot select a local path.

## Durable submission restart reconciliation (2026-10-31)

- [x] Add immutable, bounded SQLite persistence for the complete raw
  `PublicationRequest`, synthetic examples, and generated report bytes before
  any push/PR effect. Keep this data private to the gateway; do not add a CI
  read route.
- [x] Add typed `get_submission(request_id)` and `resume_request(...)` APIs.
  Resume compares spec/title/body/examples byte-for-byte/canonically, then
  reruns inspection, scanning, frozen-H publication, and idempotent PR lookup
  without generation.
- [x] Add restart tests for lost branch/PR replies, exact replay, conflicting
  logical request, record/aggregate caps, reopen durability, and no duplicate
  PR; run focused tests/Ruff and record the remaining live proof.

### Restart reconciliation implementation notes

- `PublicationGatewayStore.store_submission(request, examples,
  development_report=...)` persists the raw request before external effects;
  `get_submission(request_id)` returns a private typed `PublicationSubmission`.
- Limits: 160 MiB per canonical record, 512 MiB aggregate submission payload,
  256 immutable submissions, and 1 GiB SQLite page-plus-WAL bound. There is no
  pruning; all limits fail closed. The submission table is private to the local
  gateway and has no ASGI read route.
- `PublicationGatewayService.resume_request(...)` compares specification,
  title, body and canonical examples before rescanning or transport. It reuses
  exact frozen bundle/H plus saved report/artifacts and repeats the idempotent
  push/PR reconciliation without calling the model.
- Verification: publication service tests and related focused suites passed;
  Ruff and format checks passed. No GitHub/network or real publication was
  attempted. Live process-kill/restart against GitHub remains an owner proof.
- Focused publication service tests use a local ASGI client and synthetic
  protected tar fixtures. These tests establish local route and content-binding
  behavior only; no real deployment artifact directory or gateway credential
  was read.
