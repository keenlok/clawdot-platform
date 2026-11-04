# Google output follow-ups implementation plan

> For agentic workers: use the executing-plans skill task by task. AGENTS.md requires Luna
> execution and Sol independent review; the coordinator integrates and releases.

**Status:** Runtime implemented and independently reviewed; release and downstream delivery
remain CI-gated. Actual rollout receipt is recorded on the delivery PR.

**Goal:** Let the bot safely continue editing its own retained native Doc outputs, robustly
verify a single edit, and offer private/viewer/editor access with viewer as SBot's default.

**Architecture:** Google owns durable output authority, strict bounded read-back verification
and fixed-role link grants. Core confirmation semantics remain intact; the app selects the
reviewed default and exact immutable platform pin.

**Tech stack:** Python 3.12, existing asyncio/httpx/Pydantic, PostgreSQL, Google REST APIs.
No new dependency, arbitrary HTTP/permission tool, browser activation or live Google test write.

[Approved design](2026-11-04-google-output-followups-and-access.md) is authoritative. Owner
approved viewer default/editor on request. Claude auth status reports loggedIn=false, so the
AGENTS unavailable-model fallback applies. Public base c98f18e/v0.4.0; fetch current main at
integration. Baseline Google unit suite: 282 passed, 17 integration deselections.

## Files and ownership

Luna public executor owns `packages/plugins/google/` runtime, migrations and tests, plus Google
feature docs/explainer/architecture/changelog. Use small internal helper modules for access,
source authority and verification when this keeps service.py/edit_service.py readable; package
__init__.py remains re-exports only. Do not modify generic core, adapters, unrelated plugins,
browser-worker implementation, applied SQL, or other worktrees.

Private Luna executor owns its approved delivery plan/index, config/sb_bot.env/schema,
bots/sb_bot/.env.example and deployment regression. It must wait for a released tag before
changing exact sources/lock/identity/manifest. Coordinator handles release metadata, generated
wiki, integration verification and shipping; Sol reviews runtime and maintenance changes.

## Task 1 — Access normalization, failing tests first

- [ ] Add parameterized tests to Google tests for the following complete tool argument table:
  new/old omitted -> canonical setting, otherwise legacy setting; explicit private/viewer/editor
  with no legacy argument -> exact choice; legacy true -> viewer, false -> private; both supplied
  -> ValueError even if equivalent; invalid enum, non-bool legacy, integer booleans -> ValueError.
- [ ] Run those tests and record intended missing-feature failures before production changes.
- [ ] Add `new_docs_link_access: Literal["private", "viewer", "editor"] | None = None` to settings;
  None falls back to the existing `new_docs_link_viewer` flag. Framework defaults private.
  Add optional `link_access` to create_doc/edit_doc; keep the old link_viewer argument.
- [ ] Implement one normalization helper, with equivalent exact behavior to:

```python
ACCESS_LEVELS = frozenset({"private", "viewer", "editor"})


def resolve_access(settings, link_access=None, link_viewer=None):
    if link_access is not None and link_viewer is not None:
        raise ValueError("Use link_access or link_viewer, not both.")
    if link_access is not None:
        if not isinstance(link_access, str) or link_access not in ACCESS_LEVELS:
            raise ValueError("Choose private, viewer or editor.")
        return link_access
    if link_viewer is not None:
        if type(link_viewer) is not bool:
            raise ValueError("link_viewer must be a boolean.")
        return "viewer" if link_viewer else "private"
    configured = settings.new_docs_link_access
    return (
        configured
        if configured is not None
        else ("viewer" if settings.new_docs_link_viewer else "private")
    )
```

- [ ] Freeze canonical access in new payloads; decode old boolean-only payloads true=viewer,
  false=private and missing=private. Validate canonical/legacy coexistence consistently: reject
  conflicting choices, and never promote a legacy choice because configuration changed. A
  canonical editor payload cannot carry a contradictory legacy viewer/private choice.
- [ ] Preview role near the start, including attachments. Reject sharing requests for an original
  instead of changing its ACL. Private/viewer/editor explicit overrides apply only to a new Doc.

## Task 2 — Durable output authority with additive SQL

- [ ] Write PostgreSQL and memory tests before implementation: owner/topic binding persists after
  used core confirmations are purged and repository restart; NULL root topic differs from a
  non-root topic; wrong owner/chat/topic, missing/partial legacy binding and original/browser
  results deny authority; purge/retention revokes it. Record RED before changing runtime/SQL.
