# Skill layer, stage 0: baseline metrics, routing set, embedder benchmark

Read `plans/skill-layer.md` §3.3 (skill catalog), §4.6 (embedding admission), §9 (metrics line
and stage table) and §10 checks 12, 13 and 15. Read `ARCHITECTURE.md` §6a (logging rules). Then read the
files that define what you build against:

- `packages/core/src/chatcore/agent.py` (`AgentResponder._respond_after_typing`, the turn loop)
- `packages/core/src/chatcore/tools/registry.py` (`ToolRegistry.get/specs/call`)
- `packages/core/src/chatcore/llm/types.py` (`ToolSpec`, `Usage`)
- `packages/core/src/chatcore/embed.py` (`FastEmbedder`, `FakeEmbedder`)
- `packages/plugin-search/src/chatcore_search/__init__.py` (`_CHUNK_SIZE`, `_index_attachment`)
- `packages/core/tests/test_agent_deadline.py` (how agent tests build an app and fake provider)
- `apps/sb_bot/tests/test_tool_descriptions.py` (`EXPECTED_TOOLS`, how to build the real registry)

**Purpose.** Stage 0 records a one-week baseline in today's behaviour (`SKILLS_MODE` does not
exist yet; everything is effectively `off`). Later stages compare against it. **Behaviour must not
change**: provider requests, tool calls, replies, persistence and usage recording stay exactly as
they are. You only add counting and one log line.

**Coordination.** Another branch is editing `agent.py` in parallel. Keep the `agent.py` diff
small: create one metrics object, a handful of one-line counter updates, one emit call. All logic
lives in the new module.

## You own
- `packages/core/src/chatcore/turn_metrics.py` (new)
- `packages/core/src/chatcore/agent.py` (only the counter/emit lines described in §1)
- `packages/core/tests/test_turn_metrics.py` (new)
- `apps/sb_bot/tests/data/routing_set.jsonl` (new)
- `apps/sb_bot/tests/test_routing_set.py` (new)
- `apps/sb_bot/scripts/bench_embedder.py` (new)
- `apps/sb_bot/tests/test_bench_embedder.py` (new)

You may **not** edit any other file: no registry, provider, prompt, tool description, plugin or
migration changes, no new settings or env vars in the app. Ask in your report if one must change.

## 1. Per-turn metrics line: `chatcore/turn_metrics.py` + `agent.py`

### Module

```python
class TurnMetrics:
    """Per-turn counters for the stage-0 baseline log line (plans/skill-layer.md §9).

    Holds only registry-validated tool names, categories and counts. Never stores message text,
    tool arguments, URLs, model output or unvalidated names.
    """

    def __init__(self, registry: ToolRegistry, offered: Sequence[ToolSpec]) -> None: ...
    def note_call(self, name: str) -> None: ...
    def note_stripped(self) -> None: ...
    def note_retry(self) -> None: ...
    def emit(self, logger: logging.Logger, *, chat_key: str, steps: int, final_forced: str,
             end: str, usage: Usage, ms: float) -> None: ...
```

- `__init__` records `offered` = number of specs and `spec_bytes` =
  `len(json.dumps([{"name", "description", "parameters"} per spec], ensure_ascii=False,
  separators=(",", ":"), sort_keys=True).encode("utf-8"))` — computed once per turn from the specs
  of the first request (the offered set is fixed for the whole turn today).
- `note_call(name)`: if `registry.get(name)` is not `None` (exact match), append the name to
  `called` (keep first-seen order, deduplicated); if it is registered but not among the offered
  spec names (e.g. an admin-only tool called by a non-admin), also increment `not_offered`.
  Otherwise increment `unknown_calls`; **never store the string**.
- `note_stripped()` increments `markup_stripped` (count of `strip_tool_markup` hits this turn).
- `note_retry()` increments `retries` (the empty-answer no-tools retry).
- `emit` writes exactly one INFO line via `logger.info` with `%s`/`%d` arguments (no f-string
  building of the format), field order fixed:

  ```
  turn metrics chat=<key> mode=off offered=<n> spec_bytes=<n> steps=<n> called=<a,b|-> unknown_calls=<n> not_offered=<n> final_forced=<steps|deadline|rounds|-> markup_stripped=<n> retries=<n> end=<answer|pending|refusal|timeout|provider_error|fallback> in_tokens=<n> out_tokens=<n> ms=<int>
  ```

  `final_forced` and `end` must each be one of the listed literals; `emit` raises `ValueError`
  on anything else (tests call it directly). `called` is `-` when empty. Skill-only §9 fields
  (`skills`, `loaded`, `source`, `embed_skipped`, `top`, `eligible_calls`, `initial_cover`,
  `oracle_cover`, `meta_calls`, `hints`, `route_ms`, `gate_ms`, …) are **left out**; `mode=off`
  is a literal for now.
- `emit` must never raise into the turn for logging reasons; the agent wraps the call (below).

### Agent wiring (`_respond_after_typing` only)

