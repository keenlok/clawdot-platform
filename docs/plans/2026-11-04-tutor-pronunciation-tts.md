# Tutor pronunciation TTS framework plan

> **For agentic workers:** Implement only after explicit owner approval; preserve the live qualification gate before activation.

**Goal:** Generate one on-demand Cantonese, Mandarin Chinese or Japanese pronunciation clip at normal speed; provide a slower clip only when the learner explicitly asks.

**Status:** Implementation approved 2026-11-04 after owner review of the revised plan. The owner explicitly authorized implementation using mocks and synthetic audio, superseding the live-qualification prerequisite for coding. The feature remains disabled by default. Live provider/account/audio qualification below remains a prerequisite to activation; this approval does not satisfy it or enable TTS.

**Architecture:** The tutor plugin owns the provider client, usage limits, optional local slowdown and tool instructions. A small optional voice protocol supports direct Telegram delivery. Downstream applications own their wiring, image dependencies, credentials, platform release pins and activation.

**Tech stack and dependencies:** Existing Python, HTTPX, Pydantic and python-telegram-bot; no new Python package. Slower playback requires the FFmpeg system binary with MP3 encoding and the `atempo` filter. That is a new runtime dependency and must be declared in implementation PRs and supplied by downstream images. No migration, persistent audio cache or protected-bridge changes.

## Product contract and scope

- Explicit human requests for pronunciation/listening generate one normal-speed clip. `slow` is selected only for an explicit slower request, including a follow-up referring to the previous phrase; never infer slow from a profile, level or mistakes.
- Select exactly `Cantonese`, `Mandarin Chinese` or `Japanese`, mapping to `yue`, `zh` or `ja`. Validate canonical names at handler time; catalog aliases are not accepted on this tool path. Never auto-detect Cantonese versus Mandarin from characters.
- One clip per turn, including failed generation/delivery attempts. No paired clips, automatic reading of all replies, STT, pronunciation scoring, voice cloning or cross-user caching.
- The caption is plain text: target text, newline, its pronunciation aid, newline, `Normal` or `Slow`. Use Jyutping with tone numbers, Pinyin with tone marks, or Japanese kana readings. The tutor's final text can add translation/explanation and existing romanisation without speaking those additions.
- Bound target text to 120 Unicode characters and pronunciation aid to 240; reject empty/invalid strings and control characters except ordinary whitespace. Construct the caption in code from these fields, not an arbitrary model-supplied caption. Require at most 1,024 UTF-16 code units as well as Telegram's character limit; do not silently truncate.
- The voice is sent inside the tool call and arrives before the final tutor text. V1 preserves the exact current chat/topic but does not create a native reply to the learner's message, matching the existing file-delivery pattern. This is an explicit v1 limitation: no reply-chain attribution guarantee for the clip itself. Do not widen `ToolContext` solely for reply threading in this version.
- Direct Telegram only. Leave `BridgeTelegramPlatform`, release-control operations and protected transport allowlists unchanged. The optional protocol gate hides pronunciation in bridge mode and other unsupported platforms. A later bridge plan can add it.

## Provider evidence and slower-speech decision