- [ ] Add `0005_output_authority.sql`, adding nullable owner permission ID, owner email, topic and
  new-output mode to google_edit_results. Use this explicit constraint structure (adapt column
  names consistently in code):

```sql
ALTER TABLE google_edit_results
    ADD COLUMN output_owner_id TEXT,
    ADD COLUMN output_owner_email TEXT,
    ADD COLUMN output_thread_id TEXT,
    ADD COLUMN output_mode TEXT;
ALTER TABLE google_edit_results ADD CONSTRAINT google_output_binding_check CHECK (
    (output_owner_id IS NULL AND output_owner_email IS NULL
        AND output_thread_id IS NULL AND output_mode IS NULL)
    OR
    (output_owner_id IS NOT NULL AND char_length(output_owner_id) BETWEEN 1 AND 200
        AND output_owner_email IS NOT NULL AND char_length(output_owner_email) BETWEEN 1 AND 320
        AND output_mode IS NOT NULL
        AND ((kind = 'create_doc' AND output_mode = 'create' AND source_id IS NULL)
            OR (kind = 'edit_doc' AND output_mode IN ('copy', 'docx_import')
                AND source_id IS NOT NULL)))
);
CREATE INDEX google_output_destination_idx
    ON google_edit_results(chat_key, destination_id)
    WHERE output_owner_id IS NOT NULL AND destination_id IS NOT NULL;
```

- [ ] Extend reserve_edit with an optional validated output binding, written atomically with
  google_actions/result reservation; memory fallback copies the same fields. Create/copy/import
  reservations capture owner ID/email and action.thread_id; original/Sheet/browser rows remain
  completely unbound. No inference/backfill of legacy owner/topic from current configuration.
- [ ] Add scoped output-authority lookup by chat, exact nullable topic and destination ID. Eligible
  phases are destination_created, applying, shared, verified, partial and uncertain; require native
  Doc kind/new-output mode, full binding, valid known ID and distinct source/destination for edits.
  Conflicts/reserved/unknown IDs, malformed bindings and ambiguous multiple matches fail closed.
  `destination_creation_claimed` alone is not an eligibility test for legacy API edits.
- [ ] Keep all prior SQL bytes unchanged. Prove old INSERTs with absent bindings remain valid and
  lack authority, so older app code can run against the added schema on rollback.

## Task 3 — Use recorded output authority for reads and fresh edit proposals

- [ ] Add failing own-output follow-up tests using real GoogleService/GoogleEditService with fake
  fixed Google transport: no current human URL, retained bound destination in same topic ->
  read/proposal allowed; wrong human source_message_ref cannot negate independent output proof;
  arbitrary bot/history URL and wrong chat/topic/owner remain refused before content is fetched.
- [ ] Add a small shared source resolver for native Docs. Prefer valid human authority where it
  exists; otherwise use only the scoped durable output record. Validate configured identity,
  owner email, ownedByMe, app authorization, actual native-Doc MIME, non-trash and live metadata.
  Recheck durable binding after Google I/O and before commit. A new request against a known
  uncertain output creates a fresh action; it never replays the original uncertain mutation.
- [ ] Wire both document reads and API Doc editing to it. Retain the existing public return values
  of helpers used by browser code; browser source authority must not expand implicitly.
- [ ] Add source_message_ref to Doc-edit arguments and validate the actual retained same-topic
  human message at proposal time. Freeze/recheck selected-source authority at confirmation.
- [ ] Journal every new create_doc output, including explicit private/legacy-private choices,
  with binding and existing replay claims. Preserve known IDs and partial-result semantics.
  Legacy completed private creates without records require ordinary human-link authority.

## Task 4 — Strict bounded read-back verification

- [ ] Add RED tests for a stale GET followed by an exact GET, transient read/snapshot failure then
  exact success, permanent text/style/coverage mismatch, truncated/oversized snapshot and deadline.
  In each case assert exactly one content mutation and at most three read-back requests. Include
  both normal batchUpdate success and exception-with-possible-completion branches, DOCX/table/UTF16.
- [ ] Extract a reusable helper for native Doc verification. It uses exactly the existing strict
  full flattened text and uniform target-style/complete target coverage checks. Do not reject
  unrelated images merely because their nontext content is excluded from the edit snapshot, and
  do not loosen text/style comparison to make a regression pass.
