# P02 Isolated Development Controller and Worker Plan

> **For agentic workers:** Implement this plan task-by-task in the current worktree. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add a trusted local development controller and its fixed worker source: generate policy-approved text-only candidate files, run them through a verified disposable `runsc` container using a fixed Docker CLI policy, and return only bounded reports accepted by a required scanner.

**Architecture:** Keep generation in the trusted controller through the existing `Provider` protocol and verify each requested base commit through a mandatory trusted callback. The immutable worker image contains the fixed `development_worker.py` and a manifest listing the installed policy digest and exact allowed paths; the controller verifies the image labels and manifest digest before create. Validate generated files before staging the complete request in an owned temporary directory and copying it into a container with `docker cp`; the container has no host bind mounts, sockets, credentials, or network. Verify the configured `runsc` binary digest and immutable image repository digest before every run. Start an idle fixed container, copy input, then `docker exec` only the fixed worker module and pinned manifest digest. Capture bounded output and enforce deadlines in a concrete subprocess executor. Cleanup by exact container ID (or the unique random name if create timed out before returning an ID) on every exit. A required scanner must accept the report before return.

**Tech Stack:** Python 3.12 standard library (`asyncio`, `hashlib`, `json`, `os`, `pathlib`, `selectors`, `subprocess`, `tempfile`, `time`); existing `Provider`, `Message`, and `PolicyManifest` APIs; pytest and Ruff.

---

## Scope and acceptance

Only these P02-owned paths may change:

- Create `packages/plugin-release-control/src/chatcore_release_control/development.py` for request validation, generation control, Docker CLI sandbox policy, bounded process handling, and report scanning.
- Create `packages/plugin-release-control/src/chatcore_release_control/development_worker.py` for installed manifest verification, bounded worker request validation, safe in-container staging, and candidate test execution inside the container only.
- Create `packages/plugin-release-control/tests/test_development.py` for fake-provider, fake-process, traversal, resource, deadline, output, scanner, and cleanup coverage.
- Create `packages/plugin-release-control/tests/test_development_worker.py` for manifest/request validation and fixed in-container test command construction. Never execute candidate code on the host during tests.
- Replace this P02 plan with the detailed implementation and execution record.

Acceptance criteria:

- Generation calls the injected existing `Provider` protocol from the trusted controller, never from candidate code.
- The controller binds generated output to the request's exact base commit and installed policy digest.
- A mandatory trusted `base_commit_verifier` rejects caller-selected or stale base commits before generation or candidate execution.
- Candidate output is an exact JSON object containing only a bounded mapping of approved relative file paths to UTF-8 text. Reject traversal, private/protected/unapproved/dependency paths, duplicate keys, binary/NUL data, invalid Unicode, oversized files, aggregate oversize, and empty patches.
- Construction requires an immutable `repository@sha256:<digest>` image reference, an absolute `runsc` executable path and expected SHA-256, and every positive CPU/memory/PID/time/output/workspace limit. There is no default image, runtime, resource policy, or scanner acceptance.
- Before any candidate container starts, fixed Docker inspection commands verify that Docker registers `runsc` at the configured path, that the runtime binary hashes to the configured digest, and that the configured immutable image reference appears in the local image's RepoDigests. Never pull or build an image.
- Before create, fixed Docker inspection also verifies immutable image labels for the worker-manifest digest, installed policy digest, and baseline commit. The worker verifies the manifest file hash and refuses mismatched request policy, baseline, or paths.
- The fixed Docker CLI command array applies `--runtime=runsc`, `--network=none`, `--pull=never`, read-only root filesystem, no-new-privileges, dropped capabilities, non-root UID/GID, bounded CPU/memory/swap/PIDs, and a bounded `noexec,nosuid,nodev` tmpfs. The trusted host CLI uses only its fixed local Docker socket endpoint; the container receives no host mounts, socket, credential environment, Actions command/output channel, candidate Dockerfile, or package installation.
- Candidate input is copied to the owned container using `docker cp`; the fixed trusted worker executable runs inside that container after copy. No user-controlled command or shell is passed to Docker.
- The subprocess executor receives only a minimal fixed host environment, enforces per-command time/output caps, and returns bounded stdout/stderr. The candidate run has a global time budget.
- The exact owned container is removed on success, failure, timeout, startup error, and cancellation. Cleanup failure fails closed and never returns an unscanned report.
- A report scanner is required and must return literal `True`; exceptions, false, non-Boolean results, or oversized reports fail closed.
- The candidate test process is launched only by the fixed worker inside the disposable container. Worker unit tests validate parsing and command construction without executing candidate source on the host.
- The current worker rejects a missing or zero `unittest` discovery summary. That textual summary is untrusted candidate-controlled output and is only an empty-harness guard, not trusted pass or count evidence.
- Fake-process tests can establish command construction, validation, time/output handling contracts, cleanup attempts and scanner enforcement. They do not establish Linux `runsc`, egress, or resource isolation.

