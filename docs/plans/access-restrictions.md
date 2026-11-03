# Per-user access restrictions, moderation and request limits

Read first:
- `ARCHITECTURE.md` §3c, §3d, §6a (security baseline, non-negotiable) and §10;
- `docs/admin-setup.md`;
- `packages/core/src/chatcore/administration/{store,pg,plugin}.py`,
  `packages/core/src/chatcore/{access,app,limits,routing,confirm,pending,privacy}.py`,
  `packages/core/src/chatcore/migrations/0003_administration.sql`;
- `apps/sb_bot/src/sb_bot/app.py`;
- the tests `packages/core/tests/test_administration*.py`, `packages/core/tests/test_app.py`
  and `apps/sb_bot/tests/test_admin.py` for style and fixtures.

Environment: `export UV_CACHE_DIR="$PWD/.uv-cache"` before running anything through `uv`.
Tests: `uv run pytest -q -m 'not integration'`. Lint: `uv run ruff format . && uv run ruff check .`.

## Goal

SBot's owner can block, unblock and temporarily pause a person's access to SBot, and can set a
per-person hourly request limit that overrides the global `RATE_USER_PER_HOUR`. The owner can
also grant **moderation** of specific groups to specific people. Moderators can block, pause and
unblock people **only inside the groups assigned to them**.

The scope is access to this bot. Nothing here removes or mutes anyone in Telegram.

Everything extends the existing scoped administration store, revision and audit (§10). Do
**not** build a parallel store. Every change goes through the existing `/admin` command, the
pending-action confirmation flow (requester-only, single use, 10 minutes) and the shared
revision check (stale requests are refused).

## You own

(Also `packages/adapter-console/src/chatcore_console/__init__.py` and
`packages/adapter-console/tests/test_console.py`, only for §4a.)

- `packages/core/src/chatcore/administration/__init__.py`, `store.py`, `pg.py`, `plugin.py`
- `packages/core/src/chatcore/access.py`
- `packages/core/src/chatcore/app.py`: only the additions described in §4
- `packages/core/src/chatcore/limits.py`: only the addition described in §5
- new `packages/core/src/chatcore/migrations/0004_access_restrictions.sql`
- `apps/sb_bot/src/sb_bot/app.py`: wiring only (§6)
- `docs/admin-setup.md` (document the new commands)
- tests: `packages/core/tests/test_administration.py`, `test_administration_pg.py`,
  `test_administration_app.py`, `test_app.py`, `test_pg.py` (migration list only), new
  `packages/core/tests/test_restrictions.py`, `apps/sb_bot/tests/test_admin.py`,
  `apps/sb_bot/tests/test_app.py`

Do **not** change any existing public signature in a breaking way. Every extension must be
additive: new optional keyword arguments and new dataclass fields with defaults. Don't edit
`events.py`, `platform.py`, `plugin.py`, `pending.py`, `confirm.py`, `routing.py`,
`store/base.py`, `conversation.py`, `agent.py` or any adapter. If one must change, stop and say
why in your report.

## 1. Data model (store.py, migration 0004)

Keys are namespaced (`tg:123`, `tg:-100123`). **Global scope** is the sentinel group
`ChatRef(platform, "*")`, key `"<platform>:*"`, e.g. `tg:*`. It is valid **only** for the
restriction and limit operations below. A `tg:*` block never affects `console:*` users, and the
reverse is also true.

New frozen dataclass in `store.py`:

```python
@dataclass(frozen=True, slots=True)
class Restriction:
    user: str  # user key
    scope: str  # group key, or "<platform>:*" for global
    kind: str  # "block" | "pause"
    until: datetime | None  # None for block; UTC expiry for pause
    set_by: str
    set_at: datetime
```

