# Image generation: execution plan

Codex execution plan for the [image generation plan](2026-11-11-image-generation.md). Codex executes with `gpt-6-luna`, Codex reviews with `gpt-6.1-sol`, and Claude does the final review (`AGENTS.md`, `ARCHITECTURE.md` §7b).

Read first: `AGENTS.md` (the whole file), the design plan [2026-11-11-image-generation.md](2026-11-11-image-generation.md), and these files, which define the contracts and patterns you build against:

- `packages/core/src/chatcore/files.py` (`FilePlatform`, `VoicePlatform`)
- `packages/core/src/chatcore/testing.py` (`FakePlatform`)
- `packages/adapters/telegram/src/chatcore_telegram/__init__.py` (`TelegramPlatform`, `send_file`, `send_voice`, `_send_with_retry`, `MAX_VOICE_BYTES`)
- The whole `packages/plugins/voice/` package and `tests/test_voice_delivery_contract.py`. **Copy their structure closely**; this plugin is the same shape.
- `packages/core/src/chatcore/explainers.py` (`BUILTIN_PLUGINS`) and `.github/scripts/generate_explainers.py`

## Environment

- `export UV_CACHE_DIR="$PWD/.uv-cache"` before any `uv` command.
- After adding the package: `uv lock` then `uv sync --all-packages`.
- Lint: `uv run ruff check packages services apps tests .github/scripts` and `uv run ruff format <only the files you changed>`.
- Tests: `uv run pytest -q -m "not browser and not sandbox_integration"`. Postgres-backed tests skip without a database; that's expected.
- Never call the real OpenAI API. Mock `openai.AsyncOpenAI` (e.g. `AsyncMock` on `client.images.generate`) or use `httpx.MockTransport` via the client's `http_client`.

## You own

- `packages/core/src/chatcore/files.py`: **add** `PhotoPlatform` only; don't change existing protocols.
- `packages/core/src/chatcore/testing.py`: **add** `FakePlatform.send_photo` only.
- `packages/core/src/chatcore/explainers.py`: add `"image"` to `BUILTIN_PLUGINS` (keep alphabetical order).
- `packages/adapters/telegram/src/chatcore_telegram/__init__.py`: add `MAX_PHOTO_BYTES`, `send_photo`, and `PhotoPlatform` to `TelegramPlatform`'s bases. Re-export `MAX_PHOTO_BYTES` the way `MAX_VOICE_BYTES` is exported.
- `packages/adapters/telegram/tests/test_telegram.py`: new `send_photo` tests.
- New package `packages/plugins/image/` (all files): `pyproject.toml`, `src/chatcore_image/{__init__,plugin,gates,permissions,settings,service}.py`, `src/chatcore_image/EXPLAIN.md`, `tests/test_plugin.py`, `tests/test_service.py`.
- `tests/test_image_delivery_contract.py` (new).
- `tests/test_explainer_catalog.py`, `tests/test_explainer_wiki.py`: only the edits needed for the new built-in plugin, as the voice plugin did.
- `docs/wiki/Plugin-image.md` and `docs/wiki/Explainers.md`: generate them with `uv run python .github/scripts/generate_explainers.py`. Never hand-edit them.
- `uv.lock` (by `uv lock` only).
- `CHANGELOG.md`: one `### Added` entry under `## [Unreleased]`.
- `ARCHITECTURE.md`: one bullet in §6a (privacy), next to the ElevenLabs/voice note.

You may **not** edit: the release-control service (`services/`), `packages/adapters/telegram/src/chatcore_telegram/bridge.py`, any other plugin, the agent loop, the request snapshot (`packages/core/tests/snapshots/`), the root `pyproject.toml`, Dockerfiles, Compose files, `.github/`, or any `migrations/`. If one must change, stop and say why in your report.

## 1. `PhotoPlatform` (core)

In `chatcore/files.py`, beside `VoicePlatform`:

```python
@runtime_checkable
class PhotoPlatform(Protocol):
    """Optional capability for sending one image inline as a response attachment."""

    async def send_photo(
        self,
        chat: ChatRef,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef: ...
```

`FakePlatform.send_photo` records `(chat, filename, data, caption, thread_id)` in a list, as `send_voice` does, and returns a `MessageRef`.

## 2. Telegram `send_photo` (direct adapter)

