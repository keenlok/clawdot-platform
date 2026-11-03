# Reusable Reminders and Website Watches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The parent orchestrator owns implementation and review; this planner does not commit or implement.

**Goal:** Add reusable, explicitly enabled, chat-isolated reminders and public website watches with a durable Postgres scheduler.

**Architecture:** One `chatcore-jobs` plugin owns settings, jobs, snapshots and delivery records. It receives platform-independent authorization and delivery dependencies from the app, uses existing pending-action confirmations, and runs a bounded Postgres-backed worker. Group capability checks are live and separate from global administrator privileges; DM jobs use their actual DM scope.

**Tech Stack:** Python 3.12, uv workspace, asyncio, asyncpg/Postgres, zoneinfo, existing `safe_get`, trafilatura for bounded HTML text extraction, pytest and deterministic fakes.

---

## Inspection and boundaries

Base: `afa88c4ed3ee44be7c47dfa4b995db2d63601de3`, branch `codex/basic-tools`, isolated checkout `/path/to/worktrees/basic-tools/sb_bot`.

Read `ARCHITECTURE.md`, `plans/COMMON.md`, core plugin/platform/access/confirmation/tools/store code, Telegram adapter, web plugin, app composition and deployment scripts. Historical `plans/COMMON.md` freezes Phase 0 contracts for its old parallel tracks. This design avoids changing existing required protocol methods: add an optional role capability and explicitly register worker cleanup in the app resource stack. Do not broaden `AccessPolicy.is_admin`, `Command.admin_only`, `Tool.admin_only`, or alter existing builtins. Do not read or depend on the self-extension or dm-admin-permissions worktrees.

Current code has no scheduler, no live group-admin method, no per-group plugin registry, and no general shutdown hook. `Stores.db` and package migrations already support the new repository. `ToolRegistry` checks argument presence/names but does not validate JSON Schema value types: every handler must validate actual values. `ConfirmService` claims actions before calling handlers and reconstructs button contexts without the topic; use the stored action topic, never the button-context topic, at commit. Existing `WebPlugin` requires Tavily; jobs must work without it. Existing Telegram send retries `RetryAfter` and exposes no transport idempotency token.

No remote commands, live bot messages, live reminders, deployment, production credentials, pushes, PRs, or merges belong to this implementation. Local tests must use fake platforms. A local immutable source artifact and verification record are the handoff.

Parent baseline evidence: nonintegration suite 517 passed, 1 skipped, 13 deselected; integration suite 10 passed, 3 skipped (Elasticsearch unavailable), 518 deselected. Disposable local Postgres is already running in `sbot-basic-tools-pg`, with test DSN `postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test`; use isolated per-test schemas. This is a disposable local test credential, not production access.

Choose SQL scheduling over process-only timers because restart, quotas and races require durable coordination; an external queue adds deployment infrastructure without removing the SQL/delivery consistency boundary. Choose explicit ISO + IANA zone input over natural-language parsing to make exact execution time testable. This file is the implementation task record: update checkboxes and add verification evidence here during execution.

## Acceptance criteria

| ID | Observable criterion |
|---|---|
| J01 | Reusable package depends on core and generic libraries only; reminders and watches share one scheduler and are usable without an LLM or Tavily. |
| J02 | Group settings default disabled. A configured owner or delegated setup admin assigned to that group, or a live current group creator/administrator, can enable/disable each tool and reduce limits within global ceilings. Global-admin-only access is insufficient. Store assignments refresh at confirmation; revocation denies management. Unsupported role resolution or lookup failure denies the live-role branch. Global admin semantics and builtins remain unchanged. |
| J03 | Every user-facing read/write filters current chat, scope type, owner where required, and topic. Ordinary members create/list/cancel only their jobs; live group admins explicitly request group management. DM jobs never map to a membership group and never appear in group lists. |
| J04 | Write tools and commands request confirmation. Commit revalidates actor, chat, topic destination, current authorization, enablement, quotas and timing. Pending action ID provides creation idempotency. Arbitrary recipients, headers, credentials and code are rejected. |
| J05 | One-shot reminders accept an explicit ISO local date/time and IANA zone, show local date/time + zone + UTC offset + exact UTC instant, reject past/nonexistent/ambiguous times unless an explicit matching offset disambiguates, and persist UTC. Lists and cancellation work after restart. |
| J06 | Watches perform bounded public HTTP text retrieval through `safe_get` on every check/redirect. First successful fetch establishes baseline silently; unchanged normalized text stays silent. Changes produce bounded text differences; persistent errors produce one actionable notification per error episode. |
| J07 | Postgres is authoritative for schedules, limits, claims, retries, cancellation and delivery records. Restart recovers pending work; two workers do not claim the same run; concurrency and intervals are enforced across worker processes. |
| J08 | Cancellation/disable is checked before fetch and before delivery; completed cancellation prevents later unreserved delivery. An already-started send may finish and cancellation truthfully states this boundary. |
| J09 | A unique durable delivery record prevents replay of known sent/reserved occurrences. Crash or timeout after send starts yields delivery-unknown, never automatic replay. User documentation makes possible loss/ambiguous delivery explicit; no exactly-once promise. |
| J10 | Fetch content is data only, never instructions; no LLM in worker. Content returned by tools is untrusted; notifications are escaped/bounded plain prose. Logs contain IDs/classes/counts/hostnames only. No cross-chat snapshots/cache or user-provided credentials exist. |
| J11 | Deterministic tests cover isolation, authorization changes, timezone boundaries, quotas, cancellation races, restart, duplicate delivery, guard failures and quiet watches. Postgres integration proves actual transaction behavior where local services are available. |
| J12 | App starts/stops worker safely only with Postgres, additive migrations package correctly, privacy purge removes scoped data, and local immutable handoff documents configuration, tests and delivery limits. |

