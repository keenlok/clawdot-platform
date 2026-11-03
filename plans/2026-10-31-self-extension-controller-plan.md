# Scoped chat self-extension implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. The root orchestrator owns dispatch/review; do not create additional agents or commit/push.

**Goal:** Deliver the complete opt-in request → generate → sandbox-test → review → group/DM activation → invoke → monitor/revoke workflow for pure JSON transforms.

**Architecture:** An explicit BasePlugin uses required-chat repositories, existing pending confirmations, a live scoped-authority callback, a bounded Provider generator and the real worker client from stage A. Generic static tools dispatch approved revisions; no generated code enters the bot process or changes shared framework files.

**Tech Stack:** Python 3.12, chatcore Provider/Plugin/PendingAction, asyncpg-style Database, memory repository, httpx worker client, Telegram optional live admin lookup, pytest.

---

Prerequisite: worker plan passes real isolation tests. Read the design and worker contracts; do not stop at a fake WorkerClient. This plan plus stage A is the execution scope. Reminders and website watches are being implemented in a separate chat as trusted reusable plugins; do not implement them here.

## File ownership and interfaces

Create in `packages/plugin-extensions/src/chatcore_extensions/`:

- `repository.py`: `ExtensionRepo` protocol and locked `MemoryExtensionRepo`.
- `pg_repository.py`: Postgres implementation; all identifiers looked up with required chat key.
- `migrations/__init__.py`, `migrations/0001_extensions.sql`: additive scoped tables/indexes.
- `authority.py`: optional scoped role callback and fail-closed DM/group policy.
- `generator.py`: provider prompt, strict response handling, budget accounting.
- `service.py`: lifecycle orchestration and evidence/activation/run checks.
- `plugin.py`: tools, `/extensions`, pending handlers and privacy hooks.
- `settings.py`: feature-specific explicit settings.
- `tests/test_repository.py`, `test_pg_repository.py`, `test_authority.py`, `test_generator.py`, `test_service.py`, `test_plugin.py`, `test_e2e.py`.

Modify narrowly: `packages/adapter-telegram/src/chatcore_telegram/__init__.py` and `tests/test_telegram.py` (optional live admin method); `apps/sb_bot/src/sb_bot/app.py` and app tests (opt-in wiring/resource cleanup); `apps/sb_bot/pyproject.toml` and `uv.lock` (dependency); `apps/sb_bot/.env.example` (disabled local settings); `tests/test_tool_audit.py` (new plugin tool boundary). Add `packages/plugin-extensions/README.md` for local worker/test-chat operation. **Do not modify core access.py, platform.py, tools/registry.py, plugin.py, agent.py, confirmation behavior, system prompt, production Dockerfile/compose/deploy scripts, or another worktree.** If the parallel permissions change already provides `is_group_admin`, use/reconcile it; never create competing role semantics.

Stable new interfaces:

```python
GroupAdminCheck = Callable[[ChatRef, UserRef], Awaitable[bool]]
ScopeAllowed = Callable[[ChatRef], bool]

class ExtensionAuthority:
    async def may_manage(self, *, chat: ChatRef, user: UserRef, is_dm: bool) -> bool: ...

class ExtensionGenerator:
    async def generate(self, *, chat: ChatRef, requester: UserRef,
                       description: str, examples: tuple[ExampleCase, ...]) -> Artifact: ...

class ExtensionService:
    async def request(self, *, chat: ChatRef, actor: UserRef,
                      description: str, examples: tuple[ExampleCase, ...]) -> str: ...
    async def build(self, *, chat: ChatRef, actor: UserRef, request_id: str) -> str: ...
    async def activate(self, *, chat: ChatRef, actor: UserRef,
                       revision_id: str, digest: str) -> None: ...
    async def revoke(self, *, chat: ChatRef, actor: UserRef, revision_id: str) -> None: ...
    async def run(self, *, chat: ChatRef, actor: UserRef,
                  revision_id: str, arguments: dict[str, object]) -> RunResult: ...
```

