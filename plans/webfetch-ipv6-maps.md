# Web fetch: IPv4-first address fallback, and a Google Maps / share link resolver

Read `ARCHITECTURE.md` §6a (security baseline) first. Then read these files, which define the contracts you build against:
- `packages/core/src/chatcore/security/net_guard.py` (SSRF guard: `_resolve`, `_check_ip`, `resolve_public`, `safe_get`)
- `packages/plugin-web/src/chatcore_web/__init__.py` (`fetch_web_page`, `seen_urls` provenance)
- `packages/browser-renderer/src/sb_browser_renderer/policy.py` (`EgressPolicy.open_tunnel`)
- `packages/core/tests/test_b_net_guard.py`, `packages/core/tests/test_net_guard_public.py`, `packages/plugin-web/tests/test_web.py` (test style: `monkeypatch` `net_guard._resolve` and `net_guard._make_transport` with `httpx.MockTransport`; `respx` for plain clients)

Environment: run `export UV_CACHE_DIR=$PWD/.uv-cache` in every shell before `uv`. Run `uv sync --all-packages` once. No live network in tests.

## Background (production incident, 2026-11-01)

Production runs in Docker with **no IPv6 route**. Dual-stack sites fail at random:
- `_resolve` builds `list({... getaddrinfo ...})`; the **set** randomises address order.
- `safe_get` pins **only `addresses[0]`**, which is often IPv6, so the connect fails with `httpx.ConnectError`.
- The catch-all `except httpx.HTTPError` turns that into `FetchError("The page request failed.")` and the cause is lost from logs (the plugin logs "web fetch blocked … error=FetchError").

Separately, `https://share.google/<id>` links redirect `302 → https://www.google.com/share.google?q=<id>` then `301 → https://www.google.com/search?q=Ah+Hua+Cafe&kgmid=/g/…&…`. The final page is a JS-heavy Google Search page; fetching it is pointless. The useful facts are in the redirect URL itself.

## You own
- `packages/core/src/chatcore/security/net_guard.py`
- `packages/core/tests/test_net_guard_fallback.py` (new) and additions to `packages/core/tests/test_b_net_guard.py` only if an existing test must be adapted to the new ordering
- `packages/plugin-web/src/chatcore_web/__init__.py`
- `packages/plugin-web/src/chatcore_web/maps.py` (new)
- `packages/plugin-web/tests/test_web.py`, `packages/plugin-web/tests/test_maps.py` (new)
- `packages/browser-renderer/src/sb_browser_renderer/policy.py` and its tests under `packages/browser-renderer/tests/`
- `packages/plugin-browse/src/chatcore_browse/service.py`, `packages/plugin-browse/src/chatcore_browse/client.py` (log-line changes only) and their tests
- `.env.example` (document the new env vars, if the file lists `NET_GUARD_EXTRA_BLOCKED`)

You may **not** edit `packages/core/src/chatcore/tools/registry.py`, `packages/core/src/chatcore/images.py`, the Telegram adapter, `packages/plugin-expenses`, `packages/plugin-search` or `packages/plugin-research` (another branch is changing them). Do not add tools to `WebPlugin`: the research agent asserts the web tool set is exactly `{"search_web", "fetch_web_page"}`. Ask in your report if any of these must change.

## Security rules (non-negotiable, §6a)
- Every resolved address passes `_check_ip` **before** any connection attempt — all of them, as today, including addresses that will never be tried. A blocked address anywhere in the list still raises `BlockedURL`.
- Keep pinning to the IP literal, `extensions={"sni_hostname": host}`, the `Host` header, `follow_redirects=False`, `trust_env=False`, `cookies=None`, `auth=None`, `Accept-Encoding: identity`, `retries=0` on the transport, and the per-redirect-hop re-parse, re-resolve and re-check exactly as they are.
- No env proxies, no `follow_redirects=True`, no disabling TLS verification.
- Logs may contain the hostname and exception class names only. Never a URL path, query, place name, user text or secret.
- Tool output derived from remote data (including URL-parsed place names) is returned with `untrusted=True`.

