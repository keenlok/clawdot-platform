# Current trip itinerary execution checklist

Status: Planned; executing the owner-approved [design](2026-11-05-current-trip-itinerary.md).

Goal: resolve implicit trip updates to the saved current document, edit it in
place when supported, and advance to a copy only under explicit confirmation.

## Roles and isolation

- Parent coordinates integration, full verification, public release and private
  CI-gated deployment. Work uses the existing isolated platform and deployment
  checkouts on `codex/current-trip-itinerary`.
- Luna implements plugin/runtime/tests and downstream composite/tests separately.
- Sol independently reviews permission boundaries, state races and regressions.
- Claude auth probe returned loggedIn=false; available-model fallback applies.
- Public test fixtures use synthetic trip/file/chat identities. No live Google
  mutation, historical uncertain action replay or automatic pending-edit approval.

## Checklist

- [x] Trace the reported request through stored user turns, tool arguments,
  pending preview and output journal using read-only database transactions.
- [x] Write, index and obtain owner approval of the design and same-Doc default.
- [ ] Record the focused prechange baseline and demonstrate a failing regression
  for the missing `trip_edit_doc` tool and saved-primary follow-up behavior.
- [ ] Implement saved-primary resolution, internal proof freezing/revalidation,
  historical attachment references, explicit copy advancement and stale-target
  protection in the Google plugin. Keep mutations bounded and confirmed.
- [ ] Prove that a previously edited native primary is updated without a create,
  copy or DOCX-download call. Preserve its earlier edit and selected Docs tab.
- [ ] Exercise memory and isolated PostgreSQL cases for version conflict,
  identity/scope/settings revocation, failure and duplicate completion. A Google
  edit that already succeeded must be reported separately from association failure.
- [ ] Update downstream composite routing and request/catalog fixtures so an
  implicit trip request uses saved files, and a missing primary produces selection
  rather than a stale source edit. Keep unrelated app settings unchanged.
- [ ] Run Sol review and resolve all material findings with reproducing tests.
- [ ] Run locked sync, lint, changed-file format and repository full non-browser,
  non-sandbox-integration suites, with disposable local Postgres. Record baseline
  skips/warnings/unchanged format exceptions explicitly.
- [ ] Update operator/explainer/generated documentation, architecture decision,
  changelog and plan status/index. Verify package boundaries and skill ownership.
- [ ] Build anonymous source installs, bot and renderer images, run installed
  wheel/scaffold smoke checks, and compare reviewed packaged Google resources.
- [ ] Open/review/merge the public patch release PR, verify push CI and immutable
  release tag. Pin all downstream sources and lock origins to that exact release.
- [ ] Run downstream full checks, migration-manifest checks, encryption guard,
  gitleaks and anonymous image builds. Open/review/merge private deployment PR.
- [ ] Verify ordinary CI-gated deployment health, installed platform commit and
  Google source hashes, sharing default, browser-disabled state, preserved tutor
  and owner/admin settings, rollback images and migration backup when applicable.
- [ ] Present the exact existing-trip selection preview privately and obtain the
  normal confirmation before any live association write. Do not approve or replay
  the pending activity edit; its replacement requires a fresh correct-source preview.
- [ ] Record installed receipts, remove the task-owned disposable database, and
  report the deployed behavior with any pending trip recovery clearly separated.

## Verification commands

Platform (run from the platform worktree):

```sh
uv sync --locked --all-packages
uv run pytest -q packages/plugins/google/tests -m "not browser"
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check .
uv run pytest -q -m "not browser and not sandbox_integration"
uv run pytest -q tests/test_package_boundaries.py tests/test_plans_index.py
```

Use `TEST_PG_DSN` pointing only at the task-owned disposable local database;
integration fixtures create isolated schemas. Format only changed files when
whole-tree baseline exceptions remain unchanged.

Deployment (run from the private deployment worktree after the exact release pin):

```sh
uv sync --locked --all-packages --python 3.12
uv run ruff check bots shared tests
uv run ruff format --check bots shared tests
uv run python shared/check_encryption.py
uv run pytest -q -m "not browser and not sandbox_integration"
```

Never print runtime environment values, access tokens, database DSNs or decrypted
secrets while collecting receipts. No new source key is passed to builds.
