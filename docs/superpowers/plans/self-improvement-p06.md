# P06 Protected Telegram Bridge Implementation Plan

> **For agentic workers:** Local inline execution under the approved P06 assignment.

**Goal:** Add an injectable, local-only Telegram bridge core that authenticates ordinary clients, bounds Bot API operations to allowlisted chats and app-owned messages, protects broker release cards and callback authority, and returns file bytes without token-bearing URLs.

**Architecture:** `TelegramBridge` accepts a configured bot token privately, an authenticated-principal resolver, policy sets, an injectable Bot API transport, and an optional ownership store. Every request must match an exact bounded JSON schema and supported ordinary operation; broker callbacks/cards are never exposed to ordinary callers. `TelegramBridgeClient` is a token-free ordinary caller contract that submits only bridge requests. The core does not implement webhook ingress, Postgres decision/outbox behavior, network endpoints, token rotation, or production adapter wiring.

**Tech Stack:** Python 3.12 standard library; pytest and Ruff already available in the package workspace.

---

### Task 1: Establish the local bridge and client contract

**Files:**
- Create: `packages/plugin-release-control/src/chatcore_release_control/telegram_bridge.py`
- Test: `packages/plugin-release-control/tests/test_telegram_bridge.py`

- [x] **Step 1: Add failing contract tests**
  - A valid authenticated principal can send a bounded plain text message to an allowed chat through the fake transport.
  - Unknown principal, denied chat, unknown operation, extra/missing request fields, and malformed types fail with a fixed `BridgeError.code`.
  - Exceptions and object representations never include the configured token or fake transport exception text.
  - `TelegramBridgeClient` carries no token field or token URL and only submits the documented request object.
- [x] **Step 2: Run the focused tests and observe missing-module/import failures.**
  - Run: `uv run pytest packages/plugin-release-control/tests/test_telegram_bridge.py -q`
  - Expected before implementation: import failure for `chatcore_release_control.telegram_bridge`.
- [x] **Step 3: Implement only the public error, transport protocol, bridge configuration, strict dispatcher, and token-free client contract needed by the tests.**
  - Require exact top-level request keys `operation` and `payload`; reject unknown keys and nested keys per operation.
  - Bound serialized request size, strings, list lengths, numeric IDs, and returned data.
  - Use stable error codes without chaining transport exception text.
- [x] **Step 4: Re-run focused tests and observe the contract pass.**

### Task 2: Enforce identity, allowlists, and message ownership

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/telegram_bridge.py`
- Test: `packages/plugin-release-control/tests/test_telegram_bridge.py`

- [x] **Step 1: Add failing tests** for required authenticated principal, method allowlist, allowed chat, send-result ownership registration, and edits/deletes only for messages recorded as app-owned in that chat.
- [x] **Step 2: Run those tests to observe policy failures against the current implementation.**
- [x] **Step 3: Implement an explicit method-to-operation table** for ordinary sends, polls, documents, owned-message edits/deletes/pins, typing, membership lookup, leave, and byte-only file download. Callback answering is intentionally omitted until the ingress layer can distinguish ordinary callback queries from protected release-card callbacks. Do not expose generic Bot API method dispatch.
- [x] **Step 4: Re-run focused tests and observe ownership/allowlist cases pass.**

### Task 3: Protect broker release messages and callback authority

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/telegram_bridge.py`
- Test: `packages/plugin-release-control/tests/test_telegram_bridge.py`

- [x] **Step 1: Add failing tests** for broker-marked release message mutation denial; release callback namespace denial; arbitrary callback markup, webhook administration, token administration, copying/forwarding protected messages, and raw Bot API methods all being rejected before transport.
- [x] **Step 2: Run those tests and confirm the protected operation boundary is missing.**
- [x] **Step 3: Implement broker-only registration of protected message IDs and namespace checks.** The ordinary caller cannot supply raw reply markup. A bounded `confirm` operation generates only ordinary `c:<action>` and `x:<action>` buttons; any broker release namespace (including the reserved `release:` prefix) is inaccessible. Copy/forward operations are never in the method table.
- [x] **Step 4: Re-run focused tests and observe all protected-message cases pass.**

