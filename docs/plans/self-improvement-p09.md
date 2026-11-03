# P09: Whole-runner supervisor and deployment relay

## Objective and boundaries

Implement a local, fail-closed supervisor core that admits one ephemeral Actions
runner per authorized operation, keeps registration and relay credentials in the
supervisor, runs the complete runner under an explicitly selected `runsc` runtime,
and removes the exact owned runner/container on every exit path. Relay input is
limited to bounded operation and receipt identifiers sent to a configured fixed
origin. The service templates are inactive examples; no runner is registered and
no Docker or `runsc` command is run by this work.

Own only `runner_supervisor.py`, `tests/test_runner_supervisor.py`, inactive
`p09-*` templates under `services/release-control/src/chatcore_release_control/templates`, and this
plan. Existing journal, development, and OIDC source is read-only. Do not edit
root dependencies or deploy scripts. No network, production service, secret,
commit, push, or PR operations.

## Design decisions

1. **Immutable installed policy.** Require absolute Docker/runsc paths, verified
   runtime package digest, immutable image digest, installed runner execution
   closure digest, explicit bounded CPU/memory/PID/time/scratch/output budgets,
   and a fixed egress policy. Reject unknown/missing policy fields and unpinned
   image/runtime/closure. Freeze policy values so later caller mutation cannot
   weaken a running supervisor.
2. **Capability gate before credentials.** An injected capability probe must
   affirm the exact installed runtime, image and closure digests and supported
   isolation/resource/egress controls. Any mismatch or unavailable control
   aborts before registration. No fallback to `runc`, default runtime, privileged
   mode, host networking, or implicit Docker settings.
3. **Concrete, narrow process adapter.** Provide a fixed Docker CLI adapter that
   constructs argv from validated policy and generated owner IDs only. No shell,
   caller-selected executable/command/image, host environment inheritance,
   mounts, socket, devices, host namespaces, host PID, privilege, or ambient
   credentials. Process output/time are bounded. A separate registration adapter
   is injected; ephemeral registration material is passed only to its exact
   create/remove calls and never returned, logged, put in argv, environment, or a
   host mount. The ephemeral runner token is sent only to the fixed in-container
   bootstrap over stdin and a private socket; long-lived App credentials remain
   supervisor-only. Registration removal is idempotently attempted for the
   exact owner.
4. **Whole-runner, one-job lifecycle.** Generate an unguessable owner ID and
   durably reserve the sole runner slot in a mode-0600 SQLite journal inside a
   mode-0700 state directory. Hold an OS lock across recovery, launch, and cleanup
   so competing supervisor processes cannot overlap. Create one disposable
   container with one fixed installed runner entrypoint and `--once`, register
   it, wait only to the configured bound, then remove that exact container and
   revoke/remove the corresponding registration. Keep the journal entry on any
   ambiguous/error cleanup; retry only exact owner identities on restart before
   admitting a new runner. No broad label pruning, caller-controlled container
   names, host bind mounts, or runner config mounts are permitted. A short-lived
   registration token is passed once over stdin to the fixed in-container
   registration helper after the fixed bootstrap starts. That bootstrap must
   remain unready for jobs until configuration completes; its audited source is
   part of the still-missing image build. Long-lived App credentials remain
   supervisor-only.
5. **Bounded fixed-origin relay.** Accept only canonical bounded operation and
   receipt IDs. A fixed-origin transport supplied by the installed supervisor
   obtains OIDC through a supervisor-owned callback and calls one fixed route;
   request data cannot choose host, scheme, path, command, image, headers, or
   environment. Return only a bounded opaque receipt reference/status. Never
   expose bearer tokens in results or exceptions.
6. **Templates remain inactive.** Add root-owned systemd/config examples with
   no `Install` activation target, no embedded credentials, and explicit
   placeholders that force operator provisioning. They document a single fixed
   supervisor process and state directory; they do not claim a live host is
   provisioned.

## Test-first implementation sequence

1. **Installed CLI/config.** Require an explicit `run-once --policy ABSOLUTE_PATH`
   command, exact JSON schema with duplicate/unknown-field rejection, root-owned
   0600 file and protected parent, and no placeholder/default values. Build
   concrete adapters only from this checked configuration. An absent or unsafe
   file exits before reading credential material or launching a process.
