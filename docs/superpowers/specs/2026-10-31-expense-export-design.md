# Exporting the expense ledger as CSV (`export_expenses`, `/export`)

Requested 2026-10-31. Users only see the ledger through `/expenses` (the last 10), `list_expenses` and `/balance`. They want a file they can open in a spreadsheet to check the bot's balances by hand and confirm that no expense ended up in the wrong trip.

## Behaviour

A read-only tool, `export_expenses`, and a command, `/export`, build one CSV for one ledger scope and send it to the chat where it was asked for, as a document.

| Trigger | Scope |
|---|---|
| `export_expenses()` or `/export` | The active trip; with no active trip, the general ledger. This matches `get_balances` with no `trip`. |
| `export_expenses(trip="Japan")` or `/export Japan` | That trip (by name or id, scoped to the chat, as `get_balances(trip=…)`). Closed trips work. |
| `export_expenses(general=true)` or `/export general` | The general ledger: expenses and settlements with no trip. |

- It doesn't need confirmation, because it writes nothing.
- Access follows the existing rules. In a group, the group's ledger is used. In a DM, the group comes from `group_for_dm`, which only returns a group the user belongs to. Without one, the usual "can't identify your group" error applies. The file goes to the chat the request came from (the DM, for a DM request) and its thread.
- If the platform can't send files, the tool says so: `This chat platform can't send files.`
- The reply after sending is `Sent <filename>: <n> expenses, <m> settlements.`

## Platform support

`chatcore` can't send files today. `ChatPlatform` has `send`, `confirm`, `download` and so on, but nothing outgoing for files. Polls set the precedent for optional platform features: `chatcore/polls.py` defines a separate `runtime_checkable` `PollPlatform` protocol, and plugins check `isinstance(platform, PollPlatform)`.

Export follows the same pattern. A new `chatcore/files.py` defines `FilePlatform` with one method:

```python
async def send_file(
    self, chat: ChatRef, filename: str, data: bytes, *,
    caption: str | None = None, thread_id: str | None = None,
) -> MessageRef
```

`TelegramPlatform` implements it with `bot.send_document`, and `FakePlatform` records the calls. The required `ChatPlatform` contract doesn't change, so the console adapter and other implementers aren't affected. They simply don't offer export.

## File layout

The export is one UTF-8 CSV file, written with a BOM so Excel shows names correctly, using the `csv` module's default dialect. Each row's first column, `record`, says what kind of row it is, so a spreadsheet filter on that column separates the sections. Every row has the same 14 columns:

```
record, trip_id, trip_name, id, date, description, person, counterparty,
currency, amount, fx_rate, settle_currency, amount_in_settle, note
```

| `record` | One per | Fields used |
|---|---|---|
| `expense` | Expense | `id` (full UUID), `date` (spent_on), `description`, `person` = payer, `currency`, `amount`, plus `fx_rate`, `settle_currency` and `amount_in_settle` in convert trips. `note` = `no FX rate` when the rate is missing. |
| `share` | Share of an expense | Same `id`, `date` and `description` as its expense. `person` = who owes the share, `amount` = the share, and the expense's `fx_rate` and `settle_currency` plus the share's `amount_in_settle` in convert trips. |
| `settlement` | Settlement | `id`, `date` (creation date in the plugin's timezone), `person` = who paid, `counterparty` = who received, `currency`, `amount`. In convert trips, a settlement not in the settle currency gets `note` = `not in settle currency; excluded from balance`, because `convert_balances` skips it. |
| `balance` | Person and currency in the balance result | `person`, `currency`, `amount` = the net balance (positive means owed money, negative means owes). |
| `transfer` | Suggested payment | `person` pays `counterparty` `amount` in `currency`, from `simplify_balances`. |
| `no_fx_rate` | Convert-trip expense with no rate | `id`, `date`, `description`, `person` = payer, `currency`, `amount`, `note` = `excluded from balance until set_fx_rate`. |

Rows are written in this order: expenses, each followed by its shares; then settlements, balances, transfers, and the expenses with no FX rate. Within each group the order is fixed, so the same ledger always gives the same file:
- expenses by `(date, created_at, id)`;
- shares by person, then by user key;
- settlements by `(created_at, id)`;
- balances by currency, then person;
- transfers in `simplify_balances` order, per currency.

`trip_id` and `trip_name` are filled on every row. For the general ledger, `trip_id` is empty and `trip_name` is `General ledger`.

### Numbers

- `amount` is a plain decimal string in major units, with exactly the currency's exponent: `12.50` SGD, `3000` JPY, `-4.17` for a negative balance. It never uses scientific notation, a symbol or a thousands separator.
- `fx_rate` is the stored `fx_rate_to_settle`, normalised, with trailing zeros dropped and no exponent.
- `amount_in_settle` is the **exact, unrounded** value `amount × rate`, in settle-currency major units. `convert_balances` also sums unrounded contributions and only rounds each person's total (half-even, then it moves any one-unit remainder). So summing a person's `amount_in_settle` (`expense` rows positive, `share` rows negative), adding settlements, and rounding half-even gives the `balance` row. The rare exception is a total that absorbed the remainder, which moves by one minor unit; the `balance` rows are the authority.

### Balances match `get_balances` exactly

The scope selection and the balance arithmetic move into a shared helper in `balances.py`, `ledger_balances(trip, expenses, settlements)`. It returns the per-currency groups and the unconverted expenses, which is what `_balance_text` computes today. Both `_balance_text` and the export call it, so they can't drift apart. `_balance_text`'s output doesn't change. `balance` rows list every person and currency in the result, zeros included, so the file shows everyone who took part. `get_balances` hides zeros, so the comparison drops them.

### Soft-deleted expenses

Soft-deleted expenses are **excluded**. `ledger.expenses` already leaves them out, the balances ignore them, and the exported totals then match what the bot shows. Showing deleted rows would need a new repository query, and it would put rows in the file that don't count.

### Names

`person` and `counterparty` use display names from the chat's members. A user key with no member row (someone who left and was purged) becomes `Former member 1`, `Former member 2` and so on, numbered by sorted key within the file. Raw user keys such as `tg:12345` never appear.

### Formula injection

Descriptions, trip names and display names come from users. Any text cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'`, so spreadsheets show it as text. Numeric cells are generated by the exporter from integers and Decimals, so they skip the escape and a negative balance stays `-4.17`. Embedded commas, quotes and newlines are handled by the `csv` module's quoting.