## Task 1: Define immutable request, resource, and error contracts

**Files:**
- Create: `packages/plugin-release-control/src/chatcore_release_control/development.py`
- Test: `packages/plugin-release-control/tests/test_development.py`

- [x] **Step 1: Write a failing test for `GenerationRequest` validation.** Use a valid `PolicyManifest`, 40-character base commit, bounded nonempty specification, and a tuple of canonical JSON synthetic examples. Assert the request stores the manifest digest and rejects a mismatched digest, invalid commit, empty/oversized spec, excessive examples, unsupported JSON values, and oversized examples.
- [x] **Step 2: Run the focused test and confirm the new module is missing.** Run `uv run pytest packages/plugin-release-control/tests/test_development.py::test_generation_request_binds_policy_base_and_synthetic_examples -q`; expected result is an import/collection failure for the absent module.
- [x] **Step 3: Add `ResourcePolicy`, `SandboxConfig`, bounded result types, and fixed safe errors.** `ResourcePolicy` requires exact integer `cpu_millis`, `memory_bytes`, `pids_limit`, `timeout_seconds`, `max_output_bytes`, and `workspace_bytes`; constrain each to conservative explicit maxima. `SandboxConfig` requires absolute Docker/runsc paths, `runtime_name == "runsc"`, 64-lowercase-hex runtime digest, immutable image reference ending in a SHA-256 digest, and a `ResourcePolicy` instance. Define fixed `DevelopmentError.code` values only.
- [x] **Step 4: Implement JSON/path validation.** Reject duplicate keys and constants, non-object schemas, unapproved paths, `Change` entries rejected by `PolicyManifest.validate_changes`, invalid UTF-8, control/NUL data, per-file/aggregate bounds, and noncanonical relative paths. Never create a host staging path before validation passes.
- [x] **Step 5: Run request/resource/path tests.** Expected: valid synthetic requests construct; every malformed or unbounded case fails with a fixed error and no callback/runner calls.

## Task 2: Keep generation in the trusted controller

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/development.py`
- Test: `packages/plugin-release-control/tests/test_development.py`

- [x] **Step 1: Add a failing test with a fake `Provider`.** The provider must receive one bounded prompt containing only the validated specification, synthetic examples, base commit, and policy digest. It returns exact JSON `{"files": {"allowed/path.py": "..."}}`. Assert the provider is called before the runner and the runner gets only validated output.
- [x] **Step 2: Add failing provider-result cases.** Reject tool calls, non-`end_turn` completions, provider exceptions/timeouts, oversized completion text, extra/missing fields, duplicate file paths/JSON keys, and empty output; assert no candidate container is created.
- [x] **Step 3: Implement `DevelopmentController`.** Require `Provider`, `CandidateRunner`, trusted installed `PolicyManifest`, and `ReportScanner` constructor arguments. Require request policy digest equal the installed manifest digest. Use `asyncio.wait_for` with a fixed generation timeout, `tools=()`, fixed bounded max tokens, and a per-run session key derived only from the validated base commit and request digest. Parse and validate the provider's exact files object before calling the runner.
- [x] **Step 4: Run fake-provider tests.** Expected: provider call happens only in controller code and every denied completion stops before runner invocation.

## Task 3: Verify the configured sandbox and fixed Docker policy

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/development.py`
- Test: `packages/plugin-release-control/tests/test_development.py`

