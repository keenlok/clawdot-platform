+++
topic = "extensions"
title = "Reviewed sandboxed extensions"
summary = "Requested JSON tools require review and activation before constrained execution."
aliases = ["sandbox", "extension_tools"]
+++

## Overview
Extensions add reviewed tools for constrained JSON transformations. A request is not an immediately
installed tool: its examples and intended behavior are confirmed, then the configured development,
review and activation workflow determines what can run. Availability depends on deployed controller
and worker components.

## Workflow
A requester submits a described tool and examples through the confirmation flow. The system records
the request/revision and exposes reviewed, active tools through listing/execution operations.
Execution sends validated input to the configured sandbox runtime, then returns a bounded result.
Monitoring and revocation can stop a revision being used later.

## Storage
Extension requests, revisions, activation and relevant audit state use the configured extension
repository. Runtime artifacts and worker state have separately managed storage. A chat message
containing code is not itself a reviewed active artifact. Explain documentation does not inspect
private extension source or supply an execution history.

## Safety
Requester confirmation, review, activation checks, schemas and sandbox restrictions form separate
boundaries. Pure JSON transformations are not unrestricted shell/network/database access. Execution
checks the selected revision and current policy. Release-control services are separate from these
tools and can remain inactive even when extension tooling is configured.

## Limitations
Review and sandboxing reduce risk but do not prove arbitrary submitted code is correct. Unsupported
operations, resource limits, revoked revisions and worker outages can prevent execution. Tool output
can be wrong. Never promise that an extension can silently modify the deployed bot or bypass a gate.

## Verification
Inspect the confirmed request/examples, reviewed revision and active-tool listing. Compare execution
results with expected inputs/outputs and inspect supported status/review information. Report a bad
revision for revocation rather than treating model-generated code as trusted by default.
