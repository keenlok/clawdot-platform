# Changelog

Notable changes for platform users and application maintainers.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `/explain [topic] [section]` and read-only `read_explainer` retrieve maintained, packaged
  architecture/plugin documentation with fresh scoped tool availability and optional exact
  source links. Each built-in plugin owns its `EXPLAIN.md`; third-party plugins can opt in
  through `BasePlugin.explainer_package`. The same sources generate wiki pages, checked for drift.
  Retrieved documentation is untrusted evidence, so a subsequent write in the same turn can
  require confirmation. No new dependency, service or migration is required.

## [0.1.3] - 2026-11-03

### Fixed

- OpenAI-compatible native tool calls with `finish_reason=stop` now reach the normal tool flow.
  A bounded regeneration can recover a truncated native call without executing its incomplete
  payload. Source-derived Google Doc revisions use original-package import instead of rebuilding
  from plain text; broader rewrites must be explained.

## [0.1.2] - 2026-11-03

### Added

- Confirmed bounded text edits in native Google Docs and literal range edits in Sheets,
  with frozen destinations, content preconditions and read-back verification. Docs use
  revision-checked UTF-16 body ranges; Sheets previews disclose the collaborator race.
- Qualified native copy fallback and DOCX edit copies imported from the original package
  into a new Google Doc. `GOOGLE_COPY_QUALIFIED=false` keeps copying/conversion disabled
  until separately authorized owner-grant checks pass; originals use existing API scopes.
- Additive Google migration `0003_edit_results.sql` journals destination IDs and partial or
  uncertain outcomes. Unresolved reservations survive normal cleanup for at least
  `GOOGLE_EDIT_UNRESOLVED_RETENTION_DAYS` (default 90); privacy deletion still removes them.
- Optional Google Docs browser trial broker and dedicated editor-account worker for exact
  replacement and bold operations on new native copies. The worker is isolated behind the
  guarded proxy; the broker shares only the confirmed copy and verifies the saved result through
  the Google API. `GOOGLE_BROWSER_ENABLED=false` and copy qualification remain disabled by
  default. The trial remains unqualified and undeployed; no live Google edits
  have been run.
- Shared egress proxy primitives now have a renderer-independent core location, preserving the
  public renderer's compatibility surface. The proxy extraction is under [PR #6](https://github.com/keenlok/clawdot-platform/pull/6).

### Changed

- Google browser trial instructions state that API editing is the default, browser editing
  requires an explicit request and confirmation, and an API failure never triggers browser fallback.
- Consolidated plans in `docs/plans/` and handovers in `docs/handovers/`, with updated
  references and repository-wide local link checks.
- Downstream applications adopting 0.1.2 must update their exact platform source pin, lock,
  configuration and migration manifest together; the normal migration path applies Google
  migration `0003`. Keep `GOOGLE_COPY_QUALIFIED=false` and `GOOGLE_BROWSER_ENABLED=false` until
  the separate owner-grant and browser qualification checks pass. This release does not enable
  either feature.

## [0.1.1] - 2026-11-03

### Added

- Reusable core, console/Telegram adapters, capability plugins and standalone browser
  renderer; release-control code is included but inactive.
- Working generic scaffold with CI tests, installed-wheel checks and a pinned container build.
- CI-gated immutable Git release tags for explicitly pinned downstream applications.

[Unreleased]: https://github.com/keenlok/clawdot-platform/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/keenlok/clawdot-platform/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/keenlok/clawdot-platform/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/keenlok/clawdot-platform/tree/v0.1.1
