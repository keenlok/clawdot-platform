# Rendered reading on by default in groups — 2026-11-02

The owner requested that the deployed browser remain on by default. The immediately preceding
exchange concerned group activation. Scope: allowed group chats default on when the renderer is
configured; saved per-group opt-outs remain off; DMs retain BROWSE_DM_ENABLED (false by default).

1. Change only BrowseRepo's missing-row group default. Do not edit the applied migration:
   configure inserts and updates its row in one transaction, so its temporary SQL default
   is not externally visible. Existing rows are explicit settings and remain authoritative.
2. Add regression coverage for absent settings, saved opt-outs, group tool availability and
   the shared rendered-watch setting. Access controls, confirmations, URL provenance,
   renderer availability and limits stay unchanged.
3. Update operator instructions and the plans index. Run focused tests, repository checks,
   an independent review, and CI. Merge and deploy through the normal release path.
4. Verify the live group's computed setting is on and SBot can reach the healthy renderer.
   Record the deployed release and validations.

## Result

Deployed source `fad6463e6dfc`. The live itinerary group's computed setting is
`rendered_enabled=true, revision=0` without a database write; rendered-watch eligibility
also returns true. Both containers are healthy and the bot reaches the renderer. DMs
remain false by default. CI passed 3,536 tests (6 skipped), and an independent Codex
review found no blockers. Deployment record (`clawdot-deployments/docs/deployments/2026-11-02-browser-default-on.md`).
