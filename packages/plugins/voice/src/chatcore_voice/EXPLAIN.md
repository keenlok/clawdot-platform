+++
topic = "voice"
title = "Voice narration"
summary = "Reads a passage the bot has written aloud as one voice message, such as a news summary."
aliases = ["narration", "read_aloud"]
+++

## Overview
When the person explicitly asks to hear something, the bot writes the passage as plain spoken prose,
sends it once as a voice message, then replies with the same text. It works in any routed turn and
does not depend on tutor mode. Narration is disabled by default and needs an operator-configured
ElevenLabs key and at least one voice.

## Workflow
The read-aloud skill offers the narration tool together with the general web tools, so a request
such as "narrate today's news" can gather sources, write a short summary and narrate it. The tool
takes the exact passage, its language and a short caption title. One narration is attempted per
turn, and the voice message arrives before the text reply. Admins can run `/voice usage` to see
today's narration characters and the provider's billing-period usage.

## Storage
Narrated text is sent to ElevenLabs. The returned audio stays in memory and is uploaded to the
chat platform; the plugin keeps no audio cache. Usage counters are process-local and reset when the
bot restarts; the provider's own usage figures are read before each narration to cover that gap.

## Safety
Access is checked when the tool runs and again before sending. Per-user hourly and daily character
caps, a two-narration concurrency limit and a per-narration length limit are enforced before any
provider call. Before each paid request the plugin reads the account's billing-period usage and
refuses when the account is inactive, the remaining allowance would fall below a reserve, or
overage billing is possible without operator approval. A per-key credit quota set by the operator
remains the hard spending ceiling. ElevenLabs retains submitted data according to the account's
terms and settings.

## Limitations
Narrations are model-written summaries and can be wrong or incomplete; check the text reply and its
links. Voices and pronunciation depend on the configured provider voice. Long passages are refused
rather than truncated, so the model has to summarise to fit. Usage limits are per process, and the
provider usage figures cover the whole account rather than one key.

## Verification
Ask for a short passage to be read aloud and compare the voice message with the text reply. Use
`/voice usage` as an admin to confirm the counters moved and the provider usage is readable.
