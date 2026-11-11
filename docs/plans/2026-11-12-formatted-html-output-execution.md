# Formatted HTML output: execution plan

Codex execution plan for the [formatted HTML output plan](2026-11-11-formatted-html-output.md),
approved by the owner on 2026-11-12. Codex executes with `gpt-6-luna`, Codex reviews with
`gpt-6.1-sol`, and Claude does the final review (`AGENTS.md`, `ARCHITECTURE.md` §7b).

The work runs in two Codex runs on the same branch. **Run A** builds the framework pieces
(design-plan Phases 1–2). **Run B** builds the plugins on top (Phases 3–4) and starts only after
Run A has passed review. Each run's section says what it owns; everything else is read-only.

Read first: `AGENTS.md` (the whole file) and the design plan. Where this file and the design plan
disagree, this file wins.

## Environment (both runs)

- `export UV_CACHE_DIR="$PWD/.uv-cache"` before any `uv` command.
- Lint: `uv run ruff check packages services apps tests .github/scripts`, and
  `uv run ruff format <only the files you changed>`.
- Tests: `uv run pytest -q -m "not browser and not sandbox_integration"`. Postgres-backed tests
  skip without a database; that's expected.
- Browser tests (`-m browser`) need Chromium from Playwright. Run them with
  `uv run pytest -q -m browser services/browser-renderer` if Chromium is installed; if it isn't,
  say so in your report rather than installing it.
- No new third-party Python dependency. No migration. No real network calls in tests.
- This repository is public: no real names, handles, chat IDs or chat content in code, tests or
  fixtures. Use placeholders such as `Alex`, `Sam`, `陈明`.

## Deviation from the design plan

The design plan says the LLM factory clamps the output ceiling to the provider's own maximum.
There's no provider-maximum registry to clamp against, so the operator sets
`max_tokens_ceiling` at or below the configured model's limit. Nothing else changes.

---

## Run A: Telegram text baseline, core cards, renderer `/card`, albums, output budget

Read these files first; they define the contracts and patterns you build against:

- `packages/adapters/telegram/src/chatcore_telegram/formatting.py` and its tests
- `packages/adapters/telegram/src/chatcore_telegram/__init__.py` (`send_photo`, `_send_with_retry`,
  `MAX_PHOTO_BYTES`) and `packages/adapters/telegram/tests/test_telegram.py`
- `packages/core/src/chatcore/files.py`, `packages/core/src/chatcore/testing.py` (`FakePlatform`)
- `packages/core/src/chatcore/skills.py` (`Skill`, `_validate`, `_clamp`),
  `packages/core/src/chatcore/skill_turn.py` (`_capping_skills`, `step_cap`),
  `packages/core/src/chatcore/agent.py` (`AgentSettings`, every `max_tokens` use)
- `services/browser-renderer/src/chatcore_browser_renderer/{server,engine,settings}.py` and tests
- `packages/plugins/browse/src/chatcore_browse/client.py` (`RenderClient`, pattern only)

### You own (Run A)

- `packages/adapters/telegram/src/chatcore_telegram/formatting.py`, `__init__.py`, and their tests.
- New `packages/core/src/chatcore/cards/` package: `__init__.py`, `models.py`, `html.py`,
  `client.py`, `delivery.py`, `errors.py`.
- `packages/core/src/chatcore/files.py`: **add** `PhotoAlbumPlatform` only.
- `packages/core/src/chatcore/testing.py`: **add** `FakePlatform.send_photos` only.
- `packages/core/src/chatcore/skills.py`, `skill_turn.py`, `agent.py`: only the output-budget
  change in §A4.
- New core tests under `packages/core/tests/` (`test_cards_models.py`, `test_cards_html.py`,
  `test_cards_client.py`, `test_cards_delivery.py`, `test_output_budget.py`).
- `services/browser-renderer/src/chatcore_browser_renderer/{server,engine,settings}.py` and new
  tests in `services/browser-renderer/tests/`.
