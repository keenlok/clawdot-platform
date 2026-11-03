# DM Admin Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. The user explicitly requested Luna as executor. No further approval is needed to execute the agreed design. Steps use checkbox syntax for tracking.

**Goal:** Let the owner configure group access and delegate group-scoped setup permissions through confirmed DM commands, without granting destructive or cross-group data access.

**Architecture:** Introduce a memory/Postgres administration repository and a DM-only administration plugin. Keep `AccessPolicy`'s existing synchronous checks by refreshing an immutable permission snapshot before each event. Use transactional revision checks for confirmed changes and preserve existing static administrator behavior separately from delegated setup rights.

**Tech Stack:** Python 3.12, asyncpg, existing `Database` transaction wrapper, dataclasses, asyncio, existing `PendingAction`/`ConfirmService`, pytest-asyncio, Ruff. No dependency changes expected.

**Approved design:** `docs/superpowers/specs/2026-10-31-dm-admin-permissions-design.md`.

**Workspace:** `/path/to/worktrees/dm-admin-permissions/sb_bot`; branch `codex/dm-admin-permissions`; base `8dea66d`. The original checkout is actively edited by another chat. Never touch its changes. Use `uv sync --frozen --all-packages` for this workspace; plain root `uv sync` installs only development dependencies.

## File map

- Create `packages/core/src/chatcore/administration/store.py`: value types, protocol, validation, memory repository.
- Create `packages/core/src/chatcore/administration/pg.py`: transactional Postgres repository with identical semantics.
- Create `packages/core/src/chatcore/administration/plugin.py`: `/admin` parsing, role-filtered read views, pending actions and commit authorization.
- Create `packages/core/src/chatcore/administration/__init__.py`: small public exports.
- Create `packages/core/src/chatcore/migrations/0003_administration.sql`: additive tables and revision seed.
- Modify `packages/core/src/chatcore/access.py`: bootstrap/overrides, owners, setup assignments, async refresh, DM admission.
- Modify `packages/core/src/chatcore/config.py`: `owner_users`, comma splitting, explicit/fallback owner selection at wiring.
- Modify `packages/core/src/chatcore/app.py`: refresh at event boundary; ensure callback routing cannot skip fresh permissions.
- Modify `apps/sb_bot/src/sb_bot/app.py`: construct repository, apply migrations, hydrate policy, register plugin.
- Modify `apps/sb_bot/.env.example`: document `OWNER_USERS` bootstrap and runtime group management.
- Create `packages/core/tests/test_administration.py`, `packages/core/tests/test_administration_pg.py`, `packages/core/tests/test_administration_app.py`.
- Modify `packages/core/tests/test_pg.py`: update exact expected migration list for additive migration.
- Modify `apps/sb_bot/tests/test_app.py`: wiring and config validation cases.
- Modify `ARCHITECTURE.md`; create `docs/admin-setup.md`: shipped behavior and exact usage.

### Task 1: Memory access store and validated operations

- [x] Read `access.py`, `plugin.py`, `pending.py`, `confirm.py`, `store/base.py`, `store/pg.py`, and the approved design before coding. Keep global access data separate from `CoreRepo.purge_chat`, so purging a conversation cannot re-enable a group or remove administrative history.
- [x] Add frozen `AccessSnapshot` with `revision: int`, `overrides: Mapping[str, bool]`, and `assignments: frozenset[tuple[str, str]]`; use copied/immutable structures. Add frozen `AdminChange` with operation, group, optional user, optional value; frozen structured audit entries with revision/actor/operation/group/subject/time. Define `AdministrationStore.snapshot()`, scoped audit reads and `apply(...)` with a verified actor, configured owner set and `expected_revision`. Implement authority checks inside the atomic mutation path, not only the plugin.

Use these role semantics in one shared helper called by both backends:

```python
def authorize_change(snapshot, actor, owners, change):
    if actor.key in owners:
        return
    if change.operation in {"grant", "revoke"}:
        raise PermissionError("Owner access required")
    if (actor.key, change.group.key) not in snapshot.assignments:
        raise PermissionError("Setup access required for this group")
```

- [x] Write a failing test demonstrating delegate scope before implementation, and run it to confirm the failure is the missing store:

```python
async def test_delegate_cannot_enable_unassigned_group():
    owner, delegate = UserRef("tg", "1"), UserRef("tg", "2")
    group = ChatRef("tg", "-1001")
    other = ChatRef("tg", "-1002")
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("grant", group, user=delegate),
    )
    before = await store.snapshot()
    with pytest.raises(PermissionError):
        await store.apply(
            actor=delegate,
            owners=frozenset({owner.key}),
            expected_revision=before.revision,
            change=AdminChange("enable", other),
        )
    assert await store.snapshot() == before
```

- [x] Implement `MemoryAdministrationStore` with an asyncio lock. Validate operation values, target platform, user IDs and group IDs. Implement enable/disable as persistent overrides and grant/revoke as scoped set changes; owner targets cannot be granted/revoked to imply changing owner identity. Reject invalid operations rather than silently ignoring extra fields. Return a specific stale-change error for revision mismatch. Store no secrets or chat content in audit; persona changes record the operation and target, not persona text.
- [x] Add tests for successful owner grant then delegated enable, global grant denial, cross-platform/wrong-sign IDs, revocation and revoke/regrant stale confirmations, stable snapshots, expected-revision races (exactly one write succeeds), and audit reflecting only successful writes. For settings changes, keep configuration data separate from permissions: store the chatmode/persona write atomically alongside the revision/audit, using the existing settings representation; define the repository dependency explicitly rather than nested pool transactions.
- [x] Run `uv run pytest packages/core/tests/test_administration.py -q`; commit the coherent types/store/tests change with `feat: add scoped administration store`.

### Task 2: Persistent store and additive migration

- [x] Add `0003_administration.sql` containing these tables (names may be prefixed consistently to avoid collisions):

```sql
CREATE TABLE administration_revision (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision bigint NOT NULL DEFAULT 0
);
INSERT INTO administration_revision(singleton, revision) VALUES (true, 0);
CREATE TABLE administration_groups (
    chat_key text PRIMARY KEY,
    enabled boolean NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE administration_assignments (
    user_key text NOT NULL,
    chat_key text NOT NULL,
    granted_by text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_key, chat_key)
);
CREATE TABLE administration_audit (
    id bigserial PRIMARY KEY,
    revision bigint NOT NULL,
    actor_key text NOT NULL,
    operation text NOT NULL,
    chat_key text NOT NULL,
    subject_key text,
    created_at timestamptz NOT NULL DEFAULT now()
);
```

- [x] Add isolated-schema PG tests using the existing fixture pattern in `test_pg.py` (random safe schema name, `server_settings={"search_path": schema}`, finally drop only that schema). Parametrize shared behavioral tests where useful. Verify a second repository instance sees the first instance's assignment and disabled override. Verify failed authorization and stale writes do not increment revision, change settings or append audit.
- [x] Implement `PgAdministrationStore` with parameterized SQL. Under `db.transaction()` acquire `SELECT revision FROM administration_revision WHERE singleton=true FOR UPDATE`, load current state, validate authority and expected revision, perform exactly one requested change, increment revision, append audit. Use the yielded transaction database for all operations, including settings upsert. Read snapshot consistently under a transaction/appropriate lock to avoid mixing revision and grants from different commits.
- [x] For chatmode/persona use `INSERT INTO chat_settings (...) VALUES (...) ON CONFLICT ...` preserving the other field. Ensure the memory backend updates its supplied `CoreRepo` with the same semantics while holding its lock; make no global storage refactor. Persona length max 500, reset sets NULL; chatmode accepts exactly on/off.
- [x] Update the exact expected migration list in `test_pg.py`; never edit historical SQL. Check migration replay returns no applied migrations on the second call.
- [x] Run `uv run pytest packages/core/tests/test_administration_pg.py packages/core/tests/test_pg.py -q -rs`; expected no service skips if local Postgres reachable. Commit `feat: persist administration permissions and audit`.

### Task 3: Refreshable access policy and owner bootstrap

