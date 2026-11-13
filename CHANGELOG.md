# Changelog

Notable changes for platform users and application maintainers.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.15.2] - 2026-11-14

### Fixed

- Keep inviter tool metadata within the application description budget.

## [0.15.1] - 2026-11-14

### Fixed

- Preserve owner-DM context while filtering skill tools, including natural-language inviter batches.
- Exercise explicitly enabled skills routing in inviter regression tests.

## [0.15.0] - 2026-11-13

### Added

- Natural-language, owner-only DM management of trusted inviters. Request several people by
  exact known name or @username and confirm one preview; up to 20 trust/untrust changes commit
  atomically with per-person audit entries. Unknown/ambiguous people require clarification.
  This grants no other role or content access; existing commands and group-denial precedence
  remain unchanged. No new migration or dependency is required.
- Optional `Tool.context_allowed` predicate and context-aware registry offering/calling keep
  narrowly restricted tools out of unauthorized requests in both legacy and skills routing.
  Existing tools without a predicate behave as before; guarded tools require a ToolContext.

## [0.14.0] - 2026-11-13

### Fixed

- Replies to generated photos can use that exact version for further edits on receipt-capable
  platforms (direct Telegram). Platform file references and requester attribution survive restarts;
  group ownership checks and privacy erasure apply. Apply the additive core migration
  `0012_output_photo_attribution.sql`. Existing photo delivery APIs remain compatible; older
  untracked outputs and bridge document delivery require uploading the image again.


## [0.13.0] - 2026-11-13

### Added

- Owner-confirmed trusted inviters can admit new Telegram groups without per-group approval
  through `/admin trust` and `/admin untrust`; `/admin trusted` inspects grants. Owners are
  implicitly trusted. Admission is persistent and audited, rechecks active restrictions, and
  grants no other role or DM content access. Untrust preserves existing enabled groups.
- Owner group blocks via `/admin blockgroup` and `/admin unblockgroup`. Blocks and existing
  disabled overrides win over invitations and bootstrap enables. Unblocking keeps a group
  disabled until explicitly enabled. `/admin groups` shows owner-visible admission provenance.
- Additive core migration `0011_trusted_group_invites.sql` stores grants, blocks, admission
  provenance and join replay claims. Apply migrations before serving events. Existing group
  state is preserved; owners can now admit unknown groups by inviting the bot. Direct Telegram
  transport supports free onboarding; the protected bridge retains its independent fixed
  transport allowlist. Neither model tools nor broker permissions are expanded.

## [0.12.1] - 2026-11-13

### Fixed

- Photo edits: with `IMAGE_EDIT_ENABLED` on, models called `generate_image` without `reference`
  and generated an unrelated picture, because the tool text described text-only generation. The
  editing-on tool description, the `reference` description (photo numbering) and the edit skill
  instructions now tell the model to pass the photo. Text with editing off is unchanged. No
  configuration change; `evals/image_reference/run_eval.py` measures the live rate.

## [0.12.0] - 2026-11-12

### Added

- Image references enable photo edits through `generate_image(reference=...)` when
  `IMAGE_EDIT_ENABLED` is true (default off). `IMAGE_EDIT_PRIVACY_NOTE` is exported for app privacy
  notices.

### Changed

- Core `TurnState` records the sender key for each current or replied-to image attachment.

## [0.11.0] - 2026-11-12

### Added

- Optional `chatcore-cards` plugin (disabled by default; configure `CARDS_*` settings) sends
  validated summary cards through the local renderer. Expense balance and trip summary card tools
  are available only when an app supplies a card renderer client.
- Core `chatcore.cards` validates and renders structured cards through the local renderer's `/card`
  endpoint, with `CARD_*` page, byte and timeout settings. `PhotoAlbumPlatform` adds Telegram
  `send_photos`. `Skill.max_output_tokens` and `AgentSettings.max_tokens_ceiling` allow a skill to
  raise per-step output limits within the configured ceiling.

### Changed

- Telegram renders blockquotes as native entities and aligns narrow Markdown tables. Direct photo
  uploads now require readable dimensions within Telegram's size and aspect-ratio limits.

## [0.10.0] - 2026-11-12

### Changed

- Group prompts attach the current speaker to the message being answered, identify whose bot
  reply is quoted, and mark when someone joins another member's conversation. Conversation
  summaries now use third-person internal notes, and nickname memories include the subject's
  user key. Historical records render Unicode directly while escaping invisible controls.
  Downstream request snapshots must be refreshed.

