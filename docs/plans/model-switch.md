# Temporary per-chat model switch (`/model`)

Status: **implemented** on `claude/models-opencode-backlog-8fs7p8` (2026-11-01; plan revised
after review the same day). Off until `LLM_MODEL_CHOICES` is set. Backlog entry: `ARCHITECTURE.md` §12.
"As built" below records where the build differs from the phases.

## Goal

Anyone in an allowlisted chat can switch that chat's runtime model to another model from an
app-configured list for 30 minutes. After that the chat reverts to `LLM_MODEL`. Only the
`/model` slash command switches. The LLM has no tool for it and, when asked in natural
language, points people to `/model`.

## Production today (box `.env`, non-secret keys, 2026-11-01)

- Chat: `LLM_PROVIDER=openai_compat`, OpenCode Go (`https://opencode.ai/zen/go/v1`),
  `LLM_MODEL=deepseek-v4.1-flash`, `LLM_VISION=true`.
- Receipts: `VISION_PROVIDER=openai_compat` on the same OpenCode Go base URL,
  `VISION_MODEL=mimo-v2.6-flash`.
- Skill layer on everywhere (`SKILLS_MODE=on`, `SKILLS_CHATS=*`), router on the in-process
  `embeddinggemma-300m` embedder.

Every choice must do what `deepseek-v4.1-flash` does for the bot today: tool calling (the
skill layer's `use_skill` loads and every plugin tool), a final answer requested with
`tool_choice="none"` while tools stay declared, and, unless marked text-only, image input.

## Decisions

| Topic | Decision |
|---|---|
| Who can switch | Setting `LLM_MODEL_SWITCH=anyone\|admins`, default `anyone`: anyone in an allowlisted chat, including DMs from members. `admins` limits switching to group admins and setup managers (bot admins pass everywhere; in DMs only bot admins can switch); `/model` (show) and `/model default` stay open to everyone |
| How | `/model` command only; no LLM tool, no pending action |
| Allowed models | Per-app `LLM_MODEL_CHOICES` (comma-separated exact model IDs). Empty disables the command's switching. Only tool-capable models that passed the smoke test below go on the list |
| Duration | `LLM_MODEL_OVERRIDE_MINUTES`, default 30 |
| Renewal cap | Switching to the model that is already active doesn't extend it. A chat can run an override for at most `LLM_MODEL_OVERRIDE_MAX_MINUTES` (default 60) out of any rolling 3 hours; after that `/model <name>` replies with when it's allowed again |
| Cooldown | `LLM_MODEL_SWITCH_COOLDOWN_S`, default 300 seconds per chat; `/model default` is exempt and doesn't reset the cooldown |
| Scope | Whole chat (`chat.key`), all forum topics. The `x-opencode-session` key is unchanged |
| Storage | Valkey via `Cache`, TTL = override duration. A restart or Valkey flush falls back to the default |
| Turn binding | The model is resolved once, in `AgentResponder.respond()`, before the turn runs. An expiry mid-turn doesn't change a running turn |
| Images | Each choice may be marked text-only (`name:text`). On a text-only override the whole turn runs with vision off (see below) |
| Training | Mark a choice `name:train` when its provider trains on prompts and replies; combine with `:text` as `name:text:train`. The switch reply, `/model` status and model info warn people while such a switch is on, and model info reports the active provider's training policy |
| Unchanged | `read_receipt` and `VISION_*`, budgets and rate limits (the override's tokens count toward `DAILY_TOKEN_BUDGET` as usual), embeddings, the chatmode classifier, reopen compaction, manual `/compact` and `research` jobs (all stay on the default model) |

### Images: the two paths, and why text-only means "vision off for the turn"

1. **Receipts (`read_receipt`)** use the separate `VISION_*` provider (`mimo-v2.6-flash`
   in production). `/model` never touches it.
2. **General photos** go to the **main model** through `LLM_VISION=true` (§18). They arrive in
   two places: at turn start (the message's and the replied-to message's attachments) and
   **mid-turn**, as images returned by the `read_attachment` tool (`agent.py`, the
   `tool_images` path). A turn-start check alone can't catch the second.

So vision becomes a per-turn flag rather than reading `self.settings.vision` directly. On a
text-only override the turn runs exactly as with `LLM_VISION=false`: no images are attached,
the prompt gets the existing "You cannot see them; use a tool such as `read_receipt`" note, and
`read_attachment` images are dropped. The log line records `vision_off=override`. No silent
model fallback.

### Cost

`DAILY_TOKEN_BUDGET` counts raw tokens and has no per-model prices, while OpenCode Go's usage
limits are in money and shared by every chat (and by receipts if they use the same key). A
pricier model therefore drains the allowance faster without the bot's budget noticing. Until
the balance poll exists:

- `LLM_MODEL_CHOICES` lists only models priced at or near the default; pricier models wait for
  the balance-aware step under "Later".
- The renewal cap above stops one person keeping an override on indefinitely.
- Each switch logs chat, model and the revert time. Who switched appears in the chat reply only,
  not in logs (display names are member-written).

### Prompt caching

The `x-opencode-session` key (`chat.key`, plus the thread ID in forum topics) stays as it is.
Prompt caches are per model, so the first turn after a switch, and the first after the revert,
pays full input price for the prompt and history once. Accepted; the renewal cap bounds how
often it happens. History itself is portable: the provider doesn't send model-specific
reasoning back, only text, images and tool calls/results.

## Commands

- `/model`: current model (default or override), who set it and when it reverts, and the list.
- `/model <name>`: case-insensitive exact match against the list. For a choice marked `:train`,
  the reply warns: "Heads up: while this switch is on, `muse-spark-1.3-contributor`'s provider
  uses the prompts and replies in this chat to train its models, so don't share anything private."
  The reply ends with the default-switch instruction. Other replies, for example:
  "Switched this chat to `glm-5` for 30 minutes (set by Alice, reverts 14:32). `/model default`
  switches back now." Unknown names list the choices. During the cooldown or past the renewal
  cap it replies with the wait time.
- `/model default` (also `reset`): clears the override at once.
- Times use the chat's display time zone (`display_time`).

## Phases

### Phase 1: provider model override (core `llm/`)
- `OpenAICompatProvider.with_model(model) -> OpenAICompatProvider` returns a view sharing the
  same client and OpenCode header logic with a different `model`. `AnthropicProvider` is out of
  scope (production doesn't use it; its effort and fallbacks are tied to the model). Apps on
  Anthropic leave `LLM_MODEL_CHOICES` empty; startup rejects choices with `LLM_PROVIDER=anthropic`.
- Tests: the request body carries the new model; headers and `parallel_tool_calls` are unchanged.

### Phase 2: override store and settings (core)
- `chatcore/model_choice.py`: `ModelChoice(name, vision, trains_on_input)`, a parser for
  `LLM_MODEL_CHOICES` (`:text` and `:train` markers, in either order; rejects duplicates and
  blanks at startup), and `ModelOverrideStore` over `Cache`:
  `get(chat)`, `set(chat, choice, set_by, until)`, `clear(chat)`, `cooldown_left(chat)`,
  `minutes_used(chat)` for the renewal cap. JSON values; key prefix `model-override:`; display
  names only, never user IDs in logs.
- Settings on `LLMSettings`: `LLM_MODEL_CHOICES`, `LLM_MODEL_OVERRIDE_MINUTES`,
  `LLM_MODEL_OVERRIDE_MAX_MINUTES`, `LLM_MODEL_SWITCH_COOLDOWN_S`, `LLM_MODEL_SWITCH`.
- Tests with the in-memory cache: TTL expiry, cooldown, renewal cap, clear, malformed values
  ignored.

### Phase 3: per-turn provider in the agent (core `agent.py`)
- `AgentResponder.respond()` resolves the effective provider and the turn's vision flag once,
  before `_respond_after_typing`, and passes both down. That is where `record_usage` runs, so it
  records the effective provider's model.
- Every completion in the turn uses it: the tool loop, the final-answer retry, and the
  summary/compaction call. `_BudgetedProvider` wraps the **effective** provider, so budget
  admission (`limits.budget_exhausted`) and per-completion recording are unchanged. The turn
  metrics line gets a `model=` field.
- Unchanged and stated: `consider()` (the chatmode YES/NO classifier) and reopen compaction in
  `app.py` keep `self.provider`, the default model.
- Every use of `self.settings.vision` in the turn path reads the per-turn flag instead.
- If the override's model fails with a provider error, the turn fails as it does today (no
  silent fallback), and the error reply mentions `/model default`.
- Tests: scripted providers confirm the override model is used, expiry reverts, usage rows and
  the metrics line carry the effective model, `_BudgetedProvider` wraps the override, a
  text-only override drops both turn-start and `read_attachment` images, and the chatmode
  classifier stays on the default.

### Phase 4: `/model` command (core plugin)
- A small `ModelPlugin` in core contributing the `/model` command, registered by apps that set
  `LLM_MODEL_CHOICES`. It goes in a `/help` group (stage 4, PR #29) and counts toward the
  Telegram menu limit guard (`fit_menu`, `test_telegram_menu_stays_below_trim_threshold`).
- Tests: show, switch, unknown name, cooldown, renewal cap, `default`, empty list,
  `LLM_MODEL_SWITCH=admins`, group `/model@bot`, DM, `/help` grouping.

### Phase 5: the bot knows about `/model` (prompt and skill layer)
- System prompt (`system.md` and `system_skills.md`): "You cannot change your own model. If
  someone asks to switch models, tell them to use `/model`." Plus the current model name in the
  runtime context so "which model are you?" is answered correctly.
- Skill layer: a small read-only "about the bot" skill with routing examples ("switch model",
  "use a smarter model", "换模型", "can change model or not") and `commands=("model",)`, no
  tools. The contribute-link backlog item can later add its tool to this same skill.
- Tests: the skill passes validation, the router selects it for the examples, and no tool can
  write the override.

### Phase 6: sb_bot wiring, docs, deploy
- `apps/sb_bot/app.py` builds the store and registers `ModelPlugin` when choices are set.
- `/privacy` names the provider (it doesn't change: same OpenCode endpoint).
- `ARCHITECTURE.md`: new section with verification and deploy needs; tick the backlog item.
- Deploy needs: the model smoke test below for each choice, then `LLM_MODEL_CHOICES=...` in
  the box `.env` and recreate the bot service. No migration. Rollback: empty
  `LLM_MODEL_CHOICES`.

### Model smoke test (deploy gate, per choice)

Run with the owner's go-ahead, against the live OpenCode Go endpoint, once per model before it
goes on the list. Each model must:

1. Call a plugin tool with valid JSON arguments.
2. Complete a skill-layer turn: `use_skill` load, then a tool from the loaded skill.
3. Return a plain final answer when called with `tool_choice="none"` and tools declared.
4. Leave no native tool-call syntax in the reply text (the sanitizer only knows DeepSeek DSML).
5. Unless marked `:text`, describe an attached image correctly.
6. Check the provider's data policy and add `:train` when it trains on prompts and replies.

The bot can't probe these at runtime; a model that fails stays off the list or is marked `:text`
(failing only step 5). A provider that trains on inputs is marked `:train`.

Some models, including `muse-spark-1.3-contributor`, reject `tool_choice="none"` while tools are
declared. The OpenAI-compatible provider retries that specific 400 once without tools, so check 3
can pass through the fallback (unit-tested; not yet re-run live with the fallback in place).
Smoke-test results: `mimo-v2.6-flash` passed all checks;
`beam-free` and `iceman-contributor-free` fail image handling and are listed `:text`;
`muse-spark-1.3-contributor` passed every check except 3; the fallback is meant to cover check 3
and needs a live re-run before muse goes on the list.

### Later (not in this build)
- Balance-aware switching: once the OpenCode balance poll exists, refuse or shorten switches to
  pricier models when the balance is low. Choices would gain a price tier, and pricier models
  could go on the list.

## As built (differences from the phases)

- **Phase 5, prompt:** `system.md` is digest-pinned as the legacy prompt
  (`test_legacy_prompt_is_unchanged`), so neither prompt file changed. When `/model` is
  configured, `AgentResponder` appends `MODEL_SWITCH_PROMPT` to whichever prompt the turn uses
  (skills on or off). With `LLM_MODEL_CHOICES` empty the prompt is byte-identical to today.
- **Phase 5, skill:** a skill with no tools never loads (`SkillSet.build` drops it as
  `reason=empty`), so the `bot_model` skill carries one read-only tool, `get_model_info`. It
  reports the current model, any switch and its end time, the choices and the exact
  `/model@bot` form. It never returns who switched (member-written text) and nothing in it
  writes. That also answers "which model are you?" without adding the model to the runtime
  context. Six routing-set rows (`rs-210`–`rs-215`) cover it in English, Chinese and Singlish.
- **Code:** `chatcore/model_choice.py` (parser, `ModelOverrideStore`, `ModelSwitchConfig`,
  `build_model_switch`), `chatcore/model_plugin.py` (`/model`, `get_model_info`, `bot_model`),
  `OpenAICompatProvider.with_model`, the per-turn `_TurnModel` in `agent.py`, and
  `model=`/`vision=` at the end of the turn metrics line.
- **Cap arithmetic:** a switch that would pass the cap is shortened to what's left; under one
  minute left it is refused with the first minute at which time frees up.

## Verification

Unit tests for each phase; `uv run pytest packages/core apps/sb_bot -q` and ruff clean; a
console end-to-end run with the fake provider covering show, switch, a turn on the override,
a text-only override with an image, expiry and `default`. The live smoke test only with the
user's go-ahead.

## Open decision

Which OpenCode Go models go on sb_bot's `LLM_MODEL_CHOICES` (within the cost rule above) and
which are text-only. The code doesn't depend on it; it's a deploy-time setting gated by the
smoke test.