- Create `metrics = TurnMetrics(app.tools, app.tools.specs(is_admin=ctx.is_admin))` right after
  `ctx` is built.
- In the tool-call loop, call `metrics.note_call(call.name)` once per call, at the point where
  `ctx.turn.tool_steps += 1` happens (so it counts every call the model returned, including ones
  not run).
- Call `metrics.note_stripped()` at each of the three existing `if stripped:` /
  `if stripped_retry:` branches; `metrics.note_retry()` where the retry completion is requested.
- Track the end state with one local variable, `end`, set to: `"refusal"` at either refusal
  branch, `"fallback"` when `_empty_answer_fallback` is used, `"timeout"` in the outer
  `except TimeoutError`, `"provider_error"` in `except ProviderError`, `"pending"` when
  `pending_action_requested`, otherwise `"answer"`. Keep the existing control flow untouched.
- `final_forced`: `"deadline"` if `deadline_final_forced`; else `"steps"` if any request was sent
  with `final_answer` true because `tool_steps >= settings.tool_steps`; else `"rounds"` if the
  `for … else` branch ("couldn't complete in the allowed number of steps") ran; else `"-"`.
- Usage in the line is the turn's loop usage (all completions incl. the retry), captured
  **before** the summary usage is added. Do not change what is passed to `limits.record` or
  `store.record_usage`.
- Emit right after `store.record_usage(...)` and before the `after_turn` hooks, inside
  `try: … except Exception: log.debug("turn metrics failed")` with no exception details.
  `ms = int((loop.time() - started) * 1000)`.

### Logging rules (§6a, check 13)
No message text, tool arguments, tool results, URLs, hosts, model output, exception messages or
unvalidated names in this line or in any line this change adds. Chat key and validated tool names
only.

## 2. Synthetic routing set: `apps/sb_bot/tests/data/routing_set.jsonl`

About 200 lines (180–220), one JSON object per line, written from scratch. **No real chat text,
no real people's names, phone numbers, addresses or URLs of real private pages.** Use made-up
first names (e.g. Ah Beng, Mei Ling, Raj, Wei, Siti, Jun), generic places and amounts.

Fields:

| Field | Type | Meaning |
|---|---|---|
| `id` | str | `rs-001` … unique |
| `lang` | `"en"` \| `"zh"` \| `"singlish"` | ZH = Simplified Chinese; Singlish = English with lah/leh/lor/can/got/makan/chope/etc. |
| `segment` | `"direct"` \| `"paraphrase"` \| `"followup"` \| `"topic_change"` \| `"disabled"` | §9 stage-2 gate segments |
| `text` | str | the utterance |
| `prev` | list of `{"text": str, "skills": [str]}` | earlier turns by the same requester; required (non-empty) for `followup` and `topic_change`, empty otherwise |
| `skills` | list of str | expected skills for this utterance (from §3.3); `[]` means general only |
| `tools` | list of str | expected tool calls when meaningful, else `[]`; every name must be a currently registered SBot tool |
| `state` | object | for `disabled`: which feature is off or which permission is missing, e.g. `{"reminders": "off"}`, `{"watches": "off"}`, `{"rendered_fetch": "off"}`, `{"polls": "off"}`, `{"role": "member"}`, `{"dm_linked_groups": 0}`; `{}` otherwise |

Skill names allowed (§3.3, exactly): `time_zone`, `expenses`, `trips`, `reminders`, `watches`,
`feature_settings`, `polls`, `memory`, `research`, `rendered_pages`, `stocks`, `extensions`,
`conversation`, `plan_outing`.

