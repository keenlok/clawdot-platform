# P04: Trusted hosted CI evidence and merge control

Dependencies: P01/P03/P05/P07. Own `github_evidence.py`, `merge_control.py`, corresponding tests and inactive workflow templates under `services/release-control/src/chatcore_release_control/templates/`.

1. Implement an authenticated bounded GitHub API client with fixed repository endpoints, pagination limits, explicit run/attempt correlation and safe structured errors. Never choose the latest run as identity.
2. Resolve required check creator App, repository/event, exact B/H/tested tree, workflow commit/installed closure, run and latest relevant attempt. Missing pages/jobs, unknown synthetic merge mapping, cancellation/skips/failure/API outage block release. Integrate existing required-check predicates.
3. Define trusted CI orchestration templates that fetch source without persisted credentials and run candidate tests in the P02 isolated environment with no Git/OIDC token or runner channels. Only trusted code validates/scans/uploads bounded reports.
4. Define a merge controller which never imports or executes candidate code. Obtain P07 verified caller and P05 admission under target lock, recheck current B/H/D/policy, then call merge with `sha=H` and explicit merge method.
5. Reconcile returned or uncertain M from GitHub immutable parents/tree; an unexpected or late merge is persisted as merged-blocked. Never remerge a closed PR, auto-revert or substitute current main for recorded M.
6. Test paginated provenance, later failed attempts, missing/skipped jobs, head/base movement, lost responses and late writes with fake API transports; use temp Git objects to substantiate merge tree/parent checks. Run focused evidence/merge tests and Ruff.

Templates stay inactive until the sandbox/permissions and installed control closure are verified. GitHub's merge API cannot atomically guard expected base; retain the disclosed already-merged-but-blocked residual. Actual Free/private API and workflow behavior require disposable-repository proof.

## Execution design before implementation

1. Build an `httpx.Client` wrapper pinned to `api.github.com`, numeric repository ID, owner/name and an injected transport. Disable environment proxies, redirects and cookies; authenticate only from a protected token provider; cap response bytes/pages and total wall time; emit fixed safe error codes.
2. Resolve PR metadata, exact B/H, target branch, open state, and GitHub's synthetic merge commit. Prove the synthetic commit has parents `(B,H)` and record its tree. Resolve check runs and workflow runs by exact check-suite/run correlation, never by choosing the newest unrelated run. Inspect the latest run attempt and all paginated jobs; only fully completed success results count.
3. Bind configured check name/App ID/workflow path/digest through existing `validate_all_required_checks`. Bind workflow commit to B (protected workflow paths cannot change in D..H), and require a protected callback to verify the installed execution-closure map at B. Return immutable evidence containing repository, PR, event, B/H/tree, synthetic merge mapping, run/attempt, check-run IDs, and validated checks.
4. Implement merge as an injected trusted boundary: validate the fixed repository/PR/current bundle/run evidence; invoke mandatory target-lock broker admission; GET and recheck open PR base/head and synthetic merge mapping; call only `PUT /pulls/{n}/merge` with exactly `sha=H, merge_method=merge`; reconcile any returned or uncertain merge through authenticated immutable commit GET. Persist only trusted evidence, classify mismatched/late merges as merged-blocked, and never reissue a merge to a closed PR.
5. Add temporary Git object fixtures for merge parent/tree proof, mocked HTTP pagination/run-attempt/jobs, lost response and late/mismatched merge cases. Create trusted workflow templates as `.yml.inactive` files and verify they are outside GitHub's active workflow discovery directory. Run focused tests/Ruff and review these owned files only.

## Implementation and local verification record

