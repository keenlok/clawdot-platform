# Language tutor, stage 2: review cards and a daily reminder

Status: Planned. Builds on stage 1, [2026-11-02-language-tutor.md](2026-11-02-language-tutor.md)
(`chatcore_tutor`, the `language_tutor` skill, `/tutor` profiles, `Skill.context`/`Skill.pin`).
Start this only after stage 1 is merged and has run in the owner's DM for a while.

Read first: `AGENTS.md`, the stage 1 plan, `packages/plugins/tutor/`,
`packages/plugins/jobs/` (`service.py` `request_create`/`commit`, `worker.py` reminder delivery,
`repository.py` `reserve_delivery`/`finish_run`, `timing.py`), `packages/plugins/memory/` (a
direct-write tool and its untrusted-turn rule), `plans/bucket-a-direct-writes.md` (which writes
are direct and which confirm), `packages/core/src/chatcore/command_prompts.py` (`CommandReply`
with `awaits_reply`).

## Goal

Words or phrases the learner gets wrong become per-person review cards scheduled with SM-2. The
learner reviews them with `/review` in their DM, without an LLM call, and can turn on a daily
reminder that the jobs plugin delivers.

## Sources and licences

- SM-2 is the published SuperMemo-2 algorithm (P. Woźniak, 1987). Implement it from the
  algorithm, not from any repository.
- `spyrae/lingo` was checked on 2026-11-02: its README says MIT but the repository has no
  LICENSE file. Treat it as ideas only (cards keep easiness, interval and repetitions; quality is
  rated 0–5). Copy no code.
- `tatat/agents-playground` has no licence: ideas only.

## Design decisions

1. **Cards live in `chatcore_tutor`**, per person (`user_key`) and per target language. They are
   created only in the person's own DM, only for that person.
2. **Card creation is a direct write** in the same class as `remember` (bucket A): it writes at
   once in a clean turn, echoes the card ID, and undo is `delete_tutor_card`. On a turn that has
   read untrusted content, the tool refuses and asks the person to repeat the request, exactly as
   the memory tools do. Card text is short and length-capped.
3. **Review runs without the LLM.** `/review` shows a card; a reply reveals the answer and asks
   for a 0–5 rating; a digit reply schedules the card and shows the next. The "current card" is
   always the earliest due card, so no session state is stored.
