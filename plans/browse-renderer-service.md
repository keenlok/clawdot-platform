# Sandboxed renderer service with a guarded egress proxy

Read first: `plans/browse-links-and-gating.md` §3 (the HTTP contract this service implements; the
bot-side client already exists from run 1), `packages/core/src/chatcore/security/net_guard.py`
(every rule, in full: `_parse_url`, `_check_hostname`, `_check_ip` incl. embedded-IPv4 handling,
`_literal_ip`, `_resolve`, `NetGuardSettings.extra_blocked`), its existing tests under
`packages/core/tests/`, `apps/sb_bot/compose.yaml`, `apps/sb_bot/Dockerfile`,
`apps/sb_bot/tests/test_deploy_files.py`, `apps/sb_bot/deploy/README.md`.

## Why this design (state these in `docs/rendered-fetch.md`)
* **Playwright + Chromium** (async Python API): real JS engine, per-fetch `BrowserContext`
  isolation, request routing, official Docker image with all system libraries, actively maintained.
  Rejected: Splash/Selenium (stale/heavy, weaker isolation primitives), hosted browser APIs
  (would ship user URLs and page content to a third party), raw CDP (we would re-implement
  Playwright).
* **The egress proxy is the security boundary, not Playwright routing.** Chromium is launched with
  `--proxy-server=http://127.0.0.1:<port>` pointing at an in-process asyncio proxy that applies
  the net_guard rules to **every connection Chromium makes**: top-level navigation, redirects,
  sub-resources, XHR/fetch, iframes, workers, and WebSocket (`wss://` is a CONNECT like any other).
  Playwright `route` interception is a second, weaker layer (it cannot see everything); the
  container network is a third (§5). Any one failing must not expose private networks.
* **HTTPS only, CONNECT only, port 443 only.** No plain-HTTP proxying means no request smuggling
  or per-request Host-header re-validation to get wrong. The bot-side tool upgrades `http://` to
  `https://`; `ws://` and `http://` sub-resources simply fail (mixed-content blocked anyway).

## You own
- New package `packages/browser-renderer/` (`sb-browser-renderer`, module `sb_browser_renderer`):
  `pyproject.toml`, `src/`, `tests/`.
- `packages/browser-renderer/Dockerfile` (renderer image) and **`apps/sb_bot/compose.browser.yaml`**
  (overlay file, see §5). Do **not** edit `apps/sb_bot/compose.yaml`, `deploy.sh`, or the sb-bot
  `Dockerfile`; the sb-bot image must not contain Playwright or Chromium.
- `docs/rendered-fetch.md` (security model, limits, blocked-site behaviour, deployment steps,
  adversarial-test list).
- Root `pyproject.toml` `[tool.uv.sources]` entry and `uv.lock`.
- **One additive edit** to `packages/core/src/chatcore/security/net_guard.py`: expose public,
  behaviour-neutral wrappers so the proxy reuses the exact rules instead of importing underscored
  names: `parse_https_destination(authority_or_url) -> (host, port)` (raises `BlockedURL`; applies
  `_normalize_host`, the blocked-host rules, credential/port rules; for the renderer port must be
  443), `async resolve_public(host, port, extra_blocked=()) -> list[str]` (does
  `_check_hostname`, `_literal_ip`, `_resolve`, `_check_ip` on **every** address, raises
  `BlockedURL`/`FetchError`), and `get_extra_blocked() -> tuple[str, ...]` (the same merge
  `safe_get` does with `NET_GUARD_EXTRA_BLOCKED`). `safe_get` must call the same helpers or remain
  byte-for-byte unchanged in behaviour; existing net_guard tests must pass untouched, and you add
  new ones in `packages/core/tests/test_net_guard_public.py`. Touch nothing else in core.

## 1. Policy and resolver (`sb_browser_renderer/policy.py`)
`EgressPolicy(resolver=resolve_public, connector=asyncio.open_connection-like, overrides=None)`.
`async def authorize(authority: str) -> PinnedTarget(host, ip, port)`:
1. Parse the CONNECT authority with `parse_https_destination` (reject userinfo, empty host,
   non-443, bad IPv6 brackets, trailing-dot and IDNA tricks the same way `safe_get` does).
2. Resolve **once**; reject if the resolver returns nothing or **any** address fails `_check_ip`
   (mixed public+private answers are refused, as in `safe_get`).
3. Return the validated IP. The proxy connects to **that IP literal**, never to the hostname, so
   there is no second lookup to rebind. Try the next validated address only on connect failure.
4. `PolicyOverrides` (allowed extra ports, allowed loopback addresses, trust self-signed certs) is
   a constructor argument that only tests can reach: no environment variable, CLI flag or setting
   may populate it, and `EgressPolicy()` built from settings must have `overrides=None`. A test
   asserts that reading config from a hostile environment (`BROWSER_ALLOW_LOOPBACK=1`,
   `NET_GUARD_ALLOW_PRIVATE=1`, etc.) changes nothing.