- Added `github_evidence.py` with a fixed `api.github.com` client, protected token callback, no redirects or environment proxies, bounded response size/deadline, fixed safe errors, strict JSON decoding and count-checked pagination.
- Added a resolver that binds PR repository/owner, target branch, event, exact B/H, verified synthetic merge commit parents/tree, required check creator App IDs, check-suite/workflow-run correlation, exact latest attempt details, and every job. Workflow closure verification is mandatory and bound to B; candidate merge tests are projected onto the verified synthetic merge tree before calling the existing `validate_check_evidence` predicate.
- Added `merge_control.py` with mandatory caller verification, current deployment/policy callbacks, target lock, broker admission and merge persistence callbacks. It rechecks PR state while locked and sends only `sha=H` and `merge_method=merge`. It never imports or executes source. Lost replies are resolved by re-reading the PR and immutable merge commit; closed PRs are never submitted again; preexisting merges and mismatched trees/parents are reported and persisted as merged-blocked observations.
- Added two inactive workflow reference templates outside `.github/workflows`. The CI template has no GitHub token or OIDC permission and delegates source retrieval, isolated execution, report scanning and publication to the fixed trusted coordinator; the merge template is a separate OIDC-enabled trusted job and does not execute candidate content.
- Focused local verification: 29 evidence/merge/template tests passed; Ruff passed for the four owned Python files. Merge tests build real commits in temporary Git repositories, while all GitHub REST calls use `httpx.MockTransport`.

## Coordinator follow-up plan (executed)

1. Add `release_coordinator.py` as the real module entrypoint referenced by the inactive templates. Its command line accepts only a strict approval UUID; runtime broker origin, credentials, GitHub token provider and installed policy come from trusted service construction, never from event or job fields.
2. Add a fixed-origin HTTPS broker client with no redirects/environment proxies/cookies, short request timeout, total operation deadline, response-size cap, strict duplicate-free JSON and safe error codes. Use injected `httpx.MockTransport` in tests. Provide explicit adapters for P07 caller verification and the P05 merge lock/admission/current-state/ledger callbacks.
3. Implement `GitHubJobCorrelationVerifier` using the existing bounded fixed-origin `GitHubEvidenceClient`: fetch the exact workflow run and requested attempt, paginate all jobs, and require unique exact check-run/run/attempt/repository/workflow/ref/status correlation. Never accept a caller-supplied API URL or use workflow output as proof.
4. Define durable HTTP persistence separately for valid two-parent merge evidence and malformed-parent observations. The latter records the observed commit, actual full parent list (including zero/one/more-than-two), actual and expected trees, approval ID and fence as `merged_blocked`; do not coerce it into P05's two-parent `MergeEvidence`.
5. Repoint inactive templates at `python -m chatcore_release_control.release_coordinator`, add MockTransport tests for route/method/body/schema/auth/size/timeout/error and job-attempt mismatches, and rerun focused P04/P07 tests plus Ruff and diff self-review. No live token, broker, GitHub API, dispatch or merge is used.

## Coordinator follow-up implementation record

