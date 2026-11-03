# Changelog

Notable changes for platform users and application maintainers.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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

## [0.1.1] - 2026-11-03

### Added

- Reusable core, console/Telegram adapters, capability plugins and standalone browser
  renderer; release-control code is included but inactive.
- Working generic scaffold with CI tests, installed-wheel checks and a pinned container build.
- CI-gated immutable Git release tags for explicitly pinned downstream applications.

[Unreleased]: https://github.com/keenlok/clawdot-platform/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/keenlok/clawdot-platform/tree/v0.1.1