### Filename and caption

- The filename is `expenses-<slug>-<YYYY-MM-DD>.csv`. The slug is the trip name lower-cased with runs of non-`[a-z0-9]` characters turned into `-`, trimmed to 40 characters, or `trip` if that leaves nothing. The general ledger uses `general`. The date is today in the plugin's timezone.
- The caption is `<trip name or General ledger>: <n> expenses, <m> settlements. Deleted expenses are not included.`

## Why these choices

- **One file with a `record` column instead of several files.** Telegram sends one document per message, and one file is easier to forward and to check in one sheet. The fixed header also lets a test (or a person) parse the file back and recompute the balances.
- **One row per share instead of a column per person.** The columns stay fixed whoever joins the trip, and a pivot table gives the per-person view.
- **Unrounded `amount_in_settle`.** Rounding each row would make the hand sum differ from the bot's balance by a cent in normal cases. Exact values make the check work.
- **An optional `FilePlatform` protocol instead of a new required `ChatPlatform` method.** This follows `PollPlatform`. It avoids changing every adapter and fake, and the plugin degrades cleanly where files aren't supported.

## Out of scope

- Exporting several trips or the whole chat in one file.
- Spreadsheet formats other than CSV (xlsx), and charts.
- Including soft-deleted expenses or an audit trail of edits.
- File sending in the console adapter.