`AccessSnapshot` gains defaulted fields (keep the existing positional order working):
- `moderators: frozenset[tuple[str, str]] = frozenset()`: (user_key, group_key)
- `restrictions: Mapping[tuple[str, str], Restriction] = MappingProxyType({})`: keyed (user, scope)
- `user_limits: Mapping[str, int] = MappingProxyType({})`: user key → requests per hour

`AuditEntry` gains `detail: str | None = None`, a short machine-made string such as
`until=2026-11-01T10:00:00Z` or `limit=50`. **Never** put free text in it (no persona text, no
reasons).

New operations accepted by `validate_change` (plus the existing ones):

| Operation | group | user | value |
|---|---|---|---|
| `moderator_grant`, `moderator_revoke` | real group | required | None |
| `block`, `unblock` | real group or `<platform>:*` | required | None |
| `pause` | real group or `<platform>:*` | required | `int` seconds, 60 ≤ s ≤ 30 days |
| `limit` | `<platform>:*` only | required | `int`, 1 ≤ n ≤ 10000 |
| `limit_reset` | `<platform>:*` only | required | None |

- `AdminChange.value` becomes `str | bool | int | None`. Reject `bool` where an `int` is needed
  (`type(v) is int`).
- `unblock` removes any block **or** pause for that (user, scope).
- `pause` replaces any existing row for that (user, scope). `block` likewise.
- User and group must share a platform, as today. `"*"` is rejected for every other operation,
  including enable/disable/grant/revoke/persona.

Migration `0004_access_restrictions.sql` (additive, no data rewrites):
- `administration_moderators(user_key, chat_key, granted_by, granted_at default now(), PK(user_key, chat_key))`
- `administration_restrictions(user_key, scope_key, kind CHECK in ('block','pause'), until timestamptz NULL, set_by, set_at default now(), PK(user_key, scope_key), CHECK ((kind='block') = (until IS NULL)))`
- `administration_user_limits(user_key PK, per_hour int CHECK (per_hour BETWEEN 1 AND 10000), set_by, set_at default now())`
- `ALTER TABLE administration_audit ADD COLUMN detail text`

## 2. Authorization and target rules (store.py, shared by both stores)

Extend `authorize_change`. Keep it a pure function used by `MemoryAdministrationStore.apply` and
`PgAdministrationStore.apply`, evaluated **inside** the transaction against the locked revision's
state, as today.

- Owner only: `grant`, `revoke`, `moderator_grant`, `moderator_revoke`, `limit`, `limit_reset`,
  and any `block`/`pause`/`unblock` with global scope.
- `block`/`pause`/`unblock` on a real group: owner, or `(actor, group)` in **moderators**.
  A setup assignment (`assignments`) must **not** authorize these.
- Existing setup operations (`enable`, `disable`, `persona`, `persona_reset`, `chatmode`): unchanged,
  and moderation must **not** authorize them.

