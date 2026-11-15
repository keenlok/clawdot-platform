# Private DM trips

Status: Planned; owner approved on 2026-11-16. Claude planned; Codex CLI is installed, so Codex
executes and reviews, then Claude does the final review.

## Problem

Trips and expenses are group ledgers. In a DM, `ExpensesPlugin._chat` asks the app's
`group_for_dm` resolver, which returns the user's shared group only when there is exactly one.
So a trip started in a DM silently lands in that group, and with several shared groups every DM
expense command fails with "I can't identify your group from this DM". An owner wants trips
started in a DM to be private.

## Owner decisions (2026-11-16)

| Question | Decision |
| --- | --- |
| What is a private trip? | **Personal only**: a ledger kept in the DM itself; the requester is the only member and no splitting. |
| Starting a trip in a DM | **Private by default, no question.** Only a group the user names in the request ("start a trip in Japan Crew") gets a group trip. |
| Other DM expense commands | **Private ledger by default**; a group ledger only when the request names a group or a group trip. |

## Design

New `ExpensesPlugin(dm_ledger=...)` setting, `"group"` (default, today's behavior, unchanged) or
`"private"`. Apps opt in; framework behavior stays neutral.

With `dm_ledger="private"`, in a DM:

1. **Default target is the DM chat itself.** `_chat(ctx)` returns `ctx.chat`; every tool (start,
   add, list, balances, update, delete, restore, FX, export) then works on the private ledger
   with no other changes. Trip members are only the requester: `members` and `split_with` naming
   anyone else are refused with a clear message ("Private trips only track your own expenses;
   start a group trip in the group to split").
2. **Naming a group.** Expense tools gain an optional `group` argument (free text). It is
   resolved only among the requester's current member groups (`AccessPolicy.member_groups`,
   membership rechecked), by case-insensitive group title. Titles come from a new optional
   platform method `chat_title(chat) -> str | None` (Telegram `getChat`), cached like
   membership. No match or several matches → an error listing nothing the user isn't a member of.
3. **Naming a group trip.** When `trip` names a trip that exists in exactly one of the
   requester's member groups and not in the private ledger, that group's ledger is used. Private
   wins over a group trip with the same name.
4. Confirmations for a private trip are requested in the DM (same chat, so they reply normally).
   Group targets keep confirmations in the group, as v0.18.1 delivers them.

`dm_ledger="group"` keeps `group_for_dm` behavior exactly; the `group` argument and trip-name
resolution are offered only in private mode, so SBot's request snapshot is unchanged.

Security: the `group` argument and trip names are untrusted model input. Targets are only ever
groups the requester is a member of right now; titles are never shown for other groups; existing
gates and handler-time permission checks still apply. No gate/permission module change is
expected; if one is needed it is called out in the PR.

No migration: the ledger is already keyed by chat, and a DM chat key is a valid key.

## Tests

- Default mode: DM behavior unchanged (single group used, several groups → existing error);
  request snapshot unchanged.
- Private mode: DM `start_trip` creates the trip under the DM chat key with only the requester;
  `members`/`split_with` with others refused; add/list/balances use the private ledger.
- `group` argument: resolves a member group by title; refuses non-member groups, unknown and
  ambiguous titles; membership is rechecked (stale cache).
- Trip-name resolution: unique group trip used; private trip wins on a name clash; ambiguous
  names across groups refused.
- Confirmation for a private write is requested in the DM with a normal reply.
- Telegram `chat_title` uses `getChat` and returns `None` on errors.

## Release and rollout

Minor release **v0.19.0** (new setting, new optional platform method). Downstream, the owner
asked (2026-11-16) for **both bots** to use private mode, so anyone who may DM the bot gets their
own private ledger there. Each app sets `EXPENSES_DM_LEDGER=private` in clawdot-deployments.
Behavior change for existing users: DM expense commands that relied on "your only shared group"
now use the private ledger unless they name the group or one of its trips; the trip-name fallback
covers the common case. Existing group trips are untouched.