## [0.9.0] - 2026-11-11

### Added

- New optional `chatcore-image` plugin (`ImagePlugin`, `ImageSettings`), disabled by default.
  Configure `IMAGE_ENABLED`, `IMAGE_OPENAI_API_KEY`, `IMAGE_OPENAI_BASE_URL`, `IMAGE_MODEL`,
  `IMAGE_QUALITY`, `IMAGE_MODERATION`, `IMAGE_MAX_PROMPT_CHARS`, `IMAGE_MAX_PER_USER_DAY` and
  `IMAGE_MAX_PER_DAY` to enable it. Core adds the `PhotoPlatform` capability and Telegram adds
  inline `send_photo`; bridge mode sends generated images as documents.

## [0.8.2] - 2026-11-11

### Added

- Voice narration supports **Cantonese**. Add `Cantonese=<voice_id>` to `VOICE_TTS_VOICES` to
  offer it. Cantonese is always synthesised with `eleven_v4` and language code `yue` (the
  configurable v2 models have no Cantonese); other languages keep `VOICE_TTS_MODEL`.
  `eleven_v4` joins the model allowlist at 1 credit per character.
- The `read_aloud` skill routes Cantonese requests (`讀出嚟`, `念出嚟`, `唸出嚟`) and its
  instructions ask for colloquial Cantonese in traditional characters. Apps that test their
  resulting skill prompt should refresh that expectation.

## [0.8.1] - 2026-11-11

### Fixed

- Telegram voice delivery no longer rejects voice notes that aren't named
  `pronunciation.mp3`. `TelegramPlatform.send_voice` now accepts any plain lowercase `.mp3`
  name (letters, digits, `_` and `-`, at most 68 characters), so `narrate_text` voice notes
  (`narration.mp3`) are delivered instead of failing after synthesis. Path-like or
  non-MP3 names are still refused before any upload.
- The Telegram voice size cap rises from 2 MiB to 8 MiB to match the voice plugin's audio
  cap, so long narrations within `VOICE_TTS_MAX_CHARS_PER_REQUEST` are not rejected after
  being billed. Tutor audio (2 MiB cap) is unaffected.

## [0.8.0] - 2026-11-11

### Added

- New read-only expenses tool **`member_expenses`** in the `expenses` skill. For one member
  of a trip or the general ledger it lists every expense they paid or share, with their
  share (converted in convert trips), their settlements, and paid/share/net totals. The net
  is the same figure `get_balances` reports. It uses the same chat resolution and gate as
  `list_expenses`.

### Changed

- `list_expenses` rows now end with who shares each expense, for example
  `(paid by Alice; split: Alice, Bob)`, or per-person amounts for uneven splits. The `#id`
  prefix is unchanged.
- Expense and trip outputs (trip `Members:` lines, `list_expenses`, add/update echoes) add
  `(@username)` to members whose display names share a 3+ character prefix, so similar names
  read as different people. Other names are unchanged.
- The `expenses` skill instructions are reworded to fit three new rules: similar names are
  different people, who shares an expense comes from `list_expenses`, and one person's
  balance is explained with `member_expenses`. Apps that test their resulting skill prompt
  should refresh those expectations.
- Core's system prompt gains one fixed sentence: the speaker of the latest user message is
  who "you" means in the reply. Apps with agent request snapshots will see that sentence added.

## [0.7.0] - 2026-11-09

### Added

- A maintainer-facing pre-commit check catches real-looking Telegram identifiers and optional
  personal denylist terms in staged additions and commit messages without printing matched text.
  Re-run `uv run pre-commit install` to install the new `commit-msg` hook; keep personal terms in
  `~/.config/clawdot/private-denylist.txt` (or `$CLAWDOT_PRIVATE_DENYLIST`), outside the repository.
- New optional **`chatcore-voice`** plugin (`VoicePlugin`, `VoiceSettings`). It reads a passage
  the bot wrote aloud as one voice message, such as a narrated news summary, through the
  `narrate_text` tool and a `read_aloud` skill. It doesn't depend on tutor mode. It is disabled
  by default; enable it with `VOICE_TTS_ENABLED=true`, a dedicated `VOICE_ELEVENLABS_API_KEY`
  and `VOICE_TTS_VOICES` (`English=<voice id>,…`).
