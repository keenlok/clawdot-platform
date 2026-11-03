# Language tutor, stage 1: skill and tutor profile

Status: Planned. Stage 2 (review cards and reminders) is
[2026-11-02-language-tutor-cards.md](2026-11-02-language-tutor-cards.md).

Read first: `AGENTS.md` (plugin layout, skills, import boundaries, checks), `plans/skill-layer.md`
§3 (skill declarations) and the files that define the contracts this builds on:
`packages/core/src/chatcore/skills.py` (`Skill`, `SkillSet`), `packages/core/src/chatcore/skill_turn.py`
(`SkillTurn.build`), `packages/core/src/chatcore/agent.py` (`_context`, the "Active skills" section),
`packages/core/src/chatcore/plugin.py` (`BasePlugin`, `Command`, `CommandContext`),
`packages/core/src/chatcore/display_time_plugin.py` (a small plugin with a personal-setting command),
`packages/plugins/polls/` (plugin package layout, Postgres repository, migrations, tests),
`apps/sb_bot/src/sb_bot/app.py` (plugin wiring, `settings_from_env`).

## Problem

The owner learns a language in their DM with the bot. Answers drift into the language being
practised, and the owner keeps asking the bot to explain in a language they understand. Part of
the cause: the core prompt says "Answer in the user's language", so a message written in the
target language pulls the explanation into it. Nothing tells the model the learner's level or
explanation language, and there is no per-person place to keep them.

## Design

1. A new plugin `chatcore_tutor` (`packages/plugins/tutor`) declares a `language_tutor` skill with
   trusted instructions: always explain in the learner's explanation language, give target-language
   lines with romanisation (when the script is not Latin) and a translation, keep corrections short
   with the reason, adapt to the CEFR level.
2. A per-person **tutor profile** (target language, CEFR level, explanation language, on/off) set
   with `/tutor` in a DM and stored per user. Only the owner of a profile sees it.
3. Two small, additive core hooks on `Skill`, so the profile reaches the prompt as **trusted**
   text and the rule can't drift:
   - `context`: per-turn text appended to the skill's instructions when the skill is active.
   - `pin`: when it returns true, the skill is active this turn even without a router hit.
   The tutor plugin pins `language_tutor` for a person in their own DM while their profile is on.
   Profile values come only from a fixed language table and the CEFR enum, never free text, so
   the generated text can't carry injected instructions.
4. Off by default: the plugin loads only when `TUTOR_ENABLED=true`. Merging this PR changes nothing
   in production until the owner sets it.

Ideas only, no copied text: the profile shape (target, CEFR level, native language) and the
"mistake → fix → why" correction pairing follow `tatat/agents-playground`
`skills/language-tutor/SKILL.md`, which has **no licence**. Do not copy any of its wording.

## You own