- `CHANGELOG.md`: entries under `## [Unreleased]`.

You may **not** edit: the `/render` endpoint's behaviour, the egress proxy, `seccomp.json`,
`SECCOMP.md`, the Dockerfile, `bridge.py`, any plugin, the request snapshot
(`packages/core/tests/snapshots/`), the root `pyproject.toml`, `.github/`, migrations. If one must
change, stop and say why in your report.

### A1. Telegram text baseline (`formatting.py`)

- `blockquote_open`/`blockquote_close` produce a native `blockquote` `MessageEntity` covering the
  quote's text (nested quotes: one entity for the outermost; Telegram can't nest them). Keep
  today's surrounding newlines.
- Tables: collect a table's cells first. Compute each column's display width with
  `unicodedata.east_asian_width` (`W` and `F` count 2, everything else 1). If every rendered row
  (cells joined by two spaces) is at most `_PRE_TABLE_WIDTH = 36` display columns, emit the whole
  table as one `pre` entity: header row, a row of `─` under it, then the body. Pad cells to their
  column width; right-align a column when every non-empty body cell matches
  `^[-+−]?[$€£¥]?\s?\d[\d,]*(\.\d+)?\s?%?(\s?[A-Z]{3})?$` (left-align otherwise). Inline styles
  inside a `pre` table are dropped (text only). Otherwise keep today's `a | b` rendering exactly.
- `split_message`, link handling, code handling and confirmation literals are unchanged.

### A2. Card model, HTML and errors (`chatcore.cards`)

`errors.py`: `class CardError(Exception)` with a `code` attribute, one of `"invalid"`, `"busy"`,
`"too_large"`, `"timeout"`, `"unavailable"`. Messages never include card text or response bodies.

`models.py`, using Pydantic v2 with `model_config = ConfigDict(extra="forbid", strict=True)` on
every model:

| Model | Fields |
| --- | --- |
| `StatTile` | `label` 1–24, `value` 1–18, `note` ≤ 48 optional, `tone` optional `Tone` |
| `StatsBlock` | `type: Literal["stats"]`, `tiles` 1–4 |
| `HeadingBlock` | `type: Literal["heading"]`, `text` 1–80 |
| `ParagraphBlock` | `type: Literal["paragraph"]`, `text` 1–1,000 (may contain `\n`) |
| `BulletsBlock` | `type: Literal["bullets"]`, `items` 1–12 of 1–200 |
| `TableColumn` | `header` 1–24, `align: Literal["start", "end"] = "start"` |
| `TableBlock` | `type: Literal["table"]`, `columns` 1–5, `rows` 1–40 (each exactly `len(columns)` cells of 0–60), `total` optional row of the same length |
| `CalloutBlock` | `type: Literal["callout"]`, `tone: Tone`, `text` 1–300 |
| `Source` | `title` 1–100, `url` 1–2,000 (http or https with a hostname, no userinfo) |
| `SourcesBlock` | `type: Literal["sources"]`, `items` 1–8 |
| `Card` | `title` 1–80, `subtitle` ≤ 120 optional, `blocks` 0–16 (discriminated on `type`) |

- `Tone = Literal["info", "good", "warning", "critical"]`.
- Every string: strip surrounding whitespace, reject control characters except `\n` (and `\n`
  only in `ParagraphBlock.text`), and remove bidi controls U+202A–U+202E and U+2066–U+2069 before
  length checks.
- `validate_card(data: object, *, max_text_chars: int = 8000) -> Card` raises
  `CardError("invalid")` on any validation error, including total text (sum of every string's
  length, URLs excluded) above `max_text_chars`. The error's `detail` attribute holds a short,
  safe description of the first failing field path and rule (no values) for the tool to return.
- `card_json_schema() -> dict`: a hand-written JSON Schema (no `$ref`/`$defs`) matching the models,
  with `additionalProperties: false` everywhere and a one-line `description` on every property,
  for use as a tool's parameters.
- `Source.hostname` (property): the IDNA-decoded hostname without `www.`.