New target check `check_target(snapshot, actor, owners, protected, change)`, for every operation
with a user target, called after `authorize_change`:
- target is never an owner (any operation, including moderator_grant, limit and unblock);
- target is never the actor (an owner can't block or limit themselves; neither can a moderator);
- a non-owner actor can't target anyone in `protected` (the `ADMIN_USERS` full admins) or anyone
  who moderates that same group.

`AdministrationStore.apply` gets a new keyword `protected: frozenset[str] = frozenset()`. Both
stores implement it. Raise `PermissionError` for authorization failures and `ValueError` for
target or validation failures, matching the existing style.

`pause` stores `until = now + value seconds`, where `now` is `datetime.now(UTC)` from the app
clock, never Postgres `now()` (§7c rule). The audit `detail` is `until=<ISO UTC>` for pause and
`limit=<n>` for limit.

Revision and audit: every new operation bumps the shared revision and writes one audit row with
actor, operation, group or scope key, subject (target) and detail. This happens in the same
transaction as the change, exactly like the existing operations. `snapshot()` loads moderators,
restrictions and user limits in the same `FOR SHARE` transaction. `audit()` returns `detail`.
Global audit rows use the `<platform>:*` key, so a delegate's group-filtered audit never shows
them.

## 3. AccessPolicy (access.py)

New methods. All read the current in-memory snapshot, with **no** I/O; `BotApp.handle` already
refreshes it once per event, and store errors fail closed upstream.

- `can_moderate(user, group) -> bool`: owner, or `(user, group)` in `snapshot.moderators`.
  Full admins (`admin_users`) and setup assignees don't get it automatically.
- `moderated_groups(user) -> frozenset[str]`
- `restriction_for(user, chat, *, is_dm, now=None) -> Restriction | None`:
  - owners: always `None`;
  - ignore rows whose platform differs from the user's platform;
  - global (`<user.platform>:*`) rows apply everywhere, DMs included;
  - group rows apply only when `not is_dm` and `chat.key == scope`;
  - a pause whose `until <= now` is ignored (expired);
  - if several apply, return a block before a pause, otherwise the pause with the latest `until`.
- `user_limit(user) -> int | None`
- `can_dm`: also true for users with moderated groups (they need DM to use `/admin`). A user with
  an active **global** restriction is still let through `can_dm`. `BotApp` (§4) decides what they
  can do, so the privacy exemption works.

## 4. Enforcement in BotApp (app.py)

New optional constructor keywords:
`restriction_exempt_commands: frozenset[str] = frozenset()` and
`restriction_exempt_actions: frozenset[str] = frozenset()` (pending-action handler keys,
e.g. `"privacy.forgetme"`). Defaults keep other apps unchanged.

Constant `RESTRICTED_NOTICE_COOLDOWN_S = 600`.

In `_on_message`, after `_allowed` and **before** any responder, command handler or LLM call:
1. Compute `restriction = self.access.restriction_for(message.user.ref, message.chat, is_dm=message.is_dm)`.
2. The early `/admin` path: if the user is restricted, apply the denial in step 4 instead of running it.
3. Logging (`log_message`, `upsert_member`, `on_message` plugin hooks) stays exactly as today for
   restricted users. History/search ingestion is unchanged.
4. After `decide(...)`, when the route is not `IGNORE` and the user is restricted:
   - `REPLY_CODE`: look up the action with `stores.repo.find_pending_by_code(chat, user, code)`.
     Only if it is in a DM **and** its `handler_key` is in `restriction_exempt_actions` does it
     continue to `confirm.on_reply_code`. Otherwise the user gets the notice, and the action is
     not committed.
   - `COMMAND`: only in a DM and only for a command in `restriction_exempt_commands` does it
     continue. Otherwise notice.
   - Everything else (`RESPOND`, `CHATMODE`, other commands): notice, no responder call.
   - Notice: at most **one per (chat, user) per cooldown**, using a cache key such as
     `restricted-notice:{chat.key}:{user.key}`. Set it with `cache.set(..., ttl_s=600)` after
     checking `cache.get`. Inside the cooldown, stay silent. Text: block → "You can't use this bot
     here right now."; pause → "Your access to this bot is paused until <YYYY-MM-DD HH:MM UTC>."
     Don't reveal who restricted them, or the scope.

In `_on_press`: if the presser is restricted (`is_dm = press.chat.key == press.user.ref.key`),
load the action with `stores.repo.get_pending(press.chat, press.action_id)`. If it exists and
its requester is the presser, it continues only when the press is in a DM and the
`handler_key` is exempt. Otherwise send the notice (same cooldown) and return without calling
`confirm.on_press`. This is how **pending confirmations created before a block are refused**.

Owners are never restricted (`restriction_for` returns `None`), so an owner can always reach `/admin`.

## 4a. The global-scope sentinel must never act as a chat

`"*"` is only a restriction/limit scope. It must never be a real chat or user, and never reach
any per-chat data query.
- In `store.py`, add one helper, `global_scope(platform) -> ChatRef`, and one predicate,
  `is_global_scope(ref) -> bool`. These are the **only** places that build or recognise the
  sentinel. `validate_change` uses them, so `"*"` is accepted only for the
  block/pause/unblock/limit/limit_reset operations. Plugin parsing (`_group`) rejects a typed
  `tg:*` everywhere except the optional scope position of those commands. The "global" form is
  simply the absent `GROUP_ID`; do not accept a typed `tg:*` from users.
- Event layer: at the top of `BotApp.handle`, drop (no logging of content, no store access, no
  reply; just `log.warning` with the event class) any event whose chat id or user id is `"*"`
  or empty. Add this to `_on_press` paths too, since they come through `handle`.
- Adapter layer: the console adapter's `/group <id>` switch (owned by you for this change only:
  `packages/adapter-console/src/chatcore_console/__init__.py` and its tests) rejects `*`,
  empty strings, and IDs containing `:`. The Telegram adapter only produces numeric IDs; add a
  test asserting a mapped update can never yield `*`. Don't change Telegram code unless that
  test fails.
