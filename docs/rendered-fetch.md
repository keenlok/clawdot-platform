# Sandboxed rendered fetch

Rendered fetch uses a separate `chatcore-browser-renderer` service to read text from JavaScript pages. It is an optional companion to the browse plugin and stays unavailable unless the `compose.browser.yaml` overlay is enabled. Once configured, allowed group chats default on; an explicit group opt-out remains off. DMs follow `BROWSE_DM_ENABLED`, which defaults to false.

## Threat model

The requested URL and everything returned by the page are untrusted. The renderer may load redirects, frames, scripts, and other page-controlled resources. The security goal is to prevent Chromium from reaching loopback, host/LAN networks, Docker services, cloud metadata, or other non-public destinations, even when a page tries to navigate or make background requests to them.

The in-process asyncio CONNECT proxy is the primary egress boundary. Chromium sends all TCP web connections through it. The proxy accepts only `CONNECT host:443 HTTP/1.1`, parses the authority with the shared `chatcore.security.net_guard` policy, resolves once, checks every DNS answer, then connects directly to a validated IP literal. It never dials the hostname, so a second lookup cannot rebind the connection. Redirects and subresources pass through the same proxy. TLS stays end-to-end between Chromium and the site. The proxy applies byte, tunnel, idle, and wall-clock budgets to each render.

Playwright request routing is a second, weaker layer that aborts non-HTTPS and unnecessary resource types. It is not the security boundary. A container with no secrets, read-only root filesystem, dropped capabilities, resource limits, and isolated networks provides a third layer. Apply the host firewall restriction described below because Compose cannot prevent the egress bridge from routing to private destinations.

The renderer has no API keys, database settings, cookies shared between requests, or persisted browser storage. It never clicks controls, submits forms, types into fields, uploads files, or accepts dialogs. It performs one navigation and extracts text only.

## Browser choice

We use Playwright's async Python API with Chromium. It provides a real JavaScript engine, a fresh `BrowserContext` for each render, request routing, and an official image containing Chromium and its system libraries. Splash and Selenium were rejected as stale or heavier options with weaker isolation primitives. Hosted browser APIs would send user URLs and page content to a third party. Raw CDP would require us to rebuild Playwright's browser lifecycle and isolation features.

The proxy is the security boundary, rather than Playwright routing, because route interception cannot observe every browser connection. HTTPS-only CONNECT on port 443 avoids plain HTTP proxying and per-request `Host` header validation. The bot upgrades a supplied `http://` link before requesting a render. Plain HTTP and `ws://` subresources are refused.

The proxy uses a per-context Basic credential as an unguessable render lease so CONNECT tunnels are charged to the right budget. Chromium may first send CONNECT without credentials; the proxy answers `407 Proxy Authentication Required` with `Proxy-Authenticate: Basic realm="render"`, closes that connection, and does not resolve or dial anything. Chromium retries with the context credentials, and only then can the proxy authorize the destination. Playwright's per-context proxy configuration supplies those credentials even though the proxy endpoint is also selected at launch. A real Chromium integration test exercises this exact handshake against a local HTTPS server. The launch and context proxy settings both force loopback through the proxy, and the browser process receives a scrubbed environment so inherited Playwright or generic proxy variables cannot override routing.

Chromium keeps its default user agent and browser identity. There are no stealth plugins, fingerprint patches, proxy rotation, challenge waits, CAPTCHA solving, or retries.

## Results and blocked sites

The service returns HTTP 200 and a JSON result for renderer-controlled outcomes: `ok`, `blocked_by_site`, `blocked_url`, `timeout`, `too_large`, `busy`, or `error`. Malformed HTTP requests return 400. It never returns exception text, internal addresses, or page text in error responses.

A conservative detector recognizes short anti-automation pages using known challenge phrases, challenge status codes with short bodies, or recognized challenge frames. A long article that mentions a CAPTCHA is not enough. A plain 404 is an error. When a site blocks access, the user-facing behavior is: **“This site blocked automated access.”** The renderer stops after its single navigation; it does not try another path or change browser settings to evade the block.

## Limits

