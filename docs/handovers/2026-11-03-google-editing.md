# Google editing delivery across the repository split

Execution record for the owner's 2026-11-03 authorization of the reviewed
[API](../plans/2026-11-02-google-api-editing.md) and
[browser](../plans/2026-11-02-google-browser-editing.md) plans.

## Reconciliation

The implementation starts from public platform `f918fa9` and private deployments
`9fd24d5`, fetched from their current `origin/main`. The former monorepo is reference
only. Historical PR 59 and historical commit IDs remain associated with
`clawdot-platform-history`; they are not identifiers in this public repository.

Reusable Google code, migrations, generic worker services and scaffold checks stay
in the platform. Former `apps/sb_bot` references map to downstream `bots/sb_bot`;
its persona, composites, routing fixtures, operations and Compose files remain
private. No app is recreated here and no operator configuration is copied here.

The reviewed scope is authorized; path translation does not restart design approval.
The browser result journal uses the API journal's `shared` phase, retained with
other unresolved results. A reserved crash window is retained conservatively too.
API edits remain independent of the browser. There is no automatic backend fallback.

## Execution and review roles

Codex and Claude executables were found. Claude's actual availability probe failed:
"OAuth session expired and could not be refreshed." Execution uses Codex
`gpt-6-luna` under architecture section 7b, with independent Codex
`gpt-6.1-sol` review and coordinator integration checks. No Claude review is claimed.

The app-managed worktree tool rejected these explicitly requested repositories
because this chat's root still points at the historical checkout. Authorized Git
worktrees on isolated `codex/` branches were used instead. The historical checkout
and both existing main checkouts are preserved.

## Baseline evidence

- Platform locked sync and lint passed. Baseline non-browser/non-sandbox suite:
  3,359 passed, 58 skipped, 21 deselected. Current skips were observed, not copied
  from historical counts. Full format check reports 14 existing Markdown plans;
  those unrelated files are left unchanged.
- The unchanged public renderer's real Chromium tests passed: 18 passed.
- Installed scaffold wheel smoke passed outside the checkout. The scaffold
  container built and ran a console echo with networking disabled and a read-only
  root filesystem.
- Private deployments' locked sync, lint, formatting, encryption guard and gitleaks
  tree scan passed. Anonymous HTTPS resolved v0.1.1 to
  `7408cb82b592fed6d714eceeefbcd2403295b6da`; pinned bot and renderer images built
  without a source key, and the bot image ran its command help offline.
- The first downstream test run encountered an existing checksum mismatch on the
  default development database. No migration/checksum repair was made. A rerun
  against disposable Postgres passed: 381 passed, 7 skipped, 1 deselected. Eight
  additional real SBot create/append confirmation regressions passed.

## Shared egress prerequisite