## Decisions and interfaces

### Scope and permissions

Use a frozen `Scope(chat_key: str, is_dm: bool, thread_id: str | None)` and `Actor(user_key: str)` constructed from trusted `ToolContext`/`CommandContext`. A group configuration is keyed by `chat_key` (all topics share group enablement/quotas); a job is keyed by generated UUID plus `chat_key`, `is_dm`, `owner_key`, and `thread_id`. Every list/cancel receives a Scope, even when a job ID is supplied. Ordinary listing returns only the actor's jobs in the current topic; an explicit `all_members=true` list/cancel path requires current setup authorization for that group or a fresh group-admin lookup and stays in the current topic. Setup authorization is `AccessPolicy.can_setup` (owner or group assignment), separate from global admin status. Store authorization refreshes at confirmation. A group manager cannot inspect DM jobs. No tool accepts `chat_key`, owner, destination, platform or credentials.

Add optional `GroupAdminPlatform` protocol in `chatcore/group_access.py` with `async is_group_admin(chat: ChatRef, user: UserRef) -> bool`. Telegram implements it using live `get_chat_member`, accepting only `creator`/`administrator`; mismatch of platform, lookup exception, unsupported capability, and DM scope deny the live-role branch. Group setup permission is `access.can_setup(user, group)` OR that live-role branch. A global admin who is neither an owner nor assigned and lacks live group role is denied. Refresh the administration snapshot before management confirmation; if refresh fails, ignore the stale setup snapshot and allow only a successful fresh live-role check. Do not consult cached membership or `ctx.is_admin` for the live group-role branch. Console may implement explicitly configured fake roles for development or simply fail closed. Tests use an adapter-local fake; existing `FakePlatform` need not gain a required method.

Plugin receives `AccessPolicy` or narrow async callbacks for allowed group / authorized DM checks. Execution rechecks allowlist and enabled group settings. DM authorization must be live before create confirmation and dispatch: global admin remains allowed by the existing DM policy; otherwise directly query membership in allowlisted groups without the one-hour cache. Fail closed if none are proven. No DM-to-group routing is allowed. Owner cancellation remains available after tool disable so jobs can be cleaned up; denied access must never reveal job contents.

DM tools are controlled by global `JOBS_DM_ENABLED`; there is no group admin management of a DM. DMs allow existing global admins, users assigned to at least one setup group, or users with freshly proven membership in an allowlisted group. Group enablement affects only that group. User credentials are unsupported rather than stored globally: URL userinfo is rejected and no authenticated fetch/header field exists. Global bot transport credentials stay in the adapter and are not copied into plugin state.

### Commands and neutral tools

Expose `create_reminder(text, local_time, timezone)`, `list_reminders(all_members=false)`, `cancel_reminder(job_id)`, `create_website_watch(url, interval_seconds)`, `list_website_watches(all_members=false)`, `cancel_website_watch(job_id)`, and `configure_jobs(tool, enabled, max_jobs_per_user?, max_jobs_per_chat?, minimum_interval_seconds?)`. All create/cancel/configure tools use `writes=True` and return PendingAction. `configure_jobs` deliberately has `admin_only=False`: its handler performs the separate live group-role check. Disabled create tools may remain in neutral tool specs but return a clear disabled result and cannot write; broad registry filtering is out of scope.

Provide deterministic slash-command access: `/remind <ISO time> <IANA zone> <text>`, `/reminders`, `/cancelreminder <id>`, `/watch <seconds> <url>`, `/watches`, `/cancelwatch <id>`, `/jobtools <reminders|watches> <on|off>`. `/reminders all` and `/watches all` require live admin; cancellation of another member's job requires the same. Group settings command can accept named numeric reductions with explicit parsing. Commands share service validation with tools and use `ctx.confirm.request`; they never bypass confirmation. Each handler rejects unknown fields and bool-as-int, oversize text, excessive URL length, malformed IDs and configuration outside ceilings.

Stored PendingAction payload contains only validated kind-specific input and trusted scope/actor; commit compares action.chat/requester to context and reconstructs destination from action.chat/thread_id. Never take a destination from tool arguments. Creation summaries include exact reminder time or watch URL/interval/destination and retention/quiet behavior. Cancel/configure summaries identify scope and the exact change. Confirmed past reminders fail with a request to choose a new future time, rather than silently rescheduling.

