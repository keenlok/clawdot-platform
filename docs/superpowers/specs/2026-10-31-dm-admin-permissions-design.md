# DM administration and delegated setup permissions

Approved in chat on 2026-10-31: the user accepted the owner plus group-scoped setup-admin model and requested a plan followed by Luna execution. This approval covers implementation and verification. Keep this feature on its isolated branch for integration with the independently changing prompt work; this task does not change production group access or grant real people permissions.

## Behavior

The owner configures group access and delegates setup access using `/admin` in their private conversation with the bot. A setup admin can enable or disable only assigned groups and edit their chatmode/persona. Only owners grant and revoke assignments. Telegram group-admin status grants no bot role. All mutations show the exact target and change, then use the existing requester-only, expiring, single-use confirmation. Authorization is checked again when committing.

Owner identity is configured as immutable `OWNER_USERS` namespaced user IDs. For backwards compatibility, absent `OWNER_USERS` falls back to existing `ADMIN_USERS`; setting an explicit owner list never promotes other legacy admins. Existing admin-only destructive commands retain their original authorization and are never unlocked by delegated setup permissions. No command changes owner identity. Ownership alone includes existing administrator capability so explicitly configured owners can operate the bot.

Commands (stable IDs rather than usernames):

```
/admin
/admin groups
/admin admins
/admin audit
/admin enable tg:-100123
/admin disable tg:-100123
/admin grant tg:123456 tg:-100123
/admin revoke tg:123456 tg:-100123
/admin chatmode tg:-100123 on
/admin chatmode tg:-100123 off
/admin persona tg:-100123 Friendly and concise
/admin persona tg:-100123 reset
```

`/admin` is the text menu/help, with role-appropriate actions. Setup admins see only their own assignments, assigned group configuration and scoped audit. Owners see everything. Unprivileged callers receive no administration data; administration invoked in groups cannot write and directs an authorized user to DM. No LLM tools expose administration. The owner can assign a group before the bot joins: DM enable first, then invite it. Unknown groups still cause the bot to leave without logging or indexing messages. Disabling a group stops serving it immediately on the next event; the bot leaves on its next group event. Re-enabling requires inviting it again if it has already left. Setup commands never send unsolicited group messages.

Telegram group IDs must be negative decimal IDs, user IDs positive decimal IDs, all in the current `tg` namespace. Reject DM IDs as group targets, cross-platform IDs, usernames and malformed IDs. Keep console/fake namespaced IDs usable in tests and local development with explicit parsing rules. Permissions do not confer group membership or access to stored history. A delegated admin can DM for setup even without group membership, but ordinary data tools retain their existing independent scope and membership checks.

## Persistence and concurrency

Add a separate access-management repository backed by Postgres or memory, rather than mixing global permissions into chat-content storage. Store explicit enabled/disabled overrides, `(user_key, group_key)` assignments and a structured audit trail. Existing `ALLOWED_CHATS` remains bootstrap configuration: a persisted disabled override wins, including after restart. Preserve the currently deployed positive private-chat bootstrap entry without treating it as an assignable group. Never persist provider keys or message bodies in audit entries.

Read persisted access state at startup and refresh before handling each event; no indefinitely cached permission decisions. Database failure fails closed. Snapshots update in one operation, never by mutating a shared set incrementally. Membership results can remain cached, but candidates must always come from the current enabled groups. Revoked delegates lose setup DM admission unless ordinary group membership independently admits them.

Use a singleton revision row and transaction/row lock (an asyncio lock for the memory backend). Every admin mutation captures the displayed revision, locks, checks revision and current authority, performs the write and records the audit in the same transaction, then increments revision. A revision mismatch asks the user to retry; this also invalidates older confirmations across revoke/regrant and concurrent changes. No permission/target data is trusted merely because it was stored in a pending action. The commit verifies private chat identity, requester identity, same-platform target, operation schema, role and group assignment afresh. The owner cannot be removed or demoted using these commands.

## Validation and delivery

Use TDD for memory/PG repository behavior and app event/confirmation routing. Verify attempts by strangers, ordinary members, legacy non-owner admins and scoped delegates, including permissions revoked after showing a confirmation, replay, wrong requester/chat, direct group commands, malformed targets, restart, config override precedence, database failure and concurrent writes. Verify delegates cannot invoke purge, grant others access, read another group's admin state or use assignment as history membership. Apply and test the additive migration in a disposable local schema. Run the complete unit suite, service integration suite, Ruff and whitespace checks. Record any skipped external-service checks explicitly.

The source checkout currently has another chat's uncommitted prompt edits. Use the managed worktree `/path/to/worktrees/dm-admin-permissions/sb_bot`, branch `codex/dm-admin-permissions`, starting at `8dea66d`. Do not change, stash, commit or revert the original checkout. Deliver committed code, tests and usage documentation on this feature branch for review/integration; do not deploy stale prompt code from this branch.