- `release_coordinator.py` now provides the actual strict module CLI (`checkconfig` and `merge-approved-release --approval-id UUID`), protected environment config parsing, fixed HTTPS broker client, validated Actions OIDC token exchange, local RS256 OIDC caller verifier, and an explicit `execute_merge` runtime that wires the existing P04 controller and P01 object collector. The github API client receives only `RELEASE_CONTROL_GITHUB_API_TOKEN`; broker bearer credentials come from the audience-scoped Actions OIDC provider. No host, URL, command, image or environment selector is accepted from event/job arguments.
- Broker contracts are explicit and all requests use the signed OIDC bearer token: `GET /v1/releases/{uuid}/merge-context` returns exactly the approval/repository/repository ID/owner ID/PR number/target/sequence/dispatch fence, full deployed identity, B/H/head tree, current policy digest and manifest, card review bundle/digest, and expected workflow SHA. The client verifies manifest and review digests before use. It recomputes a canonical evidence digest over PR/repo/event/B/H/tested tree/synthetic merge/check App+workflow+run attempt/check-run/job IDs and requires equality with `review_bundle.check_evidence_digest` before merge.
- `POST /v1/releases/{uuid}/merge-lease` body is `{dispatch_fence, run_id, run_attempt, check_run_id}` and returns `{lease_id, dispatch_fence, expires_at}`. `DELETE` on that route carries `{lease_id, dispatch_fence}`. `POST /v1/releases/{uuid}/merge-admission` carries only `{head_sha, dispatch_fence, run_id, run_attempt, check_run_id, lease_id}`. P05 must atomically bind the signed OIDC identity, active fence, immutable candidate H and live lease; client body identity is correlation data only. The broker independently re-fetches/recomputes GitHub check evidence and compares its canonical evidence digest with the card's immutable review bundle; the request does not grant authority by asserting a digest.
- `GET /v1/targets/{target}/current-state` returns `{deployed_identity, deployed_commit, policy_digest}`. The adapter compares the entire parsed `DeploymentIdentity` to the card-bound value, including git SHA, image/config digests, generation and target sequence, before exposing `git_sha` to P04's existing callback.
- A verified two-parent observation is sent to `POST /v1/releases/{uuid}/merge-result` with `{merge_commit, parents, tree, expected_tree, dispatch_fence, lease_id}`. Any other bounded parent count goes to `POST /v1/releases/{uuid}/merge-anomalies` with the same observation plus allowlisted reason `unexpected_merge_observation`. Both receipts are immutable/idempotent by approval/fence/lease; anomaly persistence must remain possible after lease expiry/release and must never be represented as two-parent `MergeEvidence`.
- `GitHubJobCorrelationVerifier` retrieves the exact run/attempt and paginates every job through the existing fixed API client. It derives the unique current job check-run ID from the configured job name, then OIDC validation independently requires that run, attempt, repo, workflow SHA/path and runner environment match the signed token and installed Git blob map. The workflow-closure map and Git object store are protected deployment config; exact D/B/H objects must already be locally available.
- P04 now keeps deployment target separate from PR base branch: logical target may be `production`, while PR base defaults explicitly to `main`. Full current `DeploymentIdentity` comparison prevents a same-Git-SHA image/config/generation move from appearing current.
- Merge retries now distinguish an initial `queued` request from `merge_authorized` with an uncertain prior effect. The latter must carry an existing context lease bound to the same run/attempt/check-run; it is reused and never reacquired or released. A closed PR is reconciled and persisted against that same lease, including after its expiry. An open PR can reach the fixed merge API only when the broker replay says `may_start` and the same lease/admission deadline remains live immediately before the PUT. Terminal broker states return their stored merge and host receipt without GitHub calls.
- Local verification: `test_release_coordinator.py`, `test_github_evidence.py`, `test_merge_control.py`, and `test_oidc.py` pass (69 tests); Ruff and `git diff --check` pass. Tests use only `httpx.MockTransport` and local Git fixtures. No environment secrets were read and no network request, dispatch or merge was made.

## Remaining integration and proof

- The merge CLI executes when named protected deployment settings are present. CI phase source is now wired, but no digest-pinned CI runtime file, gateway token, scanner, Docker/runsc host or image has been provisioned. Keep the CI template inactive until those installed inputs and isolation permissions are verified.
- The broker routes above are client contracts for main's broker implementation; an integrated service test must verify its signed OIDC auth, atomic lease/admission, independent job verification, current-state response, and idempotent post-expiry merge/audit persistence.
- Local Git object storage must contain exact D/B/H and workflow-closure objects before merge execution. This increment does not fetch objects over the network, and no disposable-repository API behavior has been proven.
- P02 baseline export and the image-installed monorepo harness now have source. P02 remains partial: no Docker/runsc or host build was executed, so installed image correctness, Linux isolation, egress/resource enforcement, and live service proof remain outstanding.
- GitHub's merge API cannot atomically guard expected base B. A base move after the controller's locked recheck can still produce a merge; reconciliation marks an unexpected parent/tree merged-blocked but cannot undo that external write.

## CI phase coordinator implementation record

