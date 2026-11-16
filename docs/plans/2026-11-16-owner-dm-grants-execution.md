# Owner-granted DM access: execution brief

Codex execution brief for [the approved plan](2026-11-16-owner-dm-grants.md): `gpt-6-luna`
executes, `gpt-6.1-sol` reviews first, then Claude does the final review and commits.

Read first: `AGENTS.md`; `docs/plans/2026-11-16-owner-dm-grants.md` (the approved design: follow it
exactly, including the decision table and "What a DM-granted user may do"); the shipped
patterns you are copying: `docs/plans/2026-11-13-trusted-group-invites.md`,
`docs/plans/2026-11-13-natural-inviter-batches.md`, and the code
`packages/core/src/chatcore/access.py`, `packages/core/src/chatcore/app.py` (`BotApp.__init__`,
`_allowed`, `_on_message`), `packages/core/src/chatcore/administration/{store,pg,plugin,inviter_tools,gates,permissions}.py`,
`packages/core/src/chatcore/migrations/0011_trusted_group_invites.sql`, and the tests
`packages/core/tests/test_administration*.py`, `test_inviter_batches.py`,
`test_agent_request_snapshot.py`.

Owner decisions (approved 2026-11-16): grantees get the same DM features as a group member (a
grant simply makes `can_dm` true; do not change any plugin gate); the access-request notice is
included and **off by default**; the natural-language tool is included.

## You own

- `packages/core/src/chatcore/access.py`, `packages/core/src/chatcore/app.py`
- `packages/core/src/chatcore/administration/{store,pg,plugin,inviter_tools,gates,permissions}.py`
  (and `administration/__init__.py` only if a new public name must be exported)
- New `packages/core/src/chatcore/migrations/0013_owner_dm_grants.sql`
- Tests under `packages/core/tests/` (extend existing files or add `test_dm_grants.py`),
  plus `packages/core/tests/snapshots/*`, routing sets and skill-catalog/explainer expectations
  **only** where existing tests require an update for the new tool/skill text
- `docs/admin-setup.md`, `ARCHITECTURE.md` (new `### Owner-granted DM access (2026-11-16)`
  subsection at the end of §10, after "Natural-language invitation batches"; keep all section
  numbers), `CHANGELOG.md` (`Unreleased` only), `docs/plans/README.md` (status row of the plan)

You may **not** edit: any applied migration (`0001`–`0012`), `packages/plugins/**` (in particular
expenses), `packages/adapters/**`, `services/**`, `pyproject.toml`, `uv.lock`, `.github/**`.
Do not bump the version (release is a separate PR). Add no dependencies. If something here must
change, stop and say so in your report.

## 1. Storage

- Migration `0013_owner_dm_grants.sql`, additive, exactly the table in the plan
  (`administration_dm_grants(user_key text PRIMARY KEY, granted_by text NOT NULL, granted_at
  timestamptz NOT NULL DEFAULT now(), expires_at timestamptz)`), with a one-line comment that
  changes are serialized by `administration_revision`.
- `store.py`: frozen-slots `DmGrant(granted_by: str, granted_at: datetime, expires_at:
  datetime | None)`; `AccessSnapshot.dm_grants: Mapping[str, DmGrant]` defaulting to an empty
  `MappingProxyType` (keep the field last so positional constructors keep working).
- New operations `dm_grant` and `dm_revoke`: global scope (`ChatRef(platform, "*")`), require a
  user; `dm_grant` carries `value` = expiry as an ISO-8601 UTC string or `None`. Add them to
  `validate_change` (expiry must parse, be timezone-aware and in the future at commit) and to the
  owner-only set in `authorize_change`. `check_target` already rejects owners and self; keep it.
