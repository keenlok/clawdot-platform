# Track B: web tools, stocks tools, SSRF guard

Read `ARCHITECTURE.md` §2, §5, §6a "LLM-specific" (prompt injection, SSRF guard, size and time limits),
and §7c.

## You own

- `packages/plugin-web/` (package `chatcore_web`) and `packages/plugin-stocks/` (`chatcore_stocks`)
- `packages/core/src/chatcore/security/net_guard.py` (plus tests `packages/core/tests/test_b_net_guard.py`)

Don't edit any other `chatcore` file. Add `httpx` (and other deps) to **your** packages; add
`httpx` to `packages/core/pyproject.toml` only if `net_guard.py` needs it.

## 1. `net_guard.py`: SSRF-safe fetching (write this test-first)

`async def safe_get(url, *, max_bytes, timeout_s, allowed_types, extra_blocked=()) -> FetchResult`
- `FetchResult`: final URL, status, content type, `bytes`, and `truncated: bool`.
- `extra_blocked` is a list of CIDRs and hostnames to block on top of the defaults.

**URL checks:**
- `http`/`https` only, ports 80/443 only (explicit or default).
- Reject userinfo in the URL. Send no cookies and no auth headers.
- Hostname normalization: IDNA-encode, lowercase, strip the trailing dot. Treat decimal, octal and
  hex IPv4 forms (`http://2130706433/`, `http://0x7f.1/`) as IPs and check them.

**Pinned resolution:**
- Resolve with `getaddrinfo` in a thread and check **every** returned address against the blocked
  ranges. Then connect to the checked IP while sending the original `Host` and SNI, using httpx's
  `extensions={"sni_hostname": host}` with the URL host replaced by the IP. That way DNS rebinding
  can't swap it.
- Follow redirects **manually**, at most 3, re-checking each hop from scratch.

**Blocked ranges (defaults):**
- IPv4: `0.0.0.0/8`, `10/8`, `127/8`, `169.254/16`, `172.16/12`, `192.168/16`, `100.64.0.0/10`,
  `192.0.0.0/24`, `198.18/15`, `224/4`, `240/4`.
- IPv6: `::1`, `::/128`, `fc00::/7`, `fe80::/10`, `ff00::/8`. IPv4-mapped `::ffff:0:0/96` addresses
  are checked against the IPv4 list.
- Hostnames `localhost`, `*.localhost`, `postgresql`, `elasticsearch`, `sb-bot-valkey`, and any
  single-label host.
- `NetGuardSettings` (`NET_GUARD_EXTRA_BLOCKED`, comma list) adds the box's own addresses
  (`192.168.1.10`, `100.64.0.10`, Docker bridges) at deploy time.

**Response handling:**
- Stream the response and abort past `max_bytes`, marking `truncated=True`.
- Check `Content-Type` against `allowed_types` before reading the body.
- Raise `BlockedURL` (message safe to show the model) or `FetchError`.

**Tests** (no network: mock the resolver and transport):
- every blocked range;
- a redirect to `127.0.0.1`;
- a DNS answer mixing public and private IPs;
- decimal, octal and hex IPs;
- `[::ffff:127.0.0.1]`;
- userinfo, a bad scheme, a bad port;
- the size cap;
- the content-type gate;
- the IP being pinned (the connection goes to the checked IP).

## 2. `plugin-web`: `WebPlugin(BasePlugin)`, `name = "web"`

**Settings** (`WebSettings`): `TAVILY_API_KEY` (`SecretStr`), `WEB_MAX_RESULTS=5`,
`WEB_FETCH_MAX_BYTES=5_000_000`, `WEB_FETCH_TIMEOUT_S=10`, `WEB_FETCH_MAX_CHARS=50_000`,
`WEB_MAX_FETCHES_PER_TURN=3`.

**`search_web(query: str, max_results: int = 5)`:**
- Tavily `POST https://api.tavily.com/search` with the key in an `Authorization: Bearer` header,
  never in the URL. Use plain `httpx`, not `net_guard`, since this is a fixed trusted API.
- Return the title, URL and a snippet (trimmed to about 300 chars) for each result as
  `untrusted=True`.
- **Add each result URL to `ctx.turn.seen_urls`.**
- Cache in `ctx.stores.cache` for 1 h, keyed by a hash of the normalized query.

**`fetch_web_page(url: str)`:**
1. **URL provenance:** if the URL (normalized) isn't in `ctx.turn.seen_urls`, return an error:
   "I can only open links that someone posted or that came from a search." Track A fills
   `seen_urls` with human-posted URLs.
2. Enforce `ctx.turn.fetches < WEB_MAX_FETCHES_PER_TURN`, then increment it.
3. Fetch via `net_guard.safe_get`, allowing `text/*`, HTML, PDF and JSON.
4. Extract the text:
   - HTML with `trafilatura`;
   - PDF with `pypdf`, at most 50 pages;
   - text and JSON as is.
5. Truncate to `WEB_FETCH_MAX_CHARS` and return it as `untrusted=True`, with the final URL and
   title.
6. Cache the extracted text for 6 h, keyed by URL.

Log hostnames only.

## 3. `plugin-stocks`: `StocksPlugin(BasePlugin)`, `name = "stocks"`

**Settings:** `STOCKS_API_BASE` (default `https://api.stockinvesttoday.com/api`), with a 10 s
timeout.

**Read-only tools** (each result is `untrusted=True`, trimmed to the useful fields, and cached
15 min):

| Tool | Endpoint |
|---|---|
| `stock_analysis(ticker, lang="en")` | `GET /analysis/{ticker}?lang=` |
| `stock_digest(lang="en")` | `GET /digests/latest?lang=` |
| `stock_prices(ticker, days=30)` | `GET /stocks/{ticker}/prices?days=` (days 1–365) |
| `stock_list(sector?, recommendation?)` | `GET /analysis?page=1&page_size=20&...` |

- Validate tickers against `^[A-Z][A-Z0-9.\-]{0,9}$` (uppercase first) so nothing odd reaches the
  URL path.
- Every result states that this is stored analysis with its date, not a live quote.
- Handle 404 as "no analysis for X".
- Inspect the real response shapes by reading stock-investor's README if needed. Otherwise build
  fixtures from these field names and keep parsing tolerant (missing fields are fine).

## Tests

**Web (with `respx`):**
- Tavily request shape, the key only in the header, and `seen_urls` populated;
- provenance refusal;
- the fetch cap;
- HTML, PDF and JSON extraction (with tiny fixture files);
- truncation;
- the cache hit.

**Stocks:**
- ticker validation (including path-traversal attempts like `../x`);
- each endpoint's trimming, 404, and the cache.

**Both plugins:** they register via the `Plugin` protocol, and their tool schemas are valid JSON
Schema objects.

## Done when

All tests pass, ruff is clean, and the SSRF suite covers every bullet in §6a's SSRF list.
