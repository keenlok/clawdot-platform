# Formatted HTML output (summary cards)

> **For agentic workers:** Implement only after explicit owner approval of this plan.

**Goal:** Let the bot answer with a clean, readable card instead of a wall of chat text when the
answer is structured: a trip expense summary, who owes whom, or a research result with its
sources. The card is fixed-template HTML, rendered to a PNG by `services/browser-renderer` and
sent as a Telegram photo, with a short text reply beside it.

**Status (2026-11-11):** Planned. Nothing built.

**Architecture:** The model never writes HTML or CSS. It fills in a small, typed card document
(title, stat tiles, table, bullets, callout, sources). Core (`chatcore.cards`) validates it and
renders it through one fixed template and CSS token set, escaping every string. A new `/card`
endpoint on the renderer turns that HTML into a PNG with JavaScript and all network access off. A
new optional `PhotoPlatform.send_photo` capability delivers it. A new `cards` plugin owns the
generic `send_card` tool and its skill. The expenses plugin adds ledger-backed cards whose numbers
come from the database, not the model.

**Tech stack and dependencies:** Existing Python, Pydantic, HTTPX, aiohttp, Playwright and
python-telegram-bot. No new Python package (templating uses `html.escape` and a small typed
builder, not Jinja). Possibly new font packages in the renderer image (see "Fonts"). No migration.

**Execution:** Claude plans. Execution follows the `AGENTS.md` execution table: on the owner's
computer with Codex CLI installed, Codex executes (at the owner's chosen `ARCHITECTURE.md` §7b
model) and reviews, then Claude does the final review. Without Codex, the session's model
executes and self-reviews, and the PR says so.

## Why

Chat text works for short answers and fails for structured ones:

- **No tables in Telegram.** The adapter turns model Markdown into native entities
  (`chatcore_telegram/formatting.py`); a Markdown table becomes `a | b | c` lines with a bold
  header. A six-person balance table wraps unreadably on a phone.
- **Long answers split.** Text over 4,096 UTF-16 units is split into several messages
  (`split_message`), so a trip summary arrives in pieces.
- **No hierarchy.** Headings become bold lines and blockquotes become plain paragraphs, so the
  one number that matters ("You owe 42.10 SGD") looks like everything else.
- **Numbers don't line up.** Proportional fonts and wrapping mean amounts can't be scanned down a
  column.

What exists and can be reused:

| Piece | Today | Reuse |
| --- | --- | --- |
| `services/browser-renderer` | Headless Chromium behind a guarded egress proxy; `POST /render {url}` returns page **text** only. Sandboxed (Chromium sandbox, seccomp profile, non-root, no-new-privileges), concurrency 2, browser recycled every 50 renders | Add a second endpoint that renders supplied HTML to PNG in a no-network, no-JS context |
| `chatcore.files.FilePlatform.send_file` | Telegram `send_document`, 20 MiB cap; used by `export_expenses` | Fallback delivery for a PDF in a later phase |
| `chatcore.files.VoicePlatform.send_voice` | Response attachment pattern (`writes=False`, sent inside the tool call before the text reply) | Same pattern for photos: a new `PhotoPlatform` |
| `chatcore_voice` | Plugin with tool, gate, permission helper, one-per-turn rule | Template for the `cards` plugin's layout and checks |
| Expenses `_balances` / `get_balances` | Exact balance computation shared by tools | Data source for ledger-backed cards |
| `chatcore_browse.RenderClient` | HTTP client for `/render` | Pattern only; plugins can't import browse, so the card client lives in core |

A separate image-generation effort may also need photo delivery. Whichever lands first adds
`PhotoPlatform`; the other reuses it rather than adding a second capability.

## Delivery options