`html.py`:

- `THEMES: dict[str, dict[str, str]]` for `"light"` and `"dark"` with the design-plan tokens:
  surface, raised, ink, ink-2, rule, rule-strong, accent, and `good`, `warning`, `critical`,
  `info` tone colours. Choose tone colours that give ≥ 3:1 against both surface and raised in that
  theme; text never uses tone colours.
- `contrast_ratio(a: str, b: str) -> float` (WCAG 2.x relative luminance, `#rrggbb`).
- `render_card_html(card: Card, *, theme: str = "light", footer: str | None = None) -> str`.
  One template constant and one CSS constant. Every model value goes through
  `html.escape(value, quote=True)` into a text node; `\n` in paragraphs becomes `<br>`. Nothing
  from the model goes into an attribute, class, style or URL; tones and alignments map through
  fixed dicts to fixed class names. Each block is a `<section data-block>`; each table body row is
  `<tr data-row>`. Sources show the title and `hostname` only, never an `href`.
- The page starts with `<!doctype html>`, has `<meta charset="utf-8">`, the CSP meta
  `default-src 'none'; style-src 'unsafe-inline'; img-src data:`, and no `<script>`, `<link>`,
  `<img>`, `<iframe>`, `<object>`, `<form>` or `@import`/`url(`.
- CSS rules: canvas 400 px, padding 20 px, block gap 16 px; font stack
  `"Noto Sans", "Noto Sans CJK SC", "Noto Color Emoji", system-ui, sans-serif`; body 16 px/1.5;
  title 22 px/1.25 weight 650 with a 3 px accent rule beneath; heading 17 px/1.3 weight 600; meta
  13 px/1.4 in ink-2; stat value 26 px weight 650; `font-variant-numeric: tabular-nums` on stat
  values and `end`-aligned cells; tables with horizontal rules only, header row in meta style on
  `raised`, total row bold above `rule-strong`; callouts with a 4 px tone bar, an icon character
  (`ℹ`, `✓`, `!`, `✕`) and a tone word (`Note`, `Good`, `Check`, `Important`) in ink; long words
  wrap (`overflow-wrap: anywhere`), nothing is truncated.
- Output over 256 KiB raises `CardError("too_large")`.

### A3. Renderer `/card` endpoint

Settings (`RendererSettings`): `card_max_html_bytes` (`CARD_MAX_HTML_BYTES`, default 262,144),
`card_timeout_s` (`CARD_TIMEOUT_S`, default 15, ≤ 60), `card_max_pages` (`CARD_MAX_PAGES`,
default 4, 1–10), `card_page_css_px` (`CARD_PAGE_CSS_PX`, default 3,600), `card_max_png_bytes`
(`CARD_MAX_PNG_BYTES`, default 5 MiB).

Server:

- Raise the app's `client_max_size` to `card_max_html_bytes + 1024`. Keep `/render`'s 4,096-byte
  limit by checking both `content_length` and the length of the body actually read; behaviour
  and responses of `/render` are otherwise unchanged (existing tests must pass untouched).
- `POST /card`, JSON body with exactly the keys `{"html", "theme", "width"}`; `html` a string
  within the byte cap, `theme` in `{"light", "dark"}`, `width == 400`. Anything else →
  `{"status": "invalid"}` with HTTP 200, like `/render`'s `blocked_url`.
- Response JSON: `{"status": "ok", "pages": [<base64 PNG>, ...]}` or `{"status": <word>}` with
  `busy`, `too_large`, `timeout`, `error`, `invalid`.
- Log one line: `card status=%s duration_ms=%d html_bytes=%d pages=%d png_bytes=%d`. Never log HTML.

Engine (`RendererEngine.render_card(html, theme) -> CardRenderResult(status, pages)`):

- Shares the existing concurrency counter, busy result, browser start/recycle and task tracking
  with `render`.
