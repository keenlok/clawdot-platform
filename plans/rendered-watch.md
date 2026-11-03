# Rendered mode for website watches

Read first: `ARCHITECTURE.md` §3, §6a; `docs/rendered-fetch.md` (renderer, blocked-site behaviour);
`plans/browse-links-and-gating.md` §2-3 (browse gating, `RenderClient`, status table);
`packages/plugin-jobs/src/chatcore_jobs/{plugin,service,worker,watch,repository,settings}.py`,
`migrations/0001_jobs.sql`, and the jobs tests; `packages/plugin-browse/src/chatcore_browse/
{client,repository,service}.py`; `apps/sb_bot/src/sb_bot/app.py`.

Builds on the committed branch (browse plugin + renderer service already exist; do not change
them beyond what is listed). This is a separate, reviewable step: JS-heavy pages can be watched,
with a stricter minimum interval and a global cost cap.

## You own
- `packages/plugin-jobs/**` (src, tests, **new** migration `0002_rendered_watches.sql`).
- `apps/sb_bot/src/sb_bot/rendered_watch.py` (new adapter), a few lines in
  `apps/sb_bot/src/sb_bot/app.py` (keep the diff tiny: other branches edit this file),
  `apps/sb_bot/tests/` for the adapter and wiring, `.env.example`, `docs/rendered-fetch.md`
  (append a "Rendered watches" section).
- `packages/plugin-browse/**` only to add one small public helper if needed (see §3); no behaviour
  change.

You may **not** edit `packages/core/**`, `packages/browser-renderer/**`, `packages/plugin-web/**`.
**`chatcore_jobs` must not import `chatcore_browse`** and vice versa: the app wires them together
through the injected callables below. If a contract must change, stop and say so.

## 1. Data model
Watches stay `kind='watch'`. Payload `data` gains `"mode": "plain" | "rendered"`; a missing key
means `plain` (existing rows and tests unchanged). Repository payload validation accepts the new
key and rejects any other value. Migration `0002_rendered_watches.sql`: a partial index on
`scheduled_jobs ((payload->'data'->>'mode')) WHERE kind='watch' AND status='active'` (verify the
JSON path against how payload is actually stored; adapt) and nothing else. No new columns.

## 2. Settings (`JobsSettings`, env prefix `JOBS_`, same strict-int style)
- `minimum_rendered_watch_interval_seconds` default 3600 (range 60..604800); the effective floor for a
  rendered watch is `max(group minimum_interval_seconds, this)`, enforced at creation, at commit,
  and again in `_authorize_job` (like the existing plain-watch floor).
- `max_rendered_watches_global` default 20 (0..1000; 0 disables rendered watches entirely).
- `rendered_concurrency` default 1 (1..4): an `asyncio.Semaphore` in the worker around the rendered
  fetch only, so scheduled renders can never occupy more than this many renderer slots (the
  renderer itself allows 2 concurrent renders; interactive use keeps the rest).
- `rendered_fetch_timeout_seconds` default 35 within `execution_deadline_seconds` bounds (adjust
  validators: execution deadline max is currently 35; do not raise lease limits without keeping
  `lease_seconds > execution + send`).

## 3. Injected capabilities (jobs side defines the protocol; app supplies it)
In `chatcore_jobs/watch.py` add exceptions `RenderedBlocked(Exception)` (site blocked automated
access) and `RenderedBusy(Exception)`, and type aliases:
`RenderedFetcher = Callable[[str, JobsSettings], Awaitable[Snapshot]]` and
`RenderedAllowed = Callable[[Scope], Awaitable[bool]]`.
`JobsPlugin.__init__` and `JobsWorker.__init__` gain optional `rendered_fetcher` and
`rendered_allowed` (both default `None`; `None` means rendered watches are unavailable and every
rendered path fails closed with a short message). Existing signatures stay backward compatible.

`apps/sb_bot/src/sb_bot/rendered_watch.py`:
- `make_rendered_fetcher(client: RenderClient, browse_settings) -> RenderedFetcher`: calls the
  client, maps statuses: `ok` → `Snapshot` using jobs' `normalize_text` (title is not part of the
  digest, text only) ; `blocked_by_site` → `RenderedBlocked`; `busy` → `RenderedBusy`;
  `blocked_url` → `chatcore.security.net_guard.BlockedURL`; `timeout`/`too_large`/`error` →
  `FetchError`. No renderer-supplied text is propagated into exception messages.
- `make_rendered_allowed(db_factory, browse_settings) -> RenderedAllowed`: reads
  `BrowseRepo.settings(scope).rendered_enabled`. Fails closed (False) on any exception.
- Wire both only when `BROWSER_RENDER_URL` is set and the jobs plugin exists; otherwise pass `None`.