Distribution (minimums): each `lang` ≥ 55 lines; each `segment` ≥ 30 lines; every skill above
appears in ≥ 4 lines; each segment appears in all three languages. `followup` lines are short
("make it 7 instead", "改成明天", "ok add one more lor") and only make sense with `prev`; their
`skills` normally equal the prev turn's. `topic_change` lines switch to a different skill than
`prev`. `disabled` lines ask for a feature that the `state` makes unavailable; label them with the
skill the user wants (the router should still route it so the hint appears), and put the tool
that would be called in `tools`. Include some general-only utterances (`skills: []`, e.g. "what
time is it in Tokyo now" → `get_current_time`, a general tool; "what did Wei say about dinner
just now" → `read_chat_history`) so routers can't win by always routing something. Note
`time_zone` is only for the chat's *display* time zone setting.

Tools that exist only after stage 4 (`list_polls`, `close_poll`, `configure_polls`,
`start_new_conversation`, `request_context_reset`, `get_persona`) must **not** appear in `tools`;
label those utterances by skill only.

## 3. Embedder benchmark: `apps/sb_bot/scripts/bench_embedder.py`

A standalone script (`uv run python apps/sb_bot/scripts/bench_embedder.py --help`), **not run in
CI, not imported by the app**. It never downloads a model: it constructs `FastEmbedder` (which
uses `local_files_only=True` and requires `EMBED_CACHE_DIR`); if the directory is missing it
exits with a clear message and status 2. A `--fake` flag uses `FakeEmbedder` (for the smoke test).

Measures and prints a small report (plain text, plus `--json` for machine output):
1. **Model load time** (constructor).
2. **Isolated query latency**: warm up 3 queries, then embed each routing-set `text` with
   `embed_query` sequentially (`--queries N`, default all); report count, p50, p95, max in ms.
3. **Latency under indexing**: start a background task that calls `embed_documents` on batches
   shaped like attachment indexing (chunks of 1,600 chars of synthetic lorem-style text,
   `--index-chunks`, default 200, in batches of `--batch`, default 20, back to back), and while it
   runs issue the same queries; report p50/p95/max and how many queries completed while indexing
   was in flight.
4. **Skip rate** (§4.6 simulation): wrap the embedder in a local in-flight counter (submitted,
   not finished — the script's own wrapper, `FastEmbedder` is not modified); during phase 3, for
   each routing query, count it as `skipped=busy` when the counter is > 0 instead of submitting,
   and report skip rate; also report how many of the non-skipped queries exceeded
   `--timeout-ms` (default 300).

Script logic lives in functions (`async def run(embedder, queries, *, ...) -> dict`) so the test
can call it with `FakeEmbedder`. Queries come from `routing_set.jsonl` (default path relative to
the script). No output includes utterance text — only counts and timings.

## Tests

`packages/core/tests/test_turn_metrics.py`:
- Unit: `note_call` with registered, unregistered and admin-only-not-offered names; `called`
  dedup and order; `unknown_calls` count; `spec_bytes` equals the documented JSON computation;
  `emit` format exact (assert the full line for a fixed input); `emit` rejects bad
  `final_forced`/`end` values.
- Agent (use the `test_agent_deadline.py` style `build` helper or `ScriptedProvider`): one metrics
  line per turn, at INFO, logger `chatcore.agent`; cases: plain answer (`end=answer steps=0
  called=-`), one real tool then answer (`called=work steps=1`), step limit reached
  (`final_forced=steps`), provider error (`end=provider_error`), refusal, empty-answer retry
  (`retries=1`), markup stripped count, pending action (`end=pending`), in/out tokens equal the
  sum of loop completions' usage and exclude summary usage.
- **Check 13 (leak test)**: a sentinel string (e.g. `ZQX_SENTINEL_7731`) placed in: the user
  message text; a fabricated tool name returned by the model (`ZQX_SENTINEL_7731_tool`); unknown
  argument keys and values on a real tool; a `__invalid_json__` call; a handler that raises an
  exception whose message contains the sentinel; and stripped tool markup in the answer. Capture
  **all** loggers at DEBUG (`caplog.set_level(logging.DEBUG)` on the root) and assert the sentinel
  appears in no `record.getMessage()`, no `str(record.args)`, and no formatted `exc_info`. The
  metrics line shows `unknown_calls=1`.
- Behaviour unchanged: for one multi-step scenario, the provider request list (`system`, `tools`,
  `tool_choice`, messages) is identical with metrics wiring as before — assert concretely against
  expected values rather than comparing to itself.

`apps/sb_bot/tests/test_routing_set.py`:
- Every line parses; fields/types exact; ids unique; 180–220 lines; distribution minimums from §2.
- Every `skills` entry ∈ the allowed set; every `tools` entry is registered in the real SBot
  registry (build it like `test_tool_descriptions.py`); stage-4-only tools absent.
- `prev` non-empty exactly for `followup`/`topic_change`; `topic_change` skills differ from the
  last `prev` skills; `disabled` lines have non-empty `state` and non-empty `skills`.
- Privacy guard: no `http://`/`https://` URLs except `example.com`/`example.org`, no 8+ digit
  runs (phone numbers), no `@` handles.

`apps/sb_bot/tests/test_bench_embedder.py`:
- Load the script with `importlib.util.spec_from_file_location`, run `run(FakeEmbedder(), …)`
  with small numbers; assert the result keys (isolated/indexing/skip stats) and that no utterance
  text appears in the returned dict or printed output.
- Missing `EMBED_CACHE_DIR` without `--fake` exits with status 2 and does not import network
  download paths (just assert the exit code and message).

## Environment
Run `uv sync --all-packages` first. Always set `UV_CACHE_DIR=$PWD/.uv-cache` for `uv` commands.
Postgres tests need `TEST_PG_DSN`; if it is unset those tests skip, which is fine for you — the
reviewer runs the full suite on a scratch DB. You cannot write `.git`; do not commit.

## Done when
- `UV_CACHE_DIR=$PWD/.uv-cache uv run pytest -q packages/core/tests apps/sb_bot/tests` passes.
- `uv run ruff check .` and `uv run ruff format --check .` are clean.
- `git diff --stat` shows only owned files.
- Existing agent tests pass unmodified (behaviour unchanged).
- Left for later, do not attempt: router, skills, `SKILLS_MODE`, in-flight counter inside
  `FastEmbedder`, shadow eligibility snapshot, any change to tool/agent behaviour.
