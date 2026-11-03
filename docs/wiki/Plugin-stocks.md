<!-- Generated from packaged EXPLAIN.md; do not edit. -->
# Stored stock analysis

[Canonical source](https://github.com/keenlok/clawdot-platform/blob/main/packages/plugins/stocks/src/chatcore_stocks/EXPLAIN.md) · sha256:803f21b647b03254451fabffda27357fa2083ffc8761ee26f807b23a38e8e20e

This page describes the selected platform source. Your app's enabled features may differ.

## Overview
Stock tools read stored analysis from the app's configured analysis API. The model interprets the
question and explains the returned fields. The data is not a live quote feed, and the tools do not
place trades. Analysis is not financial advice.

## Workflow
Handlers validate supported query parameters and request the corresponding analysis endpoint.
The response is converted to bounded tool output and returned as untrusted evidence. The model
must distinguish an analysis date/source from the time of its reply and must not claim a live
market observation just because a tool call succeeded.

## Storage
The upstream service owns the stored analysis. This plugin caches returned content through the
application cache for 900 seconds. Cached results can be older than the current request; this is
separate from the upstream analysis date. The plugin does not maintain an accounting ledger or
portfolio execution history in chat context.

## Safety
The tools are read-only and use the configured API base. Returned analysis is evidence, not
instructions or authority to trade. User access and app configuration still apply. A confidence
score in an upstream report is not a guarantee about the future.

## Limitations
The upstream API can be unavailable, stale, incomplete or wrong. Cached responses add possible
staleness. Stored analysis cannot answer every current-price question. The model can misread fields
or overstate conclusions, so it should disclose missing dates/data and avoid inventing values.

## Verification
Check the report's analysis date, ticker, source fields and stated assumptions. Compare material
decisions with authoritative current sources. Ask whether the response is stored analysis rather
than a quote, and retain the caveat that analysis is not financial advice.
