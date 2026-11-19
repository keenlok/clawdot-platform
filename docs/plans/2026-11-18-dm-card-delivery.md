# DM-requested expense cards and exports go to the DM

Status: Planned (bug fix, patch release). Owner-approved 2026-11-18.

Read `AGENTS.md`, `packages/plugins/expenses/src/chatcore_expenses/plugin.py` (`_tool_context`,
`_send_expense_card`, `_export`, and the `if self.dm_ledger == "private":` block at the end of
`tools()`), and `packages/plugins/expenses/tests/test_private_dm_trips.py` and `test_cards.py`.

## Problem

With `ExpensesPlugin(dm_ledger="private")`, a DM tool call that names a group (`group="Crew"`), or a
trip that resolves to a unique group trip, goes through the `routed` wrapper in `tools()`.
`_tool_context()` returns `replace(ctx, chat=<group>, is_dm=False)` so the handler reads the group's
ledger. The handlers then *deliver* to `ctx.chat` too:

- `_send_expense_card()` (tools `send_balance_card`, `send_trip_summary_card`) calls
  `send_card_pages(ctx.platform, ctx.chat, ..., thread_id=ctx.thread_id)`.
- `_export()` (tool `export_expenses`) calls `ctx.platform.send_file(ctx.chat, ...,
  thread_id=ctx.thread_id)`.

So a card or CSV the user asked for in a DM is posted to the group chat. Seen in production: three
`send_trip_summary_card(trip=..., group=...)` calls from a DM all posted to the group, and the
"Card sent above the text reply" result made the model tell the user the card was in the DM.

The default (non-private) mode is not affected: there `ctx.chat` stays the DM and only the ledger
chat comes from `group_for_dm`.

## You own

- `packages/plugins/expenses/src/chatcore_expenses/plugin.py`
- `packages/plugins/expenses/tests/test_private_dm_trips.py`
- `packages/plugins/expenses/tests/test_cards.py` (only if a card test fits better there)
- `CHANGELOG.md` (one `Unreleased` → `### Fixed` entry)

You may **not** edit core (`packages/core/`), `ToolContext`, `gates.py`, `permissions.py`, any
`__init__.py`, migrations, tool names, tool parameter schemas or tool descriptions. The agent
request snapshot and skill catalog tests must pass unchanged. Ask in your report if one must change.

## 1. Keep the requesting chat as the delivery target

The ledger chat and the delivery chat must be separate. In the `routed` wrapper, the context the
handler receives keeps ledger scoping as today, but anything sent to the user goes to the
**original** chat and thread (`ctx.chat`, `ctx.thread_id` of the incoming DM context).

Suggested shape (pick something equivalent if cleaner, but keep it inside the plugin):

- `_send_expense_card(self, ctx, *, trip=None, general=False, summary=False, deliver_to=None)` and
  `_export(self, ctx, *, trip=None, general=False, deliver_to=None)`, where `deliver_to` is an
  internal `(ChatRef, thread_id)` tuple. `None` means `(ctx.chat, ctx.thread_id)`, which is
  today's behavior everywhere else.
- `routed` passes `deliver_to=(ctx.chat, ctx.thread_id)` (the unswapped DM context) to the
  handlers of `send_balance_card`, `send_trip_summary_card` and `export_expenses` only. The model
  must not be able to set `deliver_to`: it is not in the parameter schema, and `routed` must drop
  any model-supplied `deliver_to` key (or the handler must not be reachable with one from the
  model). Other tools are unchanged.
- The `/export` command (`_command_export`) keeps its current behavior.

Security rules that stay as they are:

- Group membership is still checked by `_named_group` / `_require_current_group_member` before a
  group ledger is read. Do not weaken or skip it.
- The existing "Card delivery is no longer allowed." scope recheck in `_send_expense_card` must
  keep working for the default (`group_for_dm`) mode exactly as `test_cards.py` asserts today.
- Delivery always goes to the chat the user is talking in; never to a chat named by the model.

## 2. Changelog

Add under `## [Unreleased]`, `### Fixed`: with `dm_ledger="private"`, balance cards, trip summary
cards and expense exports requested in a DM for a group ledger are now sent to the DM instead of
the group chat. No configuration change.

## Tests

In `test_private_dm_trips.py` (use its `dm_context` fixture, `make_member`, `FakePlatform`, and a
fake card client like `_CardClient` in `test_cards.py`):

1. `send_trip_summary_card(trip=..., group="Crew")` from a DM, where the trip is in the group
   ledger: the card photo(s) are sent to the DM chat, none to the group; the card shows the group
   ledger's figures (e.g. the group expense's description/amount, or the trip name).
2. Same for `send_balance_card(group="Crew")`.
3. A trip-name-only call that resolves to a unique group trip (no `group` argument) also delivers
   to the DM.
4. `export_expenses(group="Crew")` from a DM: the file goes to the DM chat, none to the group,
   and it contains the group ledger's expense.
5. A model-supplied `deliver_to` (or whatever the internal name is) passed to the tool handler
   does not redirect delivery to another chat.
6. A non-member group is still refused, and nothing is sent anywhere.
7. In a group chat (`is_dm=False`) the card and export still go to that group (unchanged).

Check how `FakePlatform` records photos and files (`sent_photos`, `sent_files`) and assert on the
chat of each.

## Done when

- `uv run ruff check packages services apps tests .github/scripts` is clean and
  `uv run ruff format --check` passes for the files you changed.
- `uv run pytest -q -m "not browser and not sandbox_integration" packages/plugins/expenses` passes,
  and the full `uv run pytest -q -m "not browser and not sandbox_integration"` passes (Postgres
  tests may skip without a database; say so).
- `packages/core/tests/snapshots/agent_requests_off.json` and skill catalog/routing-set tests pass
  unchanged.
- Left for later: the version bump and release tag (separate release step), and making card tool
  results name where the card was sent.
