# Image edit: the model never passes `reference`

**Status (2026-11-13):** Merged in [PR #40](https://github.com/keenlok/clawdot-platform/pull/40), released in v0.12.1; live re-test pending. Bug fix for [image reference photos](2026-11-12-image-reference-photos.md) (v0.12.0). Approved by the owner on 2026-11-13, with Claude implementing.

**Execution:** Claude plans, implements and self-reviews, by owner choice for this small text fix (Codex CLI is installed; the owner skipped delegation). The execution section below is written so Codex could run it.

## The bug

The owner sent sb_bot a photo in a group, once with the edit request as its caption and once as a reply to their own photo ("make this photo a watercolour"). The bot replied that it couldn't generate an image from the source photo, then sent an unrelated generated picture.

## Evidence

- **Deploy is right.** sb_bot runs deployments `4c2543f` (includes PR #40 `IMAGE_EDIT_ENABLED=true` and PR #42, the dedicated OpenAI key). The container env has `IMAGE_ENABLED=true` and `IMAGE_EDIT_ENABLED=true`, and its installed `chatcore_image` contains the `reference` code.
- **The model called the tool once, without `reference`.** Both image turns (2026-11-12 07:21 and 07:24 UTC, group `tg:-100…884`) log `called=generate_image steps=1` and `chatcore_image: image generated mode=generate`. There was no rejected first call: a `reference` validation error would have left the attempt flag unset and shown as a second call. Routing worked (`loaded=image_generation`, and `skills=…image_generation…` on the second turn). Model: `deepseek-v4.1-flash`, native vision on.
- **The platform plumbing works.** A local repro of the owner's turn (group, own photo with caption `@bot make this photo a watercolour`, `SkillsMode.ON`, keyword router, vision on) shows the request contains the photo as an `ImagePart`, `generate_image` with the `reference` property, and the edit instructions. `ctx.turn.attachments` and `attachment_owners` are filled for captions and for replies, which come from the stored message row.

## Root cause

What the model is told contradicts editing, so it concludes it can't use the photo:

1. **Tool description:** "Create one requested image **from a text description** and send it to the chat." It is the same when editing is on.
2. **Skill instructions** lead with "Write one self-contained prompt that restates the whole picture", and the edit sentence only says when reference is *allowed* ("Use reference only when…"), never that it should be used.
3. **Photo numbers are undefined** in native-vision turns. Images arrive as unlabelled parts, and the `reference` description only says "the 1-based number of that photo".

The unit and contract tests call the tool with `reference=1` directly, so nothing covered what the model is offered.

## Fix (plugin only, editing-on text only)

All changes are in `packages/plugins/image/src/chatcore_image/plugin.py`. With `IMAGE_EDIT_ENABLED=false`, the tool description, schema and instructions stay byte-identical, so the off-state request snapshot does not change.

1. **Tool description when editing is on:** "Create one requested image and send it to the chat, either from a text description or by changing a photo the person sent or replied to (pass reference)."
2. **`reference` description:** say to pass it whenever the person asks to change, restyle or base a picture on a photo they sent with this message or replied to, because the image API then gets the photo itself. Define the number: photos are counted in the person's message first, then in the message they replied to, so a single photo is 1.
3. **`IMAGE_EDIT_INSTRUCTIONS`:** make it directive. When the person asks to change or restyle a photo they sent or replied to, call `generate_image` with `reference` (1 for a single photo) and a prompt that says what the result should look like. You can edit photos; never say you can't or substitute a description of it. Keep "never use other photos" and the real-person safety sentence. The combined generation + edit text stays at most 800 characters.

No core change: numbering is defined in plugin text, which matches the order `agent.py` builds `source_attachments` (current message, then replied-to human message).

## Regression tests

- **Agent-level, the owner's exact cases** (new `packages/plugins/image/tests/test_edit_turn.py`, real `AgentResponder` + `BotApp` + `FakePlatform` + `ScriptedProvider`, `SkillsMode.ON`, keyword router, `AgentSettings(vision=True)`, group chat):
  1. own photo with caption "@testbot make this photo a watercolour": the first request offers `generate_image` whose description mentions the photo, whose schema has `reference`, and whose system prompt carries the directive edit instructions; the request's last user message has the `ImagePart`. The scripted model then calls `generate_image(reference=1)`, and an injected fake client's `images.edit` gets the photo's bytes; exactly one photo is sent with caption `🖼 <title>`.
  2. reply to the requester's own stored photo message (seed it in the memory repo, as `evals/speaker_attribution/run_eval.py` does) with "@testbot make this photo a watercolour": same assertions, the edit gets the replied photo.
  3. reply to **another member's** photo in the group: `reference=1` returns "In groups I can only use your own photos." and `images.edit` is never called.
- **Plugin unit:** with editing off, description, schema and instructions equal the v0.12.0 strings; with editing on, the description mentions the photo and `reference`, and the instructions mention `reference` and stay ≤ 800 characters.
- **Live eval (owner runs it):** `evals/image_reference/run_eval.py`, following `evals/speaker_attribution/run_eval.py`: capture the case-1 request from current code, replay it N times against the configured model (`LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_PROVIDER` from env), and print only the rate of `generate_image` calls that pass `reference=1`. It exits non-zero on provider errors. A copy run on `main` gives the before rate.

## Not in scope

- `image delivery failed error=PlatformError` at 07:24 UTC: delivering a 3.3 MB landscape PNG failed; the cause isn't logged. A separate follow-up.
- Labelling images "Image N of M" in core for native-vision turns.
- Guessing a reference when the model omits it.

## Execution plan (for Codex)

Read first: `AGENTS.md`; this plan; the whole `packages/plugins/image/` package; `tests/test_image_delivery_contract.py`; `packages/core/src/chatcore/testing.py` (`ScriptedProvider`, `FakePlatform`, `message`, `memory_stores`); `packages/core/tests/test_agent_skills.py` `_run` (how to build an agent turn); `packages/core/src/chatcore/agent.py` `_context` (how `replied` is loaded via `stores.repo.get_message` and how `source_attachments`/`attachment_owners` are ordered); `evals/speaker_attribution/run_eval.py`.

### Environment

- `export UV_CACHE_DIR="$PWD/.uv-cache"` before any `uv` command.
- Lint: `uv run ruff check packages services apps tests .github/scripts`; `uv run ruff format` on only the files you changed.
- Tests: `uv run pytest -q -m "not browser and not sandbox_integration"`. Postgres-backed tests skip without a database.
- Never call a real model or OpenAI API. Inject a fake image client (`ImageService(settings, client=...)` or replace `plugin.service._client`) with `images.edit`/`images.generate` as `AsyncMock` returning `b64_json` of a PNG with a real IHDR header (see `tests/test_image_delivery_contract.py`).

### You own

- `packages/plugins/image/src/chatcore_image/plugin.py` (description, schema text, instructions only; no logic change);
- `packages/plugins/image/src/chatcore_image/EXPLAIN.md` and the regenerated `docs/wiki/Plugin-image.md` (`uv run python .github/scripts/generate_explainers.py`), only if wording there describes the tool text;
- `packages/plugins/image/tests/` (new `test_edit_turn.py`; additions to `test_plugin.py`);
- `evals/image_reference/run_eval.py` (new);
- `CHANGELOG.md` (`## [Unreleased]`, a `Fixed` entry).

You may **not** edit core, adapters, other plugins, `gates.py`, `permissions.py`, request snapshots, `routing_set.jsonl`, `pyproject.toml`, `uv.lock`, `.github/`, or migrations. If one must change, stop and say why in your report.

### Done when

- The three agent-level tests and the unit tests pass, and would fail on v0.12.0 text (state in your report which assertions fail against the old strings).
- The full test command and lint pass; the off-state request snapshot and skill-catalog/routing-set tests pass unchanged.
- `evals/image_reference/run_eval.py --help` works without a key; do not run it against a real model.

## Implementation record (2026-11-13)

**Execution:** implemented by Claude and self-reviewed, by owner choice; no Codex run.

- `plugin.py`: `EDIT_DESCRIPTION`, `REFERENCE_DESCRIPTION` and the directive `IMAGE_EDIT_INSTRUCTIONS` (generation + edit text: 781 of 800 characters). `GENERATE_DESCRIPTION` keeps the v0.12.0 string for editing off.
- `tests/test_edit_turn.py`: the two owner cases (own photo with caption; reply to own stored photo) failed on the v0.12.0 text at the tool-description assertion and pass now; the other-member refusal case passes on both, as a guard.
- `test_plugin.py`: pins the editing-off description and schema, and the editing-on text.
- `evals/image_reference/run_eval.py`: captures both owner cases offline (photo and `reference` present in the request); not run against a live model.

**Not verified:** the live model's behaviour. The tests prove what the model is offered and that a `reference=1` call edits the photo, not that `deepseek-v4.1-flash` now passes it. Run the eval before and after, and re-test live.
