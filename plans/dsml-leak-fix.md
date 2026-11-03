# Tool-markup leak and truncated summaries

Read `ARCHITECTURE.md` §6a (security baseline) and §14 (final answers at the tool budget boundary), then
`packages/core/src/chatcore/agent.py`, `packages/core/src/chatcore/conversation.py`,
`packages/core/src/chatcore/llm/{base,types,openai_compat,anthropic_provider}.py`,
`packages/core/src/chatcore/testing.py` (`ScriptedProvider`) and
`packages/plugin-research/src/chatcore_research/agent.py`.

Setup: `export UV_CACHE_DIR="$PWD/.uv-cache"` then `uv sync --all-packages`.

## Background

**Bug 1.** The agent loop (`AgentResponder.respond`) runs up to `tool_steps` (8) tool calls, then
one final request with `tools=()` plus `_FINAL_ANSWER_INSTRUCTION`. DeepSeek (via the
OpenAI-compatible provider) still wanted to search. With no tools declared the provider didn't
parse its native tool syntax into `tool_calls`, so the markup arrived as plain `content` and the
bot posted it to a group verbatim. The exact production reply was (note the fullwidth `｜`,
U+FF5C, and a leading newline):

```

<｜DSML｜ calls>
<｜DSML｜ invoke name="search_web">
<｜DSML｜ parameter name="max_results" string="false">6</｜DSML｜ parameter>
<｜DSML｜ parameter name="query" string="true">REK KL restaurant review price menu 2025 Damansara</｜DSML｜ parameter>
</｜DSML｜ invoke>
</｜DSML｜ calls>
```

**Bug 2.** Rolling summaries (`ConversationStore.compact` / `compact_conversation`) use a
hard-coded `max_tokens=512`, and production logs show `output_tokens=512` repeatedly, so stored
summaries are being cut off mid-sentence and fed back as context.

## You own
- `packages/core/src/chatcore/llm/types.py`, `llm/base.py`, `llm/openai_compat.py`,
  `llm/anthropic_provider.py`
- `packages/core/src/chatcore/llm/sanitize.py` (new)
- `packages/core/src/chatcore/agent.py`
- `packages/core/src/chatcore/conversation.py`
- `packages/core/src/chatcore/testing.py` (only `ScriptedProvider`)
- `packages/plugin-research/src/chatcore_research/agent.py`
- Tests: new `packages/core/tests/test_tool_markup.py`, new
  `packages/core/tests/test_summary_truncation.py`, new `apps/sb_bot/tests/test_tool_markup_e2e.py`,
  and edits to existing tests and test fakes only where the new `tool_choice` keyword or the new
  final-step behaviour requires it (e.g. `packages/core/tests/test_a_runtime.py`,
  `packages/core/tests/test_a_providers.py`, fakes with explicit `complete()` signatures such as
  `apps/sb_bot/tests/test_admin.py` and `packages/plugin-expenses/tests/test_receipt.py`,
  `packages/plugin-research/tests/*`).

You may **not** edit `packages/core/src/chatcore/limits.py`, `apps/sb_bot/src/sb_bot/admin.py` or
anything about `/admin` budget commands (another branch is changing them), migrations, or `ARCHITECTURE.md`.

## 1. `tool_choice` on the provider interface

- `llm/types.py`: add `ToolChoice = Literal["auto", "none"]`.
- `Provider.complete` (in `llm/base.py`) gains a keyword `tool_choice: ToolChoice = "auto"`.
  Docstring: `"none"` means the tool specs stay declared (so the provider parses native tool
  syntax instead of leaking it as text) but the model must not call one.
- `OpenAICompatProvider.complete`: accept `tool_choice`. When `tools` is non-empty and
  `tool_choice == "none"`, send `"tool_choice": "none"` and do **not** send
  `parallel_tool_calls`. With `"auto"` keep today's request exactly (no `tool_choice` key), so
  existing provider tests stay valid. Without tools never send `tool_choice`.