- `MAX_PHOTO_BYTES = 10 * 1024 * 1024` (Telegram's bot photo upload limit).
- `_PHOTO_FILENAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\.(png|jpg|jpeg)")`.
- `send_photo` checks, in order:
  1. `filename` is a `str` matching `_PHOTO_FILENAME`, `data` is non-empty `bytes`, and `data` starts with the PNG signature `b"\x89PNG\r\n\x1a\n"` or the JPEG signature `b"\xff\xd8\xff"`. Otherwise `PlatformError("InvalidPhoto")`.
  2. `len(data) <= MAX_PHOTO_BYTES`, else `PlatformError("PhotoTooLarge")`.
  3. Caption, if given, at most 1,024 UTF-16 code units (use `chatcore_telegram.formatting.utf16_length`, as `send_voice` does), else `PlatformError("CaptionTooLong")`.
- Then `await self._send_with_retry(self._application.bot.send_photo, chat_id=chat.id, photo=data, filename=filename, caption=caption, message_thread_id=int(thread_id) if thread_id else None)` and return `MessageRef(chat, str(msg.message_id), thread_id)`.
- `class TelegramPlatform(VoicePlatform, PhotoPlatform):` (keep whatever else is already in the bases).
- The bridge client gets **no** `send_photo`.

## 3. `chatcore_image` plugin

Follow the plugin module layout in `AGENTS.md`. `__init__.py` holds only a docstring, re-exports and `__all__ = ["ImagePlugin", "ImageSettings"]`. Logger name `"chatcore_image"` everywhere. `pyproject.toml` is like the voice one, with name `chatcore-image`, description "Image generation for chatcore", and `dependencies = ["chatcore", "openai>=2.0"]`.

### settings.py

`ImageSettings(BaseSettings)`:

| Field | Type, default | Validation |
| --- | --- | --- |
| `IMAGE_ENABLED` | `bool = False` | |
| `IMAGE_OPENAI_API_KEY` | `SecretStr = SecretStr("")` | |
| `IMAGE_OPENAI_BASE_URL` | `str = "https://api.openai.com/v1"` | must start with `https://` |
| `IMAGE_MODEL` | `str = "gpt-image-2.5-flare"` | must be in `MODELS = ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst")` |
| `IMAGE_QUALITY` | `Literal["low", "medium", "high"] = "medium"` | |
| `IMAGE_MODERATION` | `Literal["auto", "low"] = "auto"` | |
| `IMAGE_MAX_PROMPT_CHARS` | `int = 2000` | 1–8,000 |
| `IMAGE_MAX_PER_USER_DAY` | `int = 10` | 0–1,000 |
| `IMAGE_MAX_PER_DAY` | `int = 50` | 0–10,000 |

`SIZES = {"square": "1024x1024", "portrait": "1024x1536", "landscape": "1536x1024"}` lives in `settings.py`. Use the same `BaseSettings` config (env prefix, extra handling) as `VoiceSettings`.

### service.py

`ImageError(Exception)` with a `code` attribute: `"quota"`, `"busy"`, `"refused"`, `"unavailable"`, `"invalid_output"`.

`ImageService(settings, *, client=None, clock=None)`:

- `available`: `IMAGE_ENABLED`, a non-empty key and `IMAGE_MAX_PER_DAY > 0`.
- The client is built lazily: `openai.AsyncOpenAI(api_key=..., base_url=..., timeout=httpx.Timeout(120, connect=5), max_retries=0, http_client=httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=...))`. Tests inject `client`.
- `async generate(prompt: str, size: str, *, user_key: str) -> bytes`:
  1. Under one `asyncio.Lock`: roll the UTC-day counters if the day changed; refuse with `quota` if the user's count would exceed `IMAGE_MAX_PER_USER_DAY` or the total would exceed `IMAGE_MAX_PER_DAY`; refuse with `busy` if two generations are active. Otherwise reserve 1 against both counters and take a slot. Busy refusals don't charge the counters.
  2. Outside the lock, inside `asyncio.timeout(130)`: `await client.images.generate(model=..., prompt=prompt, size=SIZES[size], quality=..., n=1, output_format="png", moderation=...)`.
  3. Always release the slot. Failures keep the reservation; no retries, no refunds.
  4. Map errors: `openai.BadRequestError` whose `code` or body mentions `moderation` → `refused`; any other `openai.APIError`, `TimeoutError` or `httpx` error → `unavailable`. Log `image generate failed code=<status_code or class name>` at warning level, nothing else.
  5. Take `response.data[0].b64_json`, decode it with `base64.b64decode(..., validate=True)`, require the PNG signature and `len <= MAX_IMAGE_BYTES` (`10 * 1024 * 1024`, a module constant that must stay `<=` the Telegram `MAX_PHOTO_BYTES`). Otherwise `invalid_output`.
  6. Log `image generated model=<model> size=<size> bytes=<n> ms=<latency>` at info level. **Never log the prompt, the key or the response body.**
- `report()` returns today's user-independent total, the daily cap, the model and quality, for `/image usage`.
- `aclose()` closes the client if this service built it.

### permissions.py

`may_generate(access, ctx)`: copy `chatcore_voice.permissions.may_narrate` exactly, with names changed. Don't import from `chatcore_voice`.

### gates.py

`build_gates(plugin)`: return `{}` if `not plugin.generation_available`, otherwise `{"generate_image": ToolGate(scope=..., user=...)}`, the same as voice. The user gate calls `may_generate`. `Gate(` and `ToolGate(` appear only in this file.

### plugin.py

`ImagePlugin(BasePlugin)`: `name = "image"`, `explainer_package = "chatcore_image"`, `help_title = "Image generation"`. The constructor is `(access, platform, *, settings=None)`, as voice's is.

- `generation_available`: `self.service.available and isinstance(self.platform, (PhotoPlatform, FilePlatform))`.
- `tools()` returns `[generate_image]` only when available. `tool_gates()` returns `build_gates(self)`.
- Tool `generate_image`, `writes=False`, description "Create one requested image from a text description and send it to the chat." Schema:

  ```json
  {
    "type": "object",
    "properties": {
      "prompt": {"type": "string", "description": "Self-contained description of the image to create: subject, style, composition and any text it should show; 1–<limit> characters."},
      "size": {"type": "string", "enum": ["square", "portrait", "landscape"], "description": "Image shape."},
      "title": {"type": "string", "description": "Short plain title for the image caption; 1–120 characters."}
    },
    "required": ["prompt", "size", "title"],
    "additionalProperties": false
  }
  ```

- Handler, in the same order as `narrate_text`:
  1. One attempt per turn: `ctx.turn.tool_attempts.get("generate_image")` already set → error "An image was already attempted this turn."
  2. Validate: `prompt` is 1–`IMAGE_MAX_PROMPT_CHARS` characters after strip, with no control characters except `\n` and `\t`. `title` is 1–120 characters with no newline or control characters. `size` is in `SIZES`. Otherwise an error that names the rule.
  3. Unavailable, or `not await may_generate(...)` → "Image generation is unavailable here."
  4. Check the turn attempt again, then set it to 1.
  5. `caption = "🖼 " + title.strip()`. Call `service.generate`.
  6. Recheck `may_generate` → "Image delivery is no longer allowed." if it fails.
  7. If `isinstance(ctx.platform, PhotoPlatform)`, call `send_photo(ctx.chat, "image.png", data, caption=caption, thread_id=ctx.thread_id)`. Otherwise call `send_file` with the same arguments. Never fall back to a document after a photo failure.
  8. Success: `ToolResult("Image sent before the text response. Now reply in one or two sentences saying what was made.")`.
  9. `ImageError` → a mapped message from `_ERRORS` ("Image limit reached for today.", "Image generation is busy; suggest trying again shortly.", "The image provider refused this prompt; offer to change the description.", "Image generation is unavailable right now.", "The image provider returned an unusable image."). Any other exception → log `image delivery failed error=<class name>` and return "Could not deliver the image."
- Skill `image_generation`, only when available: summary "Create a picture from a description", tools `("generate_image",)`, `commands=("image",)`, `max_steps=4`, and `instructions=IMAGE_GENERATION_INSTRUCTIONS`, under 800 characters. The instructions say: generate only when the person explicitly asks for an image; write one self-contained prompt that restates the whole picture, resolving references from the conversation; never put private individuals' names or personal details in the prompt; call `generate_image` once; then reply in one or two sentences saying what was made; if it fails, say so and offer to adjust the description.
  - Examples: "draw a cat wearing a space helmet", "make a picture of a sunset over the Singapore skyline", "generate an image for my birthday invite", "can you illustrate this story", "画一只在海边的狗", "create a logo idea for a bubble tea shop".
  - Keywords: "draw", "picture of", "image of", "generate an image", "make an image", "illustrate", "illustration", "画", "生成图片". Don't use bare "image" or "photo": users send photos for receipts and vision.
- Command `/image usage`, `admin_only=True`: "Images today (UTC): N of M." plus "Model: <model>, quality: <quality>." Any other argument gets a usage hint. If the plugin isn't configured: "Image generation is not configured."

### EXPLAIN.md

Write it in the same format and section headings as `chatcore_voice/EXPLAIN.md`: what the tool does, when it's offered, limits, privacy (prompts go to OpenAI), and settings. Then run the generator so the wiki page and the explainer list update.

## 4. CHANGELOG and ARCHITECTURE

- `CHANGELOG.md` under `## [Unreleased]`, `### Added`: the new optional `chatcore-image` plugin (`ImagePlugin`, `ImageSettings`), disabled by default, with the `IMAGE_*` settings listed. Also the new core `PhotoPlatform` capability and Telegram `send_photo`. Note that bridge mode sends a document.
- `ARCHITECTURE.md` §6a: one bullet saying that image prompts, which may paraphrase user messages, go to OpenAI; the image comes back in memory and is uploaded to the chat; the bot stores nothing.

## Tests

**Telegram adapter** (`test_telegram.py`, mocked bot):
- A PNG and a JPEG are sent through `bot.send_photo` with the right kwargs, including `message_thread_id`.
- `InvalidPhoto` for a bad filename, empty data, non-bytes, and HTML bytes.
- `PhotoTooLarge` at `MAX_PHOTO_BYTES + 1`, and success at exactly `MAX_PHOTO_BYTES`.
- `CaptionTooLong` at 1,025 UTF-16 units.
- `isinstance(platform, PhotoPlatform)` is true.

**Service** (`test_service.py`, mocked client and clock):
- Request shape: model, mapped size, quality, `n=1`, `output_format="png"`, moderation.
- PNG decode succeeds. Non-PNG, bad base64, empty data and oversize data each raise `invalid_output`.
- Moderation 400 → `refused`. Other 400 and 500 → `unavailable`. Timeout → `unavailable`.
- Per-user cap, daily cap and UTC rollover (advance the injected clock past midnight).
- A third concurrent call is `busy`, and busy doesn't charge. A failed call keeps its charge.
- With `caplog`, the prompt text and the fake key never appear in any log record.
- `available` is false when disabled, keyless, or with a daily cap of 0.
- Settings: an unknown model, an `http://` base URL and out-of-range caps are rejected.

**Plugin** (`test_plugin.py`, `FakePlatform`):
- The tool and skill are offered only when the service is available and the platform has photo or file capability.
- `send_photo` is used on `FakePlatform`. On a platform with only `send_file`, a document is sent. On a platform with neither, the tool isn't offered.
- One attempt per turn, including after a failure.
- The permission recheck: `may_generate` returning false after generation means nothing is sent.
- The caption is `"🖼 " + title`. Invalid prompt, title and size are rejected without calling the service.
- Instructions stay under 800 characters. `__all__` resolves.
- `/image usage` works for admins, and non-admins are refused through `admin_only`.

**Contract** (`tests/test_image_delivery_contract.py`): following `tests/test_voice_delivery_contract.py`, run `generate_image` through `ToolRegistry` against the **real** `TelegramPlatform` with a mocked bot. Stub the service to return a PNG of exactly `MAX_IMAGE_BYTES`, and assert that `bot.send_photo` is awaited once with caption `"🖼 <title>"`. Also assert `MAX_IMAGE_BYTES <= chatcore_telegram.MAX_PHOTO_BYTES`.

Existing tests must pass unchanged, especially `packages/core/tests/snapshots/agent_requests_off.json`, `tests/test_package_boundaries.py`, `tests/test_plans_index.py`, the explainer tests and the voice contract test.

## Done when

- `uv run ruff check packages services apps tests .github/scripts` is clean, and every file you changed passes `ruff format --check`.
- `uv run pytest -q -m "not browser and not sandbox_integration"` passes: all new tests plus the existing ones.
- `uv run python .github/scripts/generate_explainers.py --check` passes.
- `git status` shows only files in "You own".

Left for later, so don't attempt it: inline photos in bridge mode (the release-control allowlist), image editing, scaffold or app wiring, routing-set entries (downstream), and any live OpenAI call (that's the activation gate in the design plan).