- [ ] Limit read-back to three GET attempts under an aggregate 5-second deadline, with delays
  0.1s then 0.4s only after a failed read/check. Inject/patch sleep and clock in tests. No create,
  edit, permission or other mutation is ever retried. Read timeout cancellation is read-only.
- [ ] Record specific bounded categories: read_error, snapshot_error, text_mismatch, style_coverage,
  style_mismatch or readback_deadline, plus attempts in safe logs. Do not log bodies, credentials
  or raw upstream responses. Persistent mismatch returns uncertain with known safe URL and the
  accurate statement that content verification failed before sharing; no automatic replay.
- [ ] Test known-destination outcome wording separately from creation-unknown outcomes. Do not
  retroactively mark the historical uncertain action verified or alter its ACL/journal.

## Task 5 — Fixed editor grants and exact ACL verification

- [ ] Add failing transport/body tests: viewer -> anyone/reader, editor -> anyone/writer, both
  allowFileDiscovery=false; private -> no grant. Invalid levels cannot send a request.
- [ ] Extend fixed sharing only to the frozen role on a validated journaled new Doc, after strict
  content verification. Keep the existing local authorization recheck and durable attempt marker
  before the single POST. No recipient/email, arbitrary permission body or endpoint is accepted.
- [ ] Parameterize strict ACL-delta checking for exact requested role. Preserve all prior entries;
  exactly requested public grant added or a preexisting identical grant with zero delta succeeds.
  A different role/manual broader grant stops without downgrade/upgrade/rewrite. Refusal, timeout,
  malformed response/readback mismatch retains known URL and partial/uncertain state, never replay.
- [ ] Test legacy true/false/missing payloads after an editor-default flip; originals, Sheets,
  browser trials and the existing manually edited output must receive no ACL mutation.

## Task 6 — Documentation, review and public release

- [ ] Update Google README, operator guide, packaged EXPLAIN, architecture decision and concise
  changelog; instructions stay <=800 characters and plugin-owned. Update tool catalog/routing
  snapshots only for intentional new argument/behavior changes. Regenerate wiki using
  `uv run python .github/scripts/generate_explainers.py` and require its --check to pass.
- [ ] Run Google unit/PG and catalog/boundary/snapshot/docs/migration tests, whole-tree lint and
  changed-file format. Coordinator runs complete non-browser/non-sandbox suite with disposable
  Postgres, scaffold container and installed-wheel smoke. Scan tree/history with Gitleaks.
- [ ] Sol independently reviews the full diff, SQL NULL/rollback/privacy cases and mutation counts;
  executor reproduces and resolves findings with tests, then Sol rechecks. No Claude review claim.
- [ ] Set root release v0.5.0, uv lock root metadata and dated changelog/comparison links together.
  Commit only after checks, release dry run, push PR, attach artifact, wait CI, merge expected head,
  wait main/release success and verify immutable tag anonymously at actual merge SHA.

## Task 7 — Exact downstream pin and installed rollout

- [ ] Private plan/index precedes code. Select GOOGLE_NEW_DOCS_LINK_ACCESS=viewer with plain/bot
  classification and sample/docs/regression, retaining old boolean compatibility. Preserve current
  Mandarin/Japanese tutor flags, secrets, renderer assets and all unrelated app wiring/settings.
- [ ] Build candidate wheels and run the private full suite against them before release. After
  published v0.5.0, update all17 source declarations,16 locked Git packages, config/platform.json,
  generated app identity, current-pin docs and complete migration manifest. No local/floating pins.
- [ ] Verify every previous migration hash unchanged and new0005 exact; third-party lock entries
  unchanged; anonymous locked sync, lint/format, encryption guard, Gitleaks, full app tests and both
  images/offline smoke. Sol maintenance review precedes private PR/expected-head merge after CI.
- [ ] Wait main CI and use installed sb-bot-autodeploy.service. Verify actual current/previous tags,
  pre-migration backup mode0600/nonzero/format, SQL0005 checksum, exact package/source identity,
  effective viewer default/editor option, healthy bot/renderer, timer and retained rollback assets.
  Inspect existing output only read-only if needed; do not create Google fixtures or change ACLs.
- [ ] Record actual test/CI/tag/runtime evidence on PRs. Update plan status in the implementation
  PR before release and attach final installed receipt without another docs-only deployment.

Implementation and release: [PR #16](https://github.com/keenlok/clawdot-platform/pull/16).
