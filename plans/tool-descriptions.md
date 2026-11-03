# Stage 0a: tool and argument descriptions

Metadata-only pass over every LLM tool in the SBot composition. **No behaviour changes**: handler
code, gates, validation, schemas' `type`/`enum`/`minimum`/`required`, tool names and tool classes
(`writes`, `direct`, `direct_after`, `admin_only`) stay exactly as they are. Only `description`
strings change (tool descriptions, plus new or edited JSON-schema `description` keys on
parameters), together with the legacy prompt and one wiring flag (§4).

Setup: `export UV_CACHE_DIR="$PWD/.uv-cache"` then `uv sync --all-packages`.
Test: `uv run pytest -q`. Lint: `uv run ruff check . && uv run ruff format --check .`.

## You own
- Tool definitions (description strings and parameter `description` keys only) in:
  `packages/core/src/chatcore/display_time_plugin.py`,
  `packages/plugin-stocks/src/chatcore_stocks/__init__.py`,
  `packages/plugin-browse/src/chatcore_browse/plugin.py`,
  `packages/plugin-web/src/chatcore_web/__init__.py`,
  `packages/plugin-research/src/chatcore_research/plugin.py`,
  `packages/plugin-search/src/chatcore_search/__init__.py`,
  `packages/plugin-memory/src/chatcore_memory/plugin.py`,
  `packages/plugin-expenses/src/chatcore_expenses/plugin.py` (**description strings only** —
  another branch edits this file's logic; touch nothing else in it),
  `packages/plugin-jobs/src/chatcore_jobs/plugin.py`,
  `packages/plugin-polls/src/chatcore_polls/plugin.py`,
  `packages/plugin-extensions/src/chatcore_extensions/plugin.py`.
- `apps/sb_bot/src/sb_bot/prompts/system.md` (append policy moved out of descriptions).
- `apps/sb_bot/src/sb_bot/app.py`: only the one-argument wiring change in §4.
- New test `apps/sb_bot/tests/test_tool_descriptions.py`; existing tests that assert old
  description text may be updated to the new text (say which in your report).

You may **not** edit `packages/core/src/chatcore/tools/registry.py`, `agent.py`, any handler
logic, or any pending/direct-write machinery.

## Write classes (get this right in wording)
- `writes=True, direct=True` (memory `remember`/`forget`, expense add/update/delete/restore/undo,
  `update_trip_members`, `update_trip`, reminder/watch create/cancel/restore, `create_poll`): apply
  immediately and echo, **but fall back to a confirmation click after the turn read untrusted
  content**. Wording pattern: "Applies immediately (may ask the user to confirm after web or file
  content); <undo tool> is the undo." Never claim it *always* applies without confirmation, and
  never claim it always needs confirmation.
- `writes=True` without `direct` (`settle_up`, `set_fx_rate`, `start_trip`, `end_trip`,
  `activate_trip`, `reopen_trip`, `set_default_trip`, `configure_jobs`,
  `configure_rendered_fetch`, `request_extension`, `set/reset_display_time_zone` — check each
  tool's actual flags in code before wording it): "Requires confirmation." is correct.
- Remove every "Every write requires confirmation." (a false general statement).

## 1. Description rules
- Every tool description ≤ **300 characters** (count the final joined string). Say what it is for
  and, where it overlaps, when *not* to use it as "Not for X → other_tool".
- Policy essays leave the description and move verbatim-in-meaning to `system.md` (§3).
- Keep the provenance rule in `fetch_web_page`/`fetch_rendered_page` (posted or search-returned
  URLs only).

## 2. Per-tool changes
Overlap clusters (each listed tool gets a "not for …" clause pointing at the others):
- **Recall cluster.** `read_chat_history` = what people *said* (not for saved facts → recall; not
  for money → list_expenses/get_balances). `recall` = saved facts (not for chat messages →
  read_chat_history). `list_expenses` = recent expense items (not for totals/who-owes →
  get_balances; full ledger file → export_expenses). `get_balances` = who owes whom (not for item
  lists → list_expenses). `export_expenses` = CSV of the full ledger.
- **Web cluster.** `search_web` = one quick fact or finding URLs (not for multi-source or comparison
  questions → research). `fetch_web_page` = read one known URL. `research` = multi-source or
  comparison questions, call once (not for a single fact → search_web). `fetch_rendered_page`:
  see §4.
- **Images.** `read_attachment` = an earlier message's image or a document (not for the current or
  replied-to image, which the model sees directly when it has vision; not for receipts to record →
  read_receipt). `read_receipt` = receipt to record, then add_expense.
- **Settings.** `configure_jobs`, `configure_rendered_fetch`: one pattern — "Request turning
  <feature> on/off for this group. Only a current setup manager, group creator or administrator
  can request; the requester confirms." (Verify against `request_configure` in
  `chatcore_jobs/service.py` and the browse handler; the old configure_jobs text "only current
  group admins can confirm" is wrong.)
- **Time.** `get_current_time` = one-off time questions and conversions (does not save anything);
  `set_display_time_zone`/`reset_display_time_zone` = 1–2 sentences: save/reset this chat's
  display time zone on an explicit request; not for one-off conversions → get_current_time.

Other tools:
- `stock_analysis`, `stock_prices`, `stock_list`, `stock_digest`: add "Stored data, not live
  quotes; not financial advice." Ticker format, `days` units, `lang` values as argument
  descriptions (read the handlers for real formats/defaults).
- `create_reminder`: one-shot only; recurring reminders are not supported. `local_time` and
  `timezone` formats as argument descriptions (read the handler/parser for the accepted format).
