# Conversation prompt repair

The user requested simple greetings and an Astra review of system prompts. Astra reviewed the assembled safety/app/persona prompt path, history replay, summaries, classifier and memory extraction. Main findings: hard-coded sb_bot/group identity, vague concision guidance, stale assistant introductions and capability claims not grounded in runtime facts.

Implement the correction inline under the existing application-fix/deployment authorization:
1. Replace app instructions with calibrated examples: short natural greetings/thanks; capability overview only when asked; follow-ups continue directly; substantive tasks retain needed detail.
2. Add optional app-only BOT_DISPLAY_NAME, falling back to the configured routing name. Inject trusted display identity without changing routing. The downstream application chooses its display name and account identity in private configuration.
3. Add per-turn generic framework context: bot account name, private/group conversation kind, current chat/topic reply scope and authorized registered tools. Supply the current group title only as untrusted message data. Persona remains tone-only. Do not query group membership for greetings or change tool/access/confirmation behavior. Group titles are never hard-coded; this change does not add topic-specific access controls.
4. Add failing tests for context/identity assembly and secret-free facts, then implement and run tests. Run the configured provider on synthetic greeting, stale-intro, thanks, capabilities, follow-up and substantive-task cases before and after. Behavioral outputs, not keyword routing or lower global token limits, determine acceptance.
5. Review the final diff, verify framework and downstream app tests, and preserve access/provider/native-rendering settings. Deployment remains downstream-owned. Record secondary prompt-policy findings separately.

Runtime verification corrected one review assumption: the current positive private-chat allowlist entry returns member from Telegram getChatMember, so existing group-backed resolver callbacks may resolve that private chat. This task does not change access or make false claims of unavailability; tool-specific scope remains a precondition to check when a feature is actually used.

Secondary findings recorded for follow-up rather than changing policy here: core's universal confirmation wording differs from memory's conditional confirmation/automatic extraction, extraction wording assumes groups, and summaries reuse the conversational system prompt. None is evidence for the reported greeting itself.
