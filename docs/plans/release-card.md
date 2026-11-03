# Readable release approval card

Rework the self-improvement **release approval card** so the owner can tell what they are
approving on a phone. Only the card's presentation and two new read-only buttons (Details, View
diff) change. **Every approval binding and check stays exactly as it is.**

Read first:
- `ARCHITECTURE.md` §6a (security baseline: model-authored text is untrusted, link previews off, no
  secrets or user text in logs).
- `packages/plugin-release-control/src/chatcore_release_control/telegram_cards.py` (current card).
- `telegram_ingress.py`, `protected_bridge_http.py`, `bridge_service.py`, `telegram_bridge.py`
  (callback routing, message protection, broker → bridge `/v1/broker/cards`).
- `ledger.py` (`ReleaseBinding`, `CardNonce`, `CardRecord`, `get_card_by_message`) and
  `policy.py` (`Change`, `ReleaseBundle`) — read only.
- `git_evidence.py` (`_GitRunner`, `collect_release_bundle`), `publication.py` (`Scanner`,
  `ScanInput`, `ScanVerdict`, `ScannerBinaryAdapter`).
- Existing tests: `tests/test_telegram_cards.py`, `test_telegram_ingress.py`,
  `test_protected_bridge_http.py`, `test_bridge_service.py`, `test_git_evidence.py`,
  `test_database_roles.py`.

Environment: `export UV_CACHE_DIR="$PWD/.uv-cache"`, then `uv sync --all-packages`.
Tests: `uv run pytest packages/plugin-release-control -q`. Lint: `uv run ruff format .` and
`uv run ruff check .`. Integration tests (Postgres) skip without `TEST_PG_DSN`; the reviewer runs them.

## Background: what exists

- The card is issued by `ReleaseCardIssuer.issue(binding=..., owner_user_id=..., summary=...)`.
  `ProtectedBridgeApplication._broker_request` calls it for `POST /v1/broker/cards` with body
  exactly `{"request_id", "binding"}` and currently passes `summary=""`.
- `_text()` validates the review bundle (all those `invalid_card` checks) and then renders a wall
  of identifiers with the summary last. The validation is a contract; the rendering is not.
- The bridge has **no Git access** and no scanner. Anything computed from Git (line counts, the
  patch) must be computed by the card *producer* (the operator/broker side that already holds the
  object repository) and sent with the card request.
- `TelegramIngress.handle` routes `callback_query.data` starting with `release:` and only accepts
  `release:(a|r):<nonce>`. Ordinary bot callbacks can't use the `release:` prefix
  (`TelegramBridge.register_callback_handle`, `bridge_http`), so new `release:` callbacks are
  automatically protected from the ordinary bot.

## You own

- `packages/plugin-release-control/src/chatcore_release_control/telegram_cards.py`
- `packages/plugin-release-control/src/chatcore_release_control/release_card_view.py` (new)
- `packages/plugin-release-control/src/chatcore_release_control/git_evidence.py` — **additive
  only**: two new public functions; do not change existing functions or behaviour.
- `packages/plugin-release-control/src/chatcore_release_control/telegram_ingress.py`
- `packages/plugin-release-control/src/chatcore_release_control/protected_bridge_http.py`
- `packages/plugin-release-control/src/chatcore_release_control/bridge_service.py`
- `packages/plugin-release-control/src/chatcore_release_control/bridge_schema.sql`
- `services/release-control/src/chatcore_release_control/templates/p10-database-grants.sql.template`
- `apps/sb_bot/deploy/release-control/README.md` (a short note in step 6 only)
- Tests: `tests/test_telegram_cards.py`, `tests/test_release_card_view.py` (new),
  `tests/test_telegram_ingress.py`, `tests/test_protected_bridge_http.py`,
  `tests/test_bridge_service.py`, `tests/test_git_evidence.py`, `tests/test_database_roles.py`.

You may **not** edit `ledger.py`, `schema.sql`, `policy.py`, `telegram_bridge.py`,
`bridge_http.py`, `publication.py` or any other module. Ask in your report if one must change.

