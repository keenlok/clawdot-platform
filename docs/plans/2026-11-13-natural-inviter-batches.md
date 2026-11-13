# Natural-language invitation permissions

Status: Implemented for v0.15.0, pending release checks; the owner requested natural-language activation and a batch
flow for five people as an amendment to trusted group invitations. Execute inline under the
existing implement-project instruction; no delegation or model change. This request replaces
the earlier command-only interface requirement for this narrow capability.

## Goal and acceptance criteria

AC1: In the owner's DM, “Let Alex, Ben, Chris, Dana and Eli invite you to groups” resolves
known people and produces one exact five-person confirmation. No permission changes before
that confirmation. Natural-language removal supports the same batch flow.

AC2: Only the configured owner can request/confirm these changes, in their own same-platform
DM. Global admins, delegates, moderators and group messages cannot use the tool or read the
candidate directory. Recheck authority at commit; current restrictions and group blocks retain
their admission behavior. Trust still grants no other role or content access.

AC3: Resolve exact known names, @usernames or known numeric references from member records
of currently allowed groups the requester belongs to. Deduplicate stable user identities.
Unknown, ambiguous, bot and cross-platform targets produce clarification without a pending
write. Never guess IDs or query unrelated groups. The frozen preview identifies each selected
person by display name, handle where available, and stable numeric identity.

AC4: Apply up to 20 targets atomically under one revision lock/transaction, with one revision
increment and per-person audit entries. Validate every target before changing any. A stale
revision, revoked authority, invalid target or audit failure changes none. Duplicate/already
matching grants do not create redundant mutations. Existing individual commands remain intact.

AC5: One narrow confirmed model tool and its owner/DM-gated core skill support normal and
skills-on routing. Model arguments cannot directly write permissions or request other admin
operations. Existing confirmation expiry, requester binding and single-use checks apply.

AC6: Tests cover memory/Postgres parity, batches, rollback, names, gated visibility and real
agent/application requests. Keep third-party dependencies and applied SQL unchanged. Document
actual CI, self-review and downstream release/pin/deployment evidence when delivered.

## Chosen approach

Use the existing LLM tool/PendingAction machinery, with an owner-only invitation-management
skill. A deterministic English-only text parser would miss paraphrases and other languages;
a bulk slash command alone would still require command syntax. Retain slash commands as a
fallback and expose only trusted-inviter management to natural language, not general admin.

## Implementation sequence and files

1. Add batch store tests first, run them red, then add `apply_inviter_batch` to
   `packages/core/src/chatcore/administration/store.py` (protocol/memory) and `pg.py`.
   Reuse AdminChange validation/authorization and existing trust/audit tables. No migration.
2. Create `administration/permissions.py` for the owner-DM predicate, `gates.py` for tool gates,
   and `inviter_tools.py` for name resolution, staging and commit of batch actions. Modify
   `administration/plugin.py` to declare the tool/skill/instructions and dispatch its pending
   action kind. Keep the plugin's existing command behavior unchanged.
3. Write behavioral tests in `packages/core/tests/test_inviter_batches.py`, extend Postgres
   administration tests, and test owner/non-owner tool visibility and natural-language agent
   routing. Include ambiguous names, no shared group, repeated names and one invalid target.
4. Update `docs/admin-setup.md`, `ARCHITECTURE.md`, `CHANGELOG.md`, the plan index and any
   required explainer/catalog/routing/snapshot expectations. New argument descriptions,
   owner/DM gates, direct=false and mandatory preview must be explicit. Instructions <=800 chars.
5. Run targeted checks and required repository sync/lint/format/full tests. Run separate
   self-review for plan compliance and regressions; fix findings and repeat review. Do not
   claim environmental skips pass. Commit/publication only after review and verification.
6. For authorized delivery, release the next available platform version, update the private
   application's exact release pin/lock/packaged identity, add an application wiring regression,
   pass CI, and deploy the identical reviewed tree with normal health checks. Do not grant
   actual friends permission or send live group test messages as part of implementation.

## Verification

Run focused batch/store/administration/agent tests, then:

```sh
uv sync --locked --all-packages
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check .
uv run python .github/scripts/generate_explainers.py --check
uv run pytest -q -m "not browser and not sandbox_integration"
```

Use actual disposable-schema Postgres tests for atomicity, contention and injected audit
failure. Add no implementation-mirroring or unrelated tests. Keep existing group admission,
owner commands, budgets, DM membership and protected bridge behavior compatible.

## Limits and rollback

The bot can resolve only observed identities in authorized groups; unknown people must be
identified before granting access. No arbitrary public username directory is introduced.
Owner-only DM and a single human confirmation are deliberate parts of this flow. Removing
the new tool by rolling back code leaves previously confirmed grants intact; use existing
untrust commands to revoke them. The protected bridge's independent allowlist is unchanged.


Implementation refinement: legacy agent mode previously filtered tools only by its broad
admin flag. Add an opt-in `Tool.context_allowed` request predicate, used by registry offering
and invocation, and pass the existing ToolContext at legacy agent call sites. Existing tools
without a predicate are unchanged. This keeps the owner-only tool out of group/global-admin
requests in every skills mode; skill gates and handler/store authority checks remain independent.


## Verification and self-review

Local full suite: 4,355 passed, 57 skipped, 29 deselected, one expected failure and 14 existing
Telegram deprecation warnings. Focused regression set: 205 passed; the final batch/context-guard
set passed 21 tests, including one guard test added after full-suite collection. Postgres tests
cover five-person audit/revision, concurrent competing batches and rollback when a second audit
insert fails. Legacy request snapshots remain unchanged. Dependency sync, global lint, generated
explainers and changed-file formatting pass; repository-wide formatting retains 16 existing files.
No new dependency or migration is introduced.

Separate self-review: PASS for AC1–AC6 and regression risk. One design refinement was required:
legacy mode had only a broad admin visibility filter. The opt-in request guard now independently
hides/refuses the new tool for unauthorized contexts without altering existing tools. Unknown
names and mismatched identities never stage partial work; group membership is checked live before
reading directory rows. Batch authority and every target are revalidated under the commit lock.
Names are untrusted preview data, labels are bounded, and duplicate/no-op identities do not create
redundant grants. No real friend grants or group messages have been sent. No independent review.

## Integration correction

The private app regression found that SkillSet and SkillTurn registry filters omitted the
new context argument. The original on-mode test had no enabled chats and fell back to off.
Approved usability scope requires genuine off/on/shadow coverage: enable chats explicitly,
assert effective routing mode, pass context through all skill registry filters, retain
owner-only negative checks, and release the repair as v0.15.1 before downstream rollout.
