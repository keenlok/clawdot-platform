# Language tutor, stage 1.5: profiles in groups and tutor topics

Status: Planned. Follows stage 1 ([2026-11-02-language-tutor.md](2026-11-02-language-tutor.md),
PR #48, live with `TUTOR_ENABLED=true`). Stage 2 (cards) is
[2026-11-02-language-tutor-cards.md](2026-11-02-language-tutor-cards.md).

Read first: `AGENTS.md`, the stage 1 plan, `packages/plugins/tutor/` (all of it),
`packages/plugins/polls/src/chatcore_polls/permissions.py` and `service.py`
(`request_configure`, commit re-checks, `/polltools` with `ctx.confirm.request`),
`packages/core/src/chatcore/pending.py` (`PendingAction`, `PendingActionHandler`),
`packages/core/src/chatcore/plugin.py` (`CommandContext`), `apps/sb_bot/src/sb_bot/app.py`
(`PollsPlugin(settings, access, platform)` wiring and where `TutorPlugin()` is appended).

## Owner requests

1. "why only my dm enable every where": the person's tutor profile should apply in groups too.
2. "can i also make it only tutor in a topic channel?": a group admin marks one forum topic (or a
   whole non-forum group) as a tutor topic, where every message is a tutor turn.

## Design

### A. Profiles in groups

- The `language_tutor` **context** hook now also runs in groups: when the skill is active in a
  group turn (routed, sticky, or pinned by a tutor topic), it injects the **speaker's own** active
  profile. Only the requester's profile, never another member's.
- **No automatic pin in ordinary groups.** A profile pins the skill only in the person's own DM (as
  today) or inside a marked tutor topic.
- A paused profile (`/tutor off`) is not injected anywhere.
- Privacy trade-off, accepted by the owner: a reply in a group follows the person's explanation
  language and level, so other members can infer their level and languages. Setting, showing,
  pausing and clearing a **personal** profile stays DM-only, so the profile line itself is never
  posted to a group by a command.
- `get_tutor_profile` in a group returns the requester's own profile line (same text as in a DM),
  because the context already uses it for that turn. Without a profile it says to set one in a DM.

### B. Tutor topics

Storage, new migration `0002_tutor_topics.sql` (stage 2 moves to `0003`):

```sql
CREATE TABLE tutor_topics (
    chat_key text NOT NULL,
    thread_key text NOT NULL DEFAULT '',
    target text CHECK (char_length(target) <= 40),
    level text CHECK (level IN ('A1','A2','B1','B2','C1','C2')),
    explain text CHECK (char_length(explain) <= 40),
    set_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, thread_key),
    CHECK ((target IS NULL) = (level IS NULL)),
    CHECK (explain IS NULL OR target IS NOT NULL),
    CHECK (target IS NULL OR explain IS NULL OR target <> explain)
);
```

`thread_key` is the message's `thread_id`, or `''` when it has none. Telegram sets `thread_id` only
for forum-topic messages, so `''` means a non-forum group as a whole, or a forum's General topic. A
`''` row applies **only** to messages without a `thread_id`; it never covers other topics. Values
are re-validated on read against `languages.py`/`LEVELS`; an invalid row reads as unmarked (log
`tutor topic invalid` without values).

**Command** `/tutor here …` (groups only; in a DM reply that it marks a group topic and do nothing):