- Before each narration the plugin checks the ElevenLabs subscription usage
  (`GET /v1/user/subscription`). It refuses when the account is inactive, when the remaining
  allowance would drop below `VOICE_TTS_PROVIDER_RESERVE_CHARS`, or when overage billing is
  possible and `VOICE_TTS_ALLOW_OVERAGE` is false.
  - `VOICE_TTS_USAGE_CHECK=required` (the default) stops narration whenever usage can't be
    read, so the key needs user-read permission.
  - Local caps: `VOICE_TTS_MAX_CHARS_PER_REQUEST` (3,000), `VOICE_TTS_MAX_CHARS_PER_USER_HOUR`
    (10,000) and `VOICE_TTS_MAX_CHARS_PER_DAY` (50,000).
  - Admins can run `/voice usage` to see local and provider usage.
- `chatcore.tts`: a shared ElevenLabs speech request, MP3 validation and subscription-usage reader.
### Changed

- The tutor's pronunciation speech now goes through `chatcore.tts`. Behaviour, limits, request
  shape and log messages are unchanged.
## [0.6.2] - 2026-11-09

### Changed

- Expense echoes now name the people sharing a split and describe participant or amount changes.
  Expense and trip tools also tell the model to pass member names as written.

## [0.6.1] - 2026-11-09

### Fixed

- Telegram attachment downloads now use a configurable 30-second total timeout per download
  invocation, including retries, and pass the same timeout to both Bot API download stages.
  Set `TELEGRAM_DOWNLOAD_TIMEOUT_S` to a value greater than 0 and at most 120 seconds to adjust it.

## [0.6.0] - 2026-11-09

### Changed

- Memory facts now show who they are about and when they were saved, in the agent's context, `recall`
  and `/memory`. The new `MEMORY_CONTEXT_MAX_CHARS` setting (default 5000) bounds the injected section.
- **Behaviour change:** similar wording no longer overwrites a saved memory. Automatic extraction
  never changes an existing fact. An explicit save beside a similar fact inserts a new one and names
  the similar fact, and `remember` takes an optional `replaces` memory ID for atomic corrections.
- Automatic extraction skips bot-generated prompts, treats message bodies as untrusted data, and
  drops third-party claims that it can't link to a verified mention.
- Migration `0002_memory_control.sql` adds the `memory_control` table. No existing rows change.
- **Behaviour change:** `/forgetme` and `/purgechat` now run through a durable journal. A failed step
  is retried automatically with backoff (5 minutes up to 1 hour), including after a restart, and the
  reply says when deletion is incomplete. Core messages are now deleted only after plugins that read
  them to find purge targets have succeeded; other plugin failures no longer hold them back.
- **Behaviour change:** `/forgetme` deletes memories the person is the subject of, contributed context
  to, or is the only stated support for. A fact another person independently stated stays, without
  this person's links. Surviving facts that contain their name as a whole word are listed for manual
  `/forget`, and are not deleted by name match.
- Memory writes are fenced per chat. A save that started before `/forget`, `/forgetme` or `/purgechat`
  is rejected at commit and must be requested again. While an erasure runs, saving is paused and
  extraction is skipped. A pending save created before this release must be requested again.
  Erasures in one chat run in request order; a new request waits behind any older active or queued one.
- Memory search deletions are now durable: a failed Elasticsearch delete stays queued and is retried,
  and the reply reports pending cleanup. This includes a document indexed after its fact was erased.

### Upgrade notes

- Migrations `0010_privacy_erasures.sql` (core) and `0003_memory_sources.sql` (memory) are additive.
  They add `privacy_erasures`, `memory_sources`, `memory_index_work` and `memories.provenance`.
  Existing facts are marked `legacy_unknown`. Source author keys and message IDs stay after a message
  body expires, so erasure still finds them; operational journal rows hold no message text.
- Custom `SearchIndex` implementations must accept `delete(..., refresh_first=False)` and a `terms`
  query on `memory_id`. Custom `CoreRepo` implementations must add `create_erasure`, `list_erasures`,
  `update_erasure`, `delete_erasure` and `promote_erasure`.
- Plugins whose purge reads the message log to find targets must set `purge_reads_messages = True`.

### Added

- New memory facts record bounded links to the human messages behind them (up to 32 per fact), taken
  only from trusted turn data, and never from model arguments.
- Admin-only `/erasures` lists outstanding erasures. `/erasures retry <id>` forces a retry and
  `/erasures release <id>` unblocks memory writes once the memory purge step has succeeded.
