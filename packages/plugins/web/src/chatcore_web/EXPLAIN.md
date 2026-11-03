+++
topic = "web"
title = "Web search and page fetching"
summary = "Search discovers sources; fetching reads bounded content from permitted URLs."
aliases = ["web_search", "web_pages"]
+++

## Overview
Search and fetch tools obtain external evidence for the model. Search returns candidate sources;
fetching reads a selected permitted page. Neither operation makes a source reliable or gives the
model a complete view of the internet. Providers and credentials are selected by the app.

## Workflow
A query is submitted to the configured search service. Fetch accepts URLs supplied by humans or
returned by permitted search, applies network/redirect checks and extracts bounded page text.
JavaScript-only or inaccessible pages may need a separately enabled renderer. Returned text is
untrusted evidence that the model must evaluate and cite.

## Storage
Search/fetch can use application cache entries and retained page text according to the current
implementation. Temporary cached responses are not an authoritative archive or a guarantee that
the page is unchanged. Provider records and the application message log are separate from original
web content. Fetching a page does not save its claims as long-term memory automatically.

## Safety
URL provenance and guarded network access limit destinations, redirects and response size. Pages
cannot authorize writes or override user requests. Secrets and chat contents must not be inserted
into arbitrary URLs. A URL appearing inside fetched content is not automatically a new permitted
destination. Handlers enforce these rules even when the model proposes a call.

## Limitations
Paywalls, login requirements, rate limits, robots restrictions, JavaScript and extraction errors
can prevent useful reads. Search snippets can omit context and pages can change. A source can be
wrong or malicious. The model must not invent quotes, links or facts when a read fails.

## Verification
Open cited original sources, compare dates and inspect whether the retrieved text supports the
claim. Ask which page was actually fetched and whether it was truncated or unavailable. For complex
multi-source work, use the configured research capability rather than assuming one snippet is enough.