Settings include a monotonically increasing revision captured in every pending create/configure action. Any configuration change increments it; commit rejects stale revision even if the tool was disabled and re-enabled between request and confirmation. Cancellation compares current job state/revision so an old cancellation request cannot apply to a replaced job. Disable atomically cancels active jobs of that kind rather than merely suspending them; re-enable never resurrects old jobs or pending creates. Tests must cover disable/re-enable before confirmation and before send.

Watch URL must have provenance in `ctx.turn.seen_urls` for tool requests; slash command URLs are human-supplied. Confirmation authorizes that exact URL for future bounded checks. At commit validate scheme/userinfo/port and fetch only later through `safe_get`; never turn off guard checks based on confirmation. Do not canonicalize away userinfo before validation. Reject nonempty fragments or strip them consistently in both summary and stored URL. No arbitrary selectors, scripts, browser automation, headers or request bodies.

### Defaults and global ceilings

Settings in `chatcore_jobs/settings.py`: group tools disabled by default, DM tools enabled only if `JOBS_DM_ENABLED=true`; default maximum 10 active jobs per user per scope, 100 per chat, 1000 globally; hard maximum configurable only through environment. Watch minimum 300 seconds, maximum interval 7 days; reminder horizon 366 days; 1000 characters reminder text; 2048-character URL; fetch 256 KiB, 10 seconds and 20,000 extracted characters. Worker concurrency 2 globally, poll 1 second, execution deadline 20 seconds plus bounded send deadline 15 seconds, lease 60 seconds. Configure Pydantic bounds so lease exceeds entire execution deadline and no group setting can raise a global limit. Group admins may reduce quotas and increase minimum interval. If quota is reduced below current occupancy, preserve existing jobs but reject new ones; disable cancels queued runs and suppresses future delivery.

Use separate global and scoped transactional quota locks, acquired in consistent order (global, chat), for confirmed creation. Count active jobs only; pending confirmation does not reserve quota, so quota is checked again at commit. Claim due runs with a transaction-level global scheduler lock and a count of valid active leases before `FOR UPDATE SKIP LOCKED`; this makes concurrency a deployment-wide bound, not just an asyncio semaphore. Treat tenant IDs as values in SQL, never SQL identifiers.

### Time semantics

`parse_reminder_time(local_time, timezone, now)` returns an aware UTC datetime and canonical confirmation label. Parse ISO datetime with `datetime.fromisoformat`; resolve zone with `ZoneInfo`. For naive input, round-trip fold=0 and fold=1 candidates through UTC, discard candidates that do not return to the same local wall time, deduplicate by UTC instant, and require exactly one candidate. For aware input, require the supplied offset to match the named zone at the computed instant and the same wall time. Reject timezone abbreviations, missing zone, nonexistent DST times, unknown zones, date-only input, past or excessive-horizon instants. Require seconds precision in display, preserving exact stored instant. Watch interval means elapsed seconds, not wall-clock recurrence.

### Persistence and state transitions

Add package-owned additive migration with `job_settings`, `scheduled_jobs`, `job_deliveries`. Settings key `(chat_key, is_dm)`; jobs contain UUID, scope, owner, kind enum, status enum (`active`, `completed`, `cancelled`, `failed`), JSON kind payload, source action ID unique per chat, created time, UTC due time, integer occurrence, lease token/expiry, failure count, error episode flag, bounded snapshot/hash, and last-delivery state. CHECK constraints validate kinds/statuses and interval positivity; indexes cover `(status, next_run_at)` and `(chat_key, is_dm, owner_key)`. All state changes take the scoped ID plus lease token when worker-owned.

Deliveries contain `(chat_key, job_id, occurrence, notification_kind)` unique, status (`reserved`, `sent`, `unknown`, `suppressed`), bounded content, optional platform message ID, timestamps. No global snapshot dedup or cross-chat caches. SQL foreign key cascade permits scoped privacy purge. Payloads must be schema validated on read; malformed historical records fail closed without execution. Retention deletes terminal jobs/deliveries after configured retention (default 30 days); active future jobs survive age-based message retention. Hard purge cancels and deletes jobs/snapshots/delivery content for matching user/chat; do not accidentally purge other chats owned by that user.

`JobsRepo` public methods always take Scope for user operations: `settings(scope)`, `configure(scope, ...)`, `create(scope, actor, action_id, payload, now)`, `list_jobs(scope, actor, kind, all_members=False)`, `cancel(scope, actor, job_id, ...)`. Worker-only methods `claim_due(now, limit)`, `finish_run(claim, ...)`, `reserve_delivery(claim, text, kind)`, `finish_delivery(claim, message_id)` operate on trusted claimed records with scoped predicates/token fencing. Internal unrestricted due scanning is never exposed as a tool. Repository construction requires a real Database; tests can supply an explicit in-memory fake, production never silently falls back to volatile jobs.