2. **Capability probe.** Verify the installed gVisor manifest and every listed
   package file digest, fixed Docker runtime path via a bounded `docker info`
   query, pinned local image reference and runner-closure label via a bounded
   `docker image inspect`, cgroup v2, and a root-owned digest-pinned egress
   attestation manifest. Return a capability snapshot only when all checks match.
   This is an installed-configuration check, not proof of actual kernel isolation
   or packet-level egress; those remain live acceptance work.
3. **Repository-selected registration API.** Add a fixed `api.github.com` client
   with environment/proxy inheritance disabled and redirects disabled, bounded JSON,
   fixed repository-scoped endpoints, exact ephemeral runner names, and safe
   create/list/delete handling. It obtains an installation token only through an
   injected supervisor-owned provider. Tests use an HTTPX MockTransport. The
   source fixes the requested repository-scoped App permission to
   `administration:write` because runner-management API calls require it. The
   repository-specific grant and credential provisioning still require operator
   review; this source does not provide or provision a key. Installed policy
   records distinct publication, broker, and runner-registration App roles and
   rejects reused App IDs; only the runner-registration role may request the
   runner-management permission.
4. **Runner image/bootstrap.** Add a standard-library bootstrap module with a
   strict image manifest, digest verification of the runner file closure and
   bootstrap, no symlinks/unlisted files, and a pinned standard runner archive
   containing fixed `config.sh` and `run.sh` entries. Configure URL, name, custom
   label, work directory, ephemeral mode, and disabled updates in source. A
   bounded PTY wrapper disables terminal echo, waits for the exact registration-
   token prompt, sends the short-lived token only to PTY input, and rejects
   unknown prompts or transcript changes before starting `run.sh --once`. The
   token is absent from argv, environment, logs, and persistent image layers.
   Runner state lives only on bounded tmpfs. A private in-container Unix-socket
   handoff carries the token from supervisor stdin to the wrapper. Add a digest-
   parameterized Dockerfile recipe renderer that refuses missing/unpinned inputs;
   do not build it or claim an image exists. Prompt behavior remains a pinned-
   runner-version operational acceptance check.
5. **Fixed-origin OIDC relay.** Implement an HTTPX transport with fixed HTTPS
   origin/path, no redirect/proxy/cookie inheritance, exact ID-only request and
   response schemas, total/body limits, and a supervisor-supplied bounded OIDC
   bearer token. Tests use MockTransport only.
6. Write fake process/registration/capability/relay tests first. Cover command
   construction, prohibited argv/env/mount properties, image/runtime/closure
   pinning and capability mismatch before registration, exact single-job
   registration/cleanup, durable reopen and exact restart recovery, lock
   exclusion, cleanup after create/wait errors and cancellation, token
   non-disclosure, bounded output/time, CLI config rejection, bootstrap manifest
   and PTY-prompt handling via fake sessions, GitHub API success/error/foreign runner
   handling, relay origin/schema/body cases, and inactive template properties.
   Tests never execute generated code, Docker, `runsc`, a runner registration, or
   a live transport.
7. Implement only after the tests: strict config loader/CLI, verified bootstrap,
   concrete HTTPX API clients, capability adapter, and digest-only image recipe.
8. Add negative capability tests for Docker daemon security options that do not
   report rootless or user-namespace isolation, syntactic OIDC JWT shape checks at
   the fixed relay boundary, and cookie-isolation tests proving HTTP clients do
   not carry response cookies into later requests. JWT syntax does not authenticate
   it; signature/audience checks remain at the broker.
9. Run focused pytest, Ruff check/format, and `git diff --check`; inspect the exact
   diff for scope, command/environment/mount leakage, and truthful limits.
10. Completed follow-up source increment: replaced custom `--token-stdin` runner assumptions
    with the pinned standard `config.sh`/`run.sh` pair. Add fake PTY transcript
    tests for split exact prompt matching, token input after prompt only, unknown
    prompt/transcript rejection, token echo rejection, timeout/cleanup, and the
    complete fixed registration argv. Add strict distinct App-role config tests
    and reject Publication/Broker principals at the runner-token provider.