- `AnthropicProvider.complete`: accept `tool_choice`; with tools send `{"type": "none"}` for
  `"none"` and keep `{"type": "auto"}` for `"auto"`. Without tools send neither.
- `ScriptedProvider.complete` (`testing.py`): accept `tool_choice` and record it in each call
  dict as `"tool_choice"`.
- Every other in-repo `complete()` implementation and fake whose signature would reject the new
  keyword must accept it (`**kwargs` fakes already do).

## 2. Output sanitizer: `chatcore/llm/sanitize.py`

```python
def strip_tool_markup(text: str) -> tuple[str, bool]:
    """Remove leaked tool-call markup. Returns (clean_text, stripped)."""
```

- Remove, case-insensitively and across newlines (`re.DOTALL`):
  - DeepSeek DSML blocks: from `<｜DSML｜ calls>` / `<｜DSML｜function_calls>` (optional
    whitespace after the second bar, `function_` optional) through the matching closing tag;
    then any remaining `<｜DSML｜…>` or `</｜DSML｜…>` tag on its own, plus a stray
    `<｜DSML｜ invoke …>…</｜DSML｜ invoke>` block.
  - DeepSeek native tokens: `<｜tool▁calls▁begin｜>…<｜tool▁calls▁end｜>` (`▁` is U+2581), also
    the per-call `<｜tool▁call▁begin｜>…<｜tool▁call▁end｜>` and
    `<｜tool▁outputs▁begin｜>…<｜tool▁outputs▁end｜>`. Accept ASCII lookalikes too: `|` for
    `｜` and `_` for `▁` (e.g. `<|tool_calls_begin|>`).
  - Generic blocks: `<tool_call>…</tool_call>`, `<tool_calls>…</tool_calls>`,
    `<function_calls>…</function_calls>`, and `<invoke name="…">…</invoke>`.
  - An **unclosed** opening of any block above (the model ran out of tokens mid-call) removes
    everything from the opener to the end of the text.
  - Any remaining special token of the form `<｜…｜>` (fullwidth bars, up to ~40 chars inside,
    e.g. `<｜end▁of▁sentence｜>`), which never belongs in prose.
- Afterwards collapse runs of 3+ newlines to 2 and `.strip()` the result — but only when
  something was stripped; untouched text is returned exactly as given with `stripped=False`.
- Pure function: no logging, no I/O. Compile the patterns once at module level.

## 3. Agent loop (`agent.py`)

- Final-answer step (`final_answer` true): pass the **normal** tool specs
  (`app.tools.specs(is_admin=ctx.is_admin)`) with `tool_choice="none"` and the existing
  `system + _FINAL_ANSWER_INSTRUCTION`. Normal steps pass `tool_choice="auto"` (or omit it).
- If a final-step completion still returns `tool_calls` (any stop reason), ignore them: build a
  plain `Message.assistant(text)` with no tool calls, execute nothing, and treat it as the answer.
- Every assistant message that ends the turn as the answer (the `break` paths that aren't the
  refusal path) goes through `strip_tool_markup` first. If markup was stripped, log
  `log.info("stripped tool markup chat=%s final=%s empty=%s", message.chat.key, final_answer, not clean)`
  — never the content.
- If the clean answer is empty and either markup was stripped or this was the final-answer step,
  make **one** retry request: same messages, tool specs declared, `tool_choice="none"`, system =
  `system + "\n\n" + _FINAL_ANSWER_INSTRUCTION + "\n\n" + _NO_TOOLS_RETRY_INSTRUCTION` where the
  new constant says plainly that tool calls are impossible, any tool-call syntax will be
  discarded, and the reply must be plain text for the user. Add its usage to `usage`, ignore any
  `tool_calls`, and sanitize it the same way (log again if stripped).
- If the retry is still empty, answer with a fallback built by a small helper:
  - with URLs in `ctx.turn.seen_urls`: `"I hit my search limit before finishing. Here's what I
    found so far:"` followed by one URL per line (sorted, at most 8);
  - with none: `"Sorry, I hit my search limit before I could finish an answer. Please try again
    or narrow the question."`