### Contracts that must not change
- `ReleaseBinding`, its digest, `CardNonce`, `create_card`, `record_decision`, the 24h expiry and
  every `invalid_card` validation currently in `_text()` (keep them, byte for byte in effect).
- Approve/Reject callback data stays `release:a:<nonce>` / `release:r:<nonce>`, and the `_CALLBACK`
  regex and decision path in `telegram_ingress.py` stay as they are.
- Owner re-check (`current_telegram_owner`), `protect_message` before `create_card`,
  `send_uncertain` / `protection_unavailable` / `ledger_unavailable` semantics.
- `/v1/broker/cards` still accepts the existing body `{"request_id", "binding"}` unchanged.
- No `parse_mode` or `entities` on any message; `link_preview_options: {"is_disabled": true}` on
  every `sendMessage`.

## 1. `release_card_view.py` — presentation model and pure rendering helpers

### 1a. `CardPresentation` (frozen dataclass) + `CardPresentation.from_json(value, binding)`
Fields (all optional except where noted; JSON keys identical to field names):
- `pr_url: str` — required. Must fullmatch
  `https://github\.com/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}/pull/<binding.pr_number>`.
  This is the only link the card contains.
- `pr_title: str` — untrusted, model-authored. Cleaned with the single-line cleaner, max 100 chars.
- `what_changes: tuple[str, ...]` — untrusted, ≤ 5 items, each cleaned, max 160 chars.
- `risks: tuple[str, ...]` — untrusted, ≤ 4 items, each cleaned, max 160 chars.
- `file_stats: tuple[FileStat, ...]` — computed by the producer from Git. `FileStat(path: str,
  added: int | None, deleted: int | None)`; `None` means binary. Ints are `0 <= n <= 10_000_000`.
  **Validation:** the set of paths must equal exactly the set of `path` values in the binding's
  `review_bundle["deployed_to_head"]` (no extras, no missing, no duplicates). Otherwise reject.
- `checks: tuple[CheckLine, ...]` — ≤ 8. `CheckLine(name, conclusion, detail)`: `name` matches
  `[A-Za-z0-9][A-Za-z0-9 _./-]{0,39}`, `conclusion` in
  `{"success","failure","skipped","neutral","cancelled","timed_out"}`, `detail` is `None` or matches
  `[A-Za-z0-9 .,:/+-]{0,40}` (e.g. `"1131 passed"`).
- `review_findings: int | None`, `review_fixed: int | None` — both or neither; `0 <= fixed <= findings <= 1000`.
- `patch_state: str` — required, one of `"included"`, `"too_large"`, `"unavailable"`.
- `patch: str | None` — present iff `patch_state == "included"`; UTF-8 length ≤ `MAX_PATCH_BYTES`
  (`384 * 1024`), no NUL bytes.
- `redacted_files: int` — number of patch sections redacted by the scanner, `>= 0`.

`from_json` rejects unknown keys, wrong types, and anything out of range, raising
`CardIssueError("invalid_card")` (import it from `telegram_cards`, or define the error in
`release_card_view` and re-export it from `telegram_cards` — pick whichever avoids a cycle, but
`telegram_cards.CardIssueError` must remain the same class callers already catch).
Also provide `to_json()` for storage (patch excluded; stored separately).

### 1b. Untrusted text cleaning
`clean_untrusted(value, *, maximum) -> str`: reuse the existing single-line cleaner (control and
line/paragraph separator chars → space, whitespace collapsed), then replace URL-like tokens
(`(?i)\b(?:[a-z][a-z0-9+.-]*://\S+|www\.\S+|t\.me/\S+)`) with `[link removed]`, then truncate to
`maximum` (append `…` when truncated, still within `maximum`). No other formatting is produced.

### 1c. Path grouping and computed flags (pure functions, from Git evidence only)
- `group_key(path)`: `packages/<name>` or `apps/<name>` for paths under those dirs; otherwise the
  path itself.
- `touches(file_stats) -> list[Group]` with per-group added/deleted sums (binary files count as a
  file but add 0 lines and set `has_binary`), file count, sorted by total changed lines desc, then
  key. Rendered as `- <key> (+A / −D, N files)`; a group of exactly one file renders the file path
  `- apps/sb_bot/app.py (+6 / −1)`; binary-only shows `(binary)`. Use the real minus sign `−`.
