# Independent review of the self-improvement plan

Date: 2026-10-31 (Asia/Singapore). Verdict: **CHANGES_REQUIRED**.

The plan is a sound direction for a constrained prototype, but is **not implementation-ready as an end-to-end production release specification**. The approval verifier, runner admission boundary, merge race policy, and release transaction/recovery contract must be designed first. PR-only work can proceed after the publication and worker isolation contracts are fixed; installing a privileged production runner is not a harmless dry-run step.

This review preserves the private `keenlok/clawdot-platform` monorepo, one owner approval in Telegram DM covering merge and deployment, GitHub Free, GitHub-hosted CI/merge, and a production-box self-hosted deploy runner. It does not substitute a paid plan, second approval, public repository, or external deployment service. Alternatives requiring a decision are explicitly marked below.

## Evidence and scope

Reviewed the actual untracked `docs/plans/2026-10-31-self-improvement-plan.md` in the main checkout, not a Git copy. Its SHA-256 at review start was `bd6636500d52e10b6036ff287dc9ca8ef4e138c9cc7197b28487d0ecd031bc42`; HEAD was the supplied `0568954a14575625c42375e581343c22169a9e63`. All plan line references below refer to that file. Read ARCHITECTURE.md security/execution guidance, both current handovers, deployment scripts/runbook/Docker/Compose inputs, confirmation and owner-administration code, and reusable jobs models/schema/worker/watch implementation. There is no `.github` directory in this checkout; workflow behavior described in the plan is proposed, not implemented.

Official GitHub documentation was checked during this review; links accompany the relevant claims. No production access, GitHub mutations, pushes, commits, or application tests were performed. These are design findings, not claims of a demonstrated production exploit. **The approval card's “1131 passed” and “2 findings, both fixed” are illustrative sample text, not current test or review evidence.**

Severity: critical means a missing boundary can expose production authority; high means approval/release correctness or confidentiality can fail; medium means a material operational or specification gap. “Confirmed” identifies a contradiction supported by code/documentation; “missing design” identifies a necessary control whose absence in the plan is established, without asserting its eventual implementation will be vulnerable.

## Findings

### A01 — Critical: runner admission is not established by a label or first step

**Location:** plan:72–76, 200–209, 221–230, 298–302, 328. **Classification:** confirmed insufficiency of the stated controls; account-specific admission capabilities unverified.

**Failure:** a first-step ref check runs after GitHub has assigned the job to the production runner. Job preparation can precede steps, including container/service setup. A dedicated label is routing metadata, not authorization. If any executable workflow targeting that runner is admitted, the runner user's Docker access is already a host-root boundary; the absence of deployment secrets in Actions does not help. The exact current `release.yml` with a correct guard is not itself evidence of an exploitable branch workflow: the missing piece is proving which workflow versions can be scheduled, including historical versions and maintenance changes.

**Evidence:** GitHub documents labels/groups as runner selection, and warns that self-hosted runners can be persistently compromised by untrusted workflows. Current docs explicitly allow runner groups for **Free organizations**, and describe selected workflows pinned to refs; it would be wrong to claim Free universally lacks this feature. These are organization controls, not an established capability of this repository's as-yet-unverified account configuration. [Runner selection](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/use-in-a-workflow), [runner group access](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access), [secure use](https://docs.github.com/en/actions/reference/security/secure-use).

**Correction:** replace “only ever executes” with an explicit admission mechanism and threat model. Where available, restrict the runner group to this repository and the exact deployment workflow/ref, verify the policy, and use job-level `if` guards before allocation as defense in depth. Keep deploy jobs free of untrusted containers, services, local actions and checkout hooks. For a personal repository-level runner without equivalent server-side workflow restrictions, choose between (a) trusting the immutable workflow/publisher boundary with a clearly narrower conditional guarantee, or (b) changing account/runner registration arrangements to obtain enforceable admission. Do not claim an unconditional “no branch job reaches the host” invariant from labels. A host wrapper alone cannot make arbitrary jobs safe when the runner user already has Docker access.

**Acceptance test:** in a disposable repository and disposable host with the selected account plan, attempt non-main dispatch, historical workflow dispatch, another workflow using the label, PR jobs, and job-level service/container setup. Prove rejected jobs never allocate or execute on the host. Install the production runner only after this evidence exists.

### A02 — High: the workflow-permission barrier is overgeneralized across credentials

**Location:** plan:65–68, 210–224, 232–237, 324–327, 359–360. **Classification:** confirmed wording defects; selected-token Git transport edge cases require validation.

**Failure:** implementation could choose a broad classic PAT, assume “no workflows permission” protects all refs/paths, or reuse a workflow already present elsewhere. Even unchanged workflow YAML can execute modified local scripts/actions from a writable branch/main. Accepting direct main pushes while claiming the deployment trust chain is unchanged omits these transitive inputs.

**Evidence and credential distinctions:**

| Credential | Relevant boundary |
| --- | --- |
| Classic PAT / OAuth token | `repo` is broad private-repository authority, not a dispatch-only credential. The scope is singular `workflow`. GitHub explicitly documents an exception for a workflow with identical path/content already on another branch. Do not use this type for the narrowly scoped bot. [OAuth scopes](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps) |
| Fine-grained PAT | Repository-selected Contents/PR write and Workflows write are separate permissions. Contents REST documentation distinguishes workflow-file updates. Verify actual Git receive behavior for the selected token; do not extrapolate every classic-token exception as confirmed behavior for this type. [Contents API](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents) |
| GitHub App installation token | GitHub documents Contents for HTTP Git and Workflows for editing Actions files. Restrict installation and minted token permissions separately; keep the App private key out of bot/worker processes that need only reduced tokens. Installation tokens expire after one hour, so “no personal expiry” is not “no credential lifecycle.” [App permissions](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app), [installation tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app) |
| `GITHUB_TOKEN` | Per-job repository authority, not the gateway token. Actions may access it implicitly. Keep CI read-only and give merge-only jobs write access; never run candidate Python, local actions, hooks or dependencies in that privileged job. It is not a branch-scoped authorization mechanism. [Token authentication](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token) |

