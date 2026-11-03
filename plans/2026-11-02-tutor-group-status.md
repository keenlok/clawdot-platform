# Tutor: discoverable group status and reply-to-enable

Status: Planned. Follow-up to stage 1.5 ([2026-11-02-language-tutor-groups.md](2026-11-02-language-tutor-groups.md), PR #49, live).

Read first: `packages/plugins/tutor/src/chatcore_tutor/plugin.py` (the `/tutor` command, `get_tutor_profile`,
topic flow), `packages/plugins/tutor/tests/test_tutor_topics.py`, `packages/core/src/chatcore/command_prompts.py`
(`CommandReply`, how a reply re-runs the command as `/name <reply>` for whoever replied), and
`packages/core/src/chatcore/model_plugin.py` (a status that is also a reply prompt).

## What went wrong in production

In a non-forum group the owner sent `/tutor@bot`. The reply was "Tutor topic: off Set your own profile with
/tutor@bot in a DM with me.": one run-on line that never mentions `/tutor here …`. They replied "turn it on";
that went to the model, which called `get_tutor_profile` and said tutoring can only be set in a DM.

## You own

- `packages/plugins/tutor/src/chatcore_tutor/plugin.py`, `packages/plugins/tutor/tests/**`
- `apps/sb_bot/tests/test_tutor_wiring.py` (only if an end-to-end test is easiest there)

Nothing else. Don't change the skill instructions (they are at the 800-character cap), core, or migrations.
No tool may mark, clear or change topics: that stays command-only.

## 1. Group `/tutor` status

Lines are separated by `\n` (never one run-on line). Use `ctx.command_form("tutor")` for every command
mention (it renders `/tutor@bot` in mentions-only groups).

- Line 1: the existing status (`Tutor topic: off` / `Tutor topic: on, …; replies only when tagged` …).
- For a **setup manager or group admin** (reuse `is_group_manager`), add:
  - topic off: `To turn it on: {cmd} here <language> <level> [<explanation language>], or {cmd} here for no default language.`
  - topic on: `Change it with {cmd} here <language> <level> [<explanation language>]; {cmd} here auto replies to every message, {cmd} here quiet only when tagged, {cmd} here off turns it off.`
  - last line: `Or reply to this message with one of those, e.g. "japanese A2 english", "auto" or "off".`
- For **anyone else**: `A group admin can turn it on with {cmd} here <language> <level>.`
- Always last: `Set your own profile with {cmd} in a DM with me.`

Return it as `CommandReply(text, awaits_reply=True, placeholder="e.g. japanese A2 english")` for managers. For
non-managers, return plain text (no prompt to open).

## 2. Replies and bare topic words become `here …`

A reply to the prompt arrives as `/tutor <reply>` from whoever replied. In a **group**, map arguments that
don't start with `here` like this, before the existing private-settings refusal:

| Args (casefolded, trimmed) | Treated as |
| --- | --- |
| `on`, `enable` | `here on` (below) |
| `off`, `disable` | `here off` |
| `auto`, `quiet` | `here auto` / `here quiet` |
| parses with `parse_tutor_args` (language + level [+ explanation]) | `here <args>` |
| anything else | a usage reply (as a `CommandReply(awaits_reply=True)` for managers, plain text for others) that lists the accepted replies; nothing changes |

New sub-command `here on`: if the topic is already marked, reply with the status and change nothing; otherwise
behave like `/tutor here` (mark with no default). Every mapped path runs the existing permission check
(non-managers get "Only a group admin can change tutor topics.") and the existing confirmation flow.

In a **DM**, nothing changes: personal profile settings keep their current behaviour, and `here …` keeps its
"use it in a group" reply.

## 3. Model awareness (`get_tutor_profile`)

Tool description: `Return the requester's own language-tutor settings and, in a group, this topic's tutor status
and how to change it.`

In a group, the tool result is (lines joined by `\n`):
1. The requester's own profile line, or `No tutor profile. Set one in a DM with {cmd}.`
2. The topic status line (same text as `/tutor`).
3. `To make this group or topic a tutor topic, a group admin sends {cmd} here <language> <level> [<explanation
   language>] (or {cmd} here for no default); {cmd} here auto makes it reply to every message. You can't change
   this with a tool; tell the person the command.`

No permission lookup inside the tool (keep it cheap); the command enforces it.

## Tests

- Status text: off and on, manager and non-manager variants, exact strings with `\n`, `command_form` used
  (a `/tutor@bot` form in a mentions-only context).
- Manager status is a `CommandReply` with `awaits_reply=True`; non-manager status isn't.
- Replies: `japanese A2 english`, `auto`, `quiet`, `off`, `on` in a group each create the same pending action as
  the `here …` form (assert kind and payload); `on` on an already-marked topic changes nothing; a non-manager
  reply is refused with no pending action; `turn it on` returns the usage reply and no action.
- DM behaviour unchanged (existing tests still pass; add one for `/tutor japanese A2 english` in a DM still
  saving the personal profile).
- `get_tutor_profile` in a group: includes the topic status and the `/tutor here` instruction, uses
  `command_form`, and never another member's profile.

## Done when

Lint, format on changed files, and the full suite pass. No `.env` changes.
