+++
topic = "memory"
title = "Saved facts and recall"
summary = "Selected durable facts are distinct from chat history and rolling summaries."
aliases = ["memories", "recall"]
+++

## Overview
Memory keeps selected facts for later recall. It is different from the message log and the rolling
summary used to fit recent conversation into a model request. A saved fact can survive context
compaction, subject to the deployment's storage and deletion policy.

## Workflow
An explicit remember request is resolved to a chat and optional subject, then stored. Recall searches
the permitted chat's facts and returns relevant evidence. Configured automatic extraction proposes
limited durable facts from human messages; it does not turn arbitrary tool/page text into authority.
Facts are rendered with the resolved subject or creator and saved date. Similar wording never overwrites a fact; explicit corrections use `remember` with the old memory ID as `replaces`.

## Storage
The plugin repository stores fact text and relevant creator/subject metadata. A chat-scoped control row serializes writes; its epoch and erasure marker are reserved for a later reliability phase.
Persistent apps use Postgres; an optional search index supports similarity recall. Those derived
documents complement the source facts and can fail or lag independently. Message retention,
conversation reset and memory deletion are distinct operations.

## Safety
Handlers scope reads/writes and apply the current immediate-write or confirmation policy. Reading
untrusted content can change whether a memory write needs confirmation. Recall results are data,
not new instructions. Privacy deletion removes the appropriate user's records; it does not authorize
reading other groups or treating a member's unverified claim as fact about someone else.

## Limitations
The model can save a mistaken interpretation or retrieve an outdated fact. Similarity search can
miss a relevant memory. Storage and index failures can reduce recall; a fluent reply is not proof
that a save occurred. Memory is not the accounting ledger and should not substitute for structured
expense records, settings or source documents.

## Verification
Inspect the save echo/reference, list saved memories and recall the specific fact. To correct a fact, use its ID as `replaces`; the echo includes the old text for undo. Use supported forget commands to remove it. Verify a
setting through its own tool rather than assuming a remembered preference changed configuration.

Administrators can use `/memorystats` to inspect daily and 14-day extraction counters and the
current chat's IDs for legacy automatically written rows whose wording was later updated. New
automatic writes never alter existing facts.