- Flags, each computed from the D→H paths:
  - `migrations`: any path with a `migrations/` directory segment ending in `.sql`, or a basename
    ending `schema.sql`.
  - `config`: basename in `{"pyproject.toml", "uv.lock", ".env.example"}`, or under a `config/`
    directory segment.
  - `deploy_ci`: under `.github/`, any `deploy/` directory segment, basename starting `Dockerfile`,
    or basename matching `compose*.yaml` / `compose*.yml`.
  - `release_control`: under `packages/plugin-release-control/`.
  Render one line, e.g. `No migrations · no config · no deploy/CI files`, or
  `⚠️ Migrations (2 files) · no config · ⚠️ deploy/CI files (1)`. When `release_control` is set add
  `· ⚠️ release-control files (N)`.

### 1d. Contradiction detection
`find_contradictions(lines, flags) -> list[tuple[int, str]]` over the untrusted `pr_title`,
`what_changes` and `risks`. Case-insensitive claim patterns:
- migrations: `\bno (?:db |database |schema )?(?:migrations?|schema changes?|db changes?|database changes?)\b`
- config: `\bno config(?:uration)?(?: changes?)?\b`
- deploy/CI: `\bno (?:deploy(?:ment)?|ci|workflow|release[- ]control)(?: files?| changes?)?\b`
When a line's claim is contradicted by a computed flag, the card shows
`⚠️ PR text says "no migrations", but migration files changed` (wording per flag) **instead of**
that untrusted line. For a contradicted title, keep the title line but show the warning as the
first line of "What changes".

### 1e. PR body parsing (producer side)
`parse_pr_summary(body: str) -> tuple[list[str], list[str]]`: return `(what_changes, risks)`.
Markdown headings (`#`… or a line ending `:`) matching `what changes|summary|changes` start the
first list; `risk|limits|risks / limits|known limitations` start the second. Collect `-`/`*`/`1.`
bullet items (stripped of the marker). If no "what changes" section exists, use the first up to
3 non-empty non-heading lines. Caps as in 1a. Pure, never raises on odd input.

### 1f. Patch redaction (producer side)
`redact_patch(patch: bytes, scanner: Scanner, allowed_paths: frozenset[str]) -> tuple[str, int]`:
split on lines starting with `diff --git `; scan each section separately with
`scanner(ScanInput(f"release_diff:{index}", section_bytes))`. A section whose verdict is not
`ScanVerdict.CLEAN`, or whose scan raises, or that isn't valid UTF-8 / contains NUL, is replaced
by `diff --git a/<p> b/<p>\n# [redacted: the secret scanner flagged this file's changes]\n`, where
`<p>` is the path parsed from the header **only if** it's in `allowed_paths`, else
`[redacted section]` with no path. Returns the redacted text and the redacted-section count.
Fail closed: never return an unscanned section.

### 1g. Producer helper
`build_card_presentation(repository, binding, *, pr_url, pr_title, pr_body, checks=(),
review_findings=None, review_fixed=None, scanner) -> dict` builds the JSON for
`/v1/broker/cards`'s `presentation` field: `collect_delta_stats` and `collect_delta_patch` on
`binding.deployed_identity.git_sha` → `binding.head_sha`, `parse_pr_summary(pr_body)`,
`redact_patch`, then validates its own output with `CardPresentation.from_json(..., binding)`
before returning. `patch_state` is `too_large` when Git output or the redacted text exceeds
`MAX_PATCH_BYTES`, and `unavailable` if collecting the patch fails for another reason (stats
failure is an error, not a fallback).

### 1h. Details text
`render_details(binding_json: Mapping) -> list[str]` (list of message texts, each ≤ 4096 UTF-16
units, at most 5 messages) from the stored `CardRecord.binding` dict: PR number, target, release
kind, D commit/tree, image digest, config generation, config digest, target sequence, B commit/
tree, "B is an ancestor of H: verified", H commit/tree, policy digest, trusted check evidence
digest, then the three path lists (`D → B inherited`, `B → H PR changes`, `Complete D → H delta`)
using the current `_display_path` format. If the lists don't fit in 5 messages, end with
`… and K more paths (full list in View diff)`. Plain text only.