Claim commits a lease and occurrence identity. Fetch runs outside SQL transactions. Before recording a result, lock job row and confirm lease token, active status, current allowlist/enablement and matching occurrence. Cancellation locks the same job row and clears future activity. Reservation is the delivery linearization point: a cancellation committed before it guarantees no delivery. A cancel racing after reservation returns `cancelled; an already-started notification may still arrive`. Keep reservation/send ordering explicit in tests. Before the actual send, re-read eligibility and mark reservation suppressed if cancellation is already visible, narrowing the unavoidable race.

Successful unchanged watch advances schedule to `now + interval` (no backlog bursts). First fetch saves baseline and remains quiet. Successful changed watch atomically updates snapshot and creates one delivery candidate for that occurrence. Network/HTTP retry uses bounded exponential delays (30, 60, 120 seconds, capped at configured interval); after three consecutive failures emit one actionable error and continue at interval with capped backoff. A success resets failure episode. BlockedURL is terminal: mark failed, emit one safe actionable reason, no subsequent HTTP retries. HTTP non-2xx, disallowed/empty extraction and truncated response count as failures; do not replace good baseline with an error page or truncated content. Distinguish watch fetch errors from notification delivery errors.

Reserve delivery durably before transport call. A successful send writes message ID and sent state; normal future polling never sends it again. Stale `reserved` after process death becomes `unknown` and is not retried, using an inclusive expiration boundary. Until a reservation is finished or recovered, its job is excluded from claims and it continues to consume a global worker slot even if its original claim lease expires. Send exception/timeout is also unknown because adapter exceptions cannot prove non-delivery. A reminder with unknown delivery is terminal and visible in owner's list; never claim delivered in this case. This prioritizes duplicate suppression over eventual delivery: a crash between reservation and send can lose a notification. Retry/backoff applies to fetch/pre-send processing, not ambiguous transport sends. Platform retry on explicit Telegram RetryAfter is retained; no additional generic transport retry is added. Lists show status/next run/last delivery uncertainty so failures are actionable without repeated messages.

Expired fetch claims without reservation can safely retry using a new fencing token; an old process must fail its final conditional write. Bound fetch/send calls with asyncio timeouts below lease duration; fence every commit. The scheduler is not a distributed exactly-once message system. A process paused beyond a lease cannot be prevented by SQL from finishing a network send already in progress: the no-replay reservation rule prevents a second worker from intentionally re-sending that occurrence.

### Text watching and trust

`fetch_snapshot(url)` calls `safe_get` with text/plain, text/html, application/xhtml+xml only. Existing net guard owns DNS/IP pinning, redirect validation, credentials rejection, Tailscale/CGNAT/metadata/private address blocking. Do not implement a competing HTTP path. Check HTTP status and `FetchResult.truncated` explicitly. Extract HTML with trafilatura without comments, normalize Unicode whitespace deterministically, and hash bounded normalized text with SHA-256. Reject empty/unusable or extraction-over-limit data rather than hashing an arbitrary prefix. The precise meaning of meaningful change is change to normalized readable text; cosmetic HTML/whitespace changes are quiet. No semantic LLM comparison.

Build a bounded deterministic diff using `difflib`, escape markdown metacharacters, remove control characters, prefix with `Website text changed` and quote snippets as website data. Maximum notification fits one platform message (conservatively 1500 characters); never render arbitrary fetched links or mentions as transport markup. Reminders use the same plain-text escaping. Do not include raw exception messages, URL queries in logs, or untrusted instructions in prompts. Tool lists that include fetched excerpts must return `untrusted=True`; simpler lists should contain metadata/status only.

## File map

Create:

- `packages/plugin-jobs/pyproject.toml`: `chatcore`, `trafilatura` dependencies and uv build metadata.
- `packages/plugin-jobs/src/chatcore_jobs/__init__.py`: public exports.
- `packages/plugin-jobs/src/chatcore_jobs/settings.py`: validated ceilings/defaults.
- `packages/plugin-jobs/src/chatcore_jobs/models.py`: Scope, Actor, job/claim/delivery dataclasses and enums.
- `packages/plugin-jobs/src/chatcore_jobs/timing.py`: pure timezone parser/formatting.
- `packages/plugin-jobs/src/chatcore_jobs/repository.py`: scoped SQL and transactions.
- `packages/plugin-jobs/src/chatcore_jobs/permissions.py`: live role/member checks and scope construction.
- `packages/plugin-jobs/src/chatcore_jobs/service.py`: shared validation/confirmation/commit operations.
- `packages/plugin-jobs/src/chatcore_jobs/plugin.py`: neutral tools, commands and privacy hooks.
- `packages/plugin-jobs/src/chatcore_jobs/watch.py`: guarded snapshot and bounded notification text.
- `packages/plugin-jobs/src/chatcore_jobs/worker.py`: persisted run/delivery state machine and lifecycle.
- `packages/plugin-jobs/src/chatcore_jobs/migrations/__init__.py` and `0001_jobs.sql`.
- `packages/core/src/chatcore/group_access.py`: optional group-role protocol, no mandatory ChatPlatform change.
- `packages/plugin-jobs/tests/conftest.py`, `test_timing.py`, `test_permissions.py`, `test_service.py`, `test_watch.py`, `test_worker.py`, `test_pg_jobs.py`: deterministic and integration evidence.
- `docs/reusable-jobs.md`: commands, exact timing, trust, limits, recovery and delivery caveats.

