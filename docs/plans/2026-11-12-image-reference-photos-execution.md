# Image reference photos: execution plan

Codex execution plan for the [image reference photos plan](2026-11-12-image-reference-photos.md). Codex executes with `gpt-6-luna`, Codex reviews with `gpt-6.1-sol`, and Claude does the final review (`AGENTS.md`, `ARCHITECTURE.md` §7b).

Read these first:
- `AGENTS.md` (the whole file);
- the design plan, [2026-11-12-image-reference-photos.md](2026-11-12-image-reference-photos.md), and especially "Product contract", "Tool contract", "Authorization" and "Owner decisions";
- the shipped v1 plugin, the whole `packages/plugins/image/` package and `tests/test_image_delivery_contract.py`. You are extending it, not rewriting it.
- the files that define the contracts you build against:
  - `packages/core/src/chatcore/tools/registry.py` (`TurnState`, especially `attachments` and `downloads`);
  - `packages/core/src/chatcore/agent.py`: `_context`, which builds `source_attachments` near the `replied` handling, and the place that sets `ctx.turn.attachments`;
  - `packages/core/src/chatcore/images.py` (`download_image`, `ImageUnavailable`, `MAX_IMAGE_BYTES`, `is_image`);
  - `packages/plugins/expenses/src/chatcore_expenses/plugin.py` `_read_receipt`: the existing pattern for picking a 1-based image from `ctx.turn.attachments`.

## Environment

- `export UV_CACHE_DIR="$PWD/.uv-cache"` before any `uv` command.
- Lint: `uv run ruff check packages services apps tests .github/scripts`, and `uv run ruff format` on **only the files you changed**.
- Tests: `uv run pytest -q -m "not browser and not sandbox_integration"`. Postgres-backed tests skip without a database, which is expected.
- Never call the real OpenAI API. Mock `client.images.edit` and `client.images.generate` with `AsyncMock` on an injected client.

## You own

- `packages/core/src/chatcore/tools/registry.py`: **add** one `TurnState` field only.
- `packages/core/src/chatcore/agent.py`: only the minimal change that fills that field next to where `ctx.turn.attachments` is set. `_context` may return the owners alongside the attachments. Prompt text, message rendering and anything else the model sees must not change.
- `packages/core/tests/`: a new test file for the owners field, e.g. `test_turn_attachment_owners.py`. Don't edit existing core tests or snapshots.
- `packages/plugins/image/src/chatcore_image/`: `plugin.py`, `service.py`, `settings.py`, `__init__.py` (re-export only), `EXPLAIN.md`.
- `packages/plugins/image/tests/`: `test_plugin.py`, `test_service.py`, and new test files if useful.
- `tests/test_image_delivery_contract.py`: add the edit case.
- `docs/wiki/Plugin-image.md`: regenerate with `uv run python .github/scripts/generate_explainers.py`. Never hand-edit it.
- `CHANGELOG.md`: entries under `## [Unreleased]`.
- `ARCHITECTURE.md`: one bullet in §6a next to the existing image-generation bullet.

You may **not** edit:
- `gates.py` and `permissions.py` in the image plugin;
- any other plugin or adapter;
- the request snapshots (`packages/core/tests/snapshots/`);
- root `pyproject.toml`, `uv.lock`, Dockerfiles, Compose files, `.github/`, `services/` or any `migrations/`.

If one of these must change, stop and say why in your report.

## 1. Core: `TurnState.attachment_owners`

- In `TurnState`, directly after `attachments`:
  ```python
  attachment_owners: tuple[str, ...] = ()
  """Sender user key for each entry of ``attachments``, in the same order."""
  ```
- In `agent.py`, build the owners in the same comprehension order as `source_attachments`:
  - the current message's image attachments → `message.user.ref.key`;
  - the replied human message's image attachments → `replied.user.key`.

  `replied` is a `StoredMessage`, so its `user` is a `UserRef`. Check the actual attribute names in `events.py` and `store/base.py` and use what is there.