- Test: feeding a message, a button press and a `MemberJoined` for chat `tg:*` (and user `tg:*`)
  into `BotApp.handle` causes zero repo reads and writes (use a recording fake repo or a spy),
  zero sends, and no responder call.

## 5. Per-user limits (limits.py)

`Limits.__init__(cache, settings=None, *, user_limit: Callable[[UserRef], int | None] | None = None)`.
In `check`, the hourly threshold is `user_limit(user)` when that returns an int, otherwise
`settings.RATE_USER_PER_HOUR`. Nothing else in `check` changes: same keys, same messages, same
order. This signature is a stable seam that another feature will build on. Keep it exactly as
written here.

## 6. Administration plugin (plugin.py) and wiring (sb_bot app.py)

`AdministrationPlugin.__init__` gains `protected: frozenset[str] = frozenset()` (full admins) and
passes it to `store.apply`.

Commands (DM only, numeric IDs, same parsing style as today; `GROUP_ID` optional means global):

```text
/admin moderators                       (owner: all; moderator: their own assignments)
/admin restrictions                     (owner: all active; moderator: their groups only)
/admin mod USER_ID GROUP_ID             (owner)   -> moderator_grant
/admin unmod USER_ID GROUP_ID           (owner)   -> moderator_revoke
/admin block USER_ID [GROUP_ID]
/admin pause USER_ID DURATION [GROUP_ID]   DURATION: 30m, 2h, 7d; 1m to 30d
/admin unblock USER_ID [GROUP_ID]
/admin limit USER_ID N|default          (owner)   -> limit / limit_reset
```

- Who may run `/admin` at all: owners, setup assignees **or moderators**. Everyone else gets no
  reply, as today.
- The menu (`_menu`) shows each role only the lines it may use.
- Check permission at request time (fast feedback), and again at commit through
  `store.apply` (authoritative).
- Confirmation summary must state the target, scope and effect exactly, e.g.
  `Confirm: pause tg:42 in tg:-100123 for 2h.` or `Confirm: block tg:42 everywhere (global).`
  or `Confirm: set tg:42 hourly request limit to 50.`
- Payload stays a flat dict of plain values. In `_commit`, allow `value` of type `str`, `bool` or
  `int` (still reject other types), and keep every existing requester/DM/platform check.
- `/admin admins` (owner) also lists moderators. `/admin audit` for a delegate covers setup and
  moderated groups, and prints `detail` when present.
- `/admin restrictions` shows each active row: user, scope (`global` for `*`), kind, until.
  It also shows per-user limits (owner only). Expired pauses are hidden.

sb_bot `app.py` wiring:
- `AdministrationPlugin(..., protected=frozenset(core.admin_users) - owners)`
- `Limits(cache, settings, user_limit=access.user_limit)`: construct `access` before `limits`, or
  pass a lambda that closes over `access`.
