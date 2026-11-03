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

### Changed

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