### 1i. View store (Postgres, bridge-owned)
`ReleaseCardViewStore(pool)` with `ready()`, `async put(approval_id, presentation_json, patch,
patch_state)` (INSERT … ON CONFLICT DO NOTHING) and `async get(approval_id)`. Table in
`bridge_schema.sql` (maintenance-only, like the existing table):
```sql
CREATE TABLE IF NOT EXISTS release_control.release_card_view (
    approval_id uuid PRIMARY KEY REFERENCES release_control.release_card(id),
    presentation jsonb NOT NULL,
    patch_state text NOT NULL CHECK (patch_state IN ('included','too_large','unavailable')),
    patch bytea CHECK (patch IS NULL OR octet_length(patch) <= 393216),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((patch_state='included')=(patch IS NOT NULL))
);
```
plus an immutability trigger (no UPDATE/DELETE) and `REVOKE ALL … FROM PUBLIC`, following the
existing `bridge_broker_send` pattern. Grant `SELECT, INSERT` to `release_control_bridge` in the
p10 grants template. Update `test_database_roles.py` if it asserts the exact grant set.

## 2. `telegram_cards.py` — new card layout

`ReleaseCardIssuer.issue(*, binding, owner_user_id, summary="", presentation=None, now=None)`.
`presentation` is a `CardPresentation` or `None`. Keep `summary` for callers without a
presentation (it becomes the single "What changes" line, cleaned as untrusted).

Keep every validation in `_text()`. Replace only the rendering with this order (example):
```
Release request: PR #42 "Add /export to expenses"
https://github.com/keenlok/clawdot-platform-history/pull/42

What changes (from the PR description)
- Adds a CSV export of the active trip's ledger (/export)
- Members get a file in DM; nothing posted to the group

Touches
- packages/plugin-expenses (+180 / −12, 4 files)
- apps/sb_bot/app.py (+6 / −1)
No migrations · no config · no deploy/CI files

Checks
✅ tests 1131 passed  ✅ ruff  ✅ secret scan  ✅ path policy
Review: 2 findings, both fixed

Risk / limits (from the PR description)
- Large ledgers (>5k rows) truncated with a notice
Rollback: previous image kept (sha256:eeeeeeeeeeee); no DB change to undo

Approving merges and deploys b1b2c3d → production
```
Rules:
- Title: `Release request: PR #<n> "<clean title>"`; without a presentation or title:
  `Release request: PR #<n>`. Then the PR URL line when a presentation exists.
- Reconciliation releases: after the title, `⚠️ Reconciliation: includes previously undeployed
  changes (<X> inherited D→B files + <Y> PR B→H files)`, counts from the binding's
  `deployed_to_base` / `base_to_head` lengths.
- "What changes": untrusted bullets (or the legacy summary line); omit the section if empty.
  Contradiction warnings replace contradicted lines (1d).
- "Touches": from `presentation.file_stats` when present; otherwise from the binding's
  `deployed_to_head` paths grouped the same way, without line counts (`(N files)`). The flags line
  is always present, computed from the binding's `deployed_to_head` paths.
- Checks: one line joining `✅`/`❌`/`⚪` + name (+ detail) for each `CheckLine`, two spaces apart;
  without checks: `Checks: trusted evidence bound (see Details)`. Review line only if recorded:
  `Review: no findings` / `Review: N findings, all fixed` (`both fixed` when N == 2) /
  `Review: N findings, M fixed`.
- "Risk / limits": untrusted bullets, omitted when empty.
- Rollback: `Rollback: previous image kept (<first 19 chars of image digest>)` + `; no DB change
  to undo` when no migration flag, else `; ⚠️ includes a DB migration — image rollback won't undo
  it`.
- Last line: `Approving merges and deploys <head_sha[:7]> → <target>`.
- **Length:** total must be ≤ 4096 UTF-16 units. If over, reduce the Touches list to the top N
  groups (N = 6, 5, … 1) followed by `- and K more files (full list in View diff / Details)`;
  if still over, drop untrusted bullets from the end (risks first, then what changes, leaving a
  `- …` marker); if still over, raise `invalid_card`.