- `BasePlugin` gains optional `begin_erasure`, `finish_erasure`, `release_erasure`, `erasure_notes`
  and `pending_cleanup` hooks, plus `purge_reads_messages` and `erasure_blocks_writes` attributes.

- Memory extraction now records daily counters for attempts, skips, failures and index-write
  failures, with no message content. Admins can read them with `/memorystats`, which also lists the
  current chat's automatic memories whose wording was later overwritten. The audit changes nothing.

## [0.5.6] - 2026-11-07

### Changed

- Telegram voice-delivery failures now log a bounded, allowlisted reason category, so live tutor
  TTS failures can be traced without logging Bot API descriptions or caption contents.

## [0.5.5] - 2026-11-07

### Fixed

- Google Doc edit verification resolves provable inherited text styles, avoiding false
  formatting failures when Google normalizes matching explicit styles. Exact saved text
  with unverified formatting is reported separately, without replaying the edit; sharing
  and current-itinerary promotion still require complete verification.

## [0.5.4] - 2026-11-06

### Fixed

- Tutor pronunciation routing recognizes the colloquial request “sound please” even when the
  previous message was not routed to the tutor skill.

## [0.5.3] - 2026-11-06

### Fixed

- Tutor pronunciation routing recognizes common voice, audio and sound-clip requests in English,
  Cantonese, Mandarin Chinese and Japanese, even when embedding scores are below the route threshold.

## [0.5.2] - 2026-11-05

### Added

- `trip_edit_doc` resolves a named trip's saved primary Doc, edits native Docs in place, and freezes
  current-itinerary advancement for verified conversion/fallback copies or explicitly selected copies.
  Generic edits block implicit writes to saved historical trip references. Confirmed Doc attachments
  can cite a retained human source message.

## [0.5.1] - 2026-11-05

### Fixed

- Tutor pronunciation now selects `eleven_v4` for Cantonese, whose `yue` language code is not
  supported by v3; Mandarin Chinese (`zh`) and Japanese (`ja`) continue to use `eleven_v3`.

## [0.5.0] - 2026-11-04

### Added

- Journaled native Google Doc outputs can be reused in their exact originating chat/topic after
  current owner and permission checks; legacy, purged, ambiguous and mismatched outputs do not
  confer authority. Bounded read-back verification gates both viewer and editor sharing.
- Preview-frozen private/viewer/editor access choices for new Docs, with framework default private
  and compatibility for the prior viewer boolean. Additive migration `0005_output_authority.sql`
  stores the owner/chat/topic binding, including private outputs.

### Fixed

- Post-edit verification retries only bounded reads, keeps exact text and target-style checks,
  and reports the failed stage without replaying mutations or claiming an unverified edit saved.
- Native Doc edits accept a retained human source-message reference, while recorded output
  authority survives confirmation cleanup and stays scoped to the original owner/chat/topic.

## [0.4.0] - 2026-11-04

### Added

- Optional, preview-frozen link-viewer access for newly created or copied native Google Docs.
  Sharing is limited to anyone/reader with search discovery disabled, occurs after content read-back,
  and is journaled with known partial or uncertain outcomes. The framework default remains private.
- Additive Google migration `0004_create_doc_results.sql` extends the existing result journal
  for newly created Docs while keeping source IDs required for all existing edit kinds.

## [0.3.0] - 2026-11-04

### Added

- Optional tutor pronunciation clips for direct Telegram: explicit Cantonese, Mandarin Chinese or
  Japanese MP3 synthesis, process-local usage limits, optional FFmpeg slowdown and voice-message
  delivery. TTS stays disabled by default. Activation requires live provider/account and linguistic
  qualification, a restricted key and downstream FFmpeg installation for slow speech. No migration
  or Python dependency was added.

## [0.2.0] - 2026-11-04

### Added

- Apache License 2.0 (`LICENSE`), with `license = "Apache-2.0"` declared in every package.
- `/explain [topic] [section]` and read-only `read_explainer` retrieve maintained, packaged
  architecture/plugin documentation with fresh scoped tool availability and optional exact
  source links. Each built-in plugin owns its `EXPLAIN.md`; third-party plugins can opt in
  through `BasePlugin.explainer_package`. The same sources generate wiki pages, checked for drift.
  Retrieved documentation is untrusted evidence, so a subsequent write in the same turn can
  require confirmation. No new dependency, service or migration is required.

