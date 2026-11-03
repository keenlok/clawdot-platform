# Plugin Explainers and Coordinated Delivery Implementation Plan

> **For agentic workers:** Use the `executing-plans` skill to implement this plan task by task, in this session unless the owner requests delegation. Steps use checkboxes for tracking. Obtain owner approval of this plan before writing implementation code.

**Goal:** Give friends accurate explanations of the bot's architecture and each plugin, using the same plugin-owned Markdown for conversational answers, `/explain`, and public wiki pages; then review, merge, release, pin and deploy the result to SBot.

**Architecture:** Package an `EXPLAIN.md` with each plugin and one with core. Core loads documents only from the plugins selected by the app, provides deterministic slash-command explanations and a read-only documentation tool, and attaches fresh capability availability separately from descriptive text. A build-time generator derives wiki pages from those documents; the private deployment pins the reviewed platform release and supplies its exact source revision.

**Tech stack:** Existing Python 3.12+, `tomllib`, `importlib.resources`, frozen dataclasses, `chatcore` plugins/tools/skills/gates, uv, pytest, Ruff, GitHub PR/CI/tagging, and the existing private health-gated deployment tooling. No new dependency, database migration, service or persistent mode setting is needed.

**Status:** Owner approved implementation and delivery on 2026-11-04. Implementation and verification are in progress; platform release, wiki publication and private deployment are tracked separately below.

**Roles:** Codex drafted this plan under the unavailable-model fallback in AGENTS.md. On 2026-11-03, both CLIs were installed, but `claude auth status` returned `loggedIn: false`. Codex executes and reviews; retry Claude availability before final review. If it is still unavailable, record the fallback in each PR and use an explicit separate Codex review pass. Do not claim Claude participation that did not occur.

## 1. Baseline and repository ownership

The conversation began in an older monorepo checkout. Current work belongs in these two repositories:

| Repository | Baseline inspected | Responsibility |
| --- | --- | --- |
| `keenlok/clawdot-platform` | `54775cb38303444499ab4dd150b32c5c0fe9e82c`, root version/tag `0.1.3` / `v0.1.3` | Core interfaces, plugin documentation, generator, public documentation and platform release |
| `keenlok/clawdot-deployments` | `956a338` on `origin/main`; `config/platform.json` pins the same `v0.1.3` commit | Private persona, exact dependency pin, deployment tests, rollout and recovery evidence |

Refresh both repositories before execution. Work on isolated branches. Preserve unrelated edits in the original `sb_bot` checkout, including its modified plans index. Do not implement against the historical `apps/sb_bot` layout or `clawdot-platform-history` remote.

The authoritative rules are [AGENTS.md](../../AGENTS.md), [ARCHITECTURE.md](../../ARCHITECTURE.md), the private repository's AGENTS.md and its current runbooks. Keep stable architecture section numbers. Current plans belong in `docs/plans/`, with a row in `docs/plans/README.md` in the same change. Public plans contain no private configuration, production chat IDs or credentials.

Platform merges run platform/scaffold CI; they do not deploy SBot. A separate version-bump PR creates an immutable platform tag after successful trusted-main CI. A private dependency-pin PR then goes through private CI and the existing automatic deployer.

## 2. Approved design to implement

### User experience

| Input | Result |
| --- | --- |
| `/explain` | Short framework overview plus a bounded list of documented topics visible here |
| `/explain expenses` | Expense overview, section names and a source identity |
| `/explain expenses storage` | Exact maintained storage section, without an LLM call |
| `/explain expenses verification` | How to inspect an entry, undo a mistake and export records |
| `How do you keep expenses after your context fills up?` | Model calls `read_explainer(topic="expenses", section="storage")`, then answers from its result |
| `What can go wrong when reading a receipt?` | Model retrieves the maintained expense limitations rather than inventing guarantees |
| Unknown topic or section | Short error with permitted alternatives; no web/source-code search and no guessed explanation |

Group commands retain the existing bot-addressing rule, for example `/explain@<bot> expenses storage`; the app supplies the actual spelling through `command_form`. Responses stay in the current chat/topic. Existing access restrictions and mention routing still run before commands/tools. `/explain` is a one-request command, not a sticky group-wide mode.

Natural-language follow-ups use the existing conversation machinery. No command session, new summary store or implicit action replay is introduced. Explaining an action does not execute that action. This release documents general behavior; it does not claim to reconstruct a particular historical execution or provide a private model reasoning trace.

### One document, owned by the implementing plugin

Every document has TOML front matter followed by these exact, unique second-level headings:
`Overview`, `Workflow`, `Storage`, `Safety`, `Limitations`, `Verification`.
The corresponding section IDs are their lowercase names. Each section is at most 2,500
UTF-16 code units, so astral characters count correctly for Telegram's text limits; a whole
document is at most 24 KiB UTF-8. Headings inside fenced examples do not create sections.
A malformed or missing declared resource fails application construction with the owning
package named, before serving requests.

