# Track E: Telegram adapter

Read `ARCHITECTURE.md` §3 (when the bot responds, DMs, guardrails), §3d (the whole adapter contract), §6a
(secrets, link previews, logging), and §7c. The contract you implement is
`packages/core/src/chatcore/platform.py` (`ChatPlatform`, `Capabilities`). Study
`packages/adapter-console/src/chatcore_console/__init__.py` as a reference adapter.

## You own

- `packages/adapter-telegram/` (package `chatcore_telegram`): the only package allowed to import
  `telegram`

Don't edit any `chatcore` file or `apps/sb_bot`. The coordinator wires the adapter into the app in
Phase 2.

## Implementation: `TelegramPlatform`, using `python-telegram-bot` v21+ (async)

`name = "tg"`. Capabilities: buttons, edit, threads, files and dm are all on;
`max_message_len=4096`; markup `"plain"`.

**Settings:** `TelegramSettings` with `TELEGRAM_BOT_TOKEN` (`SecretStr`). Build the `Application`
with the token only inside the adapter.

**Lifecycle:** `start(handler)` runs **long polling** (`allowed_updates` = message,
callback_query, my_chat_member, chat_member) until `stop()`. Use the low-level
`Application.initialize/start/updater.start_polling` path so it runs inside the caller's event
loop. No webhook.

**Update → event mapping.** IDs are strings: chat `ChatRef("tg", str(chat.id))`, user
`UserRef("tg", str(user.id))`.

**`message` with text, a caption or attachments → `IncomingMessage`:**
- `is_dm = chat.type == "private"`;
- `thread_id = str(message_thread_id)` only when `message.is_topic_message`;
- `mentions_bot`: a `mention` entity equal to `@<bot username>`, or a `text_mention` of the bot;
- `reply_to_bot`: `reply_to_message.from_user.id == bot id`;
- `reply_to` is a `MessageRef` to the replied message;
- attachments: the largest `photo` size, `document`, `audio`, `video` and `voice`, mapped to
  `Attachment(id=file_id, kind, filename, mime_type, size_bytes)`;
- `user.is_bot` comes from `from_user.is_bot`;
- `sent_at` is `message.date`.

**Other updates:**
- `new_chat_members` / `left_chat_member`, and `chat_member` updates, become `MemberJoined` /
  `MemberLeft`.
- `my_chat_member` where the bot became a member or administrator becomes `BotAddedToChat`.
- `callback_query`: `data` is `c:<action_id>` or `x:<action_id>`, becoming a `ButtonPress` (confirm
  or cancel). **Always** `answer()` the callback, even for unknown data. Ignore malformed data.
- Ignore edited messages, channel posts and everything else.

**Outgoing:**
- `send`: plain text (no `parse_mode`, so there's no markup injection), split at 4096 on line
  boundaries where possible, **`link_preview_options=LinkPreviewOptions(is_disabled=True)`
  always** (§6a), `reply_parameters` when `reply_to`, `message_thread_id` when `thread_id`.
  Returns the last part's `MessageRef`.
- `edit`: `edit_message_text`, also with link previews disabled. Ignore "message is not modified".
- `typing`: `send_chat_action(TYPING)`.
- `confirm`: send the prompt with an `InlineKeyboardMarkup` of `[Confirm]` (`c:<id>`) and
  `[Cancel]` (`x:<id>`). Assert that `callback_data` is at most 64 bytes.
- `download`: `get_file`. Refuse when `file_size > max_bytes` (and refuse past the 20 MB Bot API
  limit) **before** downloading. Use `download_as_bytearray`, and enforce `max_bytes` on the
  result.
- `is_member`: `get_chat_member` with status creator, administrator or member, or `restricted`
  with `is_member=True`. A "user not found" error means `False`; other errors raise
  `PlatformError`.
- `leave`: `leave_chat`.

**Error mapping:** wrap `telegram.error.TelegramError` into `PlatformError(type name)`. **Never
include the error text,** since it can contain URLs with the token. Handle `RetryAfter` with one
sleep and retry for `send`.

**Logging:** don't log update contents. The core already sets the `telegram`/`httpx` loggers to
WARNING. Add a test proving the token never appears in logs, even when a send fails.

## Tests (offline; no real Telegram)

Use PTB's offline objects: construct `Update`/`Message` objects via `de_json` with a fake bot, and
replace HTTP with a fake `telegram.request.BaseRequest` subclass that records calls and returns
canned JSON.

**Mapping:**
- group text;
- DM;
- a mention by entity;
- reply to the bot;
- a forum topic thread;
- each attachment type (largest photo chosen);
- member join and leave;
- the bot added;
- callback confirm and cancel;
- malformed callback data;
- edited messages ignored.

**Outgoing:**
- message splitting at 4096;
- link previews disabled on **every** send and edit;
- the inline keyboard data shape;
- the download size refusal before fetching;
- `is_member` status mapping;
- `PlatformError` carries no token or error text.

**Conformance:** the adapter is `isinstance(..., ChatPlatform)` and `Capabilities` are correct.

## Done when

- All offline tests pass and ruff is clean.
- A live test with a real bot token happens later, in Phase 2, with the user. Don't attempt one.
