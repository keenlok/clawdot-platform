# Display time zones — release handoff

Worktree: `/path/to/worktrees/display-timezones/sb_bot`
Branch: `codex/display-timezones`
Release base: `fb84b149701f`, including the merged memory JSON fix.
Implementation by Luna; independent review and fix review by Sol using in-session subagents.
All findings are resolved. The user authorized PR creation, merge and deployment on 2026-10-31.

## Delivered behavior

- `/timezone <IANA-zone>` and `/timezone reset` apply immediately. Group writes require a fresh
  native group-admin lookup; personal preferences are self-managed in the sender's own DM.
- Group replies use the shared group setting, otherwise labelled UTC. DM replies use only the
  sender's saved DM preference, otherwise UTC. Private preferences are not disclosed in groups.
- `/time [IANA-zone]` and read-only model tools give current time; an explicit zone is a one-off
  override. The model receives authoritative current UTC and converted display-time context.
- Direct job, poll, research and administration timestamp outputs use one formatter with zone
  names and numeric UTC offsets. DST folds remain distinguishable.
- Additive migration `0007_display_time.sql` persists preferences. User deletion removes personal
  preferences; chat deletion removes all preferences in that chat. Shared group settings remain
  on user deletion.

Stored instants, worker execution, explicit-zone reminder/poll parsing and creation labels,
intervals, budget windows, retention, date-only values, expense report policy and rental wall-clock
values are unchanged. No dependencies or frozen contracts changed.

## Verification

Final combined non-browser, non-sandbox suite on current main plus this feature:

`UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest -m 'not browser and not sandbox_integration' -q`

**1661 passed, 1 skipped, 22 deselected in 65.38s**. PostgreSQL tests used a newly created temporary
local development database, removed after the run. The shared dev database has an old migration
0005 checksum mismatch and was preserved. All marked Postgres, Valkey and Elasticsearch checks
available in this run passed. Browser and real Docker sandbox tests were excluded.

Ruff and whitespace checks are clean. Sol reviewed the full diff and all fixes with no remaining
findings. The previously known poll-topic failure was a stale fixture clock; its pending action
expired before the real confirmation path ran. The test now uses the current UTC clock before
proposing, preserving expiry checks and topic assertions; all 142 poll tests pass.

## Release operations

The normal deployment script backs up production Postgres when migrations change, builds frozen
dependencies, recreates only the bot service and waits for healthy status. Migration 0007 is
additive; rollback can leave the unused preference table intact.

No new environment variables or inferred defaults. Preferences start empty, so replies use labelled
UTC until an authorized person configures a group or their own DM. Group commands use existing
bot-addressed routing, for example `/timezone@example_bot Asia/Singapore`.

Model-written conversions remain probabilistic; `/time` is deterministic. Explicitly supplied
zones can be compared; another member's private preference is never automatically looked up.

Release metadata and post-deploy verification are recorded locally in ignored
`reports/display-timezones-release.md`. No live Telegram test messages are required.
