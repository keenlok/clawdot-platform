# Export the expense ledger as CSV (`export_expenses`, `/export`)

Read first:
- `docs/superpowers/specs/2026-10-31-expense-export-design.md` (the design; this plan implements it, and its "File layout" section is the exact file format);
- `ARCHITECTURE.md` §4 ("Expense splitting");
- `packages/plugin-expenses/src/chatcore_expenses/{plugin,repository,balances,currency}.py`;
- `packages/core/src/chatcore/{platform,polls,testing}.py` (`PollPlatform` is the pattern for an optional platform feature);
- `packages/adapter-telegram/src/chatcore_telegram/__init__.py` (`send_poll` and `_send_with_retry`);
- `packages/adapter-telegram/tests/test_telegram.py` (`test_native_poll_send_and_stop_keep_topic_and_explicit_anonymity` shows how the bot is mocked);
- `packages/plugin-expenses/tests/{test_plugin,test_pg_integration}.py`;
- `tests/test_tool_audit.py`.

Environment: run `export UV_CACHE_DIR="$PWD/.uv-cache"` before anything that goes through `uv`.
- Tests: `uv run pytest -q -m 'not integration'`.
- Lint: `uv run ruff format . && uv run ruff check .`.
- The Postgres integration test skips without a database; the reviewer runs it.

## You own

- new `packages/core/src/chatcore/files.py`
- `packages/core/src/chatcore/testing.py`: only `FakePlatform` (add `send_file` and a `sent_files` list)
- `packages/adapter-telegram/src/chatcore_telegram/__init__.py`: only a new `send_file` method
- new `packages/plugin-expenses/src/chatcore_expenses/export.py`
- `packages/plugin-expenses/src/chatcore_expenses/balances.py`: add `ledger_balances` only; don't change the existing functions
- `packages/plugin-expenses/src/chatcore_expenses/plugin.py`
- `apps/sb_bot/src/sb_bot/prompts/system.md`: one sentence in the expenses paragraph
- tests:
  - new `packages/plugin-expenses/tests/test_export.py`;
  - `packages/plugin-expenses/tests/test_pg_integration.py` (add one new test function);
  - `packages/adapter-telegram/tests/test_telegram.py` (add one test);
  - `packages/core/tests/` (add a test for `FakePlatform.send_file` only if a natural file exists; otherwise skip it).

You may **not** edit:
- the `ChatPlatform` protocol in `platform.py`, `Capabilities`, or anything else in `packages/core`;
- `repository.py` (no repository method or SQL changes, and no migration);
- `currency.py`, `fx.py`, or the console adapter.

If one of these must change, stop and explain why in your report.

## 1. `FilePlatform` (core/files.py)

Model it on `chatcore/polls.py`:

```python
"""Optional outgoing-file capability, independent of the required platform contract."""


@runtime_checkable
class FilePlatform(Protocol):
    async def send_file(
        self,
        chat: ChatRef,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        """Send ``data`` as a document; raise PlatformError containing only the error class name."""
        ...
```

`FakePlatform` gets a `sent_files: list[SentFile]` field and a `send_file` method that appends to it and returns a fresh `MessageRef`, like `send`. Use a dataclass `SentFile(chat, filename, data, caption, thread_id, ref)` next to `Sent`.

## 2. Telegram `send_file`

Add it to `TelegramPlatform`, after `send_poll`:

```python
msg = await self._send_with_retry(
    self._application.bot.send_document,
    chat_id=chat.id,
    document=data,
    filename=filename,
    caption=caption,
    message_thread_id=int(thread_id) if thread_id else None,
)
return MessageRef(chat, str(msg.message_id), thread_id)
```

- The caption is plain text: no parse mode and no entities.
- Reject a payload over `MAX_FILE_BYTES` with `PlatformError("FileTooLarge")` before calling the bot.
- The test checks `isinstance(platform, FilePlatform)` and the exact `send_document` call, including the topic (`message_thread_id=7`) and `thread_id=None` → `message_thread_id=None`.

