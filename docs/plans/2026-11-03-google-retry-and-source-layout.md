# Google retry and source-layout repair

Status: Planned delivery; implementation and independent review complete, with a new platform
release/downstream pin still required. Authorized by the owner's request to fix the repeated-retry bug and the
existing approved API editing requirement to import the original DOCX package instead of
reconstructing a document from extracted text. This is a repair to that scope, not a new
browser capability. No live Google mutation is part of implementation or validation.

## Diagnosis and design

An OpenAI-compatible provider can return native tool calls with finish_reason=stop. The
adapter currently labels that completion end_turn. The agent discards those calls, then its
empty-answer fallback requests a text-only response. A local reproduction reaches no pending
preview with end_turn but reaches one with tool_use. Runtime metrics show matching retry-only
turns without a budget/step/deadline stop; raw production completions were not retained, so
the exact production finish reason is inferred rather than claimed as captured evidence.

Normalize the adapter's stop reason to tool_use when native tool calls exist and the provider
finish reason is stop. Keep length, refusal/filter and unknown reasons unchanged. Do not retry
remote writes, execute textual tool syntax, or bypass gates, limits or confirmation. Log only
the reason category and call count, never content, arguments or URLs.

Further runtime inspection found a 2,048-token output limit and no tool-step/deadline stop in
the affected turns. Their aggregate output counts also permit a truncated native tool call as
the cause; production stop-vs-length remains unobserved. The repair must cover this path too:
never execute a max_tokens call, but allow the existing single tool-capable continuation to
regenerate a complete, smaller call against the unchanged transcript when time/budget permits.
Share the same one-continuation budget with empty responses; never replay an executed write.
Filter/refusal/unknown reasons do not gain tool execution. If recovery still cannot complete,
report the provider-response limitation truthfully instead of claiming the tool budget was
exhausted or asking for repeated prose approval. Global/skill/time/output limits stay unchanged.

The formatting loss came from choosing google_create_doc, which creates a blank native Doc
and inserts generated plain text. Source-derived revisions must instead use google_edit_doc:
destination=copy when a separate output is requested; DOCX imports the original package before
the bounded exact replacement. google_create_doc remains appropriate for content authored from
scratch or an explicitly requested plain-text rewrite. The current edit primitive supports one
literal replacement inside one paragraph/cell; a broad restructure must be explained rather
than silently replacing the document with a plain-text recreation. Converter fidelity is not
pixel-perfect and is not established by mocked tests.

## Execution plan

### 1. Provider regression and repair

Files: packages/core/src/chatcore/llm/openai_compat.py;
packages/core/tests/test_a_providers.py; packages/core/tests/test_agent_empty_response.py.

- [x] Add a failing adapter regression with native calls and finish_reason=stop. Expect a
  tool_use completion with unchanged call ID, name and arguments.
- [x] Cover length/filter/unknown outcomes: do not normalize these to tool_use. Preserve
  malformed-argument handling and tool_choice=none enforcement.
- [x] Add an agent-level regression using the real adapter with a mocked transport: a valid
  native call marked stop reaches one pending preview without another user turn and performs
  no committed write. Test that a final/limited turn cannot execute it.
- [x] Add failing agent regressions for max_tokens with a native call: the incomplete payload
  never executes, one complete regenerated call may reach a preview, repeated truncation stays
  bounded, and exhausted time/budget/final turns cannot regain tool execution. Do not confuse
  truncation with a permission failure or force the user through repeated go prompts.
- [x] Run the new regressions and record their intended failures before implementation.
- [x] Normalize only native stop calls and add safe count/category diagnostics. Run provider,
  empty-response, budget, deadline, step-cap and confirmation tests.

### 2. Source-preserving tool guidance

Files: packages/plugins/google/src/chatcore_google/plugin.py;
packages/plugins/google/tests/test_plugin.py; existing edit-service/client tests.

- [x] Add a failing prompt-contract regression requiring source edits to route through
  google_edit_doc, separate output to use destination=copy, and DOCX to use its original
  package rather than extracted text. Distinguish blank/plain-text creation explicitly.
- [x] Update GOOGLE_INSTRUCTIONS within 800 characters and create/edit tool and argument
  descriptions. Retain provenance, confirmation, browser opt-in, sharing, uncertainty and
  Sheet-race instructions. No tool names, schemas, gates or permissions change.
- [x] Extend a structured DOCX fixture regression where useful: original ZIP bytes including
  formatting/table parts must reach import_docx unchanged, and edits must stay bounded to the
  selected match rather than replace the whole imported body. Existing exact-range, mixed
  styling and read-back tests remain authoritative. Do not invent a visual-fidelity test.
- [x] Run Google tests and skill catalog/routing/request snapshot checks. Update snapshots
  only when the intentionally changed Google descriptions are actually represented.

### 3. Downstream guidance and delivery

The private app's itinerary composite also needs explicit original-package/copy guidance.
Keep private persona/configuration out of this repository. Validate candidate wheels against
that application without committing local-path dependencies or a floating platform pin.
Update this index record and CHANGELOG.md. Open linked public/private PRs; a downstream pin
must wait for a reviewed published release, so do not claim a source-only fix is deployed.

## Verification and review

- [x] uv sync --locked --all-packages
- [x] uv run ruff check packages services apps tests .github/scripts
- [x] uv run ruff format --check . (format changed files only; record unrelated baseline issues)
- [x] uv run pytest -q -m "not browser and not sandbox_integration" with a disposable database
- [x] Gitleaks, diff and plan/link checks; scaffold/installed-wheel checks as applicable
- [x] Independent Codex review, then fresh validation of fixes and clear release dependencies

Execution roles: Claude CLI is installed but its authentication probe failed with expired
OAuth. Under AGENTS.md's unavailable-model fallback, the coordinator records this plan,
Codex executes and an independent Codex reviews; no Claude review is claimed.


## Completed validation

The new stop-reason and truncated-native-call regressions failed before the repair. The
focused provider/agent/Google checks passed 366 tests. A final styled DOCX test verifies that
bold/table package bytes reach import unchanged and only the exact match is edited, including
style preservation. The complete platform suite passed 3,579 tests, two Elasticsearch skips
and 26 browser/sandbox deselections, with 14 existing Telegram deprecation warnings. It used
a disposable Postgres database and local Postgres binaries for isolated release-control tests.
The final test-only fixture refinement was rerun separately and passed.

Locked sync, whole-tree Ruff lint, seven changed Python files' formatting, diff checks and
Gitleaks passed. Full-tree formatting still reports the same 14 unchanged Markdown files as
an isolated origin/main archive; none were reformatted. Candidate core/Google wheels passed
the downstream app's full suite with no committed dependency changes. Installed scaffold
wheel imports, persona, agent wiring and console entry point passed outside the checkout;
the scaffold image built and its network-disabled read-only console smoke passed.

Independent Codex review found no actionable findings: 164 public focused checks and 32
private checks passed (one private embedding-cache skip). Claude authentication remained
unavailable, so no Claude review is claimed. No live Google writes, runtime configuration
changes, release tags, merges or deployments were performed as part of this repair.
