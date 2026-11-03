<!-- Generated from packaged EXPLAIN.md; do not edit. -->
# Reminders and website watches

[Canonical source](https://github.com/keenlok/clawdot-platform/blob/main/packages/plugins/jobs/src/chatcore_jobs/EXPLAIN.md) · sha256:935388f99d230f3d0b6051aaaa23b80d6fc690720588df3931f4911fdb5e37c2

This page describes the selected platform source. Your app's enabled features may differ.

## Overview
Reminder/watch tools create structured jobs. A worker checks due records and sends permitted
notifications. The model interprets scheduling requests, but a stored schedule drives execution;
the model does not have to remember to wake up later. The app must configure the worker and storage.

## Workflow
Handlers resolve the chat/topic, owner, time zone and schedule or allowed website URL. Supported
job changes apply under their declared immediate-write/confirmation policy and return an echo.
The worker claims due jobs, checks current authorization and reserves a delivery before sending.
Watches compare successful bounded reads with their prior snapshots; repeated failures are reported
with bounded retries/backoff rather than silently replacing a good snapshot with an error page.

## Storage
Persistent jobs, scope settings, watch snapshots and delivery occurrences live in Postgres.
Worker leases and durable reservation records coordinate execution. Valkey may support wider app
counters, but it is not a replacement for the scheduled-job ledger. Terminal-record retention and
privacy deletion differ from active future-job retention.

## Safety
Job tools and worker execution check scope, ownership, app/group toggles and applicable roles.
Website reads use URL/network restrictions and untrusted-content handling. Cancellation committed
before delivery reservation prevents that notification. After reservation, a notification already
starting can still arrive; accepted platform messages cannot be retracted by a later purge.

## Limitations
Delivery is not exactly once. A crash or timeout after reservation can leave a send unknown;
unknown sends are not automatically replayed, preventing deliberate duplicates but risking a missed
notification. Platform, network or worker outages can delay/fail delivery. A scheduled record does
not prove its notification was received. Rendered watches require separate renderer availability.

## Verification
Check the echoed job ID, schedule/time zone and topic. List reminders or watches and inspect status,
including failed/unknown delivery states. Cancel or restore by the supported reference and ownership
rules. Inspect worker/deployment health for missing sends rather than relying on a conversational promise.
