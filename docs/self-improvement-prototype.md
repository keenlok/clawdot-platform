# Self-improvement release-control prototype

Historical record of the first, policy-only increment. Current scope and progress
are tracked in [the ten-part delivery plan](superpowers/plans/2026-10-31-self-improvement-parts.md).
Subsequent parts add executable collectors, verification and persistence; statements
below describe the earlier snapshot.

This change implements a small local foundation for the approved Revision 3 design. The new
`chatcore-release-control` package contains deterministic validation functions and immutable value
types. It has no application wiring or external side effects. Existing extension behavior remains
separate and unchanged.

## Local behavior

- A strict policy manifest binds numeric repository and owner IDs, a target, workflow content
  digests, exact allowed/protected paths, and required check creator IDs.
- Candidate path validation rejects wildcard policy entries, unapproved/protected paths, private
  `.ini` and `.env` files, common backup/credential artifacts, non-blob objects, and renames.
  `.env.example` is the one literal environment-file exception.
- Ordinary-release evaluation requires a complete D→H review claim. It returns the D→B and B→H
  segments separately, requires B to equal deployed D or have recorded descendant/same-tree
  provenance, and applies path policy to all three deltas.
- Required CI evidence must all match the trusted check set, creator App, repository, `pull_request`
  event, workflow path and content digest, workflow commit, run and attempt, candidate commit/tree,
  and successful conclusion.
- Merge reconciliation accepts only the expected B/H parents and expected merge tree. Any
  unexpected result is `merged_blocked`.
- First-switch admission checks affirmative and active approval, expiry, current owner/policy,
  current main M, target sequence, fence, full deployment identity, and a bounded start deadline.
- Journal transitions reject skips, operation-ID changes, and attempts to restart from a terminal
  state.

## Proof boundary by SI item

| SI | Implemented locally | Still required outside this checkout |
| --- | --- | --- |
| SI-01 | No runner code or production authority was added. | Disposable whole-runner sandbox compatibility, credential/host denial, cleanup, fail-closed startup, resource and hosted-CI test isolation proofs. |
| SI-02 | Strict exact-path policy model and trusted-check identities. | Create selected GitHub Apps; verify actual scopes, API write boundaries, workflow dependency closure and token renewal in a disposable private repository. |
| SI-03 | No Telegram bridge or approval path was added. | Protected bridge as sole Bot API token owner, callback/card ownership tests, ordinary message/file compatibility, maintenance rotation and proof the old token is unusable. |
| SI-04 | Pure merge parent/tree reconciliation returns `merged_blocked` on mismatch. | GitHub base/head race, merge conflict, force-push and already-merged-but-blocked rehearsal. |
| SI-05 | Pure required-check evidence matcher binds exact workflow/run/attempt/repository/tree/creator. | Authenticated paginated GitHub API collector, synthetic merge checks, stale/missing/skipped check scenarios, and actual App evidence. |
| SI-06 | Pure merge/switch predicates check phase-specific state, expiry/revocation, sequence, fence and deployment identity. | Durable broker/host atomic admission, outbox, lock/fencing, cancellation/lost-response/crash boundary tests and receipt reconciliation. |
| SI-07 | No ledger, DB migration, role, or Telegram update consumer was added. | Maintenance-created dedicated schema/role, transactional decision/outbox/ack tests, replay, restart, owner removal and token rotation proofs. |
| SI-08 | Exact protected paths and non-blob objects fail policy validation. | Installed host service, fixed build inputs, extraction/symlink/Compose/migration validation, and disposable host proof. |
| SI-09 | Deterministic IDs and restricted paths are modeled. | Immutable bare/quarantine Git ingestion, all-new-history/blob/metadata/prose/artifact scanning, real secret-canary tests, exact refspec publication, and default-deny egress in both candidate environments. |
| SI-10 | Pure journal state transitions rule out invalid jumps. | Durable journal, real container inspection, failure injection at broker and host boundaries, healthy restoration, bounded diagnostics and resource/watchdog evidence. |
| SI-11 | No GitHub account capability is asserted. | Verify actual private-repository plan, App endpoint permissions, runner and concurrency features at bootstrap; keep Free-plan limitations in the installed policy. |
| SI-12 | Full D→H review is required as an input and unknown base provenance blocks ordinary eligibility. | Trusted diff extractor and owner-visible review card; disposable tests for inherited undeployed code, merged-failed content, rewritten main, group-role denial and explicit reconciliation. |

The policy functions trust their inputs. The booleans and change lists in `ReleaseBundle` are
assertions from a caller, not proof that Git history was fetched or reviewed. The GitHub workflow
SHA and content digest must be verified by trusted orchestration; a candidate process cannot supply
its own trustworthy provenance. The checks are not cryptographic source scanning and do not
prove that credentials are absent from file contents, commit messages, PR prose, artifacts, logs,
or intermediate Git history.

## Bootstrap status

The only verification completed in this worktree is local unit/static checking. No GitHub App,
Telegram owner, production database, production bot token, hosted/self-hosted runner, Docker host,
or disposable release environment was accessed. The release feature remains disabled and requires
separate implementation and owner-reviewed bootstrap evidence before any installation.
