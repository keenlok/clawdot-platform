# Voice input

Status: Planned; owner asked on 2026-11-23. Claude planned. Codex (`gpt-6-luna`) executes, Codex
(`gpt-6.1-sol`) reviews, Claude does the final review. Follows [Voice narration](2026-11-09-voice-narration.md),
which listed STT as out of scope. Needs the `Skill.final_tools` reservation from
[Step budget follow-ups](2026-11-23-step-budget-followups.md) part 2, so it lands after that.

## Goal

The bot understands voice notes and answers them by voice. A voice note to the bot is transcribed
and handled as if the person had typed it. The bot replies with a voice note and the same text,
unless the chat has switched voice replies off. In groups, people address the bot by replying to
it, by replying to someone's voice note with a mention, or, if a group admin turns it on, by
starting the voice note with a wake word.

## Decisions (owner, 2026-11-23)

1. **STT provider: ElevenLabs Scribe on the existing account**, with its own restricted key
   (speech-to-text permission only) and its own credit quota. It is separate from the narration
   and tutor keys.
2. **Wake word: included, set per group, off by default.** A group admin sets the phrase and its
   alternates.
3. **Voice replies are on by default, with a per-chat off switch.** They only work when narration
   is configured.

## How a voice note is handled

### When the bot transcribes

| Situation | Transcribed? |
| --- | --- |
| DM voice note | Yes |
| Group voice note replying to a bot message | Yes |
| Group text message with a mention, replying to a human's voice note ("@bot what did they say?") | Yes, the replied-to note |
| Group voice note, wake word on for that group | Yes, then the wake word is checked |
| Any other group voice note | No. History shows `[voice message, 0:14]` |
| Forwarded voice note | Only when someone addresses the bot about it |
| Music or audio files (`audio`, not `voice`) | No, unless someone addresses the bot about one |

People with a restriction (`access.restriction_for`) never cause a transcription. They get the usual
notice.

### Flow

1. The Telegram adapter maps `voice` to the new `AttachmentKind.VOICE` and `video_note` to the new
   `AttachmentKind.VIDEO_NOTE`, and fills the new `Attachment.duration_s` from Telegram's
   `duration`. `audio` stays `AUDIO`.
2. A new plugin hook runs in `BotApp._on_message` after `_allowed` and the restriction check, and
   before `log_message`: `async def transform_incoming(self, message, app) -> IncomingMessage`.
   The voice plugin implements it. Other plugins return the message unchanged. One plugin's error
   is logged by class name and the original message carries on.
3. When the table above says to transcribe, the voice plugin does the following:
   - checks the caps, then downloads the audio (at most 20 MB);
   - sends it to Scribe;
   - returns `dataclasses.replace(message, text=<transcript>, voice=VoiceNote(...))`.

   `VoiceNote` is a new frozen dataclass in `chatcore.events` with the fields `transcript`,
   `language`, `duration_s`, `source` (`"self"` or `"replied"`) and `wake_word` (bool).
   `IncomingMessage` gets `voice: VoiceNote | None = None`, appended as its last field.
4. **Wake word.** The match only looks at the first five words, normalised: casefolded, Unicode
   NFKC, punctuation removed, and compared with the spaces kept and also removed, so "claw dot"
   matches "clawdot".
   - **Match:** strip the wake word, set `mentions_bot=True` and continue.
   - **No match:** return the original message with its empty text. Throw the transcript away.
     Never store, log or add it to ambient history. Its seconds still count against the caps.
   - **Cost limit:** in wake-word mode, skip notes longer than `VOICE_STT_WAKE_MAX_SECONDS`
     without transcribing them.
   - **Accuracy:** pass the wake words to Scribe as key terms when the API supports it, so a
     made-up name is transcribed correctly.
5. Routing then runs on the transformed message, and `decide()` is unchanged. The logged message
   keeps the transcript as its text, so later turns, compaction and `read_chat_history` see words.
   Each note is transcribed once.
6. **The agent.** When `message.voice` is set, the agent does the following:
   - wraps the transcript as `<untrusted-voice-transcript>`, using the existing `_wrap`;
   - adds one line saying the person sent a voice note and that the transcript is automatic and
     may contain mistakes;
   - puts the replied-to transcript (`source="replied"`) next to the request, the way a replied-to
     image is placed.

   With `voice` unset, nothing changes, so the request snapshot stays the same.

### Voice replies

- When `message.voice` is set and the chat's voice reply setting is on, the `read_aloud` skill's
  `pin` returns true. Its instructions get a voice-turn addition: write a short spoken version for
  `narrate_text`, in the transcript's language, then the full text reply. Links, tables and code
  stay in the text only.