Example source shape for the expense document:

```markdown
+++
topic = "expenses"
title = "Expenses and trips"
summary = "How expenses, splits, settlements and trip balances are recorded."
aliases = ["ledger", "trips", "receipts"]
+++

## Overview
The model interprets a request; application code calculates splits and saves records.

## Workflow
The tool resolves the payer, participants and trip, validates the input, computes exact
shares, and commits the record. The bot reports the result and supported undo route.

## Storage
Expenses, explicit per-person shares, settlements and change events live in the configured
ledger repository. The persistent implementation uses PostgreSQL. Changing trip members
does not recalculate old shares.

## Safety
The handler checks access and write preconditions. Some writes are immediate and undoable;
others require confirmation. Consult the actual tool policy rather than promising every
write is confirmed.

## Limitations
The model can misread an amount, payer, currency or participant. A saved record can therefore
be durable and still contain a mistaken input.

## Verification
Check the recorded-entry echo, list the expense, inspect balances, export the CSV and use
the supported undo or restore command where applicable.
```

This is the required document structure and starter content, not a substitute for the
source-by-source review in Task 4. Public plugin wording describes framework behavior
generically; facts about SBot's selected repository belong in private guidance.

Use `EXPLAIN.md`, keeping the existing short `Skill.instructions` constants in `plugin.py`.
Their 800-character cap and ownership remain unchanged. A plugin may implement multiple
skills; one explainer covers the plugin and gives those skills aliases where unambiguous.
Do not create a second operational `SKILL.md` instruction system.

### Public interfaces and file boundaries

Introduce these types in `packages/core/src/chatcore/explainers.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExplanationSource:
    repository: str
    revision: str


@dataclass(frozen=True, slots=True)
class ExplainerSpec:
    topic: str
    title: str
    summary: str
    aliases: tuple[str, ...]
    package: str
    owner: str
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExplainerDocument:
    spec: ExplainerSpec
    sections: tuple[tuple[str, str], ...]
    sha256: str
```

The same module owns parsing, topic/alias validation, resource loading, bounded section
lookup, deterministic rendering and `ExplainerCatalog`. Its public operations are:

| API | Input and result |
| --- | --- |
| `parse_explainer` | Source text and keyword arguments `package: str`, `owner: str`, `tools: tuple[str, ...]`; returns `ExplainerDocument` |
| `ExplainerCatalog.from_plugins` | Class method taking selected plugins and optional `source: ExplanationSource`; returns a validated catalog |
| `ExplainerCatalog.resolve` | `topic: str`; returns `ExplainerDocument` or `None` |
| `ExplainerCatalog.documents` | No arguments; returns a sorted `tuple[ExplainerDocument, ...]` |
| `ExplainerCatalog.section` | `topic: str`, `section: str`; returns exact section text or raises `ValueError` for a known-topic/unknown-section request |

Keep IO and rendering small enough to test without constructing a bot. Topic IDs
match `[a-z][a-z0-9_]{0,47}`; aliases are stripped/casefolded IDs, globally unique across
selected documents. Reject empty titles, summaries over 160 characters, duplicate metadata
keys/aliases/headings, missing sections and oversized resources. Titles are at most 100
characters. Keep paragraph order and
newlines intact. Public-facing errors never expose filesystem paths or raw configuration.

Add `explainer_package: str | None = None` to `BasePlugin`, not a new mandatory member of
the structural `Plugin` protocol. Each built-in capability plugin declares its package
literal in `plugin.py`. Use `getattr(plugin, "explainer_package", None)` for third-party
plugins; no declaration means no documentation topic, not a construction failure.

Load exactly `resources.files(package).joinpath("EXPLAIN.md")`. The package comes from
reviewed plugin code, never a user argument. Capture related tool names from that selected
plugin's `tools()` declaration. Do not import unrelated plugins, walk site-packages at
runtime, read arbitrary paths/URLs, or load every installed distribution. Include core's
`chatcore/EXPLAIN.md` once, as the reserved `framework` topic (aliases `architecture`, `bot`).

Implement `ExplainPlugin` in `packages/core/src/chatcore/explain_plugin.py`, holding the
`explain` command, `read_explainer` tool, its `explanation` skill and its instruction
constant. It takes the catalog and a callable with the signature of
`SkillSet.evaluate_tool(name, ctx)`. Core owns this functionality; no new capability
package or standalone service is required.

`BotApp` accepts `explanation_source: ExplanationSource | None = None`. It builds a catalog
from the caller-selected plugins, appends exactly one internal `ExplainPlugin`, then builds
the existing tool and skill registries from the resulting plugin list. Pass a callback
that evaluates `self.skills` at request time, after construction; do not eagerly evaluate
an unset registry. Guard against a caller installing a second reserved `explain` plugin.