Blocked ranges come from `net_guard` plus `NET_GUARD_EXTRA_BLOCKED` (compose passes the same value
as the bot: host LAN, Tailscale `100.64.0.0/10` already built in, `100.100.100.200`, metadata
`169.254.169.254`/`fd00:ec2::254`, Docker bridge gateways).

## 2. Egress proxy (`sb_browser_renderer/proxy.py`)
Asyncio TCP server bound to `127.0.0.1:<ephemeral>` inside the renderer container, one instance per
renderer process, with per-render accounting:
* Accepts only `CONNECT host:443 HTTP/1.1`. Everything else (absolute-form `GET http://…`,
  `POST`, malformed, oversized header block > 8 KiB, header read slower than 5 s) gets
  `403`/`400` and the connection closes. Reply `HTTP/1.1 200 Connection Established` only after
  authorization and a successful pinned connect; blocked targets get `403` with an empty body.
* After the tunnel is up, copy bytes both ways with backpressure. Enforce per-render ceilings
  (shared `RenderBudget`): total bytes in+out (default 12 MiB), max concurrent tunnels (24), max
  tunnels opened (150), idle timeout per tunnel (15 s), and the render wall-clock. Exceeding a
  ceiling closes tunnels and marks the render `too_large`.
* A `RenderBudget` records every refused authority (hostname only, for the tests and for the
  `blocked_url` decision, never logged with path/query; the proxy never sees them anyway).
* Never trust anything the browser says about the target: the authority in CONNECT is the only
  input.

## 3. Render engine (`sb_browser_renderer/engine.py`)
One long-lived Chromium per process; **a new `BrowserContext` per request** (no storage state, no
cookies kept, `accept_downloads=False`, `service_workers="block"`, `permissions=[]`, no geolocation,
`java_script_enabled=True`, locale `en-US`, viewport 1280x800, `ignore_https_errors=False`), closed in
`finally`. Recycle the browser after N renders (default 50) or on any crash.
Chromium args (document why each exists in code comments): `--proxy-server=…`,
`--proxy-bypass-list=<-loopback>` (so loopback also goes through the proxy and is refused),
`--host-resolver-rules="MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"` (Chromium cannot resolve names
itself), `--disable-quic`, `--disable-features=DnsOverHttps,NetworkService…` only if verified valid,
`--force-webrtc-ip-handling-policy=disable_non_proxied_udp`, `--disable-background-networking`,
`--disable-sync`, `--disable-extensions`, `--no-first-run`, `--mute-audio`,
`--js-flags=--max-old-space-size=256`. Sandbox: Chromium's own sandbox stays **on** by default; a
setting `BROWSER_NO_SANDBOX` (default false) exists only because Docker without a seccomp profile
cannot run it, and the docs/compose must say the compensating controls (non-root, `cap_drop ALL`,
read-only fs, no secrets, isolated network, pids/mem limits).
Playwright `context.route("**/*")` as the second layer: abort any scheme other than `https`,
`data`, `blob`, `about`; abort resource types `media`, `font`, `image`, `websocket` and
`eventsource` (a text read needs none of them; the proxy still guards anything that slips
through); never fulfil or continue with modified headers.
The renderer **never clicks, types, submits forms, uploads, presses keys, or dismisses dialogs by
accepting**: dialogs (`alert/confirm/prompt/beforeunload`) are dismissed; popups
(`page.on("popup")`) are closed immediately; there are no `click`, `fill`, `type`, `press`,
`dispatch_event`, `check`, `select_option`, `set_input_files` calls anywhere in the package (add a
**static test that greps the package source** for them and fails the build). Navigation is a single
`page.goto(url, wait_until="domcontentloaded")` followed by a bounded wait for network idle
(`wait_for_load_state("networkidle", timeout=…)` swallowing its timeout) and a short settle
(default 1 s), then `page.title()` and `page.inner_text("body")`.
Output limits: text capped at `RENDER_MAX_CHARS` (30 000), whitespace-collapsed, control chars
removed; `truncated` set when cut. Wall clock per render (default 30 s) enforced with
`asyncio.timeout` plus a hard kill of the context; memory cap is the container's; at most
`RENDER_CONCURRENCY` (default 2) renders at once, extra requests get status `busy` immediately
(no unbounded queue).
Honest identity: leave the default Chromium user agent; **no stealth plugins, no fingerprint
patches, no proxy rotation, no waiting out or solving challenges, no retries**.