- What is sent **and** what is persisted to conversation history is the clean/fallback text,
  never the raw markup (persisting it would teach the model to repeat it next turn).
- Keep everything else unchanged: the 8-call cap, the nine-round maximum, the timeout (the retry
  runs inside the same `asyncio.timeout`), refusal handling, confirmation early exit,
  `untrusted` wrapping, and the unreachable-in-practice for/else step-limit text.
- `consider()` (the `/chatmode` classifier) only reads `YES`/`NO` and never sends model text; do
  not change it beyond what the new keyword needs (nothing, it passes no tools).

## 4. Research agent (`chatcore_research/agent.py`)

- Both final requests (the in-loop request once `complete` is false and the `FINAL_INSTRUCTION`
  request) currently pass `tools=()`. Pass the registry specs with `tool_choice="none"` instead,
  and ignore any `tool_calls` those requests return.
- Run `final_text` through `strip_tool_markup` wherever it is taken from a completion. If the
  in-loop text is empty after stripping, the existing `if not final_text:` path already makes the
  `FINAL_INSTRUCTION` request; let it. If that request's text is empty after stripping, use
  `"I ran out of research steps before writing up findings."` and set `complete = False`. Log
  `log.info("stripped tool markup research session=%s", ...)` style counters only — use whatever
  non-content identifier the module already logs (e.g. chat key), never text.
- `ResearchReport.render` still renders the system-collected sources, so the report keeps its
  source list in the fallback case.

## 5. Summaries (`conversation.py`)

- Module constant `SUMMARY_MAX_TOKENS = 1024`. `compact()` and `compact_conversation()` gain a
  keyword `max_tokens: int = SUMMARY_MAX_TOKENS` used instead of the literal 512.
- Add `truncated: bool = False` to `CompactResult`.
- After the summary completion: sanitize the text with `strip_tool_markup`. If
  `stop_reason == "max_tokens"` (or the clean text is empty), retry once with the same transcript
  and an extra instruction in the user message asking for a shorter summary that fits well within
  the limit (e.g. "at most about 250 words"). Sum both calls' usage.
- If the retry also hits `max_tokens` or is empty, **do not save**: keep the previous summary and
  its `covers_until` unchanged, and return `CompactResult(usage, truncated=True)` with
  `turns_summarized=0`. Never store a cut-off summary.
- The existing `conversation summarized ...` log lines gain `truncated=%s` (true when a retry was
  needed, whether or not it succeeded). Add a warning-level counter line when the summary is
  dropped: `"conversation summary dropped chat=%s reason=max_tokens"`.
- `/compact` (`ConversationPlugin` command, both the threaded and legacy paths): when
  `result.truncated` and `turns_summarized == 0`, still record usage, skip
  `advance_ambient` on the legacy path, and reply
  `"I couldn't fit a summary this time, so I kept the previous one. Try again later."`.
  Summaries are not sent to chats, only stored; the sanitizer above is what keeps markup out of
  them.

## Tests

