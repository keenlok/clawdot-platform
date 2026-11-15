# Cross-chat confirmation replies

Status: Planned; owner approved the fix on 2026-11-16. Implemented in [PR #51](https://github.com/keenlok/clawdot-platform/pull/51). Claude planned; Codex CLI is installed,
so Codex executes and reviews, then Claude does the final review.

## Problem

A write requested in one chat can belong to another. Starting a trip from the owner's DM creates a
`start_trip` pending action for the shared group, so `ConfirmService.request` posts the
Confirm/Cancel message in the group. It still passes `reply_to=message.ref`, the DM message. That
message does not exist in the group, so Telegram rejects the send. The agent logs
`confirmation request failed chat=<dm> error=PlatformError` and tells the user the write could not
be requested. Seen live on 2026-11-15 in a downstream deployment: repeated `start_trip` attempts
from a DM, every pending action keyed to the group. Every app on the platform is affected.

## Fix

In `ConfirmService.request` (`packages/core/src/chatcore/confirm.py`), drop `reply_to` when
`reply_to.chat != action.chat`, for both the button path (`platform.confirm`) and the reply-code
path (`platform.send`). Same-chat confirmations keep replying to the request exactly as today. The
confirmation still goes to `action.chat` and `action.thread_id`. Only the requester can confirm,
single use, 10-minute expiry and the summary text are unchanged. Covers every caller (agent and
privacy commands).

No adapter, permission, gate, migration or configuration change. Patch release **v0.18.1**.

## Tests

- Button platform: action for group G, `reply_to` in DM D → `platform.confirm` called for G with
  `reply_to=None`; same-chat case still passes the original `reply_to`.
- Reply-code platform: the same two cases for `platform.send`.
- Agent-level: a DM turn whose tool returns a pending action for another chat requests confirmation
  without a cross-chat reply and does not log `confirmation request failed` (fake platform that
  raises when `reply_to.chat != chat`, mirroring Telegram).
- Agent request snapshot, skill catalog and routing-set tests unchanged.

## Release and rollout

- `CHANGELOG.md` `0.18.1` Fixed entry; root `pyproject.toml` and `uv.lock` version 0.18.1. The
  merged-main release job tags `v0.18.1`.
- Downstream: clawdot-deployments pins v0.18.1 for SBot and Loki in a separate PR.