| Limit | Default | Purpose |
| --- | ---: | --- |
| Concurrent renders | 2 | Return `busy` immediately instead of queueing work |
| Wall clock per render | 30 seconds | Stop pages that hang or run forever |
| Network bytes in and out | 12 MiB | Close tunnels when page traffic exceeds budget |
| Concurrent tunnels per render | 24 | Bound parallel connections |
| Tunnels opened per render | 150 | Bound connection churn |
| Tunnel idle time | 15 seconds | Close inactive tunnels |
| Returned text | 30,000 characters | Bound output sent to the bot |
| Browser lifetime | 50 renders | Recycle Chromium and its process state |
| Request body | 4 KiB | Bound service input |
| Proxy headers | 8 KiB / 5 seconds | Reject oversized or slow CONNECT requests |
| Container | 1536 MiB, 1.5 CPUs, 256 PIDs | Bound renderer resource use |

`RENDER_*` environment variables can adjust renderer capacity and time limits. `BROWSER_NO_SANDBOX` defaults to false. Keep Chromium's sandbox enabled where the host's seccomp profile supports it. If the sandbox must be disabled, retain the compensating controls in the overlay: non-root user, all capabilities dropped, read-only filesystem, no secrets, network isolation, and memory/CPU/PID limits.

## Network defense in depth

The renderer joins an internal network shared with the bot for service requests and a dedicated internet egress bridge. It does not join the database or cache networks. The overlay assigns `172.30.250.0/24` to the egress bridge; confirm that this subnet is unused on the host before deployment.

Docker Compose cannot express a host firewall policy. On the Docker host, identify the bridge interface for the dedicated renderer egress network, then install `DOCKER-USER` rules that drop forwarded traffic from its subnet to private, shared, link-local, CGNAT, and Tailscale destinations. Also restrict the host `INPUT` chain: traffic from the renderer bridge to the host or bridge gateway goes through `INPUT`, not `DOCKER-USER`. The example drops all renderer bridge traffic addressed to the host. Substitute the actual bridge interface and keep these rules persistent across reboots:

```sh
BRIDGE=br-<docker-network-id>
iptables -I DOCKER-USER 1 -i "$BRIDGE" -s 172.30.250.0/24 -d 10.0.0.0/8 -j DROP
iptables -I DOCKER-USER 1 -i "$BRIDGE" -s 172.30.250.0/24 -d 172.16.0.0/12 -j DROP
iptables -I DOCKER-USER 1 -i "$BRIDGE" -s 172.30.250.0/24 -d 192.168.0.0/16 -j DROP
iptables -I DOCKER-USER 1 -i "$BRIDGE" -s 172.30.250.0/24 -d 100.64.0.0/10 -j DROP
iptables -I DOCKER-USER 1 -i "$BRIDGE" -s 172.30.250.0/24 -d 169.254.0.0/16 -j DROP
iptables -I DOCKER-USER 1 -i "$BRIDGE" -o tailscale0 -j DROP
iptables -I INPUT 1 -i "$BRIDGE" -s 172.30.250.0/24 -j DROP
```

For nftables, insert equivalent rules into the host's hooked input and forward chains (create those base chains if the host does not already have them):

```sh
nft insert rule inet filter input iifname "$BRIDGE" ip saddr 172.30.250.0/24 drop
nft insert rule inet filter forward iifname "$BRIDGE" ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16 } drop
nft insert rule inet filter forward iifname "$BRIDGE" oifname "tailscale0" drop
```

Adapt the rules and chain names for the host's firewall manager, Docker nftables backend, and IPv6 policy. Persist both input and forward restrictions. The CONNECT proxy remains the primary control and does not depend on these host rules.

## Enable the service

A downstream application must provide a compose overlay, an internal request network,
a separate internet egress network, the documented firewall policy, a pinned image and
health checks. Do not attach the renderer to storage or cache networks. Review
[the shipped seccomp profile](../services/browser-renderer/SECCOMP.md) and keep Chromium's
sandbox enabled. The scaffold deliberately does not provision this optional service.

Set `BROWSER_RENDER_URL` in the bot's private environment and give the bot and renderer
identical `NET_GUARD_EXTRA_BLOCKED` values. The renderer must not receive the bot's
`env_file` or secrets. Group managers can use `/browsetools off` or `/browsetools on` with
confirmation; saved opt-outs persist, and DMs require explicit configuration.