The [model reference](https://elevenlabs.io/docs/overview/models#eleven-v3) documents v3's three languages and the `yue` code. The [speech endpoint](https://elevenlabs.io/docs/api-reference/text-to-speech/convert) returns audio from text and a selected voice. However, the [product guide](https://elevenlabs.io/docs/eleven-creative/playground/text-to-speech#speed) explicitly excludes v3 from speed control. Do not send `voice_settings.speed: 0.75` or present an accepted but ignored field as evidence of slow speech.

Use `eleven_v3` for normal synthesis, fixed `mp3_44100_128` output and explicit language code. A slow request synthesizes normal audio once, then applies local `atempo=0.75` before sending. Normal requests bypass FFmpeg. The fixed tempo filter changes tempo rather than deliberately changing sample rate/pitch; actual preservation of tones and pronunciation remains a listening-test requirement. [FFmpeg filter documentation](https://ffmpeg.org/ffmpeg-filters.html#atempo)

Alternatives considered: a provider-native slow model would avoid the binary but is not qualified for all three languages; changing providers requires a separately approved provider choice. V3 speed parameters and audio-tag suggestions are not reliable speed controls. Local slowdown is the specified candidate, subject to the following gate.

### Live qualification gate before activation

Use an operator-supplied restricted key and selected voices for a deliberately small paid trial: normal v3 `你好` in Cantonese and Mandarin, plus `こんにちは` in Japanese; then locally slow each returned clip. Allow at most six provider requests and 720 input characters in total, including up to three context-sensitive phrases, with no automatic retries. No application code is required for this one-off qualification. Do not retrieve secrets from unrelated projects or publish raw account/voice identifiers in the qualification record.

Record date, model, requested language, HTTP status/content type, detected MP3 signature, normal/slow durations and listening verdict. Confirm `yue` actually sounds Cantonese; distinguish Mandarin; check Japanese reading. Slow output must be approximately 1/0.75 times the normal duration (within 10% allowing MP3 padding), contain the same words, and preserve recognisable tones/pitch and intelligibility. A qualified listener must audition real context-sensitive phrases, not infer quality from duration or HTTP 200 alone. Check selected voice access and model availability for the account.

**Evidence state:** no live trial, account setting check or pronunciation audition has been performed. The implementation may be built against mocks and synthetic audio, but it must remain disabled until the record passes. If either provider language selection or local slowdown fails, revise the chosen approach and obtain approval; do not silently omit slow support or substitute a provider. Missing credentials are a reason to keep activation blocked, not to claim success.

## Configuration, cost and privacy

Add to `TutorSettings`:

| Setting | Default / interpretation |
| --- | --- |
| `TUTOR_TTS_ENABLED` | `false` |
| `ELEVENLABS_API_KEY` | Empty `SecretStr`; a restricted key dedicated to this tutor deployment |
| `TUTOR_TTS_CANTONESE_VOICE_ID` | Empty; required for that language |
| `TUTOR_TTS_MANDARIN_VOICE_ID` | Empty; required for that language |
| `TUTOR_TTS_JAPANESE_VOICE_ID` | Empty; required for that language |
| `TUTOR_TTS_MAX_CHARS_PER_DAY` | `5000`; integer 0–1,000,000; zero disables generation |

The provider-wide key name is retained for familiarity, but a later plugin must receive a distinct restricted key and separate quota unless shared credentials/budgets are explicitly designed and approved. Do not assume future plugins share this service's accounting.

- Fixed HTTPS origin `api.elevenlabs.io`; no redirects or environment proxy inheritance; voice IDs restricted to 1–128 ASCII letters, digits, underscores or hyphens. No endpoint, model, recipient or voice override in tool arguments.
- Atomically reserve one invocation, its text length and a worker slot before provider I/O. One valid authorized handler attempt per `ctx.turn`; use a real `TurnState.tool_attempts: dict[str, int]` field, not dynamic attributes on the slotted dataclass. Failed provider/delivery attempts consume that turn allowance and reserved usage; no automatic retry/refund.
- Per process, at most 20 provider attempts per user per rolling hour across chats; at most `TUTOR_TTS_MAX_CHARS_PER_DAY` target characters across all users, chats, languages and paces per UTC calendar day. Reserve with an async lock. Reject a request that would exceed either quota without a provider call; no partial generation/truncation. A UTC date change resets the character counter; user-hour expiry uses a monotonic clock.
- Retain at most 10,000 user-hour entries, evict expired entries, and reject new users if still full. Limits are process-local and reset on restart; multiple processes need a separate shared-budget design. These are not a hard account spending ceiling.
- At most two active generation/slowdown jobs. A third request fails immediately with a safe busy message; no waiting queue and no quota charge for that busy rejection. Reserve slot/quota atomically so concurrent requests cannot oversubscribe. Release slots in `finally`, including cancellation.
- Before activation, the operator must set a hard provider key/account credit allowance where supported and disable paid overage/automatic credit top-ups for this trial. Record the actual effective cap; alerts alone are not a spending limit. If a hard limit cannot be established, keep activation blocked and bring that constraint to the owner.
- HTTP connect timeout 5 seconds, request timeout 30 seconds, outer generation deadline 35 seconds, streamed response cap 2 MiB regardless of headers. Accept nonempty `audio/mpeg` or `audio/mp3`, with either `ID3` or valid MPEG frame-sync signature; cover both. Reject empty, HTML/JSON, redirects and malformed responses without exposing provider bodies.
- Slow processing: a separate fixed 10-second deadline, 2 MiB input/output caps, fixed MP3 demux/encoder and filter, one thread, no shell, user paths, network protocols or writable audio files. Invoke FFmpeg with fixed argv, `-protocol_whitelist pipe`, `-f mp3 -i pipe:0`, `-map 0:a:0`, `-vn`, `-af atempo=0.75`, `-threads 1`, `-filter_threads 1`, `-codec:a libmp3lame`, `-b:a 128k`, `-f mp3 pipe:1`. Bound stdout while reading and feed stdin concurrently; discard stderr. On timeout, cancellation, overflow or invalid output, kill and await the child. Validate executable availability before paying for a slow request; return an unavailable error if missing. Keep a normal clip unsent if slow transformation fails.
- Safe error codes/messages only; no keys, target text, pronunciation strings, raw audio, response bodies or raw exception text in logs/model diagnostics. Audio remains request-only in memory; no cross-turn caching in v1.

Update `ARCHITECTURE.md` §6a “Data and privacy”: target text goes to ElevenLabs, audio returns and is then uploaded to Telegram; pronunciation annotations stay local. ElevenLabs retains data by default; model-training use depends on account terms/settings, with an opt-out for future submissions. Zero Retention Mode is an eligible Enterprise feature, not an ordinary default; do not claim that local deletion prevents provider retention. The operator must record the current training opt-out and retention arrangement before activation. Document no guaranteed ordinary-plan retention period where none is specified. [Retention](https://elevenlabs.io/docs/eleven-api/resources/zero-retention-mode), [training controls](https://elevenlabs.io/docs/help-center/legal/is-my-data-used-to-improve-eleven-labs-ai-models)

## Authorization and tool interface

Treat delivery as a response attachment (`writes=False`) following expense export, requiring explicit approval of that policy in this plan. No stored setting changes or click per clip. Config/platform gates live in new tutor `gates.py`; handler checks remain in `permissions.py`. At call time refresh access; reject restrictions, namespace mismatch and disallowed groups; use existing `can_dm` rules for DMs. Fail closed on access errors and recheck before sending after generation.

Use only `ctx.chat`, `ctx.thread_id` and `ctx.platform` for delivery. The current context lacks raw human request text, so model-directed on-demand instructions are not a deterministic intent verifier. The one-attempt turn limit and user/global budgets bound this gap. Keep plugin-owned instructions at most 800 characters and preserve current tutor behaviours.

`pronounce_tutor_text` JSON schema:

```json
{
  "type": "object",
  "properties": {
    "text": {"type": "string", "description": "Target-language word or short phrase only; 1–120 characters."},
    "language": {"type": "string", "enum": ["Cantonese", "Mandarin Chinese", "Japanese"], "description": "Explicit pronunciation language; Cantonese and Mandarin must be selected separately."},
    "pronunciation": {"type": "string", "description": "Caption pronunciation aid: Jyutping with tones, Pinyin with tones, or Japanese kana; 1–240 characters. Not spoken or sent to the provider."},
    "pace": {"type": "string", "enum": ["normal", "slow"], "description": "Defaults to normal. Use slow only when the learner explicitly asks; one clip per turn."}
  },
  "required": ["text", "language", "pronunciation"],
  "additionalProperties": false
}
```

## Approved implementation tasks

### 1. Optional delivery protocol and turn accounting

**Modify:** `packages/core/src/chatcore/files.py`, `packages/core/src/chatcore/testing.py`, `packages/core/src/chatcore/tools/registry.py`.
**Create test:** `packages/core/tests/test_voice_platform.py`.

- [x] Add failing tests for optional protocol detection, recorded bytes/caption/topic, and distinct attempt counters across turns.
- [x] Add `VoicePlatform.send_voice(chat, filename, data, *, caption=None, thread_id=None) -> MessageRef` as a runtime-checkable optional protocol. Do not widen required `ChatPlatform` or change existing file/text records.
- [x] Add the turn counter map and a dedicated fake voice list. Run focused core tests and request snapshots; verify counters are neither persisted nor sent to the model.

### 2. Direct Telegram adapter

**Modify:** `packages/adapters/telegram/src/chatcore_telegram/__init__.py`, `packages/adapters/telegram/tests/test_telegram.py`.
**Regression test:** `packages/adapters/telegram/tests/test_bridge_adapter.py`.

- [x] Add tests for MP3 byte upload to `bot.send_voice`, MIME type, exact chat/topic/caption, invalid filename/data/caption bounds and safe delivery failures.
- [x] Implement direct voice delivery using established adapter conventions without changing logger names. Do not add blind retries on ambiguous sends.
- [x] Assert `BridgeTelegramPlatform` still lacks `VoicePlatform`; no protected transport code changes. MP3 voice messages are supported by [Telegram](https://core.telegram.org/bots/api#sendvoice); waveform display may differ by client.

### 3. Provider service and bounded slowdown

**Modify:** `packages/plugins/tutor/src/chatcore_tutor/settings.py`.
**Create:** `packages/plugins/tutor/src/chatcore_tutor/speech.py`, `packages/plugins/tutor/src/chatcore_tutor/slow_audio.py`, `packages/plugins/tutor/tests/test_speech.py`, `packages/plugins/tutor/tests/test_slow_audio.py`.

- [x] Write mock-transport tests for three explicit codes/voices, model/MP3 format/auth, and absence of a v3 speed parameter. Check missing configuration, canonical language validation, input and stream bounds, both MP3 signatures, safe 401/429/500/timeout errors and no retries.
- [x] Test atomic per-user and aggregate UTC-day reservations with an injected monotonic/UTC clock, midnight rollover, quota exhaustion, bounded user-map expiry and fail-fast two-job concurrency. Quota/busy rejections must make zero provider calls.
- [x] Implement one owned HTTP client with `aclose()` and safe error codes. Normal requests never invoke the processor; slow requests call the processor after exactly one provider request.
- [x] Test the processor boundary with a fake child for stdout overflow, timeout, cancellation, missing executable and kill/reap behaviour. Add an integration-marked real-FFmpeg test using synthetic speech-like audio to compare duration and pitch; clearly skip if the binary is absent. Synthetic tests do not replace the live linguistic qualification.

### 4. Tutor tool, gates and documentation

**Modify:** `packages/plugins/tutor/src/chatcore_tutor/plugin.py`, `packages/plugins/tutor/src/chatcore_tutor/permissions.py`, `packages/plugins/tutor/tests/test_tutor.py`, `ARCHITECTURE.md`, `CHANGELOG.md`, `docs/plans/README.md`.
**Create:** `packages/plugins/tutor/src/chatcore_tutor/gates.py`, `packages/plugins/tutor/tests/test_pronunciation.py`.

- [x] Keep `TutorPlugin(access, platform)` compatible with optional keyword settings, owned service and `aclose`. Test closing the client and disabled-feature behaviour.
- [x] Add the tool to `language_tutor`, pronunciation examples/keywords, argument descriptions and concise plugin-owned instructions. Add `tool_gates()` returning only `build_gates(self)`.
- [x] Test real-registry handler validation, one valid attempt per turn including failures, authorization/revocation, no provider call on denial, exact current-topic delivery, captions, unsupported bridge mode and truthful errors. A repeated tool invocation must not send/pay for a second clip.
- [x] Document delivery ordering/no native reply, bounds, processor requirements, privacy in §6a and configuration/API impact in the changelog. Call out core, adapter `__init__.py`, gates/permissions and system dependency for maintenance review. No applied migration edits.
- [x] Keep application routing-set changes and deployment code out of this repository. Update this plan's index/status with the actual implementation PR and activation-independent framework state.

## Verification and handoff

Run focused core/tutor/adapter tests, package boundaries, plan/link tests and unchanged disabled-feature request snapshots, then the required commands:

```sh
uv sync --locked --all-packages
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check .
uv run pytest -q -m "not browser and not sandbox_integration"
```

Format only changed files; report baseline/unavailable-service failures. No new Python dependency or migration is intended; disclose the FFmpeg binary explicitly. Downstream applications select a reviewed release and verify their own image contains the required encoder/filter. This plan does not authorize changing an application's pin, secrets, hosting or deployment.

## Review provenance and date

Codex revised this generic plan and self-reviewed it after feedback on the original integration draft. The installed Claude CLI was retried on 2026-11-04 but still reports expired OAuth; the documented unavailable-model fallback applies. The product decisions preserve normal-by-default and slow-only-on-request. The date uses Asia/Singapore; 2026-11-03 17:30 UTC is 2026-11-04 01:30 there.


## Implementation and verification record (2026-11-04)

Implemented the optional voice protocol and fake records, direct Telegram MP3 delivery, per-turn
attempt accounting, disabled-by-default tutor tool, refreshed authorization gates, bounded
ElevenLabs client and local pitch-preserving slow processor. Partial language configuration is
supported. Normal requests bypass FFmpeg; explicit slow requests keep one job slot through
synthesis and processing. Protected bridge delivery remains unsupported. No Python dependency,
applied migration, credential, provider call, release or deployment was added.

Codex (gpt-6-luna) executed; independent Codex (gpt-6.1-sol) reviewed. Review fixes cover HTTP
format placement, bounds and Unicode validation, fail-closed authorization and delivery context,
quota/concurrency handling, cancellation-safe child cleanup, route keywords, annotation length
and the processor's single deadline. The parent completed final verification and self-review.
Claude final review could not run because its CLI OAuth refresh remains unavailable; the repository's
unavailable-model fallback applies.

Verification: locked workspace sync and full Ruff lint passed. Full tests passed with an isolated
Postgres database: **3,694 passed, 3 skipped, 26 deselected**. Focused feature, adapter, routing,
package-boundary and unchanged disabled snapshot checks passed (326 passed, 1 skipped). Changed
Python files pass formatting; the repository-wide formatting check still lists 14 unrelated
pre-existing Markdown files. Gitleaks found no leaks in tracked/new nonignored source.

All 17 framework wheels were independently built and installed into the private app candidate
integration environment. The downstream bot image exercised this exact processor and wheel set
with synthetic audio: duration ratio 1.346, normal 440.3 Hz and slowed 440.1 Hz. This confirms
container codec/filter behavior, not linguistic quality. Live provider, native-listener and account
privacy/spending qualification remains pending before activation. Implementation is in review in
[PR #13](https://github.com/keenlok/clawdot-platform/pull/13); private integration is
[PR #10](https://github.com/keenlok/clawdot-deployments/pull/10).


### Activation authorization and release preparation

The owner requested “enable” after implementation review on 2026-11-04. This authorizes the
reviewed release, downstream pin/configuration and deployment work, without another activation
permission prompt. Credentials and selected voices must still be supplied and linguistic/account
qualification remains unperformed. Prepare v0.3.0 through the existing CI-gated immutable-tag job;
do not enable a nonfunctional tool or invent a released pin. The failed PR CI check was reproduced:
the tutor's generated wiki page was stale. Regenerating the maintained page fixes the exact
`generate_explainers.py --check` failure without changing runtime code.
