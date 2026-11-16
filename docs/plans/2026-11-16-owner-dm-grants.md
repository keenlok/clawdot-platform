# Owner-granted DM access

Status: Shipped inactive in v0.20.0 (PR #54); approved by the owner on 2026-11-16 as written (grantees get the same DM
features as a group member; request notice included, off by default). Claude planned; Codex executes and reviews, then
Claude does the final review (`ARCHITECTURE.md` §7b). Release target: the next minor after
private DM trips (v0.19.0), so **v0.20.0**; if this merges first it takes v0.19.0 and trips
takes the next one.

## Goal

Let the owner allow specific people to DM the bot even though they share no admitted group with
it, granted "the same way" as trusted group invitations (v0.13.0 / v0.15.x): owner-only,
confirmed, audited, revocable. The motivating use is friends keeping a private DM expense ledger
(`dm_ledger="private"`, built separately), but the grant is generic DM access.

Today `AccessPolicy.can_dm` (`packages/core/src/chatcore/access.py`) is true only for admins,
setup delegates, moderators, or current members of an allowlisted group. Nobody else can DM.
**Default stays neutral:** with no grants, behaviour is identical to today.

## Decision table: may this user DM the bot?

| Priority | State | Result |
| --- | --- | --- |
| 1 | Global user block, or active global pause | Deny (existing restriction notice; unchanged) |
| 2 | Owner or admin | Allow (unchanged) |
| 3 | Setup delegate, moderator, or member of an allowed group | Allow (unchanged) |
| 4 | Active owner DM grant (no expiry, or expiry in the future) | **Allow (new)** |
| 5 | Expired or revoked grant, or none | Deny silently (unchanged), or the optional request notice below |
| 6 | Store or snapshot failure | Fail closed (unchanged) |

Grants are per platform identity (`tg:<positive user id>`), validated like `/admin trust`.

## Commands and confirmation

Owner-only, in the owner's own same-platform DM, using the existing PendingAction flow
(requester-bound, single-use, expiring, revision-checked):

- `/admin dmgrant tg:123 [DURATION]`: allow DMs. Optional `DURATION` (`12h`, `7d`, up to
  `365d`) sets an expiry; no duration means until revoked. Re-granting replaces the expiry.
- `/admin dmrevoke tg:123`: remove the grant. Takes effect on the user's next message.
- `/admin dmgrants`: list active grants with grantor, granted time and expiry.

Confirmation text spells out the effect, for example: "Confirm: allow tg:123 to chat with me in
a private DM until 2026-11-23 (no group access or admin role)." `authorize_change` adds
`dm_grant`/`dm_revoke` to owner operations; `check_target` already rejects owners and self.
Granting someone who already has DM access some other way is allowed (it survives them leaving
a group). The owner `/admin` menu and `docs/admin-setup.md` list the new commands.

**Natural-language flow.** Generalise the existing owner-only batch tool rather than adding a
second admin surface: a new narrow tool `manage_dm_access` (operations `grant`/`revoke`, up to
20 people, optional duration) beside `manage_trusted_inviters`, in the same owner/DM-gated
skill, reusing the `inviter_tools.py` resolver, freeze-then-confirm preview and
`apply_*_batch` atomic commit. Difference: the people being granted usually share no group with
the bot, so the resolver also accepts an explicit `tg:<id>` the owner typed. The preview shows
such a target as "unknown name, tg:123" so the owner confirms the exact ID; the model can never
commit a write without that confirmation. Gates go in `administration/gates.py` and the predicate
in `administration/permissions.py` (permission change, see below).

## Optional access-request notice (off by default)

The owner needs a friend's numeric ID. Today a denied DM is dropped silently, so the friend has
no way to see it. Add an app setting (core, for example `BotApp(dm_request_notice=False)`):
when on, a denied DM from a non-bot user gets one fixed, no-model reply such as "I only chat with
people the owner has approved. To ask for access, send the owner this ID: tg:123." Bounds: once
per user per 24 h (cache key), at most 30 notices per hour bot-wide, no message logging, no
plugin hooks, no owner notification (avoids a spam relay to the owner). Off means no change.

## Storage

Additive core migration **`0013_owner_dm_grants.sql`** (next after `0012`; the trips work
changes only the expenses plugin's migrations, re-check before merging):

```sql
CREATE TABLE administration_dm_grants (
    user_key text PRIMARY KEY,
    granted_by text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz
);
```

`AccessSnapshot` gains `dm_grants: Mapping[str, DmGrant]` (`granted_by`, `granted_at`,
`expires_at`), immutable, default empty so old callers and fixtures keep working. Memory and
Postgres stores implement it under the existing administration revision lock; one revision
increment per command or batch. Expired rows stay until revoked or re-granted (expiry is
evaluated at check time like pauses, so no sweeper is needed); listings hide them.

## Audit

Each grant/revoke writes an audit entry in the same transaction as the state change
(operation `dm_grant`/`dm_revoke`, subject user, global scope `tg:*`, detail = expiry or
"none"). A failed audit insert rolls back the change. A batch writes one entry per person. No-op
changes (revoking a missing grant, granting with an identical expiry) write nothing. Expiry
lapsing is not an audit event. `/admin audit` shows the entries to the owner only.

## Revocation and expiry

- `dmrevoke` and expiry take effect on the next access check: commits call `access.refresh()`,
  and `can_dm` compares `expires_at` with the current time on every check.
- Consumers that already re-check `can_dm` at use time (research job delivery, image, voice,
  cards, tutor, Google gates) stop for that user automatically.
- Data the user created in their DM (memory, a private ledger) is kept, as for a member who
  leaves every group; erasure stays with the existing privacy commands. Jobs or reminders
  already scheduled in that DM follow today's behaviour for departed members (known limit, not
  changed here).