Modify:

- `packages/adapter-telegram/src/chatcore_telegram/__init__.py` and `tests/test_telegram.py`: live optional role lookup and tests.
- `apps/sb_bot/src/sb_bot/app.py`, `apps/sb_bot/tests/test_app.py`: package wiring only with db, cleanup callback, scoped send logging, migrations.
- `apps/sb_bot/pyproject.toml`, root `pyproject.toml` workspace source mapping, `uv.lock`: new package resolution.
- `apps/sb_bot/.env.example`, `apps/sb_bot/deploy/README.md`: operator knobs and local handoff; no secret values.

Existing core contracts, builtins, web fetching implementation, provider implementations and deployment scripts need no behavior change. Dockerfile copies packages wholesale and should package the new dependency through frozen uv lock without editing Docker logic.

## Ordered implementation tasks

Each test-first cycle is an independent work unit. Do not commit in the planner run; parent controls local checkpoints. Commands below run from the isolated checkout.

### Task 1: Package, pure timing and configuration (J01, J05)

- [x] Create package metadata/exports/settings/models/timing and workspace dependency entries from the file map. Use dataclasses with explicit immutable scoped identity; all durations are integer seconds and due times aware UTC.
- [x] Add parametrized timing tests: Singapore `2026-11-01T09:00:00` => `2026-11-01T01:00:00+00:00`; New York `2026-11-01T01:30:00` rejected ambiguous; `2026-11-01T01:30:00-04:00` and `-05:00` select distinct UTC instants; `2026-03-08T02:30:00` rejected nonexistent; invalid zone, offset mismatch, date-only, past and horizon all fail without producing pending actions.
- [x] Run `uv run pytest packages/plugin-jobs/tests/test_timing.py -q` before implementation and observe failures for the absent parser; implement the round-trip algorithm above and rerun for all passing cases.
- [x] Add settings boundary tests proving bool-as-int rejection, positive bounds, group settings unable to raise ceilings, and lease > work timeout. Run `uv sync --all-packages` and the same targeted tests.

Concrete test contract:

```python
def test_singapore_exact_time():
    now = datetime(2026, 10, 31, tzinfo=UTC)
    parsed = parse_reminder_time("2026-11-01T09:00:00", "Asia/Singapore", now)
    assert parsed.instant == datetime(2026, 11, 1, 1, tzinfo=UTC)
    assert "Asia/Singapore" in parsed.label
    assert "+08:00" in parsed.label
```

### Task 2: Optional live role authorization (J02, J03)

- [x] Add `GroupAdminPlatform` optional protocol and Telegram implementation with no changes to global `is_admin` or required ChatPlatform. Return false for nonmatching chat/user namespaces; translate Telegram errors to safe PlatformError.
- [x] Write failing Telegram tests mocking `get_chat_member` for creator, administrator, member, left, restricted and transport failure; assert a second call after revocation consults the platform again rather than cache.
- [x] Implement plugin permission functions using trusted Scope/Actor. A global admin who is not a current group admin cannot manage group tool settings. DM access uses existing global-admin allowance or freshly proven membership; unsupported adapters deny group management.
- [x] Run `uv run pytest packages/adapter-telegram/tests/test_telegram.py packages/plugin-jobs/tests/test_permissions.py apps/sb_bot/tests/test_admin.py -q`. Existing global builtin permission assertions must remain unchanged and pass.

### Task 3: Durable scoped repository and quotas (J03, J07, J08, J09, J12)

- [x] Add additive migration/schema and repository methods specified above. No generic execute-job field; only reminder/watch enum payloads. Use parameterized SQL and explicit scope arguments on all user operations.
- [x] Write integration tests with unique disposable schemas following `packages/core/tests/test_pg.py`. Populate group A, group B and Alice's DM with deliberately identical textual job IDs where schema permits or guessed foreign IDs otherwise; scope-specific list/cancel must return no foreign data and change no foreign rows.
- [x] Write concurrency tests with two independent pooled connections: one remaining quota slot accepts exactly one confirmed creation; two workers claim disjoint rows; global concurrent claims never exceed limit; stale token cannot finalize; same source action ID creates once. Implement transactions with deterministic lock ordering.
- [x] Test cancellation against a claim, against pre-reservation result, and after reservation using asyncio Events/barriers (never timing sleeps). Assert cancellation's documented linearization boundary and terminal state.
- [x] Run `uv run pytest packages/plugin-jobs/tests/test_pg_jobs.py -m integration -q`. Skips mean unverified integration, not passing concurrency evidence. If local Docker is available, use only disposable local Postgres and rerun; never connect to production or a remote DSN.

