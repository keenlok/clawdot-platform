<!-- Generated from packaged EXPLAIN.md; do not edit. -->
# Image generation

[Canonical source](https://github.com/keenlok/clawdot-platform/blob/main/packages/plugins/image/src/chatcore_image/EXPLAIN.md) · sha256:dc843134d5e37e928f863856ceff796f17eb786feb8d37eefc7337e6212f74ce

This page describes the selected platform source. Your app's enabled features may differ.

## Overview
The image plugin creates one image from a description when someone explicitly asks for one. It
sends the image before the bot's short text reply. Generation is disabled by default and requires
an operator configured OpenAI API key.

## Workflow
The skill is offered only when generation is configured and the platform can send a photo or file.
The model writes a self-contained prompt and chooses square, portrait or landscape. The plugin
generates one PNG and sends it with a short caption. `/image usage` shows today's local count.

## Storage
Prompts are sent to OpenAI and the generated PNG is held in memory for upload to the chat. The
plugin does not retain prompts or cache images. Usage counters are process local and reset when the
bot restarts.

## Safety
Access is checked before generation and again before delivery. Prompt length, daily per user and
global caps, a two request concurrency limit and output size are enforced locally. Generation
attempts consume their reservation even when the provider fails. OpenAI handles submitted prompts
under the operator's account terms and settings.

### Settings
| Setting | Default |
| --- | --- |
| `IMAGE_ENABLED` | `false` |
| `IMAGE_OPENAI_API_KEY` | empty |
| `IMAGE_OPENAI_BASE_URL` | `https://api.openai.com/v1` |
| `IMAGE_MODEL` | `gpt-image-2.5-flare` |
| `IMAGE_QUALITY` | `medium` |
| `IMAGE_MODERATION` | `auto` |
| `IMAGE_MAX_PROMPT_CHARS` | `2000` |
| `IMAGE_MAX_PER_USER_DAY` | `10` |
| `IMAGE_MAX_PER_DAY` | `50` |

### Activation gate
Activation stays blocked until the qualification in
[`docs/plans/2026-11-11-image-generation.md`](https://github.com/keenlok/clawdot-platform/blob/main/docs/plans/2026-11-11-image-generation.md)
passes. It confirms OpenAI model IDs, supported sizes, quality values and pricing; tests each size;
records cost and latency; checks refusal handling; and keeps configured caps within the project
budget.

## Limitations
Only one image can be generated per turn. The plugin supports three fixed sizes and does not edit
photos supplied by users. Bridge mode sends the PNG as a document. The OpenAI project budget is the
hard spending ceiling because local counters reset on restart.

## Verification
Enable the plugin with a dedicated OpenAI project key and a bounded project budget, then request a
picture explicitly. Confirm that the image arrives before the text reply and that `/image usage`
shows the updated local count.
