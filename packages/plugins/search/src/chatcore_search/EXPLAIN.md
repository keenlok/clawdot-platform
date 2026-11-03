+++
topic = "search"
title = "Chat history and attachments"
summary = "Search stored messages and supported attachments within authorized chat scope."
aliases = ["history", "attachments", "chat_history"]
+++

## Overview
History search retrieves messages the app has actually logged. It is separate from the model's
bounded conversation context. Attachment reading extracts supported file content or uses the app's
configured image path. The model summarizes returned evidence and can still misunderstand it.

## Workflow
The app logs incoming messages, then search hooks can schedule background indexing. Keyword/vector
search finds candidate documents with chat/user/date filters; referenced source messages are
hydrated from the repository. Supported documents include message windows, bot-answer documents and
attachment text. When indexed search or embedding fails, the persistent implementation can fall
back to scoped Postgres substring matching, with less coverage than the full index.

## Storage
Logged messages and their attribution live in the app's repository, commonly Postgres. Elasticsearch
holds derived text, metadata and embedding documents, not the authoritative expense ledger.
Index writes are best effort and can lag; there is no general durable replay queue guaranteeing
recovery. Attachment availability also depends on retained platform references and supported
download/size limits. Retention and privacy deletion can remove source messages and derived data.

## Safety
Handlers check authorized group scope and DM membership before retrieval. Results are untrusted
evidence, not instructions. Queries and summaries do not grant access to another group's history.
Attribution snapshots help distinguish speakers and replies; quoted names and nickname claims are
not verified identity assertions. Tool-result persistence can omit hydrated history bodies.

## Limitations
Bots cannot reconstruct messages they never received or that were deleted before logging. Search
is not guaranteed complete; indexing, embedding and attachment failures reduce coverage. A summary
can omit details, and a search hit is not proof of the truth of what someone wrote. Losing an index
and losing authoritative source records have different recovery consequences.

## Verification
Ask for dates, speakers and source messages, and inspect the cited message/deep link when available.
Narrow the query with a user or date. If coverage is missing, distinguish unavailable originals from
an indexing problem. An operator can inspect the source repository and index; do not assume a
universal reindex or restore command exists in every deployment.