Add `read_explainer` to `DEFAULT_GENERAL_TOOLS`, while also declaring it in the
`explanation` skill. It must work in `SkillsMode.OFF`, `SHADOW` and `ON`, including an
explicit app override that removes it from the general set but loads its owning skill.
The tool has `writes=False`, optional `topic` (absence lists visible topics) and `section`
defaulting to `overview`; every argument has a description and a bounded JSON schema.
Do not add all explainer bodies to every prompt.

Documentation tool bodies are returned as `ToolResult(untrusted=True)`: they are evidence
to summarize, not new operating instructions. This uses existing content delimiters and
confirmation rules. An explain-and-write request in the same turn can consequently need
confirmation; do not add a direct-write exception for documentation retrieval.

### Runtime facts, availability and source identity

Evaluate related tools through the existing fresh `SkillSet.evaluate_tool` checks for
this caller/chat. The framework overview is always available after normal app access
checks. For plugin topics, exclude `UNCONFIGURED` tools and admin-only tools from a
non-admin's result. Hide topics with no disclosable tools; return the same generic unknown
topic response for an absent or fully hidden topic. For `OFF`/`NOT_ALLOWED`, expose only
existing safe gate hints for otherwise disclosable tools. Never claim all tool variants
are enabled because one tool in a plugin is available.

Keep returned data visibly separated: document section, per-tool availability/hints,
and source identity. Availability does not mean a proposed write will succeed: handlers
still check current permissions, arguments, service availability and preconditions.
Admin-only filtering, gate filtering and topic visibility need explicit regression tests.

With an `ExplanationSource`, validate repository as `owner/name` and revision as an
immutable 40-character commit SHA or a plain release tag. Generate public source links
to that revision and package resource path. Private SBot passes the public platform
repository and exact commit generated from its reviewed `config/platform.json` into a
small packaged `platform_source.json`; it does not pass private repository/configuration
contents into explanations. The runtime image contains installed wheels, not the repository
root, so it must not open `config/platform.json` relative to the working directory.

Without that app-supplied source, identify the result as bundled documentation plus its
SHA-256 digest. Do not infer the platform release from the core package version: the
platform root version and component package versions are different today. Never label
GitHub `main` documentation as the currently deployed implementation. Responses may
translate or simplify the retrieved facts but must acknowledge undocumented details.

## 3. File map

All platform paths below are relative to `clawdot-platform`:

| Path | Change and responsibility |
| --- | --- |
| `packages/core/src/chatcore/explainers.py` | New document types, parser, catalog and source renderer |
| `packages/core/src/chatcore/explain_plugin.py` | New command, read-only tool and explanation skill |
| `packages/core/src/chatcore/EXPLAIN.md` | Canonical general architecture explainer |
| `packages/core/src/chatcore/plugin.py` | Optional default resource declaration |
| `packages/core/src/chatcore/app.py` | Catalog/internal plugin wiring and optional source argument |
| `packages/core/src/chatcore/skills.py` | Add documentation tool to default general tools |
| `packages/core/src/chatcore/agent.py` | Brief grounding guidance in both prompt modes |
| `packages/plugins/*/src/chatcore_*/plugin.py` | Declare each plugin's own resource package |
| `packages/plugins/*/src/chatcore_*/EXPLAIN.md` | Canonical documentation, enumerated in Task 4 |
| `packages/core/tests/test_explainers.py` | Parser, resource boundaries, source identity, aliases and size bounds |
| `packages/core/tests/test_explain_plugin.py` | Commands, tool schema, source output, fresh gates and no mutations |
| `packages/core/tests/test_app.py` | Access, group addressing, topic and help integration |
| `packages/core/tests/test_agent_skills.py` | Natural-language retrieval in all skill modes |
| `packages/core/tests/test_agent_request_snapshot.py` and its JSON fixture | Review intentional new tool/guidance only |
| `packages/core/tests/test_skills.py` | General-tool, owning-skill and cap behavior |
| `tests/test_explainer_catalog.py` | New built-in documentation coverage and factual contract checks |
| `.github/scripts/generate_explainers.py` | Build-only wiki rendering/check CLI, no live publishing credentials |
| `tests/test_explainer_wiki.py` | Generated output parity, deterministic links and preservation boundaries |
| `docs/wiki/How-the-bot-works.md`, `docs/wiki/Explainers.md`, `docs/wiki/Plugin-<topic>.md` | Generated wiki source pages |
| `docs/wiki/README.md` | Generation and post-merge publication instructions |
| `apps/_scaffold/tests/installed_smoke.py` | Installed-wheel resource and `/explain` smoke outside checkout |
| `README.md`, `ARCHITECTURE.md`, `CHANGELOG.md`, `docs/plans/README.md` | Public behavior, architecture decision, Unreleased entry and status |
| Root `pyproject.toml` | Version change only in the later release PR |

Private paths are relative to `clawdot-deployments`:

| Path | Change and responsibility |
| --- | --- |
| `bots/sb_bot/src/sb_bot/app.py` | Supply exact public platform source identity from reviewed configuration |
| `bots/sb_bot/src/sb_bot/platform_source.json` | Generated packaged public repository/tag/commit identity, without private config |
| `shared/render_platform_source.py`, `tests/test_platform_source_resource.py` | Generate/check parity with `config/platform.json` and verify installed-resource loading |
| `bots/sb_bot/src/sb_bot/prompts/system.md`, `system_skills.md` in the same prompt directory | Explain runtime configuration truthfully and retrieve docs for internals questions |
| `bots/sb_bot/tests/test_explain.py` | New real-app wiring and both prompt-mode tests |
| `config/platform.json`, root `pyproject.toml`, `uv.lock`, `README.md` | Coordinated reviewed tag/commit pin |
| `config/platform-migrations.json` | Verify against release; change only if its inventory actually differs |
| `docs/plans/2026-11-04-plugin-explainers-rollout.md`, `docs/plans/README.md` | Private delivery checklist and status |
| `docs/deployments/2026-11-04-plugin-explainers.md` | Actual deployment, smoke and rollback evidence |

No applied migration, operational secret, permission module, renderer policy or release-control
activation is part of this feature. Core, package build configuration if needed, `.github`
scripts and deployment changes receive maintenance review. Explicitly call out changes to
documentation visibility and confirmation behavior in PR descriptions.

## 4. Implementation tasks

### Task 1 — Freeze the baseline and write failing document-contract tests

- [ ] Refresh both remotes, create implementation branches from their current `main`, read
  their AGENTS.md files, and record source SHAs and available planning/review models.
- [ ] Save a private rollout plan in the private index before private implementation.
- [ ] Add `test_explainers.py` with a complete six-section fixture and table-driven failures
  for duplicate aliases, missing/duplicate sections, invalid front matter, oversized UTF-8
  input, unknown topics and arbitrary-path-like topic strings.
- [ ] Start with this concrete regression, then exercise the rest of the stated contract:

```python
def test_alias_resolves_packaged_document_without_a_checkout():
    from chatcore.explainers import ExplainerCatalog

    catalog = ExplainerCatalog.from_plugins([])
    assert catalog.resolve("architecture").spec.topic == "framework"
    assert "Postgres" in catalog.section("framework", "storage")
    assert catalog.resolve("../../.env") is None
```

- [ ] Run `uv run pytest -q packages/core/tests/test_explainers.py`; confirm it fails
  because the new module/resource does not exist, rather than accepting a broken fixture.

### Task 2 — Implement the packaged document contract

- [ ] Implement the types, parser and catalog defined in section 2; add the optional
  `BasePlugin.explainer_package` declaration and core document. Preserve third-party
  plugins that have no explainer attribute.
- [ ] Read resources with `importlib.resources`, check size before parsing, compute the
  digest over exact source UTF-8 bytes, and resolve aliases from one validated index.
  Use a small fenced-block-aware section parser rather than splitting every `##` line.
- [ ] Validate optional source identity and generate links only from validated repository,
  revision and catalog-owned resource path. Escape formatting when rendering to adapters.
- [ ] Re-run the Task 1 command and verify each negative case fails for the intended reason.
- [ ] Commit the core document contract after checking its focused diff.

### Task 3 — Add the command/tool and integrate core routing

- [ ] Add failing command/tool tests before implementing `ExplainPlugin` and BotApp wiring.
  Cover default overview, exact topic/section, aliases, invalid arguments, help output,
  registered-only topics, reserved-name collision, missing third-party docs and translation
  guidance. Use existing `FakePlatform`, `ScriptedProvider` and `memory_stores` fixtures.
- [ ] Include an observable no-write regression using a fake plugin whose action handler
  and operational tool raise if called; `/explain` must return its document without touching
  either. Assert that slash explanations make zero provider calls.
- [ ] Implement the command grammar with at most two tokens (topic and section), and return
  deterministic Markdown/text plus section choices and source identity. Cap each command
  reply at 3,500 UTF-16 code units, including headers and links. Use topic names rather than
  full summaries in the overview's index; cap that index at eight entries with an explicit
  omitted count. Keep the requested document section intact, shortening only supplemental
  index/status details and explicitly reporting omissions. Cap tool results at 16 KiB UTF-8
  with the same explicit omission rule. Add Unicode/boundary tests for both limits. Unknown
  input never becomes a file path or URL. Use `command_form` in hints.
- [ ] Implement `read_explainer`; absent topic returns the overview/topic index and the
  `section` argument defaults to `overview`. Its response contains only one selected section,
  document metadata and filtered runtime tool facts. Mark its body untrusted as specified.
- [ ] Register the `explanation` skill with 4–12 examples spanning English, Chinese and
  Singlish, tool name `read_explainer`, command `explain`, and a behavior instruction constant
  below 800 characters. Add the tool to core's default general set.
- [ ] Bind visibility checks to fresh `SkillSet.evaluate_tool` calls. Test gate transitions
  between calls, admin/non-admin, `OFF`, `NOT_ALLOWED`, `UNCONFIGURED`, mixed tools, and
  same-named topics in a different app configuration. A read does not enumerate other chats.