- Keyboard: row 1 `Approve` (`release:a:<nonce>`), `Reject` (`release:r:<nonce>`) unchanged;
  row 2 `Details` (`release:i`) and `View diff` (`release:v`).

## 3. `protected_bridge_http.py`
`/v1/broker/cards` accepts `{"request_id","binding"}` (unchanged behaviour) **or**
`{"request_id","binding","presentation"}`. Parse the presentation with
`CardPresentation.from_json(raw, binding)` before sending anything (invalid → 400 `invalid_request`,
no Telegram call). Pass it to `cards.issue(...)`. After the receipt is returned, store the view row
via an injected `card_views` store (`put(receipt.approval_id, presentation.to_json(),
patch_bytes_or_None, patch_state)`); a store failure must not fail or retry the card (the card
is already sent and recorded) — swallow it, log only the error class name. Without a
presentation, store nothing. Keep the `send_once` idempotency (the presentation is part of the
request digest automatically).

## 4. `telegram_ingress.py` — Details and View diff callbacks
Add `_VIEW_CALLBACK = re.compile(r"release:([iv])\Z")` and an optional constructor argument
`card_views: Callable[[str, CardRecord], Awaitable[None]] | None = None` (kind `"details"` or
`"diff"`). For matching data, apply **exactly the same** sender/chat/message/query-id checks as the
decision path (owner sender, private chat equal to owner, positive message id, query id 1..256),
load `card = await self._ledger.get_card_by_message(owner, message_id)`, call
`await card_views(kind, card)`, then `acknowledge(query_id)`, and return `"release_view"`.
It must **never** call `record_decision`, touch the nonce, or change card status, and works
regardless of card status. If `card_views` is `None`, or anything fails, raise `IngressError`
(same as today). Any other `release:` data still raises.

## 5. `bridge_service.py` wiring
Create `ReleaseCardViewStore(pool)` (check `ready()` at start-up like the others) and a
`card_views(kind, card)` handler:
- `details`: send each text from `render_details(card.binding)` with the existing `send_report`
  helper (plain text, preview disabled, message protected).
- `diff`: `row = await store.get(card.approval_id)`. If `patch_state == "included"`, call
  `sendDocument` with `chat_id=owner`, `document=<patch bytes>`,
  `filename=f"pr-{n}-{head[:7]}.patch"`, `caption=f"D → H diff for PR #{n} ({k} file(s) redacted by
  the secret scanner)"` (omit the parenthetical when k == 0), then protect the sent message like
  `send_report` does. If `too_large`: send `Diff too large to attach. See the PR on GitHub:
  <pr_url>`. If no row or `unavailable`: `Diff not available. See the PR on GitHub: <pr_url>`
  (or `… See PR #<n> on GitHub.` when no URL is stored).
Pass `card_views=` to `TelegramIngress` and the store to `ProtectedBridgeApplication`.

## 6. `git_evidence.py` — additive helpers
- `collect_delta_stats(repository, older, newer) -> tuple[tuple[str, int | None, int | None], ...]`
  using `diff-tree --numstat -z --no-renames -r --no-commit-id older newer` through `_GitRunner`
  (same isolated env, timeout and output limits). Binary entries (`-\t-`) → `None, None`.
- `collect_delta_patch(repository, older, newer, *, max_bytes) -> bytes | None` using
  `diff-tree -p -r --no-renames --no-color --no-ext-diff --no-textconv --full-index
  --no-commit-id older newer`; returns `None` when output exceeds `max_bytes` (catch the runner's
  `output_limit`). Validate OIDs as `collect_release_bundle` does; raise `GitEvidenceError` codes
  on other failures.

## 7. README
In `apps/sb_bot/deploy/release-control/README.md` step 6, add two sentences: the card request may
carry a `presentation` built with `release_card_view.build_card_presentation` (Git-computed
stats, a scanner-redacted patch, untrusted PR text), and Details / View diff are read-only owner
callbacks.

## Tests

Snapshot tests compare the **full rendered card text** against an inline expected string.