- New context per card: `java_script_enabled=False`, `offline=True`, `accept_downloads=False`,
  `service_workers="block"`, `permissions=[]`, `viewport={"width": 400, "height": 600}`,
  `device_scale_factor=2.5`, `color_scheme=theme`, no proxy option. `context.route("**/*", abort)`
  aborts every request. Load with `page.set_content(html, wait_until="load")`.
- Pages: read the page height and the boxes of `[data-block]` and `tr[data-row]` with
  `locator.bounding_box()`. Cut greedily so each page is at most `card_page_css_px` tall,
  cutting at the last block or row boundary that fits; if one row is taller than a page, cut at
  the page height. If boxes can't be read, cut at fixed `card_page_css_px` heights. More than
  `card_max_pages` pages → `too_large`. Screenshot each page with
  `page.screenshot(type="png", clip={x: 0, y: top, width: 400, height: h}, full_page=True)`.
  Any PNG over `card_max_png_bytes` → `too_large`.
- Whole operation inside `asyncio.timeout(card_timeout_s)` → `timeout`. Close the context in
  `finally`. Exceptions → `error` and mark the browser for recycling, like `_render`.

### A4. Skill-scoped output budget (core)

- `Skill` gains `max_output_tokens: int | None = None` (comment: raises the turn's per-step output
  limit while active, clamped to `AgentSettings.max_tokens_ceiling`; never lowers it).
  `_validate` rejects values below 1.
- `AgentSettings` gains `max_tokens_ceiling: int = 12288`.
- `SkillTurn` gains `output_tokens(default: int, ceiling: int) -> int`: the maximum of `default`
  and the `max_output_tokens` of every active or loaded skill that is offered (same membership as
  `_capping_skills`), clamped to `max(default, ceiling)`.
- In `agent.py`, every place that passes `self.settings.max_tokens` for a main-loop step uses
  `skill_turn.output_tokens(self.settings.max_tokens, self.settings.max_tokens_ceiling)` when
  skills mode is on, and `self.settings.max_tokens` otherwise. Summaries and the `max_tokens=8`
  probe are unchanged.
- No existing skill sets the field, so `agent_requests_off.json` and every existing test must
  pass unchanged.

### A5. Client, albums and delivery

`client.py`: `CardRenderClient(base_url, *, timeout_s=20, transport=None)` with the same base-URL
checks as `RenderClient` (no userinfo; http or https). `async render(card: Card, *, theme,
footer=None) -> list[bytes]` builds the HTML with `render_card_html`, posts to `/card` with
`follow_redirects=False` and `trust_env=False`, streams the response with a 28 MiB cap, maps
statuses to `CardError` codes (`invalid`/`error` → `unavailable`), base64-decodes each page with
`validate=True`, and requires the PNG signature. Connection errors and timeouts →
`CardError("unavailable")` / `CardError("timeout")`.

`files.py`: add, beside `PhotoPlatform`:

```python
@runtime_checkable
class PhotoAlbumPlatform(Protocol):
    """Optional capability for sending 2–10 images as one album."""

    async def send_photos(
        self,
        chat: ChatRef,
        photos: Sequence[tuple[str, bytes]],
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef: ...
```

Telegram (`__init__.py`):

- A helper `_image_size(data) -> tuple[int, int] | None` reads width and height from a PNG IHDR
  chunk or a JPEG SOF0/SOF2 marker. `send_photo` additionally raises `InvalidPhoto` when the size
  can't be read, when width + height > 10,000, or when the larger side divided by the smaller
  exceeds 20. Existing `send_photo` tests must still pass (adjust their fixture bytes to real
  minimal PNG/JPEG headers only if they lack readable dimensions, and say so).
- `send_photos`: 2–10 photos, each checked exactly like `send_photo`; caption checked like
  `send_photo`; sent with `bot.send_media_group(chat_id=..., media=[InputMediaPhoto(...)],
  message_thread_id=...)` through `_send_with_retry`, caption on the first item only. Returns the
  first message's `MessageRef`. `TelegramPlatform` adds `PhotoAlbumPlatform` to its bases.
- The bridge client gets neither method.