## 3. `ledger_balances` (balances.py)

Move the arithmetic out of `ExpensesPlugin._balance_text` into a pure function:

```python
def ledger_balances(
    trip: Mapping[str, Any] | None,
    expenses: Iterable[Mapping[str, Any]],
    settlements: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, int]], list[Mapping[str, Any]]]:
    """Per-currency net balances for a ledger scope, plus convert-trip expenses with no rate."""
```

- It holds exactly the code in `_balance_text` from `if trip and trip["settle_mode"] == "convert":` to `unconverted = []`.
- It returns `(groups, unconverted)`, where `groups` maps currency → {user_key: minor units} with zeros kept.
- `_balance_text` then calls it. Its output must stay byte-for-byte the same, and the existing tests prove that.

## 4. The CSV builder (export.py)

Write one pure function, with no I/O and no ledger access:

```python
@dataclass(frozen=True, slots=True)
class LedgerExport:
    filename: str
    data: bytes
    expense_count: int
    settlement_count: int

def build_export(
    trip: Mapping[str, Any] | None,
    expenses: Sequence[Mapping[str, Any]],
    settlements: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
    *,
    today: date,
    timezone: tzinfo,
) -> LedgerExport:
```

- `trip` is `None` for the general ledger.
- `expenses` and `settlements` are exactly what `ledger.expenses(...)` and `ledger.settlements(...)` return, with shares as a list of `{user_key, amount}`.
- `names` maps user key → display name.

Follow the design's "File layout" section exactly:
- the header, in this exact order: `record,trip_id,trip_name,id,date,description,person,counterparty,currency,amount,fx_rate,settle_currency,amount_in_settle,note`;
- the record kinds and the fields each one fills; leave every other cell empty;
- the row order and the sort keys.

Details:
- Encode the output as `utf-8-sig`. Write it with `csv.writer` on an `io.StringIO` using the default dialect (CRLF line endings).
- `amount`: write a helper `_major(minor: int, currency: str) -> str` that uses `currency_exponent`. It gives exactly `exponent` decimals, a leading `-` for negatives, and no exponent notation, e.g. `_major(1250, "SGD") == "12.50"`, `_major(3000, "JPY") == "3000"`, `_major(-417, "SGD") == "-4.17"`.
- `fx_rate`: `Decimal(str(rate))`, normalised and formatted with `format(value, "f")`, so `Decimal("0.0089000")` becomes `0.0089` and `Decimal("100")` becomes `100`. The rate can arrive as `str` (memory ledger) or `Decimal` (Postgres), and both must give the same text.
- `amount_in_settle`: only in convert trips with a rate. It is the exact `Decimal(minor) * rate * 10**(settle_exp - exp)` (the same expression as `convert_balances`), converted to major units by dividing by `10**settle_exp`. Format it with `format(value.normalize(), "f")`, but pad to at least `settle_exp` decimals, so `8.9` SGD becomes `8.90`.
- `fx_rate`, `settle_currency` and `amount_in_settle` stay empty in per-currency trips and in the general ledger.
- `balance` rows come from `ledger_balances(trip, expenses, settlements)`, one row per currency and user, zeros included. Write them in currency order, then display-name order with the user key as the tie-break.
- `transfer` rows come from `simplify_balances` over each currency's non-zero values, the same input as in `_balance_text`.
- `no_fx_rate` rows come from the `unconverted` list.
- `date`:
  - `spent_on.isoformat()` for expenses and shares (`spent_on` may be a `date` or an ISO `str`);
  - `created_at.astimezone(timezone).date().isoformat()` for settlements.
- `names`: a key not in `names` becomes `Former member N`, numbered 1, 2, … in sorted-key order over all unknown keys in this export. Never write a raw user key.
- Escaping: `_text(value: str) -> str` adds a leading `'` when the value starts with any of `= + - @ \t \r`.
  - Apply it to `trip_name`, `description`, `person`, `counterparty` and `note`.
  - Never apply it to the numeric or id columns.
