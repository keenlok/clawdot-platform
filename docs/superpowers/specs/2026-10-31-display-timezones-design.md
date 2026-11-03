# Display time zones — proposed design

Date: 2026-10-31
Base: 0568954a14575625c42375e581343c22169a9e63
Status: implemented by Luna and independently reviewed by Sol on 2026-10-31; findings resolved.

> Follow-up decision (2026-10-31): the approved [prompt-based timezone preferences design](2026-10-31-prompt-timezone-settings-design.md)
> supersedes this document's original command-only preference mutation and read-tools-only model
> interface. Explicit natural-language preference requests may now use the two narrowly scoped
> immediate preference tools described there. The UTC and scheduling boundaries below remain in force.

Release authorization: the user subsequently requested a PR, merge and deployment. The completed
in-session Luna/Sol workflow replaces the historical Claude CLI workflow for this release; no
additional Claude review is required. The original implementation-only restrictions below are
superseded for the release work, while the display/scheduling boundaries remain unchanged.

## Goal and boundaries

Convert and label timestamps shown to people. Keep stored instants, comparisons, conversation
ordering, job execution, and scheduling in UTC. Group replies use the configured group zone.
DM replies use the person's voluntarily saved DM preference. Telegram supplies no member zone.

Do not change BUDGET_TZ, privacy housekeeping, existing reminder/poll parsing or explicit-zone
confirmation behavior, interval scheduling, already scheduled instants, or ledger date-only values.
Do not alter other sessions' memory repair, tag investigation, or self-improvement plan.

## Findings

- AgentResponder._context supplies identity/scope/tool facts but no current date/time or display
  zone. Ambient transcript and replied-bot timestamps are UTC. History search returns timestamps
  to the model. The model currently has no authoritative local-current-time input.
- Direct outputs: job next-run listings, poll close listings, research creation listings,
  access-pause notices, and administration restriction/audit detail strings contain UTC instants.
- Reminder/poll creation confirmations deliberately show the explicitly requested IANA zone,
  offset and UTC instant. These are correct and stay unchanged.
- Expense dates and poll date choices are date-only values. Expense settlement export derives a
  date from an instant using its existing ledger zone; preserve that ledger/report policy in this
  narrow change, document the limitation. Rental pickup/return wall clocks have no named-zone
  instant and must not be converted.
- Existing optional GroupAdminPlatform.is_group_admin resolves live Telegram roles. Global bot
  ownership/setup assignment is a different privilege; it does not substitute for this feature's
  required fresh native group-admin check.
- PrivacyPlugin invokes plugin purge_user(chat,user) and purge_chat(chat) hooks. The storage
  contracts are frozen in the original track workflow, so use separate display-preference storage
  rather than expanding CoreRepo, ChatSettings, ToolContext, or ChatPlatform.

## Alternatives

1. Recommended: one core display module, dedicated settings plugin/store, and explicit call-site
   formatting. Covers command output and model-written replies with the same resolution rules.
2. Prompt only: small change, but direct command output remains UTC and preferences still require
   persistence and permission handling.
3. Reuse a global operational time zone: simple deployment setting, but cannot express different
   group/DM preferences and risks conflating display with operational policies.

## User experience

- /timezone shows the effective display zone and source.
- /timezone Asia/Singapore immediately saves a setting in the current scope:
  in groups, the shared group zone (live group admins only); in DMs, only the sender's preference.
- /timezone reset immediately removes the preference in the same scope.
- Explicit setting commands need no extra confirmation tap: this is a reversible display
  preference. This scoped exception does not change confirmation rules for other writes.
- Existing bot-addressed command routing applies in groups.
- /time [IANA-zone] returns current time deterministically; an explicit zone is a one-off override.
  No argument uses the effective display zone.
- Model tools expose read/current-time operations only. To save a preference, the model directs
  the person to the explicit /timezone command; no preference write tool is exposed.
