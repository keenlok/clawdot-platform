# Reusable reminders and website watches

The jobs plugin adds persisted reminders and public website text watches. It requires PostgreSQL and has no LLM or Tavily dependency. When PostgreSQL is absent, the plugin is not loaded and no volatile jobs are created.

## Enabling access

Group settings start disabled. A bot owner or delegated setup admin assigned to that specific group, or a current Telegram group creator or administrator, can request an enablement change with `/jobtools reminders on` or `/jobtools watches on`; confirm the resulting prompt. Setup assignments are refreshed from the administration store when the change is confirmed. Telegram group roles are looked up live. A global bot administrator without owner, group assignment, or current Telegram admin rights cannot manage group jobs. Other platform adapters fail closed unless they implement the optional live group-admin capability.

DM tools are disabled unless `JOBS_DM_ENABLED=true` is set. When enabled, each DM remains a separate scope. Global administrators and users delegated to at least one setup group may use their own DM; other users need a fresh membership check in an allowlisted group. DM jobs are never routed into a group or shown in group listings. Owners can still request cancellation after a tool is disabled, subject to the same live DM membership check.

Group administrators can reduce per-user and per-group active-job quotas and raise the minimum watch interval when enabling or disabling a tool:

```text
/jobtools watches on max_jobs_per_user=5 max_jobs_per_chat=30 minimum_interval_seconds=900
```

The command rejects unknown settings, duplicate values, invalid numbers, and any attempt to exceed process-wide ceilings. Existing jobs remain when a quota is lowered; new jobs are refused until usage falls below the new limit. A disable confirmation names the number of currently active jobs it will cancel and states that cancellation is permanent across all topics. The commit reply reports the actual count cancelled. Re-enabling does not restore jobs or stale confirmation requests.

## Reminders

Use a strict ISO local date/time and an IANA timezone:

```text
/remind 2026-11-01T09:00:00 Asia/Singapore Call the clinic
```

The confirmation includes the local wall time, IANA zone, numeric UTC offset, and exact UTC instant. A future date/time must be within 366 days. Ambiguous daylight-saving times and nonexistent spring-forward times are rejected. To choose one side of an ambiguous fold, include an offset that matches the named zone, such as `2026-11-01T01:30:00-04:00 America/New_York`; timezone abbreviations are not accepted. If the requested time passes before confirmation, request a new reminder.

Use `/reminders` to list your jobs in the current chat and topic, `/reminders all` for the current group/topic as a live group admin, and `/cancelreminder <job-id>` to request cancellation. The matching neutral tools are `create_reminder`, `list_reminders`, and `cancel_reminder`. Writes always require confirmation.

## Website watches

Use `/watch <seconds> <public URL>` or `create_website_watch` after the URL has appeared in the current conversation. For example:

```text
/watch 3600 https://example.org/news
```

Only HTTP/HTTPS on ports 80 and 443 is supported. URL userinfo and fragments are rejected. Each check calls the framework `safe_get` URL guard, including DNS resolution and every redirect. Private, loopback, link-local, metadata, CGNAT, and configured local destinations are blocked. No cookies, custom headers, request bodies, authentication, selectors, browser automation, or scripts are supported.

The watch extracts readable text from `text/plain`, `text/html`, or `application/xhtml+xml`. Cosmetic markup and whitespace changes are quiet. The first successful fetch establishes a silent baseline; unchanged readable text stays silent. A changed snapshot produces one bounded, escaped plain-prose notification. Fetched text is treated as untrusted data and is not stored in ordinary conversation history or sent to an LLM.

Each response is limited to 256 KiB, 10 seconds, and 20,000 extracted characters. Watches run no more often than every 300 seconds by default, with a maximum interval of seven days. After three consecutive retrieval failures, one actionable error notification is sent for that error episode; retries use bounded backoff. Recovery resets the episode. A blocked destination stops the watch and sends one safe reason. Unsuccessful, empty, disallowed, or truncated responses do not replace a good snapshot.

Use `/watches`, `/watches all`, and `/cancelwatch <job-id>` to list or cancel watches with the same topic and ownership rules. The matching tools are `list_website_watches` and `cancel_website_watch`. Lists show status and delivery uncertainty, not fetched page excerpts.

## Cancellation, privacy, and delivery limits

Cancellation or disablement committed before a worker reserves a notification prevents that notification. Reservation is the durable boundary: if cancellation races after reservation, a notification already starting may still arrive. The bot cannot retract a message accepted by the chat platform.

The scheduler records a unique delivery occurrence before sending. Once a send starts, a crash or timeout can make the outcome unknown. Unknown sends are never automatically replayed, which prevents deliberate duplicate delivery but can lose a notification if the process dies after reservation and before the platform accepts it. This is not an exactly-once delivery system. Reminder lists show unknown/failed delivery state rather than claiming success.

Completed and failed job records are retained for 30 days by default. Active future jobs survive normal message-retention purges. `/forgetme` removes the user's jobs and delivery content in each purged chat; `/purgechat` removes that chat's jobs, settings, snapshots, and deliveries. A send already accepted by the platform cannot be retracted by a later purge.

## Operator settings

All settings use the `JOBS_` prefix. Defaults and hard bounds are validated at startup:

| Variable | Default | Purpose |
|---|---:|---|
| `JOBS_DM_ENABLED` | `false` | Enable tools in DMs globally |
| `JOBS_MAX_JOBS_PER_USER` | `10` | Active jobs per owner and chat/topic |
| `JOBS_MAX_JOBS_PER_CHAT` | `100` | Active jobs across all members/topics in one chat |
| `JOBS_MAX_JOBS_GLOBAL` | `1000` | Active jobs across the deployment |
| `JOBS_MINIMUM_WATCH_INTERVAL_SECONDS` | `300` | Process-wide watch interval floor |
| `JOBS_MAXIMUM_WATCH_INTERVAL_SECONDS` | `604800` | Process-wide watch interval ceiling |
| `JOBS_WORKER_CONCURRENCY` | `2` | Maximum deployment-wide active worker leases |
| `JOBS_POLL_SECONDS` | `1` | Scheduler poll interval |
| `JOBS_EXECUTION_DEADLINE_SECONDS` | `20` | Fetch and pre-send work deadline |
| `JOBS_SEND_DEADLINE_SECONDS` | `15` | Platform send deadline |
| `JOBS_LEASE_SECONDS` | `60` | Postgres claim lease; must exceed both deadlines combined |
| `JOBS_RETENTION_DAYS` | `30` | Terminal job and delivery retention |

The plugin migrations are additive and apply with the other package migrations. It starts only when a PostgreSQL database is configured. No remote smoke tests or live reminders are part of local verification.