### Task 4: Proxy files as bytes and add bounded local ownership persistence

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/telegram_bridge.py`
- Test: `packages/plugin-release-control/tests/test_telegram_bridge.py`

- [x] **Step 1: Add failing tests** proving file retrieval returns only bounded bytes (never file path, URL, or token), enforces declared and actual size, rejects another principal/chat's file handle, and persists/reloads ownership across store reopen when a local store is configured.
- [x] **Step 2: Run the tests and confirm file/ownership behavior is absent or incomplete.**
- [x] **Step 3: Implement opaque file handles scoped to principal and allowed chat, a byte-only transport download method, and a separate atomic JSON ownership store under a caller-selected local path.** Use restrictive permissions where supported, reject symlinks/non-regular store files, and do not claim production durability or multi-process transactional safety.
- [x] **Step 4: Re-run focused tests and verify all file and persistence cases pass.**

### Task 5: Review and verify P06 locally

**Files:**
- Review: `packages/plugin-release-control/src/chatcore_release_control/telegram_bridge.py`
- Review: `packages/plugin-release-control/tests/test_telegram_bridge.py`
- Review: `docs/superpowers/plans/self-improvement-p06.md`

- [x] **Step 1: Inspect the actual diff and adapter call surface** without editing the adapter.
- [x] **Step 2: Run focused pytest and Ruff commands** from the repository root:
  - `uv run pytest packages/plugin-release-control/tests/test_telegram_bridge.py -q`
  - `uv run ruff check packages/plugin-release-control/src/chatcore_release_control/telegram_bridge.py packages/plugin-release-control/tests/test_telegram_bridge.py`
- [x] **Step 3: Record implemented boundaries, test evidence, and remaining production integration/persistence/rotation work.**
- [x] **Step 4: Report SI mapping truthfully:** local fake-transport controls contribute limited unit evidence for SI-03; authenticated update replay, transactional decision/outbox, restart, owner removal and rotation are not implemented (SI-07 remains unproven); full deployed-delta review/card authorization and inherited-code scenarios remain external dependencies (SI-12 remains unproven).

## Initial bridge-core result and limits (historical increment)

The local core uses a strict operation table, authenticated principal resolver, principal-to-chat allowlists, fixed errors, owned-message tracking, a separate broker-only protected-card registry, generated ordinary confirmation markup, and opaque file handles returning bytes. The ownership JSON store uses atomic replacement and restrictive mode bits where supported; it has no OS lock, multi-process transaction guarantee, encryption, or production-safe lifecycle. No Bot API, HTTP endpoint, Telegram ingress, callback consumer, Postgres ledger/outbox, adapter integration, webhook administration, live rotation, or production verification is implemented here. The fake transport tests prove only local dispatch behavior.

## Additional review repair plan

- [x] Persist broker-protected message registrations in the ownership store's versioned schema, reject the legacy unversioned store safely, and restore protected IDs before ordinary requests can be served.
- [x] Fail closed on protected-registry persistence or reconstruction errors so later mutation requests cannot run in a bridge instance missing authoritative protected-message state; test restart reconstruction and write/load failure.
- [x] Run focused bridge tests, Ruff and diff checks; mark only locally proven plan steps complete and describe production ledger reconstruction/adapter wiring as remaining integration.


## Authenticated webhook increment (main writer)

Add `telegram_ingress.py` and `test_telegram_ingress.py` with strict bounded JSON and
constant-time webhook-secret verification. Consume the release callback namespace
before ordinary relay, verify the current numeric owner/private owner DM and exact
broker message, resolve the card via protected ledger lookup, then commit the typed
DecisionCommand before answering the callback. Short callback data stores only the
nonce/action (UUID/binding resolved server-side). Ordinary updates are authenticated
with a separate HMAC relay key; app-forwarded JSON is never a decision source. Tests
use fake ledger and acknowledgement/relay to verify ordering/outage/forgery denial;
actual ledger atomicity has separate real-Postgres evidence. No HTTP listener or
Telegram request is started; adapter ingress cutover remains maintenance work.

Webhook increment local evidence (before cross-module integration): 8 focused cases pass; Ruff passes. Correct owner
callbacks commit before ack; wrong secret/owner/group/malformed reserved namespace
never relay. Commit outage never acknowledges; ordinary relay HMAC is verified by a
separate receiving helper. No webhook or Telegram message was sent. HTTPS handler
installation, deduplication integration with the real ledger, owner-policy races at
merge/switch admission and ordinary adapter compatibility remain integration work.

Independent repair P06-R1: protected messages are now persisted in a strict version-2
ownership/registry snapshot, with file and parent-directory fsync. Legacy unversioned
stores are refused for explicit maintenance reconciliation. Constructor requires an
authoritative protected-message snapshot loader before serving ordinary requests;
loader failure prevents construction, and protection-persistence failure disables the
instance. Restart/persistence-failure regression cases pass. Focused count: 18 passed;
Ruff passed. This store is still not a multi-process broker ledger. The installed
service must fetch a complete protected ledger snapshot before constructing the core.

Targeted independent re-review of P06-R1: PASS. Current limits remain single-process
local storage, mandatory authoritative startup snapshot and uninstalled HTTP/adapter
transport. The separate P10 real-ledger ingress test now proves local commit-before-ack
and replay behavior; it does not prove Telegram cutover or owner-policy race admission.

## P06 HTTP, card-issuance and ordinary-adapter increment (new local work)

### Goal and bounds

Add a concrete bounded ASGI bridge endpoint and token-free HTTPS/private-UDS client,
an approval-card issuer that calls the real `ReleaseLedger` API and injected Bot API
transport, and a new `ChatPlatform` adapter that forwards ordinary operations over
the authenticated bridge. Narrow changes to the existing bridge operation schema are
allowed for topics/replies/fixed-format rendering, poll stopping, and ingress-issued
opaque callback acknowledgement handles. Do not change existing Telegram ingress,
ledger, adapter `__init__.py`, or application wiring in this increment. No production
server, real Telegram transport, token, network call, or migration is part of this work.

### Task 1: Write contract tests and plan against existing interfaces

Files: new `bridge_http.py`, `telegram_cards.py`, `chatcore_telegram/bridge.py`, and
their tests. Inspect `ChatPlatform`, `FilePlatform`, `PollPlatform`, existing adapter
mapping, `TelegramBridge`, `TelegramIngress`, `ReleaseBinding`, `CardNonce`, and
`ReleaseLedger` before defining public APIs. Add tests first for ASGI auth, strict
bounded requests, fixed safe errors, TLS/UDS client construction, authenticated relay
consumption, card content and callback shape, uncertain send, adapter protocol
compatibility, event mapping, and all ordinary operation semantics. Run the new tests
to capture expected initial import/contract failures before implementation.

### Task 2: Implement ASGI HTTP serving and token-free client

Expose only the strict bridge operation contract through bounded ASGI routes,
authenticate credentials before dispatch, use fixed response/error schemas, and never
log raw bodies or exception diagnostics. Support a fixed HTTPS origin (reject cleartext,
userinfo, query, and fragment) or private Unix domain socket; no insecure network
default. Return proxied file contents as bounded bytes, not Bot API URLs. A separate
authenticated relay queue must accept only canonical bodies whose HMAC verifies with
the configured internal relay key, and ordinary clients may retrieve only their own
bounded authenticated relay events. No release decision operation is exposed.
Operations also include a filtered `get_me` response and bounded `set_commands`
authorized for one explicitly configured principal, with only fixed private/group
scopes and immutable broker-reserved command names.

### Task 3: Implement broker card issuance

Take immutable `ReleaseBinding`, `CardNonce`, configured ledger and Bot transport.
Construct plain text showing D/B/H and the inherited D→B and PR B→H delta sections
separately, plus a short capped untrusted prose summary. Keep link previews disabled;
do not use rich markup for untrusted prose. Use only the short `release:a:<nonce>` and
`release:r:<nonce>` callbacks. Treat transport ambiguity, malformed response, or ledger
failure as an unapprovable orphan; only a well-formed confirmed Telegram message ID
followed by successful ledger registration is a returned approvable card. Card tests
use fake Bot transport and a call-capturing ledger double to verify ordering; P05/P10
separately provide disposable local Postgres transaction/replay evidence.

### Task 4: Implement token-free ordinary `ChatPlatform` adapter

Use a separate small shared HTTP client contract without importing protected release
code, token, signing key, or Bot API client into the ordinary adapter. Consume only
authenticated relay events. Implement existing ordinary `ChatPlatform`, optional
`FilePlatform` and `PollPlatform` semantics through bridge calls, preserving thread,
reply, confirmation action, poll, file, membership, leave, typing and pin behavior.
There must be no Bot API fallback or token setting. Preserve bot identity lookup with a
strictly filtered `get_me` response. Preserve command-menu setup through one explicitly
configured menu principal and an exact private/group scope; merge immutable broker
reserved names ahead of caller commands so they cannot be replaced. If that capability
is not installed, startup fails clearly. Keep current legacy adapter tests passing.
Shared bridge extensions must remain bounded and preserve protected-card,
release-namespace and message-ownership checks. Callback acknowledgement accepts only
opaque handles registered by trusted ingress and can never answer release callbacks.

### Task 5: Focused verification and evidence limits

Run only focused new bridge/card/adapter tests plus existing adapter tests and Ruff.
Inspect all actual changes. No live Telegram, HTTP network, webhook listener, app
wiring, secret, rotation, production DB, commit or push. Record completion only for
locally implemented behavior; SI-03/07/12 end-to-end acceptance, TLS/UDS deployment,
card owner-policy admission/dispatch integration, migration and token rotation remain
unproven.

### Local implementation result

Implemented the bounded ASGI boundary, token-free HTTP clients, authenticated relay
queue and adapter in the planned files. The ASGI app reuses the bridge's principal
resolver and rejects relay audiences outside its configured chat allowlist. It accepts
HTTPS scopes by default; plain HTTP is accepted only under explicit private-UDS server
configuration. UDS clients verify a same-user socket with owner-only socket mode and a
same-user non-writable parent directory. Request bodies, relay count/bytes/wait time,
files, event payloads and response shapes are bounded. The relay queue is process-local,
not durable. It replaces Telegram file IDs and ordinary callback query IDs with opaque
handles; release callback namespace updates are rejected. The adapter consumes only
that authenticated relay and owns no Bot API client/token.

Card issuance prints D/B/H commit/tree/config/policy identity and separates inherited
D→B from PR B→H paths. It validates those identities against the bound review bundle,
caps and flattens untrusted prose, disables previews, persists message protection
before creating the ledger card, and returns a card receipt only after both operations
succeed. A send ambiguity has no ledger registration; post-send protection/ledger
failures leave no returned approvable receipt.

The ordinary bridge contract now has bounded reply/thread/entity fields, owner-checked
poll stopping, filtered `get_me`/member data, opaque one-shot callback acknowledgement,
and one explicitly configured command-menu principal. Broker-reserved commands replace
same-named application commands in fixed private/group scopes. If menu permission is
not configured, adapter startup fails without a direct Bot API fallback.

Focused evidence: **110 tests passed** across bridge core, HTTP/cards, new bridge
adapter and existing Telegram adapter tests; Ruff check and format pass. HTTP tests use
in-process ASGI/`httpx` transports, and UDS tests only create a short-lived private
local socket to check permissions; no HTTP network or Telegram API is called. Card
ordering tests use a ledger test double, while P05/P10 hold the separate temporary
Postgres evidence. Existing adapter module and application wiring were not switched.

Remaining: install/mount the ASGI app on a real TLS/private-UDS listener; configure the
matching principal/chat audiences, menu principal and reserved commands; wire the
adapter into app startup after token rotation; migrate the ingress relay callback to
`accept_relay`; replace the process-local relay queue with durable/restart-safe delivery
if required by deployment; exercise `getChatMember` not-found mapping; prove live
Telegram token revocation, callback/owner revocation races, outbox dispatch and host
receipt reconciliation. This increment gives bounded local source evidence for SI-03,
local authenticated-relay evidence relevant to SI-07, and card-content/binding evidence
relevant to SI-12. None is a deployed end-to-end acceptance proof.

## P06 durable relay, concrete transport and UDS service increment

### Goal and bounds

Replace the process-local relay queue with a maintenance-installed Postgres queue,
add the concrete fixed-origin Telegram Bot API HTTP transport, and make a runnable CLI
service mount the current ASGI endpoint on a verified owner-private Unix socket.
Startup must load strict root-owned config/secret files, establish protected Postgres
access, reconstruct every release-card message ID before the bridge is ready, and fail
closed on any DB/config/ownership error. No production config, secret, listener,
Telegram request, network call, webhook switch, or rotation is part of this work.

### Task 1: Specify durable relay semantics and write failing tests

Create `durable_relay.py`, maintenance-only `relay_schema.sql`, and focused tests.
Persist stable relay row IDs plus Telegram update IDs, use unique principal/update
deduplication, reject mismatched replay payloads, cap payload/queue sizes and delivery
leases, claim with row locks/skip-locked semantics, and remove an event only after
authenticated explicit ack. Delivery is at-least-once after lease expiry; do not claim
exactly-once processing. Test replay, restart recovery, duplicate mismatch, concurrent
claim exclusion, pending limits, explicit ack, and lease-based redelivery using only a
temporary local Unix-socket Postgres instance.

### Task 2: Integrate claim/ack into ASGI client and ordinary adapter

Replace destructive in-memory `/v1/events` polling with durable claims carrying stable
relay IDs and update IDs, and add strict authenticated ack requests scoped to the
principal and active claim. The token-free client exposes `ack_events`; the adapter
acks only after the ordinary event handler completes successfully. A failed handler or
ack leaves delivery eligible after lease expiry. Callback/file identifiers are
redecorated per delivery using opaque bridge handles. No release callback is relayable.

### Task 3: Implement concrete Bot API HTTP transport

Use `httpx` with fixed `https://api.telegram.org`, TLS verification, environment
proxy use disabled, redirects disabled, fixed method allowlist, bounded timeouts and
response bodies, no response/exception logging, and fixed safe transport errors. Build
Bot API paths internally from the protected token. For file downloads, resolve
`getFile.file_path` only inside the protected transport, validate it, stream no more
than the requested byte cap, and return bytes only. Test requests with injected mock
HTTP transports only; never call Telegram.

