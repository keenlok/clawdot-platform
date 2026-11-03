+++
topic = "research"
title = "Multi-source research"
summary = "A bounded research workflow gathers sources and produces a cited report."
aliases = ["research_reports"]
+++

## Overview
Research is a bounded multi-source workflow for questions needing several searches or comparisons.
It gathers evidence through configured web tools, then produces a report with source references.
The model organizes and summarizes evidence; citations do not automatically make every claim true.

## Workflow
The plugin checks access, quotas and budgets before starting. Work can complete inline or use
the supported background handoff. Searches and permitted fetches gather source material under
call/time/output limits. Background work checks authorization again before delivery. A single
research request is preferable to repeatedly starting competing jobs for the same question.

## Storage
Background job/status data uses the app-selected research-job repository, with persistent or
in-memory implementations. Source pages, temporary state and provider usage have their own app
storage/retention paths. A completed report is not a durable replayable archive of the entire web
or proof that a notification was received.

## Safety
Research checks requester scope, restrictions, concurrency and quotas. Web content is untrusted,
and URL provenance/network protections still apply. Gathering evidence never authorizes expense,
file or other mutations. A background job can lose authorization before its final send.

## Limitations
Source quality, blocked pages, search coverage, model mistakes and bounded budgets limit completeness.
Work can time out or fail, and reports may omit important contrary evidence. A job being accepted
is different from completion or delivery. External pages can change after the report is written.

## Verification
Inspect the report's numbered sources and whether each supports its claim. Compare independent
primary sources and event/publication dates where relevant. Check research status for background
work and distinguish incomplete, failed, cancelled and delivered results.
