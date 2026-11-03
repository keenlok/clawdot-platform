# Deep research

`chatcore-research` adds sourced, multi-search reports to SBot. It uses the existing web plugin,
so page fetches keep the shared SSRF protection and turn-local URL provenance checks. Research
offers the model only `search_web` and `fetch_web_page`; web results are treated as untrusted data.

## Commands

- `/research <topic>` runs a bounded inline report. In groups, address it as
  `/research@<bot_name> <topic>`.
- `/research --long <topic>` queues a durable report. SBot posts it later to the same chat and
  thread. The reply includes commands for checking or cancelling it.
- `/research status` lists up to five of your jobs in the current chat.
- `/research cancel` cancels your queued or running job in the current chat.

The main agent can call its `research` tool for questions that need several searches or sources
checked against each other. A tool run posts the report later only if its inline time limit is
reached and it can queue a job.

## Cost controls

All settings use the `RESEARCH_` prefix. Defaults are validated at startup:

| Setting | Default | Purpose |
|---|---:|---|
| `RESEARCH_PER_USER_PER_DAY` | 5 | Starts per person across chats |
| `RESEARCH_PER_CHAT_PER_DAY` | 20 | Starts per chat |
| `RESEARCH_INLINE_MAX_SEARCHES` / `RESEARCH_INLINE_MAX_FETCHES` | 6 / 8 | Inline source limits |
| `RESEARCH_INLINE_TIMEOUT_S` | 90 | Command and mention time cap |
| `RESEARCH_TOOL_TIMEOUT_S` | 30 | Main agent tool time cap |
| `RESEARCH_INLINE_TOKEN_CEILING` | 150000 | Inline token ceiling |
| `RESEARCH_LONG_MAX_SEARCHES` / `RESEARCH_LONG_MAX_FETCHES` | 12 / 20 | Per-attempt source limits |
| `RESEARCH_LONG_TIMEOUT_S` | 600 | Per-attempt time cap |
| `RESEARCH_LONG_TOKEN_CEILING` | 400000 | Token ceiling across job attempts |
| `RESEARCH_MAX_ATTEMPTS` | 2 | Maximum attempts after interruptions |
| `RESEARCH_MAX_TOKENS` | 2048 | Output tokens per completion |
| `RESEARCH_PAGE_CHARS` | 12000 | Maximum fetched-page text kept in research context |
| `RESEARCH_POLL_SECONDS` | 2 | Background worker poll interval |
| `RESEARCH_WORKER_CONCURRENCY` | 1 | Concurrent background workers |
| `RESEARCH_RETENTION_DAYS` | 30 | Retention for terminal jobs |
| `RESEARCH_TOPIC_MAX_CHARS` | 500 | Maximum command or tool topic length |

The global daily token budget is shared with normal SBot conversations. One active research per
person is allowed across chats. One inline run that reaches its time cap and becomes a job counts
as one start.

## Operations and privacy

`ResearchAccess.can_research(chat, user)` is checked at start, when a worker claims a job, and
before delivery. SBot currently supplies `AllowAllResearch` as a seam; connect the planned
per-user restriction store when it becomes available.

Postgres is the source of truth when configured. The in-memory job store is for local console use
and is not durable across restarts. A job's delivery is reserved before sending. If a send result
is unknown, the job is marked unknown and is not replayed, avoiding duplicate reports.

Topics and seed URLs are kept in job rows until deletion or retention. `/forgetme` purges a user's
research jobs in that chat; `/purgechat` removes all research jobs for that chat. Terminal rows
are purged after the configured retention period. Research reports are logged in the requested
chat's message history, where normal chat retention and purge rules apply.
