# Google retry and source-layout repair

Status: Planned. Authorized by the owner's request to fix the repeated-retry bug and the
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

- [ ] Add a failing adapter regression with native calls and finish_reason=stop. Expect a
  tool_use completion with unchanged call ID, name and arguments.
- [ ] Cover length/filter/unknown outcomes: do not normalize these to tool_use. Preserve
  malformed-argument handling and tool_choice=none enforcement.
- [ ] Add an agent-level regression using the real adapter with a mocked transport: a valid
  native call marked stop reaches one pending preview without another user turn and performs
  no committed write. Test that a final/limited turn cannot execute it.
- [ ] Run the new regressions and record their intended failures before implementation.
- [ ] Normalize only native stop calls and add safe count/category diagnostics. Run provider,
  empty-response, budget, deadline, step-cap and confirmation tests.

### 2. Source-preserving tool guidance

Files: packages/plugins/google/src/chatcore_google/plugin.py;
packages/plugins/google/tests/test_plugin.py; existing edit-service/client tests.

- [ ] Add a failing prompt-contract regression requiring source edits to route through
  google_edit_doc, separate output to use destination=copy, and DOCX to use its original
  package rather than extracted text. Distinguish blank/plain-text creation explicitly.
- [ ] Update GOOGLE_INSTRUCTIONS within 800 characters and create/edit tool and argument
  descriptions. Retain provenance, confirmation, browser opt-in, sharing, uncertainty and
  Sheet-race instructions. No tool names, schemas, gates or permissions change.
- [ ] Extend a structured DOCX fixture regression where useful: original ZIP bytes including
  formatting/table parts must reach import_docx unchanged, and edits must stay bounded to the
  selected match rather than replace the whole imported body. Existing exact-range, mixed
  styling and read-back tests remain authoritative. Do not invent a visual-fidelity test.
- [ ] Run Google tests and skill catalog/routing/request snapshot checks. Update snapshots
  only when the intentionally changed Google descriptions are actually represented.

### 3. Downstream guidance and delivery

The private app's itinerary composite also needs explicit original-package/copy guidance.
Keep private persona/configuration out of this repository. Validate candidate wheels against
that application without committing local-path dependencies or a floating platform pin.
Update this index record and CHANGELOG.md. Open linked public/private PRs; a downstream pin
must wait for a reviewed published release, so do not claim a source-only fix is deployed.

## Verification and review

- [ ] uv sync --locked --all-packages
- [ ] uv run ruff check packages services apps tests .github/scripts
- [ ] uv run ruff format --check . (format changed files only; record unrelated baseline issues)
- [ ] uv run pytest -q -m "not browser and not sandbox_integration" with a disposable database
- [ ] Gitleaks, diff and plan/link checks; scaffold/installed-wheel checks as applicable
- [ ] Independent Codex review, then fresh validation of fixes and clear release dependencies

Execution roles: Claude CLI is installed but its authentication probe failed with expired
OAuth. Under AGENTS.md's unavailable-model fallback, the coordinator records this plan,
Codex executes and an independent Codex reviews; no Claude review is claimed.
