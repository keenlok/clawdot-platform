# Private DM members and doubled-bar DSML markup

Status: Planned; owner approved the fix on 2026-11-16. Implemented in [PR #59](https://github.com/keenlok/clawdot-platform/pull/59). Claude planned; Codex CLI is installed, so
Codex executes and reviews, then Claude does the final review.

## Problems

Both seen live on 2026-11-16 in a downstream deployment running v0.19.0 with
`EXPENSES_DM_LEDGER=private`, while the owner added six receipts to a private DM trip.

1. **Private DM trips can't find their own member.** `ExpensesPlugin._members(ctx, chat)` reads
   `ctx.stores.repo.find_members(chat)`, and chat members are recorded only for groups, so a DM chat
   has none. `_add_expense` with no `split_with` takes the trip's members from that lookup and
   finds nobody, so every default-split expense fails with "<name> isn't on the <trip>'s member
   list", even though `trip_members` lists the requester. `update_trip_members` then reports "no
   changes", and the model loops until the step limit. Expenses only succeed when the model happens
   to pass `split_with=["me"]`. Any other path that resolves a name through `_members` or
   `_resolve_member` in a private DM (the requester's display name or `@username`, balances, member
   display) has the same gap.
2. **Doubled-bar DSML markup leaks to users.** When a turn hit the step limit and the final answer
   was forced, the model wrote its tool call as text using `<｜｜DSML｜｜ calls>` (two U+FF5C on each
   side). `chatcore.llm.sanitize.strip_tool_markup` only matches one bar, so the raw markup was
   sent to the chat (`markup_stripped=0`).

## Fix

1. `packages/plugins/expenses/src/chatcore_expenses/plugin.py`:
   - `_members(ctx, chat)`: when `self._private_dm(ctx, chat)` (private mode, DM, ledger chat is
     the DM), return the stored members plus `ctx.user` if not already present (by `ref.key`).
   - `_resolve_member(ctx, chat, name)`: in that same private DM case, a name that
     `_requester_name` accepts (me, I, user id, username with or without `@`, display name, case
     insensitive) returns `ctx.user` before any lookup.
   - Group ledgers, group-mode DMs and groups reached from a DM are unchanged: no extra member is
     added there. Private-mode scope checks (personal-only at request and at commit) are unchanged.
2. `packages/core/src/chatcore/llm/sanitize.py`: accept one or more bars (`｜+`, and ASCII `|+`
   where the existing patterns accept `|`) on each side of `DSML` and of the special-token forms,
   in every pattern that matches them (`_BLOCKS` DSML entries, `_DSML_TAG`, `_SPECIAL_TOKEN`,
   `_PARTIAL_TOKEN`). Existing single-bar behavior and prose preservation are unchanged.

No adapter, permission, gate, migration or configuration change.

## Tests

- Expenses (`packages/plugins/expenses/tests/test_private_dm_trips.py`), with a DM context whose
  repo has no members for the DM chat, mirroring production:
  - A private trip started in the DM, then `add_expense` with no `split_with` and `paid_by` of
    "me", the display name, and `@username` each succeeds, paid by and split to the requester only.
  - `update_trip_members` on that trip naming the requester reports no change and doesn't error;
    naming someone else is still refused.
  - Balance/summary output for the private trip names the requester (not a raw key or "unknown").
  - Group-mode DM (`dm_ledger="group"`) and a group ledger still don't gain an implicit member.
- Core (`packages/core/tests/test_tool_markup.py`): the exact production doubled-bar block below is
  stripped to `("", True)`; prose before it is kept; a doubled-bar partial token at the end is
  stripped; existing single-bar tests pass unchanged. Production text (one example):
  `<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="add_expense">\n<｜｜DSML｜｜ parameter name="amount"
  string="true">584.94</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>`.
- Agent request snapshot, skill catalog and routing-set tests unchanged.

## Release and rollout

- `CHANGELOG.md` `Unreleased` → `Fixed` entries for both. The version is bumped as a patch on top
  of whatever release is current when this merges (v0.20.1 if v0.20.0 has shipped).
- Downstream: clawdot-deployments pins the patch for SBot and Loki in a separate PR.
