# Protected Release Revocation Implementation Plan

> **For agentic workers:** Execute the listed tasks in order and keep focused tests green after each stage.

**Goal:** Let the configured Telegram owner revoke a pending release through a private protected webhook command, with append-only durable evidence and transactionally fenced merge/switch admissions.

**Architecture:** The broker database stores immutable revocation records and checks them while holding the same target queue, release card, and broker target-state locks used by merge and switch admission. The protected Telegram webhook recognizes the exact owner-only command before ordinary relay, verifies private direct-message and non-forwarded provenance, and calls a narrow revocation store method with Telegram update ID and authenticated sender. Existing admissions remain resumable; a revocation only denies an admission that has not yet been granted.

**Tech Stack:** Python 3.12, asyncpg, PostgreSQL, ASGI, Telegram webhook updates, pytest with isolated temporary PostgreSQL.

---

## Scope and interface

- Create `packages/plugin-release-control/src/chatcore_release_control/revocation.py` for the typed revocation request/receipt and bounded canonical digest.
- Create `packages/plugin-release-control/src/chatcore_release_control/revocation_schema.sql` for append-only table grants and `services/release-control/src/chatcore_release_control/templates/revocation-grants.sql.template` for installer role bindings. Add the table definition to the full `broker_schema.sql` so the normal maintenance schema install creates it; readiness must fail if it is absent.
- Modify `broker.py` to record revocation under the broker transaction locks and to reject revoked releases at new merge-lease and first switch admission. Keep exact already-admitted merge leases and switch operations usable for retry/reconciliation/terminal receipt.
- Modify `broker_worker.py` so a revocation before a fresh workflow dispatch prevents the dispatch; a revoked run without an admitted merge lease cannot be requeued. Keep any already-admitted merge operation untouched.
- Modify `telegram_ingress.py` to consume exact `/release_revoke <canonical UUID>` messages before generic HMAC relay, with webhook secret, numeric current-owner sender/chat, private chat, no forwarding metadata, and unique positive Telegram `update_id` required.
- Modify `bridge_service.py` only to construct the revocation adapter from the existing protected pool/current owner configuration. Add no ordinary RPC route for revocation.
- Test broker race boundaries in `test_broker.py`, ingress provenance and duplicate update behavior in `test_telegram_ingress.py`, protected wiring in bridge service tests, and schema/table/grant requirements in DB role tests.

## Acceptance criteria

- **R1:** Replaying the same Telegram update ID with identical approval and owner returns the same receipt; reusing that update ID with different request content is rejected. Each distinct authenticated update appends its own immutable receipt, including when the approval was already revoked; the first revocation remains effective and is never replaced.
- **R2:** Revocation transaction takes target queue, release card, and broker target-state locks, verifies the live Telegram owner and immutable card owner, and appends a record with canonical request digest and stable receipt.
- **R3:** Revocation before a new merge lease denies it. A merge lease already admitted before revocation can finish recording its merge observation.
- **R4:** Revocation before first switch admission denies it. Once that operation is admitted, the same operation can start, reconcile, restore, and submit its terminal receipt after revocation; another operation can never take over.
- **R5:** `/release_revoke UUID` is handled only through the secret-authenticated HTTPS webhook, from the current numeric owner in a private chat, without forward metadata. It is never sent through ordinary relay or ordinary service RPC.
- **R6:** Schema readiness fails without revocation storage. Grants permit only the protected bridge role to insert/read and broker role to read; no update/delete or public access.
- **R7:** A revoked queued release is canceled before a new Actions dispatch; an unadmitted canceled run cannot be requeued. The affirmative decision value and run-history rows are preserved.

## Ordered tasks

### Task 1: Add typed revocation record and SQL storage

- Define an immutable request/receipt representation and deterministic SHA-256 digest over `update_id`, approval UUID and owner ID.
- Add an append-only `broker_release_revocation` table keyed by Telegram `update_id`, with `approval_id`, target, immutable card owner, authenticated current owner, request digest, receipt UUID and timestamp. A repeated update ID is idempotent; distinct updates remain separate audit records and all make the approval revoked.
- Add database trigger rejecting `UPDATE` and `DELETE`. Grant bridge only `SELECT, INSERT`, broker only `SELECT`; revoke all from PUBLIC and unrelated roles.
- Include the table in `broker_schema.sql`; make `BrokerStore.schema_ready()` require it.
- Add tests for table constraints, append-only trigger, exact grants, readiness, canonical digest, and typed input rejection. Run those tests red before implementation.

### Task 2: Serialize revocation with broker admissions

- Add `BrokerStore.revoke_release(update_id, approval_id, owner_user_id, now)` that loads the card target, locks target queue then release card then broker target state in the established lock order, rechecks card owner and current target Telegram owner, and inserts immutable receipt.
- For duplicate update IDs, return the stored receipt only when digest and approval match. Reject mismatched replay. For a new update against an already revoked approval, append another immutable receipt; never replace or remove earlier receipts.
- Add a private helper that checks revocation while the target/card/state locks are held.
- Reject new `acquire_merge_lease` admission when revoked. Preserve a matching existing lease as admitted before checking revocation so it can report observed merge results.
- In `admit_switch`, allow an already `admitted`, `started`, or terminal matching operation to resume/return its current admission; reject only the first `prepared`→`admitted` transition when revoked.
- In `BrokerWorkerStore.begin_dispatch`, after checking for an exact existing dispatch intent, invalidate/cancel a revoked release before writing or sending a new dispatch intent. In `requeue_before_admission`, deny revoked requeue; clear queue/outbox only when no merge lease already admitted that work.
- Add isolated-PostgreSQL tests for revoke-before-lease, lease-before-revoke then merge observation, revoke-before-switch-admit, switch-admit-before-revoke then same operation start/receipt, different-operation denial, owner rotation, and update replay races.
- Add isolated-PostgreSQL worker tests for revoked-before-dispatch and revoked-before-requeue, asserting no network dispatch, no new run-history entry, preserved approval decision, and cleared queue state when there is no admitted merge lease.

### Task 3: Route owner command before ordinary relay

- Extend the protected ingress store protocol with `revoke_release(update_id, approval_id, owner_user_id)` and inject the broker-backed store from `bridge_service.py`.
- Recognize only one exact text command with canonical UUID in a Telegram `message`; reject forwarded origin/from/chat metadata, non-private chats, group/topic messages, nonnumeric or nonowner sender/chat, bot senders, missing/invalid update IDs, extra args, captions, and attachments.
- Authenticate the webhook secret before parsing or dispatch. Recheck the current configured owner and pass the numeric sender ID into the database owner/state check. Call the revocation store and acknowledge only after its transaction commits.
- Return the protected webhook's generic `{ok:true}` response and send no raw user-provided text, SQL errors, IDs beyond the fixed typed response, or credentials to ordinary relay. Keep ordinary RPC and HMAC relay unchanged.
- Add failing webhook tests proving accepted owner command is consumed before relay, ACK follows durable write, duplicates are idempotent, and malformed/forwarded/group/nonowner inputs do not mutate or relay.

### Task 4: Verify full source and race behavior

- Run focused ingress, broker, role/schema, bridge-service, and broker HTTP tests plus Ruff and `git diff --check`.
- Read the final diff against the acceptance criteria; report database schema installation/grant steps as operator work. Do not run production migrations, send Telegram messages, modify existing decisions, or alter already-admitted operation receipts.

## Operational boundary

Source and isolated temporary PostgreSQL tests do not grant real database permissions or validate a production webhook. Operators must install the maintenance SQL and reviewed role grants separately. No database migration is run automatically at process startup.