`FakePlatform.send_photos` records `(chat, photos, caption, thread_id)` and returns a `MessageRef`.

`delivery.py`: `async send_card_pages(platform, chat, pages, *, caption, thread_id) -> None`:
one page → `send_photo("card.png", ...)` if `PhotoPlatform`, else `send_file("card.png", ...)`;
several pages → `send_photos([("card-1.png", p1), ...])` if `PhotoAlbumPlatform`, else one
`send_photo` per page (caption on the first) if `PhotoPlatform`, else one `send_file` per page.
No platform capability → `CardError("unavailable")`. `can_deliver_cards(platform) -> bool`.
Never falls back to documents after a photo failure.

### Run A tests

- **Formatting:** blockquote entity offsets with emoji and CJK; narrow table → one `pre` entity
  with numeric columns right-aligned and CJK width respected; a 37-column table keeps the old
  format; inline styles in a `pre` table are dropped; splitting a message that contains a `pre`
  table; confirmations still literal; send and edit payloads through PTB's `FakeRequest` as the
  existing tests do.
- **Models:** every limit at the boundary and one past it; unknown keys; wrong types (strict);
  control characters; bidi controls removed; table row length mismatch; non-http, hostless or
  userinfo URLs; total-text cap; `card_json_schema()` has no `$ref` and every object has
  `additionalProperties: false`; a document valid under the schema validates under the models
  (use a few fixtures).
- **HTML:** an injection corpus (`<script>alert(1)</script>`, `</style><b>x`, `"><img src=x
  onerror=alert(1)>`, `javascript:alert(1)`, `url(https://example.com/x)`, `@import`, `&amp;`,
  `{{ x }}`, RTL override) in every field; parse the output with `html.parser.HTMLParser` and
  assert the set of tags and attributes is a subset of the template's fixed allowlist and that no
  attribute value contains model text; CSP meta present; no `url(` or `@import` anywhere.
- **Tokens:** `contrast_ratio` against known pairs (black/white is 21); in both themes every text
  token on surface and raised is ≥ 4.5, and rule-strong and tone colours are ≥ 3 on surface.
- **Renderer unit tests** (fake browser/page objects as the existing tests use): request
  validation (keys, theme, width, size), `/render` 4 KiB limit still enforced, status mapping,
  pagination cut points from given boxes (block boundaries, row boundaries, oversize row, fallback
  without boxes, page limit), PNG cap, timeout, busy shared with `/render`, context options
  (JS off, offline, scale 2.5) and route abort registered.
- **Renderer browser tests** (`@pytest.mark.browser`): a real card renders to PNGs whose decoded
  width is 1,000 px and height ≤ 9,000 px; a `<script>` that would change the document title
  doesn't run; a crafted `<img src="https://example.invalid/x">` and CSS `url()` cause no
  request to complete; a long table produces 2+ pages cut between rows.
- **Client:** `httpx.MockTransport` for ok, each status word, non-PNG page, bad base64, oversize
  stream, redirect not followed, connection error, timeout; no response text in exception
  messages.
- **Telegram photos:** dimension checks (PNG and JPEG, sum > 10,000, ratio > 20, unreadable
  header); album of 2 and 10 accepted, 1 and 11 rejected; caption only on the first item; thread
  id passed; `isinstance(platform, PhotoAlbumPlatform)`.
- **Delivery:** each fallback branch with fake platforms; no document fallback after a photo
  error.
- **Output budget:** no skill set → requests use `max_tokens` exactly (snapshot unchanged); an
  active offered skill with `max_output_tokens=12288` raises it; ceiling clamps it; an inactive
  or not-offered skill doesn't; skills mode off ignores it; `_validate` rejects 0.

### Run A done when

- Lint clean; every changed file passes `ruff format --check`.
- `uv run pytest -q -m "not browser and not sandbox_integration"` passes, with all existing tests
  unchanged (especially `agent_requests_off.json`, the formatting tests other than the two
  behaviours above, and the renderer `/render` tests).
