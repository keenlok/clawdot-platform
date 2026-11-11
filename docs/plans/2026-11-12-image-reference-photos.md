# Image reference photos plan

> **For agentic workers:** Activation stays blocked until the qualification in "Activation gate" passes.

**Goal:** Let a person send or reply to a photo and ask the bot to transform it ("make this a watercolour", "把这张改成卡通"). The bot then sends back one new inline image based on that photo, not on a text description of it.

**Status (2026-11-12):** Approved by the owner, with the proposed defaults for all three open questions. Execution: [execution plan](2026-11-12-image-reference-photos-execution.md). This is a follow-up to [image generation](2026-11-11-image-generation.md) (v0.9.0), which listed editing a user's photo as out of scope for v1.

**Architecture:** This extends the existing `image` plugin. It adds no new plugin and no new tool. `generate_image` gets an optional `reference` argument: the 1-based number of an image attached to the current request.
- With a reference, the service calls OpenAI's `images.edit` instead of `images.generate`.
- Reading the photo reuses core's attachment path: `ctx.turn.attachments`, plus `chatcore.images.download_image` with the per-turn `ctx.turn.downloads` cache. Receipt reading uses the same path.
- Delivery, budgets, gate and skill stay the same.
- Editing is a separate opt-in, `IMAGE_EDIT_ENABLED` (default `false`), because it sends users' photos to OpenAI.

**Tech stack and dependencies:** Existing Python, `openai>=2.0`, Pydantic. No new package, no migration and no adapter changes. One small core addition: attachments don't record who sent them, so `TurnState` gains `attachment_owners`, a tuple of sender user keys matching `attachments` one to one. The agent fills it where it sets `ctx.turn.attachments`. That's a core change and needs maintenance review; the request snapshot must not change.

