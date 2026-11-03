# Group polls

Polls require PostgreSQL and an adapter with native polls (currently Telegram). They start **off**
for every group. An owner, a delegated setup admin for that group, or a current native group
administrator can request `/polltools on`; confirm the prompt to enable them. `/polltools off`
also needs confirmation and rechecks permission and the settings revision. Turning polls off
blocks new polls; existing polls can still be listed and closed. DMs do not support polls.

In sb_bot groups, address commands to the bot username, for example
`/poll@your_bot Dinner? | Ramen | Tacos`. The examples below omit that suffix for readability.

```text
/poll Dinner? | Ramen | Tacos | Pho
/poll --multi --anon What should we order? | Noodles | Rice | Dumplings
/poll --close 2026-11-05T18:00:00 Asia/Singapore Dinner? | Ramen | Tacos
/poll dates 2026-11-07..2026-11-16
/poll --single dates 2026-12-31..2027-01-01 New year's meetup?
/polls
/closepoll 3
/polltools off
```

Flags come first, in any order, with no repeats. `--single` and `--multi` are mutually exclusive.
Straight or curly double quotes around the question are optional. Separate options with `|`.
Questions support 1–300 characters, and each option supports 1–100. Polls need **2–12 options**
(or fewer if the operator/adapter sets a lower maximum), with no empty or case-insensitive duplicate
options. Ordinary polls default to **single choice and named**. `--anon` makes votes anonymous.
Date ranges are inclusive, default to multiple choice, and produce ordinary native polls with
labels such as `Sat 7 Nov`; a range crossing a year includes years in the labels. `--single`
overrides the date preset's default. The default date question is “Which dates work?”.

`--close` requires an ISO local datetime **with seconds** and an IANA timezone. Close times must
be at least 60 seconds ahead and within 30 days by default. DST gaps and ambiguous wall times
are rejected; a matching explicit UTC offset can disambiguate a fold. A tool proposal includes
the exact question, numbered options, anonymity, choice mode, destination/topic and close instant.
The `create_poll` tool only proposes; the requester must confirm before anything is posted. A
literal `/poll` command posts directly because the requester typed the exact content.

`/polls` lists open polls only in the current group and topic. `/closepoll <number>` closes that
poll, or reply to its native poll message with `/closepoll`. Only its creator or a group manager
may close it. The bot posts counts, a winner/tie/no-votes line, and named voters when available;
long voter lists end with `+N more`. Multi-choice results say voters could pick several options.
Named votes are observed through platform updates and may be incomplete after downtime. Final
counts come from `stopPoll`, including for anonymous polls: **anonymous vote updates are not
available**, so their authoritative results are obtained at close. Users can retract or change
named votes, and the stored voter list reflects the latest received answer.

## Closing and recovery

A small in-plugin scan reads indexed due poll rows and claims expiring leases. Both the scan and
`/closepoll` use the same close routine; only the winner of a fenced database update posts results.
A manual close makes the poll due immediately so failed attempts can be retried by the scan.
Stop failures retry after the lease expires, up to three attempts by default. After that the poll
is recorded as closed and results fall back to observed named votes, or “Final results are
unavailable.” for anonymous polls. A poll may remain open on the platform after those failures.
Result-message send failures are logged by error class and are not retried.

This scan deliberately does not add a job kind to `plugin-jobs`. Its `scheduled_jobs.kind` check,
worker, commit authorization, enablement settings, quotas and listings branch on `reminder` and
`watch`. Adding poll closing would need a jobs migration, a dependency on poll storage or a
callback registry, and changes to deployed scheduler code. Polls already have durable `close_at`
rows, so one indexed scan plus a lease keeps one close path without coupling the plugins.
Telegram's native `open_period`/`close_date` is deliberately unused: a natively closed poll cannot
then be stopped to obtain authoritative final totals, and has no hook for posting these results.

Reservations prevent automatic duplicate sends for the same confirmation, including concurrent
commits. A crash during a native send can leave a `sending` row; it is not replayed automatically
because delivery may already have happened. Operators should inspect uncertain sends. A crash
after stopping a poll can make final totals unavailable; retries eventually use the fallback.
Fencing prevents competing workers from posting results twice, but a crash after recording
`closed` and before sending results can lose that notification.

## Operator settings and privacy

The package depends only on `chatcore`. Its additive migration runs before startup. Without
`PG_DSN` the plugin is disabled. Console with PostgreSQL starts normally, replies that polls are
unsupported, and starts no closer task. Access is refreshed before every scan; a refresh failure
skips that tick, and a removed group receives no close/result calls.

All settings have the `POLLS_` prefix:

| Setting | Default | Bounds |
|---|---:|---|
| `MAX_OPTIONS` | 12 | 2–12 |
| `MAX_OPEN_PER_CHAT` | 20 | 1–200 |
| `MAX_OPEN_PER_USER` | 5 | 1–50 |
| `CLOSE_HORIZON_DAYS` | 30 | 1–90 |
| `MIN_CLOSE_LEAD_SECONDS` | 60 | 5–3600 |
| `SCAN_SECONDS` | 30 | 1–300 |
| `LEASE_SECONDS` | 60 | 10–600 |
| `MAX_CLOSE_FAILURES` | 3 | 1–10 |
| `RETENTION_DAYS` | 30 | 1–3650 |

Open and sending polls count toward quotas across all topics in a group. `/forgetme` deletes the
user's stored votes and polls they created within each purged group; `/purgechat` deletes all poll
rows, votes and enablement settings for that group. Terminal closed/failed rows are retained for
30 days by default; active polls remain. These hooks delete database records and cannot retract
messages or polls already accepted by the platform.