Service methods are trusted internal APIs, not public authority assertions: plugin entry points and confirmation commits recheck authority before management; service still scopes every repository operation. The plugin constructor takes `repo`, `generator`, `worker`, `authority`, `limits`, `enabled` predicate. `scope_allowed` is injected from current `AccessPolicy.is_chat_allowed`; DM ownership is separate and still subject to BotApp's existing DM gate.

Repository operations all require keyword `chat: ChatRef`: `create_request`, `get_request`, `claim_build`, `save_revision`, `get_revision`, `save_evidence`, `get_evidence`, `activate`, `get_activation`, `revoke`, `reserve_run`, `record_run`, `list_active`, `status`, `purge_user`, `purge_chat`. `purge_older_than(cutoff)` is the only global retention operation and cannot retrieve source/results. `claim_build` atomically changes requested→building and increments build quota; failed attempt cannot run twice through repeated pending callbacks. `activate` checks matching passing evidence in its transaction and increments activation epoch. `record_run` checks captured epoch, updates consecutive failures and writes metadata atomically. Run-start quota reservation and active-epoch lookup occur atomically. Use UTC dates for extension quotas and state that explicitly; existing provider budget remains on its configured SGT day.

## Task B1 — persistence and privacy (SE-02, SE-04, SE-07)

- [x] Write repository conformance tests parametrized over memory and PostgreSQL implementations. Create group A, B and DM artifacts; exercise every operation with a foreign known ID; require absent/denied behavior and unchanged foreign data. Include same digest in two scopes without cross-scope lookup.
- [x] Define tables with `(chat_key,id)` composite primary/foreign keys: extension_requests (author, specification/examples, state/times); extension_revisions (request ID, immutable canonical artifact/digest, author/times); extension_evidence (revision/digest, passed, image/policy/harness identities, test count/time); extension_activations (revision, reviewer, digest, epoch, status/failure count/time); extension_runs (scope, run ID, revision/digest, actor, status/duration/time); extension_quota (scope, UTC day, builds/runs). Never store raw execution arguments/results/stderr in audit rows. Cap proposals per scope at 100 pending entries to prevent storage abuse.
- [x] Implement transaction-safe immutable insertion and CAS/row-lock transitions. Reject updates to artifact source under an existing revision ID. Build failures create safe status metadata and consume their quota; generation output is not persisted as a successful revision until strictly valid. No “fetch by ID then trust its scope” helper.
- [x] Implement privacy operations: revoke before deleting a user's authored/reviewed artifacts, delete their scoped records; chat purge deletes everything in its scope; retention drops proposals/unactivated artifacts and metadata older than cutoff but preserves current approved artifacts. Foreign groups remain unchanged. Test purge racing activation/run result: no orphan activation and no result after purge.
- [x] Run memory tests, then Postgres tests against existing development services with `@pytest.mark.integration`; root runs these if service setup is needed. A skipped database suite is reported as unverified, not completed persistence verification.

## Task B2 — live scoped authority (SE-03)

- [x] Test ordinary member denial, current administrator/creator approval, demotion between command and confirmation, lookup failure, unavailable optional method, cross-platform IDs, foreign group, and DM ownership. Assert global `ctx.is_admin=True` alone does not grant group management. Console tests inject an explicit role map; do not grant every console user admin status in production code.
- [x] Add `TelegramPlatform.is_group_admin(chat,user)` next to `is_member`, using the existing `_application.bot.get_chat_member`, validating `tg` references and returning true only for creator/administrator. Translate Telegram errors using existing `PlatformError` convention; authority catches errors and denies. No positive role cache for lifecycle writes.
- [x] Implement adapter discovery through `getattr(platform, 'is_group_admin', None)` in app wiring, wrapped in the injected callback. Do not add a required method to `ChatPlatform`. Authority checks group allowlist or exact DM identity. Existing core global-admin behavior stays untouched.
- [x] Run adapter and authority tests. At review inspect the other session's integration result only through the root's handoff; do not edit or merge its worktree.

## Task B3 — bounded generation and test evidence (SE-01, SE-04)

