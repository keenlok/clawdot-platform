# Clawdot self-improvement implementation plan

> **For agentic workers:** Execute inline in this worktree. This plan is scoped to a local, fail-closed policy prototype; it does not authorize operational bootstrap.

**Goal:** Add tested release-policy primitives for immutable candidate identity, full deployed-to-candidate delta review, trusted check provenance, and phase-specific admission decisions.

**Architecture:** Create a standalone Python workspace package with pure validation functions and immutable value types. Keep it disconnected from the Telegram bot, GitHub, Postgres, Actions, Docker, and production deployment. Its API will require verified inputs and reject incomplete, inconsistent, or out-of-policy evidence. Add documentation that distinguishes what these local checks establish from the live proof still required by SI-01–SI-12.

**Tech Stack:** Python 3.12, standard library, pytest, Ruff, uv workspace.

---

## Brief and acceptance criteria

- Preserve existing extension behavior and all user work in the primary checkout and other worktrees.
- Use exact immutable commit/tree/policy/workflow identities; never infer trust from labels, names, abbreviated SHAs, or PR-only diffs.
- An ordinary candidate must account for the full deployed `D` to candidate `H` delta. `B` must be `D` or have recorded same-tree continuation provenance, and the complete delta must be explicitly supplied.
- Reject changed paths outside an exact installed allowlist, protected control files, private configuration/backup paths, symlinks/submodules, malformed object IDs, and missing or mismatched CI evidence.
- Separate pre-merge checks, expected merge reconciliation, and first deployment-switch admission so an expected `B→M` transition does not invalidate the release, while stale sequence/fence/config/main state blocks a new switch.
- Never claim an in-memory policy prototype is a durable broker, authenticated Telegram bridge, sandbox, host service, or proof of production safety.
- Do not rotate credentials, alter production roles/schema, register runners, route Telegram ingress, deploy, commit, push, or open a PR.

## Scope mapping

| SI item | Local work | Status after local work |
| --- | --- | --- |
| SI-01 | Document runner isolation as a pending disposable-host proof | Operational proof pending |
| SI-02 | Strict installed-policy manifest model and exact-path validation | Unit-tested policy logic; real App/API permission proof pending |
| SI-03 | Document protected Telegram bridge and token rotation boundary | Not implemented; owner/bridge proof pending |
| SI-04 | Immutable B/H/M relationship validation and merged-blocked result | Unit-tested decision rules; GitHub race rehearsal pending |
| SI-05 | Bind CI evidence to repo, workflow SHA, run/attempt, B/H/tree, creator and success | Unit-tested evidence checks; actual GitHub App/API proof pending |
| SI-06 | Separate merge and first-switch admission predicates with sequence/fence/identity guards | Unit-tested predicates only; durable atomicity/race proof pending |
| SI-07 | Document dedicated transactional ledger/outbox as external work | Not implemented; DB/Telegram restart proof pending |
| SI-08 | Reject protected paths and non-regular content in policy inputs | Unit-tested path checks; host/container isolation pending |
| SI-09 | Enforce exact immutable identities and private-path rejection | Unit-tested model only; full Git history/secret/egress pipeline pending |
| SI-10 | Model allowed host journal states and truthful recovery outcomes | Unit-tested state constraints only; process-kill/health proof pending |
| SI-11 | Record account/App capability verification as bootstrap work | No account claim; live capability mapping pending |
| SI-12 | Require explicit full D-to-H review bundle and known deployed-base provenance | Unit-tested release eligibility; disposable repository scenarios pending |

## Planned files

- Create `packages/plugin-release-control/pyproject.toml` for a dependency-free workspace package.
- Create `packages/plugin-release-control/src/chatcore_release_control/__init__.py` and `policy.py` for strict immutable identities, policy manifests, paths, check provenance, delta eligibility, merge reconciliation, switch admission, and recovery state validation.
- Create `packages/plugin-release-control/tests/test_policy.py` for behavioral and hostile-input cases.
- Modify root `pyproject.toml` to include and resolve the new workspace package; update `uv.lock` only through uv.
- Create `docs/self-improvement-prototype.md` describing the implemented boundary and per-SI pending proof matrix.

## Ordered implementation steps

### Task 1 — Package skeleton and manifest contract (SI-02, SI-11)

1. Add tests first for strict JSON parsing, duplicate-key rejection, numeric repository/owner identifiers, exact workflow closure digests, exact allowed paths, and rejection of unknown keys or wildcard allowances.
2. Run `uv run pytest packages/plugin-release-control/tests/test_policy.py -q`; confirm the new imports/API fail before implementation.
3. Implement immutable, bounded policy value types and canonical SHA-256 digesting using only the standard library.
4. Run the focused package test and `uv run ruff check packages/plugin-release-control`.