The separate `codex/shared-egress-proxy` change moves renderer proxy, policy and byte-budget types into
`chatcore.security.egress_proxy`, preserving renderer compatibility imports and
object identity. This is public [PR #6](https://github.com/keenlok/clawdot-platform/pull/6).
No logger names existed in these modules to change. Existing
tests contained no relocated function patch targets; only environment patches.
No installed policy manifest listed the moved files.

Executor and coordinator checks passed 209 renderer/snapshot/boundary/index tests.
Independent review found no findings: 319 targeted tests and all 18 Chromium
boundary tests passed. The mutation probe inserted the same failure into the real
policy function on baseline and extracted code; exactly the same seven test nodes
failed, and both source trees were restored. The built core wheel exported the
new proxy outside the checkout. This refactor changes no renderer behavior.

## API implementation and review

The Google plugin now proposes bounded native Doc and typed Sheet edits and executes
only frozen confirmed actions. Qualified copies and original-package DOCX imports share
destination/result primitives with the browser broker. A durable one-time creation
claim closes concurrent/restarted helper replay; the still-unreleased additive migration
contains that claim. Applied migrations 0001 and 0002 remain byte-for-byte unchanged.

Independent review reproduced and corrected REST field-mask/visibility issues,
suggestion-container and bound checks, copy outcome/link reporting, and a shared-helper
duplicate-creation race and stale-authority gap. Final independent review found no
remaining findings: 311 targeted tests passed without skips. Coordinator Google tests
passed 238 cases against disposable Postgres. Changed code passes lint/format.

An API wheel installed into a separate downstream environment passed all 398 current
application tests, with seven observed skips (unavailable Elasticsearch, Linux flock
inspection, ShellCheck and embedding cache). The two new native-edit confirmation cases
were first observed failing against v0.1.1, then passed against the installed candidate.
The imports resolve to the wheel's site-packages outside the platform checkout; no
local-path dependency or production pin change was committed. The public API implementation is
under [PR #5](https://github.com/keenlok/clawdot-platform/pull/5).

## Browser broker and worker implementation

The browser branch adds the isolated `services/google-browser-editor` process and a Google
plugin broker/client. The broker exposes a disabled-by-default, writer-gated tool only in
configured trial chats. It always creates/imports a new native Doc, shows the exact configured
writer grantees in the confirmation, shares only that copy, checks permission read-back, and
verifies the saved selected-tab text/style through the owner's Google API before marking the
action verified. API editing remains the default; there is no automatic browser fallback.
Cancellation and privacy cleanup remove worker metadata best-effort, not Drive copies or shares.

The dedicated editor account and session were not provisioned. Synthetic worker/renderer tests and
mocked broker tests cover the protocol and policy but do not qualify live Google sign-in, Docs UI,
resource hosts or autosave. The worker's bold operation requires an exact accessibility selection
and focused editor signal and can refuse if the live UI does not provide them. The broker now
requires an explicit target bold value and unchanged style-field presence/value outside the target;
targeted inherited-style regressions pass. The operator guide records the separate 20-trial,
10-replacement/10-bold qualification; none of those live trials has been run.

Final frozen evidence: **3,566 passed**, 2 Elasticsearch skips and 26 deselections in the
public non-browser/non-sandbox suite, with 14 PTB deprecation warnings from Telegram fixtures.
All 229 worker/renderer local tests passed, including synthetic real Chromium; 304 focused
broker/plugin/boundary/index tests passed. The installed final candidate passed **401 downstream
application tests**, with seven environment skips, one deselection and six passing subtests.
All twelve real SBot confirmation cases passed after the inherited-style fix, including verified
and uncertain browser outcomes and duplicate-confirmation protection. The unchanged v0.1.1 pin
separately reports five expected new-tool failures; it does not validate this candidate.

A container smoke launched real Chromium with its sandbox enabled as UID 1001, networking
disabled, read-only root, tmpfs, dropped capabilities, no-new-privileges and the committed
seccomp profile, against a synthetic blank page. Candidate bot and renderer wheel-overlay
images built and ran offline packaging checks. Installed core/scaffold smoke passed outside the
checkout. All 23 existing migration hashes are unchanged; only Google 0003 is new. Both browser
seccomp files match the downstream existing asset. Full lint and changed-file formatting pass;
full formatting still reports the same 14 pre-existing Markdown plans. Secret scans are clean.

The browser PR is forthcoming. The code is unmerged; copying and browser editing remain disabled
by default, live behavior is unqualified, and nothing has been deployed.

## Release and live-operation boundaries

The downstream production pin remains v0.1.1 until a reviewed platform release
contains these changes. Candidate wheel tests are separate from release-pin tests;
passing against v0.1.1 does not validate the candidate. The downstream draft must
update all sources, exact lockfile commit, platform configuration, migration
manifest and relevant browser security assets together once the release exists.

No merge, release tag, credential rotation, deployment or live Google write is
authorized by this delivery. Native-copy grant cases and DOCX conversion require
owner-grant qualification. The browser trial remains disabled until separately
authorized live qualification with a fresh dedicated editor account succeeds.
That account/session setup blocks live qualification only, not local tests.