- Browser tests pass, or the report says Chromium wasn't available.
- `CHANGELOG.md` `## [Unreleased]`: `### Added` for `chatcore.cards`, the renderer `/card`
  endpoint and its `CARD_*` settings, `PhotoAlbumPlatform` and Telegram `send_photos`,
  `Skill.max_output_tokens` and `AgentSettings.max_tokens_ceiling`; `### Changed` for native
  blockquotes, aligned narrow tables, and the stricter `send_photo` dimension check.
- `git status` shows only files Run A owns.

---

## Run B: `cards` plugin and ledger-backed expense cards

Read these first: the whole `packages/plugins/image/` package and
`tests/test_image_delivery_contract.py` (**copy their structure closely**), Run A's
`packages/core/src/chatcore/cards/`, `packages/core/src/chatcore/explainers.py`
(`BUILTIN_PLUGINS`), `.github/scripts/generate_explainers.py`, and in
`packages/plugins/expenses/src/chatcore_expenses/`: `plugin.py` (`ExpensesPlugin.__init__`,
`skills()`, `_balances`, `_balance_text`, `_export`, `_chat`, `ledger_balances`) and `gates.py`.

### You own (Run B)

- New package `packages/plugins/cards/` (all files): `pyproject.toml`,
  `src/chatcore_cards/{__init__,plugin,gates,permissions,settings,service}.py`,
  `src/chatcore_cards/EXPLAIN.md`, `tests/test_plugin.py`, `tests/test_service.py`.
- `packages/plugins/expenses/src/chatcore_expenses/{plugin,gates}.py`, a new
  `packages/plugins/expenses/src/chatcore_expenses/cards.py`, and new tests
  `packages/plugins/expenses/tests/test_cards.py`.
- `tests/test_cards_delivery_contract.py` (new).
- `packages/core/src/chatcore/explainers.py`: add `"cards"` to `BUILTIN_PLUGINS` (alphabetical).
- `tests/test_explainer_catalog.py`, `tests/test_explainer_wiki.py`: only what a new built-in
  plugin needs, as the image plugin did.
- `docs/wiki/Plugin-cards.md`, `docs/wiki/Explainers.md`, and the expenses wiki page if the
  generator changes it: generate with `uv run python .github/scripts/generate_explainers.py`;
  never hand-edit.
- `uv.lock` (by `uv lock` only; the root workspace glob already includes `packages/plugins/*`).
- `CHANGELOG.md` (`## [Unreleased]`), `ARCHITECTURE.md` §6a (one privacy bullet).

You may **not** edit: core outside `explainers.py`, adapters, services, other plugins, the
request snapshot, root `pyproject.toml`, `.github/`, migrations.

### B1. `chatcore_cards` plugin

Layout as `AGENTS.md` requires. `__init__.py`: docstring, re-exports,
`__all__ = ["CardsPlugin", "CardsSettings"]`. Logger `"chatcore_cards"`. `pyproject.toml` like
the image plugin's, name `chatcore-cards`, description "Summary cards for chatcore",
`dependencies = ["chatcore"]`.

`settings.py`, `CardsSettings(BaseSettings)` with the same config style as `ImageSettings`:

| Field | Default | Validation |
| --- | --- | --- |
| `CARDS_ENABLED` | `False` | |
| `CARDS_RENDER_URL` | `""` | empty, or http(s) without userinfo |
| `CARDS_THEME` | `"light"` | `light` or `dark` |
| `CARDS_TIMEOUT_SECONDS` | `20` | 1–60 |
| `CARDS_MAX_TEXT_CHARS` | `8000` | 1,000–12,000 |
| `CARDS_MAX_PER_USER_HOUR` | `20` | 0–1,000 |
| `CARDS_FOOTER` | `""` | ≤ 60 characters, no control characters |