### Task 4: User tools, commands and confirmations (J02–J05, J10)

- [x] Write service tests with in-memory scoped repository fake and mutable role fake: create pending only; no job before confirmation; exact summary fields; outsider confirmation ignored; repeated action does not duplicate; revoke admin/disable tool/change quota before confirm denies commit; past-at-confirm rejects reminder.
- [x] Implement the shared service, pending handlers, neutral tool schemas and command parsers. Create/cancel/config writes produce PendingAction; list reads need no confirmation. `cancel` unknown/foreign ID yields the same nonrevealing result. Validate all actual argument types and reject destination/header/code/credential fields.
- [x] Test button topic preservation: request in topic 7, resolve with context thread None, job remains topic 7; a forged payload topic cannot redirect. Cross-topic list is empty. For action/requester/chat mismatch, commit makes no mutation.
- [x] Test setup managers/live group admins `all` only show or cancel current group/topic jobs, ordinary `all` denies, and managers cannot infer or cancel DM job metadata. Assert DM never calls `group_for_dm` routing.
- [x] Run `uv run pytest packages/plugin-jobs/tests/test_service.py packages/plugin-jobs/tests/test_permissions.py -q`; all assertions pass with fake platform, no external send.

### Task 5: Guarded deterministic watches (J06, J10)

- [x] Add fake guarded-fetch response fixtures for normalized same text, first baseline, changed text, hostile HTML/instructions/mentions, empty extraction, excess bytes/chars, non-2xx, blocked URL and FetchError.
- [x] Implement watch text extraction/diff as pure functions around injected `safe_get`; test allowed types and bounds are forwarded exactly. Prove every recurring check calls safe_get again, not a raw client or stored resolved IP.
- [x] Add tests for private/metadata/CGNAT/Tailscale URL, redirect to private address, URL credentials and DNS rebinding using existing net-guard mocked resolver/transport techniques. Keep real external networking disabled.
- [x] Run `uv run pytest packages/plugin-jobs/tests/test_watch.py packages/core/tests/test_b_net_guard.py -q`. Assert unchanged HTML whitespace emits no notification and instruction-like content never calls a provider/tool.

### Task 6: Worker state machine and delivery evidence (J06–J09)

- [x] Build deterministic `Clock`, fake fetcher, scripted fake sender and fake repository with production state semantics; worker exposes `run_once(now)` for tests plus lifecycle loop using injected sleeper/clock. Do not require real time to pass.
- [x] Test due reminder sends once to persisted chat/topic, future job stays queued, a fresh worker and new DB pool recover unreserved due work after lease expiry exactly once, completed run never replays, stale lease fenced, worker cancellation releases pre-send claims safely, and global limits match SQL integration behavior.
- [x] Implement claim -> authorize -> bounded execute -> validate claim -> reserve -> recheck -> bounded send -> persist outcome. If authorization fails or config disabled, no fetch/send. Do not hold a database transaction across network calls.
- [x] Test process death immediately before reservation (safe retry), immediately after reservation (unknown/no replay), exact reservation expiry, unresolved-reservation slot accounting, cancellation during blocked-watch pre-send authorization, immediately after platform acceptance before mark-sent (unknown/no replay), repeated polling after sent (zero duplicates), and transport timeout (unknown/no automatic retry). State must survive new worker object and, in integration, new DB connection.
- [x] Test watch quiet first baseline/unchanged, meaningful changed hash once, repeated failure once per episode, recovery resets episode, blocked URL terminal once, backoff schedule, no catch-up storm after long downtime. Confirm error notification contains safe actionable category and no raw exception/body.
- [x] Run `uv run pytest packages/plugin-jobs/tests/test_worker.py packages/plugin-jobs/tests/test_pg_jobs.py -q`; separately report integration skips.

### Task 7: App lifecycle, privacy and package regression (J01, J12)

- [x] Wire JobsPlugin only when db exists; no-PG app remains usable and cannot create volatile reminders. Append before migration application and PrivacyPlugin construction. Worker starts in plugin startup; register `worker.aclose` with AsyncExitStack after DB cleanup registration so it shuts down first. Constructor accepts trusted authorization/delivery callbacks; framework code imports no app or Telegram.
- [x] Test start twice creates one worker, cleanup twice is safe, worker stops before pool closure, initialization failure closes resources, jobs migrations included once and packaging imports work without Tavily/provider. Use fake send callback to preserve chat/topic and log accepted bot messages with existing store API.
- [x] Implement purge_user/purge_chat with scoped row locks, suppression of unreserved future sends and deletion of snapshots/deliveries; active future jobs are not swept merely because created_at predates message retention. Test same user in two chats retains other scope. Document in-flight send/purge caveat.
- [x] Run `uv run pytest apps/sb_bot/tests/test_app.py packages/plugin-jobs/tests -m 'not integration' -q`, then `uv run pytest -m 'not integration' -q` and `uv run ruff check .`; use `uv run ruff format --check .` after formatting changed files. Existing runtime/provider/admin/privacy tests must pass unchanged.

