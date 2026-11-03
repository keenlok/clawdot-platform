# P01 local Git evidence collector plan

## Outcome
Implement a read-only, fail-closed collector that constructs the existing immutable `ReleaseBundle` from verified local Git objects. It must report D→B, B→H, and D→H changes, commit/tree identities and ancestry, and reject any private/protected path or unsupported Git entry mode found anywhere in commits introduced from D through H, including content later removed.

## Ordered work

1. **Specify bounded interface and errors** — add `git_evidence.py` with a small public collector accepting a repository path, D/B/H object IDs, policy manifest, and recorded same-tree continuation flag. Return the existing `ReleaseBundle`; expose only bounded error codes through a dedicated `GitEvidenceError`. (SI-04, SI-09, SI-12)
2. **Isolate local Git reads** — resolve the Git executable once; invoke only fixed read commands with a minimal environment, temporary HOME, disabled system/global config and replacement objects, no hooks or external diff/textconv, protocol/network disabled, bounded stdout/stderr and per-command timeout. Never checkout, fetch, invoke a shell, or execute source. Validate object IDs and repository object availability before trusting them. (SI-09)
3. **Derive immutable evidence from objects** — resolve D/B/H commit and tree IDs, ancestry predicates, and complete raw no-rename change tuples for all three pairs. Enumerate every commit in H not reachable from D and inspect its per-parent deltas (or root tree), rejecting private/protected paths and non-regular modes (including symlinks and gitlinks); this catches introduced files that were later deleted without treating unchanged D content as a new change. Mechanical completeness must leave `complete_delta_reviewed` false; only a separate trusted review process may attest that gate. (SI-04, SI-08, SI-09, SI-12)
4. **Add temporary-repository regression coverage** — exercise normal ancestry/full deltas, unknown/diverged history, added-then-removed private/protected paths, symlink/submodule modes, malformed or truncated command output, time/output bounds, and hostile environment/config/replacement settings without reading private configuration. (SI-04, SI-08, SI-09, SI-12)
5. **Review and verify** — run focused tests and Ruff, inspect the complete diff against baseline, repair any findings, and update this plan with exact results and remaining integration/bootstrap evidence. Do not claim full-delta review means human review or secret scanning.

## Files
- `packages/plugin-release-control/src/chatcore_release_control/git_evidence.py`
- `packages/plugin-release-control/tests/test_git_evidence.py`
- This plan.

## Acceptance
- D/B/H values and tree identities come from local Git object reads; ancestry comes from the actual commit graph.
- All three deltas are complete, deterministic, bounded, and fail closed on malformed/truncated data.
- History scanning covers every commit reachable from H but not D, independent of the net diff.
- Git cannot use ambient config/replacement refs, hooks, external diff/helpers, network transports, or candidate source execution.
- No checkout/index/worktree mutation and no private config file content reads occur.
- Mechanical completeness does not set `complete_delta_reviewed`; extracted bundles remain ineligible until a separate trusted review process explicitly attests the full delta. This collector cannot assert human review, secret-content scanning, trusted GitHub provenance, or authorization.


## Implementation and evidence (2026-10-31)

Implemented the read-only collector in `git_evidence.py` and synthetic temporary-repository tests in `test_git_evidence.py`.

- `collect_release_bundle` returns the existing immutable `ReleaseBundle` with commit/tree IDs from local objects, graph ancestry, and complete no-rename D→B, B→H, and D→H deltas.
- The collector always returns `complete_delta_reviewed=False`. Its evidence alone cannot make a release eligible; the caller must obtain and explicitly supply a separate trusted review attestation.
- Introduced history comes from `rev-list --parents`; every commit is compared with each parent, including merge parents. Root commit trees are inspected. This catches private/protected paths and non-regular modes when added, modified, removed, or merged, including a path later deleted. An unchanged protected/private path inherited from D is not treated as a new change.
- Git runs with a minimal environment and an isolated temporary bare gitdir pointed at the repository object store. Ambient variables, local/system/global config, replacement refs, hooks, external diff, and protocols cannot affect these reads. Shallow repositories and object stores with alternates fail closed because completeness cannot be established by this local reader.
- `verify_installed_blob_digests` verifies an operator-supplied exact path→SHA-256 map against regular blob contents at the pinned workflow commit. It supports workflow and helper files. It does not infer whether that map contains the full transitive execution closure; the operator/bootstrap evidence must establish completeness.
- No checkout, fetch, source execution, private INI read, GitHub operation, or external network access is performed.

### Verification

- `uv run --package chatcore-release-control pytest packages/plugin-release-control/tests/test_git_evidence.py -q` — **14 passed**.
- `uv run ruff check packages/plugin-release-control/src/chatcore_release_control/git_evidence.py packages/plugin-release-control/tests/test_git_evidence.py` — **passed**.
- A direct local-worktree proof against baseline commit `0568954a14575625c42375e581343c22169a9e63` returned its actual tree `9ff79a05591e671800a9d6d0e76a5962b6b8cffc`, complete object extraction, an empty D→H delta, and no review attestation.
- Focused tests cover add-then-remove private/protected paths, unchanged protected baseline, ancestry/divergence, symlink/submodule rejection, malformed diff bytes, ambient/local config isolation, bounded subprocess output/time, workflow/helper digest mismatch, and review-gate separation.

### Self-review

**PASS after repair.** Initial review found that scanning every introduced commit's complete tree would also reject unchanged protected files already in D. Replaced it with per-parent commit delta inspection and added an unchanged protected-baseline regression test. Re-read the implementation and tests after the repair; the final focused checks above apply to that code.

### Remaining P01 work

This is a local prototype, not a complete installed release control. Callers still need to authenticate the D/B/H claims and policy source, establish operator-reviewed completeness of the installed workflow/helper digest map, supply same-tree continuation provenance, and perform independent secret-content scanning. This work does not establish GitHub CI/check provenance, human review, publication immutability, OIDC identity, P02/P03 integration, bootstrap permission/isolation evidence, or production readiness. The main chat should independently review and integrate this module. No commit, push, PR, installation, or production action was performed.

Main review repair: introduced-history inspection previously rejected protected/private
paths but accepted a nonallowlisted path added and removed between D and H. A real
Git fixture reproduced this omission. The validator now checks every introduced path
against the exact allowlist, and the fixture passes. The gitlink fixture isolates the
mode check from the independently rejected `.gitmodules` path. Final focused result:
15 passed; Ruff passed. Human review remains false on mechanically extracted bundles.

A second main review fixture showed replacing a baseline symlink with a regular file
hid the old unsupported mode. Raw diff extraction now validates both old and new
nonzero modes, including removal/conversion; the reproduced case is rejected.
Updated focused result: 16 passed; Ruff passed.