- Group precedence: group preference, otherwise labelled UTC fallback. A person's DM preference
  never changes a group reply or leaks into group model context.
- DM precedence: that sender's DM preference, otherwise labelled UTC fallback.
- A missing preference is described as a fallback when /timezone or /time is used. Other formatted
  timestamps always carry their zone; no repetitive setup prompts.
- Named-person comparisons can use zones supplied explicitly in the request. Automatic lookup
  and disclosure of another person's private DM preference is deferred. No member preference list.

Example: a group configured to Asia/Singapore sees
2026-11-01 09:00:00 Asia/Singapore (UTC+08:00).
An unset group sees UTC-labelled output regardless of the sender's private setting.

## Persistence and integration

Add a dedicated core display-preferences module and plugin, with a separate repository supporting
Postgres and the existing in-memory development mode. New additive core migration 0007 creates
display_time_preferences keyed by chat_key and subject_key: an empty subject denotes a shared group
default; a DM preference uses its own user's key and can only be read in that same DM.
Store zone and updated_by. No historical timestamps are rewritten.

Resolve display settings through a small service taking chat/user/is_dm explicitly. Keep mutable
global/context-variable zone state out of concurrent turns. Inject or call the service without
changing the frozen shared interfaces; use the same resolver in command and tool output paths.
Register the plugin in sb_bot before PrivacyPlugin captures the plugin list.

Only an explicit /timezone setting/reset command writes a preference. Validate the zone and
current chat scope, then do a fresh native group-admin lookup immediately before a group write;
fail closed on lookup error, unsupported adapter, or missing permission. Personal operations
must be a self-owned DM; there is no write-on-behalf capability. Do not create PendingAction
records for this setting. This is a narrow exception to the repository's blanket write-confirmation
policy, following the user's feedback that a timezone change should not need confirmation.

purge_user(chat,user) removes that scope's personal preference; shared group settings stay.
purge_chat removes all display preferences for the chat. DM /forgetme already purges the DM and
eligible group scopes. Update privacy disclosure/confirmation wording to include preferences.
No automatic inference, location lookup, language-based guessing, or silent memory extraction.

## Rendering and model guidance

Use stdlib ZoneInfo, validate an allowlisted installed IANA key, accept UTC, reject abbreviations,
paths and invalid IDs. Convert aware instants with astimezone; reject naive instants.
Always include IANA name and numeric offset so DST folds remain distinguishable.

Add authoritative runtime current UTC instant, effective display zone/source and converted current
time to model context. Keep internal transcript timestamps UTC; tell the model they are instants
to convert to the display zone when quoting times. Explicitly requested zones override presentation
for that answer only and must remain labelled. Do not infer anyone's time zone from Telegram data.
Use a read-only current-time tool for precise explicit-zone current-time requests and refreshes.
Model output is still probabilistic; deterministic /time is the reliable current-time path.

Use the formatter for job/poll/research lists, access pause notices and displayed restriction/audit
instants. Retain UTC in structured audit/storage records and convert only when rendering them.
Do not reinterpret arbitrary provider text, stock source timestamps, or date-only values.

## Validation and handoff

Regressions cover group precedence over a different private preference, UTC fallback, self-managed
DM isolation, no preference disclosure, invalid zones, offset/DST transitions, explicit overrides,
fresh admin checks including revocation between separate commands, no extra confirmation for
setting/reset commands, privacy deletion, and persistent reads/writes. Prove changing the zone leaves due_at/next_run_at
and reminder/poll explicit-zone parsing unchanged. Test model runtime context using a fake clock and
fake provider, plus representative direct-output paths.

Use the isolated checkout; preserve shared main source and other sessions' files. Follow the
in-session Luna implementation and independent Sol review with fixes. The initial phase left
commits and deployment for later authorization; the user's subsequent release request authorizes
PR creation, merge and deployment after checks. No live Telegram test sends are needed. Handoff
includes diff, worktree path, test evidence and limitations.