### Task 8: Review and immutable local handoff (J11, J12)

- [x] Document commands, default-disabled groups, DM global flag, UTC confirmation examples, group-role separation, supported public text pages, resource limits, quiet failures/recovery, cancellation boundary and delivery-unknown loss tradeoff in `docs/reusable-jobs.md` and operator README/env example.
- [x] Record exact targeted/full-suite commands and results, explicit integration skips, source commit/tree identity and dependency lock hash. Review source against J01–J12; include evidence references in final handoff.
- [x] Parent implementation/reviewer checks schema isolation, SQL predicates, pending-action validation, worker races, raw URL logging and arbitrary destination absence. No remote/live smoke test is authorized.
- [x] Prepare local immutable source archive from the reviewed committed tree if parent is authorized to create a local checkpoint; otherwise archive the exact staged snapshot with a content manifest and state that it is uncommitted. Calculate SHA-256 and record local path. Do not run deploy.sh even with its default host, do not publish, and do not create live test jobs.

## Material risks and review focus

1. **External delivery is not atomic with Postgres.** Reservation/no-replay prevents known duplicates but may lose a notification; display unknown honestly. Changing to at-least-once retries is a product-semantic change requiring explicit documentation.
2. **Live role checks depend on Telegram bot permissions.** If getChatMember is unavailable the feature must deny management instead of promoting global admins or trusting historical member rows. Telegram role integration needs mocked evidence now; any live validation is a separately authorized activity.
3. **Group configuration and owner quotas race with confirmation/dispatch.** Revalidate in transactions and fence leases; do not assume a pending action grants durable authority.
4. **Text websites can be noisy or inaccessible.** Semantic content hashing reduces cosmetic churn but cannot infer significance; errors, truncated content and dynamic pages must not replace baseline. No browser fallback.
5. **Privacy purge and cancellation cannot retract a send already accepted by the platform.** Document the reservation boundary and test races without claiming stronger guarantees.
6. **Integration test skips hide SQL errors.** Require successful disposable local Postgres runs for race claims whenever tooling can provide it; otherwise label the gap precisely in the handoff.
7. **Bounded implementation scope.** No cron/calendar recurrence, authenticated URLs, custom headers, arbitrary code handlers, generalized plugin installation, multi-destination routing, or LLM-generated background decisions. Per-group enablement is enforced in this package's handlers and worker; other plugins retain their existing behavior.

## Coverage review

J01: tasks 1/7; J02: tasks 2/4; J03: tasks 2/3/4; J04: task 4; J05: tasks 1/4; J06: tasks 5/6; J07: tasks 3/6; J08: tasks 3/6/7; J09: tasks 3/6/8; J10: tasks 4/5; J11: tasks 1–8; J12: tasks 3/7/8. No mandatory existing Phase 0 protocol change is required. Optional role capability is the only new platform interface. Implementation is authorized under the parent task; no additional design approval is required.


## Earlier implementation-stage evidence (before main merge and review repairs)

- Source checkout: `/path/to/worktrees/basic-tools/sb_bot`, branch `codex/basic-tools`, base/HEAD `afa88c4ed3ee44be7c47dfa4b995db2d63601de3`; implementation changes are uncommitted. The parent owns final review and the immutable local archive.
- Lockfile SHA-256: `7579242afd7069a7d41cedff786e210c34f8841b10c95d9fcabeb45c5c7d2d10` (`uv.lock`). `uv sync --all-packages --locked` succeeded. `chatcore_jobs` import succeeded without requiring Tavily or a provider.
- Source manifest SHA-256 (31 changed/untracked files, excluding this plan while recording it): `47948e9f743f3b73df2aa4041af477eb708df3dbb0cd33b47f6a3317375e7ec1`.
- Final non-integration command: `uv run pytest -m 'not integration' -q` — 585 passed, 1 skipped, 28 deselected. Four pre-existing python-telegram-bot retry-after deprecation warnings appeared.
- Final integration command: `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test uv run pytest -m integration -q` — 25 passed, 3 skipped, 586 deselected. The 3 skips are the existing Elasticsearch service-dependent tests; the disposable Postgres job integration cases ran.
- Focused Postgres command: `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test uv run pytest packages/plugin-jobs/tests/test_pg_jobs.py -m integration -q` — 15 passed.
- `uv run ruff check .` — all checks passed. Changed-file format check — 23 files already formatted. A repository-wide `uv run ruff format --check .` also reports three pre-existing, untouched plan Markdown files (`plans/phase2-memory.md`, `plans/phase2-privacy.md`, `plans/track-c.md`) as unformatted.
- At that stage no remote or live bot checks, commits, pushes, PRs, deploys, or immutable archive were created. The remaining coverage gaps recorded then are addressed by the review-repair evidence below.