- The filename and caption follow the design.
  - Put the caption in a module-level helper, `export_caption(trip, expense_count, settlement_count) -> str`, so the plugin can use it.
  - For `slug`, the trip name is user text. The slug regex alone must make it filesystem-safe: only `[a-z0-9-]`.

## 5. Plugin wiring (plugin.py)

`ExpensesPlugin._export(ctx, *, trip=None, general=False) -> ToolResult`:

1. `chat = await self._chat(ctx)`. If it's `None`, return the same "can't identify your group" error as the other tools.
2. If `not isinstance(ctx.platform, FilePlatform)`, return `ToolResult.error("This chat platform can't send files.")`.
3. Validate the arguments: `trip` must be `None` or a non-empty `str`, and `general` must be a `bool`. Giving both a trip and `general=True` is an error: `"Choose a trip or the general ledger, not both."`
4. Pick the scope:
   - `general` → `None` (general ledger);
   - `trip` → `await ledger.trip(chat, trip)`, or `ToolResult.error("Trip not found in this chat.")`;
   - neither → `await ledger.active_trip(chat)`, falling back to `None`.
5. Load the rows exactly as `_balance_text` does:
   - `ledger.expenses(chat, trip_id=selected["id"] if selected else None, general_only=selected is None, limit=None)`;
   - the matching `ledger.settlements(...)`.
6. `names` comes from `self._members(ctx, chat)`, the resolved group, not the DM.
7. Call `build_export(...)` with `today=datetime.now(self.timezone).date()` and `timezone=self.timezone`.
8. `await ctx.platform.send_file(ctx.chat, export.filename, export.data, caption=export_caption(...), thread_id=ctx.thread_id)`. The file goes to `ctx.chat`, the chat the request came from. Catch `PlatformError` and return `ToolResult.error(f"Couldn't send the file ({exc}).")`.
9. Log `expense export chat=%s expenses=%d settlements=%d bytes=%d` with the resolved chat key. Log no names, descriptions or file contents.
10. Return `ToolResult(f"Sent {export.filename}: {n} expenses, {m} settlements.")`.

Register the tool after `get_balances`. It is read-only, so don't pass `writes=True`. The schema has `trip` (string) and `general` (boolean). Description:

> Send the ledger as a CSV file for checking in a spreadsheet: every expense with its shares, FX rate and converted amount, settlements, each person's net balance (same as get_balances) and expenses missing an FX rate. Defaults to the active trip, else the general ledger; pass trip to pick a trip by name, or general=true for expenses outside any trip.

Add a `/export` command after `expenses`, with the description `Send the ledger as a CSV file (/export [trip name | general]).` Its handler builds a `ToolContext` like `_command_balance`. Strip the command text, then:
- empty → `_export(tool_ctx)`;
- `general` (case-insensitive) → `general=True`;
- anything else → `trip=<text>`.

Return the result's `content`, which covers both success and error text.

## 6. System prompt

In the expenses paragraph of `apps/sb_bot/src/sb_bot/prompts/system.md`, add one sentence: when someone wants to check the ledger in a spreadsheet or see every expense, call `export_expenses` rather than pasting a long list.

## Tests

Use `packages/plugin-expenses/tests/test_export.py` unless the test says otherwise. Use `MemoryLedgerRepo`, `FakePlatform` and `memory_stores` or the `context` fixture pattern from `test_plugin.py`. Write expenses through `ledger.commit(...)` with explicit payloads, so no FX API is called. Parse output with `csv.DictReader(io.StringIO(data.decode("utf-8-sig")))`.

