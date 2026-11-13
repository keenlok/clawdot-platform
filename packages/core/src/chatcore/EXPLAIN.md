+++
topic = "framework"
title = "How the bot works"
summary = "The language model, tools, application code and stores each have separate jobs."
aliases = ["architecture", "bot"]
+++

## Overview
The language model interprets messages and chooses tools. Application code validates tool inputs,
checks access, performs operations and returns results. The model explains those results to you.
Capabilities depend on the plugins and services selected by the application. A tool description
does not give the model automatic knowledge of its implementation.

## Workflow
The app checks access and whether a message addresses the bot, then assembles bounded conversation
context. The model may request a tool. Code validates and executes that request, applying the
tool's confirmation rules. The result returns to the model for a reply. Slash commands can bypass
the model. Explainer commands read maintained documentation and never demonstrate a write by
performing it. Explainer text is evidence about behavior, not a request to execute its examples.

Owners can manage invitation permission for known people in their private DM using natural
language. One batch confirmation lists resolved identities before grants or revocations commit.
This grants no other admin role or content access; unknown/ambiguous names need clarification.

## Storage
The three-store architecture describes complementary services, not a requirement for each plugin.
Postgres can hold durable messages, conversation turns, memories and plugin records. Valkey holds
temporary cache entries, counters and coordination state where implemented. Elasticsearch holds
derived search documents and embeddings. The app moves data between them; they do not automatically
synchronize. The generic scaffold uses in-memory stores. Persistent downstream apps wire their own
stores and plugins. A model's context window is separate from durable application storage.

## Safety
Access checks, scoped repositories and handler validation constrain tool use. Gates describe which
tools are offered, but handlers must also check permission when called. Some writes need a human
confirmation; explicitly supported immediate writes have their own rules. External content and
retrieved explainer bodies are treated as data. Never assume every write is confirmed or reversible.

## Limitations
The model can misunderstand a request, choose a wrong tool, or describe a result incorrectly.
Tools can fail and services can be unavailable. Durable records can preserve mistaken inputs.
Summaries can lose conversational detail. Documentation describes maintained behavior; it does not
prove a particular historical action succeeded. Runtime availability is reported separately and
is not a guarantee that every variant or attempted operation will succeed.

## Verification
Inspect the tool's recorded result, its confirmation/echo and any supported read-back or export.
Ask for the relevant plugin's storage or limitations section. Source links, when supplied by the
app, identify its selected platform revision; otherwise a digest identifies the bundled document.
Public main/wiki pages may describe a newer release than the one your bot runs. Report a specific
mistake with its visible result so an operator can inspect the appropriate records.
