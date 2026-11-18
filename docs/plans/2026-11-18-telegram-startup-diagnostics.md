# Telegram startup failure diagnostics

Status: Live; merged in [PR #61](https://github.com/keenlok/clawdot-platform/pull/61) and released in
v0.20.1. Requested 2026-11-18 after a downstream deploy incident. Claude planned; Codex executed and
reviewed, then Claude did the final review.

## Problem

On 2026-11-18 a downstream bot restart-looped 8 times in about 90 seconds right after its
container was recreated for platform v0.20.0. Each run died about 11 seconds after start, and the
only log line was `platform stopped error=PlatformError`. It then recovered by itself. The likely
cause is Telegram still holding the previous instance's long poll (`Conflict`, HTTP 409), but that
couldn't be confirmed: the downstream log prints only the exception type, and the Telegram adapter
logs nothing when a startup step fails.

`TelegramPlatform._safe` (`packages/adapters/telegram/src/chatcore_telegram/__init__.py`) already
raises `PlatformError(<Telegram exception class name>)` without chaining the original exception.
What is missing is a log line that says which startup step failed and with which Telegram error.

## Fix

In `TelegramPlatform.start`, wrap each of the four startup calls (`app.initialize`,
`app.bot.get_me`, `app.start`, `app.updater.start_polling`) so that when `_safe` raises
`PlatformError`:

1. log exactly one WARNING on the `chatcore_telegram` logger:
   `Telegram startup failed step=<step> error=<ExceptionClassName>`, with steps named
   `initialize`, `get_me`, `start` and `start_polling`;
2. re-raise the same `PlatformError`, whose only argument is the Telegram exception class name
   (behaviour of `_safe` is unchanged).

The log uses only the class name (the adapter's `type(exc).__name__` convention), never the
exception message, the bot token or any URL. The existing `finally` cleanup (stop updater, stop
app, shut down if initialized) is unchanged. The command-menu setup keeps its current warning and
non-fatal behaviour. `_safe` callers outside `start` are not changed.

No core, gate, permission, migration, configuration or dependency change.

## You own (Codex)

- `packages/adapters/telegram/src/chatcore_telegram/__init__.py` (only `TelegramPlatform.start`
  and, if needed, a small private helper next to it)
- `packages/adapters/telegram/tests/test_telegram.py`

Don't edit any other file. Keep the logger name `chatcore_telegram`.

## Tests

In `packages/adapters/telegram/tests/test_telegram.py`, using the fake `App` pattern from
`test_start_shuts_down_after_startup_failure`, with bot token `SecretStr("startup-fake-token")`:

- `updater.start_polling` raises `telegram.error.Conflict("startup-fake-token secret conflict
  text")`: `platform.start` raises `PlatformError` with `str(exc) == "Conflict"`; caplog has exactly
  one WARNING record containing `step=start_polling` and `error=Conflict`; neither the token nor
  `secret conflict text` appears in caplog text or in `str(exc)`/`exc.args`; the app is stopped and
  shut down.
- `bot.get_me` raises `telegram.error.RetryAfter(30)`: `PlatformError` with
  `str(exc) == "RetryAfter"`; one WARNING with `step=get_me` and `error=RetryAfter`; the token does
  not appear; shutdown still runs.
- Existing startup tests still pass.

## Done when

- `uv run pytest -q packages/adapters/telegram` passes.
- `uv run ruff check packages` and `uv run ruff format --check` on the changed files are clean.
- `CHANGELOG.md` `Unreleased` → `Fixed` entry (Claude adds it).
