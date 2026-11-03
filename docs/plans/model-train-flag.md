# `/model`: flag models whose provider trains on chats (`:train`)

Read `docs/plans/model-switch.md` (the `/model` design), `packages/core/src/chatcore/model_choice.py`
and `packages/core/src/chatcore/model_plugin.py`, and their tests
`packages/core/tests/test_model_choice.py` and `packages/core/tests/test_model_plugin.py`.

Why: some OpenCode Go models (Muse Spark Contributor, Iceman Contributor Free) let the provider
train on prompts and completions. When a chat switches to one, the people in it must be told,
and the bot must answer "is it safe to share X right now?" truthfully. A merge to `main`
auto-deploys, so behaviour for entries without `:train` must stay byte-identical.

## You own
- `packages/core/src/chatcore/model_choice.py`
- `packages/core/src/chatcore/model_plugin.py`
- `packages/core/tests/test_model_choice.py`
- `packages/core/tests/test_model_plugin.py`
- `apps/sb_bot/.env.example` (the `LLM_MODEL_CHOICES` comment only)
- `docs/plans/model-switch.md` (document `:train`)

You may **not** edit `chatcore/agent.py`, the system prompts, the settings classes, the skill
router or routing sets. Don't add dependencies. Ask in your report if one must change.

## 1. Parsing (`model_choice.py`)
- `ModelChoice` gains `trains_on_input: bool = False` (keep `name`, `vision` and their order;
  add a docstring line like the one on `vision`).
- Each `LLM_MODEL_CHOICES` entry is `name[:marker[:marker]]`. Markers are `text` and `train`,
  case-insensitive, in any order: `a:text`, `a:train`, `a:text:train`, `a:TRAIN:Text` all parse.
  Model names can't contain `:` (`_MODEL_NAME`), so split on `:`; strip whitespace around the
  whole entry only.
- Raise `ValueError` at startup (same style as the existing messages, quoting the entry) for:
  an unknown marker (`a:vision`, `a:`, `a::text`), a repeated marker (`a:text:text`,
  `a:train:Train`), and everything already rejected (bad name, reserved name, duplicate model,
  empty entry).
- Replace the `_TEXT_ONLY_SUFFIX` constant with whatever the new parser needs.

## 2. Replies (`model_plugin.py`)
One module-level notice so every place says the same thing, e.g.:

> Heads up: while this switch is on, {name}'s provider uses the prompts and replies in this chat
> to train its models, so don't share anything private.

- `_label`: `name`, `name (text only)`, `name (trains on chats)`,
  `name (text only, trains on chats)`. This covers the choices list everywhere (`/model`,
  unknown-name reply, `get_model_info`).
- Successful `/model <name>` switch to a `:train` model: the existing reply plus the notice,
  placed before the trailing "`{form} default` switches back now." sentence, so the reply ends
  by telling people how to switch back. Non-`:train` replies are unchanged, character for
  character.
- `/model <name>` when the chat is already on a `:train` model ("already on ... until ..."):
  append the notice and the "`{form} default` switches back now." sentence.
- `/model` status while a `:train` override is active: the "Model here:" line uses `_label`
  (already does), and a new line with the notice follows it.
- `get_model_info` (`bot_model` skill):
  - Active `:train` override: state plainly that the provider of the current model uses prompts
    and replies in this chat to train its models while the switch is on, so it isn't safe to
    share anything private here now, and that `{command} default` switches back.
  - Otherwise, if any choice is `:train`: one sentence saying choices marked "(trains on chats)"
    are run by providers that use prompts and replies to train their models, and the current
    model isn't marked that way.
  - No `:train` anywhere: output unchanged.
  - Update the tool description to mention it reports whether the current model's provider
    trains on chat content. Keep it read-only.

## 3. Docs
- `apps/sb_bot/.env.example`: extend the `LLM_MODEL_CHOICES` comment: mark models whose provider
  trains on prompts and replies with `:train`; markers combine (`x:text:train`). Example:
  `deepseek-v4.1-flash` default with `mimo-v2.6-flash,muse-spark-1.3-contributor:train,beam-free,iceman-contributor-free:train`.
  Leave the value itself blank.
- `docs/plans/model-switch.md`: add a "Training" row to the decisions table, mention `:train` where
  `:text` is described (Phase 2 parser, replies, smoke-test section: check the provider's data
  policy and add `:train` when it trains on inputs), and show the notice in the reply examples.

## Tests
`test_model_choice.py`:
- Parses `a:train`, `b:text:train`, `c:TRAIN:text`, `d:text` and plain `e` to the right
  `(vision, trains_on_input)` pairs.
- Rejects `a:vision`, `a:`, `a::train`, `a:text:text`, `a:train:train`, `a:Train:train`
  (add them to the existing parametrised bad-list test).

`test_model_plugin.py` (use a fixture with a `:train` choice and a `:text:train` choice next to
the existing `glm-5,kimi:text`, or a second app builder):
- `/model` lists `x (trains on chats)` and `y (text only, trains on chats)`.
- Switch to a `:train` model: reply contains the notice and still ends with
  "`/model@bot default` switches back now."; a `:text:train` switch shows both the images line
  and the notice.
- Switching to a non-`:train` model: the reply has no notice (assert the exact existing text).
- "already on" reply for a `:train` model contains the notice.
- `/model` status during a `:train` override contains the notice line.
- Unknown-name reply lists the training label.
- `get_model_info`: notice when the `:train` override is active; the "marked (trains on chats)"
  sentence when on the default with `:train` choices; unchanged output with no `:train` choices.
- Existing tests keep passing unchanged.

## Done when
- `uv run ruff check packages services apps tests` and `uv run ruff format --check` on the owned
  files are clean.
- `uv run pytest -q packages/core/tests/test_model_choice.py packages/core/tests/test_model_plugin.py`
  passes, and `uv run pytest -q -m "not browser and not sandbox_integration" packages/core` passes.
- Not in scope: the system prompt or runtime context, the routing set, deploy config.
