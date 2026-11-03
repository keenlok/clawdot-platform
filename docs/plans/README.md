# Plans index

Every plan, design and review record in the repository, with its current status. The
architecture spec is [`ARCHITECTURE.md`](../../ARCHITECTURE.md); the rules for changing the repo are
in [`AGENTS.md`](../../AGENTS.md). **A PR that adds a plan or changes what a plan has delivered
updates its row here in the same PR.**

Last checked: 2026-11-03. All plans share this folder; handovers live in
[`docs/handovers/`](../handovers/), and design specs remain in `docs/superpowers/specs/`.

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
| [Storage architecture explainer](2026-11-03-storage-architecture-explainer.md) | Record | [#2](https://github.com/keenlok/clawdot-platform/pull/2) | Documentation, visual overview and wiki publishing source; wiki publication pending |
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
| [memory-json-fix.md](memory-json-fix.md) | Live | [#1](https://github.com/keenlok/clawdot-platform-history/pull/1) | |
| [telegram-formatting.md](telegram-formatting.md) | Live | — | |

## Conversation, prompts and access

| Plan | Status | PR | Notes |
| --- | --- | --- | --- |
| [conversation-prompt-review.md](conversation-prompt-review.md) | Live | — | |
| [group-mention-only.md](group-mention-only.md) | Live | — | |
| [group-conversation-flow.md](group-conversation-flow.md) | Live | — | Deploy record: [group-flow](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-10-31-group-flow.md) |
| [2026-10-31-reply-chain-attribution.md](2026-10-31-reply-chain-attribution.md) | Live | — | `ARCHITECTURE.md` §19 |
| [access-restrictions.md](access-restrictions.md) | Live | — | Deploy record: [access-research](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-10-31-access-research.md) |
| [context-reset.md](context-reset.md) | Live | — | Deploy record: [access-research](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-10-31-access-research.md) |
| [budget-admin.md](budget-admin.md) | Live | [#5](https://github.com/keenlok/clawdot-platform-history/pull/5) | Deploy record: [budget-dsml-image](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-10-31-budget-dsml-image.md) |
| [dsml-leak-fix.md](dsml-leak-fix.md) | Live | [#6](https://github.com/keenlok/clawdot-platform-history/pull/6) | Same deploy record |
| [image-read-fix.md](image-read-fix.md) | Live | [#7](https://github.com/keenlok/clawdot-platform-history/pull/7) | Same deploy record |
| [image-vision plan](2026-10-31-image-vision.md), [design](../superpowers/specs/2026-10-31-image-vision-design.md) | Live | — | Deploy record: [image-vision](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-10-31-image-vision.md) |
| [display-timezones plan](2026-10-31-display-timezones.md), [design](../superpowers/specs/2026-10-31-display-timezones-design.md), [handover](../handovers/display-timezones.md) | Live | [#2](https://github.com/keenlok/clawdot-platform-history/pull/2) | |
| [prompt-timezone-settings plan](2026-10-31-prompt-timezone-settings.md), [design](../superpowers/specs/2026-10-31-prompt-timezone-settings-design.md) | Live | [#4](https://github.com/keenlok/clawdot-platform-history/pull/4) | |
| [dm-admin-permissions plan](2026-10-31-dm-admin-permissions.md), [design](../superpowers/specs/2026-10-31-dm-admin-permissions-design.md) | Live | — | Deploy record: [dm-admin](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-10-31-dm-admin.md) |
| [injection-heuristic.md](injection-heuristic.md) | Live | [#15](https://github.com/keenlok/clawdot-platform-history/pull/15) | Log-only; never blocks |
| [link-allowlist.md](link-allowlist.md) | Live | [#20](https://github.com/keenlok/clawdot-platform-history/pull/20) | |
| [model-switch.md](model-switch.md) | Live | [#28](https://github.com/keenlok/clawdot-platform-history/pull/28) | `/model` |
| [model-train-flag.md](model-train-flag.md) | Live | [#33](https://github.com/keenlok/clawdot-platform-history/pull/33) | |
| [command-replies.md](command-replies.md) | Live | [#44](https://github.com/keenlok/clawdot-platform-history/pull/44) | Replies to a command's question are its input; Telegram reply box on the direct adapter only |
| [2026-11-01-step-cap-and-empty-final.md](2026-11-01-step-cap-and-empty-final.md) | Live | [#34](https://github.com/keenlok/clawdot-platform-history/pull/34) | |

## Tools and plugins

| Plan | Status | PR | Notes |
| --- | --- | --- | --- |
| [research-agent.md](research-agent.md) | Live | — | Deploy record: [access-research](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-10-31-access-research.md) |
| [webfetch-ipv6-maps.md](webfetch-ipv6-maps.md) | Live | [#11](https://github.com/keenlok/clawdot-platform-history/pull/11) | Deploy record: [webfetch-direct-writes](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-11-01-webfetch-direct-writes.md) |
| [receipt-vision.md](receipt-vision.md) | Live | — | |
| [image-reader.md](image-reader.md) | Shipped, inactive | — | Receipts use the shared reader; the describe fallback is off until `VISION_DESCRIBE=true` |
| [multi-trip.md](multi-trip.md) | Live | [#9](https://github.com/keenlok/clawdot-platform-history/pull/9) | Deploy record: [multi-trip](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-10-31-multi-trip.md) |
| [update-trip plan](2026-10-31-update-trip.md), [design](../superpowers/specs/2026-10-31-update-trip-design.md) | Live | — | Deploy record: [update-trip](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-10-31-update-trip.md) |
| [expense-export plan](2026-10-31-expense-export.md), [design](../superpowers/specs/2026-10-31-expense-export-design.md) | Live | — | Deploy record: [expense-export](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-10-31-expense-export.md) |
| [expense-ux.md](expense-ux.md) | Live | [#10](https://github.com/keenlok/clawdot-platform-history/pull/10) | |
| [bucket-a-direct-writes.md](bucket-a-direct-writes.md) | Live | [#12](https://github.com/keenlok/clawdot-platform-history/pull/12) | Deploy record: [webfetch-direct-writes](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-11-01-webfetch-direct-writes.md) |
| [polls.md](polls.md) | Live | — | Per-group toggle |
| [reusable-jobs plan](2026-10-31-reusable-jobs.md) | Live | — | Reminders and website watches. Deploy record: [reusable-jobs](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-10-31-reusable-jobs.md) |
| [browse-links-and-gating.md](browse-links-and-gating.md) | Live | — | Search deep links are live; rendered page reading needs the renderer service (below) |
| [browse-renderer-service.md](browse-renderer-service.md) | Live limited | [#52](https://github.com/keenlok/clawdot-platform-history/pull/52) | Renderer deployed and connected; allowed groups default on, with explicit opt-out. [Deploy record](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-11-02-browser-renderer.md) |
| [rendered-watch.md](rendered-watch.md) | Live limited | [#53](https://github.com/keenlok/clawdot-platform-history/pull/53) | Renderer available in allowed groups; creating watches still requires confirmation |
| [2026-11-02-browser-activation.md](2026-11-02-browser-activation.md) | Record | [#52](https://github.com/keenlok/clawdot-platform-history/pull/52) | Original operator record preserved in private archive; platform stub retains references |
| [2026-11-02-browser-default-on.md](2026-11-02-browser-default-on.md) | Live limited | [#53](https://github.com/keenlok/clawdot-platform-history/pull/53) | Default-on group reading deployed; saved opt-outs and DM policy preserved |

## Skill layer

| Plan | Status | PR | Notes |
| --- | --- | --- | --- |
| [skill-layer.md](skill-layer.md) | Live, limited | [#14](https://github.com/keenlok/clawdot-platform-history/pull/14) | Design. `SKILLS_MODE=on` only for the owner's test chats; other chats use the legacy prompt |
| [skill-layer-astra-review.md](skill-layer-astra-review.md) | Record | [#14](https://github.com/keenlok/clawdot-platform-history/pull/14) | Independent design review |
| [tool-descriptions.md](tool-descriptions.md) | Live | [#17](https://github.com/keenlok/clawdot-platform-history/pull/17) | Stage 0a |
| [skills-stage0.md](skills-stage0.md) | Live | [#18](https://github.com/keenlok/clawdot-platform-history/pull/18) | Metrics, routing set, embedder benchmark |
| [skills-stage1.md](skills-stage1.md) | Live | [#19](https://github.com/keenlok/clawdot-platform-history/pull/19) | Budget admission, skill types |
| [skills-stage2-3.md](skills-stage2-3.md) | Live, limited | [#22](https://github.com/keenlok/clawdot-platform-history/pull/22) | Router and `on` mode, test chats only |
| [skills-prompt-migration.md](skills-prompt-migration.md) | Record | [#22](https://github.com/keenlok/clawdot-platform-history/pull/22) | `system.md` sentence map |
| [skills-stage4.md](skills-stage4.md) | Live, limited | [#29](https://github.com/keenlok/clawdot-platform-history/pull/29) | New tools, hints, `/help` groups |
| [2026-11-01-plugin-owned-skills.md](2026-11-01-plugin-owned-skills.md) | Live | [#41](https://github.com/keenlok/clawdot-platform-history/pull/41), [#38](https://github.com/keenlok/clawdot-platform-history/pull/38) | Plugins own skill instructions; core owns router settings and default general tools |
| [2026-11-02-empty-skill-instructions.md](2026-11-02-empty-skill-instructions.md) | Live, limited | [#43](https://github.com/keenlok/clawdot-platform-history/pull/43) | Model and composite guidance; routed and explicit skill loading verified. Applies in skills-on chats |
| [2026-11-02-document-followups.md](2026-11-02-document-followups.md) | Planned | [#60](https://github.com/keenlok/clawdot-platform-history/pull/60) | Implemented and verified; in review. Human-source rereads and bounded history continuation |
| [2026-11-02-language-tutor.md](2026-11-02-language-tutor.md) | Live, limited | [#48](https://github.com/keenlok/clawdot-platform-history/pull/48) | Stage 1: `language_tutor` skill, `/tutor` profiles, `Skill.context`/`Skill.pin`. `TUTOR_ENABLED=true` in production; applies in skills-on chats |
| [2026-11-02-language-tutor-groups.md](2026-11-02-language-tutor-groups.md) | Live, limited | [#49](https://github.com/keenlok/clawdot-platform-history/pull/49) | Stage 1.5: own profile in group turns, admin-marked tutor topics, opt-in auto-reply |
| [2026-11-02-tutor-group-status.md](2026-11-02-tutor-group-status.md) | Live, limited | [#50](https://github.com/keenlok/clawdot-platform-history/pull/50) | Discoverable group `/tutor` status, reply-to-enable, model points to `/tutor here` |
| [2026-11-02-tutor-topic-polish.md](2026-11-02-tutor-topic-polish.md) | Planned | [#51](https://github.com/keenlok/clawdot-platform-history/pull/51) | Two explanation languages, trailing auto/quiet, error prompts, auto-reply skips voice/media |
| [2026-11-02-language-tutor-cards.md](2026-11-02-language-tutor-cards.md) | Planned | [#48](https://github.com/keenlok/clawdot-platform-history/pull/48) | Stage 2: SM-2 review cards, `/review`, daily repeat reminders via jobs |

## Self-extension and self-improvement

| Plan | Status | PR | Notes |
| --- | --- | --- | --- |
| [2026-10-31-self-extension-design.md](2026-10-31-self-extension-design.md) | Live, limited | — | Sandboxed JSON extensions; enabled production trial |
| [2026-10-31-self-extension-controller-plan.md](2026-10-31-self-extension-controller-plan.md) | Live, limited | — | Same trial |
| [2026-10-31-self-extension-worker-plan.md](2026-10-31-self-extension-worker-plan.md) | Live, limited | — | Same trial |
| [2026-10-31-self-extension-deployment.md](2026-10-31-self-extension-deployment.md) | Record | — | Original operator record preserved in private archive; platform stub retains references |
| [2026-10-31-self-extension-verification.md](2026-10-31-self-extension-verification.md) | Record | — | |
| [2026-10-31-self-extension-production-verification.md](2026-10-31-self-extension-production-verification.md) | Record | — | Original operator record preserved in private archive; platform stub retains references |
| [2026-10-31-self-improvement-plan.md](2026-10-31-self-improvement-plan.md) | Shipped, inactive | [#23](https://github.com/keenlok/clawdot-platform-history/pull/23) | Design (revision 3). Release control is inactive: no policy manifest installed |
| [2026-10-31-self-improvement-astra-review.md](2026-10-31-self-improvement-astra-review.md) | Record | [#23](https://github.com/keenlok/clawdot-platform-history/pull/23) | |
| [self-improvement implementation](2026-10-31-self-improvement.md), [delivery parts](2026-10-31-self-improvement-parts.md) | Shipped, inactive | [#8](https://github.com/keenlok/clawdot-platform-history/pull/8) | Deploy record: [self-improvement-source](https://github.com/keenlok/clawdot-deployments/blob/main/docs/deployments/2026-10-31-self-improvement-source.md) |
| Parts [P01](self-improvement-p01.md), [P02](self-improvement-p02.md), [P03](self-improvement-p03.md), [P04](self-improvement-p04.md), [P05](self-improvement-p05.md), [P06](self-improvement-p06.md), [P07](self-improvement-p07.md), [P08](self-improvement-p08.md), [P09](self-improvement-p09.md), [P10](self-improvement-p10.md) | Shipped, inactive | [#8](https://github.com/keenlok/clawdot-platform-history/pull/8) | `services/release-control` |
| [broker integration](self-improvement-broker-integration.md), [database runtime](self-improvement-database-runtime.md), [revocation](self-improvement-revocation.md), [watchdog](self-improvement-watchdog.md) | Shipped, inactive | [#8](https://github.com/keenlok/clawdot-platform-history/pull/8) | |
| [release-card.md](release-card.md) | Shipped, inactive | [#13](https://github.com/keenlok/clawdot-platform-history/pull/13) | Release approval card |
| [2026-11-01-human-pr-intake.md](2026-11-01-human-pr-intake.md) | Planned | [#23](https://github.com/keenlok/clawdot-platform-history/pull/23) | Blocked on owner decisions D1–D6 |

## Repository and delivery

| Plan | Status | PR | Notes |
| --- | --- | --- | --- |
| [2026-11-01-deploy-pipeline.md](2026-11-01-deploy-pipeline.md) | Record | [#24](https://github.com/keenlok/clawdot-platform-history/pull/24) | Original operator record preserved in private archive; platform stub retains references |
| [2026-11-01-autodeploy-actions-gate.md](2026-11-01-autodeploy-actions-gate.md) | Record | [#27](https://github.com/keenlok/clawdot-platform-history/pull/27) | Original operator record preserved in private archive; platform stub retains references |
| [2026-11-01-autodeploy-tarball-root.md](2026-11-01-autodeploy-tarball-root.md) | Record | [#32](https://github.com/keenlok/clawdot-platform-history/pull/32) | Original operator record preserved in private archive; platform stub retains references |
| [2026-10-31-repo-layout.md](2026-10-31-repo-layout.md) | Live | [#25](https://github.com/keenlok/clawdot-platform-history/pull/25), [#26](https://github.com/keenlok/clawdot-platform-history/pull/26), [#31](https://github.com/keenlok/clawdot-platform-history/pull/31), [#38](https://github.com/keenlok/clawdot-platform-history/pull/38) | Phases 1–3 and PR 5 (nested `packages/`) merged; phase 4 (docs consolidation) deferred; D7 open |
| [2026-11-02-repo-split.md](2026-11-02-repo-split.md) | Live | [#57](https://github.com/keenlok/clawdot-platform-history/pull/57), [#62](https://github.com/keenlok/clawdot-platform-history/pull/62) | Split complete; clean public history approved; previous history and private app stay private; release control remains inactive |
| [2026-11-02-github-invite.md](2026-11-02-github-invite.md) | Record | [#57](https://github.com/keenlok/clawdot-platform-history/pull/57) | Historical unimplemented invite design; public forks/PRs need no invitation; deployments remain owner-only |
| [2026-11-02-secrets-sops-age.md](2026-11-02-secrets-sops-age.md) | Record | [#57](https://github.com/keenlok/clawdot-platform-history/pull/57), [#61](https://github.com/keenlok/clawdot-platform-history/pull/61) | Original operator record preserved in private archive; platform stub retains references |

| [2026-11-03-backup-and-split-execution.md](2026-11-03-backup-and-split-execution.md) | Live | [#62](https://github.com/keenlok/clawdot-platform-history/pull/62), [deployments #1](https://github.com/keenlok/clawdot-deployments/pull/1) | Backups/split/publication complete; documentation consolidation and repository-wide local-link checks prepared; no release version change |

## Backlog (not yet planned)

Open requests from [`ARCHITECTURE.md` §12](../../ARCHITECTURE.md#12-backlog), where each one has
its detail:

- Interactive browsing: filling forms, clicking through searches, booking
- Bot detection and CAPTCHAs (detect and stop, never bypass)
- Poll OpenCode for the real remaining balance
- Release history and "what changed?"
- Share a contribute or learn link in allowed groups
- One bot per group: working `apps/_scaffold` shipped; downstream provisioning remains app-owned

## Google Workspace and itinerary implementation

- [2026-11-02-google-api-editing.md](2026-11-02-google-api-editing.md) — Planned ([PR #59](https://github.com/keenlok/clawdot-platform-history/pull/59)); draft for owner review. Confirmed native Doc/Sheet edits, permission-aware copies and DOCX-to-new-Google-Doc conversion; no DOCX write-back. Includes existing write workflow verification.
- [2026-11-02-google-browser-editing.md](2026-11-02-google-browser-editing.md) — Planned ([PR #59](https://github.com/keenlok/clawdot-platform-history/pull/59)); draft for owner review. Separate authenticated Playwright worker, signed in as a dedicated editor Google account (never the owner), and a copy-only Google Docs qualification trial whose copies are shared with that account and configured chat members as editors; API remains default, dependent on the API plan's shared copy/confirmation/result primitives.
- [2026-11-02-trip-drive-files.md](2026-11-02-trip-drive-files.md) — Live, limited via [PR #56](https://github.com/keenlok/clawdot-platform-history/pull/56); automatically deployed and smoke-verified. [PR #58](https://github.com/keenlok/clawdot-platform-history/pull/58) records rollout and offers attachment previews alongside Google reads. Primary itineraries and Drive references on existing trips, Docs/DOCX/Sheets/PDF/CSV readers, and empty-response recovery; real Telegram confirmation remains manual acceptance.
- [2026-11-02-drive-docx.md](2026-11-02-drive-docx.md) — Live via [PR #54](https://github.com/keenlok/clawdot-platform-history/pull/54); automatic deployment, expanded owner read consent and deployed DOCX/native-reader verification completed.
- [2026-11-01-google-workspace.md](2026-11-01-google-workspace.md) — Live for native Docs/Sheets and CSV via [PR #47](https://github.com/keenlok/clawdot-platform-history/pull/47); owner OAuth production setup and native API checks passed. Revised design #39 was included in #47.
- [2026-11-01-itinerary-tools.md](2026-11-01-itinerary-tools.md) — Live, limited via [PR #47](https://github.com/keenlok/clawdot-platform-history/pull/47); configured and API-verified, full chat workflow validation pending. Revised design #40 was included in #47.
- [2026-11-02-google-itinerary-implementation.md](2026-11-02-google-itinerary-implementation.md) — Record; implementation, self-review and checks for [PR #47](https://github.com/keenlok/clawdot-platform-history/pull/47), with owner-authorized merge before Google setup.