- [x] Add `owner_users: CommaList` to `CoreSettings` and extend its comma-list validator. App wiring uses explicit nonempty `OWNER_USERS` if supplied; otherwise legacy `ADMIN_USERS` is the compatibility owner set. An explicit empty/whitespace OWNER_USERS value must not accidentally elevate legacy admins: reject it when explicitly present. In Telegram mode, validate owner IDs as positive decimal `tg:` user IDs. Keep console/test named identities supported.
- [x] Extend `AccessPolicy` compatibly: existing positional `allowed_chats, admin_users`; optional keyword owners/store. Existing callers without dynamic store preserve old behavior. Add `is_owner`, `can_setup(group)`, scoped assignment inspection and `refresh()`. Owners count for `is_admin`; delegates never do. `is_chat_allowed` combines bootstrap chats plus persisted overrides, with explicit false taking precedence. Preserve a separately computed effective enabled set so `groups_on` uses current state.
- [x] Exclude positive `tg:` DM entries from group enumeration. They can remain bootstrap allowed private entries, but cannot be assigned or configured through `/admin`. DM setup access comes from the owner role or current scoped assignments; ordinary users still need membership in an enabled group. A scope grant never adds someone to membership caches or authorizes content tools.
- [x] Add `await self.access.refresh()` inside `BotApp.handle`'s existing protected try before matching any event. If refresh fails, the event must not log, index, respond, leave a group, or commit. Never silently fall back to permissive stale state. Keep policy static/sync consumers compatible, including prompt assembly and tool contexts.
- [x] Write event-level regressions: disable an env-seeded group, then recreate policy from the store and ensure still denied; revoke last setup assignment and verify setup DM access disappears; membership of disabled groups cannot still confer DM access via cached membership; database refresh failure yields no processing; legacy non-owner admin cannot delegate when explicit owners exist; delegate cannot invoke existing `/purgechat` or any `admin_only` tool.
- [x] Run `uv run pytest packages/core/tests/test_app.py packages/core/tests/test_administration_app.py -q`; commit `feat: refresh owner and setup permissions for every event`.

### Task 4: Confirmed DM administration commands

- [x] Implement `AdministrationPlugin(access, store, owners)` with one `/admin` command; no LLM tool. `/admin` and `help` show the role-appropriate menu. Read verbs: groups, admins, audit (latest 20). Owners see all; delegates see assigned groups/their own assignment and only authorized audit scopes. Unauthorized users cannot list permissions or group IDs. Direct invocation in groups never mutates state and gives only an authorized requester a DM instruction.
- [x] Parse the exact command grammar in the design with whitespace splitting (`split(maxsplit=2)` where retaining persona text). Group target must match current platform and use negative decimal Telegram group IDs; user target positive decimal Telegram user IDs. Reject unknown verb, extra args, wrong sign, cross-platform IDs, username targets and invalid on/off. Show usage for malformed authorized requests. Escape nothing into SQL; all SQL parameters remain bound.
- [x] For every mutation build `PendingAction` with `plugin="administration"`, `kind="change"`, requester DM chat and user; payload includes validated operation/targets/value and expected revision. Display the exact group/user and effect; persona confirmation shows the entire text (bounded 500). Use `ctx.confirm.request(action, ctx.user)`; return no premature success text.
- [x] The action handler rechecks DM convention (`ctx.is_dm`, chat.id equals user.id and same platform), action.chat/requester equal current context, payload exact keys/types/operation, then delegates to transactional `store.apply`. Refresh policy after success. On stale revision or revoked permissions return a clear retry/denial without writing. Catch expected validation/permission errors safely. Unexpected database errors follow existing generic confirmation failure behavior. Single use/expiry/requester checks remain in ConfirmService.
- [x] Write the actual app/confirmation workflow test:

```python
async def test_owner_grant_and_scoped_delegate_enable_require_dm_confirmation():
    app, platform, store = make_admin_app()
    await app.handle(message("/admin grant fake:helper fake:trip", dm=True, sender=user("owner")))
    assert not (await store.snapshot()).assignments
    grant = platform.sent[-1]
    await app.handle(press(grant.action_id, sender=user("owner"), chat=grant.chat))
    assert ("fake:helper", "fake:trip") in (await store.snapshot()).assignments
    await app.handle(message("/admin enable fake:trip", dm=True, sender=user("helper")))
    enable = platform.sent[-1]
    await app.handle(press(enable.action_id, sender=user("helper"), chat=enable.chat))
    assert (await store.snapshot()).overrides["fake:trip"] is True
    assert not app.access.is_admin(user("helper").ref)
```