- [x] Use `ScriptedProvider` to test valid output, invalid/oversized JSON, refusal, timeout, truncated completion, unexpected tool call, secret-looking exception text and quota exhaustion. Seed explicit human examples so passing generated examples alone cannot replace them. Test source is never imported/executed on host by a sentinel artifact that would touch a host file if mistakenly evaluated.
- [x] Implement one Provider.complete call with tools empty, bounded max_tokens and 60-second timeout. `session_key` includes scope and opaque build ID; use only explicit description/examples in messages. Prompt requests strict artifact JSON and standard-library `run(arguments)`. Reject completions unless end_turn and no tool_calls. Preserve caller's examples exactly; generated cases may supplement within the limit, never overwrite. No autonomous repair loop in v1.
- [x] Check shared `Limits` budget/rate policy before generation and record returned Usage input+output tokens; add transactional per-scope five-build daily cap. Never read a production `.env` for tests. Provider config/keys stay in controller memory only.
- [x] `service.build` runs each case via the worker, compares JSON results on host using deterministic equality (reject non-finite floats), and stores evidence bound to artifact and worker identities. Worker execution is independent per case. Any failed case prevents activation. Store bounded counts/status, not raw stdout. Show model-generated cases as such in review; tests do not prove code harmless.
- [x] Unit tests simulate wrong image/policy/harness evidence and ensure activation fails. Run one scripted-generation → real-worker case after stage A.

## Task B4 — plugin user flow and confirmations (SE-01 through SE-04)

- [x] Register exactly three static tools with explicit handler arguments and no scope/authority parameters:
  `request_extension(description, examples)` (`writes=True`), `list_extensions()` and `run_extension(revision_id, arguments)`. JSON examples are `{arguments: object, expected: JSON}`. Nested arbitrary data is accepted as data only with strict size/depth checks; reject extra top-level fields. New list/run results always use `untrusted=True` because descriptions/output can contain generated instructions.
- [x] Request handler returns PendingAction kind `request`; commit validates payload again and persists current-scope proposal. It does not build on ordinary-member confirmation. Register handlers `request`, `build`, `activate`; bind exact scope/requester/IDs/digest in payload and verify `action.chat == ctx.chat`, `action.requester == ctx.user.ref`, expected plugin/kind and current stored state at commit. Pending payload is not trusted merely because it came from the database.
- [x] Implement `/extensions` grammar: `list`, `build ID`, `review ID [PAGE]`, `activate ID FULL_DIGEST`, `revoke ID`, `status [ID]`. Reject unknown subcommands, excess arguments and invalid IDs/pages. Register command with `admin_only=False`; enforce per-subcommand authority inside so group admins are not blocked by global-admin routing. Listing/running approved tools is available to admitted members; source/spec/status of unactivated requests is requester-or-current-admin only, scoped. Never expose private DM items to group admins.
- [x] `build` and `activate` request existing ConfirmService confirmations; source review paginates at <=2500 characters with exact revision/digest/page counts. Escape code fences or use safe plain formatting so generated text cannot forge review headings/buttons. Confirmation includes exact scope, full digest, pure-transform ceiling and passing evidence. `activate` commit rechecks live role and unchanged evidence. `revoke` performs immediate restrictive revocation after live role check. Non-admin reviews of others' draft source deny.
- [x] Test using `BotApp.handle` and FakePlatform: member request+confirm, admin build+confirm, review all source pages, activate+confirm, member list/run, revoke/run-denied. Include wrong requester, other chat, expiry, replay, forged IDs/digest, demotion and unsupported callback. Extend tool audit with the plugin using test dependencies; use exact schema/handler matching, no `**kwargs` authority bypass.

## Task B5 — invocation, quotas and monitoring (SE-07)