## 4. Blocked-site detection and the HTTP service
`detect_block(status, title, text, frame_urls) -> bool` in `blocking.py`, deliberately simple and
conservative, data-driven lists: main-document HTTP status 401/403/429/451/503 with short body;
title/text markers (case-insensitive): "verify you are human", "are you a robot", "captcha",
"just a moment", "attention required", "access denied", "request unsuccessful", "pardon our
interruption", "unusual traffic", "checking your browser", "enable javascript and cookies to
continue"; iframe/script origins containing `recaptcha`, `hcaptcha`, `challenges.cloudflare.com`,
`awswaf`, `datadome`, `perimeterx`, `px-cdn`, `_incapsula_`, `akamaihd` challenge paths only if
verified. If it fires, return `status: "blocked_by_site"` with empty text and stop: **no retry, no
second attempt with different settings**. A page with real content that merely *mentions* "captcha"
in a long article must not trigger it: require marker plus short body (< 1500 chars of text) or a
challenge frame; test both.
Service (`server.py`, `aiohttp`): `POST /render` (JSON `{"url": str}`, body ≤ 4 KiB), `GET /healthz`.
Listen on `0.0.0.0:8080` **inside the container only** (the compose network is internal). Validate
the URL with `parse_https_destination` (scheme must be `https` here; `blocked_url` otherwise) before
any browser work. Respond with the exact contract JSON from `plans/browse-links-and-gating.md` §3:
statuses `ok|blocked_by_site|blocked_url|timeout|too_large|busy|error`; never include exception
text, stack traces or internal addresses in the body; if the proxy refused the top-level host,
status `blocked_url`. Logging: hostname, status, duration, bytes only. The process holds **no
secrets**: no tokens, no API keys, no database settings (a test asserts the settings class has
no `SecretStr` and the service starts with an empty environment except `NET_GUARD_EXTRA_BLOCKED`
and its own `RENDER_*`/`BROWSER_NO_SANDBOX` knobs).

## 5. Deployment files (write, do not deploy)
`packages/browser-renderer/Dockerfile`: base `mcr.microsoft.com/playwright/python:<pinned version>-noble` pinned
by digest placeholder `@sha256:REPLACE_BEFORE_DEPLOY` with a comment (we cannot resolve digests in
the sandbox); non-root user; `uv sync --frozen --no-dev --package sb-browser-renderer`;
`CMD ["python","-m","sb_browser_renderer"]`.
`apps/sb_bot/compose.browser.yaml` (overlay: `docker compose -f compose.yaml -f compose.browser.yaml`):
* service `sb-bot-browser`: `read_only: true`, `tmpfs` for `/tmp` and the Chromium profile
  (`size=256m`), `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]` plus a seccomp profile
  note for Chromium's sandbox, `mem_limit: 1536m`, `memswap_limit` equal, `cpus: 1.5`,
  `pids_limit: 256`, `user` non-root, **no `env_file`**, no volumes, no secrets, healthcheck on
  `/healthz`, log rotation like the other services, `restart: unless-stopped`, environment limited
  to `NET_GUARD_EXTRA_BLOCKED` and `RENDER_*`.
* networks: `sb-bot-render` (internal: true; shared only with `sb-bot`; the bot reaches
  `http://sb-bot-browser:8080`) and `sb-bot-browser-egress` (a dedicated non-internal bridge used
  only by this service so it has internet but **cannot reach `app-backend`, valkey or
  Postgres**). Document in comments and in the docs the host-level defense-in-depth the operator
  must apply (a `DOCKER-USER` iptables rule dropping traffic from that bridge's subnet to RFC1918,
  CGNAT, link-local and the Tailscale interface) because Compose cannot express it, and state that
  the proxy remains the control that does not depend on it.
* `sb-bot` gets the overlay env `BROWSER_RENDER_URL=http://sb-bot-browser:8080` and joins
  `sb-bot-render`. Everything is **off unless the overlay is used**; with only `compose.yaml` the
  rendered tool does not exist.
Extend `apps/sb_bot/tests/test_deploy_files.py` (or add a sibling test) to assert, parsing both
YAMLs: the overlay service has no `env_file`, no volumes, `read_only`, `cap_drop ALL`,
`no-new-privileges`, memory/pids limits, is not attached to `app-backend` or
`app-internal`, and the base compose is unchanged.

