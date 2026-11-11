# Image generation plan

> **For agentic workers:** Implement only after explicit owner approval. Activation stays blocked until the qualification in "Activation gate" passes.

**Goal:** Let any bot generate a picture from a text description on explicit request and send it into the chat, using an OpenAI API key the operator already holds. Keep spending bounded by local caps, because OpenAI offers no per-key usage endpoint that a bot key can read.

**Status (2026-11-11):** Approved by the owner; inline photos and `gpt-image-2.5-flare` chosen. Execution: [execution plan](2026-11-11-image-generation-execution.md).

**Architecture:** A new `image` plugin (`chatcore_image`) owns the `generate_image` tool, its budgets, gate and skill, following the `voice` plugin's shape ([voice narration plan](2026-11-09-voice-narration.md)). It calls the OpenAI Images API through the `openai` SDK that core already depends on. It delivers the result as an **inline photo** through a new optional `PhotoPlatform.send_photo` capability in core, implemented by the direct Telegram adapter. Where a platform has only `FilePlatform` (the Telegram bridge mode), it falls back to `send_file` as a document. Downstream apps own the key, model, caps and activation.

**Tech stack and dependencies:** Existing Python, `openai>=2.0` (already a core dependency), Pydantic. No new Python package, no migration in v1.

**Execution:** Claude plans; Codex executes and reviews; Claude does the final review (`AGENTS.md`, owner's computer with Codex CLI).

## Why nothing does this today

- The model layer (`chatcore.llm`) calls only chat completions: `LLM_*` for the agent and `VISION_*` for reading images. Nothing calls an image-generation endpoint.
- `FilePlatform.send_file` already sends bytes to a chat as a document (Telegram `send_document`, 20 MiB cap, direct and bridge modes). Expenses export and Google use it.
- `VoicePlatform.send_voice` and the `voice` plugin show the pattern for a paid, gated, once-per-turn attachment tool. `VoicePlatform` is also the precedent for a capability that only the direct adapter implements.
- Nothing sends an inline photo. The release-control Telegram bridge (`services/release-control/.../telegram_bridge.py`) allows a fixed set of operations that includes `send_document` but not `send_photo` or `send_voice`.

## Product contract and scope

- Generate only on an explicit request ("draw", "make a picture of", "generate an image", "illustrate"). Never generate one automatically, and never as decoration for an ordinary reply.
- The model writes a self-contained prompt and calls `generate_image` once. The image is sent inside the tool call, before the final text reply, as narration clips are. The text reply says briefly what was made.
- **One image per turn**, including failed attempts. One image per call (`n=1`). No editing or variations of user-supplied images, no background or scheduled generation, no caching in v1.
- Prompt: 1–`IMAGE_MAX_PROMPT_CHARS` characters (default 2,000), with no control characters except ordinary whitespace. Reject over-long prompts; never truncate silently.
- Size: an enum of the sizes the configured model supports (v1: `square`, `portrait`, `landscape`, mapped in code to `1024x1024`, `1024x1536`, `1536x1024`). Quality is fixed by setting, not chosen by the model.
- Caption: a model-supplied plain title of 1–120 characters, built into the caption in code (`🖼 <title>`). The caption stays under 1,024 UTF-16 code units.
- Delivery: an inline photo through `PhotoPlatform.send_photo` when the platform has it (direct Telegram). Otherwise a document (`image.png`) through `FilePlatform.send_file` (bridge mode). The tool is hidden on platforms that have neither.

Out of scope for v1, recorded for later: editing a photo the user sends (`images.edit`), inline photos in bridge mode (adding `send_photo` to the release-control bridge allowlist), multiple images per call, scheduled images via `jobs`, a shared cross-process budget, and persisting counters across restarts.

## Provider integration

- Client: `openai.AsyncOpenAI(api_key=..., base_url=IMAGE_OPENAI_BASE_URL, timeout=..., max_retries=0)`. Fixed origin, no redirects, no proxy inheritance, matching the provider hardening already in `chatcore.llm.openai_compat`.
- Call: `images.generate(model, prompt, size, quality, n=1, output_format="png", moderation=IMAGE_MODERATION)`. GPT Image models return base64 (`b64_json`), never a URL, so there is no second fetch.
- Model: from a fixed allowlist in code, `{"gpt-image-2.5-flare", "gpt-image-2.5-sunburst"}`. **Default `gpt-image-2.5-flare`** (owner decision 2026-11-11), OpenAI's fast everyday tier. `gpt-image-2.5-sunburst` is the higher-quality, higher-cost tier for operators who opt in. Both use the same `images.generate` endpoint as `gpt-image-2`. OpenAI's developer docs list `gpt-image-2.5-sunburst`; the `-flare` ID and both sets of prices come from third-party sources so far, so qualification confirms them against OpenAI's model and pricing pages. Older models are not allowed (third-party sources report `gpt-image-1.5` and `gpt-image-1-mini` retiring 2026-12-01). An unknown model fails closed at startup.
- Response checks: decode base64, require a PNG signature (`\x89PNG\r\n\x1a\n`), enforce `IMAGE_MAX_BYTES` (default 10 MiB) after decoding, reject empty output.
- Errors: a provider content-policy refusal (HTTP 400 with a moderation code) becomes the tool error "The image provider refused this prompt"; other errors become a safe "unavailable" error. Log only `image generate failed code=<status or class>`, never the prompt or response body.
- Timeouts: connect 5 s, request 120 s, outer deadline 130 s. Image generation is slow, often 20–60 s.

**No provider preflight.** Unlike ElevenLabs, OpenAI exposes usage and costs only through organization admin endpoints, which an ordinary project key can't read. The ceilings are therefore:

1. Local caps (below), the first line.
2. A dedicated OpenAI **project** with its own key and a monthly budget, set by the operator. This is the hard ceiling.

## Local budgets

| Setting | Default | Notes |
| --- | --- | --- |
| `IMAGE_ENABLED` | `false` | |
| `IMAGE_OPENAI_API_KEY` | empty `SecretStr` | Key from a dedicated OpenAI project. It may hold the same value as another OpenAI key on the host, but a separate project key is recommended so the budget is separate |
| `IMAGE_OPENAI_BASE_URL` | `https://api.openai.com/v1` | |
| `IMAGE_MODEL` | `gpt-image-2.5-flare` | `gpt-image-2.5-flare` or `gpt-image-2.5-sunburst` |
| `IMAGE_QUALITY` | `medium` | `low`, `medium` or `high` |
| `IMAGE_MODERATION` | `auto` | `auto` or `low` |
| `IMAGE_MAX_PROMPT_CHARS` | `2000` | 1–8,000 |
| `IMAGE_MAX_PER_USER_DAY` | `10` | UTC day, per user, across chats; 0–1,000 |
| `IMAGE_MAX_PER_DAY` | `50` | UTC day, all users; 0–10,000; 0 disables |
| `IMAGE_MAX_BYTES` | `10485760` | Decoded size cap |

- Reserve one image against the per-user and daily caps, plus a worker slot, under one async lock before any provider I/O. Failed attempts keep their reservation; no retries or refunds.
- At most two active generations per process; a third fails immediately as busy, without being charged.
- Counters are process-local and reset on restart. The OpenAI project budget is what catches a restart that resets a local counter.
- `/image usage` (owner and delegated admins only): today's local counts against both caps, the model and quality. No key or prompt data.

## Code layout

**Phase 1: inline photo capability (core and direct Telegram adapter).** Maintenance review: core and adapter.

- `packages/core/src/chatcore/files.py`: add a `@runtime_checkable` `PhotoPlatform` protocol with `send_photo(chat, filename, data, *, caption=None, thread_id=None) -> MessageRef`, beside `VoicePlatform`. Errors contain only the error class name.
- `packages/core/src/chatcore/testing.py`: `FakePlatform.send_photo` records sent photos, as `send_voice` does.
- `packages/adapters/telegram/src/chatcore_telegram/__init__.py`: `send_photo` through `_send_with_retry(bot.send_photo, photo=data, filename=..., caption=..., message_thread_id=...)`. It checks Telegram's photo limits before sending: at most `MAX_PHOTO_BYTES` (10 MiB), a PNG or JPEG signature, and a caption of at most 1,024 UTF-16 code units. A failed check raises `PlatformError` (`PhotoTooLarge`, `InvalidPhoto`, `CaptionTooLong`).
- The bridge client (`bridge.py`) does **not** implement `send_photo`, so bridge mode keeps using documents and the release-control service is unchanged.
- Behaviour-neutral for every existing plugin: the request snapshot and skill catalog tests pass unchanged.

**Phase 2: `image` plugin.**

`packages/plugins/image/src/chatcore_image/`, following the plugin module layout:

- `settings.py`: `ImageSettings` with the table above and the model allowlist validator.
- `service.py`: `ImageService`, holding the OpenAI client, budgets, the PNG and size checks, and error mapping.
- `permissions.py`: `may_generate`, the same rules as `chatcore_voice.permissions.may_narrate` (refresh access; reject restrictions and namespace mismatch; `can_dm` for DMs; allowed chats for groups; fail closed on errors). Duplicated, because plugins don't import each other.
- `gates.py`: a config gate plus a platform gate that requires `PhotoPlatform` or `FilePlatform`, and a user gate calling `may_generate`.
- Delivery in `service.py`: `send_photo` if `isinstance(platform, PhotoPlatform)`, else `send_file`. A `PlatformError` from `send_photo` is not retried as a document. The attempt is already charged, and the tool reports that the image couldn't be sent.
- `plugin.py`: `ImagePlugin` with the `generate_image` tool, the `/image usage` command and the `image_generation` skill.
- `EXPLAIN.md`, plus a `pyproject.toml` and workspace entry. `uv.lock` changes, which needs maintenance review.

`generate_image` schema:

```json
{
  "type": "object",
  "properties": {
    "prompt": {"type": "string", "description": "Self-contained description of the image to create: subject, style, composition, any text it should show; at most the configured limit."},
    "size": {"type": "string", "enum": ["square", "portrait", "landscape"], "description": "Image shape."},
    "title": {"type": "string", "description": "Short plain title for the caption; 1–120 characters."}
  },
  "required": ["prompt", "size", "title"],
  "additionalProperties": false
}
```

The `image_generation` skill: summary "Create a picture from a description", keywords such as "draw", "picture of", "generate an image", "illustrate", "make an image", "画", tool `generate_image`, `max_steps=2`. Instructions (under 800 characters, written for any bot): generate only on explicit request; write one self-contained prompt that resolves references from the conversation ("make it blue" means restate the whole picture); don't put real private people's names or personal details into prompts; call `generate_image` once; then reply in one or two sentences saying what was made; if it fails, say so and offer to adjust the description.

## Authorization

- Delivery is a response attachment (`writes=False`), the same policy approved for pronunciation and narration clips. This plan asks the owner to approve it for model-composed images.
- `gates.py` and `permissions.py` are permission changes and are called out in the PR.
- The model writes the prompt, so the explicit-request instruction is not a hard intent check. The one-per-turn limit, per-user daily cap, daily cap and project budget bound the cost of a wrong call. OpenAI's moderation is the content check; the bot adds no second image classifier in v1.

## Privacy

Update `ARCHITECTURE.md` §6a: image prompts, which may paraphrase user messages, go to OpenAI. The image comes back in memory and is uploaded to the chat platform; nothing is stored by the bot. Record OpenAI's API data retention and training terms for the project, as for ElevenLabs.

## Activation gate

Before a downstream app sets `IMAGE_ENABLED=true`, the operator:

1. Creates a dedicated OpenAI project and key (or decides to reuse an existing key and records why), and sets the project's monthly budget.
2. Confirms the `gpt-image-2.5-flare` and `gpt-image-2.5-sunburst` model IDs, their supported sizes and quality values, and their per-image prices on OpenAI's own model and pricing pages. A mismatch is fixed in the code allowlist before activation.
3. Runs at most three paid generations (one per size, at `IMAGE_QUALITY`) and records model, latency, bytes, the cost shown in the OpenAI usage dashboard and a visual verdict.
4. Triggers one provider refusal with an obviously disallowed prompt and confirms the bot reports it safely.
5. Sets the daily caps so that `IMAGE_MAX_PER_DAY × 30 × per-image cost` stays under the project budget.

Missing credentials or a failed step keep activation blocked.

## Tasks

- [ ] Phase 1: `PhotoPlatform` protocol, `FakePlatform.send_photo`, and Telegram `send_photo` with size, signature and caption checks. Adapter tests use a mocked bot and assert the request shape and each `PlatformError`. Snapshot and catalog tests pass unchanged.
- [ ] `ImageSettings` validation: model allowlist, quality, caps, key required when enabled.
- [ ] `ImageService` tests with a mocked `AsyncOpenAI`: request shape (model, size mapping, quality, `n=1`, `output_format`, moderation), PNG and size checks, moderation refusal, timeout and 5xx mapping, no prompt in logs.
- [ ] Budget tests: per-user and daily UTC rollover, concurrency and busy without charge, refusals before any provider call.
- [ ] Plugin tests: tool offered only when enabled, keyed and on a `PhotoPlatform` or `FilePlatform`; photo used when available, document otherwise, no document retry after a photo failure; one attempt per turn; handler-time permission recheck before the call and before sending; caption built in code.
- [ ] `/image usage` command test, including the non-admin refusal and no secrets.
- [ ] Skill catalog and request snapshot updates.
- [ ] `ARCHITECTURE.md` §6a privacy note, `CHANGELOG.md` entry under Unreleased, `EXPLAIN.md`, plans index row update.

## Open questions for the owner

1. ~~Document or inline delivery?~~ **Decided 2026-11-11: inline photos** in direct Telegram (Phase 1), with document fallback in bridge mode. Should a later change add `send_photo` to the release-control bridge allowlist, so bridge deployments get inline photos too?
2. Are 10 images per user and 50 per day the right starting caps?
3. Should the default quality be `medium` (cheaper, faster) or `high`?
4. Should v2 support editing a photo the user sends ("make this a watercolour")? That needs the inbound image bytes passed to the tool, which the vision path already downloads.

## Implementation record (2026-11-11)

**Execution:** Codex `gpt-6-luna` executed, following the [execution plan](2026-11-11-image-generation-execution.md). Codex `gpt-6.1-sol` reviewed. Claude did the final review.

**Open questions:** inline photos and `gpt-image-2.5-flare` were chosen by the owner. The caps (10 per user, 50 per day) and `medium` quality use the plan defaults and can be changed in settings.

**Review:** Sol's first review returned FAIL, with no high-severity findings. One fix round resolved every finding:

- Moderation refusals are detected from the error code or the body.
- Non-string tool arguments are rejected.
- Budget and authorization tests were made stronger.
- The packaged explainer documents every `IMAGE_*` setting.
- Claude's review added a keyword fix. `画` and bare `draw` became `画一`/`画个`/`画张`/`帮我画` and `draw a`/`an`/`me`/`my`. Non-ASCII keywords match as substrings, so the bare forms routed 动画 (anime), 漫画 (comics) and "lucky draw" to image generation.

**Verification:**

- Full suite: 4,044 passed, 57 skipped (services not available locally). Ruff check, format and the explainer check are clean.
- Mutation checks each turned the image tests red, then were reverted:
  - charging busy refusals;
  - removing the daily cap;
  - falling back to a document after a photo failure;
  - skipping the permission recheck after generation.

**Plan changes found during implementation:**

- The `codex exec review --uncommitted` command in this Codex version rejects a custom prompt, so Sol ran as `codex exec -s read-only` with the review brief.

**Not run:** no live OpenAI call (that's the activation gate), no Postgres integration tests locally, and no live Telegram `send_photo`. The real adapter is exercised only with a mocked bot.
