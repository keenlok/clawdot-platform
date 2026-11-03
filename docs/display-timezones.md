# Display time zones

The bot stores, compares and schedules instants in UTC. Display preferences only change how an
instant is shown in a reply; they never move a scheduled job or rewrite historical data.

## Commands and scope

- `/timezone` shows the effective display zone and where it came from.
- `/timezone Asia/Singapore` saves the zone immediately. In a group this changes the shared group
  setting and requires a fresh native group-administrator check for each setting or reset. In a
  private chat it changes only the sender's own preference.
- `/timezone reset` removes the setting in the same scope. Group reset has the same fresh
  administrator check. These reversible preference commands do not ask for an extra confirmation.
- `/time` shows the current time in the effective zone. `/time Europe/London` uses a one-off zone
  for that answer without changing a saved preference.

Group replies use the group's setting, or labelled UTC fallback. A member's private preference
never affects group replies. Private replies use only that person's saved DM preference, or
labelled UTC fallback. The bot does not infer a zone from Telegram, names, language or location.
Only installed IANA zone keys and `UTC` are accepted; abbreviations such as `EST` and `CET` are
rejected.

Displayed instants include the IANA zone and numeric UTC offset, so both sides of a daylight-saving
fold remain distinct. Scheduled reminders and poll closes continue to show the explicitly
requested scheduling zone in their creation confirmations.

## Model replies and other listings

The model receives an authoritative current UTC instant, the effective display zone and its
converted current time. Transcript timestamps remain UTC instants. The model is instructed to
convert instants when quoting them and honor a zone explicitly requested for that answer. An
explicit request such as “Use Singapore time for this group” or “Remember London time for my
replies” can save the preference immediately through the preference tools; `/timezone` remains an
alternative. In a group, the preference is shared and each write requires a fresh native
administrator check. A personal preference request made in a group should be made again in the
person's own DM. Time zones are settings, not long-term memories. The model can set or reset only
the current reply scope and never infers a change from a page, file, conversation history or
another member's text. After it reads untrusted tool content in a turn, it asks the user to repeat
the preference request in a separate message.

One-off questions such as “What time is it in Tokyo?” use the read-only current-time tool and leave
the saved preference unchanged. A successful save or reset takes effect for later reads in the same
turn and subsequent turns. `/time` remains the deterministic path for current time.

Job next-run, poll close and research status listings, access-pause notices, and administration
restriction/audit pause times use the same formatter. Stored restriction and audit values remain
UTC. Provider text and source timestamps are not reinterpreted.

## Storage, deletion and unchanged policies

Preferences are stored separately from chat settings. A shared group preference is keyed by that
chat. A personal preference is keyed by the user's own DM and cannot be read from another chat.
`/forgetme` deletes the user's personal preferences in the purged scopes while shared group
settings remain. `/purgechat` deletes all display preferences for the selected chat.

The operational `BUDGET_TZ`, privacy retention schedule, reminder and poll parsing, worker due
instants, recurring watch intervals, already scheduled jobs, date-only expense values, and ledger
export/report-date policy are unchanged. Expense settlement exports continue deriving dates with
their existing ledger zone. Rental pickup and return wall clocks remain wall-clock values without a
named-zone instant.
