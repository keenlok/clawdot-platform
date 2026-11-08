# Telegram 30-second download timeout implementation plan

**Status:** Shipped, inactive pending release and downstream adoption; owner approved implementation, merge and automatic deployment on 2026-11-09.

**Goal:** Allow Telegram attachment requests to wait longer than the current
5-second connect/read defaults while bounding each complete download invocation
to 30 seconds by default.

**Roles:** Codex prepared this plan after Claude CLI failed authentication
(expired OAuth session). This uses the AGENTS.md unavailable-model fallback.
Execution: Codex under ARCHITECTURE.md §7b; reviews: Codex, then Claude if its
authentication is restored, otherwise explicitly record the unavailable review.
Owner authorized merge and normal automatic deployment. After platform CI publishes a patch release, update the downstream application to that reviewed tag and locked commit; merge its pin update after CI. No manual production rebuild or restart.

## Design and alternatives

Use a 30-second total budget per direct Telegram adapter `download` invocation.
Pass 30-second connect/read overrides to both `get_file` and the file transfer.
Retain the configured three retries, but requests and retry sleeps all consume
the same total budget. This permits a request stalled longer than five seconds
to succeed without multiplying 30-second waits across every stage and attempt.

The existing attachment tool's 30-second outer deadline stays in place. It can
cancel the adapter at the same boundary; preserve external cancellation. Automatic
loading and a later tool call are separate invocations, each with its own budget;
this is not a 30-second whole-turn deadline.

Alternatives considered:

- Only increase per-request timeouts: smaller patch, but automatic image loading
  could wait several minutes across four attempts and two stages.
- Increase the shared SDK request timeout: affects unrelated Telegram operations,
  including sends; broader than the requested attachment-download change.

The same incident image downloaded quickly during diagnosis. This is a resilience
change, not a proven cure for the underlying intermittent network failure.
Keep the size limit, retry count, backoff policy, and ordinary Telegram requests
unchanged. Bridge transport and error-message improvements are separate work.

## Files

- Modify `packages/adapters/telegram/src/chatcore_telegram/__init__.py`:
  settings, download budget wrapper, and explicit request timeouts.
- Modify `packages/adapters/telegram/tests/test_telegram.py`: focused behavioral
  tests and existing mock assertions affected by new keyword arguments.
- Update this plan and `plans/README.md` with implementation/review status.

The adapter `__init__.py` is an existing module-layout exception and a protected
file requiring maintenance review. Do not restructure it as part of this patch.
No dependencies, migrations, permission changes, or production configuration edits.

## Implementation steps

- [x] Create an isolated branch/worktree and preserve unrelated current changes.
- [x] Add failing tests for slow download stages and timeout keyword forwarding.
  Use an injected short timeout for cancellation tests, not real 30-second sleeps.
  Verify both a stalled `get_file` and stalled byte download are cancelled and
  return the safe `PlatformError("TimedOut")` label. Test fast transient retries
  still succeed and external `CancelledError` propagates.
- [x] Add a validated `TelegramSettings.download_timeout_s` field:

  ```python
  download_timeout_s: float = Field(
      default=30.0, gt=0, le=120,
      validation_alias="TELEGRAM_DOWNLOAD_TIMEOUT_S",
  )
  ```

- [x] Retain the metadata precheck in `download`, then wrap the existing retry
  loop in a private helper with an overall budget. Core structure:

  ```python
  try:
      async with asyncio.timeout(self._settings.download_timeout_s):
          return await self._download_with_retry(attachment, limit=limit)
  except TimeoutError:
      raise PlatformError("TimedOut") from None
  ```

  Move the existing retry loop unchanged into `_download_with_retry(self,
  attachment: Attachment, *, limit: int) -> bytes`, except for these calls:

  ```python
  timeout = self._settings.download_timeout_s
  file = await self._application.bot.get_file(
      attachment.id, connect_timeout=timeout, read_timeout=timeout,
  )
  content = bytes(await file.download_as_bytearray(
      connect_timeout=timeout, read_timeout=timeout,
  ))
  ```

  Preserve the existing logger, size checks, safe exception labels, RetryAfter
  handling, jitter, and sleep cap. Do not catch `CancelledError` or log exception
  messages, URLs, or credentials.
- [x] Test that immediate `TimedOut` failures still produce four attempts by
  default, while expiry of the shared budget stops further retries. Cover
  cancellation during backoff without a long real sleep. Verify an oversized
  attachment is rejected before network access, and defaults/invalid timeout
  settings are validated. Keep existing token-redaction coverage passing.
- [x] Run focused validation:

  ```sh
  uv run pytest -q packages/adapters/telegram/tests/test_telegram.py
  uv run pytest -q packages/core/tests/test_images.py
  uv run pytest -q tests/test_package_boundaries.py tests/test_plans_index.py
  ```

- [x] Run the repository's complete pre-PR checks:

  ```sh
  uv sync --locked --all-packages
  uv run ruff check packages services apps tests
  uv run ruff format --check .
  uv run pytest -q -m "not browser and not sandbox_integration"
  ```

  Format only modified files. Record baseline failures and checks blocked by
  missing services accurately; do not report them as passed.
- [ ] Review cancellation, retry deadlines, safe errors, and protected-file scope.
  Record executor/reviewer roles, validation, and longer possible waits in the PR.
  Update the plan's index status to reflect delivered state without claiming it
  is live before deployment. Merge after green CI and review; confirm the published tag, downstream pin CI, and normal automatic deployment health.

## Approval

Owner approved the design and implementation, merge and auto-deploy: 30 seconds **per complete adapter
download invocation**, including its retries; not 30 seconds for each of four
unbounded attempts. The approved source plan was copied from the historical checkout into the current platform docs/plans directory following the repository split. Publish v0.6.1 with a dated changelog and root version bump; downstream pins stay on one tag/commit. A matching downstream plan records that mechanical adoption.

## Implementation verification

- Executed by Codex `gpt-6-luna`; independent Codex `gpt-6.1-sol` review found no actionable issues; parent Codex also reviewed the diff. Claude final review was attempted but blocked by expired OAuth authentication, so the AGENTS.md fallback applies.
- Test-first evidence: nine new cases failed before implementation; final adapter suite: 93 passed. Images/reader and boundary/index checks passed.
- Full non-browser/non-sandbox suite with an isolated local Postgres: 3,965 passed, one skipped, 26 deselected, one expected failure.
- Locked sync, lint (including `.github/scripts`), generated-explainer check, changed-file format check, and gitleaks tree scan passed. Repository-wide formatting reports 15 pre-existing files; none is changed here.
- Root and lock version are 0.6.1 with a dated changelog. No dependency, permission or migration changes.
- Release, downstream pin and actual runtime receipts will be recorded on the associated PRs after CI and normal automatic rollout. No manual production restart is planned.