**Correction:** select a concrete token type and publish an endpoint/permission matrix, including Checks read, Actions read/write, PR read/write, Contents read/write and any label permissions actually needed. Deny Workflows, administration and unrelated permissions. For App private keys, use distinct Apps or a trusted token minting boundary so a compromised controller cannot mint the gateway's permissions. Protect all transitive release/CI policy inputs, not just `.github/**`. State that leaked Contents credentials can alter main and trusted code outside YAML; deployment remains safe only if an independent verifier/installed policy rejects that altered trust chain.

**Acceptance test:** against a disposable repository, test new/modified/deleted workflow files, identical-existing workflow copies, old refs, API versus Git writes, protected local-action/script changes, and direct-main push. Record actual allowed operations for the chosen token; do not perform this destructive matrix in the live repository.

### A03 — Critical: dispatch inputs do not authenticate owner approval

**Location:** plan:65, 92–93, 116–120, 184–192, 200–212. **Classification:** missing design, release blocker.

**Failure:** a syntactically valid or replayed `approval_id` is not proof of a click. The GitHub-hosted merge job cannot query private Postgres merely because the approval is stored there. A shared verifier secret in repository secrets would be available to eligible branch workflows. A stolen bot dispatch token can start/retry runs without a Telegram interaction; GitHub has no dispatch-only scope restricted to this workflow/ref. Actions write also includes other workflow operations. [Workflow API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event).