## 4. Creation (tool and command)
- `create_website_watch` accepts optional `rendered: boolean` (default false). Change the exact-keys
  check to `{"url","interval_seconds"}` plus optional `"rendered"`; `rendered` must be a real bool.
  New slash command `/watchrendered <seconds> <public URL>` (leave `/watch` untouched).
- `JobsService.request_create` for `rendered=true`, in order, failing closed with short messages:
  rendered capability present → `rendered_allowed(scope)` true → `max_rendered_watches_global > 0`
  → effective floor (§2) → URL provenance exactly like plain watches (`url_provenance=True` for the
  tool; the command path keeps its existing behaviour) → the global rendered cap using a count of
  active rendered watches (also re-checked inside the commit transaction under the same lock the
  existing global cap uses, so two confirmations cannot both pass).
- The https-only rule: a rendered watch URL must be `https` (or `http` upgraded to `https` the same
  way `fetch_rendered_page` does; reuse the logic, do not duplicate it divergently: put the shared
  helper in `chatcore_browse.urls` if it is not already public and import it from the **app**
  adapter, not from jobs; jobs keeps its own tiny validator that rejects non-http(s) and upgrades
  `http`→`https` only for rendered mode).
- The confirmation summary must say: "rendered (headless browser) watch", the URL, the interval,
  that the first check is silent, that only readable-text changes are reported, that sites that
  block automated access will stop the watch with a notice, and the retention window.

## 5. Worker behaviour (`_run_watch`)
- Choose the fetcher by `mode`; a rendered job with no rendered fetcher configured is suppressed
  (finished as cancelled/`suppressed`, no network call).
- Rendered fetch runs inside the semaphore and under `rendered_fetch_timeout_seconds`.
- `RenderedBusy`: reschedule at `now + min(300, interval)`, **do not** increment `failure_count`, no
  notification. Cap consecutive busy reschedules at 24 by counting in `payload`-free fashion
  (reuse `failure_count`-like state only if it needs no migration; otherwise accept unbounded busy
  retries since each costs nothing and note it in the report).
- `RenderedBlocked`: send **one** terminal notification and finish the job as failed: kind
  `watch_site_blocked`, content exactly "This site blocks automated access, so the website watch
  was stopped. You can open the page yourself." (<= 1500 chars, no URL or page text). Never retry,
  never fall back to a plain fetch of the same URL (assert in a test that the plain fetcher is not
  called).
- Other errors follow the existing failure/backoff/notify-on-third-failure path with
  `safe_error_message` extended with a rendered-specific generic line.
- `_authorize_job` (in `plugin.py`) additionally, for rendered watches, requires
  `rendered_allowed(scope)` true and the rendered interval floor; if the group disables rendered
  fetch, existing rendered watches are suppressed and cancelled at their next run (no
  cross-plugin write needed; say so in the docs and listing text). `list_website_watches` output
  marks rendered watches.
- Change notifications for rendered watches use the same `change_notification` quoting/escaping;
  untrusted page text never reaches the model history (the existing `send_job` wrapper already
  does this; keep it so).

## Logging
IDs, counts, latency, error class names, URL hostnames only. Never page text or full URLs.

## Tests
Fakes only (fake `RenderedFetcher`, fake `RenderedAllowed`, fake clock); no network, no Chromium.
Unit (jobs): payload `mode` validation and backward compatibility with old rows; settings
validators; creation fail-closed ordering with recording fakes (capability missing, group not
enabled, cap zero, below floor, unseen URL, global cap reached: the fetcher is never called);
global cap race: two creations confirmed concurrently, only one commit succeeds (integration);
worker: plain watches unchanged; rendered watch changed/unchanged/first-silent paths;
`RenderedBusy` reschedules without failure count and without notification; `RenderedBlocked` sends
exactly one notification with the exact text, finishes `failed`, and **never** calls the plain
fetcher (the test named `test_blocked_site_stops_rendered_watch_without_retry`); semaphore bounds
concurrent rendered fetches to `rendered_concurrency` (two claims, assert max in-flight 1); group
disabling rendered fetch suppresses the next run; rendered fetch timeout; notification text never
contains the URL or page text beyond the quoted diff.
App: adapter status mapping (each renderer status), `rendered_allowed` fails closed on DB error;
wiring present only when `BROWSER_RENDER_URL` is set; plain watches work without it.
Integration (`@pytest.mark.integration`, skip without `TEST_PG_DSN`): migration applies on top of
0001 with existing rows; the partial index exists; count query counts only active rendered watches;
cap enforced inside the commit transaction.

## Done when
- `uv run pytest -m 'not integration' -q` and `uv run ruff check .` pass; `uv run ruff format --check`
  clean for changed files; jobs integration tests pass against a disposable Postgres.
- Every existing jobs test passes unchanged except where a signature gained an optional argument.
- Docs section explains cost controls (floor, global cap, concurrency), blocked-site behaviour, and
  that disabling rendered fetch stops existing rendered watches at their next run.
- Not in this run: deploying, live-site fetches, any change to the renderer.