**Execution:** Claude plans; Codex executes and reviews; Claude does the final review (`AGENTS.md`, owner's computer with Codex CLI).

## Why a description isn't enough

Today the main model can see the photo, because vision is on, and it can describe the photo in its prompt. OpenAI never receives the pixels, though, so faces, layout, pets and objects come back only loosely similar. `images.edit` takes the image itself, which preserves the subject and composition.

## Product contract and scope

- **Explicit request only.** The user asks to change, restyle or base a new picture on a photo. The photo must be **attached to the current message or to the human message it replies to**. These are the same sources that `ctx.turn.attachments` already exposes.
  - Never a photo from conversation history, search results, web pages or the bot's own messages.
  - Never a photo found by `fetch_web_page`.
- **One reference image** per call in v2. The edit API accepts up to 16, so more can come later.
- **One image per turn** in total, generated or edited, as in v1. An edit counts as one image against the same per-user and daily caps.
- **Inputs:** JPEG, PNG or WebP, at most 5 MiB (core `MAX_IMAGE_BYTES`). Bigger or other formats get a clear error, with no provider call and nothing charged.
- **Output:** the same PNG check, inline photo, caption `🖼 <title>`, and document fallback in bridge mode as v1.
- **No masks** (inpainting a selected region), no multi-image collage and no edit history in v2.

## Tool contract

`generate_image(prompt, title, size, reference?)`:
- **`reference`** is an optional 1-based integer. Booleans and non-integers are rejected. Its description says to use it when the user wants a picture *based on* a photo they sent or replied to.
- **Validation order:**
  1. types;
  2. prompt, title and size rules (v1);
  3. `reference` range against the image attachments in `ctx.turn.attachments`;
  4. `IMAGE_EDIT_ENABLED`;
  5. permission check;
  6. the turn's attempt flag.

  Any failure before the provider call charges nothing and makes no download.
- **Errors**, returned in a fixed form the model can repeat:
  - "There's no photo to use. Send it with your request or reply to it."
  - "There are N images; pick 1–N."
  - "Editing photos isn't enabled here."
  - "That photo is over 5 MB."
  - "I can only use JPEG, PNG or WebP photos."
  - "I couldn't download that photo. Please send it again."
- **Without `reference`**, behaviour is byte-identical to v1. The request snapshot and the existing tests must not change.

## Provider integration

- `ImageService.generate(prompt, size, user_key, reference: ImagePart | None = None)`.
- **With a reference:** `client.images.edit(model=IMAGE_MODEL, image=(filename, bytes, media_type), prompt=..., size=..., quality=IMAGE_QUALITY, n=1, output_format="png")`, sent with the same hardened client (no redirects, `trust_env=False`, `max_retries=0`).
- **`input_fidelity`:** not sent by default. OpenAI's documentation doesn't clearly say whether `gpt-image-2.5` accepts it. gpt-image-2 always uses high fidelity and ignores it. The activation gate confirms this. If 2.5 supports it, add `IMAGE_EDIT_INPUT_FIDELITY` (`low`/`high`, default unset).
- **Moderation:** both the input photo and the prompt are checked by OpenAI. Refusals map to the existing `refused` code, using the same detection (code or body contains `moderation`, or `content_policy_violation`).
- **Cost:** an edit is billed for the image-input tokens plus the output image. Record the measured cost per edit in the activation gate, and set caps with that higher figure.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `IMAGE_EDIT_ENABLED` | `false` | Allows `reference`. Without it, `reference` returns "Editing photos isn't enabled here." and the tool description doesn't mention it. |

Everything else (`IMAGE_MODEL`, `IMAGE_QUALITY`, the caps and the key) is shared with generation.

## Skill and routing

- **`image_generation` keywords:** add "make this", "turn this into", "edit this photo", "this photo", "把这张", "改成", "变成".
  - Keep the v1 rule: no bare 画, and no "draw" on its own.
  - Add routing-set rows for:
    - "make this a watercolour" with a photo;
    - "把这张改成卡通" with a photo;
    - a negative, "this photo is nice", which must not call the tool when no change is asked for.
- **Instructions** (still at most 800 characters, written for any bot):
  - Use `reference` only when the person asks to transform or base a picture on a photo they sent or replied to.
  - Restate the whole desired result in the prompt.
  - Never edit a photo to depict a real person in sexual, violent, humiliating or deceptive situations.
  - Don't use photos the person didn't point to.
- **Request snapshot:** the tool description changes only when `IMAGE_EDIT_ENABLED` is on. The off-state snapshot stays unchanged.

## Authorization

- Same gate and permission recheck as v1, before the edit and again before delivery. `gates.py` gains no new rule, but the tool's offered description depends on `IMAGE_EDIT_ENABLED`. Call that out as a permission-adjacent change.
- **Whose photo:** see open question 1. The default proposed here is that `reference` may use:
  - photos in the requester's own current message;
  - a photo in a message the requester replies to, but only in a DM, or in a group when the replied-to message is the requester's own.

  Using another member's photo in a group would send their image to OpenAI without their action.

## Privacy

- **`ARCHITECTURE.md` §6a:** with editing enabled, **a photo the user points to is sent to OpenAI** along with the prompt. The bot keeps it only in the per-turn memory cache and never stores it.
- **`/privacy`:** the app composes the images notice, so the plugin exports `IMAGE_EDIT_PRIVACY_NOTE` for apps to append when `IMAGE_EDIT_ENABLED` is on.
- Record OpenAI's retention and training terms for the project, as v1 does.

## Activation gate

Before a downstream app sets `IMAGE_EDIT_ENABLED=true`, the operator:
1. Confirms in OpenAI's current reference that `gpt-image-2.5-flare` supports `images.edit`, and whether it accepts `input_fidelity`.
2. Runs at most three paid edits: a pet photo, a landscape and a drawing, each at `IMAGE_QUALITY`. For each, records latency, input and output bytes, the cost shown in the usage dashboard, and a verdict on how well the subject was preserved.
3. Triggers one refusal with an obviously disallowed edit request and confirms the bot reports it safely.
4. Re-checks that `IMAGE_MAX_PER_DAY × 30 × max(generate cost, edit cost)` stays under the project budget.

## Tasks

1. **Service:** add the `reference` path to `ImageService.generate` and its tests:
   - edit called with the file tuple and settings;
   - nothing charged on refusals before the call;
   - busy not charged;
   - moderation mapped to `refused`;
   - caps shared with generation.
2. **Plugin:** add the `reference` argument, validation order, attachment selection and download through `download_image` with `ctx.turn.downloads`, the whose-photo rule and the setting. Tests:
   - no attachment;
   - out of range;
   - booleans and strings;
   - too large and unsupported, with no provider call;
   - another member's photo in a group refused;
   - one edit and one inline photo end to end with `FakePlatform`;
   - the v1 path unchanged.
3. **Skill:** add the keywords, instructions, routing-set rows and a skill-catalog test. The off-state request snapshot is unchanged.
4. **Docs:** `EXPLAIN.md` settings, the generated wiki, a §6a privacy bullet, the CHANGELOG (minor bump), and this plan's implementation record.
5. **Release** v0.10.0. Then add a deployments pin PR, plus a later activation PR with `IMAGE_EDIT_ENABLED=true` after the gate.

## Owner decisions (2026-11-12)

The owner accepted the proposed default for each question:


1. **Whose photos in groups:** the requester's own photos only. In DMs, the replied-to photo is also allowed.
2. **Photos of people:** allowed, with the skill instruction above and OpenAI moderation as the backstop.
3. **Caps:** shared with generation. Add `IMAGE_MAX_EDITS_PER_DAY` only if the gate shows edits cost much more.
