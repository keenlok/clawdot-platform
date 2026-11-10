# Expense speaker context: who shares, who's asking, who's distinct

Status: Planned. Owner approval required before code.

## Incident

Follow-up to [Expense split names](2026-11-09-expense-split-names.md) (v0.6.2). In the same
2026-11-09 group chat, after a split was corrected:

1. A different member asked "who are the 4?". The bot answered by repeating its previous
   reply, written for the payer, including "(you — paid)", so it addressed the wrong person.
2. The bot claimed a trip member was a duplicate of another member with a similar name and
   offered to merge them. They were two different people, and no tool can merge members.

A third incident, on 2026-11-11 in the same chat: asked why one member owed so much, the bot
explained that member's balance item by item. The balance total (from `get_balances`) was
right, but the breakdown wasn't. It said the member had an equal share of a group meal, a
burger meal, dim sum and "a long tail" of ride-hailing trips. The ledger shows that member
wasn't in any of those splits, and was in only two of five rides. It had first listed only 30
of 43 expenses (`limit: 30`). The false breakdown led another member to ask the bot to remove
that member from a split they were never in.

All three were model guesses where the tools gave it nothing better:

- `list_expenses` prints `#id description: amount (paid by X)`. The repository query already
  returns `shares`, but the tool drops them, so the model couldn't look up who was in a split.
- No tool answers "what is X's part of the balance?". The model has to rebuild one member's
  history from a list without shares and from balance totals, so it guessed.
- Trip members and expense names are listed by display name only, so two similar names look
  like one person typed two ways.
- The current message's speaker reaches the model only as a JSON metadata record, the same
  shape as history records. No instruction says that this speaker is who "you" means in the
  reply.

(Real names and handles are kept out of this public record.)

## Changes

### A. Expenses plugin (`packages/plugins/expenses`)

1. **`list_expenses` shows participants.** Append the sharing members by display name to each
   row: `#id description: HKD312.00 (paid by A; split: A, B, C, D)`. For non-equal splits,
   show per-person amounts as the echoes already do. Reuse `_member_names` from v0.6.2 (8
   names, then `+K more`). Keep the row prefix unchanged so ids still parse.
2. **Disambiguate similar names.** When two or more listed members share a case-insensitive
   prefix of 3+ characters in their display names (e.g. "Dan…"), append `(@username)` to
   those members only, in the trip `Members:` line, `list_expenses` and the add/update
   echoes. Members without a username keep the display name. Unrelated names stay unchanged
   so ordinary output doesn't grow.
3. **Instructions** (`TRIPS_INSTRUCTIONS` is at 782/800, `EXPENSES_INSTRUCTIONS` at 752/800):
   add to `EXPENSES_INSTRUCTIONS` a sentence such as `"Members with similar names are
   different people; never suggest merging them."` and tighten existing wording to stay ≤ 800.
   Say who is in an expense only from tool output (covered by A1 plus this sentence if it
   fits: `"Answer who shares an expense from list_expenses, not earlier replies."`).

### C. Per-member expense history (new read-only tool)

5. **`member_expenses`** in the expenses plugin, in the `expenses` skill. Arguments: `member`
   (required, name as the user wrote it, resolved with `_resolve_member`), `trip` / `general`
   (same meaning as `list_expenses`). It returns, for that member, deterministically:
   - every non-deleted expense where they paid **or** have a share: `#id`, description,
     date, original amount and currency, payer, **their share** in the original currency and
     converted to the settle currency (using each expense's stored rate, the same way
     balances do);
   - settlements they sent or received;
   - totals: paid, own share, net. The net must equal what `get_balances` reports for them
     (same conversion code path), and the output says which currency it's in;
   - no `limit`: a member's history in one trip is small. Above 100 rows, show the 100 most
     recent and say how many were left out, with the totals still over everything.
   Expenses the member isn't in don't appear. It reads only the ledger the requester's chat
   can already see, through the same chat resolution as `list_expenses`.