- [ ] Add minimal core grounding guidance for both skills-enabled and legacy requests:
  retrieve maintained docs for implementation questions; distinguish facts from runtime
  availability; say when a detail is undocumented; use saved records for action-specific claims.
- [ ] Run the focused integration suite:

```sh
uv run pytest -q packages/core/tests/test_explainers.py packages/core/tests/test_explain_plugin.py packages/core/tests/test_app.py packages/core/tests/test_agent_skills.py packages/core/tests/test_skills.py
```

Expected: all pass, including a scripted request that calls `read_explainer` and never
calls an expense mutation while explaining it. Existing access/mention/topic behavior passes.

### Task 4 — Author every built-in plugin explainer from current code

Create these exact resources and add the corresponding class attribute to each `plugin.py`:

| Resource | Required implementation facts to review |
| --- | --- |
| `packages/plugins/expenses/src/chatcore_expenses/EXPLAIN.md` | Explicit stored shares, integer amounts/rounding, default trip selection, transaction boundaries, soft delete, event undo, recomputed balances, settlements, FX capture/override and receipt limitations |
| `packages/plugins/google/src/chatcore_google/EXPLAIN.md` | Read scopes, bounded native writes, frozen destinations/preconditions, copy/import behavior, result journal, partial/uncertain outcomes, and separately qualified/disabled browser paths |
| `packages/plugins/search/src/chatcore_search/EXPLAIN.md` | Message/attachment search, actual indexed document types, hydration, access scope, retention, best-effort indexing and fallback limits |
| `packages/plugins/memory/src/chatcore_memory/EXPLAIN.md` | Selected long-term facts versus conversation context, extraction, retrieval, deletion and indexing |
| `packages/plugins/web/src/chatcore_web/EXPLAIN.md` | Search/fetch distinction, URL provenance, untrusted output, network restrictions and fetch failures |
| `packages/plugins/browse/src/chatcore_browse/EXPLAIN.md` | Separate renderer, per-scope availability, JavaScript pages, guarded network and unsupported authenticated browsing |
| `packages/plugins/jobs/src/chatcore_jobs/EXPLAIN.md` | Stored jobs, reminders/watches, scheduling zones, worker execution, delivery and failure/retry limits |
| `packages/plugins/polls/src/chatcore_polls/EXPLAIN.md` | Native platform polls, schedules, per-group toggle, permissions and result limits |
| `packages/plugins/stocks/src/chatcore_stocks/EXPLAIN.md` | Actual upstream data, freshness, caching where implemented, read-only scope and missing-data behavior |
| `packages/plugins/research/src/chatcore_research/EXPLAIN.md` | Bounded research work, source acquisition, jobs/delivery, budgets and source-quality limits |
| `packages/plugins/extensions/src/chatcore_extensions/EXPLAIN.md` | Reviewed activation, tool scope, sandbox boundaries, confirmation, monitoring/revocation and inactive release-control distinction |
| `packages/plugins/tutor/src/chatcore_tutor/EXPLAIN.md` | Profile/topic persistence, lesson/quiz flow, personalization and model-generated feedback limits |

- [ ] Keep these documents public and framework-generic. State prerequisites as conditions,
  not a promise that a downstream app wired a service. Each section names useful user checks.
- [ ] Cross-check the framework storage section against the existing
  [storage guide](../storage-architecture.md). The three-store pattern describes the app;
  a plugin is not required to use all three. Mention the in-memory scaffold accurately.
- [ ] Record specific expense facts correctly: balances read saved expenses/settlements
  directly; the plugin does not cache balance summaries or use Valkey trip-write locks;
  FX lookup caching exists; `set_fx_rate` fills missing snapshots and affects future entries,
  without replacing existing non-null snapshots. Postgres transactions/row locks are the
  actual coordination mechanisms where present. Do not promise universal concurrency safety.
- [ ] Add `tests/test_explainer_catalog.py`. Enumerate the 12 package resources explicitly,
  parse each through the real parser, verify aliases are unique, headings/length limits
  hold, and ensure each plugin declaration references its own import package.
- [ ] Back critical document claims with behavior tests rather than only phrase matching:
  changing trip membership leaves old shares unchanged; computing balances uses all active
  ledger rows; an FX override preserves a pre-existing snapshot and fills a missing one.
  Reuse existing expense tests when they already exercise these outcomes, adding only gaps.
- [ ] Run `uv run pytest -q tests/test_explainer_catalog.py packages/plugins/expenses/tests`.
  Review each remaining plugin's document alongside its current `plugin.py`, `service.py`,
  repository and gates; no unverified architectural claim passes review.

### Task 5 — Generate wiki pages without maintaining a second explanation

- [ ] Add `.github/scripts/generate_explainers.py`, implemented with stdlib plus the core
  parser. It scans only core and the explicit repository plugin resource paths, never
  instantiates plugins or opens live services. Support `--check`, `--output-dir` and
  `--source-ref` (`main` for review output, a verified release tag/SHA for publication).