| Option | How it looks | Pros | Cons |
| --- | --- | --- | --- |
| **(a) Richer Telegram text** | Native entities only: bold, italic, underline, strike, spoiler, code, `pre`, links, blockquote, expandable blockquote ([formatting options](https://core.telegram.org/bots/api#formatting-options)) | Copyable, searchable, accessible, no new service, works in bridge mode | No tables, colour, layout or alignment beyond monospace `pre`; 4,096-unit limit still splits long answers |
| **(b) Rendered image card** | Fixed-template HTML → PNG via the renderer → `sendPhoto` | Real tables, hierarchy, aligned numbers, one glanceable unit; renderer and sandbox already exist; content stays inside our infrastructure and Telegram | Not copyable or screen-reader friendly; links aren't clickable; renderer must be running; ~1–3 s latency; photo limits (10 MB, width + height ≤ 10,000, aspect ratio ≤ 20, caption ≤ 1,024) |
| **(b′) Rendered PDF** | Same HTML → `page.pdf()` → `sendDocument` | Multi-page, selectable text, printable | Needs a tap to open; heavier on phones; worse for a quick glance |
| **(c) Hosted page / link** | Bot serves an HTML page and sends a link (or a Telegram Mini App) | Interactive, any length, live data, clickable links | A public web endpoint, auth tokens in URLs, retention and deletion, TLS and hosting: a new attack surface for private ledger data. Biggest build and the riskiest |

**Recommendation: (b) image cards as the main path, with a small (a) baseline improvement, and
(c) rejected for now.**

- The card is the only option that fixes tables and hierarchy without exposing data on the web.
- The text reply always comes too, so nothing is lost when an image can't be read, copied or
  searched, or when the renderer is down. The card is an addition to the answer, never the only
  copy of it.
- (a) is cheap and helps every reply, including bridge mode and platforms without photos, so it
  ships first as Phase 1.
- (b′) PDF waits for a real need (long itineraries, printable trip reports).
- (c) stays in the backlog. It needs its own plan covering hosting, signed expiring links and
  deletion.

## Design

### Phase 1: Telegram text baseline (adapter only)

In `chatcore_telegram/formatting.py`, behaviour stays the same except:

- Markdown blockquotes become native `blockquote` entities instead of plain paragraphs.
- A Markdown table whose rendered rows all fit in 36 characters (readable on a phone without
  wrapping) becomes one `pre` block with columns padded and numeric columns (cells matching a
  number or amount pattern) right-aligned. Wider tables keep today's `a | b` rows.
- Entity offsets stay UTF-16, `split_message` rules are unchanged, and confirmation prompts stay
  literal.

Width is measured in East Asian display width (CJK counts 2), so Chinese names don't break the
alignment.

### Phase 2: core card rendering, renderer endpoint and photo delivery

**Card document (`chatcore.cards.models`).** A strict Pydantic model (`extra="forbid"`, every
string length-bounded, control characters other than `\n` rejected, Unicode bidi overrides and
isolates removed):

| Field / block | Shape | Limits |
| --- | --- | --- |
| `title` | text | 1–80 characters |
| `subtitle` | optional text | ≤ 120 |
| `stats` | 1–4 tiles: `label`, `value`, optional `note`, optional `tone` | label ≤ 24, value ≤ 18, note ≤ 48 |
| `heading` | text | ≤ 80 |
| `paragraph` | text, `\n` for line breaks | ≤ 600 |
| `bullets` | list of text | 1–8 items, ≤ 160 each |
| `table` | `columns` (header, `align: start \| end`), `rows` of cells, optional `total` row | 1–5 columns, header ≤ 24, ≤ 25 rows, cell ≤ 60 |
| `callout` | `tone` (`info`, `good`, `warning`, `critical`) and text | ≤ 240 |
| `sources` | list of `{title, url}` | ≤ 6; title ≤ 100; `url` must be http(s); only the hostname is shown, computed in code |

At most 10 blocks after the title and 4,000 characters of text in total. `tone` is an enum; the
model never chooses colours. The footer (bot name if configured, and the generation time in the
chat's time zone) is built in code.

**Renderer of the document (`chatcore.cards.html`).**

- One HTML template and one CSS block, both string constants in core. The model's values reach
  the page only through `escape(value)` (`html.escape(quote=True)`) into text nodes. No value is
  ever placed in an attribute, a `<style>` block, a URL or a class name; tones and alignments map
  through fixed dictionaries.
- The page carries `<meta http-equiv="Content-Security-Policy" content="default-src 'none';
  style-src 'unsafe-inline'; img-src data:">` and no `<script>`, `<link>`, `<img>`, `<iframe>` or
  web font.
- Output is a complete HTML string under 256 KiB.

**Renderer endpoint (`services/browser-renderer`).** `POST /card` with body
`{"html": str, "theme": "light" | "dark", "width": 400}`:

- Request cap 256 KiB for this route only; `/render` keeps its 4 KiB cap and behaviour.
- A fresh browser context with `java_script_enabled=False`, `offline=True`, `accept_downloads=False`,
  `service_workers="block"`, no permissions, `viewport={"width": 400, "height": 600}`,
  `device_scale_factor=2.5` (1,000 px output) and `color_scheme` set from `theme`.
- `context.route("**/*", abort)` for every request, so even a crafted page can't load anything;
  the page is loaded with `page.set_content(html)`. No proxy lease is registered, so anything
  that did reach the egress proxy would be refused.
- Screenshot `full_page=True`, PNG. Refuse with `too_large` if the page is taller than 4,000 CSS
  px (aspect ratio 10:1, inside Telegram's 20:1) or the PNG exceeds 5 MiB. Timeout 10 s.
- Shares the existing concurrency limit (2) and browser recycling; returns `busy` like `/render`.
- Response: `image/png` bytes on success; JSON `{"status": ...}` with a fixed status word
  otherwise. Logs status, duration, HTML bytes and PNG bytes only, never content.
- The endpoint stays on the internal network only, like `/render`.

**Client (`chatcore.cards.client.CardRenderClient`).** HTTPX, no redirects, no proxy inheritance,
userinfo-free base URL (same checks as `RenderClient`), streamed response capped at 5 MiB, PNG
magic bytes checked. `render(doc, theme) -> bytes` raises `CardError(code)` with codes `busy`,
`too_large`, `timeout`, `unavailable`.

**Photo capability (`chatcore.files.PhotoPlatform`).**
`send_photo(chat, filename, data, *, caption=None, thread_id=None) -> MessageRef`. The direct
Telegram adapter implements it with `bot.send_photo`, checking PNG/JPEG magic bytes, ≤ 10 MB,
width + height ≤ 10,000 and ratio ≤ 20 from the PNG header, and a plain caption ≤ 1,024 UTF-16
units. The bridge adapter and console don't implement it, so the gates hide card tools there.

### Design spec the template must follow

Distilled from the `dataviz` and `artifact-design` guidance in this environment and published
accessibility guidance. These rules are fixed in the template and CSS tokens, not left to the
model.

**Layout and type (mobile-first; a Telegram photo is read on a phone at roughly full width).**

| Token | Value | Why |
| --- | --- | --- |
| Canvas | 400 CSS px wide at 2.5× (1,000 px PNG) | Sharp after Telegram's recompression on 2–3× phone screens |
| Padding | 20 px; block gap 16 px; spacing on a 4 px scale | Consistent rhythm |
| Font stack | `"Noto Sans", "Noto Sans CJK SC", "Noto Color Emoji", system-ui, sans-serif` | Latin, Chinese and emoji from installed fonts only |
| Body | 16 px / 1.5, weight 400 | ~50–60 characters per line at this width, inside the 45–75 comfortable range |
| Title | 22 px / 1.25, weight 650 | One clear entry point |
| Heading | 17 px / 1.3, weight 600 | Two type levels below the title, no more |
| Meta (subtitle, notes, footer, table header) | 13 px / 1.4, secondary ink | Recessive but still ≥ 4.5:1 |
| Stat value | 26 px, weight 650, `tabular-nums` | The headline number reads first |

**Colour.** Neutral surfaces and ink from the dataviz reference palette, one accent, and
reserved status colours:

| Role | Light | Dark |
| --- | --- | --- |
| Surface | `#fcfcfb` | `#1a1a19` |
| Raised (stat tile, table header) | a 4% ink tint of the surface | a 6% ink tint |
| Primary ink | `#0b0b0b` | `#f0efec` |
| Secondary ink | `#52514e` | `#c3c2b7` |
| Rule (table dividers) | 12% ink | 16% ink |
| Accent (title rule, source bullets) | `#2a78d6` | `#3987e5` |
| Status good / warning / critical / info | chosen at implementation and run through the dataviz validator | same |

- Text is always primary or secondary ink, never a status or accent colour. A tone shows as a
  coloured left bar or tile edge **plus** an icon and a word ("Settled", "Owes", "Check"), never
  colour alone.
- Every text/surface pair is ≥ 4.5:1 (WCAG 2.2 SC 1.4.3); rules and tone bars are ≥ 3:1 where they
  carry meaning (SC 1.4.11). A unit test computes these ratios from the tokens, so a token change
  that breaks contrast fails CI.
- Theme is an operator setting (`CARDS_THEME`, default `light`). A photo can't follow each
  viewer's theme, and light reads well in both Telegram themes.

**Tables.**

- Text columns left-aligned, numeric columns right-aligned with `font-variant-numeric:
  tabular-nums` so digits line up.
- Units in the header ("Paid (SGD)"), not repeated in every cell. Negative amounts use a real
  minus sign and a word in an adjacent column ("owes"), not red alone.
- Thin horizontal rules only: no vertical borders, no zebra stripes. Header row in meta style on
  the raised surface. Optional total row in bold above a stronger rule.
- Cells wrap; nothing is truncated with an ellipsis. Five columns is the maximum at this width.

**Content rules (in the skill instructions).** Lead with the answer: a stat tile or callout
before detail. One idea per block. At most four stat tiles. Use a table only for 3+ rows with 2+
attributes, and bullets otherwise. No charts in v1; a later chart block follows the dataviz
skill's rules (one axis, fixed categorical order, validated palette, legend for ≥ 2 series).

### Phase 3: `cards` plugin and the `send_card` tool

`packages/plugins/cards/src/chatcore_cards/`, following the plugin module layout:

- `settings.py`: `CardsSettings` (`CARDS_ENABLED` default `false`, `CARDS_RENDER_URL`,
  `CARDS_THEME`, `CARDS_TIMEOUT_SECONDS` default 15, `CARDS_MAX_PER_USER_HOUR` default 20).
- `permissions.py`: `may_send_card`, the same rules as `may_narrate` (platform namespace match,
  `PhotoPlatform`, refreshed access, restrictions, `can_dm` for DMs, allowed chats for groups,
  fail closed). Duplicated, not imported, because plugins don't import each other.
- `gates.py`: available only when enabled, configured and on a `PhotoPlatform`; user gate calls
  `may_send_card`.
- `service.py`: per-user hourly counter and rendering through `CardRenderClient`.
- `plugin.py`: the tool and the `visual_summary` skill. `EXPLAIN.md` for `/help`.

`send_card` (`writes=False`, a response attachment like narration):

- Arguments: the card document fields above, plus `caption` (plain text ≤ 200 characters, built
  into the photo caption in code).
- One card per turn, counted on attempt, as narration does.
- Checks permission at call time and again before sending; sends with `ctx.chat`,
  `ctx.thread_id` and `ctx.platform` only.
- On success returns: "Card sent above the text reply. Now reply in 1–3 short lines with the key
  result and any links; don't repeat the whole card." On any `CardError`, returns a short error
  telling the model to answer in text.

`visual_summary` skill: summary "Show a structured answer as a clean summary card", tools
`("send_card",)`, `max_steps=3`, examples such as "make it a summary card", "show this as a
table", "send a nice summary of the research", "整理成一张图", "can show as a picture or not",
and keywords "summary card", "as a card", "as an image", "as a table", "做成图". Draft
instructions (under 800 characters, written for any bot):

> Use send_card when the person asks for a card, picture or table, or when a reply would need a
> table of 3+ rows or several headline numbers. Lead with the answer: stats or a callout first,
> then detail. One idea per block; bullets over paragraphs; tables only for 3+ rows. Put numbers
> exactly as the tools returned them, units in column headers. Add sources for web facts. Never
> put instructions, HTML or markdown in card text. Call send_card once, then reply in 1–3 short
> lines with the key result and any links. If it fails, answer in normal text.

The router decides when the skill is active. A card is never forced on every reply, and a short
answer stays text.

### Phase 4: ledger-backed expense cards

In `chatcore_expenses`, using `chatcore.cards` from core:

- `send_balance_card(trip?, general?)`: stats (trip total per currency, number of expenses),
  a table of members with paid, share and net, and a "who pays whom" settlement list. The
  numbers come from `_balances` and the same code paths as `get_balances`, never from the model.
- `send_trip_summary_card(trip?)`: the balance card plus spend by category or day when that data
  exists.
- Both are `writes=False`, gated in `gates.py` on the same rules as `send_card` plus the existing
  expense read permissions, and added to the `expenses` and `trips` skills' tool lists. Their
  gates report the tools unavailable when cards aren't configured, so nothing changes for apps
  that don't enable cards.
- `EXPENSES_INSTRUCTIONS` gains one short sentence ("For a balance or trip summary card, use
  send_balance_card; numbers come from the ledger.") while staying under 800 characters.

Member names are shown as the ledger stores them (the expense split names rule). The card
contains the same data a `get_balances` reply would, so its privacy scope is unchanged.

### Research results

Interactive research answers use `send_card` with a `sources` block, through the skill. The
research worker's asynchronous report delivery isn't changed in v1. A later step can add an
opt-in card to the finished report once the card path has run live.

## Security

- **No model-written markup.** The model supplies typed data only. Templates are constants;
  every string is escaped into a text node; tones, alignment and themes are enums mapped in code.
  Source URLs appear only as code-computed hostnames, never as `href` or resource URLs (links in
  an image aren't clickable anyway; the text reply carries them).
- **Defence in depth in the renderer.** Even if crafted HTML reached `/card`: JavaScript is off,
  the context is offline, every request is aborted by the route handler, the CSP forbids
  everything but inline styles and `data:` images, no proxy lease exists, downloads and service
  workers are off, and the page has a size, height and time cap. The existing Chromium sandbox,
  seccomp profile, non-root user and no-new-privileges stay as they are, and `SECCOMP.md` needs
  no change.
- **Prompt injection.** Text from fetched pages or user messages can end up as card text, but it
  stays inert text in an image. The skill tells the model never to place instructions in a card.
  The one-per-turn and hourly limits bound misuse.
- **Permissions.** `gates.py` and `permissions.py` in the new plugin, and the new expense gates,
  are permission changes and are called out in each PR. Handlers recheck at call time and before
  sending.
- **Privacy.** Rendering happens inside our renderer container; no third party sees card
  content. The PNG is held in memory, uploaded to Telegram like any reply, and never stored.
  Logs carry status, sizes and durations only. Add a line to `ARCHITECTURE.md` §6a: card text
  goes to the local renderer and the image to Telegram; nothing new leaves our infrastructure.

## Fonts

The Playwright base image ships some fonts. Qualification renders a fixture card with Latin,
Simplified and Traditional Chinese, Tamil, Malay, emoji and currency symbols, and compares it with
a reference. If glyphs are missing (tofu), add `fonts-noto-core`, `fonts-noto-cjk` and
`fonts-noto-color-emoji` from the base image's Ubuntu archive to the renderer `Dockerfile`. That's a
maintenance-reviewed file and is called out in the PR. No web fonts, ever.

## Owned files

| Phase | Files |
| --- | --- |
| 1 | `packages/adapters/telegram/src/chatcore_telegram/formatting.py` and its tests |
| 2 | New `packages/core/src/chatcore/cards/` (`__init__.py`, `models.py`, `html.py`, `client.py`); `packages/core/src/chatcore/files.py` (`PhotoPlatform`); `packages/core/src/chatcore/testing.py` (fake `send_photo`); `packages/adapters/telegram/src/chatcore_telegram/__init__.py` (`send_photo`); `services/browser-renderer/src/chatcore_browser_renderer/{server,engine,settings}.py` and tests; possibly `services/browser-renderer/Dockerfile` (fonts); core tests |
| 3 | New `packages/plugins/cards/**` (`pyproject.toml`, `src/chatcore_cards/{__init__,plugin,gates,permissions,settings,service}.py`, `EXPLAIN.md`, tests); root `pyproject.toml` and `uv.lock` for the new workspace member; scaffold `.env.example` (disabled by default) |
| 4 | `packages/plugins/expenses/src/chatcore_expenses/{plugin,gates}.py` and tests |
| All | `CHANGELOG.md` (Unreleased), `ARCHITECTURE.md` §6a privacy line, `docs/plans/README.md` row, wiki page for the plugin |

Not touched: `/render` behaviour, the egress proxy, `seccomp.json`, other plugins, migrations.

## Tests

- **Phase 1.** Blockquote entity offsets (with emoji and CJK); narrow tables become one aligned
  `pre` with numbers right-aligned; wide tables unchanged; CJK display width; splitting across a
  `pre` block; confirmations still literal; Bot API payloads through PTB `FakeRequest` for send
  and edit.
- **Card model.** Every limit, unknown fields rejected, control characters rejected, bidi
  controls stripped, non-http source URLs rejected, total-text cap.
- **Escaping corpus.** `<script>`, `</style>`, `"><img src=x onerror=…>`, `javascript:`, CSS
  `url(…)` and `@import`, HTML entities, `{{ }}`, and RTL overrides in every field. Assert the
  output has no tag or attribute beyond the fixed template's set (parse the output with
  `html.parser` and compare against an allowlist).
- **Design tokens.** Contrast ratio of every text/surface and meaningful non-text pair in both
  themes meets 4.5:1 or 3:1; status colours pass the dataviz validator (recorded in the PR).
- **Renderer `/card`** (unit tests with a fake browser, plus `-m browser` integration tests):
  JS disabled (a `<script>` that would change the title doesn't run), every network request
  aborted (a crafted `<img src=https://…>` and CSS `url()` make zero proxy connections), oversize
  request refused, height cap gives `too_large`, timeout, busy, PNG returned, `/render` unchanged.
- **Client.** Redirects not followed, size cap while streaming, non-PNG body refused, error codes
  mapped, no response text in exception messages.
- **Telegram `send_photo`.** Magic bytes, 10 MB, dimension and ratio checks from the PNG header,
  caption length, thread and reply metadata, through `FakeRequest`.
- **`cards` plugin.** Gate hidden when disabled, unconfigured or not a `PhotoPlatform`; user gate
  and call-time permission checks (DM, group, restricted user, namespace mismatch); one per turn
  including failures; hourly cap; permission rechecked before sending; errors leave the model a
  text fallback.
- **Expense cards.** Numbers equal `get_balances` for the same ledger (one shared fixture across
  currencies and settle modes); member names as stored; trip resolution errors match `_export`;
  gate hides tools when cards are off.
- **Skills and prompts.** Skill catalog test includes `visual_summary` with instructions ≤ 800
  characters; `EXPENSES_INSTRUCTIONS` still ≤ 800. The agent request snapshot
  (`packages/core/tests/snapshots/agent_requests_off.json`) must not change in Phases 1–2; if a
  later phase changes it, the diff is reviewed in the PR. `routing_set.jsonl` lives downstream, so
  downstream apps add card phrasings there when they enable the plugin.
- **End to end.** A contract test like `tests/test_voice_delivery_contract.py`: `send_card` at
  its content limits through the real Telegram adapter (mocked bot) produces a photo within
  Telegram's limits.

## Checks

```sh
uv sync --locked --all-packages
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check .
uv run pytest -q -m "not browser and not sandbox_integration"
uv run pytest -q -m browser services/browser-renderer   # where Chromium is available
```

Name any check that couldn't run (browser integration, Postgres) in the PR.

## Qualification before a downstream app enables cards

1. Render the fixture cards (balance, trip summary, research with sources) in both themes and
   attach them to the PR for an owner visual review on a real phone, in Telegram light and dark.
2. Confirm glyph coverage (see "Fonts").
3. Measure render latency (target p95 under 3 s on the deploy host) and PNG size (target under
   600 KB for a typical card).
4. Send one card to a test chat and confirm Telegram's recompression keeps 13 px meta text
   legible.

## Done when

- Phase 1: narrow tables and blockquotes render natively in Telegram, with no other formatting
  change.
- Phase 2: `chatcore.cards` renders a validated document to escaped HTML; `/card` returns a PNG
  with JS and network off; `PhotoPlatform.send_photo` works on direct Telegram.
- Phase 3: `send_card` and `visual_summary` ship disabled by default, gated, one per turn, with
  a text fallback.
- Phase 4: balance and trip-summary cards match `get_balances` exactly.
- Every phase: tests and checks above pass, `CHANGELOG.md` and the plans index row are updated,
  and qualification evidence is attached before any downstream enablement.

## Out of scope (recorded for later)

- Hosted pages, signed links or a Telegram Mini App (option c).
- PDF output (option b′) for long or printable reports.
- Charts (would follow the dataviz rules above).
- Cards in the research worker's asynchronous report delivery.
- Model-chosen colours, layouts or free-form HTML, ever.

## Open questions for the owner

1. Is image-plus-short-text the right contract, or should some replies be the card alone?
2. Should cards default to the light theme, or follow a per-chat setting later?
3. Should the bot offer a card on its own for long structured answers, or only when asked?
   The draft skill does both (asked, or 3+ table rows).
4. Phase order: ship Phase 1 (text baseline) on its own first, or with Phase 2?