## Review repair execution evidence (after merge `009e505`)

- Source checkout remains `/path/to/worktrees/basic-tools/sb_bot`, branch `codex/basic-tools`; repairs are uncommitted on top of merge commit `009e505`.
- Main merge baseline: `uv run pytest -m 'not integration' -q` — 644 passed, 1 skipped, 46 deselected, with four existing PTB deprecation warnings. First disposable-Postgres integration attempt found one stale migration in the shared test database; only the dedicated local `sbot-basic-tools-pg` `chatcore_test` database was reset. The clean rerun `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test uv run pytest -m integration -q -rs` — 43 passed, 3 Elasticsearch-dependent skips, 645 deselected.
- Final nonintegration run: `uv run pytest -m 'not integration' -q` — 652 passed, 1 skipped, 49 deselected, four existing PTB deprecation warnings (12.27 seconds).
- Final integration run: `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test uv run pytest -m integration -q -rs` — 46 passed, 3 skipped, 653 deselected (7.34 seconds). The three skips are Elasticsearch-dependent: `packages/plugin-memory/tests/test_integration.py:70`, `packages/plugin-search/tests/test_es_integration.py:19`, and `tests/test_app_integration.py:120`. Postgres integration tests ran in isolated schemas.
- `uv run ruff check .` — passed. `uv run ruff format --check packages/core/src/chatcore/group_access.py packages/adapter-telegram/src/chatcore_telegram/__init__.py packages/adapter-telegram/tests/test_telegram.py apps/sb_bot/src/sb_bot/app.py apps/sb_bot/tests/test_app.py packages/plugin-jobs/src packages/plugin-jobs/tests` — 23 files already formatted.
- Added `docs/reusable-jobs-review.md` with CONFIRMED/FIXED decisions, file/line evidence, test evidence for F1–F7 and R1–R6, additional review results, exact checks, and limitations. J02 and user-facing setup/DM/disable wording are updated. Task 4 broad-manager scope tests and Task 6 fresh-worker/new-pool restart tests are now checked above.
- Parent's independent review remains pending. The archive checkbox above remains open because this repair pass was instructed to leave fixes uncommitted and parent owns any later immutable handoff.

## R7 review repair evidence

- Confirmed the exact-expiry duplicate-claim and blocked-watch pre-send cancellation repros first: all three new PostgreSQL tests failed before the R7 code changes. After fixing, `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test uv run pytest packages/plugin-jobs/tests/test_pg_jobs.py -q -k 'exact_lease_expiry or unresolved_reservation or cancel_during_blocked' -rs` passed 3 tests. The cases verify inclusive reservation expiry, global-slot accounting for unresolved reservations beyond the claim lease, and a fresh worker producing no repeat fetch/send after cancellation during the third authorization check.
- Focused package commands: `uv run pytest packages/plugin-jobs/tests -m 'not integration' -q` — 68 passed, 29 deselected; `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test uv run pytest packages/plugin-jobs/tests/test_pg_jobs.py -q -rs` — 29 passed.
- Final `uv run pytest -m 'not integration' -q` — 653 passed, 1 skipped, 52 deselected, four existing PTB deprecation warnings (9.59 seconds).
- Final `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test uv run pytest -m integration -q -rs` — 49 passed, 3 skipped, 654 deselected (4.53 seconds). The three skipped cases remain Elasticsearch-dependent; local Postgres cases all ran.
- `uv run ruff check .` passed; changed-file format check passed with 23 files already formatted. Repairs including R7 remain uncommitted on top of merge `009e505`; no live service or bot message was used.


## Final orchestrator verification and handoff — 2026-10-31

Astra independently re-reviewed the repaired implementation: **PASS**, 192 focused tests passed,
including local Postgres; R1–R7 and F1–F7 are addressed, with no remaining substantive findings.
The parent inspected the final diff and reran the required checks against unchanged code:

- `uv run pytest -m 'not integration' -q`: 653 passed, 1 skipped, 52 deselected, four existing PTB warnings (8.70s).
- `TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:32779/chatcore_test uv run pytest -m integration -q -rs`: 49 passed, 3 skipped, 654 deselected (4.21s). The same three Elasticsearch tests were skipped; they provide no integration evidence.
- `uv run ruff check .`: passed. The 23-file format command recorded above passed; `git diff --check` passed.
- `uv sync --all-packages --locked` and importing `chatcore_jobs`: passed.

The requested checkpoint is `5e26499`; the merge of main is `009e505`. All later fixes stay
uncommitted. Final source snapshot, tree identity, per-file hashes, archive SHA-256 and operator
handoff are stored under `/path/to/local-artifacts/basic-tools/2026-10-31-final/`.
The snapshot uses a temporary Git index; it does not stage or commit the working-tree fixes.
Final verdict: **ready for the user's review**. No deployment, push, PR, live job or live message.
External delivery retains the documented possible-loss/unknown-outcome tradeoff. Elasticsearch
and live Telegram behavior were not verified. Earlier evidence sections are historical snapshots.
