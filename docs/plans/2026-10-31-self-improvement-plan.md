# Clawdot Platform self-improvement plan

Date: 2026-10-31 (Asia/Singapore). Revision 3, addressing
[Astra's independent review](2026-10-31-self-improvement-astra-review.md).

Status: **Design review passed for detailed implementation planning** in
[Astra Round 3](2026-10-31-self-improvement-astra-review.md#round-3--revision-3-re-review-2026-10-31-asiasingapore).
Implementation and bootstrap acceptance remain pending. This document does not
implement the feature or authorize production installation. Deployment gates below
are requirements, not claims of completed verification.

## Goal and retained decisions

Clawdot proposes, implements and tests a code change in isolation, then opens a PR.
The owner gives **one approval in Telegram DM** covering merge and deployment of
that candidate's complete change from the deployed release. New content or an
unrelated changed baseline requires a fresh card; the release's own recorded
merge/deployment transitions do not invalidate its approval.

Retain the private [keenlok/clawdot-platform](https://github.com/keenlok/clawdot-platform)
monorepo, current Python package/CLI names, GitHub Free, GitHub-hosted CI and merge,
and a self-hosted deployment runner on the production box. Cards expire after
24 hours. Group administrators/moderators cannot release shared code. Initial
generated releases refuse migrations and changes to release controls.

Private .ini/.env files, credentials, production data and backups stay outside
Git and build inputs. Internal timestamps can remain UTC; the parallel task for
displaying local times does not change release authorization.

## Current foundation and verified gaps

The uv workspace has apps/sb_bot, packages/core and adapter/plugin packages, with
one lockfile and Git release revision. The current extension worker runs reviewed
JSON transformations; it does not produce PRs or deploy framework changes.

deploy.sh (`clawdot-deployments/shared/deploy.sh`) archives committed HEAD from a clean
checkout and invokes remote-deploy.sh (`clawdot-deployments/shared/remote-deploy.sh`)
over SSH. The runbook (`clawdot-deployments/shared/README.md`) describes private host
configuration, backups, extension preflight, health checks and rollback.

These operator-driven foundations need changes before automated releases:

- The remote script executes candidate helpers, Compose and build inputs.
- Automatic restoration starts the previous image without waiting for its health.
- Application logs are forwarded and release metadata changes in separate writes.
- ConfirmService consumes a button before its handler; used_at does not record
  whether the user approved or rejected. It is not a release authorization log.
- Existing jobs cover reminders/public-page watches, not authenticated GitHub
  release polling or a transactional release ledger.
- Git/Docker .ini exclusions and Gitleaks pre-commit exist, but are not trusted
  publication enforcement.

## Revised architecture and authority

The central correction is that **the Actions runner has no production authority**.
Reaching its label must not grant Docker or access to private host configuration.

| Component | Responsibility and boundary |
| --- | --- |
| Ordinary bot | Request work and display status through a restricted bridge; no Bot API token, approval-ledger writes, release keys or Docker |
| Protected Telegram bridge / approval broker | Sole Bot API token owner; control ingress, outbound message ownership, release cards/callbacks, ledger and dispatch; authenticate Actions callers |
| Development sandbox | Generate/test with synthetic configuration; no production data, GitHub credentials, runner state or arbitrary egress |
| Publication gateway | Scan immutable outgoing objects and publish clawdot/* PRs with its separate repository-selected App |
| GitHub-hosted CI | Execute bounded candidate tests with read-only source access and no production secrets |
| GitHub-hosted merge job | Verify approval/candidate and merge using pinned control code; never execute candidate code |
| Sandboxed self-hosted runner | Relay authorized deployment/status requests using OIDC; no Docker, production mounts, host shell or deployment secrets |
| Protected host deployment service | Independently authorize, fetch/validate objects, build via fixed policy, deploy and verify recovery |

The broker and deployment service are root-owned systemd installations on the
production box, outside the bot and candidate checkout. Their source may share
the monorepo, but their installed policy changes only through operator maintenance.
Ordinary bot database roles cannot write the release ledger. Postgres stays private.

The **whole runner**, not merely a workflow step, must run in a disposable,
single-job sandbox. Use the existing pinned gVisor capability only after Actions
compatibility and isolation tests pass. Provide disposable scratch and constrained
GitHub/broker egress; no host sockets, production mounts or secrets. A trusted
supervisor owns registration/lifecycle credentials and destroys runner state.
Failure to support this arrangement blocks production; never silently fall back
to an unsandboxed Docker-group runner.

Labels and job-level main/event guards are defense in depth. This is a personal
repository, verified read-only as owner type User on 2026-10-31; organization runner
groups are not assumed. GitHub documents group controls for eligible organizations,
including Free configurations. Verify actual capabilities if ownership changes.
An unwanted workflow might reach the sandbox: the guarantee is denial of host
authority, not an unsupported promise of universal job-admission rejection.
See [runner access](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access)
and [secure use](https://docs.github.com/en/actions/reference/security/secure-use).

Host root, the installed control plane, GitHub/Telegram authentication and sandbox
boundaries are trusted. Owner-approved application code becomes trusted runtime
code and receives its configured application credentials after deployment, excluding
the protected Telegram token and release credentials. Scanning cannot prove
arbitrary approved code will never misuse its app credentials. A separate service survives
bot restarts, but same-box operation is not high availability against host failure.

## Credentials and installed policy

Choose separate repository-selected **GitHub Apps** for publication and release
coordination. Keep private keys in protected services, never the worker, CI or bot.
Installation tokens are short-lived and need renewal/rotation handling, rather
than being permanent credentials. See
[installation tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).

| Principal | Necessary permissions | Exclusions |
| --- | --- | --- |
| Publication App | Contents write, Pull requests write, Metadata read; this repository only | Workflows, Actions, Checks write, Administration, deployment |
| Broker App | Actions write/read; Contents, Pull requests, Checks and Metadata read | Contents/PR write, Workflows, Administration |
| CI token | Minimum source read required | Writes, production secrets, release authorization |
| Merge job token | Contents write, Pull requests write; OIDC acquisition | Candidate execution and production access |
| Deploy relay job | OIDC acquisition; no repository write | Production credentials, Docker, private config, raw logs |

Candidate PR identity comes from the gateway record, not trusted labels; labels
are optional display metadata and need no Issues write permission. Validate the
endpoint/permission matrix using the actual Apps in a disposable repository.
Do not substitute classic PAT/OAuth repo scope. Workflow-file restrictions differ
by credential type and edge case; they are defense in depth, not branch protection.

Actions write is broader than dispatch-only. The broker only dispatches release.yml
on main. A stolen dispatch token cannot create approval or satisfy host admission.
The merge job never imports candidate Python, runs local candidate actions/hooks,
installs candidate dependencies or executes downloaded candidate artifacts.

A versioned **installed policy manifest** records numeric repository/owner IDs,
target, allowed workflow blobs and complete helper/action dependency closure,
trusted checks, allowed source files, protected paths, build/runtime inputs and
migration identity. Pin external Actions to full commits. At OIDC workflow_sha,
verify the entire policy closure; a matching filename/main ref is insufficient.

Protect .github, deploy/ingress/auth/approval code, Compose/overlays, Dockerfiles,
.dockerignore, build backends/package metadata, lockfile, health/preflight helpers,
scanner configuration and trusted regression harnesses. Inventory all transitively
executed modules. Initially allow only enumerated ordinary plugin Python files
and their tests outside that closure; a broad packages/** rule is insufficient.
Dependencies, symlinks/submodules, binaries and uninspectable changes go through
maintenance. Candidate tests supplement protected independent checks.

## Immutable publication and secret prevention

Receive a content-addressed object bundle in a separately owned bare/quarantine
repository. Use fixed remote/configuration, disabled hooks/filters/helpers and
no worker-controlled credential settings. Freeze object IDs before scanning and
push exactly those IDs to an explicit clawdot/* refspec. No force/mirror/tags,
worker-selected remote or mutable post-scan ref resolution.

Inspect every newly reachable commit/blob and commit message/metadata. A secret
added and removed in intermediate history still blocks publication. Scan initial
baseline/history once; incremental checks cannot remove already published secrets.
Any actual exposure needs separately recorded revocation/rotation and remediation.

Reject case-insensitive .ini, private .env variants, backups/credentials and
protected control inputs, including renamed/copied content. Treat the synthetic
.env.example as an explicit reviewed exception. Scan PR prose, patch context,
artifacts, summaries and logs before upload. Detected source secrets block release;
do not approve an incomplete redacted source diff. Permitted diagnostic redaction
must be visibly marked as incomplete evidence.

Candidate execution has default-deny network egress. Only the trusted controller
can send necessary source/specification to the configured, owner-authorized LLM.
Dependency acquisition is a separate pinned/cache-verified operation. Do not give
tests production data, arbitrary package/network access or GitHub credentials.
Test leakage with synthetic sentinels. Scanning and isolation cover different risks.

This applies to candidate execution in hosted CI as well as the development worker.
Trusted CI orchestration fetches source without persisted Git credentials, then
runs candidate code in a separate egress-restricted sandbox with no Git/OIDC token,
runner home, Actions command/output channels or host sockets. Only trusted code
validates and uploads bounded, scanned reports. Read-only GitHub credentials still
expose private source and are not safe to hand to untrusted tests.

## Authentic owner approval and release ledger

The protected Telegram bridge is the **sole holder of the Bot API token** and
creates release cards itself. Receive HTTPS webhooks with a dedicated secret
header; check numeric owner identity, private chat, exact broker-issued message
and callback nonce. Consume release updates before forwarding ordinary updates
through the authenticated relay. Never accept approval from ordinary bot RPC,
forwarded JSON or application DB writes.

All ordinary sends, edits, deletes, callback answers and media operations use a
bounded, authenticated, method-allowlisted bridge API. Track app-owned message IDs
and permitted chats; app requests can mutate only those messages. Deny edits,
deletion, copying or markup replacement of release messages, release callback
namespace/button creation, webhook changes and token administration. Proxy files
without returning token-bearing URLs. Known release chat/message IDs give the app
no authority to change the card.

Keep token, ingress secret, owner policy and ledger credentials outside the app.
Maintenance rotates the formerly app-held token, proves the old token unusable,
and removes it from app environment/config snapshots. Migrating polling and
outbound adapters to this bridge is an explicit maintenance/bootstrap task, with
ordinary chat/file/confirmation compatibility tests, not existing behavior.
See [Telegram webhook authentication](https://core.telegram.org/bots/api#setwebhook).

A card binds PR, full head H, base B, head tree, target, last healthy deployed
baseline D, installed policy digest, current owner, issuance/expiry and fresh nonce.
The deployment identity D includes Git SHA, image digest, configuration generation
and digest, installed policy digest and target sequence, not Git SHA alone.
The card shows the **full D-to-H release delta**, separately identifying inherited
D-to-B changes and the PR's B-to-H changes. Bind that complete review bundle and
its verified checks/provenance to the decision. Model prose is
untrusted, capped plain text with previews disabled. Test counts must come from
trusted run-bound evidence; example UI text is not verification. Abbreviated UI
SHAs never replace full IDs in authorization.

For initial ordinary PR releases require B to be the recorded deployed commit or
a verified same-tree continuation with recorded provenance. Unknown undeployed
main content, divergent/rewritten history and earlier merged-but-failed changes
block an ordinary approval card. Resolve them through maintenance or an explicit
reconciliation card covering the full deployed-to-intended-commit delta, with the
same policy/review requirements. Never silently fold them into a small PR. Path,
migration, scan and evidence checks cover the full release delta and relevant
introduced history, not just the PR. A previous green PR is not approval for its base.

Record **Approve or Reject explicitly**. Deduplicate Telegram update IDs and commit
the decision, nonce consumption and release outbox entry in one DB transaction
before acknowledging the callback. Generic pending used_at is never approval.
Uncertain card-send responses require message-ID reconciliation or replacement
with a fresh nonce before becoming approvable. Repeated taps return the same state.

Use a dedicated release schema/DB role, not ordinary bot write privileges or
generic public-URL watches. Bootstrap schema through maintenance; later generated
releases still refuse migrations. Persist B/H/tree/D/policy, owner decision/expiry,
per-target sequence, GitHub run ID/attempt, lease/fence, merge M, host receipt and
terminal outcome. Typed GitHub polling handles authenticated run status.

States include awaiting approval, rejected, expired/invalidated, queued, merge
authorized, merged verified, merged blocked, deploying, healthy, restoring,
restored healthy and recovery failed. Persist cancellation and uncertain results.
One logical result is the guarantee; reconcile external effects rather than
claiming network operations happen exactly once.

Apply the phase-specific guards below. Expiry/revocation prevents a new side-effect
admission, but does not undo a completed merge, invalidate a completed receipt or
interrupt safe reconciliation/recovery. The authorized B-to-M and D-to-M transitions
are expected effects, not unrelated baseline changes. Polling does not prove instant
invalidation or detect force-push away/back without event history.

## OIDC-authenticated release protocol

Expose narrow HTTPS endpoints at the maintenance-configured broker origin.
Postgres is not exposed. No verifier/DB secrets belong in Actions. Accept bounded
schemas with fixed repository/target policy, not URLs, shell commands, executable
paths, environment payloads or deployment hosts.

Validate GitHub OIDC issuer/JWKS signature/algorithm, audience, issued/not-before/
expiry, numeric repo/owner IDs, private visibility, workflow_dispatch, main ref,
workflow_ref/sha, run ID/attempt, check_run_id and runner environment. Correlate
job identity to the approved workflow/run: hosted for merge, self-hosted for relay.
job_workflow_ref applies to reusable workflows, not all jobs. Verify the installed
policy closure at the claimed SHA. See
[GitHub OIDC claims](https://docs.github.com/en/actions/reference/security/oidc).

OIDC authenticates the caller, not the owner's decision. The broker independently
resolves approval/GitHub state and atomically claims a fenced release bound to
candidate, run and attempt. Narrow short-lived receipts bind one release/operation/
fence; IDs and tokens cannot authorize another release. Reruns require reconciliation
and explicit rebinding, not reuse of an expired or revoked approval. The host
service verifies current broker authorization independently of workflow outputs.

GitHub, verifier, policy/identity or ledger outages fail closed. Return bounded
structured results and receipt IDs, not credentials, raw exceptions or app logs.

## Phase-specific admission and retry rules

| Phase | Guard, effect and retry contract |
| --- | --- |
| Before merge | Under the target lock require affirmative unexpired approval, current owner/policy, current main B and PR head H, expected deployed/config identity D, latest target sequence and full-delta evidence. Grant a bounded merge authorization tied to the run/fence. |
| Merge uncertain or complete | Reconcile immutable M, PR result and expected B/H parents/tree. The authorized main transition from B to M is valid. Never remerge a closed PR or apply a pre-merge main-equals-B rule after success. Unexpected results become merged blocked. |
| Preparing build | Preparation grants no container-switch authority. Validate M and policy, produce a verified image and retain D. Expiry, revocation or maintenance during a long build can still deny switching. |
| First host switch admission | Under the host lock, the broker atomically admits one deploy operation against the current fence/sequence, still-valid approval and actual deployed image/config/policy identity. Require current main M at this admission; unrelated later main movement blocks admission. Bind an operation ID, baseline, payload and bounded start deadline, then durably journal before switching. |
| Admitted switch and recovery | Revocation/maintenance serialize against the same admission record. Before admission they deny switching; after admission only that already-admitted operation and bounded safe recovery may finish. No fresh attempt or arbitrary configuration restore follows from its receipt. |
| Lost result or completed operation | Reconcile that same operation/receipt against journal and actual containers. Expected D-to-M change is not staleness. Return the recorded outcome without another merge, switch or tap. Starting a genuinely new switch after expiry/revocation needs a fresh reconciliation card for intended M and current baseline. |

The broker's atomic admission transaction is the switch authorization linearization
point, not the start of a build or a relay job. The host reports its actual baseline
under its lock; the receipt binds that baseline and a bounded start window. If a
crash occurs before any switch and that window expires, reconcile the operation
without starting a late switch. Once switching began, finish or restore only the
same journaled operation under its bounded recovery policy. Neither a timed-out
lease nor a lost reply proves that no switch occurred.

Configuration maintenance uses the host lock and advances its generation/digest.
Unexpected out-of-band changes invalidate admission/restoration until reconciled.
Never overwrite newer configuration to satisfy an old release. A completed receipt
can be reported after maintenance without reverting that maintenance.

An old hosted job may retain GitHub write authority after a lease expires; broker
fencing cannot retroactively fence GitHub's merge API. Preserve the merged-blocked
residual for late external effects. Current-main checks occur at their specified
admission points; a later ref change never replaces the already bound payload M.

## Trusted CI, merge and deployment flow

1. Capture the explicit request/specification; generate/test in isolation.
2. Publish the exact scanned objects as a PR through the gateway.
3. Verify required CI/check provenance and independent review for candidate B/H.
4. The broker sends its revision-bound card; one owner tap records the decision.
5. The broker dispatches release.yml on main with PR number, full H and approval ID.
6. The GitHub-hosted merge job authenticates, claims the release, revalidates it,
   merges and verifies the resulting M before making deployment eligible.
7. The sandboxed self-hosted job relays M/release receipt to the host service.
8. The host independently verifies policy, objects and baseline, deploys M, checks
   health/smoke, recovers if needed, and journals the actual result.
9. The broker polls typed run/host status and reports in DM across bot restarts.

CI validation checks creator App AND expected workflow/version, repository/event,
B/H/tested tree, run and latest relevant attempt. Paginate all results. Missing,
skipped, cancelled, ambiguous, stale or API-failed evidence blocks release.
Names or the Actions App ID alone are insufficient. Map synthetic merge checks
to their exact B/H/tree. See [check runs](https://docs.github.com/en/rest/checks/runs).

Use a per-target broker lock, require B ancestor of H, and call the merge API
with sha=H and explicit merge_method=merge. Resolve M's parents/tree against B/H
in the GitHub-hosted job before admitting deployment; repeat verification on host.
Deploy the recorded immutable M, never a newly resolved main/HEAD.

The [merge API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)
guards head, not an expected base. Route normal writers/maintenance through the
same lock, but Free/private Contents credentials can still write main directly.
If B moves during merge, an unexpected commit can already be merged. Record
**merged blocked**, deny deployment, and require operator reconciliation. Do not
auto-revert, force-push or claim nothing merged. This disclosed residual means
atomic main protection against a compromised writer is not guaranteed; unapproved
content still cannot deploy. Eliminating it requires separately reviewed write
controls/publication mechanics, not an invented API base guard.

Compare migration/policy/content and review evidence over actual D-to-H, including
undeployed main changes. Reconcile B-to-M and D-to-M according to the phase table;
reject unrelated baseline changes and stale run attempts/sequences. Main advancing
after an admitted operation cannot substitute a different payload for M.

## Durable queueing and crash reconciliation

The broker ledger/outbox is authoritative. Dispatch one active release per target.
Record API-returned run correlation when supported, otherwise bind approval ID
to the authenticated run/attempt claim; never guess the latest run.

Use workflow-wide production concurrency, cancel-in-progress false, covering
merge and deploy. Validate queue:max support in the chosen syntax/API version.
Default concurrency can replace pending work; larger queues have overflow and
ordering caveats. Observe cancellation and reconcile/requeue the same logical
release, never lose its decision. See
[Actions concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

Reconcile lost dispatch/merge/deploy responses from GitHub state, recorded M and
host receipts before repeating effects. Maintenance takes the same host lock and
updates baseline/sequence. The host also uses an OS lock and current broker fence:
stale leases or an old successful workflow cannot overwrite a newer healthy
release. A killed relay does not duplicate or cancel service-owned host work.

## Protected deployment and truthful recovery

Only the installed host service reaches Docker. It accepts release/receipt IDs,
fetches objects independently, validates fixed repository, policy, baseline and
migration manifests, and never executes candidate-selected host commands.

Use installed Compose/manifests/helpers, fixed services/networks/volumes and
pinned build backends/dependencies. Validate modes, extraction boundaries,
symlinks/submodules, sizes and ownership. Build in bounded disposable isolation
without production env, Docker sockets or host mounts; the service controls the
context. Candidate Dockerfile/.dockerignore cannot define the security boundary.
Only service-verified images for M/policy can run, not caller-supplied image digests.

Require migration-set equality with the last deployed release, covering additions,
deletions, renames and loader changes. Reject protected paths through outgoing
history too. Undeployed migrations on main are not an exception. No automatic DB
restore or unrelated-service restart.

Keep production config/snapshots outside source/releases/build inputs. Retain the
last healthy image and compatible config/source/policy manifest until installed
health AND smoke checks pass. Restore only the recorded compatible configuration;
do not overwrite newer unrelated settings. Health policy cannot be weakened by
the candidate. Runtime app code remains trusted after owner approval.

Use an atomic release journal: prepared, switching, checking, healthy, restoring,
restored-healthy, recovery-failed. Reconcile actual containers after a crash.
Restoration must wait for old-image health/smoke; never suppress its failure and
report success. Bot outages do not erase approval/results.

Bootstrap must measure host capacity and set explicit CPU/memory/PID/time/output,
disk/cache and retention limits, reserving capacity for the running bot and shared
services. Guessed limits are not acceptance evidence. Add a protected watchdog
and Tailscale/console break-glass runbook; host failure still needs external help.

Do not forward raw production application logs to Actions. Output only allowlisted
release/SHA/policy/receipt IDs, enumerated phase/error and health/recovery status.
Keep bounded sensitive diagnostics on host; redaction is secondary protection.

## Free-plan limits and production enablement

Do not rely on paid private branch/ruleset/environment controls. Contents authority
is not branch-scoped. Main-only YAML does not make repo secrets main-only; secret
availability also depends on event. No production deployment secrets go into Actions.

Pro may add private branch protection and environment/deployment-branch features,
but does not supply every private reviewer/wait-timer rule. Preserve broker owner
approval after any upgrade. Check actual account features at bootstrap; see
[environment availability](https://docs.github.com/en/rest/deployments/environments).

Before installation the owner reviews the concrete policy manifest, ingress/relay
compatibility, broker network origin, sandbox/resource evidence and disclosed
main-write/same-box residuals. This bootstrap decision is not a second approval
for ordinary PRs. Editing the plan does not mark residual risks accepted or
authorize an organization transfer, paid upgrade or public repository.

## Staged delivery

1. **PR-only:** implement isolated generation, immutable publication, selected Apps,
   secret/egress policy and trusted CI/review. Keep release disabled.
2. **Disposable maintenance bootstrap:** install broker schema/outbox, Telegram
   ingress/relay, OIDC verifier, policy, sandboxed runner and host service in a
   disposable environment. A production runner is not a harmless dry-run install.
3. **Failure-injected rehearsal:** use actual Free/private/App configurations in a
   disposable repo/host for races, permissions, isolation, resources and recovery.
4. **Owner-approved bootstrap and narrow production trial:** install only after
   evidence passes; release only enumerated ordinary plugin changes. Broader core,
   dependency, migration/control changes and automatic approval are separate work.

## Acceptance and review disposition

| Criterion / finding | Required evidence |
| --- | --- |
| SI-01 / A01 | Branch/PR/historical/alternate workflows and pre-step containers cannot leave disposable runner isolation or access Docker/config/host authority. Verify cleanup and fail-closed startup. |
| SI-02 / A02 | Test actual App Git/API writes, workflow edit/copy/delete, transitive helper changes, direct-main writes and token renewal. Record the real permission boundary and installed-policy rejection. |
| SI-03 / A03 / R2-01 | Forged/rejected/foreign/expired/revoked approvals, wrong sender/message, bot RPC/DB forgery, wrong OIDC/workflow and verifier outage cannot release. A hostile app cannot edit/delete broker cards, replace markup, mint release buttons, change webhooks or obtain tokens/file URLs; old token rotation is proved. Ordinary chat/media operations still work. |
| SI-04 / A04 | Inject main/head movement and merge conflicts, including same-tree/different-history bases and force-push away/back. Unexpected results become merged-blocked and never deploy. |
| SI-05 / A05 | Foreign/same-name/wrong-workflow checks, stale success/new failure, skipped/missing jobs, synthetic-merge mismatch and pagination fail closed; known run/tree evidence passes. |
| SI-06 / A06 / R2-02 | A/B/C dispatch, cancellations/overflow, old reruns, lost responses and expired leases retain one result. Normal B/H-to-M needs one tap; post-merge/healthy retries return receipts. Test revoke/expiry/config maintenance immediately before/after switch admission, expired start windows and crashes at both journals; no stale switch/config restore. |
| SI-07 / A07 | Crash at decision/outbox/ack boundaries, replay rejection, owner removal, bot restart and GitHub-token rotation retain the explicit choice and typed run watch. |
| SI-08 / A08 | Compose mounts/privilege, helpers/backends/Dockerfiles, symlink escape, arbitrary images and undeployed-main migrations fail before privileged use/secrets. Allowed content deploys through installed inputs. |
| SI-09 / A09 | Intermediate-commit/metadata/context secrets, INI/env/backup renames, poisoned Git hooks/config, ref TOCTOU, binary diffs and network sentinels cannot leak; push exact scanned objects. Exercise candidate-hosted CI as well as the development sandbox; neither can obtain runner credentials, command channels or unapproved network access. |
| SI-10 / A10 | Kill each component around journal boundaries; simulate disk/OOM/new-health/old-health failures. Assert truthful recovery, protected diagnostics, previous artifacts and no DB/unrelated-service reset. |
| SI-11 / A11 | Map each required control to actual account/plan/token support; no reliance on unavailable paid protection. |
| SI-12 / R2-03 | One tap approves the full deployed-to-candidate delta. Inject allowlisted unknown D-to-B code before a benign PR; refuse the ordinary card/switch. Test earlier merged-failed changes, same-tree history, rewritten main and explicit reconciliation. No PR-only diff hides inherited code; group roles cannot approve; existing bot/JSON extensions remain compatible. |

All eleven initial findings and R2-01 through R2-03 have corresponding design
corrections and acceptance gates. Revision 3 chooses the sole-token protected
bridge and refusal of unexplained undeployed-main content as narrow design defaults;
it does not add another routine owner approval or claim prototype tests passed.
They are not claims of implementation or passing tests. Preserve Astra's initial
report as historical evidence and append a re-review with this revision's digest.
After design review passes, split implementation into owned tasks with exact
files, tests, bootstrap commands and rollback procedures.
