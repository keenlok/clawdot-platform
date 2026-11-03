# Prompt-based timezone preferences

Date: 2026-10-31. Follow-up to PR #2.
The user clarified that saving timezone preferences must work through natural-language requests,
not just slash commands. The existing no-extra-confirmation UX, Luna/Sol workflow and PR/merge/
deployment authorization remain in effect; no new approval pause is needed.

## Behavior

Add `set_display_time_zone(time_zone)` and `reset_display_time_zone()` to the model's tools.
Explicit requests such as "Use Singapore time for this group" or, in DM, "Remember London time
for my replies" can save a preference immediately. Translate clear city names to validated IANA
keys in tool arguments; ask for clarification when ambiguous. "What time is it in Tokyo?" and
one-off conversions use existing read tools and do not save. Reset is also explicit, never inferred.

The target is always the current reply scope: shared group preference in groups, caller's own
personal DM preference in DMs. No target chat/user argument, no cross-scope lookup. A personal
preference request made in a group should direct the person to DM rather than mutate the shared
group zone. Group writes and resets require a fresh native group-admin lookup every time, including
owners/global bot admins. Existing routing/access/restriction rules remain unchanged.

Reuse the command permission, validation, persistence and reply behavior. Tool errors must not
claim a write occurred. Keep commands available. Do not alter schemas, stored instants, scheduling,
retention, budgets or existing confirmations for any other feature.

## Integration and write policy

Prefer the existing contextual/immediate write pattern used by memory's remember/forget tools,
which do not set Tool.writes=True. That flag enforces always-confirmed writes. Document the
timezone preference's explicitly authorized immediate-write exception, without weakening the
ToolRegistry guard or changing frozen interfaces. Do not add a generic confirmation-bypass system.

For model calls after reading untrusted tool output in the same turn, fail closed with a safe
instruction to request the preference in a separate message. This follows the existing memory
trust boundary while preserving no-extra-confirmation for direct human preference requests.
Never derive a save request from recalled memories, pages, files, history or another person's text.

Update both core safety text and time-zone runtime guidance so they consistently permit only this
narrow immediate preference exception. State that only an explicit current-human preference request
can call these tools and a successful result takes precedence over the start-of-turn zone snapshot.
Other write confirmations stay unchanged. A subsequent current-time read and subsequent turn must
use the newly saved zone.

## Verification and release

Use scripted-provider agent tests (no paid/live LLM calls) to prove an addressed group prompt and
a DM prompt run the setting tool and persist the correct scope without a confirmation action.
Cover non-admin/lookup failure, admin revocation, invalid zones, explicit reset, one-off current-time
queries leaving preferences unchanged, untrusted-tool-result denial, no private preference leak,
and new preference use in follow-up reads/turns. Preserve the general always-confirmed-write guard.

Luna implements, Sol independently reviews/fixes are re-reviewed. Parent runs the combined suite
against a disposable local database, existing secret/format hooks, creates and merges the PR and
deploys the exact merge commit. No live Telegram test sends. No new migration is expected.
