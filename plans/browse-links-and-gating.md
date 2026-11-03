# Browse plugin: deep links, gated `fetch_rendered_page`, renderer contract

Read first: `ARCHITECTURE.md` §3, §3d, §6a; `packages/plugin-web/src/chatcore_web/__init__.py` (URL
provenance, caching, untrusted results); `packages/plugin-jobs/src/chatcore_jobs/{plugin,service,
repository,permissions,settings}.py` and its `migrations/0001_jobs.sql` (per-group gating through
an admin-confirmed `PendingAction`, revision checks, `can_use_scope`, `is_group_manager`);
`packages/core/src/chatcore/tools/registry.py`; `apps/sb_bot/src/sb_bot/app.py`.

This is run 1 of 2 for the "rendered fetch + deep links" feature. Run 2
(`plans/browse-renderer-service.md`) builds the sandboxed renderer service on the other side of the
HTTP contract in §3. **Do not build the renderer here**; use a fake transport.

## Why
A user asked for rental cars at Fukuoka airport. `fetch_web_page` only gets plain HTML, so
JS-rendered pages are empty and prices need a search form the bot cannot drive. We add two
read-only things only:
1. A **deep-link builder**: structured input becomes a prefilled search URL *for the user to
   open themselves*. No network call.
2. **`fetch_rendered_page`**: read a URL after JavaScript ran, via a separate sandboxed renderer.
   Off by default; enabled per group by an admin.

Out of scope, never build: filling forms, clicking, booking, evading bot detection or CAPTCHAs.

## You own
- New package `packages/plugin-browse/` (`chatcore-browse`, module `chatcore_browse`) with its
  `pyproject.toml`, `src/`, `tests/`, `migrations/`.
- Wiring: root `pyproject.toml` (`[tool.uv.sources]` entry only), `apps/sb_bot/pyproject.toml`
  (add dependency), `apps/sb_bot/src/sb_bot/app.py` (register the plugin; keep the diff to a few
  lines because another branch edits this file too), `.env.example` / docs for new env names,
  `apps/sb_bot/tests/` only if an existing app test must learn the new plugin.
- `uv.lock` (regenerate with `uv lock`).

You may **not** edit: `packages/core/**`, `packages/plugin-web/**`, `packages/plugin-jobs/**`,
anything under `packages/browser-renderer/` (run 2). Import from `chatcore_jobs.permissions`
(read-only use: `make_scope`, `make_actor`, `can_use_scope`, `is_group_manager`) rather than copying
it. If a contract must change, stop and say so in the report.

## 1. Deep links (`chatcore_browse/deeplinks.py`, `chatcore_browse/templates.py`)
Tool `build_search_link` (read-only, `writes=False`, not gated, always registered):

```
site:        enum of registered site ids ("rentalcars", "toyota_rent_a_car")
kind:        enum, only "rental_car" for now
location:    3-letter airport IATA code (rental_car), e.g. "FUK"
pickup_date, return_date:  "YYYY-MM-DD"
pickup_time, return_time:  "HH:MM" (default "10:00"); minutes must be 00 or 30
car_class:   optional enum economy|compact|midsize|suv|van   (informational, see below)
driver_age:  optional int 21..80 (default 30; rentalcars shows "30-65" pricing band by default)
```
`additionalProperties: false`. All validation is strict and fails with a short user-safe error:
IATA `^[A-Za-z]{3}$` upper-cased; real calendar dates; pickup must not be in the past (injectable
`clock`, tests never use wall time) and at most 365 days ahead; return strictly after pickup;
rental at most 90 days; no control characters anywhere; never echo raw model-supplied strings back
unescaped. Build query strings only with `urllib.parse.urlencode` from already-validated typed
values, never string concatenation of free text.

Templates are **data, not code**: a frozen dataclass `LinkTemplate` (site id, label, supported
kinds, scheme/host/path, `prefill: bool`, field→query-param map with a formatter per field,
`verified_on: date`, `source_note: str`) in a registry dict. Adding a site means adding data plus a
test. A registry test must assert every template has `verified_on` and `source_note`, uses `https`,
and has a host in a fixed allowlist.

Verified on the live sites on **2026-10-31** (do not re-verify from tests; tests never touch the
network):