`packages/core/tests/test_tool_markup.py`:
1. `strip_tool_markup` on the exact production DSML text above → `("", True)`.
2. DeepSeek native variant
   `<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>search_web\n```json\n{"query":"x"}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>`
   → empty, stripped; ASCII variant `<|tool_calls_begin|>…<|tool_calls_end|>` likewise.
3. Mixed: `"Here's what I found: REK is in Damansara.\n\n" + DSML` → keeps the sentence exactly
   (no trailing blank lines), stripped=True. Markup in the middle of two paragraphs keeps both.
4. Generic `<tool_call>{...}</tool_call>`, `<function_calls><invoke name="x">…</invoke></function_calls>`.
5. Unclosed `<｜DSML｜ calls>\n<｜DSML｜ invoke name="search_web">` (truncated) → everything from the
   opener stripped, preceding text kept.
6. Plain text with ordinary `<b>`, `a < b > c`, `|pipes|` and code is returned unchanged with
   `stripped=False`.
7. Agent (`ScriptedProvider`, 8 tool steps then final): the final request has non-empty `tools`
   and `tool_choice == "none"`, earlier requests `"auto"`; final response is the exact DSML text
   with `stop_reason="end_turn"`, retry response is plain text → the retry text is sent, the retry
   request carries the stronger instruction, persisted history holds no `｜DSML｜`, and a
   `stripped tool markup` record is logged with no DSML/query text in any log record (`caplog`).
8. Final step returns mixed text + DSML → only the text is sent, no retry call is made.
9. Empty-after-strip fallback: final and retry both return DSML, a search tool put URLs in
   `ctx.turn.seen_urls` → sent text starts with "I hit my search limit before finishing" and lists
   those URLs; same with no URLs → the apology.
10. Final step returns real `tool_calls` (`stop_reason="tool_use"`) plus text → no tool runs, the
    text is sent.
11. A normal (non-final) step returning markup-only text with no `tool_calls` → retry, then the
    retry's text is sent.
12. Update `test_agent_stops_after_eight_tool_steps` and the §14 test asserting
    `provider.calls[-1]["tools"] == []` to the new behaviour (tools declared, `tool_choice="none"`),
    keeping their original intent (8 calls cap, recovered next turn has clean context).
13. Provider wire tests (in `test_a_providers.py` style, mocking the SDK client):
    openai_compat sends `tool_choice: "none"` and omits `parallel_tool_calls` with tools +
    `"none"`; sends no `tool_choice` with `"auto"` or without tools; anthropic sends
    `{"type": "none"}` / `{"type": "auto"}` and nothing without tools.
14. Research: final in-loop text of DSML → falls through to the `FINAL_INSTRUCTION` request, whose
    plain text becomes the report; both empty → the fixed text with `complete=False`; final
    requests carry tool specs with `tool_choice="none"`.

`packages/core/tests/test_summary_truncation.py`:
15. `compact()` and `compact_conversation()` use `max_tokens=1024` by default and honour an
    explicit value.
16. First summary call `stop_reason="max_tokens"`, retry `end_turn` → retry text saved, request
    asked for a shorter summary, usage summed, log has `truncated=True`.
17. Both calls truncated → previous summary and `covers_until` unchanged (also when there was no
    previous summary: nothing saved), `CompactResult.truncated` is true, warning logged.
18. `/compact` with a truncated result replies with the kept-previous message and (legacy path)
    doesn't advance ambient.
19. A summary containing DSML is stored without it.

`apps/sb_bot/tests/test_tool_markup_e2e.py` (end to end):
20. `build_app("console", env={...}, platform=ConsolePlatform(bot_name=..., lines=<async
    iterator of scripted lines>, out=<StringIO>), provider=ScriptedProvider(...))` — follow the
    existing `build_app("console", ...)` usage in `apps/sb_bot/tests/test_app.py`. Script the
    provider so the bot uses its tool budget (any registered read-only tool, or a fake plugin if
    simpler) and the final step returns the exact DSML text, then the retry returns plain text.
    Run the platform until the scripted lines end and assert the console output contains the
    plain answer and no `｜DSML｜`, `DSML`, or `search_web` markup. A second case where both final
    and retry are DSML asserts the fallback text appears. If wiring a full tool budget through
    `build_app` is impractical, set the agent's tool budget low via the app's existing settings or
    env and say so in the report.

## Done when
- `uv run pytest -q` passes, except the known pre-existing failure
  `packages/plugin-polls/tests/test_service.py::test_real_core_button_confirmation_preserves_topic`
  (and integration tests that skip without services).
- `uv run ruff check .` and `uv run ruff format --check .` are clean.
- No log line anywhere in the diff contains model text, prompts, queries or URLs.
- Left for later, don't attempt: changing the tool budget size, live provider calls, ARCHITECTURE.md or
  deployment docs.