- [x] **Step 1: Add fake-process tests for runtime/image verification.** Fake `docker info --format '{{json .Runtimes}}'` returns the exact `runsc` path; a local synthetic executable file hashes to the configured runtime digest; fake `docker image inspect --format '{{json .RepoDigests}}' <immutable-ref>` returns that exact reference. Assert mismatched name/path/hash/image digest or malformed inspect output denies before create.
- [x] **Step 2: Add tests for immutable configuration and explicit resource policy.** Reject mutable image tags, invalid digests, relative executable paths, missing resource policy, booleans masquerading as integers, negative/oversized limits, and runtime names other than `runsc`.
- [x] **Step 3: Implement sandbox verification.** Invoke only fixed Docker inspection arrays through the injected command executor with a sanitized host environment and bounded timeout/output. Read and hash the configured regular runtime executable without following symlinks, with an explicit maximum binary size. Do not contact registries, pull, build, or run candidate source during verification.
- [x] **Step 4: Add an argv policy assertion.** Assert candidate create arguments include pinned image, `--runtime=runsc`, `--pull=never`, `--network=none`, `--read-only`, `--security-opt=no-new-privileges`, `--cap-drop=ALL`, fixed non-root UID/GID, explicit `--cpus`, memory and swap caps, `--pids-limit`, and bounded tmpfs; assert no `--mount`, `-v`, host socket, env forwarding, candidate shell, or candidate-selected executable appears.
- [x] **Step 5: Run verification/argv tests.** Expected: all invalid identities deny before `docker create`, and valid identities produce one fixed policy command.

## Task 4: Copy bounded input into an owned container and run fixed worker

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/development.py`
- Test: `packages/plugin-release-control/tests/test_development.py`

- [x] **Step 1: Add fake-process lifecycle tests.** Successful flow is: verify runtime/image labels, create a unique random-named idle container with fixed `/usr/bin/sleep`, start it, copy a generated `request.json` to `/workspace/request.json`, invoke only fixed `env -i` plus `/opt/chatcore-release/development_worker.py` via `docker exec`, scan its stdout report, then remove the exact returned container ID. Assert no host path appears in container mount flags.
- [x] **Step 2: Add startup/cleanup failure tests.** Inject failures at create response parsing, start, `docker cp`, worker exec, timeout, oversized stdout/stderr, and cancellation. Assert cleanup uses the exact returned container ID after create or the exact random name if create timed out before returning its ID; assert no report escapes when any execution or cleanup step fails.
- [x] **Step 3: Implement `ProcessExecutor` and bounded subprocess capture.** Use `subprocess.Popen` with argv arrays (never shell), fixed `PATH`/locale/temporary `HOME`/empty Docker config only, no inherited environment, closed descriptors, and a new process group. Use selectors/nonblocking reads to enforce total stdout+stderr bytes while the process runs; kill/reap the process group on timeout or overflow and return fixed errors without raw diagnostics.
- [x] **Step 4: Implement staging and `DockerSandboxRunner`.** Stage only the canonical JSON request in an owned temporary directory with mode `0600`; create the container using a fixed argv, start it, `docker cp` the request into the container, and `docker exec` an immutable fixed worker entrypoint with `env -i`. Apply the global run deadline to each step and always attempt exact-owned-container cleanup in `finally`.
- [x] **Step 5: Implement cancellation-safe controller waiting.** Run the synchronous Docker sequence in `asyncio.to_thread`; shield its task, and on cancellation wait until its bounded cleanup path finishes before re-raising cancellation.
- [x] **Step 6: Run lifecycle tests.** Expected: the fake process trace shows only allowed fixed commands, every injected error attempts the right cleanup target, and timeout/output caps produce no returned report.

## Task 5: Require scanning before returning reports

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/development.py`
- Test: `packages/plugin-release-control/tests/test_development.py`

- [x] **Step 1: Add failing scanner tests.** A scanner returning literal `True` allows a bounded report; absent scanner at construction, `False`, non-Boolean result, exception, and over-limit worker report all deny and reveal no raw unscanned report.
- [x] **Step 2: Implement report gating.** Call the required scanner on the raw bounded worker stdout after successful cleanup. Only return immutable `DevelopmentResult` containing the scanned report, report SHA-256, base commit, and policy digest after scanner success. Do not interpret untrusted test counts as trusted check evidence.
- [x] **Step 3: Run report-scanner tests.** Expected: only literal `True` makes a report returnable.

## Task 6: Final review and evidence boundaries

**Files:**
- Review: `packages/plugin-release-control/src/chatcore_release_control/development.py`
- Review: `packages/plugin-release-control/tests/test_development.py`
- Review: this plan.

- [x] **Step 1: Run focused validation.** Run both P02 test modules and focused Ruff; all local fake-process/worker validation tests pass and Ruff reports no issues.
- [x] **Step 2: Self-review the actual diff.** Check request limits, callback boundary, runtime/image verification, exact argv, no-mount input copying, process output/time limits, cleanup on every branch, mandatory report scanning, and the P02-owned paths.
- [x] **Step 3: Report limitations truthfully.** Tests prove only local controller, manifest, command, and staging behavior; actual Linux `runsc`, kernel isolation, egress, resource impact, installed fixed worker image, and production bootstrap remain unverified. No Docker or `runsc` is executed in this task.