4. **The daily reminder goes through the jobs plugin.** Jobs reminders are one-shot today, so this
   stage adds a **daily repeat** to reminders in `chatcore_jobs` (service and worker only; the
   `create_reminder` tool schema doesn't change). The tutor plugin creates the repeating reminder
   through `JobsService.request_create`, and slash commands keep the confirmation step.
5. Everything stays behind `TUTOR_ENABLED`. Repeating reminders are only created by the tutor
   path in this stage.

## You own

- `packages/plugins/tutor/**` (new migration `0003_tutor_cards.sql`, `srs.py`, card repository
  methods, tools, `/review` command, tests)
- `packages/plugins/tutor/pyproject.toml` (add the `chatcore-jobs` dependency) and `uv.lock`
- `packages/plugins/jobs/src/chatcore_jobs/service.py`, `worker.py`, `repository.py`, `timing.py`
  (daily repeat only), `packages/plugins/jobs/tests/test_daily_repeat.py` (new)
- `apps/sb_bot/src/sb_bot/app.py` (pass the jobs plugin to `TutorPlugin`)
- `apps/sb_bot/tests/data/routing_set.jsonl` and its tests (card tool labels)
- `AGENTS.md` (one line: add tutor → jobs to the allowed plugin dependencies)

Not owned: core, other plugins, the jobs tool schemas and gates, applied migrations.

## 1. SM-2 (`chatcore_tutor/srs.py`)

```python
@dataclass(frozen=True, slots=True)
class Schedule:
    easiness: float      # starts at 2.5, floor 1.3
    interval_days: int   # 0 for a new card
    repetitions: int     # consecutive correct reviews

def review(schedule: Schedule, quality: int, today: date) -> tuple[Schedule, date]:
```

Rules: quality outside 0–5 raises `ValueError`. Easiness += 0.1 − (5−q)(0.08 + (5−q)·0.02),
floored at 1.3. q < 3 → repetitions 0, interval 1. Otherwise repetitions + 1; interval 1 for the
first, 6 for the second, then round(previous interval × new easiness), at least 1. Return the new
schedule and `today + interval`. Interval capped at 365 days. Pure function; no clock inside.

## 2. Cards

Migration `0003_tutor_cards.sql` (`0002` is stage 1.5's `tutor_topics`):

```sql
CREATE TABLE tutor_cards (
    id bigserial PRIMARY KEY,
    user_key text NOT NULL,
    target text NOT NULL CHECK (char_length(target) <= 40),
    front text NOT NULL CHECK (char_length(front) BETWEEN 1 AND 200),
    reading text CHECK (char_length(reading) <= 200),
    back text NOT NULL CHECK (char_length(back) BETWEEN 1 AND 300),
    note text CHECK (char_length(note) <= 200),
    easiness real NOT NULL DEFAULT 2.5 CHECK (easiness >= 1.3),
    interval_days integer NOT NULL DEFAULT 0 CHECK (interval_days BETWEEN 0 AND 365),
    repetitions integer NOT NULL DEFAULT 0 CHECK (repetitions >= 0),
    due_on date NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    reviewed_at timestamptz,
    UNIQUE (user_key, target, front)
);
CREATE INDEX tutor_cards_due_idx ON tutor_cards (user_key, due_on, id);
```

`front` is the target-language form to recall, `reading` its romanisation, `back` the meaning in
the explanation language, `note` the short "why" from the correction.

Limits (`TutorSettings`): `TUTOR_MAX_CARDS_PER_USER=500`, `TUTOR_MAX_NEW_CARDS_PER_DAY=30`,
`TUTOR_REVIEW_BATCH=20` (most cards shown per day by `/review`).

## 3. Tools (added to the `language_tutor` skill)

All DM only (in a group: refuse with the privacy line from stage 1); identity from
`ctx.user.ref.key`; target language from the active profile (refuse with the `/tutor` usage if
there is none). Results that echo card text are returned `untrusted=True` (the text came from a
model turn).

- `add_tutor_card(front, back, reading?, note?)`: direct write as described above. Duplicate
  `front` updates `back`/`reading`/`note` and keeps the schedule. Enforce caps. New cards are due
  today. Returns `Added card #12: …`.
- `delete_tutor_card(card_id)`: deletes only the requester's own card. Direct write.
- `list_tutor_cards(limit?)`: the requester's cards, due ones first, at most 20.

Skill instructions gain (keep the total ≤ 800 characters, trim stage 1 text if needed): when you
correct a word or phrase the learner got wrong and their profile is on, add at most two cards per
turn for the most useful corrections and say so in one short line; never add cards for typos.
Routing rows: add 3 rows for "add this to my cards", "what cards do I have", "delete card 12".

## 4. `/review`

DM only. Reads due cards (`due_on <= today` in the person's display time zone, from
`chatcore.display_time` resolution, falling back to UTC).

| Input | Behaviour |
| --- | --- |
| `/review` | If due cards: show the earliest due card's `front` only (the reading and meaning stay hidden until the reveal), with "Reply with your answer, or anything to reveal." (`CommandReply(awaits_reply=True)`). If none: "No cards due. Next due: <date>." |
| reply that is not a lone digit | Reveal: front, reading, back, note; ask "How well did you know it? Reply 0–5." (awaits reply). |
| reply `0`–`5` | Apply SM-2 to the earliest due card, save, then show the next due card or "Done for today: N reviewed." Stop after `TUTOR_REVIEW_BATCH` reviews in a day. |
| `/review stats` | Counts only: total cards, due today, reviewed today. |
| `/review daily HH:MM` | Create a daily repeating reminder in this DM via jobs (confirmation step applies). Replace an existing tutor reminder. |
| `/review daily off` | Cancel the tutor reminder. |

The rating applies to the card that was due first at the time of the reply; if the person reviews
on two devices at once the second rating may land on the next card. Accept that; document it.

Card text in command replies is the person's own data shown back to them in their DM; send it as
plain text (no Markdown parsing).

## 5. Jobs: daily repeat

`JobsService.request_create` accepts `data["repeat"] == "daily"` for reminders (any other value
rejected). The payload stores `repeat`, the original `local_time` time-of-day (`HH:MM:SS`) and the
IANA `timezone`. The worker, after a successful reminder delivery of a repeating job, sets
`next_run_at` to the next occurrence of that local time in that zone (strictly after now; on a DST
gap use the first valid instant after it; on an overlap the earlier one), keeps the status
`active` and increments `occurrence`. A failed delivery follows the existing retry rules. Cancel,
restore, per-user caps and `JOBS_DM_ENABLED`/the per-DM reminders toggle apply unchanged; when the
DM toggle is off, `/review daily` explains that reminders are off in this chat and how to enable
them. The confirmation text says "every day at HH:MM <zone>". The tool schema of
`create_reminder` does not change, so the model can't create repeating reminders in this stage.

The tutor reminder text is fixed: "Language review time. Send /review to see today's cards." The
tutor plugin remembers the job ID in a `tutor_reminders(user_key PRIMARY KEY, job_id uuid)` row
(same migration) to replace or cancel it.

## 6. Privacy

`purge_user`: delete the person's cards and reminder row (the jobs plugin's own `purge_user`
removes the job). `purge_older_than`: nothing (cards are kept while the person keeps them).
Logs carry IDs and counts only, never card text.

## Tests

- SM-2: the published sequence for q=5,5,5 (1, 6, 16 days at EF 2.5→2.6…), q<3 resets, EF floor,
  365 cap, invalid quality.
- Cards: caps, duplicate update keeps schedule, cross-user isolation (B can't list/delete A's
  cards by ID), group refusal, tainted-turn refusal, no profile refusal.
- `/review`: due ordering, reveal/rate cycle, batch stop, time-zone "today", stats counts, no text
  in logs (caplog).
- Jobs daily repeat: next occurrence across a DST change (use a zone with DST, e.g.
  `Europe/London`), delivery reschedules and stays active, cancel stops it, repeat rejected for
  watches and for unknown values, the `create_reminder` tool schema and request snapshot
  unchanged. Postgres repository tests are integration tests (skip without `TEST_PG_DSN`).

## Done when

- Lint, format on changed files and the full test suite pass; integration tests pass with Postgres.
- With `TUTOR_ENABLED` unset, tools, skills, commands and the jobs tool schemas are unchanged.
- Owner `.env` for this stage (owner applies, not the PR): `JOBS_DM_ENABLED=true` if it isn't
  already, then reminders on in their DM.
- Left for later: buttons for ratings, cards from photos, sharing decks, a model-facing repeat
  option on `create_reminder`.