- The three existing strict PR-number commands now execute source paths: `inspect-pull-request` obtains and checks the authenticated gateway context, verifies fixed GitHub PR metadata against it, runs P01 D/B/H object inspection, and writes an immutable bounded prepared request; `run-isolated-candidate` refreshes context/PR identity and invokes the configured P02 `DockerSandboxRunner`; `scan-and-publish-report` refreshes context again, runs the mandatory `ScannerBinaryAdapter`, validates the exact version-2 P02 report identity and zero exit, writes a digest-named private artifact, uploads its bounded bytes, and validates the gateway receipt. CLI output is limited to phase, numeric PR, request ID, full head SHA, report/patch digests, and artifact ID. Candidate report text is not written to Actions stdout.
- The PR overlay is built directly from regular UTF-8 blobs at immutable H through P01's isolated `_GitRunner`, after the whole D/B/H history/path policy check. No worktree is checked out. Added and modified text blobs are supplied to the version-2 worker. Deleted, binary, symlink/submodule, excessive, malformed, or otherwise unrepresentable overlays fail closed instead of testing a different source tree.
- `ProtectedCiRuntimeConfig` requires canonical JSON at `RELEASE_CONTROL_CI_CONFIG_PATH` with the matching `RELEASE_CONTROL_CI_CONFIG_SHA256`, plus `GITHUB_REPOSITORY`, `GITHUB_EVENT_NAME=pull_request`, `RELEASE_CONTROL_GITHUB_API_TOKEN`, and `RELEASE_CONTROL_CI_GATEWAY_TOKEN`. Config schema pins `gateway_origin`, repository/owner numeric IDs, local object repository, policy path+digest, worker manifest path+digest, installed baseline root, Docker binary/runsc path+digest, immutable worker image ref, six resource limits, scanner executable/config path+digests+identity, and fixed state/artifact directories. Builder verifies policy and worker artifacts, baseline tree inventory/digests, scanner identity and runtime value types before constructing clients; no shell, host, URL, command, or image is selected by PR data.
- The fixed gateway contract is `GET /v1/ci/pull-requests/{number}/context` and `POST /v1/ci/pull-requests/{number}/report`. Context includes the exact P03 `ProvenanceRecord`, published `refs/heads/clawdot/{request_id}`, immutable repository/PR/D/B/H/tree/policy, title/body/specification/examples/artifacts, and `context_sha256` over canonical JSON with that digest field omitted. Upload contains only request ID, head, policy/context/report digests, and base64 report; receipt binds the request, PR, head, report digest and immutable artifact ID. The bearer token and GitHub read token remain in the trusted host process; P02's runner does not forward environment, mount, token or Actions command channels to the container.
- Local verification uses a synthetic temporary Git repository, fake P02 runner, and `httpx.MockTransport`. It covers exact H overlay extraction, PR movement, deletion rejection, stale context, report scanner finding, report stdout suppression, fixed gateway routes/auth, missing configuration and missing-runsc configuration mapping. No Docker, GitHub API, gateway, secret or live artifact upload is invoked.
- Local verification: focused P04 CI/coordinator/evidence/merge/OIDC suites pass (81 tests); Ruff and `git diff --check` pass. The runsc/image host has not been provisioned or exercised. Gateway service routes are client contracts for main's broker implementation and need an integrated service test.

## CI phase coordinator follow-up plan

1. Replace the three parsed-but-disabled CI commands with real `ci-prepare`, `ci-test`, and `ci-report` operations. Load one canonical, digest-pinned protected runtime JSON file; require fixed repository and gateway identity, local exact Git object repository, installed P02 version-2 baseline manifest/image/runsc/resource limits, scanner binary/config hashes, and fixed private state/artifact directories. Missing configuration reports `invalid_runtime_configuration`.
2. Add a fixed HTTPS gateway context client for `GET /v1/ci/pull-requests/{number}/context` and `POST /v1/ci/pull-requests/{number}/report`, with bounded JSON/body/time, no redirects/proxies/cookies, and bearer token only in the trusted host process. Context binds the PR to the P03 immutable provenance record and provides bounded trusted specification/examples; client verifies a canonical context digest and exact policy/provenance/ref schema.
3. `ci-prepare` re-fetches the PR through fixed `api.github.com`, requires exact open PR base/head/repository IDs from gateway context, runs P01 D/B/H collection, verifies H's tree, and exports only added/modified regular UTF-8 blobs from exact H for P02's fixed worker request. Deletion/unrepresentable changes fail closed; no checkout or candidate process is used.
4. `ci-test` verifies the private prepared-state digest, then calls `DockerSandboxRunner.run` with that request. It writes only a size-bounded report into a gateway-owned fixed state path; it never emits raw report bytes to stdout or forwards credentials, Actions channels, mounts, or arbitrary config to the container.
5. `ci-report` rechecks the report digest, runs the mandatory installed `ScannerBinaryAdapter`, validates exact P02 version-2 report identity/exit fields and bounded output, atomically writes a digest-named private artifact, then uploads the bounded report through the same fixed gateway client and verifies the immutable response receipt. Stdout contains only phase, IDs and digests.
6. Add MockTransport/fake-runner tests for context/provenance mismatch, changed PR head/base, object-to-overlay extraction including deleted paths, no raw report stdout, scanner findings/failures, report upload and invalid installed config. Do not invoke Docker, real GitHub, gateway, or inspect actual secrets.