`service.py`, `CardService(settings, *, client=None, clock=None)`:
- `available`: enabled, URL set, per-user cap > 0.
- `async render(card, *, user_key) -> list[bytes]`: rolling-hour per-user counter under an
  `asyncio.Lock`, charged on attempt (refusal → `CardError("busy")` is **not** used for quota; use
  a separate `CardQuotaError` subclass with code `"quota"`), then `client.render(card,
  theme=..., footer=<CARDS_FOOTER and the current time in the chat's timezone if the tool passes
  one, else UTC>)`. Logs `card rendered pages=%d bytes=%d ms=%d` or
  `card render failed code=%s`; never card text.
- `aclose()`.

`permissions.py`: `may_send_card(access, ctx)`: copy `chatcore_image.permissions.may_generate`,
names changed, with the platform test `chatcore.cards.can_deliver_cards(ctx.platform)`.

`gates.py`: `{}` unless `plugin.cards_available`, otherwise `{"send_card": ToolGate(scope=...,
user=...)}` as the image plugin does.

`plugin.py`, `CardsPlugin(BasePlugin)`, `name = "cards"`, `explainer_package = "chatcore_cards"`,
`help_title = "Summary cards"`, constructor `(access, platform, *, settings=None, service=None)`.
`cards_available`: service available and `can_deliver_cards(platform)`.

Tool `send_card` (`writes=False`), description "Send a structured answer as one clean summary
card image; the text reply follows." Parameters: `card_json_schema()` from core with one extra
top-level property `caption` (string, 1–200 characters, plain text, required). Handler, in the
same order as `generate_image`: one attempt per turn; validate `caption` (no newlines or control
characters) and the card with `validate_card(..., max_text_chars=CARDS_MAX_TEXT_CHARS)`, returning
`"Invalid card: <detail>"`; unavailable or not permitted → "Cards are unavailable here."; set the
attempt; render; recheck permission ("Card delivery is no longer allowed."); `send_card_pages`
with caption `"📋 " + caption`; success → "Card sent above the text reply. Now reply in 1–3 short
lines with the key result and any links; don't repeat the whole card." Errors: `quota` → "Card
limit reached for now; answer in text.", `busy` → "The card renderer is busy; answer in text.",
`too_large` → "That card is too long; shorten it or answer in text.", `timeout`/`unavailable` →
"Couldn't make the card; answer in text.". Other exceptions → log class name, "Couldn't deliver
the card; answer in text.".

Skill `visual_summary`, only when available, exactly as in the design plan: summary, examples
(4–12, including at least one Chinese and one Singlish phrasing), casefolded keywords,
`max_steps=6`, `max_output_tokens=12288`, and `VISUAL_SUMMARY_INSTRUCTIONS` (the design plan's
draft, ≤ 800 characters). Don't use bare "image", "photo" or "table" as keywords.

`EXPLAIN.md`: same format and headings as `chatcore_image/EXPLAIN.md`: what it does, when it's
offered, limits, privacy (rendered locally; the image goes to the chat), settings.

### B2. Expense cards

- `ExpensesPlugin.__init__` gains `cards: CardRenderClient | None = None` and
  `cards_theme: str = "light"`. The card tools exist only when `cards` is set **and** the
  platform passed in, or `ctx.platform` at call time, can deliver cards (`can_deliver_cards`).
  With `cards=None` the tool list, skills, gates and every existing test are unchanged.
- New module `cards.py` builds `Card` objects from ledger data; no rendering or delivery there.
  - `balance_card(trip, totals, groups, names, *, currency_exponents)`: title "Balances · <trip
    name>" (or "Balances · General ledger"), a `stats` block (total spend per currency, up to
    3; number of expenses), a `table` with columns Member / Paid (CUR) / Share (CUR) /
    Net (CUR) (numeric columns `end`), net shown as `+12.50` / `−8.20` with a real minus, and a
    second table or bullets "Who pays whom" from the same settlement suggestions `get_balances`
    shows. Multi-currency ledgers get one table per settle currency group, within the block cap.
  - `trip_summary_card(...)`: the balance card plus a `table` of spend by category when expenses
    have categories, else by day (top 10 rows, the rest as an "Other" row).
  - Every number comes from `ledger_balances` and the same helpers `_balance_text` uses. If a
    value would exceed a card limit (for example more than 40 members), fall back to text with a
    tool error "Too many members for a card; showing text balances." and the normal balance text.