### Task 4: Implement strict config loading and executable local service CLI

Create `bridge_service.py` with strict TOML config and separate protected secret files.
Reject unknown keys, symlinks, non-regular files, wrong owner, insecure modes, malformed
principal/chat/command entries, and missing service paths with fixed errors. CLI mode
requires root-owned protected files and a root-owned private UDS directory/socket; config
validation helpers accept a test-only expected UID argument for synthetic files. `--help`
and `--check-config` do not open a listener or connect to PostgreSQL. Runtime builds the
DB pool, reads all protected release-card message registrations, constructs ready
bridge/issuer/ASGI objects, then serves only the configured private UDS. Schema
installation remains a separate explicit maintenance action.

### Task 5: Verification and evidence limits

Run focused durable relay/transport/config tests, the HTTP/card/adapter regression
tests, and Ruff. Review actual owned changes. No production files, configs, credentials,
Postgres, external HTTP, Telegram API, listener, app wiring or cutover. Report that the
queue offers at-least-once delivery; owner-policy admission, Telegram webhook mounting,
service installation, runtime config provisioning, live token revocation and overall
SI-03/07/12 acceptance remain external.

### Owner namespace repair

`ReleaseBinding.owner_id` is the GitHub repository owner numeric ID. Telegram chat/user
IDs are a separate namespace. Card issuance now receives a separate installed current
Telegram-owner policy callback and requires the target Telegram owner to match it;
it preserves the GitHub owner ID in the immutable release binding. The focused fixture
uses GitHub owner 202 and Telegram owner 7, with wrong Telegram actors denied before
any send.