## 1. Address ordering and fallback in `net_guard.py`

Settings — add to `NetGuardSettings`:
- `prefer_ipv4: bool = Field(default=True, validation_alias="NET_PREFER_IPV4")`
- `connect_attempts: int = Field(default=3, validation_alias="NET_CONNECT_ATTEMPTS")`; clamp the used value to 1..5.

`_resolve(host, port)`:
- Deduplicate while **keeping `getaddrinfo` order** (e.g. `dict.fromkeys`), no set.
- Then, if `prefer_ipv4`, stable-partition IPv4 addresses before IPv6 (relative order within each family preserved). Do this inside `_resolve` so `resolve_public` (used by the browser renderer) and `safe_get` both get it. Expose the partition as a small pure helper `_order_addresses(addresses: list[str], prefer_ipv4: bool) -> list[str]` so it is unit-testable.

`safe_get`:
- Factor the per-hop request into a helper so it can be reused by §2 (e.g. `async def _open_hop(...)` as an async context manager yielding the `httpx.Response`, or equivalent). Keep `safe_get`'s public signature and `FetchResult` unchanged.
- For each hop, after the existing `_check_ip` loop over **all** addresses, try `addresses[:connect_attempts]` in order. For each attempt build the pinned URL / Host header / SNI exactly as now.
- Retry with the next address **only** on `httpx.ConnectError` or `httpx.ConnectTimeout` raised **before any response is received** (i.e. from opening the stream). Any other error, any response (including 4xx/5xx and redirects), or an error while reading the body is never retried.
- Overall budget: all attempts and hops share the single `deadline = now + timeout_s` as today. Before each attempt recompute `remaining`; if `remaining <= 0` raise `FetchError("The page request timed out.")`. Give each attempt a connect timeout of `min(remaining, max(1.0, remaining / attempts_left))` (`httpx.Timeout(remaining, connect=…)`) so one black-holed address cannot consume the whole budget while others remain; the whole attempt (connect + body) stays inside `asyncio.timeout(remaining)`.
- When every attempt fails at connection level, raise `FetchError("The site could not be reached.")` **chained** (`from`) the last connection exception. Other `httpx.HTTPError`s keep `FetchError("The page request failed.")`, also chained. Every `FetchError` raised from a lower-level exception must use `raise … from exc`.
- Add a public helper `def failure_cause(exc: BaseException) -> str` that returns `type(exc.__cause__).__name__` when there is a cause, else `"none"`. Export it alongside `FetchError`.

Do not change `_check_ip`, `_check_ipv4`, `_check_hostname`, `_parse_url`, `parse_https_destination`, `check_public_ip`, `_BLOCKED_NETWORKS`, `_BLOCKED_HOSTS`.

## 2. Redirect-only resolution in `net_guard.py`

Add:

```python
@dataclass(frozen=True, slots=True)
class RedirectResult:
    final_url: str
    status: int | None   # status of the last response received; None when stopped before requesting final_url
    hops: int

async def resolve_redirects(
    url: str,
    *,
    timeout_s: float,
    stop_at: Callable[[str], bool],
    max_redirects: int = 5,
    extra_blocked: tuple[str, ...] | list[str] = (),
) -> RedirectResult:
```

Behaviour:
- Loop: if `stop_at(current_url)` is true **and** at least one hop has been made, return without requesting it. (The first URL is always requested.)
- Otherwise request `current_url` through the same per-hop helper as `safe_get` (same parse, hostname check, resolve, `_check_ip` on all addresses, pinning, SNI, Host header, address fallback, shared deadline). Use `GET` and **never read the body**: close the response as soon as headers arrive.
- On 301/302/303/307/308 with a `Location`, `current_url = urljoin(current_url, location)` and continue; the next iteration re-parses and re-checks the new URL exactly as `safe_get` does. Exceeding `max_redirects` raises `FetchError("The page redirected too many times.")`.
- On any non-redirect response, return `RedirectResult(current_url, status, hops)`.
- Same error mapping as `safe_get` (`BlockedURL`/`FetchError` pass through, timeouts → `FetchError("The page request timed out.")`, connection failures → `FetchError("The site could not be reached.")`, other `httpx.HTTPError` → `FetchError("The page request failed.")`, all chained).