| Input | Behaviour |
| --- | --- |
| `/tutor here` | Mark this topic (or chat) with no default language. |
| `/tutor here <language> <level> [<explain>]` | Mark with a default, parsed by the stage 1 parser. Default explanation language when omitted: none (the model uses the group's usual language). |
| `/tutor here off` | Clear the mark for this topic (or chat). |
| `/tutor` in a group | Show this topic's status: `Tutor topic: on, default Japanese A2, explanations in English` / `on, no default language` / `off`, plus one line: "Set your own profile with /tutor in a DM with me." Never shows anyone's personal profile. |
| Any other `/tutor …` in a group | The stage 1 privacy reply (set personal settings in a DM). |

Permission: only a **setup manager or current native group admin** may mark or clear: copy the
polls `is_group_manager(access, platform, chat, user)` pattern into
`chatcore_tutor/permissions.py` (plugins don't import each other). Non-managers get
"Only a group admin can change tutor topics." and nothing is stored.

Mark and clear are group setting changes, so they go through the **confirmation flow** like
`/polltools`: build a `PendingAction` (`plugin="tutor"`, kinds `topic_set` / `topic_clear`,
payload `{chat_key, thread_key, target, level, explain}` with validated canonical values only,
`thread_id` set), call `ctx.confirm.request(action, ctx.user, reply_to=…)`, and return `None`. If
`ctx.confirm` is `None`, reply "Confirmation is unavailable; nothing changed." The commit handler
(`action_handlers()` returns `{"topic_set": h, "topic_clear": h}`) **re-checks** the manager
permission for the confirming requester and that the action's chat matches, then writes. Summaries
say exactly what changes: "Make this topic a tutor topic (default Japanese A2, explanations in
English)" / "Stop tutor mode in this topic".

**Pin** in groups: true when the current `(chat.key, thread_id or '')` is a marked tutor topic,
for every speaker. DMs: unchanged (own active profile).

**Context** (trusted, built only from table values, ≤ 400 characters for every combination, which
a test must prove by enumerating all language pairs and levels):

| Situation | Text |
| --- | --- |
| DM, active profile | stage 1 profile line (unchanged) |
| Group, speaker has an active profile | the same profile line; in a tutor topic append " Everything in this topic is language practice." |
| Tutor topic, no profile, topic default | "This topic is for language practice: {target} at CEFR {level}; explain in {explain or 'the group's usual language'}; romanisation: {system or 'none'}. Everything in this topic is language practice." |
| Tutor topic, no profile, no default | "This topic is for language practice. Infer the language being practised from the conversation and explain in the group's usual language. Everything in this topic is language practice." |
| Group, not a topic, no profile | `None` |

The stage 1 instructions don't change, so the routing calibration doesn't need re-running.

### C. Wiring and privacy

- `TutorPlugin(access, platform)`; `apps/sb_bot/src/sb_bot/app.py` passes them (keep
  `TUTOR_ENABLED` gating).
- `purge_chat(chat)`: delete that chat's `tutor_topics` rows. `purge_user`: unchanged (profile).
- Logs: `tutor topic set|cleared chat=<key> thread=<id or ->` and permission refusals by IDs only.

## You own

- `packages/core/src/chatcore/app.py` (only the auto-address step in `_on_message` and `_auto_addressed`), `packages/core/src/chatcore/plugin.py` (only `BasePlugin.auto_address`), `packages/core/tests/test_auto_address.py` (new)
- `packages/plugins/tutor/**` (new `permissions.py`, `migrations/0002_tutor_topics.sql`,
  repository/model additions, plugin changes, tests)
- `apps/sb_bot/src/sb_bot/app.py` (constructor arguments only), `apps/sb_bot/tests/test_tutor_wiring.py`

Not owned: other core modules, other plugins, applied migrations (`0001_tutor_profiles.sql` must not change),
`gates.py` of any plugin (don't add a tutor gate), the routing set and calibration script.

## Tests

- Group profile injection: speaker A with an active profile, skill active in a group turn → A's
  line in context; speaker B in the same group → none; A paused → none.
- Topic pin: marked topic pins for a speaker with a profile (their profile line plus the topic
  sentence) and for one without (topic default line; and the infer line when there's no default).
- Other topics unaffected: a mark on thread `7` doesn't pin thread `8` or the `''` chat entry,
  and a `''` mark doesn't pin thread `7`.
- Non-manager `/tutor here` refused, nothing stored, no pending action.
- Manager flow: `/tutor here japanese A2 english` creates a pending action with canonical values;
  committing writes the row; commit by a requester who lost manager rights is refused.
- `/tutor here off` clears after confirmation; `/tutor` in the group shows status and never a
  personal profile; personal `/tutor japanese A2` in a group is still refused.
- `/tutor here` in a DM does nothing.
- `get_tutor_profile` in a group returns only the requester's profile.
- Context length ≤ 400 for every language pair, level and situation above.
- Postgres repository integration tests (skip without `TEST_PG_DSN`): CHECK constraints, PK on
  `(chat_key, thread_key)`, invalid row reads as unmarked, `purge_chat`.
- End-to-end (sb_bot, scripted provider, skills `on`, keyword router): a marked topic turn with no
  keyword hit has the `## language_tutor` section and the topic line; an unmarked topic doesn't.

## Done when

- Lint, format on changed files, and the full suite pass (integration tests skip without services;
  the reviewer runs them).
- With `TUTOR_ENABLED` unset, the app is unchanged.
- Production needs no new `.env` (the plugin is already on). Topics only work in chats where
  `SKILLS_MODE=on` applies (`SKILLS_CHATS`).

## D. Auto-reply in tutor topics (owner request, same PR)

Owner: "in a tutor enabled topic, can it act like dm mode where it replies to any message regardless
of tag or non tag bot?"

**Core hook (additive).** `BasePlugin` gains `async def auto_address(self, message:
IncomingMessage, stores: Stores) -> bool: return False`, documented as: "Return True to treat an
untagged group message as addressed to the bot. Called only for human, non-command, non-empty
group messages that routing would otherwise ignore." Add it to the `Plugin` protocol docstring
list the way `before_turn` is handled (call it through `getattr`, so plugins without it work).

In `BotApp._on_message` (`packages/core/src/chatcore/app.py`), right after
`route = decide(...)` and the `prompt_command` override, and **before** the restriction block:

```python
auto = False
if (
    prompt_command is None
    and route in {Route.IGNORE, Route.CHATMODE}
    and not message.is_dm
    and not message.user.is_bot
    and (message.text.strip() or message.attachments)
    and not message.text.lstrip().startswith("/")
    and await self._auto_addressed(message)
):
    route, auto = Route.RESPOND, True
```

`_auto_addressed` asks each plugin's `auto_address` in order and returns on the first True;
an exception is logged as `plugin %s auto_address failed: %s` (class name only) and counts as
False. If `auto` and the sender has a restriction, return silently (no restricted notice for
messages that never addressed the bot). Everything after is the normal `RESPOND` path, so the
per-user hourly limit, the per-chat daily limit and the daily token budget all apply unchanged.
Nothing changes for DMs, commands, reply codes, or messages already routed to `RESPOND`/`COMMAND`.

**Tutor plugin.** `tutor_topics` gains `auto_reply boolean NOT NULL DEFAULT false` (edit
`0002_tutor_topics.sql`; it hasn't been applied anywhere yet). `TutorTopic.auto_reply: bool`.
`auto_address(message, stores)`: True only when the message is in a group and its exact
`(chat.key, thread_id or '')` is a marked topic with `auto_reply` true.

Commands (admin-only and confirmed exactly like marking; kind `topic_reply`, payload
`{chat_key, thread_key, auto_reply}`; commit re-checks the manager and that the topic is still
marked):
- `/tutor here quiet`: replies only when tagged or replied to. `/tutor here auto`: replies to every
  message. On an unmarked topic: "This topic isn't a tutor topic. Use /tutor here first." and no
  action.
- Auto-reply is **opt-in** (owner: "can be a setting to enable i guess"): a new mark defaults to
  auto-reply off (mentions-only). Re-marking an existing topic keeps its current setting.
- `/tutor` status adds "replies to every message" or "replies only when tagged".

In groups that run mentions-only, commands still need the bot's name (`/tutor@bot here`); keep the
help text using `ctx.command_form`.

**Rate-limit implications** (state in the PR): every auto-replied message counts toward
`RATE_USER_PER_HOUR` (20) and the whole group's `RATE_CHAT_PER_DAY` (150), and spends the daily
token budget. Past a limit, each further message gets the existing limit notice.

**Tests.**
- Core (`packages/core/tests/test_auto_address.py`): an untagged group message with a plugin
  returning True gets a response; returning False (or raising, logged by class name) stays
  ignored; bot-authored, empty, and `/command` messages never consult the hook; a restricted
  sender gets no notice; DMs and tagged messages behave as before.
- Tutor: auto-addressed in the marked topic only (other thread and the `''` chat entry not);
  quiet mode returns False; `/tutor here quiet|auto` permission, confirmation and unmarked-topic
  refusal; re-mark keeps the setting; status text.
- sb_bot end to end (`group_mentions_only=True` as in production, scripted provider): an untagged
  message in a marked topic gets a reply; the same message in another topic doesn't; a bot sender
  is ignored; after quiet mode an untagged message is ignored and a tagged one is answered.
