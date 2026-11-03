# Protected Release-Control Watchdog Implementation Plan

> **For agentic workers:** Execute inline in this worktree; do not delegate or commit task files.

**Goal:** Add a root-installed, read-only watchdog that measures explicitly configured host limits, detects fixed-service/host/journal failures, and sends only deduplicated fixed-enum alerts through the protected bridge.

**Architecture:** A strict root-owned configuration binds fixed paths and four fixed systemd units plus operator-measured thresholds. Read-only probes collect bounded systemd, `/proc`, disk, protected-host UDS and journal state. A separate fsync+lock state store gives each incident/transition a stable UUID before the bridge request. The bridge validates exact enums and uses its existing durable `BridgeSendStore` before calling its current-owner send callback. `--once` observes without notifying; the root-only installed service runs the active loop. No repair actions exist.

**Tech Stack:** Python 3.12, asyncpg bridge send ledger, httpx UDS clients, `fcntl`, systemd unit/timer templates.

---

### Task 1: Narrow journal and protected-alert contracts

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/host_journal.py`
- Modify: `packages/plugin-release-control/src/chatcore_release_control/protected_bridge_http.py`
- Test: `packages/plugin-release-control/tests/test_host_journal.py`
- Test: `packages/plugin-release-control/tests/test_protected_bridge_http.py`

- [x] Add a locked read-only current-record accessor.
- [x] Add `/v1/broker/alerts` with exact `{request_id,kind,service,state}` schema, fixed label maps, current owner callback and existing send intent/dedup path.

### Task 2: Watchdog types and probes

**Files:**
- Create: `packages/plugin-release-control/src/chatcore_release_control/watchdog.py`
- Create: `packages/plugin-release-control/tests/test_watchdog.py`

- [x] Write tests for strict config, exact units/paths, explicit thresholds, CPU/load/memory/disk readings, bounded read-only systemctl, host state, journal deadlines, missing-source incidents and fixed alert enums.
- [x] Implement the bounded probes and fail-closed config without repair/restart/deploy operations.

### Task 3: Durable incidents and runtime entrypoint

**Files:**
- Modify: `packages/plugin-release-control/src/chatcore_release_control/watchdog.py`
- Modify: `packages/plugin-release-control/tests/test_watchdog.py`
- Create: `services/release-control/src/chatcore_release_control/templates/p10-watchdog.service.template`
- Create: `services/release-control/src/chatcore_release_control/templates/p10-watchdog.timer.template`

- [x] Test locked atomic fsync state storage, stable UUID reuse after lost replies/restart, quiet unchanged findings, one resolved transition, and no blind duplicate send.
- [x] Implement `--check-config`, root-only dry `--once`, and root-only private-bridge `serve` modes plus inactive systemd templates. Do not add an HTTP listener or remediation action.

### Task 4: Verify

- [x] Run watchdog, host-journal, protected-bridge tests with fake processes/transports and temporary files only.
- [x] Run Ruff and `git diff --check`; report that no systemd, production metrics, socket, service or message was contacted.

**Verification status:** 46 focused tests passed; Ruff checks and format checks passed. No live host, systemd, production metrics, protected socket, or notification was contacted. Linux service confinement and operational threshold measurement remain owner deployment proofs.