### Task 2 — Full-delta and protected-path eligibility (SI-04, SI-08, SI-12)

1. Add failing tests for `B == D`, recorded same-tree descendant continuation, unexplained or rewritten bases, incomplete D→H bundles, unknown inherited D→B changes, protected paths, `.ini`, private `.env` variants, backups, case variants, symlink/submodule objects, and renamed paths.
2. Verify the focused tests fail because the release evaluator is absent.
3. Implement an evaluator that returns a bounded typed decision and preserves separate D→B and B→H change lists; ordinary eligibility requires a complete D→H review bundle and acceptable recorded base provenance.
4. Run focused tests, then Ruff.

### Task 3 — Check provenance and merge reconciliation (SI-04, SI-05)

1. Add failing tests for wrong repository/event/workflow/ref/SHA, stale attempt, foreign creator, unsuccessful/skipped check, wrong tested tree, and valid evidence tied to exact B/H/tree.
2. Verify failure before implementation.
3. Implement check-evidence matching and merge reconciliation against immutable M, expected B/H parents and expected tree; unexpected effects return `merged_blocked` and never become deployable.
4. Run focused tests, then Ruff.

### Task 4 — Phase-specific admission and recovery state (SI-06, SI-10)

1. Add failing tests for approval expiry/revocation before first switch admission, outdated sequence/fence/config/policy/deployed identity/current-main M, expired start deadline, and admission after newer maintenance. Cover recovery transitions that must retain the same operation and deny a fresh switch.
2. Verify failure before implementation.
3. Implement pure admission and journal-transition validators. They accept caller-verified state and do not persist it; no API may treat a timed-out attempt or prepared state as proof of deployment.
4. Run focused tests, then Ruff.

### Task 5 — Documentation, compatibility, and verification (all SI items)

1. Document how to use the pure local policy API and state clearly that it is not connected to runtime authorization.
2. Add a compatibility test ensuring the existing extension package remains unchanged and run its non-integration suite.
3. Run the new package suite, relevant existing extension suite, full workspace unit suite excluding explicitly marked infrastructure tests, and Ruff for changed Python files.
4. Review the complete diff against the approved Revision 3 and this plan, record any findings and dispositions, and report every missing live proof without presenting it as passed.

## Verification commands

```sh
uv run pytest packages/plugin-release-control/tests -q
uv run pytest packages/plugin-extensions/tests -q -m 'not integration and not sandbox_integration'
uv run pytest -q -m 'not integration and not browser and not sandbox_integration'
uv run ruff check packages/plugin-release-control
```

Do not run production/deployment scripts, Docker sandbox experiments, Telegram calls, or GitHub writes. These require the disposable or owner-controlled environments specified in the accepted design.

## Execution and self-review record

- Added `packages/plugin-release-control` as dependency-free, pure local policy code; added no runtime wiring or external service.
- **R-L01 — Required-check set completeness:** An early API version validated individual check records but did not require every policy check exactly once. Added failing missing/duplicate-set cases, then implemented exact set validation. Resolved.
- **R-L02 — Journal operation identity:** An early transition predicate validated state names but did not prevent a different operation ID from continuing the journal. Added failing same/different operation ID cases, then bound transitions to one validated operation ID. Resolved.
- **R-L03 — Invalid Unicode manifest input:** Self-review found a lone surrogate could escape the bounded `PolicyError` path as `UnicodeEncodeError`. Added a failing parser case, then normalized Unicode encoding/decoding failures to `PolicyError`. Resolved.
- **R-L04 — Backup filename exception:** Self-review found the private-path classifier exempted source-looking names such as `backup.py`, which could also be backup content. Added a failing allowlist case and removed that exception. Resolved.
- Focused verification: `uv run pytest packages/plugin-release-control/tests packages/plugin-extensions/tests -q -m 'not integration and not sandbox_integration'` — **117 passed, 10 deselected**.
- Static/lock/diff verification: `uv run ruff check packages/plugin-release-control`, `uv lock --check`, and `git diff --check` — **passed**.
- Full unit suite: `uv run pytest -q -m 'not integration and not browser and not sandbox_integration'` — **1,540 passed, 1 skipped, 1 failed, 116 deselected**. The failure is `packages/plugin-polls/tests/test_service.py::test_real_core_button_confirmation_preserves_topic`; reproduced unchanged on a clean archive of base `0568954a14575625c42375e581343c22169a9e63`. It is unrelated and pre-existing.
- Self-review result: **PASS for the local policy-prototype scope**; **BLOCKED for end-to-end self-improvement readiness**. SI-01, SI-03, SI-07, most SI-08/SI-09/SI-10 operational controls, and all live bootstrap acceptance remain pending. Pure predicates trust caller-supplied evidence and do not authenticate, extract, scan, persist, or enforce it.