- Set `ctx.turn.attachment_owners` where `ctx.turn.attachments` is set. The lengths must always be equal.
- **No behaviour change otherwise.** `packages/core/tests/snapshots/agent_requests_off.json` and every existing agent test must pass unchanged.

## 2. Settings

Add to `ImageSettings`: `IMAGE_EDIT_ENABLED: bool = False`.

## 3. Service: the edit path

Change the signature to `async generate(self, prompt: str, size: str, *, user_key: str, reference: ImagePart | None = None) -> bytes`, where `ImagePart` comes from `chatcore.llm.types`.
- **Budget:** lock, caps, busy, reservation and the no-refund rules are exactly as today. An edit counts as one image against both counters.
- **With `reference`:** call
  ```python
  client.images.edit(
      model=..., image=(filename, reference.data, reference.media_type),
      prompt=prompt, size=SIZES[size], quality=..., n=1, output_format="png",
  )
  ```
  - `filename` is `"reference.png"`, `"reference.jpg"` or `"reference.webp"`, chosen from `media_type`.
  - Check the `openai` SDK's `images.edit` signature in the installed package. If `moderation` is accepted there, pass it as `generate` does; if not, omit it.
  - **Don't pass `input_fidelity`.**
- **Without `reference`:** the existing `images.generate` call, byte-identical.
- **Error mapping, PNG check, size limit and logging:** the same for both paths. The log line gains `mode=generate|edit`. Never log the prompt, the key, the image bytes or the response body.

## 4. Plugin: the `reference` argument

- **Schema:** add an optional property, not in `required`:
  ```json
  "reference": {"type": "integer", "minimum": 1, "description": "Use only when the person asks to change or base a picture on a photo they sent or replied to: the 1-based number of that photo."}
  ```
  Add it **only when `IMAGE_EDIT_ENABLED` is true**. With editing off, the tool schema and description must be byte-identical to v1.
- **Handler order** (any failure before step 6 makes no download and no service call):
  1. existing type checks, plus: `reference` is either absent/`None`, or an `int` that isn't a `bool`. Otherwise "The photo number must be a whole number."
  2. existing prompt, title and size rules;
  3. if `reference` is given:
     - editing disabled → "Editing photos isn't enabled here."
     - pick the image attachments: `[(a, owner) for a, owner in zip(ctx.turn.attachments, ctx.turn.attachment_owners) if is_image(a)]`. Use strict zip semantics; if the lengths differ, treat it as no photos.
     - none → "There's no photo to use. Send it with your request or reply to it."
     - out of range → "There are N images; pick 1–N."
     - **whose photo:** in a group (`not ctx.is_dm`), the owner must equal `ctx.user.ref.key`. Otherwise "In groups I can only use your own photos." In a DM any listed photo is allowed.
  4. existing availability check and `may_generate`;
  5. existing one-attempt-per-turn check, then set the attempt flag;
  6. if `reference` is given, `await download_image(ctx.platform, attachment, chat=ctx.chat, cache=ctx.turn.downloads)`. Map `ImageUnavailable` without calling the service:
     - `too_large` → "That photo is over 5 MB."
     - `unsupported_format` → "I can only use JPEG, PNG or WebP photos."
     - `download_failed` → "I couldn't download that photo. Please send it again."
  7. `service.generate(..., reference=part)`. Everything after is unchanged: the permission recheck, `send_photo`/`send_file`, the caption, the result text and the error map.
- **Privacy constant:** in `plugin.py`, `IMAGE_EDIT_PRIVACY_NOTE = "When photo editing is on, a photo you ask the bot to change is sent to OpenAI with your request; the bot doesn't store it."` Export it from `__init__.py` and `__all__`.

## 5. Skill