## Durable relay and runnable local service increment

### Scope and implementation steps

- [x] Define a maintenance-only `relay_schema.sql` for bounded per-principal events,
  stable relay UUIDs, unique Telegram update IDs, payload hashes, leases and ack state.
  The service never installs or migrates this schema.
- [x] Implement `DurableRelay` over an injected asyncpg pool. Exact update replays return
  the stored relay ID; a reused update ID with a different canonical payload rejects.
  Claims use row locks with `SKIP LOCKED`, expire after a bounded lease, and repeat
  until the ordinary adapter acknowledges after its event handler succeeds. This is
  at-least-once delivery; application handlers must tolerate duplicate delivery.
  Pending count and payload bytes are capped, acknowledged rows are retained for at
  most seven days and at most 4,096 rows per principal.
- [x] Replace the ASGI process-local queue with an injected durable relay, stable
  `relay_id`/`update_id` envelopes, and an authenticated principal-scoped ack route.
  File IDs and callback IDs remain only in the protected relay database and are turned
  into fresh opaque bridge handles when a leased event is materialized.
- [x] Add a fixed `https://api.telegram.org` Bot API transport with no redirects,
  `trust_env=False`, method allowlist, bounded responses/downloads, multipart file
  sends, strict file-path checks and fixed errors. Token-bearing paths stay inside the
  protected server transport and are never included in a result or exception.