## What a DM-granted user may do

- Talk to the bot in **their own DM only**, with the same DM features a group member gets
  there (tools gated on `can_dm`), within the same restrictions.
- **Nothing else:** no group admission (that is `trust`), no setup, moderation, admin or owner
  role, no `/admin` commands (the menu returns nothing for them). `member_groups` is empty for
  them, so search and privacy-scoped history expose no group content; tools that read other
  people's data keep their existing per-chat ownership checks.
- Trust and DM grants stay independent: trust still grants no DM access, a DM grant grants no
  invitation right.

## Interactions

- **Group blocks**: not involved; a DM grant never enables or admits a group.
- **User restrictions**: a global `/admin block` or active global pause wins over a grant.
  Group-scoped restrictions don't apply to DMs (unchanged).
- **Admins**: unchanged; they already have DM access. Only owners may grant or revoke.
- **Bridge transport**: the protected bridge keeps its independent fixed relay allowlist and no
  broker authority is expanded. If that allowlist drops DMs from unknown users, a grant has no
  effect there; grants are effective on direct transport. Downstream deployments must verify
  which transport each bot (SBot, Loki) uses before relying on this.

## Rate limits and budget

Grantees get no exemptions: the per-user hourly limit (`/admin limit` can lower it per person),
the shared daily token budget, and every plugin's own caps (image, voice, research) apply. Their
usage counts toward the bot's daily budget, so each grant costs the owner real money; the
confirmation and the admin docs say so. Batch size stays capped at 20.

## Implementation sequence

1. Store tests first (memory + Postgres), then migration 0013, `DmGrant`, snapshot field,
   `validate_change`/`authorize_change`, `apply` and batch support in
   `administration/store.py` and `pg.py`.
2. `AccessPolicy.has_dm_grant(user, now=None)` and the new clause in `can_dm`.
3. Commands, confirmation text, menu and listing in `administration/plugin.py`; a separate
   duration parser bounded 1m to 365d (pause keeps its 30d bound).
4. `manage_dm_access` tool, resolver change for explicit IDs, gates, skill declaration and
   instructions (under 800 characters), routing set and request snapshot updates.
5. Optional request notice in `app.py` `_allowed`, wired through `BotApp`, default off.
6. Docs: `docs/admin-setup.md`, `ARCHITECTURE.md` §10 subsection, `CHANGELOG.md` (Added, plus
   the migration and security notes), plans index row update.

Primary files: `packages/core/src/chatcore/{access,app}.py`,
`packages/core/src/chatcore/administration/{store,pg,plugin,inviter_tools,gates,permissions}.py`,
core migration 0013, core tests. No dependencies, adapter, plugin or applied migration changes.

**Permission change call-out:** this edits `administration/gates.py` and `permissions.py` and
widens who `can_dm` admits. The PR description must say so for maintenance review.

## Tests

- `can_dm`: grant allows; none, expired and revoked deny; global block/pause beat a grant;
  admins/members unchanged; no grants means identical results to today.
- Commands: owner-only, DM-only, same requester confirms, stale revision rejected, replay
  rejected, invalid/cross-platform/negative IDs rejected, owner and self targets rejected,
  duration bounds, re-grant replaces expiry, listing hides expired rows, non-owner menus omit it.
- Store: memory/Postgres parity, restart persistence, audit in the same transaction (injected
  audit failure rolls back), batch atomicity and one revision per batch, concurrent changes.
- Isolation: a grantee has empty `member_groups`, no `/admin` access, cannot invite the bot to a
  group, and trust alone still gives no DM access.
- Natural language: owner DM grants a mix of a known name and an explicit `tg:` ID in one
  confirmation; unknown names stage nothing; tool hidden and refused for non-owners, groups and
  global admins in off/on/shadow routing modes.
- Request notice: off sends nothing; on sends once per 24 h per user, respects the global cap,
  makes no model call and logs no text.
- Required checks from `AGENTS.md`, generated explainer check, real Postgres tests.

## Rollback

Revoke grants first if the feature should stop immediately. A code rollback ignores the table
(no one keeps DM access through it); keep the additive table. Live Telegram messages and real
friend grants are not part of automated checks; the owner grants friends after deployment.

## Review record

Codex (`gpt-6-luna`) executed; Codex (`gpt-6.1-sol`) reviewed twice; Claude did the final
review. Review 1 found memory-store audit atomicity, a preview identity-spoofing path through
display names, missing commit-time expiry re-validation and thin tests; fix round 1 resolved
all of them. Review 2 confirmed those fixes and found hollow target-rejection tests (fixed in
round 2, tests only). Only `administration/gates.py` changed among the permission files;
`permissions.py` is reused unchanged.

Known pre-existing limit, not changed here: for any globally blocked or paused DM user, the app
logs the message and runs plugin `on_message` hooks before sending the restriction notice. The
responder (model) is never called. Grantees inherit this ordering; tightening it for all
restricted users is a separate follow-up.