- **Keywords** to add to `image_generation`: "make this", "turn this into", "edit this photo", "this photo into", "把这张", "改成", "变成".
  - Don't add bare "photo", "image", 画 or "draw".
  - Check that the existing keyword test cases still route correctly ("推荐一部动画", "lucky draw" and "withdraw" don't route to `image_generation`), and add "make this a watercolour" and "把这张改成卡通" as positives.
- **Instructions:** extend `IMAGE_GENERATION_INSTRUCTIONS`, staying **under 800 characters** and written for any bot. Use `reference` only when the person asks to change or base a picture on a photo they sent or replied to. Never use photos they didn't point to. Never edit a real person into sexual, violent, humiliating or deceptive scenes. Keep the existing rules.
  - If the instructions can't fit, keep them constant and put the reference guidance in the `reference` property description instead. Say so in your report.
- **Examples:** add "make this photo a watercolour" and "把这张照片变成卡通".

## 6. Docs

- **`EXPLAIN.md`:** add `IMAGE_EDIT_ENABLED` to the settings section, plus a short "Reference photos" section covering:
  - what the feature does;
  - group photos are the requester's own only;
  - photos go to OpenAI;
  - it shares the caps.

  Then regenerate the wiki and check that `--check` passes.
- **`CHANGELOG.md`** under `## [Unreleased]`:
  - `### Added`: reference photos through `generate_image(reference=...)` behind `IMAGE_EDIT_ENABLED` (default off), plus the `IMAGE_EDIT_PRIVACY_NOTE` export;
  - `### Changed`: core `TurnState.attachment_owners`.
- **`ARCHITECTURE.md` §6a:** with editing on, a photo the user points to is sent to OpenAI with the prompt. It's held only in the per-turn memory cache and never stored.

## Tests

**Core** (new file):
- A message with one photo yields `attachment_owners == (sender key,)`.
- A reply to another human's photo message yields owners for the current message's photos first, then the replied sender's.
- A reply to the bot's own message adds none.
- Use the existing agent test helpers (`FakePlatform`, `ScriptedProvider`) and a capture tool that records `ctx.turn`.

**Service:**
- With `reference`, `images.edit` is awaited once with the file tuple (filename, bytes, media type), model, mapped size, quality, `n=1` and `output_format="png"`, and `images.generate` is not called.
- Without `reference`, the existing generate test is unchanged.
- Edits share caps with generation: after 9 generations, 1 edit reaches the user cap of 10.
- Moderation 400 on an edit → `refused`. Other errors → `unavailable`.
- With `caplog`, no record contains the prompt, the key or the reference bytes.

**Plugin** (through `ToolRegistry` where possible):
- With editing off: the schema has no `reference`, and the tool description and schema are equal to v1. A call with `reference=1` returns "Editing photos isn't enabled here." with no service call.
- With editing on:
  - no photo, out of range, `reference=True`, `reference="1"` and `reference=1.5` each give their error with no download and no service call;
  - group + another member's photo → refused with no download;
  - group + own photo → one edit, then one `send_photo` captioned `🖼 <title>`;
  - DM + replied photo → allowed;
  - too large, unsupported format and download failure each map their error with no service call. Use a `FakePlatform` download stub or prefill `ctx.turn.downloads`;
  - mismatched `attachments`/`attachment_owners` lengths → treated as no photos.
- Keywords and instructions: the routing positives and negatives above, and instructions under 800 characters.

**Contract** (`tests/test_image_delivery_contract.py`): an edit through `ToolRegistry` against the real `TelegramPlatform` with a mocked bot, with the reference prefilled in `ctx.turn.downloads` and the service stubbed. Assert that `bot.send_photo` is awaited once.

Existing tests must pass unchanged, especially:
- `packages/core/tests/snapshots/agent_requests_off.json`;
- the skill catalog and routing tests;
- `tests/test_package_boundaries.py` and `tests/test_plans_index.py`;
- the explainer tests;
- the v1 image tests (edit only the ones whose signatures must change, and say which).

## Done when

- `uv run ruff check packages services apps tests .github/scripts` is clean, and every file you changed passes `ruff format --check`.
- `uv run pytest -q -m "not browser and not sandbox_integration"` passes.
- `uv run python .github/scripts/generate_explainers.py --check` passes.
- `git status` shows only files in "You own".

Left for later, so don't attempt it:
- masks and inpainting;
- multiple reference images;
- `input_fidelity`;
- a separate edit cap;
- app wiring or `/privacy` composition (downstream);
- the version bump and release;
- any live OpenAI call (that's the activation gate).