## 3. Browser renderer (`policy.py`)

`EgressPolicy.open_tunnel` already checks every address and then tries each in turn; with §1 the order becomes IPv4-first. Additionally:
- Cap the attempts to `NetGuardSettings().connect_attempts` (clamped 1..5) addresses.
- Wrap each `self.connector(address, port)` in `asyncio.wait_for(..., timeout=…)` with a per-attempt timeout (add a constructor arg `connect_timeout_s: float = 5.0`) so a black-holed IPv6 address cannot hang the tunnel; treat `TimeoutError` like `OSError` (try the next one).
- Keep the final `raise OSError("Pinned destination connection failed.") from last_error`.
- Leave `authorize` and `_parse` unchanged.

## 4. Logging

- `plugin-web` `fetch_web_page`: split the handler.
  - `BlockedURL` → `logger.info("web fetch blocked host=%s error=%s", host, type(exc).__name__)`.
  - `FetchError` → `logger.info("web fetch failed host=%s error=%s cause=%s", host, type(exc).__name__, failure_cause(exc))`.
  - The user-facing `ToolResult.error(str(exc))` text is unchanged in mechanism.
- `plugin-browse` `service.py` "render failed" and `client.py` "renderer request failed": append `cause=%s` using `failure_cause(exc)`.
- `host` is always `urlsplit(...).hostname` — never the full URL.
- `plugin-research` reaches the network only through `fetch_web_page`, so it is covered by the plugin-web change. Do not edit it.

## 5. Google Maps / share link resolver (`chatcore_web/maps.py`)

Decision: no new tool. `fetch_web_page` detects map/share links and answers with structured place facts instead of fetching page HTML. Provenance (`seen_urls`), the per-turn fetch cap (counts as one fetch) and the page cache are applied **before** this branch exactly as for any other URL.

Public API of `maps.py`:

```python
@dataclass(frozen=True, slots=True)
class MapPlace:
    name: str | None          # place name or search text
    kind: str                 # "place" or "search"
    kgmid: str | None
    place_id: str | None      # ftid / !1s0x…:0x… / place_id / query_place_id / cid
    lat: float | None
    lng: float | None

def is_map_link(url: str) -> bool
def is_google_target(url: str) -> bool
def parse_map_url(url: str) -> MapPlace | None
async def resolve_map_link(url: str, *, timeout_s: float) -> MapPlace | None
def format_place(place: MapPlace) -> str
```

`is_map_link(url)` — true for (case-insensitive host, after IDNA normalisation, `http`/`https` only):
- `share.google`
- `maps.app.goo.gl`
- `goo.gl` when the path starts with `/maps`
- `maps.google.<tld>` and `maps.google.com.<cc>` / `maps.google.co.<cc>`
- `google.<tld>` / `www.google.<tld>` (incl. `google.com.sg`, `google.co.uk`) when the path starts with `/maps`

Match Google TLDs with a strict regex on the whole host (e.g. `^(www\.|maps\.)?google\.(com|[a-z]{2}|com\.[a-z]{2}|co\.[a-z]{2})$`); `google.evil.com`, `evilgoogle.com`, `google.com.evil.net` must not match.

`is_google_target(url)` — the stop condition: host is a Google host per the regex above, and the path is `/search` or starts with `/maps`.

