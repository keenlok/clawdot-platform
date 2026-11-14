+++
topic = "image"
title = "Image generation"
summary = "Creates one requested image from a text description and sends it to the chat."
aliases = ["illustration"]
+++

## Overview
The image plugin creates one image from a description when someone explicitly asks for one. It
sends the image before the bot's short text reply. The plugin's setup default is off, and an
operator must configure an OpenAI API key. That default is not this deployment's status: the
runtime availability lines report whether it is available here and now.

## Workflow
The skill is offered only when generation is configured and the platform can send a photo or file.
With photo editing on, a photo the person may edit (sent with the message, or directly replied to)
makes the skill active whatever the message says, so the wording of an edit request does not
matter. Without such a photo, a short follow-up activates it only as a reply to the requester's own
earlier image request. Activation offers the tool; it never generates by itself. A photo alone, a
question about it, a translation request or thanks gets a normal answer, and the bot creates an
image only when the current message asks for one.
The model writes a self-contained prompt and chooses square, portrait or landscape. The plugin
generates one PNG and sends it with a short caption. `/image usage` shows today's local count.

## Storage
Prompts are sent to OpenAI and the generated PNG is held in memory for upload to the chat. The
plugin does not retain prompts or cache images. Usage counters are process local and reset when the
bot restarts. With editing enabled and a receipt-capable platform (direct Telegram), the bot
retains the delivered photo's platform file reference and requester attribution in message history.
This supports replies and successive edits across restarts; image bytes are not cached. Normal
history retention and requester erasure remove these references.

## Safety
Access is checked before generation and again before delivery. Prompt length, daily per user and
global caps, a two request concurrency limit and output size are enforced locally. Generation
attempts consume their reservation even when the provider fails. OpenAI handles submitted prompts
under the operator's account terms and settings.

### Settings
| Setting | Default |
| --- | --- |
| `IMAGE_ENABLED` | `false` |
| `IMAGE_EDIT_ENABLED` | `false` |
| `IMAGE_OPENAI_API_KEY` | empty |
| `IMAGE_OPENAI_BASE_URL` | `https://api.openai.com/v1` |
| `IMAGE_MODEL` | `gpt-image-2.5-flare` |
| `IMAGE_QUALITY` | `medium` |
| `IMAGE_MODERATION` | `auto` |
| `IMAGE_MAX_PROMPT_CHARS` | `2000` |
| `IMAGE_MAX_PER_USER_DAY` | `10` |
| `IMAGE_MAX_PER_DAY` | `50` |

### Reference photos
When enabled, the image tool can create an edited image from a photo the person explicitly
sends with the request or explicitly replies to. Any authorized group member may edit an explicitly
replied-to photo in the same chat/topic, whoever uploaded or generated it. Other members' photos
reached only through text-reply mappings or history cannot be used.
The photo and prompt are sent to OpenAI, and edits share the generation caps. Replying to a
generated photo edits that version and preserves requester attribution for retention and erasure.
Older untracked outputs and platforms without photo receipts require uploading the photo again.
One photo is used per edit. A new photo sent as a reply to a text message does not bring in an
earlier generated image, and photos elsewhere in the history are never picked up. The bot may
decline an edit for its content, such as putting a real person in a sexual, violent, humiliating or
deceptive scene; that is a content decision, not a sign that image generation is missing.

### Activation gate
Activation stays blocked until the qualification in
[`docs/plans/2026-11-11-image-generation.md`](https://github.com/keenlok/clawdot-platform/blob/main/docs/plans/2026-11-11-image-generation.md)
passes. It confirms OpenAI model IDs, supported sizes, quality values and pricing; tests each size;
records cost and latency; checks refusal handling; and keeps configured caps within the project
budget.

## Limitations
Only one image can be generated per turn. Daily limits, busy and provider errors are reported only
by an actual attempt; a turn that never attempted an image did not hit them. The plugin supports three fixed sizes. Bridge mode sends the PNG as a document. The OpenAI project budget is the
hard spending ceiling because local counters reset on restart.

## Verification
Enable the plugin with a dedicated OpenAI project key and a bounded project budget, then request a
picture explicitly. Confirm that the image arrives before the text reply and that `/image usage`
shows the updated local count.