## Acceptance and remaining proof

Local acceptance requires fake tests to prove construction and control flow only.
It does **not** establish Actions compatibility, actual gVisor isolation, denial of
host access, egress enforcement, resource ceilings, cleanup against Docker,
registration revocation semantics, or policy effectiveness against historical or
alternate workflows. Those need a disposable Linux host and repository after
independent approval. The local source increment will include a CLI, bootstrap,
adapters, and an image recipe generator, but no runner image will be built or
registered. The selected runner archive version must be pinned, its interactive
registration prompt verified, and the recipe built with operator-
supplied artifact digests. The repository-specific GitHub App must be provisioned
with reviewed least authority for the fixed `administration:write` request; no
credential is included here. The work did not create an installation token or
grant that permission; distinct runner-App bootstrap review remains pending.
Actual Actions compatibility, gVisor isolation, host denial, egress, resource
ceilings, registration revocation, and cleanup proofs require a disposable Linux
host. No production readiness is claimed from source or unit tests.

## Repair plan: bounded resource and workflow relay integration

**Scope:** continue P09 only in `runner_supervisor.py`, its focused tests, this
plan, and a new `runner_relay.py` with its tests plus an inactive P09 relay job
snippet. Do not edit the assembled merge/deploy workflow or other P08 code.

1. Add failing fake-process tests proving runner container creation passes
   `--memory-swap` equal to `--memory`.
2. Remove generated per-file checksum argv from the image recipe. Keep the
   in-image manifest verifier as authority over the complete file closure,
   enforce the manifest's 4096-file/size limits, and test a closure above 256
   files without proportional Dockerfile argument expansion.
3. Add failing HTTPX MockTransport tests for an Actions OIDC token exchange and
   broker `host-deploy`/`host-reconcile` routes. The command accepts only
   canonical approval and operation UUIDs; origin/audience come from the
   workflow's fixed `RELEASE_CONTROL_BROKER_ORIGIN` and
   `RELEASE_CONTROL_OIDC_AUDIENCE`; request body has only operation ID. Require
   the response to bind that operation to a bounded phase, receipt ID and digest.
   Never print or return OIDC or request-capability tokens.
4. Add an inactive job snippet selecting exactly `chatcore-p09-ephemeral`, with
   only `id-token: write` permission and workflow-provided approval/operation
   IDs. Do not author global concurrency or a complete workflow; root integration
   owns that assembly.
5. Run only fake transport/process tests, Ruff, format and diff checks. No live
   network, Docker, runner, GitHub App or gVisor execution.

### Packaging and implementation result

The relay is a standalone Python standard-library script at
`/opt/p09/runner_relay.py`; the inactive job snippet invokes it by fixed path
with isolated mode (`python3 -I`). `render_runner_image_recipe()` copies this
script and includes its SHA-256 in the immutable runner closure manifest and
image verifier. It does not assume the base image already has the release
control wheel or its dependency tree. The production HTTP adapter uses stdlib
HTTPS, disables proxy inheritance and redirects, and restricts destinations to
HTTPS; tests inject HTTPX MockTransport through an adapter, so HTTPX is not a
runner image dependency.

The recipe relies on the verifier installed in the image to validate every
manifest entry after copying. It accepts the declared 4096-file bound without
expanding a per-file checksum list into Dockerfile arguments. Runner container
creation fixes swap equal to the memory limit. The relay exchanges the Actions
request capability for a short-lived OIDC token, calls only the fixed broker
deploy/reconcile route with the approval and operation identifiers, validates
the operation-bound typed response, and emits only bounded identifiers and
phase/receipt fields. A lost deploy reply triggers bounded reconciliation and,
only after typed not-found, one idempotent deploy retry with the same IDs.

Focused source verification is fake-only: supervisor/bootstrap/relay tests and
Ruff. These tests do not prove pinned runner prompt compatibility, immutable
image construction or signature provenance, Linux gVisor enforcement, egress
policy, resource ceilings, registration cleanup, or host isolation. Those
remain disposable-host proofs; no image was built and no runner registered.