6. **Gate:** same as `list_expenses` (read-only). Mirror its entry in `gates.py`; no new
   permission. Call this out in the PR as a gate addition with unchanged policy.
7. **Instructions:** `"To explain one person's balance or history, call member_expenses;
   never infer shares from list_expenses or memory."` Tighten wording to stay ≤ 800.
8. Register it wherever the tests require a new tool: `tests/test_tool_audit.py` and the
   request snapshot.

### B. Core speaker framing (`packages/core/src/chatcore/agent.py`)

4. **State who "you" is.** Add one fixed instruction to the system prompt (a constant next to
   `_TRANSCRIPT_INTRO`): `"The speaker of the latest user message is the person you are
   replying to; "you" in your reply means them, not people you addressed earlier. In groups,
   name other people instead of calling them "you"."` It's a constant, so no user-controlled
   text enters the system prompt. The existing per-turn metadata record (already inside the
   untrusted wrapper) stays the source of the speaker's identity.

Core is maintenance-reviewed (`AGENTS.md`). B changes the agent request snapshot; A doesn't
touch core.

Out of scope: changing `_resolve_member`, nickname memories, a member-merge tool, and
model choice.

## Owned files

- `packages/plugins/expenses/src/chatcore_expenses/plugin.py`
- `packages/plugins/expenses/tests/**`
- `packages/core/src/chatcore/agent.py` (one constant and where it's added to the system
  prompt)
- `packages/core/tests/**` only where they assert the system prompt, and
  `packages/core/tests/snapshots/agent_requests_off.json` (regenerate; the diff must be just
  the new sentence)
- `CHANGELOG.md` (`Unreleased`)
- `docs/plans/README.md` (this plan's row)

- `packages/plugins/expenses/src/chatcore_expenses/gates.py` (only the new tool's entry,
  mirroring `list_expenses`)
- `tests/test_tool_audit.py` (register the new tool)

Don't touch `permissions.py`, migrations or `__init__.py`. `repository.py` only if the existing
`expenses()` query can't serve C without one; prefer reusing it and the balance conversion
helpers. Don't change existing tool names, argument schemas or pending-action payload shapes.

## Tests

- `member_expenses`: includes expenses the member paid or shares, excludes ones they're not
  in, shows their share per row, includes settlements both ways, and its net equals
  `get_balances` for that member in a convert-mode trip with mixed currencies and an exact
  split. Deleted expenses are excluded. Ambiguous or unknown member → the resolver's error.
  Over 100 rows → truncation note with totals over everything.
- `list_expenses` rows include participants for equal and exact splits, with truncation over 8,
  and keep the `#id` prefix.
- Similar-name handles: two members "Daniel"/"Danny" with usernames show `(@…)` in the
  `Members:` line, list rows and echoes; an unrelated "Alice" doesn't; a similar-name member
  with no username shows the display name only.
- Instructions ≤ 800 characters and contain the new sentence(s).
- Core: the system prompt contains the speaker sentence exactly once; snapshot diff is only
  that sentence.
- Use placeholder names and handles only; the private-identifier hook now runs on commit.

## Checks

```sh
export UV_CACHE_DIR="$PWD/.uv-cache"
uv sync --locked --all-packages
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check <changed .py files>
uv run pytest -q -m "not browser and not sandbox_integration"
```

Postgres-backed tests may skip in the Codex sandbox; Claude re-runs them locally.

## Done when

- A–B implemented as above; lint clean; full suite passes locally.
- Snapshot diff limited to the speaker sentence.
- After release and re-pin (separate steps, as in v0.6.2), in-chat checks: a second member
  asking "who's in that split?" gets the ledger's participants and isn't called "you" unless
  they're in it; listing a trip with similar names shows their handles; "why does X owe so
  much?" is answered from `member_expenses`, and every item it names includes X.

## Execution

Claude planned; Codex executes and reviews with `gpt-6-luna` at high reasoning effort
(owner's request); Claude does the final review. Release (v0.6.3) and the downstream re-pin
follow the same path as v0.6.2.
