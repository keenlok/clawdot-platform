# Editing the active trip (`update_trip`)

Requested 2026-10-31. A trip's name, countries, currencies and settle mode were fixed
after creation. The reusable feature lets a user propose changes to an active trip with
confirmation and rejects changes that would invalidate recorded expenses. Automatic FX
already uses the expense date (`fx.get_rate`: trip override, then Frankfurter, then
open.er-api as an approximate fallback). Production travel details remain private.

## Behaviour

A new write tool, `update_trip`, edits the **active** trip in the current chat (or the DM's resolved group, as the other tools do). It works like the other trip writes. The tool returns a pending action, the requester presses Confirm, and the commit applies the change.

Arguments (all optional, at least one must change something):

| Argument | Effect |
|---|---|
| `name` | Rename. Stripped; 1–100 characters. |
| `add_countries` | Append countries. Each must map through `currency_for_country`; its currency is added if missing. |
| `remove_countries` | Remove countries (case-insensitive match against stored names). Their currencies are **not** removed automatically. |
| `add_currencies` | Append ISO currency codes directly, e.g. HKD. Validated with `currency_exponent`. |
| `remove_currencies` | Remove currencies. Refused while any non-deleted expense or settlement in the trip uses that currency. |
| `settle_mode` | `convert` or `per_currency`. Refused once the trip has any non-deleted expense or any settlement. |
| `settle_currency` | ISO code. Same rule as `settle_mode`. |

Rules:
- Countries and currencies keep their order, new items go last, duplicates are dropped (countries case-insensitively, currencies upper-cased).
- A trip must keep at least one country and one currency.
- Removing a country that isn't on the trip, or a currency that isn't on it, is an error. Adding one that is already there is a silent no-op. If nothing would change, the tool returns an error ("Nothing to change").
- No active trip: error. Closed trips can't be edited.
- Anyone in the chat may propose it, as with `start_trip`; only the requester can confirm.

## Why these choices

- **Deltas, not final values, in the payload.** The commit re-reads the trip row (`FOR UPDATE` in Postgres) and applies the adds and removes to the current values. Two pending edits then can't erase each other's changes. The payload carries `trip_id`, so a confirmation made after the trip has ended or been replaced is refused.
- **Usage checks are repeated at commit.** An expense could be added between proposing and confirming.
- **Removing a country doesn't remove its currency.** Several countries can share a currency (EUR), and expenses may use it. The bot can pass `remove_currencies` explicitly.
- **Settle changes only on an empty trip.** Existing expenses store `fx_rate_to_settle` against the old settle currency, and settlements are recorded in it. Re-pricing history is out of scope. A user who set up the trip wrongly can still fix it before the first expense.
- **No migration.** `trips.countries`, `trips.currencies`, `settle_mode` and `settle_currency` already exist (migration 0001).

## Confirmation summary

It shows old → new for each changed field only, e.g.:

```
Update trip: China
Countries: China → China, Hong Kong
Currencies: CNY → CNY, HKD
```

The commit reply is `Updated trip <name>.` followed by the same lines computed from the committed row. `get_trip` reads the row and so reflects the change.

## Small fixes in the same change

1. **FX wording.** The `add_expense` and `set_fx_rate` tool descriptions and `apps/sb_bot/src/sb_bot/prompts/system.md` say that in convert trips the rate is fetched automatically for the expense date (Frankfurter, then open.er-api as an approximate fallback). They also say `set_fx_rate` is only an optional override, e.g. to match a money changer. It applies to later expenses in that currency and fills expenses that have no rate.
2. **Off-trip currency note.** When an active trip's `add_expense` uses a currency not in the trip's `currencies`, the summary adds: `Note: HKD is not one of this trip's currencies (CNY). Use update_trip to add it.` The expense is still allowed.
3. **FX line in convert summaries.** When the expense currency differs from the settle currency, the summary shows `FX: 1 HKD = 0.1712 SGD`, with ` (approximate)` when the fallback was used. When no rate could be found, it shows `FX: no rate found yet; set one with set_fx_rate`. This replaces the bare `FX: approximate rate` line, so the user sees that the rate was fetched.

## Out of scope

- Editing closed trips, re-pricing existing expenses, or changing the settle currency of a trip with history.
- Editing individual expenses.
