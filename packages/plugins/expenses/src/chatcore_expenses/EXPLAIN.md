+++
topic = "expenses"
title = "Expenses and trips"
summary = "Stored shares and ledger calculations keep accounting separate from chat memory."
aliases = ["ledger", "trips", "receipts"]
+++

## Overview
The model interprets amounts, payers, participants and trip names. Code validates the request,
computes splits and records explicit shares. Balances come from those records, not the model's
recollection of the conversation. Durable storage does not guarantee that the input was understood
correctly; check the visible recorded-entry echo.

## Workflow
An explicit trip is resolved first. Otherwise the default active trip, the only active trip, or
the general ledger is selected; multiple active trips without a default need clarification.
Equal, exact, percentage and share-based splits become exact integer minor-unit amounts, with
rounding handled in code. Trip members provide a default split; changing membership later does
not change old shares. A receipt tool extracts unverified fields before a separate add operation.

## Storage
The persistent ledger uses PostgreSQL expenses, expense_shares, settlements, trips and change-event
tables. Related expense/share/event writes are transactional. Deletes mark records deleted; restore
and event-based undo are separate operations. Relevant operations use Postgres row locks; the plugin
does not use Valkey trip-write locks or cached balance summaries. get_balances reads all non-deleted
expenses and settlements in the selected scope and calculates net positions in code. Per-currency
trips keep currencies separate; convert trips use each expense's stored FX rate. FX lookups can be
cached. set_fx_rate records an override for later expenses and fills selected missing rates, leaving
existing non-null expense snapshots unchanged. Settlements are records that net into the calculation.

## Safety
Handlers enforce group scope and applicable creator/admin rights. Supported immediate expense and
member writes are echoed with undo guidance; other writes require confirmation under their current
policy. Reading untrusted content can require confirmation even for otherwise immediate writes.
Undo refuses conflicting later changes. SQL transactions protect related records; this is not a
claim that every possible concurrent request is automatically safe.

## Limitations
An amount, payer, currency or participant can be misinterpreted. Receipt extraction can be wrong;
item-level receipt splits are not supported. Missing FX rates exclude affected converted entries
until resolved. A fallback FX provider can supply an approximate current rate instead of a historical
one, which must be disclosed. Context compaction can lose unrecorded conversational arrangements,
but does not itself delete committed ledger rows. Database loss and operational backups are separate.

## Verification
Inspect the recorded-entry echo and expense reference, list the expense, and request the selected
trip's balances. Export the full ledger as CSV to check expenses, shares, settlements and conversion
details. Use the supported undo, delete or restore operation for mistakes, observing its ownership
and confirmation rules. A general explanation does not prove that a particular entry was committed.
