# Trusted group invitations

Status: Implemented for the owner-approved v0.13.0 release; direct transport only for free onboarding.
Release target: v0.13.0. Implemented and self-reviewed by Codex in the main chat under the
owner's implement-project instruction. No delegated or independent review was performed.

## Goal and approved policy

Let an owner trust a friend once so that friend can invite the bot to new Telegram groups
without per-group approval. Keep invitation permission separate from setup, moderation,
full administrator roles and content membership. A pure open-by-default blocklist would also
accept strangers' groups; per-group setup delegation would still require group approvals.
The approved approach trusts inviters instead.

| Priority | State | Result |
| --- | --- | --- |
| 1 | Owner group block | Deny, including owner invitations and delegated enable |
| 2 | Explicit disabled override | Deny; invitation never undoes a deliberate disable |
| 3 | Existing effective enable | Allow through current join behavior |
| 4 | Unknown Telegram group; owner or trusted inviter without active restriction | Atomically enable, record provenance and audit, then run hooks |
| 5 | Unknown group; missing, invalid, cross-platform or untrusted inviter | Leave without content logging or model calls |
| 6 | Storage or authorization failure | Fail closed without hooks or content handling |

`/admin trust USER`, `/admin untrust USER`, `/admin blockgroup GROUP` and
`/admin unblockgroup GROUP` are confirmed owner-only DM commands. `/admin trusted` lists
explicit grants; `/admin groups` shows state and owner-visible provenance. Existing user
`/admin block` commands remain separate. Numeric namespaced identities are validated.
All mutations retain requester-only, expiring, single-use confirmation and revision checks.

Untrust affects future admissions only. Auto-admitted groups survive restarts and revocation.
Group blocking also writes a false enable override, so old application versions respect it.
Unblock removes the block but leaves that disabled override; explicitly enable and re-invite
to restore access. Existing scoped setup delegation remains valid for unblocked groups.
Trusted-only users gain no DM content access and no role or budget exemptions.

## Implementation sequence

1. Extend immutable administration snapshots with inviter grants, blocked groups and admission
   provenance. Add the next additive migration, 0011, after existing privacy migration 0010.
2. Extend memory and Postgres store validation/owner authorization. Serialize automatic
   admission with confirmed mutations under the existing revision lock. Check current group
   state, trust and user restrictions there; commit enable, provenance, audit and event claim
   together. Failed audit must roll back all state. Duplicate delivery writes no extra audit.
3. Extend deterministic admin command parsing, confirmations and role-filtered listings.
   Handlers and the store both enforce permissions; blocked groups cannot be enabled.
4. Add `BotAddedToChat.event_key` from bot identity plus Telegram update ID. Both adapters
   emit only actual group/supergroup join transitions and preserve authenticated inviter
   identity. Transactional event claims suppress replay hooks while preserving genuine rejoins.
5. Admit before hooks or group content handling, refresh access after commit and reuse the
   existing privacy notice. Existing membership/history/jobs/polls consumers continue using
   the effective allowed-group set. Denied groups remain unrecorded and unindexed.
6. Update administration documentation, architecture, changelog and plan index. Verify the
   final diff and release a reviewed immutable version before downstream applications pin it.

Primary files: `packages/core/src/chatcore/administration/{store,pg,plugin}.py`,
`packages/core/src/chatcore/{access,app,events}.py`, core migration 0011, both Telegram adapters,
core administration/app/privacy/migration tests and Telegram adapter tests. No dependencies,
plugin gates, applied migrations or broker policies change. Core, migration and adapter
package initializer changes require maintenance review.

## Acceptance and verification

- Trusted/owner invitations persist; strangers and missing identities do not admit a group.
- Explicit blocks and disables win over bootstrap enables and trust. Unblock does not enable.
- Trust grants no unrelated role or content permission; active restrictions deny new admission.
- Memory and Postgres agree; restart, duplicate, audit rollback and concurrent permission
  changes are covered. All snapshots retain backward-compatible defaults and immutability.
- New commands retain requester, stale-revision, replay and target-validation protections.
- Direct and bridge adapter event mapping, privacy hooks and shared access consumers work.
  The bridge transport restriction below is an explicit deployment limitation.

Required checks: `uv sync --locked --all-packages`,
`uv run ruff check packages services apps tests .github/scripts`,
`uv run ruff format --check .`, and
`uv run pytest -q -m "not browser and not sandbox_integration"`.
Also check generated explainers, plan links/boundaries and staged private identifiers.
Run actual Postgres regression tests; report environmental skips and failures separately.

## Deployment limits and rollback

The protected bridge also filters relay audiences and API calls through its own fixed
allowlist. Adapter parity alone does not let unknown groups pass that independent boundary.
This release does not expand broker authority; free onboarding requires direct transport.
The owner authorized releasing the implemented feature, and the downstream deployment is
verified to use direct transport. No additional permission is inferred for the broker.

Messages delivered before the join event remain denied; delivery failures may require a
fresh authorized invitation. Hooks are best effort if the process fails after admission.
Telegram basic-group to supergroup ID migration is outside this change.

Deploy with normal backup and health checks. Removing explicit trust stops friend-driven
expansion; code rollback also removes owner-driven automatic admission. Already admitted
groups remain enabled, so disable/block unwanted groups separately. Retain additive tables
on rollback. Live Telegram messages are not sent as part of automated checks.


## Local review evidence

The port was applied as a focused diff to current platform main, preserving later privacy,
image, cards, voice and explainer work. One fixture needed to pass only its explicit plugins
when rebuilding an app because the current app installs its own reserved explainer plugin.
The corrected focused suite passed 281 tests, including actual Postgres, adapter transitions,
plan links and package boundaries. Generated explainers and global lint passed; all changed
Python files format cleanly. The full repository format check retains 16 existing failures.
The full suite and hosted Linux CI results are reported on the PR; no skipped environment
checks are represented as passes. Self-review found no unresolved direct-mode source defect.
