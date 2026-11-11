# Group speaker attribution: who the bot is answering

Status: Approved by the owner 2026-11-11 (all of A–F, including F; B2 as written). Implemented in PR #36 and released in v0.10.0;
downstream rollout tracked privately. The live eval wasn't run before release.
Implemented in PR #36 (Codex executed, Codex and Claude reviewed); not yet released. The live
eval baseline and results are still to be recorded. Predecessor:
[Expense speaker context](2026-11-10-expense-speaker-context.md) (v0.8.0, PR #30), whose
part B added the fixed `_SPEAKER_INSTRUCTION`.

## Incident

After v0.8.0 was deployed (2026-11-11 01:43 UTC), the group bot still addressed the wrong
member. (Real names, nicknames, handles, IDs and chat content are kept out of this public
record. Member A is the bot owner, who has a saved nickname, "nickname-A"; member B has none.)

1. **Not a reply (the main case).** About 2.5 hours after deploy, in a forum topic, member B
   mentioned the bot with a food question. The reply opened with "Short answer, nickname-A:".
   B's stored turn had the right `speaker` and no `reply_to_id`, and it opened a new
   conversation whose only participant was B. Every bot exchange in that topic for the
   previous hour had been with A (news, then a voice note).
2. **Replies to the bot.** Before v0.8.0: member B replied to the bot's confirmation of an
   expense that member C had logged, asking to fix the split. The reply joined C's
   conversation, and the bot answered as if B had paid. In another case a member replied to a
   bot message that had addressed several people, and the bot mixed up who had said what.
3. **Summaries.** The summary later written for case 1's conversation is in the second person
   ("You asked whether…"), names nobody, and ends with a chat-style offer ("Want me to go hunt
   that down now?").

## How the request was rebuilt

The request builder is unchanged between v0.8.0 and `main`: v0.8.1–v0.8.2 changed only the
Telegram adapter, and v0.9.0 added photo-sending helpers in `files.py` and `testing.py`. So
`main` reproduces the deployed request. A private scratch script (kept
out of the repository because it loads real rows) seeded the in-memory stores with the
topic's last 21 `messages` rows, an empty conversation for B, the group's persona and the
deployed app prompt from the downstream app, plus a `before_turn` stub that returns the
memory plugin's `build_context` output for the chat's seven memories. It then ran
`AgentResponder.respond` against a `ScriptedProvider` and captured the first request. Skills
mode was off in the rebuild; that changes which skill sections appear in the system prompt,
not the order or content of the turns below.

The request for case 1 had an 11.2k-character system prompt and four user messages:

| # | Content | What the model sees |
| --- | --- | --- |
| system | core safety, app prompt, runtime context, `_SPEAKER_INSTRUCTION`, persona ("…snarky commentator for our trip…") | No names. The speaker sentence refers to "the latest user message". |
| 0 | memory context (untrusted) | Pinned trip fact, then "Possibly relevant memories", including `#2 [about <A's display name>] <A> prefers to be addressed as "nickname-A"`, plus nickname memories for two other members. None for B. |
| 1 | ambient transcript (untrusted) | **Only four records fit**: A's request, the bot's reply to A, A's "give audio", the bot's reply to A. Bot records show `"display_name":"(you)"`. |
| 2 | current-turn metadata (untrusted) | `{"message_id":…,"speaker":{"user_key":"tg:…","display_name":"<B, emoji escaped as 🍚>",…},"text":""}` |
| 3 | the message text, bare | B's question with the bot mention stripped. No name. |

## Root cause

Nothing in the request was wrong about B's identity; the speaker record was correct. The
model was outweighed by everything else pointing at A, and the speaker sentence didn't
connect the two:

1. **The question has no name on it.** The text the model answers (message 3) carries no
   speaker. The speaker is a separate JSON record (message 2) with an empty `text`. The
   system prompt says "the speaker of the latest user message", but the latest user message
   is the bare text. Every message in the request has the user role, so "latest user message"
   doesn't pick out record 2.
2. **The history window held only A.** `render_record` serializes with `ensure_ascii=True`,
   so each CJK character costs six characters (`佛`). The bot's two Chinese news
   replies each hit the 2,400-character record cap, and the 6,000-character ambient budget
   held four records from the last 23 minutes, all A and the bot. Other members' messages
   from the previous two hours, and B's last message (the previous evening), were cut.
3. **A nickname was on offer, and B had none.** The memory plugin recalls the top 5 of the
   chat's 6 unpinned memories by similarity (`recall_k=5`), so the nickname memories are
   almost always included whatever the question. They're attributed by display name only
   (`about <name>`), with no user key to match against the speaker record. The bot had
   addressed A by that nickname in 38 earlier messages, and the persona asks for a familiar,
   snarky voice, so "Short answer, nickname-A" was the likeliest opening.
4. **Contributing: "(you)" means the bot.** Bot records label the bot's own speaker
   `"(you)"`, while `_SPEAKER_INSTRUCTION` says "you" means the latest speaker. That's two
   meanings of the same word in one prompt. It didn't cause case 1 on its own, but it
   weakens the instruction. B's name being escaped (`🍚`) doesn't help either.

**Replies (case 2).** `chatcore.threads._reply_conversation` puts a reply to a bot message
into the conversation that message belongs to, even when someone else started it. The
responder then loads that conversation's summary and turns, all addressed to the starter,
and adds `Replying to your earlier message (<time>): <bot record>`. That record doesn't say
whom the bot was answering, and its text calls that person "you" ("(you — paid)").

**Summaries (case 3).** `ConversationStore.compact_conversation` (and `compact`) is called
with the full chat system prompt: persona, `_SPEAKER_INSTRUCTION`, skill sections and "Answer
in the user's language", followed by `ATTRIBUTION_SUMMARY_INSTRUCTION`. So the summarizer
acts as the chat bot replying to the last speaker: second person, no names, and an offer at
the end. The attribution instruction asks it to "preserve speaker user keys", but nothing
says not to write "you", and the persona and speaker sentence point the other way. The next
turn loads this summary as the first piece of history. If a different person joins the
conversation (case 2), "You asked…" then reads as being about them.

## Changes

All in core except D (memory plugin). Core is maintenance-reviewed (`AGENTS.md`). A, B, C, E
and F change the agent request snapshot.

### A. Put the speaker on the message being answered (primary fix)

`AgentResponder` (`packages/core/src/chatcore/agent.py`, the end of the request builder):

1. Replace the separate metadata record and bare text with one final user message whose
   first part is a wrapped header, then the text:
   `<untrusted-…>Message from <display_name> (@<username>, <user_key>), id <message_id>,
   replying to <id or "nothing">:</untrusted-…>` followed by a newline and the text. Render
   the header with readable Unicode (bounded and control-character-escaped as in F); the
   name stays inside the untrusted wrapper. Mentions stay as today, in the header's JSON tail
   if they're present.
2. Reword `_SPEAKER_INSTRUCTION` (still a fixed constant, no user-controlled text):
   `"The final message starts with who sent it. That person is who you are replying to;
   "you" in your reply means them, not people you addressed earlier. In groups, name other
   people instead of calling them "you". Use a nickname or form of address only for the
   person it belongs to (match the user key)."`

Trade-off: the snapshot and every test that asserts the last message's text change. Images
still attach to the same final message.

### B. Say whom a quoted bot message answered, and mark handovers

1. **Reply provenance.** In the `replied.is_bot` block, look up `replied.reply_to_id` (same
   chat and topic, the existing `get_message`) and name that person:
   `Replying to your earlier message (<time>), which answered <name> (<user_key>):`. When
   that person isn't the current speaker, add the fixed sentence `"That reply was written for
   them; "you" in it means them, not the current speaker."` If the lookup fails, keep today's
   wording.
2. **Handover marker.** Keep joining the replied-to conversation; the context is usually
   needed (fixing a split on someone else's expense). When the current speaker isn't among
   the conversation's participants *before* this turn, add one wrapped note after the summary:
   `"<current speaker> is joining a conversation started by <participant names>. The summary
   and earlier turns were written for them."` The executor confirms that the `Conversation`
   in `current_conversation` holds the pre-turn participant list; if it doesn't, pass that
   list from `resolve_conversation`.

Considered and not recommended as the default: starting a new conversation when a different
person replies. It's the cleanest attribution, but it drops the context that makes the reply
answerable. A per-app setting can come later if B isn't enough.

### C. Summaries in the third person, with names

`packages/core/src/chatcore/conversation.py`:

1. Add a fixed `SUMMARY_SYSTEM_PROMPT`: core safety text plus `"You write internal notes about
   a chat for an assistant to read later. This is not a reply. Write in the third person:
   name each person by display name with their user key the first time, and never write
   "you" or "the user". Record who asked what, and whom each answer was for. No greetings,
   questions or offers."` followed by `ATTRIBUTION_SUMMARY_INSTRUCTION`.
2. `compact_conversation` and `compact` use it instead of the chat system prompt. Callers in
   `agent.py` and the `/compact` command stop passing the chat prompt (keep the parameter
   with a deprecation note if it's part of the public API; check before changing the
   signature).
3. Older second-person summaries: conversations idle for over an hour aren't continued, so
   they rarely load again. When one is folded into a new summary, the prompt says `"An
   earlier summary may say 'you' for the conversation's first participant."` No migration.

Trade-off: the summarizer loses the persona and app prompt, which are chat-reply
instructions anyway. Summaries may get a little longer because they name people.

### D. Memories say whom a nickname belongs to (memory plugin)

`packages/plugins/memory/src/chatcore_memory/service.py`, `render_fact`: when a memory has a
`subject_user_key`, render `about <name> (<user_key>)` so it can be matched to the speaker
record. No recall or ranking change. The rule about nicknames lives in A2's core sentence,
because the memory context appears without the memory skill being active.

### E. Rename the bot's speaker label

`chatcore.attribution.message_record`: bot rows get `"display_name": "<bot account name>
(this bot)"` instead of `"(you)"`, taking the name from the caller (`bot_name` is already
passed to `_transcript_text`). `turn_record` assistant turns keep `speaker: null`, and
`_TRANSCRIPT_INTRO` gains `"assistant records are this bot's own messages."`

### F. Render records with readable Unicode (budget fix)

`chatcore.attribution._serialize`: `ensure_ascii=False`, still escaping `<`, `>`, `&`, and
escaping control and invisible characters explicitly (Unicode categories Cc and Cf, and
U+2028/U+2029), so bidi overrides and zero-width characters stay visible as `\uXXXX`. In
Chinese-heavy topics this lets the existing 6,000-character window hold several times more
history, and it makes names with emoji readable. It changes every rendered record, so it gets
its own commit and a security note in the PR. The owner may defer F; A–E stand without it.

Out of scope: memory recall ranking, the persona, model choice, the 6,000-character budget
itself, and splitting conversations per speaker.

## Regression check

1. **Prompt tests** (`packages/core/tests/test_speaker_attribution_prompts.py`, CI). Build
   requests with `memory_stores`, seeded `StoredMessage` rows and a stub `before_turn`
   plugin, using placeholder members (`tg:111111111` "Member A" with a "nickname-A" memory,
   `tg:222222222` "Member B"). Scenarios:
   - *ambient-only-A*: four A↔bot exchanges with CJK bot replies, then B asks something new.
     Assert the final message's header names B and B's key, no separate empty-text metadata
     record remains, the memory line for nickname-A carries A's key, and no record uses
     `"(you)"`.
   - *reply-to-other's-answer*: the bot confirms C's expense, then B replies to it. Assert the
     quote says it answered C, the "written for them" sentence is present, and the handover
     note names B and C.
   - *summary*: compact a conversation with A and B turns. Assert the summarizer's system
     prompt is `SUMMARY_SYSTEM_PROMPT` (no persona, no `_SPEAKER_INSTRUCTION`).
   - With F: the CJK bot replies render as readable text, and a bidi override in a name stays
     escaped.
2. **Live eval** (`evals/speaker_attribution/run_eval.py`, run by hand, not in CI, like
   `evals/receipts`). It builds the same three requests with placeholder data at realistic
   sizes, sends each N times (default 20) to the configured chat model with the key only in
   the environment, and prints rates only: replies that address A's name or nickname to B
   (fail), and summaries containing "you" or with no participant names (fail). Run it on
   `main` first for the baseline and record the numbers in the PR.

## Owned files

- `packages/core/src/chatcore/agent.py` (request builder end, the `replied` block,
  `_SPEAKER_INSTRUCTION`, `_TRANSCRIPT_INTRO`, compaction call sites)
- `packages/core/src/chatcore/attribution.py` (`message_record` label; `_serialize` for F)
- `packages/core/src/chatcore/conversation.py` (`SUMMARY_SYSTEM_PROMPT`, `compact*`,
  `/compact`)
- `packages/core/src/chatcore/threads.py` only if B2 needs the pre-turn participants passed
  through
- `packages/plugins/memory/src/chatcore_memory/service.py` (`render_fact`) and its tests
- `packages/core/tests/**`, including `test_speaker_instruction.py` and
  `snapshots/agent_requests_off.json` (regenerate; the diff must be limited to A, B, E, F)
- `evals/speaker_attribution/**` (new)
- `CHANGELOG.md` (`Unreleased`), `docs/plans/README.md` (this row)

Don't touch migrations, `__init__.py` files, gates or permissions. No new dependencies.

## Checks

```sh
uv sync --locked --all-packages
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check <changed .py files>
uv run pytest -q -m "not browser and not sandbox_integration"
```

Downstream: the app's own prompt and request-snapshot tests may need updating when it
re-pins.

## Execution

Codex is installed on the owner's computer, so per `AGENTS.md` Codex executes (at the
owner's chosen `ARCHITECTURE.md` §7b model), Codex reviews, and Claude does the final review.
Suggested order: E, A, B, D, C, then F as a separate commit, with the eval baseline run
before A.

## Done when

- A–E (and F if approved) are implemented; lint is clean; the full suite passes locally.
- The snapshot diff is limited to the planned changes.
- The live eval on the deployed chat model shows 0 of 20 wrong-member openings in the
  ambient-only-A and reply scenarios, and 0 of 20 second-person summaries, against the
  baseline recorded from `main`.
- After release and re-pin (separate steps), in-chat check: after one member's run of
  requests in a topic, a different member's new question is answered without the first
  member's name or nickname.

## Owner decisions (2026-11-11)

1. F (readable Unicode rendering) ships in this PR.
2. B2 as written: a reply from a different person joins the replied-to conversation, with a
   handover note.

## Notes for the executor

- Compaction is called from `agent.py` (after a turn), `app.py` (reopen compaction) and the
  `/compact` command in `conversation.py`. All of them use `SUMMARY_SYSTEM_PROMPT` after C.
  Keep the `system` parameter of `compact`/`compact_conversation` working for other callers,
  but don't pass the chat prompt from these call sites.
- The eval builds its chat provider with `chatcore.llm.factory.LLMSettings` and
  `build_provider`, reading the environment the same way the bot does; look at
  `evals/receipts/run_eval.py` for the style. It must import cleanly without a key, and it
  must print rates only (no prompt or reply text).
- Placeholder data only, everywhere: names like "Member A", IDs like `tg:111111111`. The
  private-identifier hook runs on commit.
- Regenerate the snapshot with `UPDATE_SNAPSHOT=1 uv run pytest -q
  packages/core/tests/test_agent_request_snapshot.py`, then explain its diff in the report.
- Update existing tests that assert the old wording (`test_speaker_instruction.py`,
  attribution and transcript tests); don't delete coverage.
- Add a `CHANGELOG.md` `Unreleased` entry (Changed: request shape, summary prompt, record
  rendering; note that downstream request snapshots change).
