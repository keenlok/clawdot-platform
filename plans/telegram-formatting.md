# Telegram formatting repair implementation plan

> Execute inline using the debugging, test-driven development and verification workflows. This repairs the user's reported live formatting defect under the existing deployment authorization.

Goal: render model Markdown as native Telegram text and entities, including safe splitting and edits.

Architecture: keep platform-independent replies as text; parse common Markdown only in the Telegram adapter. Use markdown-it-py's CommonMark token stream with raw HTML disabled, plus Telegram MessageEntity UTF-16 offsets. Confirmation prompts remain literal so users can review their exact contents. The Telegram adapter advertises its Markdown input capability; no interface signatures change.

Files:
- Add packages/adapter-telegram/src/chatcore_telegram/formatting.py for parsing, native entities and chunk boundaries.
- Update adapter send/edit methods and platform capability/documentation.
- Add focused formatter and actual Bot API payload tests; keep existing preview, reply/thread, retry and confirmation behavior.
- Add markdown-it-py dependency to the adapter and refresh the workspace lock.

Steps:
1. Reproduce current send/edit behavior with failed expectations for bold, inline/code blocks, links and headings. Add expected literal handling for raw HTML, special characters and confirmations.
2. Parse supported Markdown to native text/entities; represent unsupported structure readably. Exclude code/pre from overlapping styles, and preserve emoji with UTF-16 offsets.
3. Split rendered text by Telegram's UTF-16 limit and rebase/clamp every entity, retaining reply/thread metadata. Edits use the same rendering rules.
4. Verify malformed/unclosed Markdown, code across boundaries, long emoji messages, links, nested emphasis, ordinary underscores and unchanged literal confirmations. Verify serialized payloads through PTB FakeRequest.
5. Run focused tests, unit suite, lint, and service tests as appropriate; request code review before committing/deploying.
6. Deploy the clean committed source through the existing deploy script. Preserve the live DeepSeek V4.1 Flash/private-only configuration. Check health, runtime logs and a real API rendering result; update the deployment report.

Primary references: https://core.telegram.org/bots/api#formatting-options and https://markdown-it-py.readthedocs.io/en/latest/using.html .

Verification before deployment: 70 adapter tests passed; 504 unit tests passed with one
ShellCheck skip; 13 integration tests passed. Ruff and whitespace checks passed. Focused
review found and closed invalid/relative-link and nested image/link cases. Native Bot API
serialization is verified through PTB FakeRequest for sends and edits.