**Correction:** specify a narrow HTTPS approval verifier/broker reachable from GitHub-hosted jobs, with Postgres remaining private. Authenticate jobs using GitHub OIDC and validate issuer, signature, audience, expiry, repository/owner IDs, exact workflow ref and approved workflow identity, main ref, event, runner environment, run ID and attempt. Reject branch/PR identities and policy-changing main commits. GitHub supplies these claims; `job_workflow_ref` applies to reusable workflows, whereas ordinary jobs use `workflow_ref`/`workflow_sha`. [OIDC claims](https://docs.github.com/en/actions/reference/security/oidc).

The broker must atomically claim a recorded approval bound to repository ID, PR, full head SHA/tree, approved base SHA, target, owner identity, policy version and expiry, returning only a narrow authorization. A replay resumes the same release, not a new authorization. Recheck revocation and candidate state at claim time. Keep this authority outside ordinary bot-authored code and outside ordinary bot database-write privileges, or explicitly accept that compromise of the bot can fabricate approval. No direct Postgres exposure is needed. An outbound bridge with signed authorization and a separately designed online claim/revocation ledger is another option; a signature alone does not give single use or revocation. Broker hosting/connectivity and trust ownership need an owner decision.

**Acceptance test:** forged/random ID, valid ID for another repo/head/base/target, expired/revoked owner, branch OIDC, wrong audience, modified workflow identity, two simultaneous claimants, rerun attempt and lost response all fail closed or resume the same recorded release. Assert no Postgres port/credentials become accessible from CI. A verifier outage must prevent merge.

### A04 — High: head pinning does not close the base race

**Location:** plan:118–136, 193–196, 332–334, 353–354. **Classification:** confirmed API/design race.

**Failure:** validate `main=B`, with B an ancestor of approved H; another writer advances main to C before merge. The API's `sha=H` still matches. GitHub can merge H onto C, producing an unapproved tree. The later tree check stops deployment but cannot undo the already completed merge. A force-push away from H and back also defeats a literal “any new commit immediately voids” policy if only current SHA polling is used.

**Evidence:** the merge endpoint documents a head SHA guard, no expected-base SHA parameter. [Merge API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request).

**Correction:** record B and H at approval, choose `merge_method: merge` explicitly, verify ancestry, parents and tree before any production execution, and reject/re-card changed bases. Move the merge-result tree check into the GitHub-hosted job before making the deploy job eligible; independently verify it on the host too. Serialize all normal main writers through the same release authority. Free/private plus an accepted stolen gateway credential capable of direct main pushes cannot guarantee atomic “nothing unapproved ever merges” using this API. Explicitly accept “unexpected base can leave a merged, non-deployed commit and requires reconciliation,” or choose a separately reviewed compare-and-swap publication mechanism / enforceable write restriction. The latter changes the chosen merge mechanism or platform controls and needs approval. Do not silently adopt it. Define invalidation as current revision binding unless reliable event history is also maintained.

**Acceptance test:** inject an unrelated main commit between validation and API merge; verify no deployment is scheduled and report the actual merged-but-blocked state. Test head movement, base movement, same-tree/different-history base changes, force-push away/back, merge conflict and a normal merge whose tree equals H.

### A05 — High: check names and green status are insufficient provenance

**Location:** plan:109–114, 118–120, 171–174, 229–230, 362. **Classification:** missing validation contract.

**Failure:** a same-named success from another App/workflow, stale rerun, skipped job, partial paginated result, or test report authored by the candidate can be accepted as authoritative CI. A PR workflow commonly tests a synthetic merge revision; calling that “checks on head” without identifying what was executed can either block good PRs or approve the wrong tree.

**Evidence:** checks expose creator App, head SHA and check-suite identity; commit statuses have independently supplied contexts and creators. Names alone are not provenance. [Check runs](https://docs.github.com/en/rest/checks/runs), [commit statuses](https://docs.github.com/en/rest/commits/statuses).

**Correction:** define required check identities and trusted workflow versions, expected event/repository/head/base/tested tree, run/attempt selection and success-only conclusions. Correlate check suite/run to the expected Actions workflow; GitHub Actions App ID alone is not enough to distinguish workflows. Paginate exhaustively and fail closed on ambiguity or API failure. Record test counts from a trusted report bound to that run, not PR prose. Protect validation scripts/configs and critical tests against ordinary self-improvement changes, or explicitly require independent trusted tests of candidate behavior.

**Acceptance test:** same-name foreign App/status, wrong workflow from the same Actions App, old successful attempt plus newer failure, skipped/missing/cancelled check, PR merge SHA mismatch and more than one page of checks all fail; a known valid run succeeds.

### A06 — High: concurrency and the head guard do not provide release idempotency

**Location:** plan:189–192, 338–339. **Classification:** confirmed platform behavior plus missing transaction design.

**Failure:** three dispatched releases can replace the pending middle run even with `cancel-in-progress: false`. A rerun can deploy an old merge after a newer release, or duplicate deployment after a lost result. The head guard is a precondition on merging, not an idempotency key for the entire release.

**Evidence:** current GitHub docs specify a default single pending slot; `queue: max` permits up to 100 pending entries, with overflow cancellation. Ordering is by entry into the concurrency wait, not necessarily dispatch order. [Concurrency documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

**Correction:** use a durable release state machine/outbox with unique approval/candidate/target identity, fenced claims, recorded run IDs/attempts, merge SHA, deploy receipt and terminal outcome. Reconcile after network uncertainty before repeating effects. Put concurrency around the entire merge/deploy transaction, explicitly choose queue behavior, and retain a host lock plus stale-release rejection. Serialize maintenance deployments through the same host lock. A cancellation is observable state to reconcile, not a lost request or proof of failure. If the API version supplies a dispatch run ID, record it; otherwise define correlation explicitly rather than guessing the latest run.

**Acceptance test:** dispatch A/B/C, cancel pending B, rerun A after C, lose dispatch/merge/deploy responses, kill a runner after container replacement, and expire a worker lease while work continues. Each authorization has one logical result; old attempts cannot overwrite a newer healthy deployment.

### A07 — High: existing confirmations and jobs are foundations, not a release ledger

**Location:** plan:70, 116, 127–129, 184–192, 298–300. **Classification:** confirmed implementation gap.

**Failure:** `ConfirmService._resolve` consumes the pending action before calling the handler (`packages/core/src/chatcore/confirm.py:76–86`; `store/pg.py:254–265`). A crash in that gap loses the intended release; a second tap returns expired. The claim operation does not store whether the user confirmed or rejected, so `used_at` cannot be treated as approval evidence. Existing job kinds/schema only support reminders and watches (`chatcore_jobs/models.py:34–36`, migration `0001_jobs.sql:20–47`). Watches fetch public text through `safe_get` (`watch.py:74–81`), not authenticated private GitHub run JSON.

**Correction:** atomically persist affirmative owner approval and a release outbox entry with consumption, recording the choice explicitly, or introduce a release-specific recoverable confirmation transaction. Reject/group-admin/DM validation belongs in the release handler; generic requester matching is insufficient. Use the administration handler's scope checks (`administration/plugin.py:356–367`) as a pattern, not delegated-admin authorization. Set 24-hour expiry explicitly; the generic default is ten minutes. Build a typed GitHub run poller and release ledger using reusable scheduling primitives. Never put authenticated GitHub tokens into generic watch URLs/payloads. Bootstrap any needed schema in a separately approved maintenance release; initial self-improvement candidates still refuse migrations.

**Acceptance test:** crash after button consumption but before enqueue; reject then replay its ID; concurrent taps; owner removal between card and tap; bot restart during deploy; private GitHub API outage and token rotation. Recovery retains the recorded decision and never converts a rejected/merely-used pending action into approval.

### A08 — Critical: protecting the entry script alone leaves transitive host execution

**Location:** plan:84–88, 123–126, 207–209, 245–255, 257–260. **Classification:** confirmed integration risk; exact allowlist missing.

**Failure:** the current remote script executes a helper from the candidate release with the private env path (`remote-deploy.sh:124–129`), feeds candidate Compose files to Docker (`:200–209`), and builds the candidate Dockerfile/package metadata. A changed Compose mount can expose `/`, Docker socket or `.env`; build inputs can run code or fetch malicious dependencies. Moving only the shell entry point to a host installation does not remove these paths. Docker build does not automatically expose the whole host, so a host escape from a normal Dockerfile is not assumed here.

**Correction:** enumerate the entire protected execution closure: Compose and overlays, Dockerfile, `.dockerignore`, build contexts, helpers, health/preflight scripts, local Actions, package/build backends and dependency policy. Use installed/pinned manifests and helpers, fixed project/services/networks/volumes, clean staging, sanitized environment and no candidate-selected paths. Validate Git modes/symlinks/submodules and extraction boundaries. Enforce migration-set identity against the actual deployed release, including additions/deletions/renames and migration loader changes; comparing only candidate-versus-PR-base SQL misses undeployed main migrations. Ordinary approved application code necessarily receives runtime credentials after deployment; define that owner-approved code becomes trusted at this point rather than claiming arbitrary approved code cannot access them.

**Evidence:** `apps/sb_bot/compose.yaml:5–13` supplies build paths and private env; Dockerfile runs `uv sync` over candidate package metadata; `remote-deploy.sh:14–25,134–143` detects SQL manifests but does not refuse changes. These are valid operator-driven deploy inputs today, not proof the proposed automated trust boundary already exists.

**Acceptance test:** candidate Compose host mount/privileged mode, Dockerfile exfiltration probe, helper replacement, alternate build backend, symlink escape, changed lockfile policy and undeployed-main migration all fail before privileged use or secret loading. A permitted application-only candidate deploys with the installed manifests.

### A09 — High: pre-push scanning needs an immutable, credential-free handoff

**Location:** plan:30–33, 98–108, 266–288. **Classification:** missing design, not a claim scanning is absent from the plan.

**Failure:** tests can read private source and transmit it before any push gate if worker network egress is unrestricted. A gateway that runs Git in the worker-controlled checkout can execute hooks/filters/helpers or push a changed ref after scanning. Publication can also occur through commit messages, PR text, review uploads, Telegram patches, artifacts or retained logs. `.gitignore` and `.dockerignore` neither enforce all those channels nor purge history.

**Correction:** define approved source/LLM destinations and worker network/package access policy; exclude production data at ingestion. Transfer an immutable content-addressed patch/object bundle into a separately owned bare/quarantine repository; use trusted Git config, no worker hooks/credentials/helpers, fixed remote and explicit refspec, no force/mirror/tags, and push exactly the scanned object IDs. Validate all newly reachable commits/blobs plus messages/metadata, and all outbound review/PR/chat/log artifacts before upload. Scan the existing publication baseline/history once or document a trusted baseline; an all-outgoing-commits check cannot repair secrets already published. Reject binaries/oversize/uninspectable diffs or require maintenance review. `.env.example` should be an explicit synthetic exception if retained. Redacted diffs need a visible redaction marker so the owner does not mistake them for complete source evidence.

**Evidence:** the plan already correctly requires all-commit scanning and artifact redaction; the missing points are the process/TOCTOU boundary and pre-publication egress. Current Docker context copies `packages/` and `apps/`; ignore files are filters, not a guarantee against copied secret values. ARCHITECTURE.md:518 prohibits private text/tool payloads in logs and :657 assumes worktrees contain no secrets; a production worker must enforce that assumption.

**Acceptance test:** add/delete secret in intermediate commit, secret in commit title and patch context, synthetic `.INI`/`.env` and renamed backup, poisoned Git hook/filter, ref replacement during scanning, binary diff and outbound network exfiltration probe. Verify no synthetic sentinel reaches any unauthorized sink, including failure logs. Do not use real credentials as fixtures.

### A10 — High: recovery and same-box availability are overstated by “retain existing behavior”

**Location:** plan:72–82, 125–130, 239–260, 335–339, 363. **Classification:** confirmed code limitations plus missing operational contract.

**Failure:** a separate runner Unix account survives a bot restart, not host OOM, disk exhaustion, kernel failure or a wedged Docker daemon. Current automatic restore starts the old image but does not wait for its health (`remote-deploy.sh:29–44`); callers suppress its error (`:210,218,234`). Healthy-release metadata is updated across separate writes (`:241–250`), and logs from the new app are forwarded to deployment output (`:213`). A malicious or broken new app may leak runtime secrets into Actions logs despite having passed source scanning. The explicit rollback path has a health loop, but that does not make automatic restoration health-verified.

**Correction:** specify host disk/memory/CPU/PID quotas for builds and workers, fixed source/cache cleanup, independent watchdog and break-glass owner recovery. Preserve the last known-good image/config manifest until a trusted health/smoke contract passes; persist an atomic release journal and distinguish failed, restoring, restored-healthy and recovery-failed. Health checks must not be freely weakened by the candidate. Do not forward raw production logs to Actions; emit bounded structured results and keep sensitive diagnostics protected. Keep bot outages from destroying the approval ledger/result path. Record that same-box host failure requires external/operator recovery; separate accounts are not high availability.

**Acceptance test:** on a disposable deployment kill the bot, runner and host-side deployment process at each journal boundary; simulate full disk/build OOM, failed new health and failed old health. Verify truthful recovery reporting, retained previous artifacts, no unrelated service restart/database restore, and no synthetic runtime secret in Actions output.

### A11 — Medium: the future Pro upgrade claim promises unavailable reviewer protection

**Location:** plan:216–218, 235–237. **Classification:** confirmed documentation mismatch.

**Failure:** an operator may assume upgrading to Pro supplies private-repository required deployment reviewers and remove the custom owner approval boundary.

**Evidence:** private protected branches are available with Pro; private environments/secrets/deployment branches are also available, but required reviewers and wait timers on Free/Pro/Team are documented as public-repository-only. [Protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches), [environment availability](https://docs.github.com/en/rest/deployments/environments).

**Correction:** name the exact features rather than “environment rules”: Pro can add private branch protection and environment/deployment-branch features; it does not supply every private deployment protection rule. Keep the chosen Free design and single owner DM approval. Qualify “every branch's workflows can read repository secrets” by event/secret availability; not every event receives them, but repository secrets are still not a main-only boundary for writable internal branches.

**Acceptance test:** a documentation/configuration checklist maps each relied-upon control to the chosen account/repository plan; no production safety property relies on a paid or unavailable feature.

## Decisions and proposed revision order

1. **Owner:** choose runner admission assurance. Verify repository owner type and actual runner-group capabilities. If staying with personal repository-level scheduling, explicitly accept a trusted-publisher/workflow assumption; it is incompatible with an unconditional guarantee against arbitrary workflow execution by a compromised repository writer.
2. **Owner + Claude:** choose approval broker/online ledger connectivity and whether compromise of the bot itself is in scope. Keep private Postgres private and prevent ordinary bot code/database credentials from minting approval if that stronger guarantee is required.
3. **Owner:** acknowledge the merge-base race residual under Free/direct-main-write capability, or authorize a separately reviewed change to merge publication mechanics. Head pinning plus tree equality can reliably refuse deployment, but cannot retroactively refuse an API merge already completed on a changed base.
4. **Claude:** specify the exact credential matrix, immutable policy closure, candidate allowlist, CI provenance, transactional approval/outbox and deploy journal before splitting implementation tasks. Bootstrap release schema/control code through maintenance, not self-improvement.
5. **Owner + Claude:** select worker host/egress/resource policy and trusted smoke/recovery checks. Same-box development remains an option only with enforced boundaries; it is not established by another account or checkout.

After these corrections: validate PR-only generation/publication in isolation, exercise approval and merge races in a disposable private repository with the actual token types, then exercise deployment/recovery on a disposable host. Only then register/enable the production deploy runner and trial the narrow application change class. This keeps the requested architecture and one approval while avoiding an implied production authorization from this review.

## Concerns not promoted to defects

- Tree equality is a useful strong deployment invariant when verified against recorded immutable objects. An up-to-date head genuinely has the expected merge tree if the base remains unchanged; A04 addresses the race, not the basic Git property.
- GitHub Free/private does not prohibit GitHub-hosted CI or self-hosted deployment. Account minutes/quotas and runner availability need operational handling, not a change of the chosen plan.
- Current docs do not justify saying all Free organizations lack runner groups. Actual account configuration was not inspected, so availability here is an unresolved prerequisite rather than an asserted absence.
- App/FGPAT workflow restrictions are real permission boundaries; they are not proof against transitive code changes or every historical/ref manipulation. Token-specific tests are deliberately requested rather than invented evidence.
- The existing pending-action requester check, durable jobs and restoration paths are reusable foundations. This review does not allege a current owner-impersonation exploit in their deployed implementations.
- Initial migration refusal and keeping configuration outside build inputs are appropriate. They require enforcement against the actual deployed baseline and the whole execution closure.
- No suggested exploit or acceptance test here was run against production or the live GitHub repository. The two bug sessions' handovers were read for context only and were not acted upon.

---

## Round 2 — Revision 2 re-review, 2026-10-31 (Asia/Singapore)

**Verdict: CHANGES_REQUIRED.** The revised architecture addresses most initial defects at the design level and no longer depends on unsupported runner-admission guarantees. Three remaining contracts below need correction before design approval. This is not a finding that gVisor or the broker design is inherently infeasible, nor a demand to change the selected Free/private/personal repository, one DM approval, hosted merge and same-box runner choices.

Reviewed the actual Revision 2 file at SHA-256 `aa26b175f6b6987021aa3747912b8c5f90d048a1eef288d732bb65a893913786`. HEAD remained `0568954a14575625c42375e581343c22169a9e63`. Line numbers in this round refer to Revision 2 of `docs/plans/2026-10-31-self-improvement-plan.md`, not the historical snapshot above. Original review text is preserved. I rechecked the Telegram adapter's token/edit capabilities and current official Telegram/GitHub identity documentation. No implementation, production inspection, live GitHub mutation or disposable-host acceptance test was performed. The account-owner check at plan:78–81 is evidence reported by the coordinator, not a live check repeated in this review.

### Disposition of A01–A11

“Addressed in design” means the revised requirement corrects the finding; it does not mean the implementation exists or its acceptance test passed.

| ID | Disposition | Revision 2 evidence and remaining limit |
| --- | --- | --- |
| A01 | Addressed in design; feasibility gate remains | :51–83 removes production authority from the whole runner and admits unwanted jobs may reach it. Sandbox compatibility, no host sockets/mounts, cleanup and resource evidence remain mandatory. |
| A02 | Addressed in design | :95–132 chooses distinct Apps, records scopes and protects transitive policy inputs. Token/API tests and lifecycle implementation remain unexecuted. |
| A03 | Partially addressed | :160–223 specifies authenticated ingress, isolated ledger and OIDC broker. Owner-visible card integrity still needs R2-01; callback authenticity alone is insufficient. |
| A04 | Addressed in design for the head/base race | :248–261 correctly discloses an already-merged but blocked result and does not invent an API base guard. R2-02 must distinguish pre/post-merge checks; R2-03 closes approval presentation of undeployed base content. |
| A05 | Addressed in design | :242–246 binds checks to workflow/run/attempt and tested B/H/tree with fail-closed pagination. Protected independent checks remain an implementation requirement. |
| A06 | Partially addressed | :179–223 and :267–284 add transaction/outbox, fencing, run correlation and cancellation reconciliation. R2-02 must define phase-specific admission and retry rules so ordinary completion does not invalidate itself. |
| A07 | Addressed in design | :179–189 replaces generic used_at/public watches with explicit decisions, typed polling and separately bootstrapped schema/roles. Existing code has not been migrated. |
| A08 | Addressed in design | :120–132 and :288–308 protect execution inputs, independently fetch/verify images, and compare migrations against deployed D. Entire deployment delta needs the explicit review rule in R2-03. |
| A09 | Addressed in design | :134–158 provides immutable quarantine publication, artifact scanning and default-deny candidate egress. This requirement must cover hosted CI candidate execution as well as the development worker. |
| A10 | Addressed in design | :304–322 requires atomic journal, healthy restoration, bounded resources, structured output and external recovery for host failure. Resource values/compatibility and failure-injection evidence remain pending, correctly disclosed. |
| A11 | Addressed in design | :324–339 corrects paid-feature claims and explicitly leaves bootstrap acceptance to the owner. |

### R2-01 — High: protecting inbound callbacks does not protect cards if the ordinary app retains the Bot API token

**Location:** plan:56–57, 87–90, 162–169, 172–183. **Classification:** missing security contract, with a concrete attack under the existing token ownership model. This also confirms the coordinator's independently raised concern; it is not a claim that protected webhook authentication itself can be forged with only a bot token.

**Failure:** a compromised ordinary app holding the same bot token can edit the text of a broker-issued approval message. It can replace the description with a harmless change while leaving its approval button/nonce associated with a different candidate. The real owner then clicks the real card: webhook secret, numeric sender, chat, message ID and callback nonce all validate, but the content the owner saw was not the broker's authorized card. A guessed/known message ID suffices to target edits; secrecy of a message ID is not an authorization boundary. The token can also reconfigure webhooks or delete messages, creating disruption and interception risks. Inbound nonce binding prevents a simple fabricated callback from an ordinary RPC, but does not solve this presentation attack.

**Evidence:** the adapter currently constructs its Telegram client with the configured bot token (`packages/adapter-telegram/src/chatcore_telegram/__init__.py:80–89`) and edits text by chat/message ID (`:462–476`). Telegram's same-bot edit API has no distinction between a broker message and an app message. Webhook `secret_token` authenticates delivery to the configured endpoint; it does not restrict Bot API message edits. [Telegram message editing](https://core.telegram.org/bots/api#editmessagetext), [webhook configuration](https://core.telegram.org/bots/api#setwebhook).

**Proposed correction:** “The protected Telegram bridge is the sole holder of this bot's API token. The ordinary runtime receives no token or token-bearing download URL. All outbound sends, edits, deletes, callback answers and media operations pass through an authenticated, method-allowlisted bridge. The bridge tracks app-owned message IDs and permits mutations only within that set and authorized chats; release messages, release callback namespace, webhook configuration and token administration are inaccessible to app requests. Release updates are consumed before ordinary-update forwarding. App messages cannot mint release buttons or substitute broker-owned card content. Maintenance rotates the previously app-held token and proves the old token unusable.” File transfer must be proxied without giving the app the credential. A separate approval bot with its own protected token is an alternative requiring an explicit product/owner decision; it is not required if the bridge contract is implemented.

**Acceptance test:** give a hostile ordinary app all its intended runtime credentials and known release chat/message IDs. Attempt direct API use, edits/deletes/reply-markup replacement, webhook changes, callback answering and release-button creation via the bridge. All must fail without changing the card. Confirm ordinary chat/file operations still work and the protected owner click completes once. Repeat with the old pre-bootstrap token after rotation. Read-only database access and a release ID must not grant message or approval mutation.

### R2-02 — High: baseline and revocation checks need phase-specific linearization rules

**Location:** plan:191–201, 218–223, 248–265, 269–284, 310–313. **Classification:** specification ambiguity affecting liveness and race handling, not evidence of an implemented stale-deploy vulnerability.

**Failure:** :200 says current head/base/deployed-baseline mismatch requires a new card, while :264–265 permits main to advance after merge without substituting the payload. After the authorized merge itself, main is M rather than B; after healthy deployment, D has become M. Applying the mismatch rule on every claim/retry would invalidate the intended release, prevent a successful lost-response retry, or demand another tap for its own effects. Conversely, skipping all checks after one admission could allow a long build to switch after revocation, expiry, changed configuration or superseding maintenance. “Current broker fence” and “revocation prevents later effects” do not yet define the instant at which a switch becomes an authorized in-flight effect.

**Proposed correction:** define a transition table with guards and an explicit host-switch admission point:

- Before merge authorization: require current main B, current H, valid approval, D/config/policy identity and latest target sequence. A lease/token alone is never permission to skip this check.
- After uncertain/successful merge: reconcile the recorded immutable M and its expected B/H parents/tree. The authorized B-to-M transition is not staleness. Do not remerge a closed PR. Distinguish unrelated later main movement according to the stated residual policy; never use it to change M.
- Before first container switch: under the host lock, atomically claim a deploy operation against the current broker fence, sequence, still-valid approval and actual deployed image/config/policy baseline. Long preparatory builds confer no switch permission. Serialize revocation/maintenance against this admission. Revocation before this point denies the switch; revocation after it permits only the already-admitted operation and bounded recovery, not a fresh deployment attempt.
- After a switch or lost response: reconcile the same durable operation/receipt against actual host state. Completion and safe restoration do not require another merge or a new tap merely because the operation changed D to M. Starting a new switch after an expired/revoked authorization requires a fresh recovery/release card with the intended immutable M and current baseline, not implicit approval reuse.

Keep this as one normal owner approval; renewed approval is only for a genuinely stale/new operation. Specify config generation/digest as part of D's deployment identity, not just Git SHA, so settings changes cannot be silently reverted by a stale recovery manifest. An old hosted job may still hold GitHub write authority; host fencing cannot retroactively fence GitHub's merge API. Preserve the disclosed merged-blocked residual for late merge effects.

**Acceptance test:** normal B/H→M deployment needs one tap; retries after merge and after healthy deployment return the same outcome. Advance unrelated main after M, change host config during build, revoke immediately before/after switch admission, expire approval mid-build, lose the switch response, and run maintenance between claims. Assert the chosen phase rule, no stale replacement/config rollback, truthful merged-blocked outcomes and a defined route to recovery without remerging.

### R2-03 — High: approving a PR diff must not implicitly approve unknown undeployed main content

**Location:** plan:12–14, 172–177, 232–246, 255–265, 299–301. **Classification:** missing content-review contract; the plan correctly records D but leaves the card's diff range ambiguous.

**Failure:** a leaked publication credential pushes an ordinary allowlisted plugin change directly onto main, producing B while production remains D. A later legitimate small PR H is based on B. Its PR diff B→H and CI can be valid; its merge tree equals H; migrations and protected policy can be unchanged. Deploying H also deploys the unreviewed D→B code. Merely binding D in a record and checking that paths are allowed does not show the owner this additional behavior. Thus “unapproved content still cannot deploy” at :260 needs an explicit full-release review definition. This is not an assertion that the existing text definitely intended PR-only diffs; it is a security-relevant ambiguity to remove.

**Proposed correction:** “The authorization card and trusted review bundle cover the complete runtime source change from the last healthy deployed tree D to proposed H, with D→B undeployed-main changes shown separately from the PR's B→H diff. Independently validate allowed/protected paths, migration equality, evidence and provenance over this full delta and relevant newly introduced history. No unexplained undeployed-main content is eligible by default: block for maintenance reconciliation, or issue an explicitly complete release card/review that includes it and passes the same policy. A PR's previously green checks or gateway record is not proof of approval for inherited base content. Reject unrelated-history/divergent baseline cases in the initial version unless a maintenance reconciliation records a new trusted baseline.”

The default narrow implementation can simply refuse undeployed base content absent a previously verified release-chain explanation. This retains the one-tap flow for eligible candidates without weakening owner review or silently trusting main. A failed deployment whose code remains merged must likewise have an explicit path: reconcile that recorded release or review the complete combined delta on a new card.

**Acceptance test:** inject an allowlisted non-migration change into B outside the gateway, then open a benign small PR. Assert no card presents only the benign PR delta and no host switch occurs from that evidence. Test merged-but-failed earlier release content, same-tree/different-history baselines, force-rewritten main and a normal verified baseline. When policy permits a combined release, all D→H changes and evidence must be visible and bound to the single approval.

### Protocol checks that do not warrant additional defect claims

- **OIDC/job identity:** current official GitHub documentation includes `check_run_id` for the current job, so its presence at :212 is valid; rejecting it as a nonexistent claim would be incorrect. The jobs API supplies check-run/run/runner information for correlation. Implement the required correlation using the exact run attempt and expected job, not display name or runner label alone. Verify claim availability using the chosen real workflow in rehearsal. [OIDC reference](https://docs.github.com/en/actions/reference/security/oidc), [workflow jobs API](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt).
- **Replay and wrong-run dispatch:** :218–223 explicitly requires operation-bound short-lived receipts, fenced claims and rerun reconciliation. That is an adequate design direction once R2-02 defines guards. OIDC is a bearer credential, not proof of intent; receipt consumption must deduplicate side effects, and the host must reject merge-job identity at a relay-only endpoint and vice versa. One legitimate run cannot claim another release merely because its workflow identity matches; broker release/run binding remains required.
- **Unwanted jobs on the runner:** a disposable whole-runner sandbox with no host credentials/sockets and an independently authenticated host API can replace the old reliance on workflow admission for *host-authority protection*. This is conditional on real isolation and on the trusted supervisor never honoring arbitrary job requests for privileged containers/mounts. It does not guarantee queue availability; hostile jobs/dispatch cancellation can still starve or disrupt work. Bounded capacity, alerts and owner recovery are appropriate residual handling, not reasons to assert the architecture impossible.
- **Database forgery:** the dedicated role/ledger requirement is sufficient at design level if bootstrap proves the ordinary role has no release-schema ownership, inherited write/DDL rights, usable SECURITY DEFINER mutation path, or shared broker credentials. The ordinary application's startup migration path must not recreate those privileges. No current deployed database privilege audit was performed here.
- **Build and runtime trust:** installed policy plus isolated, secret-free builds and independently verified images addresses the former candidate Compose/helper problem. It does not prove approved plugin code harmless once deployed; :87–91 and :308 disclose that trust transition honestly. Protecting the bridge credential in R2-01 is still necessary because approval authority is intentionally outside that runtime trust.
- **Evidence:** no acceptance row SI-01 through SI-12 is counted as passing. No historical/sample test count establishes current CI health, and a design review cannot prove Actions/gVisor compatibility, host quotas, broker availability or recovery correctness.

### Round 2 readiness and decisions

The revised architecture is suitable for **detailed disposable-prototype planning after R2-01–R2-03 are incorporated**, with separate tasks for the bridge/adapter migration, ledger/state machine, OIDC policy verifier, sandbox supervisor and host service. PR-only isolation/publication work can be planned independently while these release contracts are finalized. Production installation remains gated on actual implementation tests, disposable Free/private/App rehearsals, measured resources and explicit owner bootstrap acceptance.

Claude should amend the three contracts and add their tests to SI-03/SI-06/SI-12. The owner should choose sole-token protected bridge versus a separate approval bot if product behavior differs, and review the concrete bootstrap ingress origin, policy and sandbox evidence. The safest narrow default for unexplained D→B content is refusal/reconciliation. No paid upgrade, repository transfer, public exposure of source, second routine approval, or production change is required by these corrections. The original main-write and same-box availability residuals remain honestly disclosed and require bootstrap acknowledgement; this review does not accept them on the owner's behalf.

---

## Round 3 — Revision 3 re-review, 2026-10-31 (Asia/Singapore)

**Verdict: PASS for detailed implementation planning only.** Revision 3 resolves R2-01 through R2-03 at the design level. I found no remaining material contradiction requiring another architecture repair round within the stated scope and trust model. This verdict does **not** establish implementation correctness, sandbox compatibility, production readiness, owner acceptance of residual risks, or authorization to install anything.

Reviewed the actual `docs/plans/2026-10-31-self-improvement-plan.md`, Revision 3, SHA-256 `7cc80af45dd4745f88882f3f0f162f2e2238b3a88a567c8cf21c3b80bc6c2c6b`. HEAD remained `0568954a14575625c42375e581343c22169a9e63`. References below use Revision 3 line numbers. All previous review rounds remain historical evidence; their verdicts apply to their respective snapshots. The official GitHub/Telegram documentation and repository evidence examined in the earlier rounds remain the basis for the platform claims; no live account, production or permission experiment was added in this round.

### Disposition of the remaining findings

| Finding | Disposition | Evidence and reasoning |
| --- | --- | --- |
| R2-01 — Token ownership/card integrity | **Addressed in design** | :58–59, :89–93 and :172–191 explicitly remove the Bot API token from ordinary runtime authority. The protected bridge owns inbound and outbound operations, enforces method/chat/message ownership, protects release messages/callbacks and webhook administration, proxies token-bearing file access, and requires rotation of the previously app-held token. These requirements address the same-bot message-edit attack rather than relying on webhook authentication or message-ID secrecy. SI-03 at :425 tests hostile app access as well as ordinary adapter compatibility. |
| R2-02 — Phase-specific admission/retries | **Addressed in design** | :194–197 makes D an image/config/policy/sequence identity. :232–236 and :263–290 distinguish premerge validation, expected merge reconciliation, build preparation, atomic switch admission, bounded switch start, safe recovery and completed receipt returns. Revocation/maintenance are ordered against admission; the release's own B→M and D→M effects do not demand another approval. SI-06 at :428 covers the relevant boundary races and lost responses. |
| R2-03 — Undeployed main content | **Addressed in design** | :194–212 binds the full D→H review bundle, separately displays inherited D→B and PR B→H changes, and defaults to blocking unexplained/divergent/merged-failed base content. :327–330 applies full-delta evidence at release validation; SI-12 at :434 tests the benign-PR-on-unreviewed-base case. Recording H no longer silently substitutes for showing what changes in production. |

The earlier A01–A11 dispositions carry forward: all are now **addressed in design**, subject to the unexecuted implementation/bootstrap gates. In particular, the partial A03/A06 dispositions in Round 2 are closed at this level by the bridge and phase-specific protocol changes. No new defect IDs are raised in Round 3.

### Coherence of the normal and exceptional paths

For an eligible ordinary candidate, B is the known deployed baseline or a recorded acceptable same-tree continuation. Trusted evidence covers D→H. One affirmative owner click creates a durable decision/outbox entry. The hosted merge checks current B/H and approval, produces M with the expected parents/tree, and records that expected transition. The host's preparation does not authorize replacement. Under the host lock, the broker admits one switch only against the valid decision, current sequence/fence, actual image/config/policy baseline and current main M. A journaled switch then finishes or performs bounded recovery; reporting its receipt does not require a new click. These rules are mutually compatible.

The stricter requirement that main remain M at first switch admission is a deliberate fail-closed choice. Unrelated advancement before that point can block deployment and require reconciliation; advancement after admission cannot change the payload. This is an availability tradeoff, not a new approval bypass or contradiction. The plan still correctly acknowledges that GitHub writes by an expired hosted job or compromised Contents credential cannot be retroactively fenced by the host broker (:287–290, :317–325). Such effects must be reconciled without pretending no merge occurred.

The deployment relay remains untrusted for production authority. Its OIDC identity and operation receipt are inputs to the protected service's independent checks, not a grant of Docker access. Allowing an unwanted job into a genuinely isolated disposable runner is consistent with this revised guarantee. Runner starvation, stolen-dispatch-token disruption and host failure remain possible availability failures; the plan does not claim otherwise. The new hosted-CI isolation requirement (:163–168) also closes the ambiguity about exposing read-only repository tokens and runner command channels to candidate tests.

### Implementation and bootstrap gates that remain open

These are deliverables required by the accepted design, not additional findings or evidence already obtained:

1. **Concrete policy and protocol specification.** Turn the protected execution closure, exact plugin allowlist, API schemas, OIDC claim/job correlation, role grants, state transitions, operation receipts, deadlines and recovery decisions into owned implementation tasks and tests. Fail closed on missing identity/evidence. Ensure expired or cancelled attempts cannot start a new operation merely by presenting a previously valid receipt.
2. **Telegram/DB migration proof.** Implement the token-free ordinary adapter, bridge message-ownership rules and authenticated update relay. Demonstrate that ordinary DB roles, startup migrations and any callable database helpers cannot mutate the ledger. Rotate the old bot token through maintenance and verify ordinary messages, files and existing confirmations still function. Do not put protected credentials back into compatibility fallbacks or restored app snapshots.
3. **Disposable isolation and permission proof.** Use actual selected App scopes and Free/private configuration in a disposable repository. Prove whole-runner gVisor compatibility and destruction, supervisor credential isolation, denial of Docker/host/config access, and candidate-test isolation on hosted CI. If unsupported, stop rather than weakening the boundary. The source-review PASS is not that proof.
4. **Crash/race and recovery proof.** Exercise SI-03/04/06/07/08/10/12, including both broker and host journal boundaries, approval expiry/revocation, delayed start, uncertain merge/switch responses, stale attempts, config maintenance, unknown base content, bad health and failed restoration. After an ambiguous crash, the implementation must inspect actual state; a timeout or prepared journal entry alone cannot establish whether a switch happened.
5. **Operational bootstrap.** Measure resource budgets and shared-host impact; configure the broker origin, TLS/ingress authentication, quotas, diagnostics, watchdog and break-glass recovery. Present the concrete installed policy and rehearsal evidence to the owner before production installation. Initial automated migrations remain refused; broker/schema/control-plane setup is separate maintenance.

The protected control plane, Telegram/GitHub authentication, sandbox boundary and host root remain explicit trusted components. Owner-approved application code still receives ordinary application credentials and can misuse them; that disclosed runtime trust does not extend to the protected Telegram token or release authority. No source-scanning or design-review claim should be interpreted as proving arbitrary approved code safe.

**Readiness boundary:** proceed to detailed implementation planning and isolated prototypes using the staged delivery at :406–417. Do not register a production runner, switch Telegram ingress, change production database roles or deploy the feature based on this verdict alone. A normal eligible release retains one owner tap; bootstrap consent and genuinely stale/new recovery authorizations are distinct. No acceptance test has been executed or marked passed by this review, and sample/historical test counts remain illustrative rather than current CI evidence.
