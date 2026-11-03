# Group mention-only reply policy

Goal: keep SBot silent on untagged group messages, including forum topics, while DMs continue normally. A native bot mention or an explicitly bot-addressed slash command qualifies. Untagged replies to bot messages, bare slash commands and chatmode classifiers do not trigger group replies. Existing pending-action confirmation buttons/codes remain deliberate interactions. Logging, ingestion, access and privacy lifecycle notices are unchanged.

Use a framework BotApp/routing opt-in group_mentions_only flag (default false for existing framework consumers), enabled unconditionally by sb_bot. Hide the incompatible chatmode command in this app with a ConversationPlugin enable_chatmode option; an old stored chatmode=true cannot bypass the routing flag. No system-prompt classifier is used for this decision.

- [x] Add red application tests for ordinary/reply/command messages with stale chatmode=true; verify zero LLM calls/sends and continued ingestion, then tagged mentions and addressed commands respond within the topic.
- [x] Add private-chat and core routing regression coverage, plus confirmation compatibility.
- [x] Implement minimal routing/composition flags and disable SBot chatmode exposure; 537 unit tests passed (1 ShellCheck skip), 10 integration tests passed (3 ES checks skipped after local OOM). Ruff and changed Python formatting clean. Group help/join notice use addressed commands.
- [x] Independent review found no blockers, including help/notice command targeting and confirmation compatibility. Commit/deployment follows verification.
- [x] Deployed 5d4523b4b058; installed routing/composition smoke passed with no Telegram delivery. Service healthy, zero restarts/OOM/errors; private-only access preserved; rollback e30c9d1d4279 retained. Deployment notes updated.
