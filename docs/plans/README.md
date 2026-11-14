# Plans index

| Plan | Status | Notes |
| --- | --- | --- |
| [Image follow-up routing and capability claims](2026-11-14-image-routing-capability-claims.md) | Shipped inactive | [PR #48](https://github.com/keenlok/clawdot-platform/pull/48), v0.17.0: complete gated skill discovery, evidence-based availability guidance and contextual image routing. All five review findings fixed; Claude executed, Codex reviewed, Claude final review. 4587 tests pass with disposable Postgres; downstream pin and runtime verification follow separately. Photo policy unchanged; real-model qualification not run |
| [Shared group photo edits](2026-11-14-shared-photo-edits.md) | Shipped inactive | v0.16.0 (this PR), editing off by default: owner-approved explicit same-group photo replies may be edited by any member; scoped provenance rejects indirect history |
| [Natural-language invitation permissions](2026-11-13-natural-inviter-batches.md) | Live limited | v0.15.2: owner-DM batch name resolution and one confirmed natural-language action for trusted inviters; protected bridge boundary unchanged |
| [Replying to generated photos](2026-11-13-image-reply-handoff.md) | Shipped inactive | v0.14.0 (this PR), default editing off: persist outgoing photo receipts with requester attribution and resolve exact-photo replies for successive edits; preserve group ownership checks |
| [Trusted group invitations](2026-11-13-trusted-group-invites.md) | Live limited | v0.13.0: trusted invitations, owner group blocks, persistent audited admission in direct transport; protected bridge transport stays fixed |
| [Formatted HTML output execution](2026-11-12-formatted-html-output-execution.md) | Record | Codex execution plan in two runs (A: Telegram text baseline, core cards, renderer `/card`, albums, output budget; B: `cards` plugin and expense cards): `gpt-6-luna` executes, `gpt-6.1-sol` reviews, Claude final review |
| [Formatted HTML output (summary cards)](2026-11-11-formatted-html-output.md) | Shipped inactive | [PR #33](https://github.com/keenlok/clawdot-platform/pull/33), released in v0.11.0, disabled by default; qualification pending. Fixed-template HTML cards rendered to PNG by `browser-renderer` (new no-JS, no-network `/card` endpoint) and sent as Telegram photos with a short text reply; Telegram text baseline (native blockquotes, aligned narrow tables); `cards` plugin with `send_card` (8,000-character cards, multi-page albums, skill-scoped output budget); reuses `PhotoPlatform` from image generation; ledger-backed expense balance cards |
| [Group speaker attribution](2026-11-11-group-speaker-attribution.md) | Live limited | [PR #36](https://github.com/keenlok/clawdot-platform/pull/36), released in v0.10.0; live eval not run (owner shipped without it). Follow-up to expense speaker context (v0.8.0): the bot still addressed the wrong member. Put the speaker on the answered message, say whom a quoted bot reply answered, mark handovers, third-person summaries, keyed nickname memories, rename the bot's "(you)" label, readable Unicode records; prompt tests plus a live eval |
| [Image generation](2026-11-11-image-generation.md) | Shipped inactive | [PR #34](https://github.com/keenlok/clawdot-platform/pull/34), released in v0.9.0, disabled by default; activation gate not run. An `image` plugin with `generate_image` on the OpenAI Images API (default `gpt-image-2.5-flare`), delivered as an inline photo through a new core `PhotoPlatform` capability (direct Telegram; document fallback in bridge mode), local per-user and daily caps plus a dedicated OpenAI project budget |
| [Image generation execution](2026-11-11-image-generation-execution.md) | Record | Codex execution plan for image generation: `gpt-6-luna` executes, `gpt-6.1-sol` reviews, Claude does the final review |
| [Image reference photos](2026-11-12-image-reference-photos.md) | Shipped inactive | Follow-up to image generation: optional `reference` on `generate_image` sends a photo the user sent or replied to through OpenAI `images.edit`, behind `IMAGE_EDIT_ENABLED` (default off); approved with default answers; implemented (Luna executed, Sol reviewed, two fix rounds, Claude final review), [PR #39](https://github.com/keenlok/clawdot-platform/pull/39), released in v0.12.0; edit activation gate not run |
| [Image reference photos execution](2026-11-12-image-reference-photos-execution.md) | Record | Codex execution plan for reference photos: `gpt-6-luna` executes, `gpt-6.1-sol` reviews, Claude does the final review |
| [Image edit reference fix](2026-11-12-image-edit-reference-fix.md) | Shipped inactive | Bug fix for v0.12.0 reference photos: live sb_bot never passed `reference` because the tool text described text-only generation; editing-on tool description, `reference` numbering and directive edit instructions, agent-level regression tests and a live eval; [PR #40](https://github.com/keenlok/clawdot-platform/pull/40), released in v0.12.1; live re-test pending |
| [Expense speaker context](2026-11-10-expense-speaker-context.md) | Live limited | [PR #30](https://github.com/keenlok/clawdot-platform/pull/30), released in v0.8.0. Follow-up to expense split names: list_expenses shows participants, new read-only member_expenses tool for one person's history, similar names get handles, core states who "you" is |
| [Private identifier pre-commit check](2026-11-09-private-identifier-hook.md) | Live limited | [PR #27](https://github.com/keenlok/clawdot-platform/pull/27): pre-commit and commit-msg check for real-looking Telegram IDs/links plus an optional personal denylist kept outside the repo; each maintainer opts in with `uv run pre-commit install`; not run in CI |
| [Voice narration](2026-11-09-voice-narration.md) | Live limited | [PR #28](https://github.com/keenlok/clawdot-platform/pull/28), released in v0.7.0 and enabled for sb_bot 2026-11-11; Telegram delivery fix in v0.8.1; Cantonese (Eleven v4) in v0.8.2 (live narration not yet confirmed). A `voice` plugin with `narrate_text` that works outside tutor mode, shared ElevenLabs client in core, ElevenLabs subscription-usage preflight, news narration through the `read_aloud` skill |
| [Expense split names](2026-11-09-expense-split-names.md) | Live limited | [PR #25](https://github.com/keenlok/clawdot-platform/pull/25), released in v0.6.2: echo who shares each split and what changed on update; keep member names as the user wrote them (2026-11-09 wrong-member split incident) |
| [Google Doc verification outcomes and inherited styles](2026-11-06-google-doc-verification-outcomes.md) | Live limited | [PR #20](https://github.com/keenlok/clawdot-platform/pull/20): v0.5.5 separates verified text from formatting warnings and resolves provable inheritance; reviewed implementation and downstream receipt on the associated PR |
| [Current trip itinerary execution](2026-11-05-current-trip-itinerary-execution.md) | Live limited | [PR #17](https://github.com/keenlok/clawdot-platform/pull/17): v0.5.2 runtime and review evidence; downstream rollout receipt tracked on PR |
| [Current trip itinerary targeting](2026-11-05-current-trip-itinerary.md) | Live limited | [PR #17](https://github.com/keenlok/clawdot-platform/pull/17): owner-approved same-Doc edits and confirmed current-itinerary advancement; requires downstream release pin |
| [Google output follow-ups execution](2026-11-04-google-output-followups-execution.md) | Live limited | [PR #16](https://github.com/keenlok/clawdot-platform/pull/16): v0.5.0 implementation, Luna execution/Sol review; release and downstream installed receipts on PR |
| [Google output follow-ups and access levels](2026-11-04-google-output-followups-and-access.md) | Live limited | [PR #16](https://github.com/keenlok/clawdot-platform/pull/16): v0.5.0 delivers owner-approved viewer default/editor on request, durable own-output authority and strict bounded read-backs; actual receipt on PR |
| [New output Doc link viewers](2026-11-04-google-output-link-viewers.md) | Live limited | [PR #15](https://github.com/keenlok/clawdot-platform/pull/15): v0.4.0 release delivers preview-frozen access for new output Docs; framework default stays private; source/Sheet/browser policies unchanged; downstream rollout tracked on PR |

Every plan, design and review record in the repository, with its current status. The
architecture spec is [`ARCHITECTURE.md`](../../ARCHITECTURE.md); the rules for changing the repo are
in [`AGENTS.md`](../../AGENTS.md). **A PR that adds a plan or changes what a plan has delivered
updates its row here in the same PR.**

Last checked: 2026-11-03. All plans share this folder; handovers live in
[`docs/handovers/`](../handovers/), and design specs remain in `docs/superpowers/specs/`.

| Repair plan | Status | Notes |
| --- | --- | --- |
| [Google retry and source-layout repair](2026-11-03-google-retry-and-source-layout.md) | Live | [PR #9](https://github.com/keenlok/clawdot-platform/pull/9): merged repair; native stop calls and bounded truncation recovery; source edits use original-package conversion/copy; 3,579 tests passed; v0.1.3 release and coordinated downstream delivery |

References written `history #N` or `history PR #N` are pull requests in the private
pre-publication repository `clawdot-platform-history`; paths written
`clawdot-deployments/...` are in the private deployments repository. Neither is public, so
they are named rather than linked.

## Status key

| Status | Meaning |
| --- | --- |
| **Live** | Implemented platform capability. Downstream apps select a pinned platform release and configure the tools they enable; platform merges do not deploy private bots. |
| **Live, limited** | Implemented with limited or opt-in activation in the original downstream app; deployment state is tracked privately. |
| **Shipped, inactive** | The code is on `main`, but the feature is disabled or the service is not installed in the original downstream app. |
| **Planned** | Not built. |
| **Record** | A review, verification, handover or rules document, not a plan to build. |

PR `—` means the work was committed straight to `main` before the repo used pull requests.
The owner selected a fresh public history; earlier development/PR records remain in
`clawdot-platform-history`. Historical notes refer to pre-split application deployments. "Deploy record" links resolve to
the private deployment repository. Paths such as `apps/sb_bot` in older framework plans describe
the original workspace; that app now belongs in private `bots/sb_bot`.

## Foundation (phases 0–2)

| Plan | Status | PR | Notes |
| --- | --- | --- | --- |
| [Storage architecture explainer](2026-11-03-storage-architecture-explainer.md) | Record | [#2](https://github.com/keenlok/clawdot-platform/pull/2) | Documentation, visual overview, wiki source and publishing instructions |
| [COMMON.md](COMMON.md) | Record | — | Rules every Phase 1 track followed |
| [track-a.md](track-a.md) Core runtime | Live | — | Phase 1 |
| [track-b.md](track-b.md) Web, stocks, SSRF guard | Live | — | Phase 1 |
| [track-c.md](track-c.md) Attachments, history search, embeddings | Live | — | Phase 1 |
| [track-d.md](track-d.md) Expenses, trips, FX | Live | — | Phase 1 |
| [track-e.md](track-e.md) Telegram adapter | Live | — | Phase 1 |
| [phase2-memory.md](phase2-memory.md) | Live | — | Phase 2 (`ARCHITECTURE.md` §7d) |
| [phase2-privacy.md](phase2-privacy.md) | Live | — | Phase 2 |
| [phase2-wiring.md](phase2-wiring.md) | Live | — | Phase 2 |
| [phase2-deploy.md](phase2-deploy.md) | Record | — | Original operator record preserved in private archive; platform stub retains references |
| [memory-json-fix.md](memory-json-fix.md) | Live | history #1 | |
| [Memory reliability and evidence](2026-11-05-memory-improvements.md) | Live limited | [PR #22](https://github.com/keenlok/clawdot-platform/pull/22): v0.6.0 | Slices 0–2a only, approved 2026-11-06; slices 1 and 2a must ship together; 2b, 2c, 3 and 4 are not built and need separate approval; live-model measures remain unmeasured |
| [Memory framework comparison](2026-11-05-memory-framework-comparison.md) | Record | — | Research behind the memory reliability plan |
| [Memory reliability slice 0 baseline](2026-11-05-memory-baseline.md) | Record | — | Synthetic and scripted behavioral baseline; no production quality run |
| [telegram-formatting.md](telegram-formatting.md) | Live | — | |

## Conversation, prompts and access

| Plan | Status | PR | Notes |
| --- | --- | --- | --- |
| [conversation-prompt-review.md](conversation-prompt-review.md) | Live | — | |
| [group-mention-only.md](group-mention-only.md) | Live | — | |
| [group-conversation-flow.md](group-conversation-flow.md) | Live | — | Deploy record: group-flow (`clawdot-deployments/docs/deployments/2026-10-31-group-flow.md`) |
| [2026-10-31-reply-chain-attribution.md](2026-10-31-reply-chain-attribution.md) | Live | — | `ARCHITECTURE.md` §19 |
| [access-restrictions.md](access-restrictions.md) | Live | — | Deploy record: access-research (`clawdot-deployments/docs/deployments/2026-10-31-access-research.md`) |
| [context-reset.md](context-reset.md) | Live | — | Deploy record: access-research (`clawdot-deployments/docs/deployments/2026-10-31-access-research.md`) |
| [budget-admin.md](budget-admin.md) | Live | history #5 | Deploy record: budget-dsml-image (`clawdot-deployments/docs/deployments/2026-10-31-budget-dsml-image.md`) |
| [dsml-leak-fix.md](dsml-leak-fix.md) | Live | history #6 | Same deploy record |
| [image-read-fix.md](image-read-fix.md) | Live | history #7 | Same deploy record |
| [image-vision plan](2026-10-31-image-vision.md), [design](../superpowers/specs/2026-10-31-image-vision-design.md) | Live | — | Deploy record: image-vision (`clawdot-deployments/docs/deployments/2026-10-31-image-vision.md`) |
| [display-timezones plan](2026-10-31-display-timezones.md), [design](../superpowers/specs/2026-10-31-display-timezones-design.md), [handover](../handovers/display-timezones.md) | Live | history #2 | |
| [prompt-timezone-settings plan](2026-10-31-prompt-timezone-settings.md), [design](../superpowers/specs/2026-10-31-prompt-timezone-settings-design.md) | Live | history #4 | |
| [dm-admin-permissions plan](2026-10-31-dm-admin-permissions.md), [design](../superpowers/specs/2026-10-31-dm-admin-permissions-design.md) | Live | — | Deploy record: dm-admin (`clawdot-deployments/docs/deployments/2026-10-31-dm-admin.md`) |
| [injection-heuristic.md](injection-heuristic.md) | Live | history #15 | Log-only; never blocks |
| [link-allowlist.md](link-allowlist.md) | Live | history #20 | |
| [model-switch.md](model-switch.md) | Live | history #28 | `/model` |
| [model-train-flag.md](model-train-flag.md) | Live | history #33 | |
| [command-replies.md](command-replies.md) | Live | history #44 | Replies to a command's question are its input; Telegram reply box on the direct adapter only |
| [2026-11-01-step-cap-and-empty-final.md](2026-11-01-step-cap-and-empty-final.md) | Live | history #34 | |

## Tools and plugins

| Plan | Status | PR | Notes |
| --- | --- | --- | --- |
| [Plugin explainers and coordinated delivery](2026-11-04-plugin-explainers.md) | Live | [#11](https://github.com/keenlok/clawdot-platform/pull/11) | Platform implementation and generated wiki source; release tag, private pin/deployment and wiki publication tracked separately |
| [research-agent.md](research-agent.md) | Live | — | Deploy record: access-research (`clawdot-deployments/docs/deployments/2026-10-31-access-research.md`) |
| [webfetch-ipv6-maps.md](webfetch-ipv6-maps.md) | Live | history #11 | Deploy record: webfetch-direct-writes (`clawdot-deployments/docs/deployments/2026-11-01-webfetch-direct-writes.md`) |
| [receipt-vision.md](receipt-vision.md) | Live | — | |
| [image-reader.md](image-reader.md) | Shipped, inactive | — | Receipts use the shared reader; the describe fallback is off until `VISION_DESCRIBE=true` |
| [multi-trip.md](multi-trip.md) | Live | history #9 | Deploy record: multi-trip (`clawdot-deployments/docs/deployments/2026-10-31-multi-trip.md`) |
| [update-trip plan](2026-10-31-update-trip.md), [design](../superpowers/specs/2026-10-31-update-trip-design.md) | Live | — | Deploy record: update-trip (`clawdot-deployments/docs/deployments/2026-10-31-update-trip.md`) |
| [expense-export plan](2026-10-31-expense-export.md), [design](../superpowers/specs/2026-10-31-expense-export-design.md) | Live | — | Deploy record: expense-export (`clawdot-deployments/docs/deployments/2026-10-31-expense-export.md`) |
| [expense-ux.md](expense-ux.md) | Live | history #10 | |
| [bucket-a-direct-writes.md](bucket-a-direct-writes.md) | Live | history #12 | Deploy record: webfetch-direct-writes (`clawdot-deployments/docs/deployments/2026-11-01-webfetch-direct-writes.md`) |
| [polls.md](polls.md) | Live | — | Per-group toggle |
| [reusable-jobs plan](2026-10-31-reusable-jobs.md) | Live | — | Reminders and website watches. Deploy record: reusable-jobs (`clawdot-deployments/docs/deployments/2026-10-31-reusable-jobs.md`) |
| [browse-links-and-gating.md](browse-links-and-gating.md) | Live | — | Search deep links are live; rendered page reading needs the renderer service (below) |
| [browse-renderer-service.md](browse-renderer-service.md) | Live limited | history #52 | Renderer deployed and connected; allowed groups default on, with explicit opt-out. Deploy record (`clawdot-deployments/docs/deployments/2026-11-02-browser-renderer.md`) |
| [rendered-watch.md](rendered-watch.md) | Live limited | history #53 | Renderer available in allowed groups; creating watches still requires confirmation |
| [2026-11-02-browser-activation.md](2026-11-02-browser-activation.md) | Record | history #52 | Original operator record preserved in private archive; platform stub retains references |
| [2026-11-02-browser-default-on.md](2026-11-02-browser-default-on.md) | Live limited | history #53 | Default-on group reading deployed; saved opt-outs and DM policy preserved |

## Skill layer

| Plan | Status | PR | Notes |
| --- | --- | --- | --- |
| [2026-11-04-tutor-pronunciation-tts.md](2026-11-04-tutor-pronunciation-tts.md) | Live, limited | [#13](https://github.com/keenlok/clawdot-platform/pull/13), [#14](https://github.com/keenlok/clawdot-platform/pull/14), [#18](https://github.com/keenlok/clawdot-platform/pull/18), [#19](https://github.com/keenlok/clawdot-platform/pull/19), [#21](https://github.com/keenlok/clawdot-platform/pull/21), [deploy #16](https://github.com/keenlok/clawdot-deployments/pull/16) | PR #21 adds private-safe Telegram voice failure categories to diagnose the live delivery error |
| [skill-layer.md](skill-layer.md) | Live, limited | history #14 | Design. `SKILLS_MODE=on` only for the owner's test chats; other chats use the legacy prompt |
| [skill-layer-astra-review.md](skill-layer-astra-review.md) | Record | history #14 | Independent design review |
| [tool-descriptions.md](tool-descriptions.md) | Live | history #17 | Stage 0a |
| [skills-stage0.md](skills-stage0.md) | Live | history #18 | Metrics, routing set, embedder benchmark |
| [skills-stage1.md](skills-stage1.md) | Live | history #19 | Budget admission, skill types |
| [skills-stage2-3.md](skills-stage2-3.md) | Live, limited | history #22 | Router and `on` mode, test chats only |
| [skills-prompt-migration.md](skills-prompt-migration.md) | Record | history #22 | `system.md` sentence map |
| [skills-stage4.md](skills-stage4.md) | Live, limited | history #29 | New tools, hints, `/help` groups |
| [2026-11-01-plugin-owned-skills.md](2026-11-01-plugin-owned-skills.md) | Live | history #41, history #38 | Plugins own skill instructions; core owns router settings and default general tools |
| [2026-11-02-empty-skill-instructions.md](2026-11-02-empty-skill-instructions.md) | Live, limited | history #43 | Model and composite guidance; routed and explicit skill loading verified. Applies in skills-on chats |
| [2026-11-02-document-followups.md](2026-11-02-document-followups.md) | Planned | history #60 | Implemented and verified; in review. Human-source rereads and bounded history continuation |
| [2026-11-02-language-tutor.md](2026-11-02-language-tutor.md) | Live, limited | history #48 | Stage 1: `language_tutor` skill, `/tutor` profiles, `Skill.context`/`Skill.pin`. `TUTOR_ENABLED=true` in production; applies in skills-on chats |
| [2026-11-02-language-tutor-groups.md](2026-11-02-language-tutor-groups.md) | Live, limited | history #49 | Stage 1.5: own profile in group turns, admin-marked tutor topics, opt-in auto-reply |
| [2026-11-02-tutor-group-status.md](2026-11-02-tutor-group-status.md) | Live, limited | history #50 | Discoverable group `/tutor` status, reply-to-enable, model points to `/tutor here` |
| [2026-11-02-tutor-topic-polish.md](2026-11-02-tutor-topic-polish.md) | Planned | history #51 | Two explanation languages, trailing auto/quiet, error prompts, auto-reply skips voice/media |
| [2026-11-02-language-tutor-cards.md](2026-11-02-language-tutor-cards.md) | Planned | history #48 | Stage 2: SM-2 review cards, `/review`, daily repeat reminders via jobs |

## Self-extension and self-improvement

| Plan | Status | PR | Notes |
| --- | --- | --- | --- |
| [2026-10-31-self-extension-design.md](2026-10-31-self-extension-design.md) | Live, limited | — | Sandboxed JSON extensions; enabled production trial |
| [2026-10-31-self-extension-controller-plan.md](2026-10-31-self-extension-controller-plan.md) | Live, limited | — | Same trial |
| [2026-10-31-self-extension-worker-plan.md](2026-10-31-self-extension-worker-plan.md) | Live, limited | — | Same trial |
| [2026-10-31-self-extension-deployment.md](2026-10-31-self-extension-deployment.md) | Record | — | Original operator record preserved in private archive; platform stub retains references |
| [2026-10-31-self-extension-verification.md](2026-10-31-self-extension-verification.md) | Record | — | |
| [2026-10-31-self-extension-production-verification.md](2026-10-31-self-extension-production-verification.md) | Record | — | Original operator record preserved in private archive; platform stub retains references |
| [2026-10-31-self-improvement-plan.md](2026-10-31-self-improvement-plan.md) | Shipped, inactive | history #23 | Design (revision 3). Release control is inactive: no policy manifest installed |
| [2026-10-31-self-improvement-astra-review.md](2026-10-31-self-improvement-astra-review.md) | Record | history #23 | |
| [self-improvement implementation](2026-10-31-self-improvement.md), [delivery parts](2026-10-31-self-improvement-parts.md) | Shipped, inactive | history #8 | Deploy record: self-improvement-source (`clawdot-deployments/docs/deployments/2026-10-31-self-improvement-source.md`) |
| Parts [P01](self-improvement-p01.md), [P02](self-improvement-p02.md), [P03](self-improvement-p03.md), [P04](self-improvement-p04.md), [P05](self-improvement-p05.md), [P06](self-improvement-p06.md), [P07](self-improvement-p07.md), [P08](self-improvement-p08.md), [P09](self-improvement-p09.md), [P10](self-improvement-p10.md) | Shipped, inactive | history #8 | `services/release-control` |
| [broker integration](self-improvement-broker-integration.md), [database runtime](self-improvement-database-runtime.md), [revocation](self-improvement-revocation.md), [watchdog](self-improvement-watchdog.md) | Shipped, inactive | history #8 | |
| [release-card.md](release-card.md) | Shipped, inactive | history #13 | Release approval card |
| [2026-11-01-human-pr-intake.md](2026-11-01-human-pr-intake.md) | Planned | history #23 | Blocked on owner decisions D1–D6 |

## Repository and delivery

| Plan | Status | PR | Notes |
| --- | --- | --- | --- |
| [2026-11-01-deploy-pipeline.md](2026-11-01-deploy-pipeline.md) | Record | history #24 | Original operator record preserved in private archive; platform stub retains references |
| [2026-11-01-autodeploy-actions-gate.md](2026-11-01-autodeploy-actions-gate.md) | Record | history #27 | Original operator record preserved in private archive; platform stub retains references |
| [2026-11-01-autodeploy-tarball-root.md](2026-11-01-autodeploy-tarball-root.md) | Record | history #32 | Original operator record preserved in private archive; platform stub retains references |
| [2026-10-31-repo-layout.md](2026-10-31-repo-layout.md) | Live | history #25, history #26, history #31, history #38 | Phases 1–3 and PR 5 (nested `packages/`) merged; phase 4 (docs consolidation) deferred; D7 open |
| [2026-11-02-repo-split.md](2026-11-02-repo-split.md) | Live | history #57, history #62 | Split complete; clean public history approved; previous history and private app stay private; release control remains inactive |
| [2026-11-02-github-invite.md](2026-11-02-github-invite.md) | Record | history #57 | Historical unimplemented invite design; public forks/PRs need no invitation; deployments remain owner-only |
| [2026-11-02-secrets-sops-age.md](2026-11-02-secrets-sops-age.md) | Record | history #57, history #61 | Original operator record preserved in private archive; platform stub retains references |

| [2026-11-03-backup-and-split-execution.md](2026-11-03-backup-and-split-execution.md) | Live | history #62, deployments #1 | Backups/split/publication complete; documentation consolidation and repository-wide local-link checks prepared; no release version change |

## Backlog (not yet planned)

Open requests from [`ARCHITECTURE.md` §12](../../ARCHITECTURE.md#12-backlog), where each one has
its detail:

- Interactive browsing: filling forms, clicking through searches, booking
- Bot detection and CAPTCHAs (detect and stop, never bypass)
- Poll OpenCode for the real remaining balance
- Release history and "what changed?"
- One bot per group: working `apps/_scaffold` shipped; downstream provisioning remains app-owned

## Google Workspace and itinerary implementation

- [2026-11-03 execution handover](../handovers/2026-11-03-google-editing.md) — Record; split reconciliation, execution/review roles, owner-authorized platform integration and coordinated release boundaries. Frozen platform suite: 3,566 passed; installed downstream candidate: 401 passed. No live qualification or deployment.

- [2026-11-02-google-api-editing.md](2026-11-02-google-api-editing.md) — Shipped inactive in [platform PR #5](https://github.com/keenlok/clawdot-platform/pull/5); native edits, qualified copies and original-package DOCX conversion implemented and reviewed. Downstream release pin and copy qualification remain pending; undeployed. Reviewed plan: historical PR #59 (history PR #59).
- [2026-11-02-google-browser-editing.md](2026-11-02-google-browser-editing.md) — Shipped inactive by [platform PR #7](https://github.com/keenlok/clawdot-platform/pull/7), following API [#5](https://github.com/keenlok/clawdot-platform/pull/5) and behavior-preserving proxy extraction [#6](https://github.com/keenlok/clawdot-platform/pull/6). Copy-only broker/worker implemented and reviewed; disabled, unqualified and undeployed. Reviewed plan: historical PR #59 (history PR #59).
- [2026-11-02-trip-drive-files.md](2026-11-02-trip-drive-files.md) — Live, limited via history PR #56; automatically deployed and smoke-verified. history PR #58 records rollout and offers attachment previews alongside Google reads. Primary itineraries and Drive references on existing trips, Docs/DOCX/Sheets/PDF/CSV readers, and empty-response recovery; real Telegram confirmation remains manual acceptance.
- [2026-11-02-drive-docx.md](2026-11-02-drive-docx.md) — Live via history PR #54; automatic deployment, expanded owner read consent and deployed DOCX/native-reader verification completed.
- [2026-11-01-google-workspace.md](2026-11-01-google-workspace.md) — Live for native Docs/Sheets and CSV via history PR #47; owner OAuth production setup and native API checks passed. Revised design #39 was included in #47.
- [2026-11-01-itinerary-tools.md](2026-11-01-itinerary-tools.md) — Live, limited via history PR #47; configured and API-verified, full chat workflow validation pending. Revised design #40 was included in #47.
- [2026-11-02-google-itinerary-implementation.md](2026-11-02-google-itinerary-implementation.md) — Record; implementation, self-review and checks for history PR #47, with owner-authorized merge before Google setup.

- [Telegram download timeout](2026-11-09-telegram-download-timeout.md) — Shipped, inactive; v0.6.1 implements the owner-approved 30-second download budget; downstream release adoption and automatic rollout receipts follow on the associated PRs.