- [x] Implement atomic start reservation capturing activation epoch and exact digest, 100/day/scope cap, then worker call; after completion require the same active epoch and enabled policy before exposing output. Failed/denied result is fixed text marked untrusted, without raw worker exceptions. Arguments/results are not added to plugin audit logs. Source already exists in scoped review storage only.
- [x] Implement three-consecutive-failure suspension, reset counter on success, metadata status command and global enabled predicate. Reactivation is a fresh admin confirmation of the existing tested digest. Do not automatically regenerate, approve, deploy or increase limits after failure.
- [x] Add race tests with barriers: revoke after start but before result, purge during run, two concurrent quota reservations at boundary, failure/success updates, suspension, and kill-switch flip. Assert no late output and no foreign-scope effect. Test restart persistence through PostgreSQL.

## Task B6 — app wiring, real end-to-end verification and handoff (SE-08, SE-09)

- [x] Add `ExtensionsSettings` with `EXTENSIONS_ENABLED=False`, worker socket path and token-file path; do not put token text in env/example. Load with existing explicit `settings_from_env`. When enabled, require provider, valid private worker credentials, reachable authenticated health, matching supported policy and persistent DB for Telegram. Console can use memory repo with explicit injected authority for tests. Fail startup with safe field names if configuration is incomplete. Disabled mode does not contact Docker or worker.
- [x] Add plugin before migration application and PrivacyPlugin construction; use existing `migration_sources(plugins)`. Register client `aclose` on AsyncExitStack. Since new migration package appears only when enabled, document admin CLI's hard-coded migration list and add its explicit plugin migration source if needed for enabled operation; do not silently make migration command require a live worker/provider. Add targeted `apps/sb_bot/src/sb_bot/admin.py` tests only if this minimal source registration is necessary.
- [x] Add app composition tests for default-disabled compatibility, enabled startup failure, provider/migration/privacy/resource lifecycle and worker secret redaction. Existing builtin tools must remain executable normally and unchanged.
- [x] Add real Unix-worker plus Docker BotApp end-to-end tests using ScriptedProvider and injected live role callback. Cover both group and DM success and foreign-scope/revocation denial. This exercises actual plugin/controller/client/server/container, not only a mocked worker. Real provider calls are not required in automated tests; README explains an optional explicit local provider smoke run with no production credentials copied.
- [x] README gives exact local launch and smoke procedure: create private temporary socket directory/token, inspect existing image to immutable ID, start worker from sanitized environment (`env -i` with explicit PATH/HOME needed by local Docker context and no bot credentials), configure test bot/controller separately, propose unit conversion with supplied examples, confirm/build/review/activate/run/revoke, then stop worker and remove only its private temporary files. Clearly state current Docker context and local/test limitation; do not tell users to mount Docker into production bot. Mention Docker context config may be needed by clean worker; pass only the necessary local config path, not an entire bot environment.
- [x] Run final checks from checkout:

```sh
uv run pytest -q -m 'not integration and not sandbox_integration'
SB_EXTENSION_SANDBOX_TESTS=1 uv run pytest packages/plugin-extensions/tests -q -m sandbox_integration
uv run pytest packages/plugin-extensions/tests/test_pg_repository.py -q -m integration
uv run ruff check .
uv run ruff format --check packages/plugin-extensions apps/sb_bot/src/sb_bot/app.py packages/adapter-telegram/src/chatcore_telegram/__init__.py
git diff --check
```

Baseline before implementation: 517 passed, 1 skipped (ShellCheck), 13 integration deselected. Explain changed counts and every skip. If a real enforcement or DB test cannot run, report the exact gap and do not claim that acceptance ID passes.

- [x] Root review maps each SE acceptance ID to tests/results, checks concurrency/security/privacy behavior and reconciles additive admin capability with other work. Do not broaden into reminders/site watches or production provisioning.
- [x] After root's authorized commit, handoff records full immutable `git rev-parse HEAD`, base SHA, clean/dirty state, test commands/counts, image ID/RepoDigest, policy/harness digest and remaining production limitation. Without a commit, report a working-tree diff and explicitly mark immutable source handoff incomplete. No remote is configured; don't invent a PR URL or push destination. Production release remains a separate action from approval of a generated tool. Generated artifacts currently have content digests, not Git commits; disclose that limitation explicitly and keep artifact-repository export as the documented follow-up, not an implicit claim of completion.