## Task 7: Add the fixed worker module and immutable baseline binding

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/development.py`
- Create: `packages/plugin-release-control/src/chatcore_release_control/development_worker.py`
- Modify: `packages/plugin-release-control/tests/test_development.py`
- Create: `packages/plugin-release-control/tests/test_development_worker.py`

- [x] **Step 1: Add failing tests for worker/image baseline binding.** A controller without a base-commit verifier fails construction; a verifier rejection or exception stops before generation/runner. Docker image inspection must return matching immutable labels for configured worker-manifest, policy, and baseline commit before create; mismatches deny.
- [x] **Step 2: Add failing worker validation tests.** Parse an exact versioned installed manifest with baseline commit, policy digest, worker source digest, and exact allowed paths. Reject a mismatched manifest digest, request policy digest, base commit, path set, duplicate JSON keys, traversal, symlink staging roots, nontext/binary/NUL source, and unknown request fields.
- [x] **Step 3: Implement the fixed worker module.** Read only fixed `/opt/chatcore-release/manifest.json` and `/workspace/request.json`; verify the manifest digest supplied by the trusted launcher; validate request baseline/policy identifiers and allowed paths; stage text files only beneath `/workspace/candidate` with exclusive regular-file writes; invoke the fixed Python unittest runner inside the container with bounded timeout/output capture; emit one bounded JSON report with base commit and policy digest. Do not import or execute candidate code in controller or host tests.
- [x] **Step 4: Bind the controller and image.** Require configured `worker_manifest_digest`, `policy_digest`, and `baseline_commit`; inspect image labels against all three; include the trusted base/policy and manifest digest in the worker payload and fixed exec argv; require base equality and the trusted verifier to return literal `True` before provider invocation.
- [x] **Step 5: Test only fake execution on the host.** Assert the Docker runner invokes the fixed installed `development_worker.py` with `-I -S` and the pinned manifest digest. Unit tests validate worker parsing/staging/command argv but do not run candidate source on the host. Candidate execution remains behind the fake-process Docker boundary locally.
- [x] **Step 6: Run focused P02 tests and Ruff.** Run both P02 test modules plus focused Ruff. Record that no Docker/runsc command was executed and actual Linux isolation remains unproven.

## Known boundaries

This P02 increment is **partial**. The worker writes only generated files under `/workspace/candidate` and discovers tests only under `/workspace/candidate/tests`; it does not stage the immutable baseline repository, install its pinned dependencies, or run the repository's approved test harness. A nonzero `unittest` summary is candidate-controlled output, not evidence that baseline tests passed. The required report scanner must reject raw or unverified output; no trusted pass/count assertion is made here.

This task does not create a Docker image, change root dependencies, invoke Docker/`runsc`, contact an LLM service, configure Linux egress, or install a service. The image must package the fixed worker module and versioned manifest binding the worker source digest, policy digest, baseline commit, and allowed paths; local code checks the pinned image digest and labels, and the worker verifies the installed manifest. Operational work must build and pin that image and measure its isolation/resource behavior separately.

## Remaining source work: immutable-baseline test harness

- [ ] Stage the exact verified baseline source into the disposable image/container without host bind mounts, and make its identity match the manifest-bound baseline commit.
- [ ] Provide a fixed, policy-selected test harness that installs only pinned/verified dependencies and runs the approved baseline tests plus generated candidate tests inside the sandbox.
- [ ] Emit a bounded structured report whose baseline identity, invoked test command, discovery status, exit status, and output are validated by trusted code. Do not infer trusted pass/count evidence from candidate stdout.
- [ ] Add fake-process tests proving baseline staging and fixed harness policy, without executing generated code on the host; retain actual runsc/isolation proof as separate work.

## Execution record

- RED check: the new worker tests initially failed collection because `development_worker.py` was absent; controller/image tests failed on the absent verifier and manifest-bound config.
- Focused P02 verification: `uv run pytest packages/plugin-release-control/tests/test_development.py packages/plugin-release-control/tests/test_development_worker.py -q` (57 passed after the empty-discovery guard).
- Focused Ruff: `uv run ruff check packages/plugin-release-control/src/chatcore_release_control/development.py packages/plugin-release-control/src/chatcore_release_control/development_worker.py packages/plugin-release-control/tests/test_development.py packages/plugin-release-control/tests/test_development_worker.py` (all checks passed).
- No Docker or `runsc` command was run. The immutable-baseline test harness is not implemented. Installed image correctness, gVisor isolation, egress blocking, resource enforcement, host image build/deploy, and live service proof remain outstanding; P02 is not complete.

## Resumed baseline/harness implementation

Main owns this increment; P04/P06/P09 writers do not edit P02 files.

1. Add `baseline.py` which derives a bounded regular-file snapshot from the actual
   immutable Git object tree using the isolated P01 reader. Reject private paths,
   traversal, symlinks/submodules and size limits before reading blob contents.
   Bind all path/mode/size/content digests, baseline tree, uv.lock digest, exact
   source roots/test paths, worker source and harness source in canonical v2 manifest.
   Return a deterministic tar artifact for offline, operator-owned image preparation.
2. Extend the fixed worker to require the v2 immutable baseline manifest for execution,
   verify installed baseline bytes, copy them as data into the private candidate
   workspace, then overlay only policy-allowed generated files with no-follow writes.
   Legacy v1 parsing may remain for inspection, but execution must reject it.
3. Add `monorepo_harness.py`: fixed trusted installed interpreter/pytest launch,
   exact manifest-selected test paths and source roots, no candidate-selected shell,
   command, environment, network or dependency installation. Dependencies are prepared
   offline in the immutable worker image against the manifest uv.lock digest.
4. Worker/launcher reports distinguish observed process exit from untrusted candidate
   stdout. Verify bounded schema and exact baseline/policy/manifest/harness bindings
   before returning the scanned report; no trusted test count or semantic correctness
   is inferred from candidate stdout.
5. Add temp Git/snapshot and fake-child tests for actual baseline staging, preserved
   baseline files, allowed overlay, digest/mode/source-root rejection, fixed command
   and malformed/wrong-identity/empty reports. Do not execute generated source on host.
6. Provide a trusted offline image recipe generator with immutable parent digest and
   fixed harness/source paths. Do not build/pull/run it here. Actual Linux image and
   runsc/egress/resource proofs remain operational acceptance, separate from source.

### Baseline/harness source completion record

The resumed implementation adds actual Git-derived deterministic baseline artifacts
in `baseline.py`, a version-2 file/content/mode/lock/source-root/test-path manifest,
and the image-installed stdlib `monorepo_harness.py`. It verifies/copies baseline
files before an allowed generated overlay, then invokes only installed pinned pytest
against manifest-selected paths with auto-plugin discovery disabled. Version-1 images
are accepted for metadata inspection but refused for execution; there is no legacy
candidate-only fallback. Dependency cache bytes are copied, hashed and safely checked
for traversal/symlinks/hardlinks/special entries before offline frozen uv installation
in the separately generated immutable-parent image recipe. No image was built.

The controller now rejects malformed/wrong-identity report envelopes and nonzero
process exits after scanning. It does not treat candidate stdout as trusted counts,
pass evidence or semantic correctness. Reports remain untrusted candidate observations;
P04 independent checks/review are still required.

Local regression evidence: 74 focused baseline/development/worker cases passed;
Ruff passed. Tests stage only synthetic source as data and use fake child execution;
no generated code was executed on the host, and no Docker/runsc/image build occurred.
Remaining P02 acceptance is actual audited image/dependency preparation and disposable
Linux isolation/egress/resource proof, plus cross-service integration. Earlier notes
that the baseline staging and monorepo harness SOURCE were absent are historical and
superseded by this completion record.

### Independent archive review repair

The reviewer found that a forged v2 inventory could contain private paths and that
unlisted files from an unverified `ADD` archive could remain readable in the image.
The manifest now binds the exact archive SHA-256 and only explicitly reviewed source
script exceptions. The fixed image-preparation helper freezes the bounded archive,
verifies its hash, then validates every entry's path, type, mode, size and content
digest and the exact set before creating the baseline. Runtime staging rejects extra
files/directories and verifies all installed bytes again. Raw source/cache archives
are copied mode 0600 instead of implicitly extracted by Docker. Credential paths
cannot be allowed through a script exception. Generated candidate files are retained
as an immutable repr-hidden result tuple for publication rather than discarded.

Regression evidence: the three initial review regression tests failed before the
repair; 87 focused P02 tests pass afterward. An independent read-only review reran
21 baseline tests and found the archive issue repaired. No candidate code, Docker,
image build or network operation was executed.