- `MemoryAdministrationStore.apply` and the Postgres `apply`: upsert/delete under the existing
  revision lock, one revision increment, audit entry in the same transaction (operation name as
  the audit operation, group = global scope key, subject = user key, detail = expiry ISO string
  or `"none"`). No-op changes (revoke of a missing grant; grant with identical expiry) must not
  increment the revision or write audit; follow how the existing trust/untrust no-ops behave and
  make dm grants consistent with them. Failed audit insert rolls back the change.
- Batch: generalise `apply_inviter_batch` (or add `apply_dm_grant_batch` beside it, your choice;
  prefer the smaller diff) so up to 20 `dm_grant`/`dm_revoke` changes commit atomically with one
  revision increment and one audit entry per changed person. Validate every target before any
  write; any invalid target, stale revision or audit failure changes nothing.
- Snapshots load `dm_grants` in both stores; memory and Postgres must agree.

## 2. Access policy

- `AccessPolicy.has_dm_grant(user: UserRef, *, now: datetime | None = None) -> bool`: true only
  if the snapshot has a grant for `user.key` whose `expires_at` is `None` or later than `now`
  (default `datetime.now(UTC)`). No snapshot means false.
- `can_dm` adds `or self.has_dm_grant(user)` as the **last** clause, so existing checks keep
  their order. Global block/pause already deny before tools/commands via `restriction_for`;
  keep that behaviour (a granted but globally blocked user gets the existing restriction notice
  and nothing else). Update the module docstring.

## 3. Commands (`administration/plugin.py`)

- `/admin dmgrant USER [DURATION]`, `/admin dmrevoke USER`, `/admin dmgrants`. Owner-only and
  owner-DM-only exactly like `trust`/`untrust`/`trusted`, through the existing PendingAction
  `change` flow (requester binding, same DM, single use, expiry, expected revision).
  `USER` parsed by `_user` (positive numeric `tg:` id on the current platform).
- Duration: a new parser accepting `Nm`/`Nh`/`Nd` from 1 minute to 365 days. Do not change
  `_parse_duration` (pause keeps its 30-day bound). The expiry instant is computed when the
  confirmation is created and frozen in the payload (as the ISO string), and re-validated at
  commit (must still be in the future).
- Confirmation summaries: grant → "Confirm: allow tg:123 to chat with me in a private DM until
  <expiry in the owner's display zone, or 'until revoked'>. This gives no group access or admin
  role, and their usage counts toward the daily budget." Revoke → "Confirm: remove tg:123's
  private DM access; data they created stays." Commit replies clearly ("DM access granted for
  tg:123 until …" / "DM access removed for tg:123").