* **rentalcars.com** (prefillable). Base `https://www.rentalcars.com/search-results`, GET params:
  `locationIata`=IATA, `puDay`,`puMonth`,`puYear`,`puHour`,`puMinute` (integers, no zero
  padding required, month 1-12), `doDay`,`doMonth`,`doYear`,`doHour`,`doMinute`, `driversAge`=int,
  `ftsType`=`A` (airport search). Loading
  `…/search-results?locationIata=FUK&puDay=19&puMonth=11&puYear=2026&puMinute=0&puHour=10&doDay=28&doMonth=11&doYear=2026&doMinute=0&doHour=10&driversAge=30&ftsType=A`
  showed the form prefilled with FUK, 19 Nov 10:00 to 28 Nov 10:00, age 30-65. **No car-class
  parameter was verified**: the site filters class (Small/Medium/Large/SUV/People carriers) on the
  results page, so do **not** invent one. `car_class` therefore only adds a line to the reply such as
  "Car class is not prefilled; use the 'Small car' filter on the results page." (map the enum to the
  site's own filter labels: economy/compact→"Small car", midsize→"Medium car", suv→"SUVs",
  van→"People carriers").
* **Toyota Rent a Car English** (`https://rent.toyota.co.jp/eng/reservation/index01.aspx`) is an
  ASP.NET WebForms page submitted by POST with ViewState; shop and date selection use hidden shop
  codes and no query-string prefill exists. Template has `prefill=False`: the link goes to the
  reservation page only, and the reply lists exactly what to enter (pickup shop name as typed by
  the user's `location`, dates, times). Do **not** fabricate parameters for it.

Reply format (plain text, `untrusted=False`, it is our own output; escape nothing that is not
user text): the URL on its own line, then a short checklist of the inputs used, then the fixed
disclaimer **"This is only a search link. It does not confirm availability or price; the site
shows live results."** For `prefill=False` templates the disclaimer instead says the page could not be
prefilled and shows the values to type. Never use words like "available", "booked" or "confirmed"
about the link. After building, add the generated URL to `ctx.turn.seen_urls` (normalised the way
`chatcore_web._normalize_url` does; copy those few lines into `chatcore_browse/urls.py` with its own
tests) so `fetch_rendered_page` may read it later in the same turn.

## 2. Per-group gating (`chatcore_browse/settings.py`, `repository.py`, `service.py`, `plugin.py`)
Mirror how `plugin-jobs` gates its tools, in miniature:

* Migration `migrations/0001_browse.sql` (package `chatcore_browse.migrations`, plugin
  `migrations_package`): table `browse_settings(chat_key text, is_dm boolean, rendered_enabled
  boolean not null default false, revision integer not null default 0, updated_by text,
  updated_at timestamptz, primary key (chat_key, is_dm))`.
* `BrowseSettings` (env prefix `BROWSE_`, pydantic-settings, strict ints like `JobsSettings`):
  `render_url: str | None` (env `BROWSER_RENDER_URL`; **unset means the tool is not registered at
  all**), `dm_enabled: bool=False`, `request_timeout_s=45`, `max_chars=30000`,
  `max_renders_per_turn=2`, `cache_ttl_s=900`, `max_response_bytes=300_000`.
* Tool `configure_rendered_fetch(enabled: bool)`, `writes=True`, `admin_only=False`: checks
  `is_group_manager`, returns a `PendingAction` (plugin `"browse"`, kind `"configure"`, payload with
  scope, actor, enabled, revision) whose summary says exactly what changes and that rendered fetch
  loads pages in a sandboxed headless browser. Register an action handler in `action_handlers()`.
  Commit re-checks manager status and revision (stale revision means "settings changed, nothing
  done"). Also slash command `/browsetools <on|off>`. DMs are disabled unless `dm_enabled`.
  Enabling/disabling is the only thing persisted. Also implement `purge_chat` / `purge_user`
  hooks like `JobsPlugin` (purge deletes the chat row).
* Tool `fetch_rendered_page(url)` is registered only when `render_url` is set. At call time it
  must, in order, fail closed with a short message: scope not allowed (`can_use_scope`) → rendered
  fetch disabled for this chat (`rendered_enabled` false; say an admin can enable it) → URL invalid
  or has credentials → URL not in `ctx.turn.seen_urls` (same normalised comparison and same error
  text as `fetch_web_page`) → per-turn limit (`ctx.turn.fetches` shared with web fetches **and**
  its own `max_renders_per_turn` counter; add the counter to a module-level `WeakKeyDictionary`
  keyed by `ctx.turn` if `TurnState` has no field, since you may not edit core) → cache hit
  (`ctx.stores.cache`, key `browse:page:<sha256 of normalised url>`) → render call.
  A top-level `http://` URL is upgraded to `https://` before the call (the renderer is HTTPS-only).
* Result is always `ToolResult(content=..., untrusted=True)`. Content format:
  `Title: …\nURL: <final_url>\nRendered by a headless browser; text only.\n\n<text>` truncated to
  `max_chars` with a `[Truncated]` suffix. Never cache or return blocked/error results as pages.

## 3. Renderer HTTP contract and client (`chatcore_browse/client.py`)
`RenderClient(base_url, timeout_s, transport=None)` using `httpx.AsyncClient` (inject
`httpx.AsyncBaseTransport` for tests). One call: `POST {render_url}/render` with JSON
`{"url": "<https url>"}` → JSON object, **hard-capped at `max_response_bytes`** (stream and abort;
never `response.json()` an unbounded body). The client must not follow redirects, send cookies or
auth headers, and must ignore any extra fields. Response shape:

```
{"status": "ok" | "blocked_by_site" | "blocked_url" | "timeout" | "too_large" | "busy" | "error",
 "final_url": str, "title": str, "text": str, "truncated": bool}
```
Map statuses to ToolResult errors (`is_error=True`, short fixed strings, **never** pass through
renderer-supplied error text):

| status | message |
|---|---|
| `blocked_by_site` | "This site blocked automated access, so I can't read it. Please open the link yourself." (also used for HTTP 403/429 style walls; the tool must **not** retry, rephrase the URL or try alternative fetch paths) |
| `blocked_url` | "That address is not allowed." |
| `timeout` | "The page took too long to render." |
| `too_large` | "The page was too large to read." |
| `busy` | "The page reader is busy. Try again in a minute." |
| anything else, HTTP errors, malformed JSON, oversize body, transport errors | "The page could not be rendered." |

Validate the success payload: `final_url` must be `https` and pass a hostname sanity check (no
credentials, no IP literal in a private range; reuse `chatcore.security.net_guard` public helpers
only if they exist, otherwise do a local `ipaddress` check), `title`/`text` strings, control
characters stripped, lengths bounded. Anything off becomes the generic error.

## Logging
IDs, counts, latency, error class names, URL **hostnames** only. Never page text, full URLs,
exception messages or tool arguments.

## Tests (`packages/plugin-browse/tests/`, no network, fake `httpx` transport / fake store)
Deep links: golden URL for the FUK 19→28 Nov example exactly as in §1; every validation failure
(bad IATA incl. `FU`, `FUKK`, `F1K`, unicode; Feb 30; past date with fixed clock; return before or
equal pickup; 91-day rental; minute 15; control chars/newlines/`&`/`#`/`%` in every string field
prove no extra query params can be injected; unknown `site`/`kind`; extra args rejected); Toyota
template returns the page link plus checklist and never any query string; registry test (verified_on,
https, host allowlist, no template builds a URL from free text); disclaimer always present and the
forbidden words ("confirmed", "booked", "available") never describe the link; generated URL lands in
`seen_urls`.
Gating: tool absent when `render_url` unset; default-off per group; DM off; non-admin cannot
configure; stale revision rejected; chat A enabled does not enable chat B (isolation test); each
fail-closed step in §2 in order, with the render client **never called** on every refusal (assert
with a recording fake); provenance reuse (URL not seen → refused, seen with different case/trailing
dot/default port → accepted); per-turn limit; http→https upgrade; cache hit skips the client;
blocked/error results are not cached; result is `untrusted=True`.
Client: each status mapping above; oversize body aborted; malformed JSON; non-https `final_url`;
private-IP `final_url`; redirects not followed; no cookies/auth sent; renderer error text never
reaches the user; **a test named `test_blocked_site_is_reported_plainly_and_not_retried`** asserting
exactly one client call and the exact message.
Postgres-backed repository tests are `@pytest.mark.integration` and skip when `TEST_PG_DSN` is
unreachable (copy the jobs pattern).

## Done when
- `uv run pytest -m 'not integration' -q` and `uv run ruff check .` pass from the repo root;
  `uv run ruff format --check .` is clean for your files.
- `uv run pytest packages/plugin-browse -q` passes; integration tests skip cleanly without Postgres.
- The app still starts in its existing tests with `BROWSER_RENDER_URL` unset (no behaviour change:
  only `build_search_link` appears) and with it set (plus `configure_rendered_fetch` and
  `fetch_rendered_page` appear).
- Not in this run: the renderer service, Docker/compose files, watches. Say so in the report.
