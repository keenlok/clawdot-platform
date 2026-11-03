# Skill layer, stage 4: new tools, matching hints, `/help` regrouping

Status: **built** on `origin/main` after stages 0–3 (2026-11-01). Design: `docs/plans/skill-layer.md`
§3.3, §5, §6, §6.1, §6.3 and §9 (stage 4 row). Menu trimming is out of scope: the user decided
to trim only when the Telegram menu nears its 100-command limit (§11, "trim at the limit").

Rollback: the new tools exist in every mode, like other plugin tools. `SKILLS_MODE=off` keeps the
legacy prompt; reverting is an ordinary code revert. No migrations: polls reuse their tables, and
reset and `/new` reuse the conversation store and the `/new` cache marker.

## 1. New tools

| Tool | Owner | Class | Gate (routing only) | Checked at call | Re-checked at commit |
|---|---|---|---|---|---|
| `list_polls` | polls | read, `untrusted=True`, output ≤ 3,000 chars | group | group, platform, allowlist | — |
| `close_poll` | polls | pending (`polls.close`) | group | creator or group manager; poll open in this topic | same poll id still open; creator or manager; requester, chat and topic match |
| `configure_polls` | polls | pending (`polls.configure`, the existing `/polltools` handler) | group + group manager | group manager | group manager; settings revision |
| `get_persona` | conversation | read, `untrusted=True` when set, ≤ 600 chars | — | — | — |
| `request_context_reset` | conversation | pending (`conversation.reset`) | DM, or owner/moderator (`can_reset`) | same | same; requester, chat and topic match |
| `start_new_conversation` | conversation | pending (`conversation.new`); registered only when conversation threads are on | group | group | requester, chat and topic match; sets only the requester's marker |

All write tools are `writes=True` without `direct`, per §6.3. None of the §6.1 excluded
operations gained a natural-language path: persona *set*, `/compact`, admin, budget and privacy
commands stay command-only.

`close_poll` works while polls are off, matching `/closepoll`. So the `polls` skill stays
available when polls are off: it offers `list_polls` and `close_poll` and carries the "polls are
off" hint for `create_poll`.

The `/new` cache key moved to `chatcore.threads.new_conversation_key`, used by the command, the
tool commit and the marker consumer. `/reset` and the reset commit share
`ConversationPlugin._reset`.

## 2. Skills and instructions

- `polls` (plugin): `create_poll`, `list_polls`, `close_poll`; examples extended for list and close.
- `conversation` (plugin, new): its registered tools; EN/ZH/Singlish examples.
- `feature_settings` (SBot composite): `configure_polls` added as optional.
- SBot instructions: `polls` now points to `list_polls` then `close_poll`. New `conversation`
  instruction separates the personal fresh start from the group-wide reset, and says the persona
  is data.

## 3. Hints match call-time errors

- `ToolContext` and `CommandContext` gained `command_form` (default `/name`). `BotApp` fills it with
  `/name@bot` in mention-only groups.
- Jobs: `chatcore_jobs.service.DISABLED_HINTS` is the single text for the routing gate and the
  call-time refusal (§5 "defense in depth"). DMs keep the short text.
- Polls: `chatcore_polls.service.POLLS_OFF_HINT` likewise, and it now mentions asking in plain words.

## 4. `/help`

Opens with "You can also just ask me in plain words…". Commands are grouped under each plugin's
`help_title` (new optional `BasePlugin` attribute; defaults to the capitalised plugin name), with
`/help` under "General" and `/new` under "Conversation". Admin-only commands are still hidden
from non-admins.

## 5. Menu limit guard (instead of trimming)

`chatcore_telegram.menu.fit_menu` is used by both Telegram adapters. It logs a warning at 90
commands per scope, and above 100 it logs an error and keeps the first 100, so `setMyCommands`
cannot fail and empty the menu. `test_telegram_menu_stays_below_trim_threshold` fails CI at 90.
SBot has 29 menu commands at this stage.

## 6. Incidental fix

`SkillTurn._refresh_use_skill_spec` now drops `use_skill` once `SKILLS_MAX_LOADS` is reached. Before,
it stayed offered while other skills were loadable, so the model could spend steps on loads that
could only fail. Two loads in the same response still get "Skill load limit reached".

## 7. Tests

- `packages/plugin-polls/tests/test_poll_tools.py`: list (untrusted, topic scope, cap, works
  while off), close (propose → commit → results, idempotent; permission at request and commit;
  wrong clicker), configure (manager plus confirmation), tool classes.
- `packages/core/tests/test_conversation_tools.py`: registration with and without threads,
  persona cap and untrusted flag, reset gate hint, reset propose/commit/recheck, `/new` marker
  only after confirm, shared marker key.
- Jobs and polls tests assert the call-time text equals the rendered hint. `/help` grouping test.
  Menu policy tests.
- Routing set: stage-4 rows labelled with their tools, plus 9 rows for list, close and configure
  polls. The "stage-4 tools absent" test is now "registered and labelled".
- `snapshots/agent_requests_off.json` regenerated. The diff is only the two new conversation tool
  specs and their names in `available_tools`.

Not done here: the console end-to-end run against the real provider (stage-4 gate) and stage 5
(`plan_outing` tuning).