1. **Balances match `get_balances`.** Use a convert trip (SGD settle) where two JPY expenses with awkward rates (e.g. `0.0089123`) force the remainder allocation in `convert_balances`, plus one SGD settlement and one expense with no rate.
   - Assert that the `balance` rows, parsed back to minor units, equal `ledger_balances(...)`.
   - Assert that the non-zero ones, formatted as `_balance_text` formats them, appear in the `get_balances` tool output. Each line is `- <name> is owed|owes S$x.xx SGD`; compare against the actual tool result text.
   - Do the same for a per-currency trip with JPY and KRW, and for the general ledger.
2. **The file recomputes its own balances.** From the `expense`, `share` and `settlement` rows alone, rebuild minor-unit dicts in the shape `convert_balances` accepts and call it. The result must equal the `balance` rows. For the per-currency case, plain sums must equal them.
3. **No cross-trip leakage.**
   - Set up trip A (ended), then trip B (active), plus a general-ledger expense and a settlement in each scope.
   - The trip B export (default) contains only trip B ids and descriptions, and every row's `trip_id` is B's id.
   - `trip="A"` contains only A's.
   - `general=True` contains only the general expense, with `trip_id` empty and `trip_name` `General ledger` on every row.
   - The same trip name in another chat isn't found ("Trip not found in this chat.").
4. **Injection escaping.**
   - A description and a member display name each starting with `=`, `+`, `-`, `@`, `\t` and `\r` come out with a leading `'`.
   - A description containing a comma, a quote and a newline round-trips through `DictReader` unchanged.
   - Negative `balance` amounts stay `-x.xx` with no `'`.
5. **Names.** No row contains any user key (`fake:…`). A share for a key with no member row shows `Former member 1`.
6. **Number formatting.** JPY has no decimals and SGD has two. `amount_in_settle` is exact, e.g. 1000 JPY at `0.0089123` gives `8.9123`. Pg-style `Decimal("0.00891230")` and memory-style `"0.0089123"` give the same `fx_rate` text.
7. **Soft-deleted expenses excluded.** Delete an expense through `commit("delete_expense", …)`. It is absent from every row, and the balances still match `get_balances`.
8. **Plugin behaviour.**
   - The default scope sends one file to `ctx.chat` with `ctx.thread_id`, with the expected filename shape and caption.
   - The reply text matches the format.
   - A DM resolves the group through `group_for_dm`, but the file goes to the DM chat.
   - A DM without a resolver gives the group error.
   - A platform object without `send_file` gives "This chat platform can't send files." (use a minimal stub class or a `FakePlatform` subclass with the attribute removed).
   - `trip` plus `general=True` is an error.
   - A non-bool `general` or non-str `trip` is an error.
   - The `/export`, `/export general` and `/export <name>` command paths work.
9. **Memory and Postgres give the same file.** This is an integration test in `test_pg_integration.py`, as a **new** function using the same schema-per-test setup and skip logic as the existing one.
   - Apply the same commits to a `MemoryLedgerRepo` and a `PostgresLedgerRepo`: a convert trip with a rate, a missing rate, two shares, and a settlement. Give each expense a different `spent_on`, so the ordering doesn't depend on ids.
   - Build both exports with the same `names`, `today` and `timezone`.
   - Parse both, blank the `trip_id` and `id` columns (UUIDs differ), and assert the rows are equal.
   - Also assert that the Postgres export's `balance` rows equal `ledger_balances` over the Postgres rows.
10. **Telegram:** the `send_file` test from §2, plus the size-limit refusal.
11. **Audit:** `tests/test_tool_audit.py` must still pass unchanged, since `export_expenses` takes no forbidden argument names and isn't a write.

## Done when

- `uv run pytest -q -m 'not integration'` passes. One test, `plugin-polls test_real_core_button_confirmation_preserves_topic`, may fail; it already fails on `main`, so don't touch it.
- `uv run ruff format --check . && uv run ruff check .` is clean.
- Existing `get_balances`/`/balance` outputs are unchanged.
- The integration test exists and skips cleanly without Postgres.
- Deliberately left out: console-adapter file sending, xlsx, multi-trip exports, deleted-row audit, and any deploy.
