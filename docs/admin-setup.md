# Administration setup

Applications configure owner and group-scoped administration through core settings.
All IDs below are illustrative; provision real accounts in private deployment configuration.

## Bootstrap an owner

Set an immutable recovery owner in the bot environment:

```dotenv
OWNER_USERS=tg:123456789
ALLOWED_CHATS=
```

`OWNER_USERS` is a comma-separated list of positive Telegram user IDs. Owners can configure
groups and grant or revoke setup access. If `OWNER_USERS` is absent, the existing `ADMIN_USERS`
list remains the compatibility owner list. If `OWNER_USERS` is present, `ADMIN_USERS` users keep
their existing full administrator commands, but do not become owners or gain setup delegation.
An explicitly empty `OWNER_USERS` is rejected. There is no command to change owner identity;
update the environment and restart to recover or rotate an owner.

`ALLOWED_CHATS` remains the bootstrap list of groups the bot serves. A persisted `/admin disable`
override wins over an entry in that environment setting. An owner can start with no enabled group,
enable the desired group in a private conversation, then invite the bot.

For a Telegram group, use its stable negative numeric chat ID, such as `tg:-1001234567890`. Get
the ID from an existing trusted client or a Telegram ID lookup tool that you already trust. Check
that the returned chat ID is negative and belongs to the intended group, then remove any temporary
lookup bot. For a person, use the positive numeric Telegram user ID. Usernames, invite links,
Telegram group-admin status, and IDs from another platform do not grant bot permissions.

## Commands

Run `/admin` in a private chat with the bot to see the commands available to your role. Every
change shows its target and effect and waits for the requester to confirm. Confirmation buttons
are requester-only, single-use, and expire after ten minutes. No administrator can approve
another person's pending request. If another administration change lands before confirmation,
the old request is rejected and must be issued again.

```text
/admin groups
/admin admins
/admin moderators
/admin restrictions
/admin audit
/admin enable tg:-1001234567890
/admin disable tg:-1001234567890
/admin grant tg:123456789 tg:-1001234567890
/admin revoke tg:123456789 tg:-1001234567890
/admin mod tg:123456789 tg:-1001234567890
/admin unmod tg:123456789 tg:-1001234567890
/admin block tg:123456789 [tg:-1001234567890]
/admin pause tg:123456789 2h [tg:-1001234567890]
/admin unblock tg:123456789 [tg:-1001234567890]
/admin limit tg:123456789 50
/admin limit tg:123456789 default
/admin persona tg:-1001234567890 Friendly and concise
/admin persona tg:-1001234567890 reset
```

Only owners can grant or revoke setup access. A setup admin can enable or disable their assigned
groups and change persona there. They cannot grant access to other people, run
existing administrator-only destructive commands, or read another group's setup state or audit.
An assignment allows setup in a private chat; it does not add group membership or grant access to
stored messages, search results, or history. Ordinary data tools still require their existing
independent membership and scope checks.

Persona text is limited to 500 characters. The confirmation displays the full text, while the
audit records only the operation and group so it does not copy persona text into administrative
history. `/admin audit` shows the latest 20 entries visible to the caller.

Only owners can grant or revoke moderation. A moderator can block, pause, or unblock users only
in groups assigned to them; setup assignments alone do not grant moderation. Omitting the optional
group ID makes a block, pause, or unblock apply across that user's platform, including DMs. A
pause duration is from `1m` through `30d`. `/admin limit` sets an owner's per-person hourly request
limit from 1 through 10000; `default` removes the override and restores `RATE_USER_PER_HOUR`.
Owners cannot be restricted, and administrators cannot target themselves. Restrictions apply only
to SBot access; they do not ban or mute anyone in Telegram. A restricted person can still use
`/privacy` and `/forgetme` in a DM, including confirming a pending `/forgetme` action. Other
restricted requests receive a notice at most once every ten minutes per chat and person.

SBot replies in groups only when tagged or addressed through a command with its username.
Setup permissions cannot enable automatic group replies; `/admin chatmode` is unavailable.
Private setup commands do not need a bot tag.

## Enable, disable, and invite order

To invite a bot to a group that has not been configured yet, the owner first runs:

```text
/admin enable tg:-1001234567890
```

After confirming, invite the bot to that group. Enabling does not invite the bot or send a group
message. If the bot is added before the group is enabled, it leaves without logging or indexing
group content.

Disabling takes effect on the next group message or bot-added event. The bot leaves on that event.
If it has left, enabling the group again does not rejoin it; invite the bot again. An
assignment may be granted before the group is enabled, so a delegate can prepare group setup
without receiving message-history access.

Setup commands work in private chat only. Running `/admin` in a group does not mutate settings and
directs an authorized owner or delegate to the private chat. Topic-specific restrictions are
outside this feature.

## Talking to the bot in groups

Tag the bot to start a request, or reply to one of its messages to continue. A bare `@bot` uses
your most recent earlier message in the same topic as the request. If an addressed command is
unknown or unavailable to you, the bot points you to `/help@<bot>`.

Replying to any of the bot's messages continues that conversation, however old the reply is,
unless the topic was reset in the meantime. Tagging the bot again within an hour continues your
current conversation. Use `/new@<bot>` to start a fresh one, or `/new@<bot> question` to ask a
question in a fresh conversation. Each conversation keeps its own summary. `/compact@<bot>` compacts
the current conversation, and `/reset@<bot>` clears all conversations and summaries for the topic.
The continuation window defaults to 60 minutes and can be changed with
`CONVERSATION_CONTINUE_MINUTES` in the app environment.

## Resetting context

Owners and moderators assigned to that group can use `/reset` or `/compact` in groups; a private
chat user can use them in their own DM. In groups, address the bot as `/reset@example_bot` or
`@example_bot /reset` (and likewise for `/compact`); bare commands are ignored. `/reset` starts
fresh for the current chat or topic and keeps long-term memories. `/compact` summarizes older
conversation while keeping the last four turns. It can use the language model and is limited to
one run per chat or topic every five minutes, so it consumes the normal request and token limits.
Neither command deletes the message log or long-term memories. Use `/forgetme` to delete your own
data, and `/memory` and `/forget` to inspect or remove long-term memories.

## Reply attribution update (2026-10-31)

This build requires additive migration `0008_message_attribution.sql` after upstream migration
`0007_display_time.sql`; it adds no environment variables and preserves upstream timezone behavior.
Explicit replies can follow up to 32 reply links in the same chat and topic to find the original
mapped conversation; unmapped links are followed only when their stored message rows are present.
Chat, topic, reset and generation boundaries stop that walk; a personal handle or display name
never merges conversations.

The build snapshots each sender's display name and available username, verified mention targets,
and reply ID at the time a message arrives. Renaming a member does not rewrite earlier snapshots.
Legacy rows keep null handles and empty mention metadata; old conversation turns with no snapshot
are identified as unknown rather than joined to the current member list. A nickname such as
“Alice calls Bob Captain” stays an attributed statement rather than a verified identity.

Hydrated `read_chat_history` results are available to the provider during the current answer, but
the saved tool-result turn contains only `[Tool result omitted from persistent history.]`. This
also applies when a user reads an authorized group's history in a DM; it prevents retrieved
identity snapshots from becoming durable copies in the requester's conversation. Other tool
results and the original user and assistant turns retain their existing persistence.

`/forgetme` removes the requester's messages and linked turns, then clears normalized mention
metadata from surviving messages, turns and pending history buffers in that chat. Other people's
original message text and data in other chats remain unchanged.
