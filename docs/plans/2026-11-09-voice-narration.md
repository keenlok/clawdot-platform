# Voice narration plan

> **For agentic workers:** Implement only after explicit owner approval. Activation stays blocked until the qualification in "Activation gate" passes.

**Goal:** Let any bot read a passage it has written aloud, such as a news summary, as one Telegram voice message, without depending on tutor mode. Make the spending limits provider-aware by checking ElevenLabs' own usage figures before each paid request.

**Status (2026-11-09):** Implemented and disabled by default (see the implementation record at the end). The live activation gate hasn't been run.

**Architecture:** A framework speech client in core (`chatcore.tts`) owns the ElevenLabs HTTP call, MP3 checks and a provider usage reader. A new `voice` plugin owns the `narrate_text` tool, its budgets, gate and skill. The tutor plugin keeps its pronunciation tool and limits, and moves onto the shared client without changing behaviour. A news narration that combines web tools with `narrate_text` is an app composite. Downstream apps own keys, voices, caps and activation.

**Tech stack and dependencies:** Existing Python, HTTPX, Pydantic and python-telegram-bot. No new Python package, no FFmpeg requirement for narration (no slow pace), no migration in v1.

**Execution:** Claude plans; Codex executes and reviews; Claude does the final review (`AGENTS.md`, owner's computer with Codex CLI).

## Why the current speech path can't do this

The only speech tool today is the tutor's `pronounce_tutor_text`
([plan](2026-11-04-tutor-pronunciation-tts.md)):

- It accepts 1–120 characters of target-language text: a word or phrase.
- It has only Cantonese, Mandarin and Japanese voices.
- It is offered only inside the `language_tutor` skill, so it appears only when a turn routes to tutoring.
- Audio is capped at 2 MiB of 128 kbps MP3, about 2 minutes.
- Its budget is a process-local 5,000 characters per UTC day (`TUTOR_TTS_MAX_CHARS_PER_DAY`). That setting already accepts up to 1,000,000, so raising the tutor cap is a deployment change, not a code change.

That earlier plan also requires any later speech plugin to use **a distinct restricted key and a separate quota**. This plan follows that rule.

## Product contract and scope

- Narration happens only on an explicit request ("read it out", "voice note of the summary", "narrate the news"). Never narrate every reply automatically, and never because of a profile setting.
- The model writes the passage first, then calls `narrate_text` with exactly that text. The voice clip is sent inside the tool call, before the final text reply, as pronunciation clips are. The text reply still contains the full passage, so the content can be read without audio.
- One narration per turn, including failed attempts. No paired clips, no background or scheduled narration in v1, no STT, voice cloning or caching.
- Text: 1–`VOICE_TTS_MAX_CHARS_PER_REQUEST` characters (default 3,000, roughly 3–4 minutes of speech), with no control characters except ordinary whitespace. Reject over-long text; never truncate silently. The model is told to keep narrations short and summarise to fit.
- Language: one of the configured voice languages (v1: `English` required; others optional by setting). The tool takes an explicit `language` from the enum; no auto-detection.
- Caption: a model-supplied plain title of 1–120 characters, built into the caption in code (`🔊 <title>`). The caption stays under 1,024 UTF-16 code units.
- Direct Telegram only, through the existing `VoicePlatform.send_voice`. Bridge mode and other platforms hide the tool through the gate, as they do for pronunciation.

Out of scope for v1, recorded for later: scheduled narrated digests via `jobs`, a shared cross-process budget, persisting daily counters across restarts, and moving the tutor onto provider-usage checks.

## Provider usage integration

ElevenLabs exposes usage through these endpoints:

| Endpoint | What it gives | Use in v1 |
| --- | --- | --- |
| `GET /v1/user/subscription` | `character_count`, `character_limit` for the current billing period, `next_character_count_reset_unix`, `status`, `can_extend_character_limit`, `max_credit_limit_extension` (`0` = usage-based billing off, `"unlimited"` = no cap), `current_overage` | Preflight check and `/voice usage` report |
| `GET /v1/usage/character-stats` | Usage over a time range, with `breakdown_type` options including `api_keys`, `voice` and `model`, and metrics such as `credits` and `tts_characters` | Not used: the docs mark it deprecated in favour of `/v1/workspace/analytics/query/usage-by-product-over-time`, which isn't documented well enough yet |
| Per-key credit quota | A hard credit limit set on each API key | The real spending ceiling; set by the operator. No documented API reads a key's remaining quota |

Sources: [subscription](https://elevenlabs.io/docs/api-reference/user/subscription/get), [character stats](https://elevenlabs.io/docs/api-reference/usage/get), [API key restrictions](https://elevenlabs.io/docs/overview/administration/workspaces/api-keys).

The subscription figures cover the **whole account or workspace**, not one key, so they protect the shared plan allowance. The per-key credit quota is what stops one bot from spending another's allowance.

Usage reader (`chatcore.tts.ProviderUsage`):

- Fetch `/v1/user/subscription` with the same fixed origin, no redirects and no proxy inheritance as the speech call. 5-second timeout. Cache the parsed result for `VOICE_TTS_USAGE_CACHE_SECONDS` (default 60). After each successful generation, add the charged characters to the cached `character_count` locally, so a burst inside the cache window can't overshoot.
- Parse only the fields above into a frozen dataclass. Ignore invoices, payment and discount fields; never log the response body.
- Preflight rule. Refuse without a provider TTS call when any of these hold:
  - `status` is not `active`, `trialing` or `free`.
  - `character_limit - character_count - len(text) × model_multiplier < VOICE_TTS_PROVIDER_RESERVE_CHARS` (default 2,000). The reserve leaves headroom for the tutor and anything else on the account.
  - `VOICE_TTS_ALLOW_OVERAGE=false` (the default) and `can_extend_character_limit` is true with `max_credit_limit_extension != 0`, meaning the account could bill beyond its plan. The operator must turn overage off, or explicitly allow it.
- If the key can't read the endpoint (401/403) or the call fails, follow `VOICE_TTS_USAGE_CHECK`:
  - `required` (default): refuse narration with a safe "unavailable" error and log `voice usage check failed code=<status>`.
  - `advisory`: continue under the local caps only, and log the same line.
  - `off`: skip the check. Allowed only when the operator records that the per-key credit quota alone is the ceiling.
- `model_multiplier` is a fixed per-model table in code (credits charged per character). Its values are confirmed during qualification against ElevenLabs' pricing page, never guessed; an unknown model fails closed.

## Local budgets

Local caps stay as the first line of defence, because the provider check is cached and account-wide.

| Setting | Default | Notes |
| --- | --- | --- |
| `VOICE_TTS_ENABLED` | `false` | |
| `VOICE_ELEVENLABS_API_KEY` | empty `SecretStr` | Distinct restricted key with its own credit quota; needs TTS and user-read permission |
| `VOICE_TTS_VOICES` | empty | `English=<voice id>` pairs, comma-separated; voice IDs restricted to `[A-Za-z0-9_-]{1,128}` |
| `VOICE_TTS_MODEL` | `eleven_multilingual_v2` | From a fixed allowlist with known multipliers |
| `VOICE_TTS_MAX_CHARS_PER_REQUEST` | `3000` | 1–5,000 |
| `VOICE_TTS_MAX_CHARS_PER_USER_HOUR` | `10000` | Rolling hour, per user, across chats |
| `VOICE_TTS_MAX_CHARS_PER_DAY` | `50000` | UTC day, across all users; 0–10,000,000; 0 disables |
| `VOICE_TTS_USAGE_CHECK` | `required` | `required`, `advisory` or `off` |
| `VOICE_TTS_USAGE_CACHE_SECONDS` | `60` | 0–600 |
| `VOICE_TTS_PROVIDER_RESERVE_CHARS` | `2000` | 0–1,000,000 |
| `VOICE_TTS_ALLOW_OVERAGE` | `false` | |

- Reserve the request's characters against the user-hour and daily caps, plus a worker slot, under one async lock before any provider I/O, as `SpeechService` does now. Failed attempts keep their reservation; there are no retries or refunds.
- At most two active narrations per process; a third fails immediately as busy, without being charged.
- Counters are process-local and reset on restart. The provider preflight is what catches a restart that resets a local counter.

## Audio and delivery

- Output format `mp3_44100_64`. At 64 kbps, 3,000 characters (about 4 minutes) is roughly 2 MB.
- Streamed response cap `VOICE_MAX_AUDIO_BYTES` = 8 MiB, enforced while reading. Telegram's bot upload limit is far above that.
- Same MP3 validation as the tutor (`ID3` or MPEG frame sync; `audio/mpeg` or `audio/mp3` content type; reject empty, HTML or JSON bodies).
- HTTP connect timeout 5 s, request timeout 60 s, outer deadline 70 s (longer than pronunciation because the text is longer).
- Recheck access after generation and before sending. Delivery uses only `ctx.chat`, `ctx.thread_id` and `ctx.platform`.

## Code layout

**Phase 1: shared client in core (behaviour-neutral).**

- New `packages/core/src/chatcore/tts.py`: `ElevenLabsClient.synthesize(text, voice, model, language_code, output_format, max_bytes, timeout)`, `ProviderUsage` and the MP3 validation helpers, moved from `chatcore_tutor.audio` and `chatcore_tutor.speech`.
- `chatcore_tutor.speech.SpeechService` keeps its quotas, slow processing and settings, and calls the core client. `chatcore_tutor.audio` re-exports what it used to define. Keep the `chatcore_tutor` logger name.
- Proof of no behaviour change: the tutor test suite, the agent request snapshot and the skill catalog and routing-set tests all pass unchanged. Move test patch targets with the code (`AGENTS.md`, "Moving or splitting code").

**Phase 2: `voice` plugin.**

`packages/plugins/voice/src/chatcore_voice/`, following the plugin module layout:

- `settings.py`: `VoiceSettings` with the table above.
- `service.py`: `NarrationService`, holding budgets, the usage preflight and delivery.
- `permissions.py`: `may_narrate`, the same rules as the tutor's `may_pronounce` (refresh access; reject restrictions and namespace mismatch; `can_dm` for DMs; allowed chats for groups; fail closed on errors). It's duplicated rather than imported, because plugins don't import each other.
- `gates.py`: config and `VoicePlatform` gate, plus a user gate calling `may_narrate`.
- `plugin.py`: the `narrate_text` tool, the `/voice usage` command and the `read_aloud` skill.

`narrate_text` schema:

```json
{
  "type": "object",
  "properties": {
    "text": {"type": "string", "description": "Exactly the passage to read aloud; plain prose, no markdown or links; at most the configured limit."},
    "language": {"type": "string", "enum": ["<configured voice languages>"], "description": "Language of the passage."},
    "title": {"type": "string", "description": "Short plain title for the caption; 1–120 characters."}
  },
  "required": ["text", "language", "title"],
  "additionalProperties": false
}
```

The `read_aloud` skill: summary "Read a passage aloud as a voice message", keywords such as "read it out", "read aloud", "voice note", "narrate", "audio version", tool `narrate_text`, `max_steps=2`. Instructions (under 800 characters, written for any bot): narrate only on explicit request; write the passage first as plain spoken prose without markdown, URLs or tables; call `narrate_text` once with that exact text; keep it within the limit by summarising, never by cutting off mid-sentence; then reply with the same text.

`/voice usage` (owner and delegated admins only): today's local characters against the daily cap, plus the provider's `character_count` / `character_limit`, reset date and whether overage is possible. No key, voice ID or invoice data.

**Phase 3: news narration composite (downstream app).**

A `news_narration` composite in `apps/<app>/src/<app>/skills.py` combining `search_web`, `fetch_web_page` and `narrate_text`. It handles requests like "narrate today's news on X". Instructions: gather from a few sources, write a short spoken summary that names its sources in words, narrate it, then reply with the text plus links. The scaffold gets an example entry; the real app owns its version and prompt tests.

## Authorization

- Delivery is a response attachment (`writes=False`), the same policy approved for pronunciation clips. This plan asks the owner to approve that policy again for longer, model-composed text.
- `gates.py` and `permissions.py` are permission changes and are called out in the PR.
- The model chooses the text, so the explicit-request instruction is not a hard intent check. The one-per-turn limit, per-user hourly cap, daily cap and provider preflight bound the cost of a wrong call.

## Privacy

Update `ARCHITECTURE.md` §6a: narrated text, which may include summaries of user messages or fetched pages, goes to ElevenLabs. The audio comes back in memory and is uploaded to Telegram. The existing retention and training notes for ElevenLabs apply. The usage reader receives billing-adjacent account fields; it keeps only the parsed counters and never logs them with user identifiers.

## Activation gate

Before a downstream app sets `VOICE_TTS_ENABLED=true`, the operator:

1. Creates a dedicated restricted key with TTS and user-read permission, sets a per-key credit quota, and records the effective cap.
2. Turns off usage-based billing, or records an explicit decision to set `VOICE_TTS_ALLOW_OVERAGE=true`.
3. Calls `/v1/user/subscription` once with that key and confirms the fields above parse, including the `max_credit_limit_extension` value.
4. Runs at most three paid narrations (English, about 300, 1,500 and 3,000 characters) and records model, status, content type, bytes, duration, the characters the account was charged (from the before/after subscription counts) and a listening verdict. This confirms `model_multiplier`.
5. Records the training opt-out and retention arrangement, as for the tutor.

Missing credentials or a failed step keep activation blocked.

## Tasks

- [ ] Phase 1: extract `chatcore.tts`; tutor uses it with no behaviour change; snapshot, routing-set and tutor tests pass unchanged.
- [ ] `ProviderUsage` tests with a mock transport: parsing, caching, local increment after generation, status/overage/remaining refusals, 401/403/timeout under each `VOICE_TTS_USAGE_CHECK` mode, unknown model failing closed. Refusals make zero TTS calls.
- [ ] Phase 2: `voice` plugin, settings validation, budgets (rolling hour, UTC rollover, concurrency, busy without charge), MP3 and size caps, gate and handler-time permission tests, one-attempt-per-turn test.
- [ ] `/voice usage` command test, including the non-admin refusal and that no secrets appear.
- [ ] Skill catalog, `routing_set.jsonl` entries for narration phrasing, and request snapshot updates.
- [ ] Scaffold wiring (disabled by default) and an example `news_narration` composite with a prompt test.
- [ ] `ARCHITECTURE.md` §6a privacy note, `CHANGELOG.md` entry under Unreleased, wiki page for the plugin, plans index row update.

## Open questions for the owner

1. Is `required` the right default for the usage check, so narration stops whenever the key can't read the subscription?
2. Is a 3,000-character, roughly 4-minute ceiling per narration right, or should it be shorter?
3. Which languages besides English should get narration voices first?
4. Should a later phase move the tutor onto the same provider preflight, so both features respect one account reserve?

## Implementation record (2026-11-09)

**Execution change:** the owner chose `/implement-project`, so this plan was implemented and self-reviewed in a single Claude session instead of the Codex execute/review split above.

**Open questions, answered with the plan defaults** (the owner can change any of them through settings):

1. The usage check defaults to `required`.
2. Narrations are capped at 3,000 characters.
3. Languages come from a fixed allowlist (English, Mandarin Chinese, Japanese, Korean, Malay, Tamil, Indonesian, Hindi, French, German, Spanish). A language is offered only when `VOICE_TTS_VOICES` gives it a voice.
4. The tutor keeps its own limits and doesn't use the provider preflight yet.

**Acceptance criteria:**

- **AC1.** `chatcore.tts` holds the ElevenLabs speech request, MP3 validation and subscription reader. The tutor uses it, and its tests, logs and request shape stay unchanged.
- **AC2.** The `voice` plugin offers `narrate_text` only when enabled, keyed, voiced and on a voice-capable platform. This is independent of tutor mode.
- **AC3.** Text limits, the one-per-turn limit, the per-user-hour and daily caps and the two-job concurrency limit are enforced before any provider call.
- **AC4.** The provider preflight refuses on bad status, low remaining allowance or possible overage, and follows the `required`, `advisory` and `off` modes when the usage call fails. Refusals make no TTS call and don't use up local quota.
- **AC5.** Access is checked again at call time and before sending. The caption is built in code.
- **AC6.** `/voice usage` is admin-only and shows local and provider usage without secrets.
- **AC7.** The plugin has an explainer, a wiki page, a changelog entry, a §6a privacy note and an updated plans index row.

**Plan changes found during implementation:**

- **No composite.** `search_web` and `fetch_web_page` are already in `DEFAULT_GENERAL_TOOLS`, which is offered on every routed turn. So the `read_aloud` skill alone can gather news and narrate it, and the Phase 3 composite isn't needed. Downstream apps can still add one.
- **No scaffold wiring.** The scaffold wires no optional plugins (not even the tutor), so the voice plugin isn't wired there either.
- **No routing-set update.** `routing_set.jsonl` lives downstream, not in this repository.
- **Conservative multiplier.** Every allowed model uses a multiplier of 1.0 credit per character. That overstates Flash/Turbo spend, which errs on the safe side, until qualification measures the real value.
- **`language_code` only where supported.** It is sent only for models that accept it (`eleven_flash_v2_5`, `eleven_turbo_v2_5`). `eleven_multilingual_v2` picks the language from the text.

**Self-review (single session, not independent):** PASS after one repair.

- **F10, fixed.** The skill's Chinese example "读给我听" is a tutor pronunciation keyword, and the generic 语音/語音 keywords would have pulled pronunciation requests into narration. The example was replaced and those keywords dropped.
- **Mutation check.** Making `chatcore.tts.synthesize` raise fails the tutor request-shape tests, which proves the tutor uses the shared path.
- **Known limitation.** Provider counters can lag recent requests. The reserve margin and local caps cover that gap; the per-key credit quota remains the hard ceiling.
- **Not run.** No live ElevenLabs call has been made (that's the activation gate). Postgres integration tests were skipped locally.