- [x] Add strict TOML and secret-file loading, SHA-256 client-credential verification,
  Postgres ledger protection reconstruction before readiness, and a `--check-config`
  command. The service entrypoint is `python -m chatcore_release_control.bridge_service`;
  it binds an owner-only Unix socket and does not expose a plaintext TCP listener.
- [x] Keep injected fake relay/Bot transports for tests. Exercise the durable relay
  against a disposable local Postgres instance using Unix sockets with TCP disabled.

### Verification and boundaries

The focused checks cover exact dedup/replay rejection, database-backed lease retry and
ack, payload/pending limits, bridge relay auth/ack routing, adapter ack-after-handler,
strict synthetic configuration, fail-closed ledger reconstruction, and fake Bot API
requests including redirect/path/size failures. No database migration is run by service
startup. No actual Telegram request, internet call, production config, secret, public
listener, webhook route or app mode switch is exercised here. Installation of both
maintenance schemas, provisioned root-owned configuration, Unix-socket deployment,
TLS/private service operations, webhook-to-bridge integration, service supervision,
owner/policy admission races and token/key rotation remain deployment/integration work.

### Trusted check evidence card binding

Cards also require a 64-character lowercase SHA-256 `check_evidence_digest` in the
immutable review bundle and display that digest as the checks reference. The test
fixture uses a synthetic digest only. The card does not display caller-provided test
counts or model prose as check evidence. P04 must produce the canonical trusted check
snapshot digest, and the merge admission path must independently compare current
provider evidence to the stored digest; that provider wiring is not proven by card
issuance tests.