- [ ] Generate `How-the-bot-works.md`, `Explainers.md` and `Plugin-<topic>.md`. Strip TOML
  front matter, preserve source section content, add generated-file/source/digest headers,
  and render relative links against the selected public repository ref. Reject generated
  filename collisions and links that escape the repository. Do not modify existing
  `Storage-architecture.md`, Home or sidebar content.
- [ ] Add renderer fixtures proving exact section-text reuse, deterministic ordering,
  safe link rewriting, missing/collision failures, drift detection and release-ref links.
  `--check` returns nonzero for stale/missing/extra managed pages without overwriting files.
- [ ] Document manual post-merge wiki publication in `docs/wiki/README.md`. The separate wiki
  repository is a publication target, not an editable source of plugin facts. Preserve
  unrelated wiki pages and navigation; add a link to the generated Explainers index once.
- [ ] Generate/check the reviewed repository artifacts and run their tests:

```sh
uv run python .github/scripts/generate_explainers.py --source-ref main
uv run python .github/scripts/generate_explainers.py --source-ref main --check
uv run pytest -q tests/test_explainer_wiki.py tests/test_plans_index.py
```

Expected: deterministic generation, no source drift, all local links resolve. Add the
generator `--check` command to platform CI after locked sync; review the `.github` change.

### Task 6 — Verify installed packages and intentional request changes

- [ ] Build core and all 12 documented plugin wheels with `uv build --wheel --package NAME`
  and inspect each ZIP for its `EXPLAIN.md`. If uv_build omits these package files, add the
  minimal explicit include configuration only to affected `pyproject.toml` files and review
  it as a protected build change. Do not add a runtime dependency to ship documentation.
- [ ] Extend `apps/_scaffold/tests/installed_smoke.py` to resolve core documentation and
  exercise `/explain` from installed wheels outside the checkout, without network access.
  Add an equivalent installed-resource check for all plugin wheels without importing every
  plugin's heavy optional dependencies. Parsing source trees alone is insufficient.
- [ ] Run skill catalog/routing checks that exist on the execution baseline. There is no
  `routing_set.jsonl` in the inspected public baseline; if the execution baseline adds one,
  update it with explainer questions. Do not invent a nonexistent test path.
- [ ] Review and refresh the legacy request snapshot only for the deliberate documentation
  tool schema, core guidance and explanation skill/catalog additions. Preserve unrelated
  tool schemas and prompts. Add scripted natural-language regressions in all three modes.
- [ ] Add an `Unreleased` Added entry describing the command/tool, plugin document hook,
  capability/source qualification, and same-turn confirmation effect after reading docs.
  Document the source argument and third-party plugin opt-in in README/architecture.

### Task 7 — Platform verification and review before the feature merge

- [ ] Run the required commands on the actual feature head:

```sh
uv sync --locked --all-packages
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check .
uv run pytest -q -m "not browser and not sandbox_integration"
uv run python .github/scripts/generate_explainers.py --source-ref main --check
git diff --check
```

- [ ] Use disposable development services for integration coverage; run the scaffold's
  installed-wheel and container checks exactly as current CI does. Run Gitleaks with the
  repository configuration. Record skipped/unavailable checks and baseline-only formatting
  failures; format only changed files. Never replace a required failure with a success claim.
- [ ] Perform an independent Codex review pass over code, documentation claims, access
  semantics, package resources, prompt/snapshot diffs and generated pages. Fix findings and
  rerun affected checks. Obtain Claude final review if available; otherwise document the
  actual fallback, with a separate final review pass.
- [ ] Open a feature PR naming the planner/executor/reviewers, the public API addition,
  maintenance-reviewed core/CI/build changes, exact verification and downstream dependency.
  Keep the root release version unchanged in this feature PR. Update this plan's status to
  reflect merged platform capability, without claiming private deployment or wiki publication.
- [ ] Wait for required PR checks and merge the reviewed feature PR. Record its merge SHA.

### Task 8 — Validate private SBot against candidate wheels

- [ ] Add private app tests and wiring on its own branch, following its indexed rollout
  plan. Implement `shared/render_platform_source.py` with `--check`: read the reviewed
  `config/platform.json`, validate its public repository/tag/40-character SHA, and generate
  exactly those three fields into `bots/sb_bot/src/sb_bot/platform_source.json`. Commit this
  generated non-secret resource alongside the pin; parity tests fail if either drifts.
  Add the generator check to existing private source-integrity tests, rather than requiring
  new runtime filesystem access or environment variables.
- [ ] Import `ExplanationSource` from `chatcore.explainers` (core submodule imports are
  allowed). Load the sanitized packaged resource using `importlib.resources` and pass only
  public repository plus exact public source SHA to BotApp. Test it from the installed SBot
  wheel outside the checkout, matching the current container. Confirm uv_build includes the
  JSON; add an explicit package include only if required, with protected-file review.
