# Storage architecture explainer implementation plan

**Goal:** Explain how Postgres, Valkey and Elasticsearch cooperate in a persistent
Clawdot application, with a reusable image and a wiki entry prepared for publication.

**Authorization:** The owner approved the proposed guide, visual explainer and wiki
scope and requested a plan and PR on 2026-11-03. This is documentation-only work.
No runtime code, service configuration, permissions, dependencies or wiki settings change.

**Roles:** Codex plans and executes under the model-unavailable fallback in
`AGENTS.md`: Claude CLI was found but failed authentication because its OAuth session
expired. Codex reviews the diff; the PR records the unavailable Claude final review.

## Design and files

Keep one detailed guide as the canonical explanation. Use an editable SVG with a PNG
export for portable GitHub rendering, and Mermaid for the two detailed flows. A concise
wiki entry reuses the image and links to the guide, avoiding two independent manuals.
The public scaffold remains an in-memory example; a downstream app chooses the
persistent stores and plugins. Historical architecture intentions are not implementation
guarantees.

| File | Responsibility |
| --- | --- |
| `docs/storage-architecture.md` | Roles, write/search flows, memory, wiring, isolation, outages, recovery and source links |
| `docs/images/storage-architecture.svg` | Editable overview with labeled application-to-store connections |
| `docs/images/storage-architecture.png` | Rendered image used by the guide and wiki |
| `docs/wiki/Storage-architecture.md` | Copy-ready wiki overview with absolute public image/guide URLs |
| `docs/wiki/README.md` | Enable/initialize/publish instructions and update procedure |
| `README.md` | Discoverable link to the guide |
| `docs/plans/README.md` | Index this plan as a documentation record, with the PR link |

## Execution checklist

- [x] Inspect the public repository separately from the historical local checkout;
  create `codex/storage-architecture-explainer` from public `main`.
- [x] Audit `store/base.py`, `store/pg.py`, `store/es.py`, `store/valkey.py`,
  `app.py`, `limits.py`, `embed.py`, the search/memory plugins and scaffold wiring.
  Check indexing order, buffering, fallback scope and whether `/reindex` exists.
- [x] Write the guide with relative source links. Explain durable records, temporary
  TTL keys and derived search documents. Show application-driven indexing rather than
  a direct database replication arrow. State that lost cache state is not reconstructed
  wholesale, and background index failures have no durable replay queue.
- [x] Draw and render the SVG. Label persistence, temporary state, embeddings and
  application-controlled data flow; include meaningful image alt text and a nearby
  text explanation. Inspect the PNG for clipping, legibility and correct arrows.
- [x] Prepare the wiki entry and instructions. Its absolute URLs target public `main`
  after merge. Wiki is disabled at planning time; do not claim publication or change
  repository settings. Manual publication follows merge and wiki initialization.
- [x] Add navigation and this plan's index row. Review for private hosts, credentials,
  app-specific deployment assertions and claims unsupported by source.
- [x] Stage the documentation before running `tests/test_plans_index.py`, whose
  Markdown link scan uses Git's tracked file list. Run the required checks:

  ```sh
  uv sync --locked --all-packages
  uv run ruff check packages services apps tests .github/scripts
  uv run ruff format --check .
  uv run pytest -q -m "not browser and not sandbox_integration"
  git diff --check
  ```

  Record skipped service checks and pre-existing formatting failures without changing
  unrelated files. Use the existing documentation/link tests; add no implementation-
  mirroring tests for prose or images. Validate the SVG as XML and visually inspect
  its PNG export. Rendering tools may be temporary, outside the repository.
- [x] Review the final diff, commit and push the branch, open the public PR, add its
  URL to the plan index and report the wiki publication prerequisite.

## Acceptance and limits

The PR contains a guide, readable image, accurate flow diagrams, source references,
README navigation and reviewable wiki source. It makes no claim of a running deployment,
automatic ES backfill, cache continuity, scaffold service auto-configuration or wiki
publication. Publishing the wiki is a separate action from merging the repository PR.

## Verification record

- `uv sync --locked --all-packages`: passed.
- Required Ruff lint: passed; changed Markdown formatting: six files passed.
- Repository-wide Ruff formatting: 14 pre-existing historical plan files would be
  reformatted; none are changed by this PR.
- Full requested test selection: 3,359 passed, 58 skipped, 21 deselected.
- Staged documentation/link tests: five passed, including all new Markdown paths.
- SVG XML parsed successfully; PNG rendered at 1560 × 1020 and visually inspected.
- Staged diff passed whitespace and Gitleaks checks.
- Independent Codex review checked implementation claims and SVG semantics and found
  no actionable issues. Claude review could not run because its OAuth session expired.
- Wiki was disabled during initial preparation; the owner enabled it on 2026-11-03
  and authorized publication after review fixes and merge.
- Separate integration selection: 98 passed, 55 skipped; `TEST_PG_DSN` was not
  configured and Elasticsearch was unavailable.
- Public PR: [#2](https://github.com/keenlok/clawdot-platform/pull/2).

## PR review follow-up

The owner supplied a Claude Code review on PR #2 and requested fixes, merge and wiki
publication. Codex verified both findings against the source and documented the
historical cache prefix defaults and pending-buffer eviction at the 1,000-key limit.
This supplied review supplements the independent Codex review; the original CLI
authentication limitation remains part of the execution record.