- `cancel_*` / `restore_*`: `job_id` "from list_reminders / list_website_watches or the echo".
- `create_website_watch`: not for one-off reads → fetch_web_page; `rendered` for JS-heavy sites;
  `interval_seconds` minimum from the code/settings (state it as "at least the group's minimum
  interval" if it is configurable).
- `add_expense` (452 chars now): cut to ≤ 300. FX provider names (Frankfurter, open.er-api) and
  trip-selection detail move to `system.md`. Argument descriptions: amount units, `split_with`
  semantics, `split_type` values.
- `export_expenses` (352): purpose + when; column detail moves to `system.md`.
- `start_trip` (367): trim; detail already in `system.md` — add whatever isn't.
- `request_extension`/`list_extensions`/`run_extension`: replace "pure JSON transformation" with
  a concrete example (e.g. "a reusable calculation such as splitting a bill by weights: JSON in,
  JSON out, no network or chat access").
- `build_search_link`: **do not rename.** Make the description say it is for rental-car searches
  only.
- `read_chat_history`: make the scope precise from the handler (this chat, and from a DM the
  groups the user belongs to — verify), `since` format, `from_user` form, `limit` default/max.
- `search_web`: `max_results` default/max.
- Every other tool: keep the description unless it breaks a rule above; still add argument
  descriptions.

## 3. Arguments
Every property of every tool's `parameters` (recursively: nested object `properties` too) gets a
`description`: format (YYYY-MM-DD, HH:MM, IANA zone, ISO 4217, decimal string), units (seconds,
days), default when the handler has one, and where IDs come from (which list tool or echo).
Read the handler for each fact; don't invent defaults. Keep each ≤ 300 chars, ideally < 120.

## 3b. `system.md`
Every policy sentence removed from a description must land in
`apps/sb_bot/src/sb_bot/prompts/system.md` in the same change, unless the same guidance is
already there (then leave it). Add short paragraphs next to the related existing paragraph
(time zone, expenses/FX/export, web). In your report, list each removed sentence and where it
landed (or which existing line already covers it). Known items: the ~600-char
set/reset_display_time_zone scope policy (scope is the current reply's scope; group changes need
a fresh native group-admin check; personal preference in a group → tell them to ask in DM;
one-off questions → get_current_time), FX providers and "don't ask the user for a rate",
export columns, start_trip details, add_expense trip-selection details.

## 4. `fetch_rendered_page`, registry-conditional
`fetch_web_page` exists only when `TAVILY_API_KEY` is set; `fetch_rendered_page` only with
`BROWSER_RENDER_URL`. Add a keyword argument to `BrowsePlugin.__init__` (e.g.
`web_fetch_available: bool = False`) and pass `web is not None` from `build_app` in `app.py`.
- When true: "… Use only after fetch_web_page returned empty or JS-only text."
- When false: describe it as the way to read a posted or search-returned URL directly.
Handler checks stay byte-for-byte unchanged; it must keep working directly (rendering on, Tavily
absent) and keep refusing when rendering is off or provenance is invalid.

## Tests (`apps/sb_bot/tests/test_tool_descriptions.py`)
Build the **fully populated** composition, following `test_jobs_receive_rendered_capabilities_
only_with_renderer_and_database` in `test_app.py` for faking Postgres:
env `PG_DSN=fake`, `BROWSER_RENDER_URL=http://renderer.invalid`, `TAVILY_API_KEY=fake-tavily`,
`LLM_PROVIDER=openai_compat`, fake LLM key/base URL/model, `provider=ScriptedProvider()`,
`vision_provider=ScriptedProvider()` (for `read_receipt`), and extensions enabled
(`EXTENSIONS_ENABLED=true`, worker socket/token file settings) by monkeypatching
`module.WorkerClient` with a fake whose `health()` returns the expected runtime, manifest,
`SandboxPolicy(...).digest` and `HARNESS_DIGEST`, and `module.read_token_file`. If extensions
cannot be wired that way, build `ExtensionsPlugin` with fakes and include its tools; say so.
1. Assert the tool name set equals the expected full list (all 52 tools today, incl.
   `read_receipt`, `request_extension`, `list_extensions`, `run_extension`,
   `fetch_web_page`, `fetch_rendered_page`) so the test fails if the composition shrinks.
2. Every tool description ≤ 300 chars; every property, recursively, has a non-empty
   `description` ≤ 300 chars.
3. No description contains "Every write requires confirmation"; no `direct=True` tool's
   description claims it requires confirmation unconditionally; `configure_jobs` does not say
   admins confirm.
4. Overlap clauses: `recall` mentions `read_chat_history`; `read_chat_history` mentions `recall`;
   `list_expenses` mentions `get_balances`; `get_balances` mentions `list_expenses`;
   `search_web` mentions `research`; `research` mentions `search_web`; `read_attachment`
   mentions `read_receipt`; `set_display_time_zone` mentions `get_current_time`.
5. `fetch_rendered_page` description mentions `fetch_web_page` with Tavily set and does not
   without it (composition with `BROWSER_RENDER_URL` but no `TAVILY_API_KEY`).
6. `system.md` contains the moved policy (assert a few key phrases: native group-admin check for
   display time zone, Frankfurter/open.er-api, export columns).
Keep existing tests green (update any asserting old description text; list them).

## Done when
- `uv run pytest -q` passes; ruff check and format are clean.
- No handler/gate/schema-constraint changes (diff touches only description strings, `system.md`,
  the BrowsePlugin flag + its wiring, and tests).
- Report lists each removed policy sentence and its new home in `system.md`.
- Out of scope: renames (`build_search_link` stays), merges, retirements, skill/routing code.
