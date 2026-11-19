# Itinerary lookup fixes: duplicate `gid` links and empty history search

Status: Planned, [PR #65](https://github.com/keenlok/clawdot-platform/pull/65). Owner-approved 2026-11-19. Claude plans, Codex (`gpt-6-luna`) executes,
Codex (`gpt-6.1-sol`) reviews, Claude does the final review.

## Observed failure (read-only production inspection, 2026-11-19)

In a downstream bot's group, a member posted a Google Sheets itinerary link and later asked
for "tomorrow's itinerary" several times. Each new turn, the bot said it had no itinerary.

1. **Standard Sheets share links are rejected.** Sheets' share link carries the tab id twice
   with the same value: `…/edit?gid=464258837#gid=464258837`. `parse_google_ref` in
   `packages/plugins/google/src/chatcore_google/refs.py` rejects any link with more than one
   `gid`, so `approved_source_ref` reports "The referenced human message does not contain that
   file link." Attaching the sheet to the trip as its itinerary failed for that reason.
2. **History search returns nothing and never falls back.** The deployment's Elasticsearch
   index was created by dynamic mapping, not the chatcore template, so `chat_key` is a `text`
   field and every `term` chat filter matches zero documents. `_search_chat` in
   `packages/plugins/search/src/chatcore_search/plugin.py` returns ES's empty list as the
   answer. The SQL `ILIKE` fallback only runs when ES raises. A human message containing
   "itinerary" and the link was in `messages`, and SQL would have found it.

The index template itself is a deployment fix (create templates, reindex), out of scope here.
This plan makes the framework tolerate the link form and degrade to SQL when ES finds nothing.

## You own

- `packages/plugins/google/src/chatcore_google/refs.py`
- `packages/plugins/google/tests/test_refs_render.py`
- `packages/plugins/search/src/chatcore_search/plugin.py` (only `_search_chat` and a new
  private helper beside it)
- `packages/plugins/search/tests/test_search.py`
- `CHANGELOG.md` (`Unreleased` section only)

You may **not** edit `packages/core/`, `chatcore.store.es`, any `gates.py` or `permissions.py`,
any migration, `pyproject.toml` or `uv.lock`. Ask in your report if one must change.

## 1. Accept a repeated `gid` with the same value (`refs.py`)

In `parse_google_ref`, collect `gid` values from the query and fragment as now. Accept the link
when all collected values are identical (after the existing ASCII-digit check on each). Still
raise `ValueError` when the values differ (`?gid=1#gid=2`) or when one source repeats with
different values. The returned `GoogleRef.gid` is that single value. Nothing else changes: the
host, scheme, path, `tab` rules and error messages stay the same.

## 2. SQL fallback when the search index returns no hits (`chatcore_search/plugin.py`)

In `_search_chat`:

- If `ctx.stores.search` returns a non-empty list, return it unchanged (current behaviour).
- If it returns an empty list, run the existing SQL fallback, with the same filters (chat key,
  `user_keys`, `since`, `include_bot_answers`, `limit`). If the fallback returns rows, log one
  WARNING with no message text or query: `"search index returned no hits; sql fallback
  matched chat=%s rows=%d"` (chat key and row count only). Return the fallback hits. If it
  returns none, return the empty list.
- If ES raises, keep the current warning and fallback.
- If `ctx.stores.db` is None after an empty ES result, return the empty list (not None).

Make the SQL fallback match words, not only the whole phrase. Move the SQL into a private
helper. It first runs the current whole-query `ILIKE`. If that returns nothing and the query
has two or more words, it runs once more, matching any word of at least 3 characters (at most
6 words, `ILIKE` with the same escaping, combined with `OR` through `text ILIKE ANY($2)` and
a `text[]` parameter of `%word%` patterns). That pass orders by the number of matched words,
descending, then `sent_at DESC`. Keep every query parameterised. Never interpolate query text
into SQL. Hit shape, scores and source keys must stay what the current fallback returns, so
callers and `read_chat_history` rendering don't change.

## Tests

`test_refs_render.py`:
- `…/edit?gid=464258837#gid=464258837` parses with gid 464258837.
- `?gid=1#gid=2` raises. `#gid=1&gid=1` (same value twice in one place) parses. A non-digit
  value still raises.
- `approved_source_ref` accepts a human message whose text holds the duplicate-`gid` link, when
  the requested url is either that link or the link with the query and fragment removed.

`test_search.py` (use the existing fakes in that file; no network):
- ES returns `[]`, the DB has a matching human message: `read_chat_history` returns it, and the
  WARNING is logged with no message text.
- ES returns hits: the DB is not queried.
- ES raises: fallback runs (existing behaviour still holds).
- Multi-word query whose phrase doesn't match but one word does: the word pass returns the
  message. A message matching more words ranks first.
- Bot answers stay excluded unless `include_bot_answers=True` in both passes; `from_user` and
  `since` filters apply in both passes.
- `%` and `_` in the query are escaped in the word pass.

## Done when

- `uv run ruff check packages services apps tests .github/scripts` is clean, and
  `uv run ruff format --check` passes on the files you changed.
- `uv run pytest -q -m "not browser and not sandbox_integration"` passes. The agent request
  snapshot and the skill catalog and routing-set tests pass unchanged.
- `CHANGELOG.md` `Unreleased` has a `Fixed` entry for each of the two fixes.
- Left for later, do not attempt: ES template creation or reindexing, memory `recall` fallback,
  the `itinerary_summary` format limit, and persisting `google_read_sheet` content.
