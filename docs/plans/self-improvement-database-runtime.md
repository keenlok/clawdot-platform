# Release-Control Database Runtime Guard Implementation Plan

> **For agentic workers:** This plan is executed inline in the current worktree; no delegation or task-repository commits.

**Goal:** Refuse to start any bridge, broker, or worker runtime unless its PostgreSQL session has durable commit settings and the intended restricted LOGIN role.

**Architecture:** A new shared helper validates `fsync`, `full_page_writes`, and `synchronous_commit` on each asyncpg connection and verifies the authenticated LOGIN's attributes, expected NOLOGIN group membership, and absence of ownership or membership in control-object owner roles. Each service builder passes a validator as the asyncpg pool `init` callback and checks one acquired connection before declaring startup ready. The temporary PostgreSQL fixture runs with durability enabled and tests real separate LOGINs.

**Tech Stack:** Python 3.12, asyncpg, pytest-asyncio, temporary local PostgreSQL.

---

### Task 1: Runtime guard tests

**Files:**
- Create: `packages/plugin-release-control/tests/test_database_runtime.py`
- Modify: `packages/plugin-release-control/tests/test_database_roles.py`
- Modify: `packages/plugin-release-control/tests/test_ledger.py`

- [ ] Test accepted durable settings and restricted bridge/broker LOGINs, reject each relaxed setting and role/ownership violation, and exercise helpers against temporary PostgreSQL.
- [ ] Remove `-F` from the local PostgreSQL startup options and assert the effective durability settings.

### Task 2: Shared database guard

**Files:**
- Create: `packages/plugin-release-control/src/chatcore_release_control/database_runtime.py`

- [ ] Implement a safe `DatabaseRuntimeError` and async connection validator using server-effective settings and PostgreSQL catalog checks. Keep the expected role limited to `release_control_bridge` or `release_control_broker`; require LOGIN, inherit, no superuser/create-role/create-db/bypass-RLS, expected NOLOGIN group membership, and no direct/transitive membership in control schema/table/sequence/function owner roles.
- [ ] Keep validation read-only and do not install grants or migrations.

### Task 3: Enforce the guard at service startup and connection creation

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/bridge_service.py`
- Modify: `packages/plugin-release-control/src/chatcore_release_control/broker_http.py`
- Modify: `packages/plugin-release-control/src/chatcore_release_control/worker_service.py`

- [ ] Pass the correct bridge/broker group validator through each asyncpg pool's `init` callback.
- [ ] Validate a checked-out connection before schema readiness or returning/serving the service. On failure, close constructed resources and expose only the existing fixed safe runtime error.

### Task 4: Verify

- [ ] Run the database runtime, role, ledger, bridge, broker, and worker focused tests with temporary PostgreSQL.
- [ ] Run Ruff check/format and `git diff --check`; inspect only owned diffs and report that fsync settings do not establish power-loss guarantees.

## Implementation and verification record

- Added the shared PostgreSQL runtime guard and installed it as asyncpg's
  per-connection initializer plus an explicit startup pool check in bridge,
  broker, and worker builders. It checks effective commit durability, service
  LOGIN identity and attributes, exact expected NOLOGIN group membership,
  excludes the sibling/maintenance groups, and rejects control-schema/object
  ownership reachable directly or through role membership.
- The role integration test creates real temporary LOGINs for bridge and
  broker and exercises ledger operations using those credentials. It also
  verifies an ordinary login and an owner-inheriting login fail the guard.
  Temporary PostgreSQL starts with `fsync=on`, `full_page_writes=on`, and
  `synchronous_commit=on`; a test asserts the live settings.
- Verification: the focused database, ledger, bridge, broker, and worker suite
  passed 75 tests. Ruff and formatting checks are run before handoff. These
  settings do not establish power-loss guarantees for a particular storage
  stack; no such claim is made.