`tests/test_telegram_cards.py`
- Update the existing first test: keep every assertion about binding, nonce, ledger call,
  `protect_message`, callback data and its 64-byte limit, `link_preview_options`, no
  `parse_mode`/`entities`, newline stripping. Replace assertions about identifier lines in the card
  text with assertions that they are **absent** from the card (D/B/H trees, digests now only in
  Details). Keep every other existing test green unchanged unless it asserted old layout text.
- Snapshot: ordinary release with a presentation (matches the layout above).
- Snapshot: reconciliation release — shows the reconciliation note with inherited/PR counts.
- Snapshot: legacy call without presentation (summary only, touches without counts).
- Many files: 40 packages → collapses to top N + `and K more files`, total ≤ 4096 UTF-16.
- Very long untrusted bullets/title are capped; URLs in untrusted text become `[link removed]`;
  the only URL in the card is `pr_url`.
- Contradiction: PR says "No migrations" but `packages/core/src/chatcore/migrations/0010_x.sql`
  changed → warning line shown, claim absent; flags line shows migrations; rollback line warns.
- Keyboard has the two rows and exact callback data; Approve/Reject unchanged.
- All existing invalid-binding tests still raise `invalid_card` (do not loosen).

`tests/test_release_card_view.py`
- `CardPresentation.from_json`: rejects unknown keys, wrong PR number in URL, non-GitHub URL,
  file_stats path set ≠ binding D→H set (extra, missing, duplicate), out-of-range ints, bad check
  names/details, patch present with wrong state, oversized patch.
- `parse_pr_summary` on a worker-style body, a body without headings, and junk input.
- `redact_patch` with a fake scanner flagging one section: that section replaced, others intact,
  count = 1; scanner raising → section redacted; header path not in `allowed_paths` → no path.
- `group_key`, `touches`, flags for each category, `find_contradictions` for each flag.
- `render_details`: contains every identifier; long path lists split into ≤ 5 messages each ≤ 4096
  UTF-16 with the "and K more paths" tail.
- `build_card_presentation` against a temp Git repo (like `test_git_evidence.py`) with a fake
  scanner: stats match, patch included and redacted; tiny `MAX_PATCH_BYTES` (monkeypatch) →
  `too_large`.

`tests/test_telegram_ingress.py`
- `release:i` and `release:v` from the owner in the owner's private chat call `card_views` with
  the right kind and card, then acknowledge; `record_decision` is never called.
- Non-owner sender, group chat, chat id ≠ owner, missing message, unknown message
  (`get_card_by_message` raises), `card_views=None`, and `release:x` all raise `IngressError` with
  no `card_views` call and no acknowledgement.
- Existing approve/reject tests unchanged and green.

`tests/test_protected_bridge_http.py`
- Card request with a valid presentation issues the card and stores the view row; invalid
  presentation → 400 and no Telegram call; legacy body still works; store failure still returns
  the receipt.

`tests/test_bridge_service.py`
- Details handler sends protected plain-text messages with previews disabled and every identifier.
- Diff handler: included → `sendDocument` with the stored (already redacted) bytes and filename;
  too_large and missing → fallback text with the PR URL; neither path changes card state or calls
  the ledger's decision methods.

`tests/test_git_evidence.py`
- `collect_delta_stats` on a temp repo with text and binary changes; `collect_delta_patch` returns
  the patch and `None` over the cap.

Integration (Postgres, skip without `TEST_PG_DSN`): `ReleaseCardViewStore` put/get round-trip
against a schema built from `schema.sql` + `bridge_schema.sql`, immutability trigger rejects
UPDATE/DELETE, as the existing `test_protected_bridge_http.py` integration tests do.

## Done when
- `uv run pytest packages/plugin-release-control -q` passes; `uv run ruff format --check .` and
  `uv run ruff check .` are clean.
- The new card renders in the order above, with identifiers only in Details.
- All existing binding, nonce, expiry, ownership and callback tests are green and unchanged in
  intent.
- Out of scope: no live Telegram/GitHub calls, no installation, no CLI for producing the card
  request, no changes to how bindings are created or verified.
