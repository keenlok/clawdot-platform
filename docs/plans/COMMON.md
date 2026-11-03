# Rules for every Phase 1 track (read first)

You are implementing one track of **chatcore**, a reusable chat-bot framework, and **sb_bot**, the
first bot built on it. The full design is in `ARCHITECTURE.md` (read §3, §3d and §6a at least, plus the
sections your track plan names). Phase 0 is done: the shared contracts are on `main`, listed in
`ARCHITECTURE.md` §7c. Your job is the track plan that follows this file.

## Environment

- Python 3.12, `uv` workspace. Run everything through `uv`:
  - `export UV_CACHE_DIR="$PWD/.uv-cache"` first. The sandbox can't write your home directory.
  - `uv sync --all-packages` after adding a dependency.
  - Add dependencies to **your package's** `pyproject.toml` only, never the root one.
- Tests: `uv run pytest <your package dirs> -q`. Lint: `uv run ruff format . && uv run ruff check .`.
- You run in a sandbox with **no Docker**: write unit tests with fakes. Integration tests that need
  Postgres, Elasticsearch or Valkey go under `tests/` marked `@pytest.mark.integration` and must
  **skip** when the service isn't reachable. The Claude session supervising you runs them.
- Network access is on for `uv` installs. Don't call real external APIs from tests; use recorded
  fixtures (`respx` for `httpx`).
- **Do not commit.** The sandbox can't write `.git`. Leave your changes in the working tree; the
  supervising Claude session reviews and commits them.

## Hard rules

1. **Edit only the files your track owns** (listed in your plan), plus their tests and migrations.
   If you think a Phase 0 contract (anything listed in `ARCHITECTURE.md` §7c) must change, **stop and
   explain it in your report** instead of changing it.
2. Nothing outside `packages/adapter-telegram` may import `telegram`. Framework packages never
   import `sb_bot`. (`ruff` enforces both.)
3. **No secrets** in code, tests or fixtures. Settings classes use `pydantic.SecretStr` for every
   secret. Fake test keys must not look real.
4. **Security baseline (`ARCHITECTURE.md` §6a) is part of "done":**
   - Write tools never write. They return `ToolResult(pending_action=PendingAction.create(...))`
     with a `summary` stating exactly what will be written, and register a `PendingActionHandler`
     under that `kind` in `action_handlers()`.
   - Anything from outside the chat (pages, files, API output, search results) is returned with
     `ToolResult(untrusted=True)`.
   - Every per-chat query takes the chat as a required argument and filters by it. Write tests
     proving chat A can't see chat B's data.
   - Logs carry IDs, counts, latency, error class names and URL **hostnames** only. Never log
     message text, prompts, tool payloads, full URLs or exception args.
5. Match the existing code style: dataclasses or pydantic, type hints, short docstrings that say
   *why*, `from __future__ import annotations`, 100-char lines.
6. Keep it simple. Don't add abstraction the plan doesn't ask for.

## When you finish

Make sure `uv run ruff check .` is clean and your tests pass. Then end with a report in this
format (it is saved as your final message):

```
## Summary
## Files changed
## Dependencies added (name, version, why)
## Tests (command, result counts)
## Deviations from the plan (and why)
## Contract change requests (if any)
## Open issues / things the reviewer should check
```