`parse_map_url(url)` — pure, URL only, never network:
- `/maps/place/<name>/…` → `name` = URL-decoded segment with `+` → space, `kind="place"`.
- `/maps/search/<text>/…` → `name` = decoded text, `kind="search"`.
- Query `q` or `query` (on `/search` or `/maps`) → `name`. If `q` is just `lat,lng`, treat it as coordinates, not a name.
- `kgmid` query → `kgmid`; its presence makes `kind="place"`.
- Place id from `ftid`, `place_id`, `query_place_id`, `cid` queries, or `!1s(0x[0-9a-f]+:0x[0-9a-f]+)` in the path/`data` → `place_id`, `kind="place"`.
- Coordinates: prefer `!3d<lat>!4d<lng>` anywhere in the path or `data` query; else `@<lat>,<lng>` in the path; else `ll=<lat>,<lng>`; else `q=<lat>,<lng>`. Accept only floats with −90≤lat≤90 and −180≤lng≤180.
- Sanitise `name`: strip control characters, collapse whitespace, trim, cap at 200 chars; empty → `None`. Cap ids at 100 chars and keep them only if they match `[A-Za-z0-9_/:.\-]+`.
- Returns `None` unless the host is a Google host and the path is `/search` or `/maps…`, or when no name, id or coordinates are found. A Google URL that is not search/maps (e.g. `https://www.google.com/doodles`, `https://docs.google.com/…`) returns `None`.

`resolve_map_link(url, *, timeout_s)`:
- If `parse_map_url(url)` already yields a place (a full `www.google.*/maps/place/…` link), return it with no network call.
- Otherwise call `net_guard.resolve_redirects(url, timeout_s=timeout_s, stop_at=is_google_target)` and return `parse_map_url(result.final_url)` (may be `None`).
- Never logs URLs. `BlockedURL`/`FetchError` propagate to the caller.

`format_place(place)` — plain text, for example:

```
Google Maps link
Place: Ah Hua Cafe
Google entity id: /g/11abc
Coordinates: 1.30000, 103.80000
This link only identifies the place. Use search_web with the place name (plus area if known) for address, hours and reviews.
```

Omit lines with no value; for `kind="search"` write `Search text:` instead of `Place:`. Do not include the original or resolved URL.

Wiring in `fetch_web_page` (after provenance, fetch-cap and cache checks, before `safe_get`):
- If `is_map_link(normalized)`: `place = await resolve_map_link(normalized, timeout_s=self.settings.fetch_timeout_s)`.
  - If `place` is not `None`: content = `format_place(place)`; cache it under the same page key (same JSON shape, `content` field); `logger.info("map link resolved host=%s kind=%s", host, place.kind)`; return `ToolResult(content=..., untrusted=True)`.
  - If `None` (chain ended off Google, or a Google page with no place facts): fall through to the normal `safe_get` path on `normalized` (it follows the same guarded redirects).
- `BlockedURL`/`FetchError` from the resolver are handled by the same `except` blocks and log lines as §4.
- Do not add anything to `ctx.turn.seen_urls` from the resolver.
- Update the `fetch_web_page` tool description to: `"Read a link posted in this turn or returned by web search. Google Maps and share.google links return the place name and coordinates."`

## Tests

All unit tests use `monkeypatch` on `net_guard._resolve` / `socket.getaddrinfo` / `net_guard._make_transport` (with `httpx.MockTransport`; raise `httpx.ConnectError` / `httpx.ConnectTimeout` from the handler based on `request.url.host`) or `respx`. No live network.

`packages/core/tests/test_net_guard_fallback.py`:
1. `_resolve` keeps `getaddrinfo` order and dedups (patch `socket.getaddrinfo` to return v6, v4, v6-dup, v4-2); with `NET_PREFER_IPV4` default the result is `[v4, v4-2, v6]`; with `NET_PREFER_IPV4=false` it is `[v6, v4, v4-2]`.
2. IPv6 first, v6 connect fails (`ConnectError`), v4 succeeds: with `_resolve` patched to return `[public v6, public v4]`, `safe_get` returns the v4 response; assert both pinned hosts were attempted in order and both carried `sni_hostname` and the correct `Host` header.
3. `ConnectTimeout` on the first address also falls back.
4. All addresses fail: `FetchError("The site could not be reached.")`, `failure_cause(exc) == "ConnectError"`.
5. Attempts are capped: 5 addresses, all failing, `NET_CONNECT_ATTEMPTS=3` → exactly 3 attempts.
6. A blocked address anywhere (first, middle, last, and beyond the attempt cap) → `BlockedURL`, and the transport is never called.
7. No retry on a received response: first address returns 500 → result status 500, one attempt. No retry when the error happens while reading the body.
8. Timeout budget: first address handler sleeps past its per-attempt connect share (simulate by raising `ConnectTimeout` after `asyncio.sleep`) and total elapsed stays ≤ `timeout_s` + small tolerance; with the budget exhausted, the error is the timeout `FetchError`.
9. Each redirect hop re-resolves and re-checks: hop 1 falls back v6→v4, redirect to a host whose answers include a private address → `BlockedURL`.
10. `resolve_redirects`: follows 302 then 301, stops before requesting the URL for which `stop_at` is true, never reads the body (use a recording stream and assert it was not pulled), rejects a redirect to a private IP, enforces `max_redirects`.
11. `failure_cause` returns `"none"` for an unchained error.

