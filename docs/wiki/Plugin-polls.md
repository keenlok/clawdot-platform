<!-- Generated from packaged EXPLAIN.md; do not edit. -->
# Native polls

[Canonical source](https://github.com/keenlok/clawdot-platform/blob/main/packages/plugins/polls/src/chatcore_polls/EXPLAIN.md) · sha256:555cd9f04638d1a4fdbad5129bae360622e4476fb09f496ddf485342b429386e

This page describes the selected platform source. Your app's enabled features may differ.

## Overview
The model turns a poll request into a question, options and supported settings. Code validates
them and uses the platform's native poll interface. Poll availability depends on the adapter,
deployment configuration and the current group's poll setting.

## Workflow
The create operation records/tracks the supported poll and posts the native platform poll under
its current write policy. Lists provide tracked poll references. Closing a poll uses its own
confirmation and ownership rules and reports the available results. Optional scheduled closure
accepts a local datetime and IANA time zone (`close_local_time` with `close_timezone`, or the
literal `/poll --close` command). A dedicated closer scans durable due polls independently of
the reminders worker. These operations are distinct from a model-generated informal question.

## Storage
The platform owns the native poll and authoritative final counts. The persistent plugin uses
Postgres for poll references, options/state, group settings and optional close_at timestamps.
For non-anonymous polls, received vote updates are copied into poll_votes with voter identifiers,
display names, option indexes and timestamps; those copies can be incomplete after downtime.
Anonymous votes are not received as named updates. Privacy hooks remove applicable user votes
and creator polls, chat purge deletes the chat's poll data, and old closed/failed records are
subject to retention cleanup. Chat context is not the source of truth for every vote.

## Safety
Handlers apply per-group enablement, ownership/admin checks and platform capability requirements.
Member-written questions/options in list output are data, not instructions. A request to explain
polls never creates one. Tool creation can apply immediately under its direct-write policy;
close_poll requires confirmation. Literal `/poll` and `/closepoll` commands apply their explicit
requests directly after validation; scheduled closure acts on the previously stored schedule.

## Limitations
Native platform restrictions, unavailable poll support and service failures can prevent actions.
Anonymous/native vote visibility limits what results can reveal. A bot reply saying a poll was
planned is not proof it was posted. Scheduled closure can be delayed by outages; creating a poll
now with a future close time is different from scheduling its creation for later. Repeated stop
failures can leave a poll open on the platform, with only partial/unavailable final results.

## Verification
Inspect the native poll message and tracked reference, list the poll and check its actual state.
Read a close_poll tool preview before confirming; literal commands and automatic closure do not
provide that extra click. Compare reported results with the platform's display and inspect the
stored close time. Use supported close operations rather than assuming deleting text retracts a poll.