- `read_aloud` already declares `final_tools=("narrate_text",)` (step budget plan), so the voice
  reply survives a busy turn.
- The model skips the voice reply when the answer is mostly a table, code or links. When
  narration refuses (caps, length, provider usage check), the text reply still goes out, with one
  short line saying why there's no audio.
- Spending, caps and the ElevenLabs usage preflight are narration's, unchanged. Voice replies share
  narration's budget.
- A voice note that says "text only" or "say it out loud" overrides the setting for that reply.
  The model handles this through the instructions; no parsing is needed.

### Feedback while it works

- Add an optional `ChatActivityPlatform` protocol in `chatcore.platform` with `react(message, emoji)`
  and `activity(chat, kind, thread_id)`, where `kind` is `"typing"`, `"record_voice"` or
  `"upload_voice"`. Telegram implements it with `setMessageReaction` and `sendChatAction`.
  Platforms without it are skipped.
- When the bot is about to transcribe, it reacts 👂 to the note. It shows `record_voice` while
  generating narration.
- In groups the text reply starts with the transcript in a collapsed quote (`🎙 heard: …`), so people
  can catch mishearings. In DMs this is off by default. It's a per-chat setting.

### Failure messages

These are short and say what to do next. They aren't sent for wake-word misses, which stay silent.

- Too long: "That's 4:12. I can take voice notes up to 2:00. Can you split it or type it?"
- Can't hear it: "I couldn't make that out. Try again closer to the mic, or type it."
- Quota used up: "I've hit today's voice-note limit. Can you type it?"
- STT not configured: the bot treats the note as before, with no reply.
- Provider error: "Voice notes aren't working right now. Can you type it?" Only the class and status
  code are logged.

## Settings

### Operator (`VoiceSettings`, environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `VOICE_STT_ENABLED` | `false` | Master switch |
| `VOICE_STT_ELEVENLABS_API_KEY` | empty | Restricted key, speech-to-text only |
| `VOICE_STT_MODEL` | `scribe_v2` | Validated against an allowlist (`scribe_v1`, `scribe_v2`); check current ids in the ElevenLabs docs |
| `VOICE_STT_MAX_SECONDS_PER_NOTE` | `120` | Longer notes are refused before download |
| `VOICE_STT_MAX_SECONDS_PER_USER_DAY` | `1800` | Per person |
| `VOICE_STT_MAX_SECONDS_PER_DAY` | `14400` | Whole bot |
| `VOICE_STT_WAKE_MAX_SECONDS` | `60` | Longest note checked for a wake word |
| `VOICE_STT_USAGE_CHECK` | `advisory` | `required` / `advisory` / `off`: reads `/v1/user/subscription` and refuses when the account is inactive. STT usage may not be reported there, so the seconds caps and the key quota are the limits |

The counters are process-local, like narration's. The caps are checked before download, using
`duration_s`. When `duration_s` is missing, the bot downloads and treats the byte size as an
estimate: 16 kB is about one second for Opus voice notes.

### Per chat (new `voice_chat_settings` table)

Migration `packages/plugins/voice/src/chatcore_voice/migrations/0001_voice_chat_settings.sql`, with
`migrations_package` declared on the plugin:

```sql
CREATE TABLE voice_chat_settings (
    chat_key text PRIMARY KEY,
    transcribe boolean NOT NULL DEFAULT true,
    wake_word_enabled boolean NOT NULL DEFAULT false,
    wake_words text[] NOT NULL DEFAULT '{}',
    voice_reply boolean NOT NULL DEFAULT true,
    show_transcript boolean,           -- null: on in groups, off in DMs
    updated_by text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

When a chat has no row, the defaults apply.

**Who can change them:**

- In a DM, the person.
- In a group, a group admin (`ChatPlatform.is_admin` or the existing admin check). The operator
  can always change them.
- These are permission changes: they go in `gates.py` and `permissions.py` and are called out in
  the PR.

**How:**

- `/voice` shows the current settings with on/off buttons, using the existing `confirm`/button
  support where the platform has it, and plain text commands otherwise.
- `/voice reply on|off`, `/voice transcribe on|off` and `/voice transcript on|off`.
- `/voice wakeword "hey dot" [alt …] | off`. At most 5 phrases of 1–4 words, 40 characters each.
- A gated tool `voice_settings` lets people change these in plain language ("stop replying in
  voice", "set the wake word to hey dot"). It runs the same permission checks as the commands.
- **Wake-word notice.** When someone turns the wake word on, the bot posts once in the group: voice
  notes there are now sent to ElevenLabs for transcription, and a short line on how to use the wake
  word. This notice is required. It can't be turned off while the wake word is on.

## Privacy and security

- The audio stays in memory and is never cached. Only the transcript of an addressed note is
  stored, as the message text. Unaddressed and wake-miss transcripts are never stored or logged.
- Transcripts are untrusted content and always wrapped. A voice note that says "ignore your
  instructions" carries no more weight than the same text typed.
- Logs never contain transcript text or audio. They contain counts, durations, status codes and
  error classes only.
- `ARCHITECTURE.md` gets a "Voice input" privacy bullet next to the voice narration one. It covers
  what goes to ElevenLabs, the wake-word mode, retention and the separate key.

## Codex execution

Read `AGENTS.md`, this plan, [Voice narration](2026-11-09-voice-narration.md), the
[image vision plan](2026-10-31-image-vision.md) (replied-to attachments and `_wrap`), then:

- `packages/core/src/chatcore/events.py`, `platform.py`, `app.py` (`_on_message`), `agent.py`
  (`_images`, `_wrap` and where the user content is built), `skills.py` (`Skill.pin`), `tts.py`
  (client, `fetch_subscription`)
- `packages/adapters/telegram/src/chatcore_telegram/bridge.py` (`_attachments`, `_has_content`,
  download)
- everything in `packages/plugins/voice/src/chatcore_voice/`

### You own

- core:
  - `packages/core/src/chatcore/events.py` (new kinds, `Attachment.duration_s`, `VoiceNote`,
    `IncomingMessage.voice`)
  - `platform.py` (the `ChatActivityPlatform` protocol)
  - `app.py` (the `transform_incoming` hook call)
  - `agent.py` (transcript wrapping and the voice-turn line)
  - `plugin.py` or wherever the plugin base lives (the default `transform_incoming`)
  - new `stt.py`, the ElevenLabs STT client, next to `tts.py`
- `packages/adapters/telegram/src/chatcore_telegram/bridge.py` and `__init__.py` (attachment mapping
  and `ChatActivityPlatform`)
- `packages/plugins/voice/**`, including `migrations/`, `gates.py`, `permissions.py` and
  `EXPLAIN.md`
- tests under `packages/core/tests/`, `packages/adapters/telegram/tests/` and
  `packages/plugins/voice/tests/`
- `docs/wiki/Plugin-voice.md`, the `ARCHITECTURE.md` privacy bullet, `CHANGELOG.md` (`Unreleased`)

You may **not** edit `routing.decide`, other plugins, `pyproject.toml`, `uv.lock`, `apps/` or the
request snapshots. The request snapshot must pass unchanged, because `voice` is unset in it. Don't
call ElevenLabs in tests; use `httpx.MockTransport`. You may read the ElevenLabs speech-to-text
docs to confirm the endpoint, field names and model ids. Put what you confirmed in your report.

### Tests

- Adapter: `voice` maps to `VOICE` with `duration_s`, `video_note` maps to `VIDEO_NOTE`, and `audio`
  stays `AUDIO`.
- Transcribe/skip for every row of the "When the bot transcribes" table, including restricted
  users (no download, no provider call).
- Wake word:
  - "Hey clawdot, what's the weather" and "claw dot what's…" both match;
  - a match after word 5 doesn't;
  - a miss leaves `messages.text` empty and logs no transcript text (use `caplog`);
  - an over-long note in wake mode isn't downloaded.
- Caps: per-note, per-user and global limits are refused before download. A missing duration falls
  back to the byte estimate.
- The provider call is a multipart request to the STT endpoint with the model and key. Non-2xx
  responses and malformed JSON map to the provider-error message. The key never appears in logs.
- Agent: with `voice` set, the transcript is wrapped and the voice-turn line is present. A
  replied-to transcript sits next to the request. With `voice` unset, the snapshot is unchanged.
- Voice reply:
  - `read_aloud` is pinned when voice is set and the reply setting is on, and not pinned when it's
    off;
  - a narration refusal still sends the text reply with the one-line reason.
- Settings:
  - a group non-admin can't change them, through either the command or the tool;
  - a DM user can;
  - turning on the wake word posts the notice once;
  - the wake-word limits are enforced.
- Migration applies on a fresh database. With no row, the defaults apply.

### Done when

- The `AGENTS.md` checks pass, with Postgres where available.
- The request snapshot, skill catalog and routing-set tests pass. Where a new tool requires it,
  `routing_set.jsonl` gets an entry for `voice_settings`.
- `EXPLAIN.md` and the wiki describe voice input, the wake word, settings, privacy and limits.
- Left for later:
  - live ElevenLabs checks, which Claude runs with the owner before enabling;
  - enabling in SBot (downstream pin and env);
  - local wake-word detection;
  - pronunciation practice through the tutor plugin.