`packages/plugin-web/tests/test_maps.py`:
1. `parse_map_url` on `https://www.google.com/search?q=Ah+Hua+Cafe&kgmid=/g/11abc&shndl=30` → name `Ah Hua Cafe`, kind `place`, kgmid `/g/11abc`.
2. `https://www.google.com/maps/place/Ah+Hua+Cafe/@1.3001,103.8002,17z/data=!3m1!4b1!4m6!3m5!1s0x31da19:0x5f1c!8m2!3d1.3005!4d103.8009` → name, place id, coordinates from `!3d!4d` (1.3005, 103.8009).
3. `@lat,lng` only, `ll=`, `q=lat,lng`, out-of-range coordinates rejected.
4. Non-Maps Google URLs (`https://www.google.com/doodles`, `https://docs.google.com/document/d/x`, `https://www.google.com/`) → `None`; lookalike hosts (`google.evil.com`, `evilgoogle.com`, `google.com.evil.net`, `share.google.evil.com`) → `is_map_link` false.
5. Name sanitisation: control chars stripped, length capped.
6. `resolve_map_link` share.google chain with a fake transport: `share.google/abc` → 302 `https://www.google.com/share.google?q=abc` → 301 `https://www.google.com/search?q=Ah+Hua+Cafe&kgmid=/g/11abc`; result name `Ah Hua Cafe`; the final `/search` URL is **not** requested.
7. `maps.app.goo.gl/xyz` → 302 `https://www.google.com/maps/place/Ah+Hua+Cafe/@1.3,103.8,17z` → place with coordinates, final URL not requested.
8. A full `/maps/place/…` link resolves with zero transport calls.

`packages/plugin-web/tests/test_web.py` additions:
1. `fetch_web_page` on a provenanced `share.google` link (patch `chatcore_web.maps.resolve_redirects` or the transport) returns the formatted place text, `untrusted=True`, counts one fetch, is cached, and does not call `safe_get`.
2. A map link whose chain ends off Google falls through to `safe_get`.
3. Unprovenanced map link is refused without any network call.
4. Log hygiene (`caplog`): for a `FetchError` with a `ConnectError` cause, the record reads `web fetch failed host=example.com error=FetchError cause=ConnectError`; for `BlockedURL` it reads `web fetch blocked …`; for the map path the record contains only host and kind. Assert no record contains the path, query, place name or `?`.

Browser renderer tests: `open_tunnel` caps attempts, times out a hanging connector (fake connector that sleeps) and moves to the next address, and still checks every address before connecting.

Browse tests: the "render failed" log includes `cause=` and no URL path.

Adapt existing tests only where the new behaviour requires it (e.g. assertions on the exact `FetchError` message for connect failures), and say which ones in your report.

## Done when
- `uv run ruff check .` and `uv run ruff format --check .` are clean.
- `uv run pytest -q -m "not integration"` passes.
- All new tests above exist and assert real behaviour (pinned host order, attempt counts, stream not pulled, log text).
- `git diff` touches only owned files.
- Report lists: files changed, tests added, any existing test changed and why, any deviation from this plan.
- Not in scope: live network checks (Claude runs the manual `share.google` check), integration tests against Postgres (Claude runs them), commits (Claude commits).
