# Backup, split and clean-history execution record

Status: **Live** — backups, deployment split and clean-history publication complete;
documentation consolidation prepared (2026-11-03). The owner selected fresh public history, retaining earlier history
and operational evidence privately.

## Completed sequence

1. Private encrypted database backups and an isolated restore drill established recovery
   before the split. The job verifies its intended mounted filesystem, streams encryption,
   publishes successful ciphertext atomically, limits retention to owned files and preserves
   unrelated jobs. Media, network paths, schedules and recovered data counts remain private.
2. A working `apps/_scaffold` now exercises the generic core, console adapter, packaged persona
   and minimal plugins. Platform CI tests wiring, independent wheel installation and a pinned
   container. The search downloader and generic release-control templates ship as package
   resources, without workspace-only runtime paths.
3. Private bot wiring, persona, composites, app integration tests and operator files moved to
   deployments. Reviewed platform release pins, independent CI, deployment/rollback/forward
   drills and an actual automatic deployment established the separate deployment boundary.
   Platform no longer imports or packages the private application.
4. Current-tree review removed private operator records and identifiers. The owner chose a
   disconnected public repository containing only this audited snapshot and a following
   release-version commit. Historical PR references point to the private history archive.

## Publication contract

Both clean commits must satisfy the privacy audit: no actual infrastructure identifiers,
Telegram identities, travel records or private application persona. Synthetic test fixtures,
generic provider protocols, reserved/loopback addresses and the working scaffold persona
remain useful public examples. No private proofs or runtime secrets enter these commits.

The public repository has no imported old objects, refs, tags or authors. Root version
`0.0.0` bootstraps the snapshot; `0.1.1` follows so trusted-main CI can compare the first
parent and publish the release tag. A platform release never triggers bot deployment.

The inactive release-control example and validation use generic network and model-volume
aliases. The allowed network set and capability checks remain exact; this is a protected compatibility
name change, not permission expansion. Cross-repository activation still requires new review
and fresh policy evidence. Core, plugin, adapter, active browser and snapshot runtime bytes
remain unchanged; privacy-sensitive test examples are synthetic.

## Verification and private records

Platform verification includes locked installation, lint, changed-file formatting, the
full required test suite with disposable Postgres, independent scaffold wheels/container,
current-tree and two-commit history scans, and object/author ancestry checks. The private
publication audit records exact source provenance, exceptions, changed-file hashes and results.

Original operator records and earlier development history remain in the
[private history archive](https://github.com/keenlok/clawdot-platform-history) and private
deployment records. Generic feature plans and stable architecture section references remain
available here. Recurring off-box delivery and offline-key handling are private operational
follow-ups and do not change the framework boundary.

## Documentation consolidation (owner-approved housekeeping)

Prepared after clean-history publication: all 105 plan/index files now share `docs/plans/`,
and the handover is under `docs/handovers/`. Existing `docs/superpowers/specs/` remains in
place. Git moves preserve file history; no filename collisions required deduplication or
renaming. Current links and repository path references use the canonical locations, while
private archived snapshot URLs retain their original paths.

The documentation checks enforce one plan/handover home, exhaustive plan/spec/handover
indexing, and valid local Markdown file links across all tracked documents. Regression
cases cover broken links outside the index, references resolved from each document,
reference-style links, paths with spaces and literal code examples. Root release version
and its existing tag remain unchanged. Framework edits only update documentation comments
or module docstrings; permission logic and runtime behavior are unchanged.
