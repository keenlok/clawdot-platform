# Drive-hosted DOCX read support

Status: **Live.** Owner approved implementation, merge of [PR #54](https://github.com/keenlok/clawdot-platform-history/pull/54), automatic deployment and the additional read-only Drive consent on 2026-11-02. Deployed reader verification passed.

**Goal:** Let a person paste a Drive-hosted `.docx` link into an allowed chat and
have SBot read its text and tables, including as an itinerary-import source.

**Architecture:** Extend `google_read_doc` with a metadata-driven DOCX read branch.
The Google plugin downloads bounded binary content through its existing authenticated,
fixed-host transport and passes it to a pure bounded ZIP/XML reader. Native Google
Docs/Sheets, CSV attachments, chat gates and confirmed writes retain their behavior.

**Plan author / execution:** Codex prepared this plan because the installed Claude
CLI failed with an expired OAuth session that could not refresh. Per `AGENTS.md`,
use the available model and disclose this fallback in the implementation PR.
Codex implements and self-reviews; attempt Claude's required final review if its
authentication becomes available, otherwise record the unavailable review explicitly.

**Branch / base:** `codex/drive-docx-support`, starting at `c081dc1` on `origin/main`.
This is an existing isolated worktree; preserve the other local checkouts.

## Scope and observed evidence

- The owner requested **Drive-hosted DOCX**, not uploaded DOCX or new Word writes.
- Synthetic DOCX fixtures exercise the Word Open XML MIME branch and bounded extraction.
- Drive file access may return `appNotAuthorizedToFile` under file-scoped consent even when
  metadata is readable. A pasted link alone does not grant the OAuth client file access.
- Existing scopes are Docs, Sheets, `drive.file`, and `drive.metadata.readonly`.
  A pasted link does not itself grant the OAuth client's per-file Drive access.
- The selected design supports pasted links with additional **`drive.readonly`**
  consent. A new Picker UI is outside this change. Google classifies the broader
  scope as restricted; do not promise that publishing removes verification rules.
- No authorization scopes, Google grants, server credentials, production configuration
  or public privacy content are changed while preparing this plan.

## Acceptance criteria

| ID | Required behavior |
| --- | --- |
| A1 | A human-posted Docs/Drive URL resolving to DOCX can be read with `google_read_doc`; native Docs URLs still use the Docs API. |
| A2 | Main-body paragraphs and table cells are rendered in document order, with empty cells, paragraph boundaries, tabs and line breaks represented. |
| A3 | Results state their extraction coverage and truncation; images, headers/footers, comments, footnotes and visual layout are not claimed as read. |
| A4 | ZIP, XML, streamed download and result limits reject hostile/corrupt inputs with bounded safe errors and no external resource access. |
| A5 | Human URL provenance, current chat access/opt-out, owner identity, untrusted-data marking and omitted persistent read results remain enforced. |
| A6 | The existing `itinerary_import(source_url=...)` path accepts DOCX as a source, rechecks source access, and preserves confirmation for the resulting Sheet write. |
| A7 | Word append/create/convert, Excel, legacy `.doc`, macro-enabled `.docm`, file sharing and Drive enumeration are not added. |
| A8 | Existing native Docs/Sheets credentials continue to work without reauthorization; insufficient DOCX content access gives a clear reauthorization hint. |
| A9 | Documentation and the public privacy description disclose broader Drive read access before the new grant is installed. Credentials never enter code, fixtures or logs. |
| A10 | After separately approved consent and rollout, the supplied real file passes an authenticated read through the bot's service path, without changing it or sending chat messages automatically. |

## Design decisions

### MIME dispatch and access

Add `DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"`
in `service.py`. Keep the existing native-only `check_type` behavior for writes and
Sheet targets. Add an explicit read/source allowance to `file(...)`, defaulting to
native-only; `read_doc` and the itinerary import source branch are its only callers
that permit DOCX. Do not make `expected=DOC` globally accept Word files.

Use MIME metadata, never the URL host, extension alone or the presence of a heading
fragment, to select the reader. For DOCX, read the whole main body within limits;
state that a Google Docs tab or heading fragment does not select a DOCX section.

The call order stays provenance -> chat authorization -> owner identity -> metadata
-> download eligibility/size -> content request -> extraction. Add
`capabilities(canDownload)` to metadata and require affirmative download capability
for DOCX. Never set `acknowledgeAbuse`, follow a `webContentLink`, or bypass a
download restriction. Do not use ambient search/history URLs as authorization.

### Bounded binary transport

Refactor the current bounded request body read into a shared internal transport
helper in `client.py`; JSON requests parse those bytes afterward. Expose a bounded
binary read method for the exact fixed Drive endpoint
`https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true`.
Keep HTTPS/host/userinfo/port validation, public-IP pinning, TLS hostname, no proxy,
no redirects, no automatic retries, status handling and timeout behavior unchanged.

Reuse `GoogleSettings.max_bytes` (default 2 MB) for the compressed download. Check
metadata size and `Content-Length` when present, but enforce the actual streamed
byte count independently. Do not buffer unbounded error bodies. Map content-access
403 to a safe hint that the owner may need to reauthorize Drive reads; do not claim
that every 403 is a scope failure or echo Google's raw error text.

### Pure DOCX extraction

Create `packages/plugins/google/src/chatcore_google/docx.py` with the entry point:

```python
def docx_text(data: bytes, *, max_chars: int) -> str:
    """Return bounded, coverage-labelled main-body paragraph/table text."""
```

Use standard-library `zipfile` for the container and a declared `defusedxml>=0.7.1,<0.8`
dependency for XML parsing. Use `defusedxml.ElementTree.iterparse` with
`forbid_dtd=True`, `forbid_entities=True`, `forbid_external=True`, start/end events,
and an explicit depth/node budget. Do not monkey-patch the standard library.

Proposed constants: at most 1,024 ZIP entries, 16 MiB total declared expanded size,
8 MiB per XML member actually read, compression ratio at most 200, XML depth at most
64 and at most 100,000 XML elements. Enforce the actual byte budget while reading
members, not just ZIP metadata. Never extract the archive to disk or open members
by filesystem path. Reject duplicate mandatory parts, encrypted members, malformed
archives, unsupported compression and missing/invalid main-document parts. Reject
macro parts if a mismarked DOCX contains them. Read only the package content-type
part and `word/document.xml`; do not resolve package relationships or embedded URLs.

Recognize transitional and strict WordprocessingML main-document namespaces. Render
visible main-body paragraphs and tables in order, including hyperlink display text
without following links. Use explicit table row/cell boundaries; preserve empty
cells and identify merged cells without inventing repeated content. Bound nested
tables with the same depth budget. Avoid duplicate text from alternate branches.
Omit deleted text, hidden text and non-body parts rather than presenting them as
ordinary visible text; disclose those coverage limitations. Include a compact
coverage line and a truncation marker within `max_chars`; never silently cut a
result and imply full coverage.

### OAuth and rollout boundary

Add `https://www.googleapis.com/auth/drive.readonly` to new operator authorization
requests in `oauth.py`; retain the existing scopes for compatibility and writes.
Loading a preexisting profile must not require the new scope or mutate its stored
scope list. Google remains authoritative about granted access.

This is a **permission/privacy change**, even if `gates.py` and `permissions.py` do
not need edits. The broader grant permits reading the owner's accessible Drive
files at the Google layer; the bot still permits only authorized human-posted
links. No Drive search/list tool is introduced.

Before owner reauthorization, update the public privacy page in the separate
`clawdot-oauth-site` project to disclose the broader read permission, requested-link
use, DOCX processing and the existing AI-provider data flow. Use the same Sites
project and public audience. Prepare and review all changes before requesting the
action-time grant approval. Do not reuse the old consent approval as authorization
to add this scope. Do not publish secrets or the sample document's content.

## Ordered implementation and verification

### 1. Container/XML reader (A2-A4, A7)

Files: create `docx.py` and `tests/test_docx.py` in the Google plugin; modify its
`pyproject.toml` and `uv.lock` for the explicit XML dependency.

- [x] Generate synthetic ZIP/XML fixtures in tests; do not commit a real user's DOCX.
- [x] Red tests cover paragraphs around a table, empty/multi-paragraph cells, nested
  and merged cells, line breaks/tabs, display text for hyperlinks, both namespaces,
  hidden/deleted/alternate content, truncation and honest coverage labels.
- [x] Red security tests cover ZIP entry/expanded/ratio limits, actual XML read caps,
  duplicates, missing parts, malformed XML/ZIP, encryption/compression failures,
  DTD/entities in UTF-8 and UTF-16, macro parts, depth/nodes and unsupported roots.
- [x] Implement the pure reader; pass `uv run pytest -q packages/plugins/google/tests/test_docx.py`.

### 2. Binary client and service read branch (A1, A4, A5, A7, A8)

Files: `client.py`, `service.py`, `plugin.py`, `tests/test_client.py`,
`tests/test_service.py`, and relevant existing Google tool/result tests.

- [x] Add failing MockTransport tests for exact media path/auth, byte streaming,
  advertised size, redirects, timeout, non-public destinations, safe 403/401 errors
  and unchanged JSON requests/token refresh behavior.
- [x] Add service tests showing that provenance, opt-out and wrong owner fail before
  media access, MIME selects DOCX despite a Docs URL, download-denied/oversized
  metadata fails before content, and native Docs still use `client.document`.
- [x] Prove Word remains rejected as a write target or spreadsheet target and that
  read results remain untrusted with `persist_content=False`.
- [x] Implement the binary method and read-only dispatch. Update only the relevant
  read-tool description/instructions and affected snapshots.
- [x] Pass `uv run pytest -q packages/plugins/google/tests/test_client.py packages/plugins/google/tests/test_service.py`.

### 3. Itinerary source integration and routing (A5-A7)

Files: `itinerary_service.py`, `tests/test_itinerary_service.py`,
`apps/sb_bot/src/sb_bot/skills.py`, routing fixtures/catalog tests where affected.

- [x] Add a regression where `propose_import` sees DOCX metadata, calls the Doc reader
  rather than `read_sheet`, and stores no raw DOCX bytes in the pending action.
- [x] Preserve exactly-one-source validation, CSV attachment behavior, target MIME
  rules, source/destination provenance and requester confirmation/rechecks.
- [x] Add one Drive-Word reading and one Word-to-trip routing case. Keep the composite
  instructions within their existing ownership and length constraints.
- [x] Pass Google itinerary tests and the affected app routing/catalog/snapshot tests.

### 4. Authorization, documentation and reviews (A8-A10)

Files: `oauth.py`, its existing tests, `docs/google-workspace.md`, relevant Google
and itinerary plans, `docs/plans/README.md`, plus the separately owned public privacy page.

- [x] Test that a newly constructed authorization URL requests `drive.readonly` and
  the existing scopes; ensure old private profiles/native reads still work.
- [x] Explain the new scope, 403 behavior, reauthorization steps, extraction limits,
  dependency and permission change. Replace the earlier blanket Word deferral with
  the supported read-only scope; keep other deferred formats explicit.
- [x] Update Google activation status records to reflect the already completed
  production setup, while distinguishing DOCX grant/live validation still pending.
- [x] Codex reviews the complete diff against A1-A10 and repairs findings. Attempt
  Claude final review; disclose any unavailable reviewer rather than claiming it ran.
- [x] Run the required repository checks below; document any environmental failure
  with evidence. Do not silently skip Postgres-backed tests.
- [x] Prepare the implementation PR with dependency, expanded permission, execution/
  review roles, exact checks and any live rollout steps still pending. No automatic merge.
- [x] After separately authorized merge/deployment and action-time consent, rotate
  the private owner profile safely, verify identity and real DOCX extraction through
  the deployed service, confirm the original file is unchanged, and record completion.

```sh
uv sync --locked --all-packages
uv run ruff check packages services apps tests
uv run ruff format --check .
uv run pytest -q -m "not browser and not sandbox_integration"
```

Only format changed files if main has preexisting formatting failures. Preserve
secrets and unrelated edits throughout; no migration or new long-running service
is expected.

## Primary references

- [Drive downloads and download capabilities](https://developers.google.com/workspace/drive/api/guides/manage-downloads)
- [Drive scopes and per-file access](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [defusedxml controls and explicit parser use](https://github.com/tiran/defusedxml)
- Existing [Google plan](2026-11-01-google-workspace.md) and [itinerary plan](2026-11-01-itinerary-tools.md).


## Execution and self-review record (2026-11-02)

Codex executed inline after the owner's explicit plan approval. Claude planning and final
review were attempted through the installed CLI; both failed because its OAuth session had
expired and could not refresh. No independent/Claude review is claimed.

- A1-A8: implemented and covered by synthetic parser, MockTransport, real service/tool-result,
  itinerary confirmation/revocation, routing, catalog and package-boundary tests. All earlier
  native Doc/Sheet behavior remains covered. No gates or permission modules were changed;
  expanding the requested OAuth scope is nevertheless a permission/privacy change.
- Parser review repaired safe-error handling for unknown XML encodings and corrupt deflate,
  and narrowed macro detection so harmless image names containing `vba` are accepted. Regression
  cases were observed failing before each repair. Hidden text handling covers direct run
  properties; style inheritance is deliberately not interpreted and is stated in coverage.
- The supplied real DOCX was fetched through the connected Drive plugin solely for a private
  parser check: 5,467 extracted characters and table structure. No content or real file ID was
  added to fixtures. The temporary private download was removed. This is not an end-to-end
  verification using the bot's still-unexpanded OAuth grant.
- A9: public privacy version 2, source `e8fafd5640f27d49a5db104541b81de4cda08ebb`, was published
  through the existing Sites project, preserving public access. It describes optional broad
  Drive reads, posted-link limits, DOCX extraction and AI processing before any new grant.
- `uv sync --locked --all-packages`: passed. Repository-wide Ruff lint passed. All changed
  files pass formatting and whitespace checks; the full format command reports 18 untouched
  preexisting Markdown files, with no overlap with this diff.
- Full local suite: 3,413 passed, 71 skipped, 22 deselected; 92 release-control setup errors
  from macOS shared-memory exhaustion and one integration failure from the existing database's
  `chatcore/0005_context_reset.sql` checksum mismatch. The migration, release-control files and
  failing integration test are unchanged from the base. No applied migrations, host IPC or
  kernel settings were altered. The final clean Linux result is recorded below.
- Self-review: PASS for the implementation scope; rollout was subsequently verified below. The
  expanded dependency is `defusedxml>=0.7.1,<0.8`; the lockfile records the resolved package.
- A10 completed after explicit owner authorization: PR #54 merged as
  `0a5cd47ecddbe239e86d9d81c782741f4af35f44`; main CI passed and the existing host timer
  automatically deployed release `0a5cd47ecddb`. The approved `drive.readonly` grant was
  installed atomically in the private owner profile, preserving owner/client identity, mode
  600 and runtime UID 10001. The deployed tool registry read the supplied DOCX using an
  isolated QA chat context and the real owner account: 5,511 result characters with tables,
  untrusted and non-persistent read flags. Original file modification time was unchanged;
  native Doc and Sheet reads passed. The same deployed container remained healthy. No live
  Google write or chat message was sent; temporary credential/setup copies were removed.
- Final [Linux CI](https://github.com/keenlok/clawdot-platform-history/actions/runs/41462816332) passed
  on `f807320d771c2f8b0edb33d40e0362393213b1c9`: 3,572 passed, 6 skipped, 22 deselected.
  The local environment failures above were absent. [Main CI](https://github.com/keenlok/clawdot-platform-history/actions/runs/41463997167)
  also passed before the automatic deployment.