- `packages/plugins/tutor/**` (new package: `pyproject.toml`, `src/chatcore_tutor/__init__.py`,
  `plugin.py`, `languages.py`, `models.py`, `repository.py`, `settings.py`, `gates.py` only if you
  add a gate (you shouldn't need one), `migrations/__init__.py`, `migrations/0001_tutor_profiles.sql`,
  `tests/**`)
- `packages/core/src/chatcore/skills.py` (only the two new `Skill` fields and their validation)
- `packages/core/src/chatcore/skill_turn.py` (only pin evaluation in `SkillTurn.build`)
- `packages/core/src/chatcore/agent.py` (only the "Active skills" section in `_context`)
- `packages/core/tests/test_skill_context_pin.py` (new)
- `apps/sb_bot/src/sb_bot/app.py` (wire the plugin behind `TUTOR_ENABLED`)
- `apps/sb_bot/pyproject.toml` (add the `chatcore-tutor` dependency), root `uv.lock` (via `uv lock`)
- `apps/sb_bot/.env.example` (document `TUTOR_ENABLED`)
- `apps/sb_bot/tests/data/routing_set.jsonl`, `apps/sb_bot/tests/test_routing_set.py`,
  `apps/sb_bot/tests/test_skill_routing_set.py` (set `TUTOR_ENABLED=true` in the fixture env)
- `apps/sb_bot/scripts/calibrate_router.py`
- `apps/sb_bot/tests/test_tutor_wiring.py` (new)
- Any existing test that fails only because the skill catalog, command list or `/help` output
  gained the tutor entries when enabled (update the expectation; say which in your report).

You may **not** edit any other core module, any other plugin, `CORE_SAFETY_PROMPT`, the agent
request snapshot `packages/core/tests/snapshots/agent_requests_off.json`, or any applied migration.
If one must change, stop and explain in your report.

## 1. Core: `Skill.context` and `Skill.pin`

In `packages/core/src/chatcore/skills.py`, add two fields to the frozen `Skill` dataclass, after
`sticky_turns`, both defaulting to `None`:

```python
context: Callable[[ToolContext], Awaitable[str | None]] | None = None
"""Trusted per-turn text appended to ``instructions`` while the skill is active. Build it only
from validated, enumerated values (never user free text or tool output). At most 400 characters;
longer text is dropped and logged. Errors are logged by class name and skipped."""
pin: Callable[[ToolContext], Awaitable[bool]] | None = None
"""Return True to make the skill active this turn without a router hit (for example a mode the
person switched on). Errors count as False."""
```

`SkillSet._clamp` and the instruction override/addition code must carry both fields through
(`dataclasses.replace` already does). Generated `*_other` skills never have them.

In `SkillTurn.build` (`skill_turn.py`): after `hits` are computed and before `ordered` is built,
evaluate `pin` for every catalog skill that has one, in catalog order, with `ctx`. Wrap each call
in `try/except Exception` and log `skill pin failed skill=%s error=%s` with the class name only.
Pinned skills go **first** in `ordered` (before router hits and inherited skills, without
duplicates), so a person's explicit mode is never crowded out by `max_active`. Add pinned names to
`_fresh` exactly as router hits are (a pinned skill restarts its sticky count). Nothing else in
`build` changes; availability and gates still apply to pinned skills. Don't change `RouteResult`
or the router; metrics already report `skills` from `active`.

In `agent._context`, the "Active skills" section: for each active skill, if `skill.context` is set
and `tool_ctx` is not `None`, await it (`try/except Exception`, log
`skill context failed skill=%s error=%s` by class name). If it returns a non-empty string of at
most 400 characters, append it to that skill's instructions under the same `## name` heading,
separated by a newline. A skill with no instructions but non-empty context still gets a section.
Text over 400 characters is dropped with a warning `skill context too long skill=%s chars=%d`.
`off` mode is unchanged (no hook runs), so `agent_requests_off.json` must not change.

## 2. Plugin package `chatcore_tutor`

Follow the polls package layout. `pyproject.toml`: name `chatcore-tutor`, version `0.1.0`,
`dependencies = ["chatcore"]`, same build backend as polls. Add it to `apps/sb_bot/pyproject.toml`
dependencies the way the other plugins are listed (check for a `[tool.uv.sources]` workspace entry
and copy the pattern), then run `uv lock`.

`__init__.py`: docstring, re-exports and `__all__` only: `TutorPlugin`, `TutorSettings`.

### `languages.py`

A fixed table of supported languages. Each entry: canonical display name, lowercase aliases
(English name, ISO 639-1 code, native name), and the romanisation system to show, or `None` for
Latin-script languages. At least:

| Canonical | Aliases (besides the casefolded name) | Romanisation |
| --- | --- | --- |
| English | `en` | — |
| Japanese | `ja`, `日本語` | Hepburn romaji |
| Mandarin Chinese | `chinese`, `mandarin`, `zh`, `中文`, `普通话`, `华语` | Hanyu Pinyin |
| Cantonese | `yue`, `粤语`, `廣東話` | Jyutping |
| Korean | `ko`, `한국어` | Revised Romanization |
| Thai | `th`, `ภาษาไทย` | RTGS |
| Malay | `ms`, `bahasa melayu` | — |
| Indonesian | `id`, `bahasa indonesia` | — |
| Vietnamese | `vi`, `tiếng việt` | — |
| Tamil | `ta`, `தமிழ்` | ISO 15919 |
| Hindi | `hi`, `हिन्दी` | IAST |
| Tagalog | `tl`, `filipino` | — |
| French, Spanish, German, Italian, Portuguese, Dutch | their ISO codes | — |
| Russian | `ru`, `русский` | scholarly transliteration |
| Arabic | `ar`, `العربية` | transliteration |

`lookup_language(token: str) -> Language | None` matches casefolded aliases exactly. Multi-word
aliases (`bahasa melayu`) are matched by the command parser before splitting (see §4). Expose
`LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")` and `parse_level(token) -> str | None`
(case-insensitive).

### `models.py`

```python
@dataclass(frozen=True, slots=True)
class TutorProfile:
    user_key: str
    target: str        # canonical name from languages.py
    level: str         # one of LEVELS
    explain: str       # canonical name from languages.py
    active: bool
    updated_at: datetime
```

### Migration `0001_tutor_profiles.sql`

```sql
CREATE TABLE tutor_profiles (
    user_key text PRIMARY KEY,
    target text NOT NULL CHECK (char_length(target) <= 40),
    level text NOT NULL CHECK (level IN ('A1','A2','B1','B2','C1','C2')),
    explain text NOT NULL CHECK (char_length(explain) <= 40),
    active boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (target <> explain)
);
```

`user_key` is `UserRef.key` (platform-qualified). The profile is per person, not per chat.

### `repository.py`

`TutorRepo(db: PgDatabase)` (raise `TypeError` for anything else, as `PollsRepo` does) with
`get(user_key)`, `upsert(user_key, target, level, explain)` (sets `active=true`, `updated_at=now()`),
`set_active(user_key, active) -> bool` (False when no row), `delete(user_key) -> bool`.
Values read back are re-validated against `languages.py`/`LEVELS`; a row that no longer validates
is treated as missing (log `tutor profile invalid` with no values).

Also provide `MemoryTutorRepo` with the same methods for unit tests and for when `stores.db` is
`None` (console runs). Pick the repo the way polls does (`stores.db` present → Postgres).

### `settings.py`

`TutorSettings(BaseSettings)` with `TUTOR_ENABLED: bool = False`. App reads it with
`settings_from_env`.

## 3. The `language_tutor` skill (`plugin.py`)

`TutorPlugin(BasePlugin)`: `name = "tutor"`, `help_title = "Language tutor"`,
`migrations_package = "chatcore_tutor.migrations"`.

**Tool** (the skill needs one; read-only): `get_tutor_profile`, no arguments. Description: returns
the requester's own language-tutor settings. Handler: outside a DM, return (not an error)
"Tutor settings are private; use {cmd:tutor} in a direct message." rendered with `ctx.command_form`.
In a DM, return the profile as one line (`Japanese, level A2, explanations in English, on`) or
"No tutor profile. The person can set one with /tutor <language> <level> [<explanation language>]."
Identity comes from `ctx.user.ref.key` only; the tool takes no user argument.

**Skill:**

```python
Skill(
    "language_tutor",
    "Language practice: explanations, corrections and drills at the learner's level",
    examples=(...),           # 10–12, see below
    tools=("get_tutor_profile",),
    keywords=(...),           # see below
    commands=("tutor",),
    max_steps=3,
    sticky_turns=3,
    instructions=LANGUAGE_TUTOR_INSTRUCTIONS,
    context=self._skill_context,
    pin=self._skill_pin,
)
```

**Instructions** (`LANGUAGE_TUTOR_INSTRUCTIONS`, ≤ 800 characters, written for any bot, your own
wording). They must say, in this order of priority:
1. Explain in the learner's explanation language: the tutor profile's if one is given below,
   otherwise the language the person normally writes to you in. This holds even when their message
   is in the language they are practising, and it overrides answering in the message's language.
2. Every target-language line is followed by its romanisation in brackets when the language uses a
   non-Latin script, then its translation in the explanation language.
3. Corrections are short: what they wrote → the corrected form, plus one brief reason. At most
   three corrections per message; don't rewrite everything.
4. Match the CEFR level: A1–A2 short sentences and everyday words; B1–B2 natural phrasing and
   common idioms; C1–C2 nuance and register.
5. When the person is practising, end with one short prompt that continues the practice.
6. A plain translation request gets the translation (with romanisation), no lesson.
7. If this turn isn't about language, help normally, still in the explanation language.

**Context hook** `_skill_context(ctx) -> str | None`: return `None` unless `ctx.is_dm` and the
requester has an **active** profile. Otherwise return, built only from table values:
`"Tutor profile (set by this person with /tutor): learning {target} at CEFR {level}; explain in
{explain}; romanisation: {system or 'none'}. Follow it even when their message is in {target}."`

**Pin hook** `_skill_pin(ctx) -> bool`: true only when `ctx.is_dm` and the requester has an active
profile. Groups never pin, never inject.

Both hooks do one keyed read. Don't cache across turns (the person may run `/tutor` between turns).

**Examples** (routing only; 10–12, English, Chinese and Singlish, none from real chats). Aim at
someone practising or asking about a language, and avoid phrasings that overlap other skills
(reminders, memory, research, trips):
`"correct my japanese sentence"`, `"how do I say this in korean"`, `"teach me some basic thai
phrases"`, `"is my grammar right in this french sentence"`, `"quiz me on japanese vocabulary"`,
`"explain this japanese grammar point"`, `"帮我纠正这句日语"`, `"这个韩语语法怎么用"`,
`"教我几句简单的泰语"`, `"can help me practise my japanese anot"`, `"what's the difference between
は and が"`.

**Keywords** (casefolded; keep them specific so a passing mention doesn't route): `grammar`,
`vocabulary`, `vocab`, `conjugate`, `conjugation`, `jlpt`, `hsk`, `topik`, `cefr`, `romaji`,
`furigana`, `pinyin`, `语法`, `生词`, `文法`. Do **not** use `practice`, `practise`, `translate`,
`learn` or language names on their own.

## 4. `/tutor` command

DM only. In a group, reply "Tutor settings are private. Send /tutor to me in a direct message."
(use `ctx.command_form`) and store nothing.

| Input | Behaviour |
| --- | --- |
| `/tutor` | Show the profile line (as the tool does) plus a one-line usage hint; or usage if none. |
| `/tutor <target> <level> [<explain>]` | Validate, upsert, reply with the saved line. `explain` defaults to the existing profile's, else English. |
| `/tutor off` | Pause (keeps settings). Reply says the tutor is paused and `/tutor on` resumes. |
| `/tutor on` | Resume; if no profile, reply with usage. |
| `/tutor clear` | Delete the profile. |

Parsing: casefold, then replace known multi-word aliases with a single token before splitting on
whitespace. Accept the level token anywhere; of the language tokens, the first is the target and
the second the explanation language. Errors (unknown language, missing or bad level, target equal
to explanation language, extra tokens) reply with a short message listing supported languages and
levels, and write nothing. Cap input at 200 characters before parsing.

No confirmation step: it is the person's own setting, in their own DM, from an explicit slash
command (the same class as `/timezone` in a DM). Logs: `tutor profile set|paused|resumed|cleared
user=<key>` with no language values.

## 5. Privacy hooks

`purge_user(chat, user, stores)`: delete the profile for `user.key` whatever the chat (it's the
person's own data). `purge_chat`: nothing (profiles aren't chat data). `purge_older_than`: nothing.

## 6. App wiring (`apps/sb_bot/src/sb_bot/app.py`)

Read `TutorSettings` with `settings_from_env`. When `TUTOR_ENABLED` is true, append
`TutorPlugin()` to the plugin list next to the other optional plugins; otherwise don't load it (no
tool, no skill, no command, no migration run). Import through the package top level
(`from chatcore_tutor import TutorPlugin, TutorSettings`). Document in `.env.example`:

```
# Language tutor skill and /tutor profiles (DM only).
TUTOR_ENABLED=false
```

## 7. Routing set and calibration

Add rows to `apps/sb_bot/tests/data/routing_set.jsonl`, continuing the id sequence, all invented
(never real messages), labelled `"skills": ["language_tutor"], "tools": ["get_tutor_profile"]`
where they target the tutor, or other skills/none where they don't:
- 6 `direct` and 6 `paraphrase` tutor rows (en, zh, singlish mix).
- 3 `followup` tutor rows whose `prev` turn was a tutor turn (e.g. "ok next one", "and the past
  tense?").
- 4 `topic_change` rows: a tutor `prev` followed by a turn for another skill (expense, reminder).
- 6 **negative** rows that mention languages or words but aren't tutoring, labelled with their real
  skill or `[]`: e.g. "remind me about my japanese class at 7pm" (reminders), "add 30 for the
  korean bbq" (expenses), "remember that Siti speaks Thai" (memory), "research the best language
  schools in Tokyo" (research), "what's the weather in Seoul" (none), "the menu is all in Thai lol"
  (none). Use segment `topic_change` or `direct` as fits the existing schema; read the tests to
  see which rows are allowed `skills: []`.

Update `SKILLS`/`SKILL_TOOLS` in `apps/sb_bot/tests/test_routing_set.py`, and set
`"TUTOR_ENABLED": "true"` in the `catalog_app` env of `test_skill_routing_set.py`.

`apps/sb_bot/scripts/calibrate_router.py`: force `TUTOR_ENABLED=true` in the env it passes to
`build_app` (copy of `os.environ`), and add to each threshold line two counts: `fp=` (rows where
the router returned any skill not in the row's labels, over all rows) and `tutor_fp=` (rows not
labelled `language_tutor` that routed to it, over rows not labelled `language_tutor`). Counts only;
never print row text.

## Tests

Core (`packages/core/tests/test_skill_context_pin.py`, fakes only):
- A pinned skill is active with no router hit; pinned goes first and survives `max_active=1`
  against a router hit.
- `pin` raising → treated as False, warning logged with class name only, turn still builds.
- `pin` is not consulted for skills without one; unavailable pinned skill (gate OFF/UNCONFIGURED)
  follows normal availability rules.
- `context` text appears in the system prompt under the skill's heading only when the skill is
  active; absent when inactive; skipped when it raises or exceeds 400 characters; `off` mode never
  calls it (assert the hook was not awaited).
- The off-mode snapshot test still passes unchanged.

Plugin (`packages/plugins/tutor/tests/`):
- Parser: all forms in §4 table; aliases (`ja`, `日本語`, `bahasa melayu`); level anywhere and any
  case; target = explain rejected; unknown language rejected; extra tokens rejected; 200-char cap.
- Command: group invocation stores nothing and replies with the DM hint; DM set/show/off/on/clear.
- One person's profile is invisible to another: user B's `/tutor`, tool call and hooks see nothing
  of user A's profile.
- Hooks: context and pin are `None`/False in a group even with an active profile; False/None when
  paused; context text contains only table values (assert exact string for a Japanese/A2/English
  profile) and is ≤ 400 chars for every table pair.
- Tool: DM with and without profile; group returns the privacy line; no user argument accepted.
- `purge_user` deletes the profile.
- Repo: memory repo behaviour; Postgres repo as an **integration** test (skip without
  `TEST_PG_DSN`), including the CHECK constraints and that an invalid stored row reads as missing.
- Skill declaration validates (`SkillSet.build` with the plugin) and instructions ≤ 800 chars.

App (`apps/sb_bot/tests/test_tutor_wiring.py`):
- `TUTOR_ENABLED` unset → no `language_tutor` skill, no `tutor` command, no `get_tutor_profile`.
- `TUTOR_ENABLED=true` → all three present.
- Routing-set tests pass with the new rows.

## Done when

- `uv sync --locked --all-packages`, `uv run ruff check packages services apps tests`,
  `uv run ruff format --check` on changed files, and
  `uv run pytest -q -m "not browser and not sandbox_integration"` pass (integration tests skip
  without services; the reviewer runs them).
- `agent_requests_off.json` is unchanged.
- With `TUTOR_ENABLED` unset, the built app is identical in tools, skills and commands to `main`.
- Left for later, don't attempt: review cards, SM-2, reminders (stage 2), `/privacy` wording,
  running the calibration on the real model (the reviewer does it; it needs the model download).

## Calibration result (2026-11-02, real EmbeddingGemma, margin 0.05)

`apps/sb_bot/scripts/calibrate_router.py` with the pinned model. Baseline on `main` (5bb7c41) at
0.40: direct 41/53, paraphrase 24/44, followup 12/40, topic_change 28/40, disabled 31/40.

| Threshold | direct | paraphrase | followup | topic_change | disabled | tutor_fp |
| --- | --- | --- | --- | --- | --- | --- |
| 0.35 | 50/60 | 34/47 | 15/43 | 32/36 | 34/40 | 6/227 |
| **0.40 (prod)** | 49/60 | 30/47 | 13/43 | 32/36 | 31/40 | 5/227 |
| 0.45 | 46/60 | 25/47 | 9/43 | 29/36 | 29/40 | 2/227 |
| 0.50 | 45/60 | 21/47 | 4/43 | 27/36 | 25/40 | 2/227 |

At 0.40:
- Every direct and paraphrase tutor row routes. Two router-only follow-up misses ("ok next one")
  are covered by sticky inheritance in real turns, and by the pin in a profiled DM.
- On the 217 rows that existed before, the tutor adds **no false positives** and **no row loses a
  skill it routed to without the tutor** (compared per row with `TUTOR_ENABLED` off).
- All 5 tutor false positives are the new negatives that name a language (0.41–0.57): "add 30 for
  the Korean BBQ", "Remember that Siti speaks Thai", "Research the best language schools in
  Tokyo", "What's the weather in Seoul today?", "the menu is all in Thai lol". A false hit only adds
  the tutor section; the instructions open with an applicability guard telling the model to ignore
  it when a language is only mentioned. Other example sets were tried; each lowered recall as much
  as it lowered false positives, so the threshold stays 0.40.