## Hosted-runner bootstrap implementation record (2026-10-31)

- Added `hosted_bootstrap.py`, a standard-library-only module that can be
  independently downloaded by the inactive workflow after a protected
  `RELEASE_CONTROL_BOOTSTRAP_SOURCE_SHA256` check. It then independently
  verifies its source hash against a separately digest-pinned canonical
  manifest, checks repository/name/numeric ID, and requests only the fixed
  manifest and content-addressed archive paths over authenticated HTTPS with
  redirects and environment proxies disabled.
- The canonical manifest defines one bounded archive and exact files/modes/
  sizes/hashes. Archive parsing rejects duplicates, unknown or unsafe paths,
  symlink/hardlink/special/sparse/PAX entries, unlisted directories, mode
  mismatches, truncation, oversize data, and digest mismatch. It validates
  that the CI config, policy, worker manifest, baseline archive, scanner,
  image, runsc, and explicit owner runtime policy agree before privileged
  commands. The baseline tar is separately checked against the P02 snapshot
  file inventory and sensitive path exclusions.
- Installation uses only verified local artifacts. Coordinator and dependency
  wheels are installed into a root-owned offline venv with `--no-index`,
  `--no-deps`, and `pip check`. The pinned runsc binary is installed, its
  explicit runtime policy is registered in Docker's daemon config, and the
  advertised runtime is verified. The local worker image tar is loaded with no
  pull and its RepoDigest must match the immutable image reference; a minimal
  no-network/read-only runsc capability probe uses only the pinned image.
  The generated digest-pinned runtime config path is passed to later trusted
  steps through `GITHUB_ENV`. No candidate checkout or candidate dependency
  installation occurs.
- The inactive CI template now runs a bounded bootstrap-source fetch and hash
  check before using the installed venv coordinator. It keeps the template
  outside `.github/workflows`, accepts no runtime selector from PR data, and
  passes the gateway credential only to trusted host steps.
- Verification: 15 hosted-bootstrap tests and the combined P04 CI/coordinator,
  GitHub evidence, merge, and OIDC suite passed (84 tests). Ruff check/format
  and `git diff --check` passed. Tests use a fake downloader and command
  executor; no download, secret read, package install, Docker, or Linux runner
  was used.

## Hosted-runner bootstrap integration record

- `publication_service.py` now serves the fixed authenticated raw-byte
  bootstrap routes. Startup binds the exact protected source, canonical
  manifest, and `runtime.tar` paths, checks the source against the installed
  module, validates pinned hashes and archive inventory, and rechecks hashes
  on each read. Unknown paths and archive digests cannot expose arbitrary
  files.
- `git_fetch.py` populates the CI `object_repository` when P01 reports missing
  objects. It fetches only the context-bound PR head ref from the fixed
  `https://github.com/{owner}/{repo}.git` URL into a temporary bare quarantine,
  checks exact H and complete D/B/H policy evidence, then promotes bounded
  pack/index files. It uses no checkout, shallow/filter fetch, redirect,
  proxy, hook, submodule recursion, replacement object, user config, token
  argument/environment value, or persisted Git credential.
- `CiPhaseCoordinator.prepare` invokes the fetch only after authenticated PR
  correlation. The runtime builder constructs the fetcher from installed
  repository configuration and the protected GitHub token provider. Complete
  local history skips network access.
- Local tests cover route authentication/tampering, archive/source binding,
  fetch command construction, token file permissions/cleanup, exact D/B/H
  promotion, policy rejection, and timeout without promotion, using mock
  transports and temporary Git fixtures.
- Still pending: provision actual digest-pinned artifacts, configure the
  gateway and GitHub credential provider, and demonstrate bootstrap on a
  disposable hosted Ubuntu runner. No live download/fetch, package install,
  sudo, Docker daemon change, runsc launch, image load, or hosted-runner proof
  was performed. The workflow remains inactive.

## Gateway bootstrap artifact routes and Git object population plan

