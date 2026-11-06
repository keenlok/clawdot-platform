# Google Doc verification outcomes and inherited styles

Status: Owner approved 2026-11-07; implemented and independently reviewed for v0.5.5.
Implementation: [PR #20](https://github.com/keenlok/clawdot-platform/pull/20).
Downstream release/deployment evidence is recorded on the associated PRs.

## Problem and evidence

A confirmed native-Doc update saved the exact requested text once, in the selected
current itinerary. Its journal and response nevertheless reported uncertain because
all three read-backs returned `style_mismatch`. Read-only inspection established
that the replacement's raw text-style object was empty, while its paragraph's
NORMAL_TEXT named style supplied the same expected font, size, weight and color.
The existing verifier compares raw overrides, without resolving inheritance.
Fresh read-only qualification on 2026-11-07 also identified table ancestry for this
paragraph. Under the conservative table-parent rule below, this historical case
therefore qualifies for exact saved-text reporting with a formatting warning; the
matching paragraph named style alone does not establish its complete effective style.

Google documents that an update matching its parent style becomes inherited and
that inherited properties are returned as unset fields. See the
[update request contract](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request#UpdateTextStyleRequest)
and [text style contract](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents#TextStyle).

The content is already present. This repair must not repeat that write, append the
addition again, recreate a file, change an ACL or rewrite the historical journal.

## Approaches and decision

1. **Recommended:** resolve provable effective styles and report text/formatting
   outcomes separately. This corrects the false mismatch and keeps useful failure
   reporting when a style cannot safely be resolved.
2. Reporting-only repair: describe verified text accurately while leaving the raw
   comparison in place. Smaller, but retains avoidable formatting warnings.
3. Remove formatting verification: rejected because genuine formatting changes
   would no longer be detected.

Use approach 1, preserving the existing strict text, revision, coverage and
authorization checks. Unresolvable inheritance remains unverified.

## Result behavior

| Read-back evidence | Outcome and response |
| --- | --- |
| Exact text and equivalent effective formatting | Verified success at the actual destination URL |
| Exact text, formatting differs or cannot be resolved | Explicitly say text was saved and verified, then state the formatting warning and instruct against repeating the edit |
| Text differs | Do not claim the requested text is saved; report the failed text check |
| No usable read-back | Report verification unavailable and require inspection before any further mutation |

For a saved original, use wording such as: “The text was saved and verified in
this Doc. Formatting preservation could not be verified. Do not repeat the edit.”
Do not label the entire result uncertain when the current text read-back is exact.
Keep genuine text uncertainty distinct from a formatting-only warning.

For a new copy with a formatting warning, disclose its URL and that sharing and
current-itinerary advancement were not performed. Those operations remain gated
on complete verification. No successful text write is retried automatically.

## Implementation boundaries

- `edit_snapshot.py` and `edit_models.py`: capture the selected tab's named-style
  context and each paragraph's named-style identity alongside existing raw runs.
  Resolve supported effective properties through the documented inheritance chain.
  Keep raw styles available for write requests and preserve selected-tab/UTF-16
  range handling. Additive contract fields must have compatible defaults.
- Introduce a focused style-resolution helper if needed. Validate context shape;
  do not assume a missing field means a fixed editor default, ignore an unresolved
  table-style parent, drop unsupported properties or invent color tolerances.
  Unknown parent/default/table cases must produce a formatting-unverified outcome.
  Explicit overrides still take precedence, and actual font/size/color/weight/link
  differences must fail preservation verification.
- Freeze the expected style evidence at proposal/commit under the existing source
  revision checks. Compare resolved before/after formatting from the appropriate
  snapshots; do not recompute the expected baseline solely from the post-edit Doc.
- `doc_verification.py`: expose separate text and formatting evidence in its result.
  Preserve at most three reads within five seconds and never repeat a mutation.
  A later readable text mismatch must invalidate an earlier matching-text result;
  read errors/deadlines must not create unsupported claims of current success.
- `edit_service.py` and relevant Doc-creation callers in `service.py`: use structured
  verification evidence to form precise results. Record known text-only completion
  as a partial result using the existing journal schema, with a bounded formatting
  category; reserve fully verified for complete checks. Do not infer success from
  exception wording. Keep sharing and trip-primary advancement behind full success.
- Plugin-owned guidance and operator/explainer docs: distinguish saved text from
  formatting verification and require inspecting the current Doc before proposing
  another write. Keep skill instructions within 800 characters and regenerate wiki
  sources. No new tool, dependency or SQL migration is proposed.

Existing source/owner/chat/topic/writer checks, current-itinerary selection,
confirmed access choices, unrelated Sheets/browser behavior and pending-action
single-use protections remain unchanged. This is a verification and reporting
repair, not a grant of additional write authority.

## Regression and review requirements

- First reproduce the real API shape with synthetic content: explicit overrides
  before the edit, the same values inherited from NORMAL_TEXT after it. The test
  must fail on the current raw-comparison verifier before implementation.
- Verify native Doc edits and qualified copy/conversion paths with named styles,
  paragraph-specific styles, explicit overrides and the selected Docs tab.
- A real font, size, weight, color or link change must still be detected. Mixed
  styles, suggestions, missing coverage, unknown inheritance and unsupported table
  context must not be silently accepted.
- Test exact-text/formatting-only warnings separately from text mismatch, read
  failure and deadline. Include an early exact read followed by later mismatched
  text so “saved” cannot become a stale success claim.
- Assert exactly one content mutation; format warnings must not cause another
  edit, copy, permission grant or current-itinerary promotion. Verify the emitted
  confirmation result and durable result phase/category together.
- Preserve same-Doc trip follow-ups, source revocation, concurrent primary
  selection and duplicate-action tests. Use memory and task-local Postgres tests
  where the durable outcome paths change.
- Run repository-required locked sync, lint, changed-file format and the full
  non-browser/non-sandbox-integration suite, with package-boundary, plan/catalog,
  instruction-cap, routing and request-snapshot checks. Record unchanged baseline
  skips/warnings/format exceptions.

## Execution and delivery

After approval, use Luna for implementation and Sol for independent review under
the repository's model policy. Claude CLI is present but the fresh auth probe
returned loggedIn=false; document the available-model fallback in the PR.

Publish a reviewed platform patch, update the private exact release pin and package
identity, and follow normal main-CI-gated automatic deployment. Verify anonymous
builds, installed source hashes, healthy bot/renderer and unchanged runtime flags,
sharing defaults and browser-disabled state. Existing applied migrations and
third-party dependency locks must remain unchanged.

Production qualification is read-only: inspect the already-saved replacement with
the repaired resolver and report current evidence. Do not replay its uncertain
action, retroactively mark it verified, send a chat message or alter any document
content/sharing as part of this code repair.

## Implementation validation — 2026-11-07

Luna implemented runtime/tests, Sol independently reviewed and confirmed the final
inheritance-context fixes, and Codex coordinated docs/release. The synthetic
explicit-to-NORMAL_TEXT regression failed before implementation. The final focused
snapshot/edit/trip run passed 86 tests; the Postgres partial-result journal test
passed across repository reload. The final full suite passed **3,788 tests**, with
four skips, 26 deselections and 14 existing Telegram deprecation warnings.

Locked sync, whole-tree lint, changed-Python formatting, generated explainers and
diff checks passed. Whole-tree formatting retains exactly the same 14 untouched
Markdown baseline exceptions. All 26 migration hashes and all 104 third-party
registry records are unchanged. All 19 candidate wheels built, and the Google
wheel's 34 runtime/SQL/explainer resources match reviewed source. The isolated
private app candidate suite passed 433 tests (24 skips, one deselection, six
subtests); released-pin validation and installed receipts follow on the private PR.

Production read-only inspection confirms the saved addition is present once and
its text hash matches the earlier inspection. Its table ancestry remains a
formatting warning under this conservative resolver. Historical journals, document
contents/access and trip associations remain unchanged.