- [ ] Update both private prompt modes so internal-workings questions use `read_explainer`;
  private runtime facts can explain that this app uses Postgres, while generic documents
  remain conditional. The prompt must not infer active tools from a public wiki page.
- [ ] Test the actual built SBot app: `/help`, `/explain`, addressed group commands,
  sections, natural-language expense questions, unconfigured/disabled tools, source SHA,
  and a receipt explanation without adding an expense. Check no private data appears in
  the public source identity or generated wiki.
- [ ] Install candidate wheels only in a disposable private test environment, against the
  existing lock's third-party versions. Run the private tests before publishing a release.
  Do not commit local wheel paths, an editable framework checkout, or a floating main pin.

### Task 9 — Bump and publish the platform release

- [ ] After the feature merge and green main CI, refresh all release tags. The proposed
  additive-feature release is **v0.2.0**, starting from inspected v0.1.3. Confirm that it is
  still higher than every published tag; if another release overtook it, choose the next
  unused higher minor release and carry that exact value through every following step.
- [ ] Create a separate release PR: set root `project.version` to `0.2.0`, date and move
  the curated Unreleased notes into `0.2.0`, start a fresh Unreleased section, and update
  changelog comparison links. Do not mass-bump component package versions or dependencies.
- [ ] Run the release decision tests, full required checks and the release-script dry run
  against the candidate release commit. Verify the root version/changelog agree:

```sh
uv run pytest -q tests/test_platform_release.py
python3 .github/scripts/platform_release.py --commit HEAD
```

Expected before tag publication: `Would publish v0.2.0` when comparing this release commit
to its first parent. If the selected version changed, the output must show that value.

- [ ] Review/merge the release PR, wait for the trusted-main `ci`, `scaffold` and `release`
  jobs, and verify `refs/tags/v0.2.0` resolves to that exact release merge commit. The tag is
  created by CI; never force, move, or manually replace an existing release tag.
- [ ] Record the immutable release tag and full SHA for the private pin and wiki source.

### Task 10 — Pin, review and merge the private delivery

- [ ] Replace every platform source tag in private `pyproject.toml` with the published
  release tag, update `config/platform.json` tag/full SHA, regenerate `uv.lock` and the
  packaged source-identity resource, and update README's pin. Confirm all platform sources
  resolve to the same public commit, including
  worker/renderer packages, without changing their configuration or enabling inactive tools.
- [ ] Check `config/platform-migrations.json` against the tagged package inventory. This
  feature adds no migration, so its existing contents should remain identical. If an
  intervening release adds one, include and review that separate upgrade explicitly.
- [ ] Run private required checks, with encrypted-secret validation and no decryption:

```sh
uv sync --locked --all-packages --python 3.12
uv run ruff check bots shared tests
uv run ruff format --check bots shared tests
uv run python shared/check_encryption.py
uv run python shared/render_platform_source.py --check
uv run pytest -q -m "not browser and not sandbox_integration"
git diff --check
```

- [ ] Run the current private CI bot/renderer image builds anonymously against the exact
  public pin, and Gitleaks with `.gitleaks.toml`. Record the successful pin-integrity and
  migration-manifest tests from that repository rather than assuming their names.
- [ ] Review the private diff independently, then obtain the available final reviewer.
  Open a private PR linking the public feature and release PRs, exact tag/SHA and rollout
  plan. Say who executed/reviewed and note unavailable-Claude fallback if still applicable.
- [ ] Wait for checks and merge. This private main merge is the production deployment
  trigger; do not merge before the exact release exists and all review gates are satisfied.

### Task 11 — Observe deployment and perform bounded production checks

- [ ] Observe the existing private automatic deployment rather than launching a second
  competing manual deploy. Use current runbooks and private CI/deploy records. Verify
  the handled private commit, deployed release, container health and public platform pin.
  Use read-only host checks, for example:

```sh
ssh "$DEPLOY_HOST" 'cat /home/ubuntu/sb_bot/CURRENT_TAG; readlink /home/ubuntu/sb_bot/current; docker ps --filter name=sb-bot --format "{{.Names}} {{.Status}}"'
ssh "$DEPLOY_HOST" 'sudo journalctl -u sb-bot-autodeploy --since "30 minutes ago" --no-pager'
```

- [ ] Read only relevant log excerpts; do not copy secrets or user conversation content
  into reports. Confirm the deployment is the private merge SHA and its release manifest
  pins the exact published platform SHA. A release notification alone is insufficient.
- [ ] With the owner's existing test chat/session authorization, check `/explain`, expense
  overview/storage/limitations, an ordinary-language context-window question, and one
  disabled/unconfigured topic. If sending test messages has not been authorized, prepare
  these exact commands for the owner and leave live conversational verification pending;
  do not send messages to friends or groups without explicit authorization.
- [ ] Verify source links target the deployed platform revision; no explanation claims
  Valkey balance caching, Valkey trip locks, universal three-store plugin use, or replacement
  of existing FX snapshots. No production expense writes are needed for this smoke check.