1. Extend only the concrete P03 gateway config/ASGI implementation with
   protected file paths and digests for the bootstrap source, canonical
   manifest, and content-addressed runtime archive. At startup, validate their
   exact schemas, hashes, modes, and archive inventory against the pinned
   manifest. Serve only fixed authenticated GETs for the bootstrap module,
   manifest, and the one matching archive digest; return bounded raw bytes,
   never arbitrary files or candidate publication artifacts.
2. Add a read-only GitHub fetch adapter for CI. It accepts only the already
   authenticated fixed repository ID/name and context-bound PR number,
   base branch, D/B/H. It fetches only `refs/pull/<n>/head` from the fixed
   `https://github.com/{owner}/{repo}.git` endpoint into a temporary bare
   quarantine with no shallow flags, redirects, hooks, filters, user config,
   proxy, submodule recursion, or token argv/env/config. A 0600 token file and
   fixed askpass helper feed credentials only to Git's HTTPS prompt.
3. Before promotion, require the fetched PR ref to equal exact H and use P01
   to prove D/B/H completeness, ancestry, and all path/history policy. Reject
   incomplete, shallow, oversized, timed out, or wrong-ref fetches. Only then
   copy verified pack/index objects into the job's configured isolated bare
   object repository. Missing D/B/H objects trigger this fetch, not a permanent
   runtime configuration error.
4. Invoke the fetcher after gateway context and authenticated GitHub PR
   identity validation and before P01 bundle collection. Keep object access
   read-only in later CI phases; do not checkout H or execute candidate code.
5. Test bootstrap GET authentication, fixed route mapping, digest mismatch,
   unsupported request paths, response bounds, and protected file modes with
   ASGI fixtures. Test fetch command construction, token-file permissions and
   cleanup, exact remote/ref, shallow rejection, timeout/output bounds,
   complete-ancestry promotion, and wrong-H rejection with a fake process
   executor plus temporary local Git objects; make no real HTTP/GitHub calls.

## Hosted-runner bootstrap follow-up plan

The inactive CI template currently invokes the release coordinator directly on
`ubuntu-latest`, where its package, pinned configuration, worker image, and
gVisor runtime are not installed. Before any activation, add a concrete
bootstrap source path which makes those trusted inputs available first.

1. Add `hosted_bootstrap.py` with a fixed HTTPS origin and paths, protected
   bearer token, pinned canonical manifest digest, repository ID binding,
   total time/body/file limits, and no redirects/proxies/cookies. All
   transport and process calls are injectable for tests.
2. Define a canonical signed-by-configuration manifest that pins one archive
   digest/size plus an exact file inventory and every extracted file digest,
   mode, and size. Validate archive structure before writing: regular files
   only, no links/sparse entries/traversal/duplicates/unknown files, bounded
   expansion, exact inventory, and no candidate-derived paths.
3. Provision only protected files: complete coordinator wheel plus every
   dependency wheel for offline installation, canonical CI config, worker
   baseline/manifest, immutable worker image tar, runsc binary, and explicit
   owner runtime policy. Verify archive, inventory, config/schema, all file
   hashes, runtime policy/image/config agreement, and image digest before any
   installation or execution.
4. Install runsc and register the fixed `runsc` Docker runtime only after
   checking an explicit owner policy and digest; restart/check the Docker
   daemon's advertised runtime. Load the pinned image from the verified local
   archive and verify its immutable repo digest without pulling. Build an
   offline immutable venv from only verified wheels, then install the
   digest-verified config at its fixed protected location.
5. Update the inactive CI template so bootstrap completes before coordinator
   phases. Keep candidate PR contents out of checkout, workflow scripts,
   dependency installation, image loading, and bootstrap. Keep bearer tokens
   only in the trusted host process. Do not invent artifact URLs, image or
   runsc versions/digests; absent protected owner configuration must fail
   closed.
6. Test canonical manifest/asset parsing, bad hashes/inventory, traversal,
   links, sparse/duplicate/binary/oversized entries, fixed-origin auth and
   bounds through mocked transport, offline install ordering, daemon/runtime
   policy mismatches, missing config, and immutable image digest verification.
   Tests must never perform a download, Docker command, install, or real token
   read. Record Linux hosted-runner and gVisor capability as a pending live
   proof.
