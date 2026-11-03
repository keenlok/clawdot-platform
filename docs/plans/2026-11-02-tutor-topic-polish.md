# Tutor: two explanation languages, trailing modifiers, error prompts, quiet auto-reply on media

Status: Planned. Follow-up to [2026-11-02-tutor-group-status.md](2026-11-02-tutor-group-status.md)
(PR #50, live).

Read first: `packages/plugins/tutor/src/chatcore_tutor/` (`plugin.py` parser, `/tutor` command, topic
actions and `commit`, `auto_address`, context hook; `repository.py` validation; `languages.py`),
`packages/plugins/tutor/tests/`, `packages/core/src/chatcore/command_prompts.py` (`CommandReply`),
`packages/core/src/chatcore/events.py` (`Attachment`, `AttachmentKind`).

## Live feedback (bot replies and commands only)

1. `/tutor@bot here cantonese A1 english and chinese auto` and `… english and chinese` both got the bare
   usage text. The owner wants explanations in two languages and `auto` in the same command. The usage
   reply wasn't a reply prompt, so replying `here auto` went to the model.
2. In an auto-reply topic the bot answered an untagged message with "your last message came through empty".
   Telegram delivers voice notes, audio and video with empty text; `auto_address` let them through.

## You own

`packages/plugins/tutor/**` (source and tests). No core changes, no new migration, don't edit applied
migrations. The skill instructions (800-character cap) don't change.

## 1. Up to two explanation languages

- The parser accepts the explanation part as one or two languages joined by `and`, `&`, `,`, `/` or `+`
  (with or without spaces): `english and chinese`, `english,chinese`, `english/chinese`. Order is kept.
  First language token = target; the rest (at most **2**) = explanation languages, in order. Duplicates,
  the target among them, or a third language are errors.
- `TutorArgs.explain` becomes `tuple[str, ...]` (empty = not given). Keep canonical names.
- Storage stays in the existing `explain text` columns (≤ 40 chars): canonical names joined by `,` with no
  spaces, e.g. `English,Mandarin Chinese`. Every pair of table names fits in 40; add a test that proves it.
  Reading back splits on `,` and re-validates each name; a row that fails reads as missing/unmarked, as today.
  Old single-name rows read unchanged.
- Display: `explanations in English and Mandarin Chinese` (profile line and topic status).
- Context text: one language → `explain in English`; two → `explain in English, adding Mandarin Chinese
  where it helps`. The ≤ 400-character test must cover every target with every ordered explanation pair
  and every level, for profile and topic-default contexts.
- Personal profiles (`/tutor` in a DM) use the same parser, so they get two languages too. A DM `/tutor
  <target> <level>` with no explanation keeps the existing profile's explanation languages, else English.

## 2. Trailing `auto` / `quiet` on a mark

`/tutor here <language> <level> [<explanation…>] auto|quiet` and `/tutor here auto|quiet` stay valid.
When a mark (with or without a language) ends with `auto` or `quiet`, strip it before parsing and record
it in the same confirmation: the `topic_set` payload gains `auto_reply: true | false | null` (`null` =
keep the current value; a new topic then defaults to quiet). The summary says it, e.g. "Make this topic a
tutor topic (default Cantonese A1, explanations in English and Mandarin Chinese; replies to every
message)". `commit` validates `auto_reply` is a bool or `None` and writes both in one repository call
(extend `upsert_topic` with an optional `auto_reply`). The same trailing word works in replies to the
status prompt (`cantonese A1 english and chinese auto`).

## 3. Errors say why, and are reply prompts

Parsing errors name the problem and the offending token, for example:
- `Unknown language "klingon". Supported: English, Japanese, … .`
- `Explanation languages must differ from the language you're learning (Cantonese).`
- `At most two explanation languages.`
- `Give one level: A1, A2, B1, B2, C1 or C2.`

Then one line with the usage and `Reply to this message with a corrected value.`

Return error and usage replies as `CommandReply(awaits_reply=True, placeholder=…)` when the replier would
be allowed to act on a reply: in a group only for setup managers or group admins (plain text for others,
as now); in a DM always (it's their own profile). A reply then re-runs `/tutor <reply>`; in a group the
existing mapping turns `here auto`, `auto`, `cantonese A1 english`, etc. into the topic path. Make sure a
reply that starts with `here` works too (`here auto`, `here cantonese A1 english auto`).

## 4. Auto-reply only for text and readable attachments

`auto_address` returns True only if the message has non-empty text **or** an attachment of kind `PHOTO`
or `DOCUMENT`. Voice, audio, video and other kinds alone return False (the message stays unaddressed).
Tagged messages and replies to the bot are untouched (they never reach `auto_address`).

## 5. Status keeps the DM hint

Keep the existing last line `Set your own profile with {cmd} in a DM with me.` (exactly once).

## Tests

- Parser: every separator form; order kept; two explanation languages; three rejected; duplicate and
  target-in-explanation rejected; multi-word names (`mandarin chinese and english`); trailing `auto`/`quiet`
  stripped only in the topic path; each error message names the token.
- Storage round-trip (memory and Postgres integration, skip without `TEST_PG_DSN`) for one and two
  languages; old single-name rows; invalid joined value reads as missing; the 40-character proof.
- Topic: `here cantonese A1 english and chinese auto` creates one pending action with both languages and
  `auto_reply=True`; commit writes both; `here … quiet` on an auto topic sets quiet; a mark without the
  modifier keeps the current mode; non-manager refused.
- Prompts: manager error/usage replies have `awaits_reply=True`; non-manager ones don't; DM errors do.
  A reply `here auto` to the prompt (run as `/tutor here auto`) creates the auto action.
- Context and display strings for two languages; the exhaustive ≤ 400 test.
- `auto_address`: text → True; photo-only or document-only → True; voice/audio/video/other-only → False;
  empty text and no attachments → False; quiet topic → False.

## Done when

Lint, format on changed files, and the full suite pass. No `.env` or migration changes.