- [ ] Record timestamp in Asia/Singapore, both repository SHAs, release tag, deploy result,
  health and completed/pending smoke checks in the private deployment record. Update its
  plan index and the public plan's platform-only status without publishing private evidence.

### Task 12 — Publish generated wiki pages and verify recovery

- [ ] Generate pages using the verified release tag and review them against the packaged
  documents. Clone/pull the existing public wiki, preserving unrelated edits. For example,
  from the reviewed platform release checkout:

```sh
WIKI_WORKDIR=$(mktemp -d /tmp/clawdot-explainers-wiki.XXXXXX)
git clone https://github.com/keenlok/clawdot-platform.wiki.git "$WIKI_WORKDIR"
uv run python .github/scripts/generate_explainers.py --source-ref v0.2.0 --output-dir "$WIKI_WORKDIR"
git -C "$WIKI_WORKDIR" diff --check
git -C "$WIKI_WORKDIR" status --short
```

- [ ] Review only the managed generated pages and add one navigation link without
  replacing Home/sidebar content. Commit/push the wiki change only after reviewing the
  exact diff. Open the public pages and verify relative/source links, section formatting,
  Unicode examples and selected release identity. Wiki publication is separately verified;
  a platform merge alone does not publish it.
- [ ] Preserve the existing previous healthy private release. If health or behavior fails,
  set the existing autodeploy kill switch first, then use the committed compatible
  `shared/deploy.sh rollback` from a clean private checkout. Verify health and the restored
  source pin. Do not restore the database for this documentation/API-only feature.

```sh
ssh "$DEPLOY_HOST" 'touch /home/ubuntu/sb_bot/AUTODEPLOY_DISABLED'
bash shared/deploy.sh rollback --dry-run
bash shared/deploy.sh rollback
```

- [ ] Revert/fix the private main change before removing the kill switch so the poller
  cannot immediately reinstall a bad release. Published platform tags remain immutable.
  If wiki content was wrong, regenerate from the corrected or restored release rather
  than silently retaining false claims. Record recovery actions only if actually exercised.

## 5. Completion and evidence checklist

- [ ] Every built-in capability plugin owns one packaged, reviewed explainer; third-party
  plugins without one remain compatible.
- [ ] `/explain` works without a provider, honors access/addressing/topics, and never calls
  operational mutation handlers. Natural-language questions retrieve only relevant sections.
- [ ] Fresh availability facts and exact source identity accompany documentation without
  private configuration or cross-chat disclosure. All skill modes and installed wheels pass.
- [ ] Generated wiki content comes from the same source; drift checks and release links pass.
- [ ] Platform feature PR reviewed/merged; version-bump PR reviewed/merged; immutable release
  tag matches CI's exact merge SHA and dated changelog.
- [ ] Private candidate and pinned tests/builds reviewed/passed; all platform sources and lock
  point to the same release; private delivery PR reviewed/merged.
- [ ] Production health and source pin verified; live conversational checks are completed or
  explicitly pending owner execution. Wiki publication and public links are checked separately.
- [ ] Both plan indexes and private deployment record reflect what actually shipped. Final
  report distinguishes built, released, pinned, deployed, conversationally verified and
  published; it does not collapse those states into a generic success claim.

## Planning self-review

The plan covers the agreed document ownership, general and per-plugin explanations,
on-demand natural-language retrieval, single-source wiki reuse and exact deployed-source
identity. It includes the full implementation → review → feature merge → version-bump
merge/tag → downstream pin/merge → deployment → verification chain and recovery route.
The separate wiki publication and no-authorized-live-message cases are explicit. Private
release/control/secrets changes are not smuggled into the public feature. Version and
repository baselines are recorded as observations to refresh, not assumptions about future
main. This planning change alters documentation only.

## Implementation validation (2026-11-04)

The owner approved implementation and coordinated delivery. Core/parser/command tests
failed before implementation. Source-attribution, hint-rendering and inline-code review
findings were reproduced and fixed with regressions. Independent Codex review and final
review found no remaining actionable correctness/security issues. Claude remained signed out.

The platform suite passed 3,637 tests with two optional Elasticsearch skips, 26 browser/sandbox
deselections and 14 existing Telegram deprecation warnings, using a disposable Postgres database.
Locked sync, whole-tree Ruff lint, formatting of all changed Python files, generated wiki drift,
plan/link checks, diff checks and Gitleaks passed. Full-tree formatting still reports the same
14 unchanged historical Markdown records; none were reformatted. All 13 documented package
wheels include their resource. Outside-checkout scaffold and SBot installed-wheel smokes passed;
the scaffold container built and passed a network-disabled read-only smoke including /explain.
The private candidate full suite passed 410 tests, seven optional skips and one deselection.

These are implementation/candidate results, not evidence of a published release or production
rollout. Feature/release PRs, immutable tag, downstream pin, deployment and wiki publication
are recorded when each is actually completed.
