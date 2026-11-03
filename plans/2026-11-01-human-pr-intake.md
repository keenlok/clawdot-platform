# Human PR intake through the release-control pipeline

Date: 2026-11-01 (Asia/Singapore). Status: **plan only, not built, not installed.**
Builds on the approved design [2026-10-31-self-improvement-plan.md](2026-10-31-self-improvement-plan.md)
(Revision 3, passed in [Astra Round 3](2026-10-31-self-improvement-astra-review.md)) and the
merged source in `services/release-control` (PR #8, #13). Workflow: this plan is split
into Codex tasks; Codex `gpt-6-luna` implements, `gpt-6.1-sol` reviews, Claude does final
review and commits. **Production changes need the owner's explicit OK each time.**

Goal: friends open PRs; the owner registers one; it gets the same scanned, revision-bound
Telegram card and one-tap merge+deploy as a generated candidate. **The bot's code generation
stays disabled** (no generation service, no LLM egress, no development sandbox on the box).

## Open owner decisions

Nothing below is decided by this document. Each one blocks the step named.

| # | Decision | Options | Recommendation | Blocks |
|---|---|---|---|---|
| D1 | **Repository write control** (see §4) | (a) GitHub Pro on the personal account; (b) transfer to a free organization, contributors get *Read* and send fork PRs; (c) stay on Free, accept block-and-reconcile | **(b)** if you are fine with a transfer; else **(c)** for a small trusted group. Decide before inviting anyone. | Inviting contributors; T7 |
| D2 | **Contributor allowlist** | Numeric GitHub user IDs (not logins) in installed policy | Start with 1–3 people | T1, bootstrap |
| D3 | **Re-publish vs widen ref rule** (§2.3) | Re-publish exact commits to `clawdot/<id>` / widen to contributor refs | **Re-publish** (no coordinator/merge change) | T2 |
| D4 | **Path scope for human PRs** | Same enumerated plugin files as generated releases / wider | **Same as design**: enumerated ordinary plugin Python + tests. Anything else ships through maintenance as today. | Policy manifest |
| D5 | **Bootstrap go-ahead** | Disposable rehearsal now, production later / defer | Rehearsal first; production install is a separate OK | §3 steps B5–B7 |
| D6 | **Actions secret exposure** (§4.4) | Accept that write collaborators can read the CI gateway token / move CI→gateway auth to OIDC first (T6) | Do T6 before any write collaborator exists; not needed under D1(b) | Inviting write collaborators |

## 1. What blocks a human PR today

`CiPhaseCoordinator._validate_context` (`release_coordinator.py:1673-1797`) requires a gateway
CI context with exactly these keys and checks, among others:

- `request_id` matching `[a-z0-9][a-z0-9-]{0,63}` and
  `published_ref == refs/heads/clawdot/<request_id>` (line 1712);
- non-empty `specification` (≤ 8 KiB) and 1–8 `synthetic_examples` (≤ 16 KiB);
- an insert-once `provenance` record (`publication.ProvenanceRecord`) with
  `baseline_history_scan_complete = true` and the installed scanner identity;
- `_validate_pull_request` (line 1799): PR open, head ref = `published_ref` minus
  `refs/heads/`, head and base repo IDs = this repository, title/body equal to the context.

Only `PublicationGatewayService.publish_request` creates that context, and only from a
generated `PublicationRequest`. A human PR has no record, so it can never be prepared by CI,
never gets check evidence, and never gets a card.

What already fits: `PublicationService.publish` (`publication.py:542`) takes **any** Git
bundle with D/B/H. It unbundles into a quarantine repo, runs `collect_release_bundle`
(policy paths, protected paths, migrations), scans every commit's metadata and every
old/new blob on every `D..H` edge (`_scan_history`, so a secret added then removed still
blocks), rejects baseline-forbidden blobs, inserts the provenance record once, then pushes
exactly H to `refs/heads/clawdot/<id>`. `GitObjectFetcher` (`git_fetch.py`) already fetches
`refs/pull/<N>/head`. Nothing in that path cares who wrote the commits.

## 2. Design: human candidate intake

### 2.1 Flow

1. A contributor opens PR #N against `main` (fork or branch, depending on D1).
2. The **owner** runs, on the protected host, as the gateway's service user:
   `python -m chatcore_release_control.human_intake --config <gateway config> --pr N --head <full H> --note-file <owner note>`
   The owner copies full H from the PR page after reading it. This is the "owner-initiated"
   step; the bot cannot trigger it and no Telegram command reaches it.
3. Intake (new module, runs inside the gateway process boundary, same config/keys):
   1. Read PR #N with the gateway's App token. Require: open, not draft, base `main`,
      `user.id` in the installed **contributor allowlist** (D2), `head.sha == H`.
      Fork PRs allowed only if D1(b); then the head repo must be a fork of this repo.
   2. Read current main B and the recorded deployed commit D (from the protected host
      client, as generation does). Require B to be an ancestor of H; if the PR is behind
      main, the contributor rebases or merges main: **intake never rewrites commits.** If B ≠ D and B is not a
      recorded same-tree continuation, stop: the ordinary-card rule (design §"Authentic
      owner approval") already refuses this.
   3. Fetch `refs/pull/N/head` into a fresh quarantine (reuse `GitObjectFetcher`'s
      isolated fetch: no hooks, no credential helpers, fixed remote). Require the fetched
      tip is exactly H (TOCTOU: the contributor may push between steps 3.1 and 3.3).
   4. Build a full `D..H` Git bundle from the quarantine objects and a
      `PublicationRequest` with `request_id = pr<N>-<first 12 hex of H>`. A new push gives a
      new H and so a new request ID; the old record stays (insert-once, never updated).
   5. Call the **unchanged** `PublicationService.publish`. All existing scans run on the
      human commits: secret scanner over commit metadata, paths and all blobs of every edge
      `D..H`; private-file rules (`.ini`, `.env` variants, backups, credentials); protected
      paths; baseline-forbidden blobs; size/history limits (64 MiB bundle, 2048 commits).
      Any hit stops intake; nothing is pushed.
   6. Publish creates `clawdot/<request_id>` at exactly H and a gateway-owned PR (Publication
      App, `maintainer_can_modify=false`) titled `Human PR #N: <title>` with a body linking #N.
   7. Store the CI context with the existing v1 schema (no schema change):
      - `specification`: the owner's note (required, ≤ 8 KiB, scanned) — what the owner says
        the change is for. Contributor PR text is never used as specification.
      - `synthetic_examples`: `[{"origin": "human", "source_pr": N}]` (satisfies 1–8).
      - `artifacts`: one `human-intake.json` (scanned surface, sha256-bound by the coordinator)
        holding the trusted intake record: source PR number, contributor numeric ID and
        login, source head repo ID and ref, H, head tree, B, D, owner note sha256, intake time.
      - A separate insert-once `human_intakes` table in `PublicationGatewayStore` keyed by
        `request_id` holds the same record for audit. Same immutability triggers as the
        other four tables.
4. From here the pipeline is identical to a generated candidate: hosted CI runs on the
   gateway PR, the coordinator's context checks pass, check evidence and independent review
   cover the full D→H delta, the broker issues the card, one owner tap merges `sha=H` and
   deploys M.
5. Because H is unchanged, merging the gateway PR also makes GitHub mark #N as merged; the
   contributor keeps authorship. Intake leaves a comment on #N pointing at the gateway PR.
   *(Verify in rehearsal; if GitHub does not auto-close #N, the merge job does not touch it
   and the owner closes it by hand.)*

### 2.2 What does not change

The card, ledger, binding, nonce, OIDC verification, merge control, runner relay, host
service and their checks. The coordinator's `_validate_context` and `_validate_pull_request`
are untouched. The only new trust input is the owner's CLI call, which sits at the same
level as running `generation_service` today.

The card should still say it is a human PR and whose. That is presentation only: the card
producer reads `human-intake.json` from the context and passes contributor login, ID and
source PR as trusted lines in `presentation` (T4). Binding fields do not change.

### 2.3 Decision D3: re-publish to `clawdot/*`, do not widen the ref rule

**Recommendation: re-publish the contributor's exact commits to `refs/heads/clawdot/<request_id>`.**

Against the approved design:

- Design §"Revised architecture": the gateway's job is to *"scan immutable outgoing objects
  and publish clawdot/\* PRs with its separate repository-selected App"*. Re-publishing keeps
  that sentence true. Widening makes contributor branches a second publication path the
  design never reviewed.
- Design §"Immutable publication": *"Freeze object IDs before scanning and push exactly those
  IDs to an explicit clawdot/\* refspec."* Re-publishing does this with the human objects.
  With a widened rule the scanned objects live on a ref the contributor controls; the
  binding to H still protects merge (`sha=H`), but every reader of that ref (CI fetch, card
  view, host fetch) has a TOCTOU window the design closed.
- Design §"Credentials": *"Candidate PR identity comes from the gateway record, not trusted
  labels."* The gateway PR is created by the Publication App from the gateway record; the
  contributor's PR is created by the contributor and its title/body are theirs to edit.
- Code cost: widening touches `release_coordinator.py:1712` and `:1814`,
  `publication_service.py:692`, `:745`, `:1843`, `publication_github.py:39`, `:427`,
  `:456`, `:684` and `candidate_bundle.py`, plus the head-repo-ID check, which rejects fork
  PRs outright. Re-publishing changes none of them and **makes fork PRs work for free**,
  which D1(b) needs.
- Contributor experience is fine: SHAs are identical, so authorship, signatures and the
  "merged" state on the original PR carry over.

Residual, same for both options: on GitHub Free any write collaborator can push to
`clawdot/*` too. That cannot get unapproved code deployed: `_validate_pull_request` checks
`head.sha == H`, the gateway transport refuses a moved ref (`publication_mismatch`), and the
merge API is called with `sha=H`. It can only make a candidate fail (denial of service).

## 3. Live bootstrap still required (generation off or on)

Nothing is installed today: `/etc/chatcore-release-control` is absent, `.github/` does not
exist, no Apps, no runner. Generation-off removes only the generation side.

| # | Work | Needed with generation off? | Notes |
|---|---|---|---|
| B1 | **Three GitHub Apps**: Publication (contents+PR write), Broker (Actions write, read rest; also source read for host), Runner Registration | **Yes, all three** | Publication App pushes `clawdot/*` and opens the gateway PR for human candidates too. Verify write denials in a disposable repo (SI-02). |
| B2 | **Baseline history audit** (`baseline_audit`) | **Yes** | Intake scans D..H incrementally; the one-time full-history scan is still the base. |
| B3 | **Policy manifest** incl. contributor allowlist (D2), path scope (D4), workflow closure | **Yes** | Add the allowlist field (T1). |
| B4 | **Publication gateway service** (`publication_service serve`) + scanner binary/config | **Yes** | Intake runs through it. |
| B5 | **Postgres release schemas and roles** (`schema.sql` … `worker_schema.sql`, p10 grants, revocation) | **Yes** | |
| B6 | **Sole-token Telegram bridge cutover**: bridge owns the Bot token, webhook with secret header, app moves to `TELEGRAM_TRANSPORT=bridge`, old token rotated and proved dead | **Yes** | Cards and callbacks need it. Separate production maintenance OK. |
| B7 | **Broker HTTP + broker worker**, target state bootstrapped from observed healthy D | **Yes** | |
| B8 | **`.github/workflows`**: CI (from `p04-ci.yml.inactive`), `release.yml` merge (from `p04-merge.yml.inactive`), relay job (`p09-runner-relay-job.yml.template`), pinned actions | **Yes** | First commit of `.github/` is a maintenance change. |
| B9 | **CI candidate sandbox**: worker image + offline dependency cache, hosted bootstrap artifacts | **Yes** | Human code must still run tests in the runsc worker in hosted CI, never on the runner host. |
| B10 | **gVisor whole-runner sandbox + supervisor** for the deploy relay | **Yes** | SI-01. No fallback to an unsandboxed runner. |
| B11 | **Protected host deployment service** (p08 templates), TLS ingress/proxy routes | **Yes** | |
| B12 | **Watchdog** (p10), break-glass runbook | **Yes** | |
| B13 | **Disposable rehearsal** (disposable repo, host, test bot) covering SI-01…SI-12 | **Yes** | SI-09 narrows: drop the development-sandbox half, keep hosted-CI sandbox. Add the intake cases in T5. |
| B14 | **Production install + narrow trial** | **Yes** | Separate owner OK (D5). |
| — | Generation service (`generation_service`) and its request files | **Skip** | Do not install the CLI or its config. |
| — | Development sandbox on the box (`DevelopmentController`, `DockerSandboxRunner`), its resource budget and the per-B baseline image for generation | **Skip** | The CI worker image (B9) is still built. |
| — | LLM provider config and **LLM egress** from the controller | **Skip** | No model key on the release host. |
| — | Development-sandbox egress sentinels (half of SI-09) | **Skip** | |

Skipping generation also removes the "bot writes code" risk class from the first production
trial, which is the owner's stated order of events.

## 4. GitHub Free personal private repo: contributors and `main`

Verified read-only on 2026-11-01: `keenlok/clawdot-platform` is private, owner type
**User**, only collaborator is the owner, forking allowed.

### 4.1 The problem

- On a personal repo, collaborators have a single fixed role: **write**. They can push to
  every branch, including `main`, and to `clawdot/*`.
- Branch protection and rulesets for private repos are not available on the Free plan.
- A private repo can only be forked by someone with access, so "contributors fork" does not
  avoid giving them collaborator (write) access on a personal account.

### 4.2 What the existing design does when a contributor pushes to `main`

Fails closed, by design. Main moves to B′ ≠ D with content no card covered. The broker
refuses every **ordinary** card while B ≠ D (no recorded same-tree continuation), and
merge admission requires current main = B. Nothing deploys. Recovery is maintenance or an
explicit **reconciliation card** showing the full D→B′ delta (`evaluate_reconciliation_release`
in `policy.py`). Until then every pending human or generated card is blocked.

### 4.3 Options (D1)

| | (a) GitHub Pro (personal) | (b) Free organization | (c) Free, block-and-reconcile |
|---|---|---|---|
| Cost | Paid subscription | Free | Free |
| Contributor access | Still write collaborators | **Read** role; fork PRs | Write collaborators |
| Direct push to `main` | Blocked by branch protection / ruleset requiring PRs (verify the merge job's token still merges via API) | **Impossible** (no write) | Possible; blocks cards until reconciliation |
| Push to `clawdot/*` | Possible unless a ruleset restricts the pattern (verify) | Impossible | Possible (DoS only, §2.3) |
| Read Actions secrets via own workflow | Yes (write users can run workflows on branches) | **No** (fork PR workflows get no secrets) | Yes |
| Trigger/cancel workflow runs | Yes | No | Yes |
| Migration work | None to identifiers | Transfer: **owner ID changes** (policy manifest, OIDC `repository_owner_id`, App installations, gateway/broker config, git remotes on the box); repo ID should be kept (verify) | None |
| Design fit | Design says Pro adds protection but owner approval stays | Design notes org runner groups exist even on Free; "verify if ownership changes" | Exactly the disclosed residual in the design |

**Recommendation:** (b) gives the strongest result for no money: contributors never hold
write access, so §4.4 and the push-to-main block disappear. It needs a disposable-org
rehearsal of the transfer effects and the fork-PR CI path (private fork PR workflows must
be allowed in org settings, and still get no secrets). If you do not want a transfer,
choose (c) for a small trusted group, add T7's alert, and move to (a) if direct pushes
happen. This plan does not authorize a transfer or a paid upgrade.

### 4.4 Secret exposure with write collaborators (D6)

`p04-ci.yml.inactive` passes the `RELEASE_CONTROL_BOOTSTRAP_TOKEN` Actions secret, and the
gateway's CI routes (`/v1/ci/pull-requests/<n>/context|report`, `publication_service.py`
ASGI) authenticate with that bearer token only. Any write collaborator can push a branch
workflow that prints it. What it buys them, from the code:

- Read any CI context (spec, examples, artifacts). Low impact; they can read the repo anyway.
- Upload a CI report first. Reports are insert-once per request ID, so the genuine CI job's
  upload then fails: **denial of service**, not approval. Approval evidence comes from
  GitHub check runs with verified workflow closure, not from the stored report.
- Download hosted bootstrap artifacts (trusted runtime). Not secret, but not intended.

Under (a) or (c) do **T6** (gateway CI auth via GitHub OIDC: hosted runner, `pull_request`
event, approved workflow ref/sha, exact PR number) before inviting a write collaborator.
Under (b) T6 is optional hardening.

Other write-collaborator abilities the rehearsal must cover (all already design residuals):
dispatching `release.yml` themselves with copied inputs (the broker must reject an unclaimed
run; SI-06), cancelling release runs (reconcile, SI-06), targeting the self-hosted label from
a branch workflow (sandbox must hold, SI-01).

## 5. Codex tasks

Shared rules for every task: read `ARCHITECTURE.md` §6a and this plan first. Environment:
`export UV_CACHE_DIR="$PWD/.uv-cache"`, `uv sync --all-packages`. Tests:
`uv run pytest services/release-control -q`; lint `uv run ruff format .` and
`uv run ruff check .`. Postgres tests skip without `TEST_PG_DSN`; the reviewer runs them.
No secrets, tokens or real IDs in code, fixtures, plans or logs; synthetic fixtures are
generated at runtime as in the existing tests. **No task installs anything, registers a
runner, creates an App or touches the production box.** Each task is one PR against `main`.

### T1. Contributor allowlist in the policy manifest

- Files: `policy.py` (`PolicyManifest`: new `human_contributor_ids: tuple[int, ...]`,
  sorted/unique, ≤ 32, positive ints; included in canonical JSON and therefore the digest),
  `templates/p09-policy.json.template` (empty list), `tests/test_policy.py`.
- Compatibility: manifests without the field load as empty (no human intake). Digest of an
  existing manifest **does** change if the field is serialized; decide: serialize only when
  non-empty, so current digests and fixtures stay valid. Test both.
- Tests: duplicates, non-int, bool, zero, > 32 rejected; round-trip; digest unchanged for
  empty list; digest changes when an ID is added.
- Rollback: revert the PR; nothing installed reads it yet.

### T2. Human intake core (`human_intake.py`)

- Files: new `src/chatcore_release_control/human_intake.py`; `publication_service.py`
  (`PublicationGatewayStore`: insert-once `human_intakes` table with the same no-update/
  no-delete triggers; `PublicationGatewayService.publish_human_candidate(...)` that reuses
  `publish_request`'s steps with the human surfaces of §2.1 step 7); `publication_github.py`
  only if a read helper for `GET pulls/{n}` with `user.id`/`head.repo` is missing;
  new `tests/test_human_intake.py`.
- Behaviour exactly as §2.1 step 3. Error codes are bounded strings
  (`intake_not_allowlisted`, `intake_head_moved`, `intake_base_mismatch`,
  `intake_fork_denied`, `intake_draft`, `intake_closed`, plus existing publication codes).
  Never echo PR text, scanner output or tokens in errors or logs.
- Tests (local bare repos, fake GitHub transport, fake scanner, as in
  `test_publication_service.py`):
  1. Allowlisted same-repo PR → `clawdot/pr<N>-<h12>` at exactly H, gateway PR opened,
     context passes `CiPhaseCoordinator._validate_context` **unchanged** (import and call it).
  2. Non-allowlisted author, draft, closed, base ≠ `main` → refused, nothing pushed.
  3. Head moved between PR read and fetch → `intake_head_moved`.
  4. Secret added in an intermediate commit and removed later → refused (scanner sees it).
  5. Commit message containing a secret sentinel → refused.
  6. `.ini` / `.env.local` / backup file added then renamed → refused.
  7. Protected path (e.g. `.github/…`, `apps/sb_bot/deploy/…`) touched anywhere in D..H → refused.
  8. Path outside the policy's allowed paths → refused.
  9. B ≠ D without recorded continuation → refused before any push.
  10. Same intake re-run with same H → idempotent (same record, same PR); same request ID
      with different note → `state_conflict`.
  11. Fork PR refused unless config enables forks; enabled → works and head-repo check is
      "fork of this repo".
  12. `human_intakes` rows cannot be updated or deleted.
- Rollback: revert; the gateway store table is additive and unused without the CLI.

### T3. Intake CLI

- Files: `human_intake.py` `main()` (`python -m chatcore_release_control.human_intake`),
  reusing `ProtectedPublicationConfig` / `build_publication_gateway`; config gains
  `human_intake_enabled: bool` and `human_intake_allow_forks: bool` (both required, default
  template values `false`); `templates/` gateway config template if one exists for p03,
  else document keys in the release-control README; `tests/test_human_intake_cli.py`.
- CLI args: `--config`, `--pr`, `--head` (full SHA only, 40 or 64 hex), `--note-file`
  (owner-only mode 0600 file, ≤ 8 KiB). Refuses when `human_intake_enabled` is false.
  Prints only request ID, gateway PR number, H and a bounded result code.
- Tests: abbreviated SHA, world-readable note file, oversized note, disabled flag, missing
  config keys all refused; happy path calls the service once.
- Rollback: revert; disable via config flag without a code change.

### T4. Card shows human origin

- Files: `release_card_view.py` (`build_card_presentation`: optional trusted
  `origin={"kind": "human", "contributor_login", "contributor_id", "source_pr"}` read from
  the context's `human-intake.json`, never from PR text), `telegram_cards.py` (one line
  `👤 Human PR #N by <login> (id <id>)` under the title; login escaped and length-capped),
  bridge presentation validation in `protected_bridge_http.py` if it enumerates keys,
  `tests/test_telegram_cards.py`, `tests/test_protected_bridge_http.py`.
- Binding, nonce and callback handling unchanged; tests assert the binding digest is
  identical with and without `origin`.
- Rollback: revert; cards fall back to the generated layout.

### T5. Rehearsal script and evidence entries for intake

- Files: `apps/sb_bot/deploy/release-control/README.md` (new "Human PR intake" section:
  the CLI, the owner steps, what a refusal means, how to re-intake after a push),
  the SI evidence index referenced there (add SI-13 below), no code.
- **SI-13 (human intake)** in the disposable repo: allowlisted PR end to end to a healthy
  deploy with one tap; non-allowlisted refused; mid-intake push refused; secret in
  intermediate commit refused; contributor pushes to `clawdot/<id>` after intake → CI/merge
  fail closed; contributor pushes to `main` → ordinary cards blocked, reconciliation card
  clears it; original PR shows merged after the gateway PR merges; under D1(b) a fork PR
  from a Read member works and its CI gets no secrets.
- Rollback: revert docs.

### T6. Gateway CI routes authenticated by GitHub OIDC (needed for D1 a/c, per D6)

- Files: `publication_service.py` (`PublicationGatewayASGI`: accept an Actions OIDC token
  verified with the existing `oidc.py` verifier — issuer/JWKS/audience/expiry, numeric repo
  and owner IDs, hosted runner, `pull_request` event, approved CI workflow ref and sha in the
  installed closure, and PR number from the token's ref `refs/pull/<n>/merge` equal to the
  route's `<n>`; keep bearer token behind a config flag for the transition),
  `hosted_bootstrap.py` and `templates/p04-ci.yml.inactive` (`id-token: write`, request
  audience, drop the secret), `tests/test_publication_service.py`, `tests/test_hosted_bootstrap.py`.
- Tests: token for another PR, another workflow, a branch-push event, self-hosted runner,
  expired, wrong audience → 401/403; correct token → context/report as before; report
  upload for PR n with a token for PR m refused.
- Rollback: config flag back to bearer mode; revert PR.

### T7. Unexpected `main` movement alert (only for D1 c)

- Files: `watchdog.py` (new read-only check: GitHub `main` vs broker's recorded D and
  in-flight merge M; alert once per new unexpected SHA through the protected bridge),
  `templates/p10-watchdog.json.template`, `tests/test_watchdog.py`.
- No remediation; alert text has SHA prefixes and "reconciliation needed" only.
- Tests: main = D → no alert; main = recorded M during release → no alert; unknown SHA →
  one alert, repeated poll → no duplicate; GitHub API failure → bounded "unknown" state.
- Rollback: disable the check in config; revert.

Order: T1 → T2 → T3 → T4; T5 after T3; T6 and T7 are independent of T2–T4 and only needed
per D1/D6. T1–T4 are local source changes and can merge before any bootstrap; they stay
inert until §3 is installed and `human_intake_enabled` is set.

## 6. Done when

- T1–T5 merged (plus T6/T7 per decisions), full release-control suite green, Sol and Claude
  reviews clean.
- Disposable rehearsal (B13 + SI-13) evidence recorded.
- Owner gives the production install OK (D5) separately; nothing in this plan does.
