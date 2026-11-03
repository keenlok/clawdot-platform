# Replies as command input

Status: **built** in [#44](https://github.com/keenlok/clawdot-platform-history/pull/44), Phases 1–3 (Phase 4 not started). Approved 2026-11-01 with these changes from the draft: anyone in the chat can answer
a prompt, and there is no `ask` mode (commands whose input is natural language, like `/remind`
and `/watch`, already work by asking the bot in plain words; this feature is an add-on for the
rest). Follows `docs/plans/model-switch.md` (PR #28).

## Problem

Slash commands only take input on the same line, and several need syntax people don't know:
`/remind <ISO local time> <IANA timezone> <text>`, `/watch <seconds> <public URL>`,
`/jobtools <reminders|watches> <on|off> [limit=value ...]`. A bare command answers with a
"Use /x <args>" line and stops. If the person then replies to that message with what it asked
for, the reply goes to the LLM as an ordinary chat turn rather than to the command. So the
command they started never gets the input, and the result depends on the model reading the
usage line correctly.

Goal: when a command needs more input, it **asks**, and a reply to that question **is** the
input. `/model` is the pilot: `/model` shows the choices and asks "Reply with a model name";
replying `glm-5` switches.

## Rules

| Topic | Proposal |
|---|---|
| Who can answer | Anyone in the chat (decided 2026-11-01). The command runs as the person who replied, with their permissions: an admin-only command answered by a non-admin stays silent, and a manager-only toggle refuses as it does when typed |
| How long | A prompt stays open 10 minutes, and is used once. A command that wants another answer (e.g. an unknown model name) asks again, which opens a new prompt |
| What counts as the answer | The reply's text, trimmed. A reply that is itself a slash command runs as that command (so replying to a poll with `/closepoll` still works). `cancel` closes the prompt with "Cancelled." |
| Mode | The reply becomes the command's argument text and the handler runs without the LLM (cheap, deterministic). No `ask` mode (dropped 2026-11-01) |
| Groups | Works in mention-only groups without a tag, like replies to the bot today. The reply is checked with the same allowlist, restriction and admin rules as typing the command |
| Storage | Cache key per sent prompt (`cmd-prompt:<chat>:<bot message id>`) holding command name, mode, requester, thread and expiry. No migration. A restart loses open prompts; the reply then falls back to today's behaviour |
| Privacy and logs | Prompt state holds no message text. Logs record chat, command and outcome only |
| Unchanged | Reply codes (`YES 1234`), confirmation buttons, `/ask`, reply chains to ordinary bot answers, and commands typed with their arguments |

## How a command opts in

`Command` handlers may return a `CommandReply(text, awaits_reply=True, placeholder="")` instead
of a plain string. `BotApp` sends the text, and when `awaits` is set registers the sent message
as a prompt for that command and requester. Plain-string handlers keep working unchanged.

Routing: in `BotApp._on_message`, a message whose `reply_to` is an open prompt is handled before
`decide()`: the command runs with the reply text as `args`, through the same path as a typed
command (access, restrictions, `admin_only`).

Where the platform supports it, the prompt is sent with a reply request (Telegram `ForceReply`,
`selective`, so the reply box opens for the person who ran the command; anyone else can still
reply by hand). Platforms opt in with an optional `ReplyPromptPlatform.send_reply_prompt`
method, the same pattern as `MultipartSender`, so the `ChatPlatform` contract doesn't change.
The direct Telegram adapter implements it. The release-control Telegram bridge doesn't yet,
because that means changing the protected bridge service; prompts sent through it are plain
messages, and replies to them work the same.

## Phases

### Phase 1: framework (core)
- `CommandReply`, the prompt store, routing, single use, expiry, `cancel`, and the
  slash-command-wins rule.
- `/model` as the pilot: bare `/model` shows today's text plus "Reply with a model name to
  switch"; the reply switches; an unknown name asks again.
- Tests: answer by the requester, by someone else (runs as them), after expiry, twice, with
  `cancel`, with a slash command, in a mention-only group without a tag, with a restricted
  user, for an `admin_only` command from a non-admin (silent, as today).

### Phase 2: reply prompts in Telegram (adapter)
- `TelegramPlatform.send_reply_prompt` sends `ForceReply(selective=True)` with a short
  placeholder on the last part. Other adapters don't implement it and fall back to `send`.

### Phase 3: adopt it across commands
Each bare command asks for what it needs:

| Command | Bare today | With this change | Mode |
|---|---|---|---|
| `/model` | shows model and choices | same, plus "reply with a model name" | args |
| `/persona` | shows persona | same, plus "reply with new persona text" | args |
| `/timezone` | shows zone | same, plus "reply with a zone, e.g. Asia/Singapore" | args |
| `/forget` | usage line | lists memory IDs, "reply with the ID" | args |
| `/closepoll` | closes the replied-to poll, else usage | lists open polls numbered, "reply with the number" | args |
| `/cancelreminder`, `/cancelwatch` | usage | lists them, "reply with the ID" | args |
| `/research` | usage | "What should I research? Reply with a topic" | args |
| `/polltools`, `/browsetools`, `/chatmode` | usage | shows state, "reply on or off" | args |
| `/jobtools` | usage | shows settings, "reply e.g. `reminders off`" | args |
| `/search` (admin) | usage | "reply with search text" | args |

Out of this round: `/remind`, `/watch`, `/watchrendered` and `/poll`, whose input is natural
language that the bot already understands when asked in plain words; `/admin` and `/extensions`
(multi-step owner tools); and `/help`, `/privacy`,
`/balance`, `/expenses`, `/memory`, `/reminders`, `/watches`, `/polls`, `/time`, `/new`, `/reset`,
`/compact`, `/export` and `/forgetme`, which already work bare or confirm with buttons. Each
plugin's change is ordinary plugin code plus tests; usage lines are rewritten in plain words
with one example.

### Phase 4 (optional, later): choice buttons
For short fixed lists (model choices, on/off, open polls), inline buttons instead of typing. It
needs a general choice-button event alongside today's Confirm/Cancel `ButtonPress`, which is a
larger contract change, so it stays separate.

## As built

- `chatcore/command_prompts.py`: `CommandReply`, `CommandPromptStore` (one record per sent
  message part, all parts closed by the first answer) and the optional `ReplyPromptPlatform`.
- `BotApp`: `_prompt_answer` runs before `decide()`; an answer goes through the same restriction
  and command path as a typed command (`_run_command(..., parsed=...)`). `BotApp.send` takes
  `reply_prompt` for the reply box.
- `/help` gains one line: send a command on its own and reply to its question.
- Bare `/forget`, `/closepoll` and `/cancelreminder`/`/cancelwatch` with nothing to act on reply
  plainly and open no prompt.

## Verification

Unit tests per phase; the agent request snapshot and the skill catalog and routing-set tests
pass unchanged; `AGENTS.md` checks; a console end-to-end run covering `/model` → reply.

## Decisions (2026-11-01)

1. Anyone in the chat can answer a prompt.
2. Prompts stay open 10 minutes.
3. Telegram `ForceReply` is in this round, on the direct adapter.
4. No `ask` mode: `/remind`, `/watch` and `/poll` keep their typed form, and people use plain
   words with the bot for those.