- [x] Add revoked-after-prompt, revoke/regrant, another caller's button, callback sent from group, confirmation replay, expired action and reply-code fallback regressions. Verify a payload fabricated with extra/mismatched targets cannot perform an unauthorized change; test the action handler directly as well as BotApp routing.
- [x] Test scoped menu/audit filtering with two delegates/two groups; no group/history/persona of an unassigned target may be disclosed. Test chatmode/persona writes keep the other setting unchanged and use `updated_by` of the real actor.
- [x] Run `uv run pytest packages/core/tests/test_administration*.py packages/core/tests/test_app.py -q -rs`; commit `feat: manage group setup through confirmed DM commands`.

### Task 5: Application composition and documentation

- [x] In `build_app`, construct store using `PgAdministrationStore(db)` or `MemoryAdministrationStore(repo=stores.repo)`, configure owners and create `AccessPolicy`. Register AdministrationPlugin before app construction. Apply the core migration before the first store snapshot, then await policy.refresh() before returning Built. Existing plugin wiring, shared privacy hooks and independent tool scopes stay intact.
- [x] Telegram startup must support bootstrapping with owners and zero enabled groups (no required nonempty ALLOWED_CHATS once valid OWNER_USERS or compatible admin fallback is present). Still reject startup with neither a usable owner/bootstrap route nor valid access configuration. Preserve legacy test expectations when no management owner is configured; update tests intentionally to new valid bootstrap semantics rather than removing validations broadly.
- [x] Add app tests for owner-only empty-group startup, explicit owner selection, default legacy fallback and absence of any unintended assignment. Add a rebuild/persistence integration case using a random PG schema where possible. Keep integration artifacts isolated and cleaned. Existing `sb-bot-admin migrate` automatically includes the new core migration, so avoid adding hardcoded plugin lists.
- [x] Add `OWNER_USERS=` docs explaining fallback and immutable recovery ownership. Explain that `ADMIN_USERS` retains old full-admin permissions and must not be used for delegated setup access. Document every supported command, stable numeric IDs, enable-before-invite order, how to find numeric group IDs via a trusted existing client/tool, no bot self-approval, disable/leave/reinvite behavior, confirmation/retry behavior, and the fact topic restrictions are outside this feature. Give the owner's explicit assigned-group flow; never include real user IDs or secrets.
- [x] Update ARCHITECTURE.md with the implementation status and link to `docs/admin-setup.md`. Do not claim deployed or enabled; no production settings change belongs in this task.
- [x] Run focused app tests then `uv run ruff check .` and `git diff --check`; commit `docs: explain owner and delegated group setup` with coherent wiring tests if not already committed separately.

### Task 6: Review and full verification

- [x] Run unit validation: `uv run pytest -m 'not integration' -q -rs`. Base has 504 passing unit tests and one ShellCheck skip. New tests should add coverage without broad assertion weakening.
- [x] Run service validation serially: `uv run pytest -m integration -q -rs`. Base has 13 service integration tests; new PG tests add to this. Do not run a Docker build concurrently with ES integration (local VM has roughly 1.9 GiB). Skipped service tests are unverified, never passing evidence. No production service mutation is authorized for this feature.
- [x] Run `uv run ruff check .`, `git diff --check`, inspect final diff and ensure no credentials or untracked test artifacts are included. Confirm all commits are on codex/dm-admin-permissions and original checkout unchanged.
- [x] Review the authorization and race paths with the primary agent: owner/legacy distinction, immutable roots, target validation, no setup-to-full-admin escalation, no content access from grants, fresh DM/callback checks, disabled bootstrap precedence, replay/revision safety and transaction atomicity. Fix substantive issues and rerun affected tests.
- [x] Update this plan's checkboxes and report feature commit SHA(s), exact tests and skips, files/commands users can use, and any material limitation. Stop before merge/deploy: main has another chat's in-progress changes, so integration must be reviewed against that final state first.


**Execution result (2026-10-31):** feature commits `a24c282`, `6e922c2`, `c02825c`,
`ccf2d1f`, and `26cb116` are on `codex/dm-admin-permissions`, followed by a clean merge
from `main` at `afa88c4` (`697683c`). Unit validation: 551 passed, one ShellCheck skip,
23 deselected. Integration validation: 20 passed and three Elasticsearch checks skipped because
dev Elasticsearch was unavailable; Postgres tests ran. Ruff and `git diff --check` passed. No
production settings, deployment, or merge back to `main` occurred.