### Changed

- Links to the private history and deployments repositories are now named, not linked,
  so public readers no longer reach 404 pages.

## [0.1.3] - 2026-11-03

### Fixed

- OpenAI-compatible native tool calls with `finish_reason=stop` now reach the normal tool flow.
  A bounded regeneration can recover a truncated native call without executing its incomplete
  payload. Source-derived Google Doc revisions use original-package import instead of rebuilding
  from plain text; broader rewrites must be explained.

## [0.1.2] - 2026-11-03

### Added

- Confirmed bounded text edits in native Google Docs and literal range edits in Sheets,
  with frozen destinations, content preconditions and read-back verification. Docs use
  revision-checked UTF-16 body ranges; Sheets previews disclose the collaborator race.
- Qualified native copy fallback and DOCX edit copies imported from the original package
  into a new Google Doc. `GOOGLE_COPY_QUALIFIED=false` keeps copying/conversion disabled
  until separately authorized owner-grant checks pass; originals use existing API scopes.
- Additive Google migration `0003_edit_results.sql` journals destination IDs and partial or
  uncertain outcomes. Unresolved reservations survive normal cleanup for at least
  `GOOGLE_EDIT_UNRESOLVED_RETENTION_DAYS` (default 90); privacy deletion still removes them.
- Optional Google Docs browser trial broker and dedicated editor-account worker for exact
  replacement and bold operations on new native copies. The worker is isolated behind the
  guarded proxy; the broker shares only the confirmed copy and verifies the saved result through
  the Google API. `GOOGLE_BROWSER_ENABLED=false` and copy qualification remain disabled by
  default. The trial remains unqualified and undeployed; no live Google edits
  have been run.
- Shared egress proxy primitives now have a renderer-independent core location, preserving the
  public renderer's compatibility surface. The proxy extraction is under [PR #6](https://github.com/keenlok/clawdot-platform/pull/6).

### Changed

- Google browser trial instructions state that API editing is the default, browser editing
  requires an explicit request and confirmation, and an API failure never triggers browser fallback.
- Consolidated plans in `docs/plans/` and handovers in `docs/handovers/`, with updated
  references and repository-wide local link checks.
- Downstream applications adopting 0.1.2 must update their exact platform source pin, lock,
  configuration and migration manifest together; the normal migration path applies Google
  migration `0003`. Keep `GOOGLE_COPY_QUALIFIED=false` and `GOOGLE_BROWSER_ENABLED=false` until
  the separate owner-grant and browser qualification checks pass. This release does not enable
  either feature.

## [0.1.1] - 2026-11-03

### Added

- Reusable core, console/Telegram adapters, capability plugins and standalone browser
  renderer; release-control code is included but inactive.
- Working generic scaffold with CI tests, installed-wheel checks and a pinned container build.
- CI-gated immutable Git release tags for explicitly pinned downstream applications.

[Unreleased]: https://github.com/keenlok/clawdot-platform/compare/v0.15.0...HEAD
[0.15.0]: https://github.com/keenlok/clawdot-platform/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/keenlok/clawdot-platform/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/keenlok/clawdot-platform/compare/v0.12.1...v0.13.0
[0.12.1]: https://github.com/keenlok/clawdot-platform/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/keenlok/clawdot-platform/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/keenlok/clawdot-platform/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/keenlok/clawdot-platform/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/keenlok/clawdot-platform/compare/v0.8.2...v0.9.0
[0.8.2]: https://github.com/keenlok/clawdot-platform/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/keenlok/clawdot-platform/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/keenlok/clawdot-platform/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/keenlok/clawdot-platform/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/keenlok/clawdot-platform/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/keenlok/clawdot-platform/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/keenlok/clawdot-platform/compare/v0.5.6...v0.6.0
[0.5.6]: https://github.com/keenlok/clawdot-platform/compare/v0.5.5...v0.5.6
[0.5.5]: https://github.com/keenlok/clawdot-platform/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/keenlok/clawdot-platform/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/keenlok/clawdot-platform/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/keenlok/clawdot-platform/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/keenlok/clawdot-platform/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/keenlok/clawdot-platform/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/keenlok/clawdot-platform/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/keenlok/clawdot-platform/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/keenlok/clawdot-platform/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/keenlok/clawdot-platform/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/keenlok/clawdot-platform/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/keenlok/clawdot-platform/tree/v0.1.1