- `/admin dmgrants` lists active grants (`user_key`, granted by, granted at, expiry or "until
  revoked"), sorted; expired grants are hidden; "None" when empty.
- Owner menu (`_menu`) and `_usage` gain one line for the three commands. Non-owners see nothing
  new. `_audit_display_detail` must render the new detail sensibly.

## 4. Natural-language tool

- In `inviter_tools.py` add a `manage_dm_access` tool next to `manage_trusted_inviters`:
  `operation` enum `["grant", "revoke"]`, `people` (1–20 strings, same limits), optional
  `duration` string (`Nm`/`Nh`/`Nd`, same 1m–365d bound, grant only). Description at most 300
  characters (there is a test bounding this; check it). Every argument has a description.
  `writes=True`, `admin_only=True`, `context_allowed=lambda ctx: is_owner_dm(plugin.access, ctx)`.
- Resolution: reuse `resolve_people`. Additionally accept an explicit platform identity string
  exactly matching `tg:<positive int>` (current platform only) even if it is not in the
  directory; label it `unknown name [tg:123]` in the preview. Names still never become guessed
  IDs; unknown/ambiguous/bot/cross-platform names stage nothing.
- Same freeze-then-confirm flow: one PendingAction with a distinct `kind` (for example
  `dm_batch`), dispatched in `AdministrationPlugin.commit` / `action_handlers` beside the inviter
  batch; commit re-checks owner DM, requester, revision and every target.
- `gates.py`: add the `manage_dm_access` gate with the same scope/owner gates (all `Gate(`/
  `ToolGate(` constructions stay in `gates.py`). Add the tool to the existing owner-only skill's
  tool tuple, extend its summary/examples/keywords for DM access, and update its instructions
  constant (keep it at most 800 characters, generic wording, mention that a grant gives only a
  private DM and no other role).
- Update routing set / catalog / request snapshot expectations only as the existing tests
  require; the legacy-off request snapshot for non-owner requests must stay unchanged.

## 5. Access-request notice (off by default)

- `BotApp.__init__` gains keyword `dm_request_notice: bool = False`.
- In `_allowed`, when `message.is_dm`, `can_dm` is false, the notice is enabled, the user is not a
  bot and the user's platform matches: send exactly one fixed text "I only chat with people the
  owner has approved. To ask for access, send the owner this ID: <user key>" — at most once per
  user per 24 h (cache key, e.g. `dm_notice:<user key>`, set before sending) and at most 30
  notices per rolling hour bot-wide (cache counter). No model call, no message logging, no plugin
  hooks, no owner notification. Send failures are logged by exception type only. Disabled → no
  behaviour change at all.

## Tests

- Access: grant allows; no grant, expired, revoked deny; a grant with future expiry flips to
  deny once `now` passes it; global block and active global pause still stop a granted user;
  admins/delegates/moderators/members unchanged; with no grants `can_dm` results equal today's.
- Commands (via the app/plugin test harness used in `test_administration_app.py`): owner DM
  grant and revoke round trip; non-owner, group chat, other requester, stale revision, replayed
  confirmation all refused; invalid, negative, cross-platform, owner and self targets refused;
  duration 0, 366d, malformed refused; re-grant replaces expiry; listing hides expired grants;
  non-owner `/admin` menus unchanged.
- Store: memory and Postgres parity for grant/revoke/expiry/listing; persistence across a new
  store instance; injected audit failure rolls back (copy the existing Postgres audit-failure
  pattern); batch atomic with one revision; concurrent changes serialize. Postgres tests run
  against `TEST_PG_DSN` (default `postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test`)
  and skip like the existing ones when unavailable.
- Isolation: grantee has empty `member_groups`, `/admin` returns nothing for them, a grantee who
  adds the bot to a group does not admit it (they are not a trusted inviter), and trust alone
  still gives no DM access.
- Natural language: owner DM batch mixing a resolved name and an explicit `tg:` id produces one
  preview listing both; unknown name stages nothing; tool hidden and refused for non-owners,
  group chats and global admins in off/on/shadow skill modes (copy the inviter-batch tests).
- Notice: off sends nothing; on sends once per 24 h per user; global cap enforced; no
  `log_message`, no model/agent call, no plugin `on_message`.

Environment: run `uv sync --locked --all-packages` first. If the uv cache is not writable in the
sandbox, use `UV_CACHE_DIR=.uv-cache` for every `uv` command.

## Done when

- `uv run ruff check packages services apps tests .github/scripts` is clean.
- `uv run ruff format --check` passes for every file you changed (repository-wide failures that
  already exist on `main` are not yours; do not reformat unrelated files).
- `uv run python .github/scripts/generate_explainers.py --check` passes.
- `uv run pytest -q -m "not browser and not sandbox_integration"` passes, and the report states
  pass/skip/fail counts and which Postgres tests ran or skipped.
- `CHANGELOG.md` `Unreleased` has an **Added** entry (commands, NL tool, notice setting off by
  default, no-default-change note), a migration note for 0013 and a security note (widens
  `can_dm` only for owner-granted users; gates/permissions changed).
- Your report lists every file changed, any deviation from this brief and why, and anything you
  could not run.
- Left for later, do not attempt: version bump/release, downstream deployment pins, bridge
  changes, live Telegram messages, real grants.