- Tools `send_balance_card(trip?: string, general?: boolean)` and
  `send_trip_summary_card(trip?: string)`, `writes=False`, resolved exactly as `_balances` and
  `_export` resolve trips and errors. One card attempt per turn across both tools. Recheck the
  same permission as other expense reads before sending. Deliver with `send_card_pages`, caption
  `"📋 " + <title>`. Success → "Card sent above the text reply. Now reply in 1–2 short lines with
  the headline result."
- `gates.py`: the two tools get the same `linked` user gate plus a scope gate that is
  `NOT_ALLOWED` when the plugin has no card client. `Gate(`/`ToolGate(` stay only in `gates.py`.
- Add both tools to the `expenses` and `trips` skills' `tools` only when the plugin has a card
  client (so `skills()` is unchanged without it). Add one sentence to `EXPENSES_INSTRUCTIONS`
  only when cards are on, through a separate constant appended in `skills()`, keeping the total
  ≤ 800 characters: "For a balance or trip summary card, use send_balance_card or
  send_trip_summary_card; numbers come from the ledger."

### Run B tests

- **Service:** per-user rolling hour and rollover (injected clock), charge on attempt, quota is
  `quota` not `busy`, error mapping, no card text in logs (`caplog`), `available` rules, settings
  validation.
- **Plugin:** offered only when available and deliverable; one attempt per turn, including after
  failure; invalid card and caption rejected without rendering; permission recheck before
  sending (nothing sent when it fails); single page → `send_photo`, multi-page → `send_photos`,
  file-only platform → `send_file`; caption `"📋 " + caption`; instructions ≤ 800; `__all__`
  resolves; skill has `max_output_tokens=12288` and `max_steps=6`.
- **Expense cards:** for one ledger fixture with two currencies, settlements and a converted
  trip, every number in `balance_card` equals the corresponding figure in `get_balances` text;
  member names exactly as stored (including CJK and similar names); minus sign is U+2212;
  trip resolution errors match `_export`; `general` and `trip` together rejected; tools absent and
  skills, gates and instructions byte-identical when `cards=None`; one card per turn across both
  tools; permission recheck.
- **Contract** (`tests/test_cards_delivery_contract.py`): following
  `tests/test_image_delivery_contract.py`, run `send_card` through `ToolRegistry` against the real
  `TelegramPlatform` with a mocked bot and a stub `CardRenderClient` returning (a) one real
  1,000 × 9,000 PNG and (b) three PNG pages: assert `bot.send_photo` once with the caption, then
  `bot.send_media_group` once with three items and the caption on the first. Also build a
  maximum-size card (every block at its cap, total text at `CARDS_MAX_TEXT_CHARS`) in English and
  in Chinese, serialise its tool arguments to JSON, and assert `len(json) / 2.5 <= 12288` for
  English and `len(json) <= 12288 * 1.3` for Chinese (a conservative stand-in for tokens; the
  real measurement is a qualification step).

### Run B done when

- Lint, format and the non-browser test suite pass, with all existing tests unchanged (snapshot,
  package boundaries, plans index, explainer tests, voice and image contract tests).
- `uv run python .github/scripts/generate_explainers.py --check` passes.
- `CHANGELOG.md` `## [Unreleased]` `### Added`: the optional `chatcore-cards` plugin (disabled by
  default, `CARDS_*` settings) and the expense card tools (on only when an app passes a card
  client). `ARCHITECTURE.md` §6a: card text is rendered by the local renderer and the image is
  uploaded to the chat; nothing new leaves the deployment.
- `git status` shows only files Run B owns.

Left for later, so don't attempt it: scaffold or app wiring, routing-set entries (downstream),
renderer Dockerfile fonts (decided after qualification), PDF output, research-worker cards,
charts, hosted pages.