Build/start, recovery, firewall persistence and cross-release rollback belong in the
application's deployment runbook. The original implementation instructions are retained
in the [private pre-split guide](https://github.com/keenlok/clawdot-deployments/blob/main/docs/archives/platform-ac5a4b0/docs/rendered-fetch.md).

Check `/healthz` through the internal network. Logs contain hostnames only, never URL
paths, query strings, page text or exception messages.

## Rendered watches

Website watches can use the optional headless renderer when `BROWSER_RENDER_URL` is configured.
Create one with `/watchrendered <seconds> <public URL>` or set `rendered=true` on
`create_website_watch`. The confirmation shows the URL, interval, retention window, and that the
first check is silent and only readable-text changes are reported. HTTP links are upgraded to
HTTPS before rendering.

Rendered watches have a minimum interval of one hour by default, configurable with
`JOBS_MINIMUM_RENDERED_WATCH_INTERVAL_SECONDS`. A global cap of 20 active rendered watches bounds
ongoing renderer use (`JOBS_MAX_RENDERED_WATCHES_GLOBAL`; zero disables them). Scheduled renders
use a separate semaphore, defaulting to one concurrent render (`JOBS_RENDERED_CONCURRENCY`), so
interactive page reads retain renderer capacity. Render fetches time out after 35 seconds by
default (`JOBS_RENDERED_FETCH_TIMEOUT_SECONDS`), bounded by the execution deadline:
`min(JOBS_RENDERED_FETCH_TIMEOUT_SECONDS, JOBS_EXECUTION_DEADLINE_SECONDS)`. The sample settings
use a 35-second execution deadline, a 15-second send deadline, and a 60-second lease; the lease
must remain greater than the execution plus send deadlines.

The first successful check stores a silent baseline; later checks notify only for readable-text
changes. A site that blocks automated access stops the watch and sends one notice without retrying
or falling back to a plain fetch. Busy renderer responses are retried after up to five minutes
without increasing the failure count. If a group disables rendered fetch, its existing rendered
watches are suppressed and cancelled on their next run; listings mark rendered watches and this
behavior.

## Adversarial tests

Unit tests use fake resolvers, connectors, browser drivers, and HTTP clients; they never call external sites. They cover numeric and abbreviated IPv4 forms, IPv6 and embedded IPv4, metadata and link-local addresses, private/LAN/CGNAT ranges, local and single-label names, blocked service names, credentials, non-443 ports, IDNA and trailing-dot handling, extra blocked CIDRs/hosts, mixed public/private answers, empty DNS answers, and DNS rebinding. They also cover malformed and oversized proxy requests, tunnel and byte limits, browser context cleanup, output truncation, the no-interaction source check, challenge detection, request validation, and fixed response shapes.

The authority tests include `127.0.0.1`, `127.1`, `2130706433`, `0x7f.0.0.1`, `0177.0.0.1`, `0`, `::1`, `::`, IPv4-mapped IPv6, NAT64 and 6to4 forms embedding loopback, Teredo embedding a private IPv4 address, `169.254.169.254`, `fd00:ec2::254`, scoped link-local IPv6, `100.64.0.1`, `100.100.100.200`, RFC1918 addresses, unspecified and multicast addresses, `localhost`, trailing-dot and subdomain forms, `postgresql`, `elasticsearch`, `sb-bot-valkey`, single-label hosts, userinfo authorities, malformed and non-443 ports, oversized or empty authorities, a Unicode lookalike of `localhost`, uppercase names, and configured blocked CIDRs, exact IPs, and hostnames. Public IPv4/IPv6 answers and ordinary dotted hostnames are also accepted by the fake-resolver tests.

Opt-in integration tests use a local HTTPS server and skip when Chromium or local sockets are unavailable. They exercise the real Chromium 407 credential retry, JavaScript rendering, default loopback refusal, and separate redirect, image, fetch, XHR, iframe, WebSocket, sendBeacon, and prefetch attempts against IPv4 and IPv6 canary listeners. Each vector checks both zero canary connections and a proxy refusal record. Tests also cover no form submission, context cookie isolation, download refusal, wall-clock enforcement, byte-budget enforcement, and Chromium process cleanup. They do not contact the internet.