- `BotApp(..., restriction_exempt_commands=frozenset({"privacy", "forgetme"}),
  restriction_exempt_actions=frozenset({"privacy.forgetme"}))`. Verify the real handler key
  from `PrivacyPlugin.action_handlers` and the `PendingAction.handler_key` format.

Logging: IDs, operation names and counts only. Never log reasons, message text or payload values.

## Tests

Unit (memory store and fakes), in `test_restrictions.py` and the existing files:

1. Validation: `*` scope only for block/pause/unblock/limit/limit_reset; pause bounds (59s
   rejected, 30d accepted, 30d+1 rejected); limit bounds (0, 10001 rejected; `True` rejected);
   mixed platforms rejected.
2. Authorization:
   - owner can do all;
   - moderator of G can block/pause/unblock in G but not in H, and not globally;
   - setup assignee of G (no moderation) can't block in G;
   - moderator of G can't enable/disable/persona G (no setup assignment);
   - non-owner can't mod/unmod/limit.
3. Targets:
   - nobody can target an owner (block, pause, limit, mod);
   - an owner can't target themselves;
   - a moderator can't target self, a full admin (`protected`), or another moderator of the same group.
4. Revision/audit: every new operation bumps the revision; a stale `expected_revision` raises
   `StaleChangeError`; audit has actor, operation, scope, subject and detail; a delegate's audit
   filter hides global rows.
5. `restriction_for`:
   - global applies in DM and group;
   - group-scoped applies only in that group and not in DMs;
   - an expired pause is ignored;
   - block takes precedence over pause;
   - owners are never restricted;
   - `tg:*` doesn't affect a `console:` user.
6. BotApp, using `FakePlatform`, `ScriptedProvider` or a counting fake responder, and `memory_stores()`:
   - a blocked user's mention gets one notice and the responder is **not called**; a second
     mention inside the cooldown gets nothing; the message is still logged;
   - a paused user is blocked until expiry, then served (inject `now` or set `until` in the past);
   - DM: a globally blocked user's `/privacy` and `/forgetme` work and their confirmation commits;
     other commands and normal DM messages get the notice;
   - group: a blocked user's `/forgetme@bot` gets the notice (no exemption in groups);
   - **pending confirmation created before the block** (e.g. a confirmed-write tool or
     `/admin` action by a moderator who is then globally blocked): pressing Confirm and the
     `YES 1234` reply code both refuse without committing;
   - an owner is never blocked even if a stale row names them.
7. Limits: an override of 1 makes the second request in the hour return the limit message; no
   override uses the global setting; `limit_reset` restores the default.
8. Plugin: each new `/admin` command creates exactly one pending action with the right summary;
   commit applies it; wrong requester, non-DM confirm, tampered payload (`value` as a list,
   unknown operation) and stale revision are refused. A moderator's `/admin` menu shows the
   moderation lines only.
9. Persistence across restart: memory store isn't persistent, so cover this in the Postgres
   integration test.

Integration (`test_administration_pg.py`, `@pytest.mark.integration`, skips without Postgres):
- migration 0004 applies (update the expected list in `test_pg.py`);
- mod grant/revoke, block, pause (until stored from the app clock), unblock, limit and limit_reset
  round-trip through `snapshot()`;
- a **new** `PgAdministrationStore` on the same DB sees the same state (restart persistence);
- audit rows include detail;
- target and authorization failures leave no row and don't bump the revision;
- the concurrent stale-revision case.

## Done when

- `uv run pytest -q -m 'not integration'` passes and `uv run ruff check .` is clean.
- Integration tests are written and skip cleanly without Postgres (the reviewer runs them).
- `docs/admin-setup.md` documents moderation, block/pause/unblock, limits, owner immunity, the DM
  privacy exemption and the notice cooldown.
- Not in scope: Telegram ban/mute, per-group request limits, stopping a blocked user's already
  scheduled jobs (leave that as an open issue in your report), and the context-reset feature
  (a separate run).