### SI acceptance mapping for this increment

- **SI-03:** Focused fake-transport tests prove local request bounds, ownership gates,
  protected card handling, token redaction, durable lease/ack behavior and fail-closed
  configuration reconstruction. They do not prove deployed operation or all hostile
  GitHub/Telegram flows.
- **SI-07:** The queue now survives service-object restart and redelivers expired leases,
  but production restart recovery, service supervision, owner-removal admission,
  rotation, webhook mounting and end-to-end provider integration remain unverified.
- **SI-12:** Card text binds D/B/H, inherited and PR deltas, policy and the trusted
  check-evidence digest. Production P01/P04 snapshot production and merge-time evidence
  equality are still external prerequisites; no untrusted count or model prose is treated
  as check evidence.

## Ordinary application factory wiring (main)

1. Add an explicit `TELEGRAM_TRANSPORT=bridge` mode and default existing direct mode.
   In bridge mode reject any Bot token, require a private ordinary credential file
   and exactly one HTTPS origin or private socket; never instantiate TelegramSettings
   or the direct platform. Malformed bridge configuration cannot fall back to direct.
2. Read the credential with no-follow/regular-file/UID/mode/size checks and construct
   only the token-free adapter. Add its HTTP cleanup to the application's exit stack.
   Reuse normal command/conversation wiring for both Telegram platforms.
3. Write composition tests with synthetic credential files and fake HTTP clients,
   verify wrong permissions/legacy token/partial config rejection and cleanup. No
   production environment, listener, ingress, credential or token rotation is changed.

Application-factory result: `sb_bot.telegram_transport` now validates explicit bridge
mode and a protected ordinary credential file before connecting services. It constructs
only BridgeTelegramPlatform, does not parse TelegramSettings in bridge mode, rejects
Bot tokens and incomplete/mixed configuration, shares menu wiring, and closes the
ordinary HTTP client through the application exit stack. Ten new synthetic composition
and configuration cases plus existing app cases pass (75 total). This is source wiring;
the default installed mode and every production environment remain unchanged.

## Protected route integration (main)

Mount TelegramIngress on an exact private-socket webhook route, preserve the dedicated
webhook secret header, and route ordinary updates to the durable relay. Add independent
broker credential card/report routes, unavailable with ordinary credentials. Require
maintenance-installed idempotency rows committed before each Bot API send; retries
return the stored response or an explicit uncertain outcome without blindly repeating
a send. Report text is generated from ledger states only. Test synthetic ASGI requests
and real temporary PostgreSQL, including response loss and crash-intent retry. Extend
strict service config with separate protected webhook and broker credential files.
No webhook registration, external ingress, message or deployment occurs.