## Tests (`packages/browser-renderer/tests/`, plus the core additions)
Unit tests use a **fake resolver and a fake connector** (record which IP/port was dialled) and a
fake browser driver; no Chromium, no network.
Adversarial egress matrix, parametrised, each asserting `BlockedURL`/`403` **and that the
connector was never called**: `127.0.0.1`, `127.1`, `2130706433`, `0x7f.0.0.1`, `0177.0.0.1`,
`0`, `[::1]`, `[::]`, `[::ffff:127.0.0.1]`, `[::ffff:7f00:1]`, `[64:ff9b::7f00:1]`,
`[2002:7f00:1::]`, a Teredo address embedding a private IPv4, `169.254.169.254`,
`[fd00:ec2::254]`, `[fe80::1%25eth0]`, `100.64.0.1`, `100.100.100.200` (Tailscale/Alibaba metadata),
`10.0.0.1`, `172.16.5.4`, `192.168.1.1`, `0.0.0.0`, `224.0.0.1`, `localhost`, `localhost.`,
`foo.localhost`, `postgresql`, `elasticsearch`, `sb-bot-valkey`, single-label hosts, `a@127.0.0.1`
and `evil.com@127.0.0.1` authorities, ports 80/8080/22/5432/6379/65535/0/`443abc`, empty and
oversized authorities, unicode/IDNA lookalikes of `localhost`, trailing dots, uppercase,
`NET_GUARD_EXTRA_BLOCKED` entries (CIDR and exact IP and hostname). Plus: public IPs and ordinary
hostnames are allowed; resolver returning a mix `[public, private]` is refused; resolver returning
only a CNAME-style private address is refused; empty answer refused.
DNS rebinding: a resolver that answers public on call 1 and `127.0.0.1` on call 2 — assert the
connector dialled the **first, validated** IP, the resolver was called exactly once per CONNECT,
and a second CONNECT to the same name re-validates the new (private) answer and is refused.
Proxy protocol: non-CONNECT methods, absolute-form requests, header flood, slow header, half-open
connection, pipelined bytes after CONNECT before the 200, many tunnels over budget, byte-budget
overrun, idle timeout, upstream connect failure maps to 502 and does not leak the resolved IP.
Engine (fake driver): new context per render and always closed (also on timeout and exception);
no cookie/storage reuse between two renders; dialogs dismissed and popups closed; the static grep
test; output truncation and control-char stripping; wall-clock timeout returns `timeout`;
concurrency cap returns `busy` immediately; browser recycled after N renders and after a crash.
Blocking: each marker and challenge frame returns `blocked_by_site` with empty text and **exactly
one** navigation attempt; long article mentioning "captcha" is not blocked; plain 404 is `error`,
not `blocked_by_site`. **A test named `test_blocked_site_is_reported_plainly_and_not_retried`**
mirroring the bot-side one.
Service: schema validation, oversized body, wrong content type, non-https URL → `blocked_url`
before any browser work, no exception text in responses, settings have no secrets, hostile env
cannot enable overrides.
Core: `test_net_guard_public.py` proves the new wrappers match `safe_get` decisions on the same
vector list and that existing behaviour is unchanged.
**Integration tests** (`@pytest.mark.integration`, extra marker `browser`, skip when Playwright's
Chromium is not installed; run with a local HTTPS server on an ephemeral port using `trustme` as a
dev dependency; the test passes `PolicyOverrides(allowed_ports={port}, allowed_loopback={"127.0.0.1"},
ignore_cert_errors=True)` programmatically). They must prove with the **real browser**: a JS page
that builds its text after load is returned rendered; the same server is **refused under the default
policy**; an allowed page that 302-redirects to a blocked address, loads an `<img>`, `fetch()`,
`XMLHttpRequest`, `<iframe>`, `new WebSocket('wss://…')`, `navigator.sendBeacon`, and a
`<link rel=prefetch>` to a blocked address each result in **zero** connections reaching a canary
listener on a second loopback address, and the refusals show in the proxy's refused-authority
record; a page with a form and a button is fetched and the server **never receives a form
submission or click**; a `Set-Cookie` from render 1 is not sent on render 2; a download link is not
followed; an infinite-loop page hits the wall clock; a page streaming > budget bytes is cut with
`too_large`; the process exits Chromium children on shutdown. No test touches the internet.

## Done when
- `uv run pytest -m 'not integration' -q` and `uv run ruff check .` pass from the repo root.
- `uv run pytest packages/browser-renderer -q` passes; browser integration tests skip cleanly if
  Chromium is missing (try `uv run playwright install chromium` with the repo-local cache
  `PLAYWRIGHT_BROWSERS_PATH="$PWD/.pw-browsers"` and tell the reviewer whether it worked; add
  `.pw-browsers/` to `.gitignore`).
- `docs/rendered-fetch.md` documents: threat model, why proxy-first, the adversarial vector list,
  limits table, blocked-site behaviour ("this site blocked automated access"; never evaded), how
  to enable per group, deployment steps for the overlay, and the operator's iptables note.
- Contract addition from run 1's review: the service must ALWAYS answer HTTP 200 with the JSON
  contract body for every outcome it controls (busy, timeout, blocked_by_site, too_large, ...). The
  bot client maps raw HTTP 403/429 to "blocked by site", so the service must never emit those
  itself; malformed requests get 400. Also never pass through hostile page text into logs.
- Run-1 package `packages/plugin-browse` is committed (e8ca480): do not edit it.
- Not in this run: deploying, pushing, live-site fetches, website-watch rendered mode.
